"""
일일 분봉 백테스트 데이터 수집기 (장 마감 후 자동 실행).

KIS 분봉 API는 한 번 호출에 30봉만 주므로, 시각을 09:30~15:30까지 30분 간격으로
13번 호출해서 당일 전체 ~390봉을 수집한다 (중복 제거).

저장 구조 (하이브리드):
  ─── 고정 20종목 (가격필터 50k~300k 통과, 추세 추적용) ───
  - 일자별:  data/ohlcv/minute_archive/{code}_{YYYYMMDD}.csv
  - 주차별:  data/ohlcv/weekly/{code}_minute_{YYYY-Www}.csv  ← ISO 주차
              매주 월요일 새 파일 자동 생성, 이전 주 파일 보존

  ─── 동적 거래대금 상위 5종목 (당일 핫한 종목, 단발 분석용) ───
  - 일자별:  data/ohlcv/top_volume/{code}_{YYYYMMDD}.csv
              매일 다른 종목이므로 주차 누적은 X

사용:
    # 기본: 20 고정 + 5 동적 = 25종목
    python scripts/fetch_daily_backtest_data.py

    # 동적 수집 끄기 (고정 20종목만)
    python scripts/fetch_daily_backtest_data.py --top-volume-count 0

    # 특정 종목만 (동적 비활성화)
    python scripts/fetch_daily_backtest_data.py --codes 005930,000660 --top-volume-count 0

GHA mock-trade.yml 의 'Run AutoTrader' 다음 단계로 실행되며,
data/ohlcv/ 경로가 캐시되어 매일 누적된다.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# 프로젝트 루트를 sys.path에 추가 (스크립트 단독 실행 시)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.constants import (
    INTRADAY_DYNAMIC_MAX_PRICE,
    INTRADAY_DYNAMIC_MIN_PRICE,
)
from config.settings import settings
from core.kis_api_client import KISApiClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# 고정 20종목: 가격필터 50k~300k 통과 + 거래대금 상위 + 다양한 섹터
# 매일 같은 종목 추적 → 주차별 누적이 의미있게 작동
FIXED_CODES: list[tuple[str, str]] = [
    # 반도체/IT (4)
    ("005930", "삼성전자"),
    ("000660", "SK하이닉스"),
    ("042700", "한미반도체"),
    ("035420", "NAVER"),
    # 자동차/모빌리티 (3)
    ("000270", "기아"),
    ("012330", "현대모비스"),
    ("035720", "카카오"),
    # 2차전지 (3)
    ("086520", "에코프로"),
    ("247540", "에코프로비엠"),
    ("003670", "포스코퓨처엠"),
    # 바이오 (1)
    ("068270", "셀트리온"),
    # 금융 (2)
    ("105560", "KB금융"),
    ("055550", "신한지주"),
    # 철강/에너지 (2)
    ("005490", "POSCO홀딩스"),
    ("096770", "SK이노베이션"),
    # 방산/조선 (1)
    ("047810", "한국항공우주"),
    # 통신/인프라 (1)
    ("017670", "SK텔레콤"),
    # 지주/기타 (1)
    ("028260", "삼성물산"),
    # 엔터/게임 (2)
    ("251270", "넷마블"),
    ("035900", "JYP Ent."),
]

DEFAULT_FIXED_CODES = [code for code, _ in FIXED_CODES]
NAME_MAP = dict(FIXED_CODES)

# 13개 시각 (09:30~15:30, 30분 간격) — 각 호출이 직전 30봉 반환
PAGINATION_TIMES = [
    "153000", "150000", "143000", "140000", "133000", "130000",
    "123000", "120000", "113000", "110000", "103000", "100000", "093000",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="일일 분봉 백테스트 데이터 수집기")
    p.add_argument(
        "--codes", default=",".join(DEFAULT_FIXED_CODES),
        help=f"고정 수집 종목코드 콤마 구분 (default: {len(DEFAULT_FIXED_CODES)}종목)",
    )
    p.add_argument(
        "--top-volume-count", type=int, default=5,
        help="당일 거래대금 상위 N종목 추가 수집 (default: 5, 0=비활성)",
    )
    p.add_argument(
        "--market", default="J",
        help="시장 코드 (J=코스피, Q=코스닥)",
    )
    p.add_argument(
        "--no-append-weekly", action="store_true",
        help="주차별 CSV 누적 안 함 (일자별 파일만)",
    )
    p.add_argument(
        "--throttle-sec", type=float, default=1.2,
        help="페이지네이션 호출 간 대기 시간 (KIS 1req/s 안전마진)",
    )
    return p.parse_args()


def fetch_prev_close(api: KISApiClient, code: str, market: str) -> int:
    """전일 종가 조회 (분봉에 prev_close 컬럼으로 추가)."""
    end = datetime.now()
    start = end - timedelta(days=10)
    try:
        rows = api.get_daily_ohlcv(
            code=code,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            market=market,
            adj_price=True,
        )
        if len(rows) >= 2:
            return int(rows[-2]["close"])
        if len(rows) == 1:
            return int(rows[-1]["close"])
    except Exception as exc:
        logger.warning("[%s] prev_close 조회 실패: %s", code, exc)
    return 0


def fetch_full_day_minutes(
    api: KISApiClient, code: str, market: str, throttle_sec: float,
) -> list[dict]:
    """13개 시각 페이지네이션으로 당일 전체 분봉 수집 (중복 제거)."""
    seen_times: set[str] = set()
    all_rows: list[dict] = []
    for time_str in PAGINATION_TIMES:
        try:
            rows = api.get_minute_ohlcv(code=code, time_str=time_str, market=market)
        except Exception as exc:
            logger.warning("[%s] %s 분봉 호출 실패: %s", code, time_str, exc)
            time.sleep(throttle_sec)
            continue
        for r in rows:
            t = str(r.get("time", "")).zfill(6)
            if not t or t in seen_times:
                continue
            seen_times.add(t)
            all_rows.append(r)
        time.sleep(throttle_sec)
    return all_rows


def fetch_top_volume_codes(
    api: KISApiClient, n: int, exclude: set[str], throttle_sec: float,
) -> list[tuple[str, str]]:
    """
    당일 거래대금 상위 N종목 조회 (가격필터 50k~300k 적용, exclude 종목 제외).
    Returns: [(code, name), ...]
    """
    if n <= 0:
        return []
    try:
        ranked = api.get_volume_rank(top_n=50)
    except Exception as exc:
        logger.warning("[TopVolume] volume_rank 조회 실패: %s", exc)
        return []
    time.sleep(throttle_sec)

    selected: list[tuple[str, str]] = []
    for row in ranked:
        code = row.get("code", "").strip()
        if not code or code in exclude:
            continue
        try:
            price = int(row.get("price", 0) or 0)
        except (ValueError, TypeError):
            continue
        if not (INTRADAY_DYNAMIC_MIN_PRICE <= price <= INTRADAY_DYNAMIC_MAX_PRICE):
            continue
        if str(row.get("upbnd_yn", "")) == "Y" or str(row.get("lwbnd_yn", "")) == "Y":
            continue
        name = row.get("name", code)
        selected.append((code, name))
        if len(selected) >= n:
            break
    return selected


def normalize_rows(rows: list[dict], code: str, prev_close: int) -> pd.DataFrame:
    if not rows:
        raise ValueError(f"[{code}] 분봉 응답 비어있음")

    today = datetime.now().strftime("%Y-%m-%d")
    df = pd.DataFrame(rows)
    df["time"] = df["time"].astype(str).str.zfill(6)
    df["date"] = today
    df["datetime"] = pd.to_datetime(
        df["date"] + " " + df["time"].str.slice(0, 2) + ":" + df["time"].str.slice(2, 4)
    )
    df = df.sort_values("datetime").drop_duplicates("time").reset_index(drop=True)
    df["code"] = code
    df["prev_close"] = prev_close
    return df[[
        "date", "time", "datetime", "code",
        "open", "high", "low", "close", "volume", "prev_close",
    ]]


def save_daily(df: pd.DataFrame, code: str, date_str: str, subdir: str) -> Path:
    """일자별 저장 (subdir: 'minute_archive' 또는 'top_volume')."""
    out_dir = settings.DATA_DIR / "ohlcv" / subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{code}_{date_str}.csv"
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out_path


def append_weekly(df: pd.DataFrame, code: str) -> tuple[Path, str]:
    """주차별 CSV에 append (ISO 주차 기준 — 같은 날짜 자동 교체)."""
    iso_year, iso_week, _ = datetime.now().isocalendar()
    week_id = f"{iso_year}-W{iso_week:02d}"

    out_dir = settings.DATA_DIR / "ohlcv" / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    weekly_path = out_dir / f"{code}_minute_{week_id}.csv"

    if weekly_path.exists():
        existing = pd.read_csv(weekly_path, dtype={"time": str})
        new_date = df["date"].iloc[0]
        existing = existing[existing["date"] != new_date]
        combined = pd.concat([existing, df], ignore_index=True)
    else:
        combined = df

    combined = combined.sort_values(["date", "time"]).reset_index(drop=True)
    combined.to_csv(weekly_path, index=False, encoding="utf-8-sig")
    return weekly_path, week_id


def collect_one(
    api: KISApiClient, code: str, name: str, market: str, throttle_sec: float,
    date_str: str, subdir: str, append_weekly_flag: bool,
) -> dict:
    """단일 종목 수집 → 결과 dict."""
    try:
        prev_close = fetch_prev_close(api, code, market)
        time.sleep(throttle_sec)
        rows = fetch_full_day_minutes(api, code, market, throttle_sec)
        df = normalize_rows(rows, code, prev_close)

        save_daily(df, code, date_str, subdir)
        week_id = "-"
        week_days = 0
        if append_weekly_flag:
            wp, week_id = append_weekly(df, code)
            try:
                week_days = pd.read_csv(wp, dtype={"time": str})["date"].nunique()
            except Exception:
                pass

        return {
            "code": code, "name": name, "rows": len(df),
            "time_range": f"{df['time'].min()}~{df['time'].max()}",
            "prev_close": prev_close,
            "week_id": week_id, "week_days": week_days, "status": "OK",
        }
    except Exception as exc:
        logger.error("[%s] ✗ 수집 실패: %s", code, exc)
        return {
            "code": code, "name": name, "rows": 0, "time_range": "",
            "prev_close": 0, "week_id": "-", "week_days": 0,
            "status": f"FAIL: {exc}",
        }


def print_results(title: str, results: list[dict]) -> None:
    print()
    print("=" * 100)
    print(title)
    print("=" * 100)
    print(f"{'종목':<8}{'이름':<14}{'봉수':<6}{'시간범위':<18}{'전일종가':<12}{'주차':<12}{'일수':<6}상태")
    print("-" * 100)
    for r in results:
        name = (r["name"] or "")[:12]
        print(
            f"{r['code']:<8}{name:<14}{r['rows']:<6}{r['time_range']:<18}"
            f"{r['prev_close']:<12,}{r['week_id']:<12}{r['week_days']:<6}{r['status']}"
        )


def main() -> None:
    args = parse_args()
    fixed_codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    if not fixed_codes:
        logger.error("종목코드가 비어있습니다")
        sys.exit(1)

    total_estimate = (len(fixed_codes) + args.top_volume_count) * 14 * args.throttle_sec
    logger.info(
        "수집 시작 | 고정 %d종목 + 동적 top %d종목 (예상 ~%d초)",
        len(fixed_codes), args.top_volume_count, int(total_estimate),
    )

    api = KISApiClient()
    api.init()
    date_str = datetime.now().strftime("%Y%m%d")

    # ── 1단계: 고정 종목 수집 (일자별 + 주차별) ──────────────────────
    fixed_results: list[dict] = []
    for i, code in enumerate(fixed_codes, 1):
        name = NAME_MAP.get(code, code)
        logger.info("[고정 %d/%d] %s (%s) 수집 중...", i, len(fixed_codes), code, name)
        r = collect_one(
            api, code, name, args.market, args.throttle_sec,
            date_str, "minute_archive", not args.no_append_weekly,
        )
        if r["status"] == "OK":
            logger.info(
                "[%s] ✓ %d봉 | %s 누적 %d일/5",
                code, r["rows"], r["week_id"], r["week_days"],
            )
        fixed_results.append(r)

    # ── 2단계: 거래대금 상위 N종목 동적 수집 (일자별만) ──────────────
    dynamic_results: list[dict] = []
    if args.top_volume_count > 0:
        exclude = set(fixed_codes)
        logger.info("거래대금 상위 %d종목 조회 중 (가격필터 50k~300k)...",
                    args.top_volume_count)
        top_codes = fetch_top_volume_codes(
            api, args.top_volume_count, exclude, args.throttle_sec,
        )
        if not top_codes:
            logger.warning("거래대금 상위 종목 0개 (KIS 모의 한도 또는 필터 미통과)")
        else:
            logger.info(
                "거래대금 상위: %s",
                ", ".join(f"{c}({n})" for c, n in top_codes),
            )
            for i, (code, name) in enumerate(top_codes, 1):
                logger.info("[동적 %d/%d] %s (%s) 수집 중...",
                            i, len(top_codes), code, name)
                # 동적 종목은 주차별 누적 X (매일 다른 종목)
                r = collect_one(
                    api, code, name, args.market, args.throttle_sec,
                    date_str, "top_volume", append_weekly_flag=False,
                )
                if r["status"] == "OK":
                    logger.info("[%s] ✓ %d봉 (동적, 일자별만)", code, r["rows"])
                dynamic_results.append(r)

    # ── 요약 출력 ────────────────────────────────────────────────────
    print_results(
        f"고정 종목 수집 결과 ({date_str}) — 일자별 + 주차별 누적",
        fixed_results,
    )
    if dynamic_results:
        print_results(
            f"거래대금 상위 동적 수집 결과 ({date_str}) — 일자별만",
            dynamic_results,
        )

    fixed_ok = sum(1 for r in fixed_results if r["status"] == "OK")
    dyn_ok = sum(1 for r in dynamic_results if r["status"] == "OK")
    total = len(fixed_results) + len(dynamic_results)
    print()
    print(f"성공: 고정 {fixed_ok}/{len(fixed_results)} + 동적 {dyn_ok}/{len(dynamic_results)} = {fixed_ok + dyn_ok}/{total}")

    week_id = next((r["week_id"] for r in fixed_results if r["status"] == "OK"), "YYYY-Www")
    sample_code = fixed_codes[0]
    print()
    print("다음 단계:")
    print(f"  # 이번 주 누적 데이터 백테스트")
    print(
        f"  python run_intraday_backtest.py "
        f"--csv data/ohlcv/weekly/{sample_code}_minute_{week_id}.csv "
        f"--strategy S1 --code {sample_code}"
    )
    print()
    print("저장 위치:")
    print("  - 고정 일자별:  data/ohlcv/minute_archive/{code}_{YYYYMMDD}.csv")
    print("  - 고정 주차별:  data/ohlcv/weekly/{code}_minute_{YYYY-Www}.csv")
    print("  - 동적 일자별:  data/ohlcv/top_volume/{code}_{YYYYMMDD}.csv")


if __name__ == "__main__":
    main()
