"""
일일 분봉 백테스트 데이터 수집기 (장 마감 후 자동 실행).

KIS 분봉 API는 한 번 호출에 30봉만 주므로, 시각을 09:30~15:30까지 30분 간격으로
13번 호출해서 당일 전체 ~390봉을 수집한다 (중복 제거).

저장 위치 (주차별 분리):
- 일자별:  data/ohlcv/minute_archive/{code}_{YYYYMMDD}.csv
- 주차별:  data/ohlcv/weekly/{code}_minute_{YYYY-Www}.csv  ← ISO 주차
            예: 2026-W19 (2026년 19번째 주, 월~금 5거래일 누적)
            매주 월요일 자동으로 다음 주 파일 생성됨

사용:
    # 기본 10종목
    python scripts/fetch_daily_backtest_data.py

    # 특정 종목만
    python scripts/fetch_daily_backtest_data.py --codes 005930,000660

    # 주차별 누적 안 하고 일자별 파일만
    python scripts/fetch_daily_backtest_data.py --no-append-weekly

GHA mock-trade.yml 의 'Run AutoTrader' 다음 단계로 실행되며,
data/ohlcv/ 경로가 캐시되어 매일 누적된다 (5일=일주일치 = 한 파일).
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

from config.settings import settings
from core.kis_api_client import KISApiClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# 기본 종목 (가격대/섹터 다양화 — 50k~300k 필터에 들어오는 후보)
# 005930(삼성전자) 등 비싼 종목도 분할 매수 가능 검증용으로 포함.
DEFAULT_CODES = [
    "005930",  # 삼성전자
    "000660",  # SK하이닉스
    "035720",  # 카카오
    "035420",  # NAVER
    "005380",  # 현대차
    "000270",  # 기아
    "207940",  # 삼성바이오로직스
    "068270",  # 셀트리온
    "051910",  # LG화학
    "012450",  # 한화에어로스페이스
]

# 13개 시각 (09:30~15:30, 30분 간격) — 각 호출이 직전 30봉 반환
PAGINATION_TIMES = [
    "153000", "150000", "143000", "140000", "133000", "130000",
    "123000", "120000", "113000", "110000", "103000", "100000", "093000",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="일일 분봉 백테스트 데이터 수집기")
    p.add_argument(
        "--codes", default=",".join(DEFAULT_CODES),
        help=f"수집할 종목코드 콤마 구분 (default: {len(DEFAULT_CODES)}종목)",
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
        # rows가 시간 오름차순이므로 마지막이 오늘, 그 직전이 전일
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
        new_count = 0
        for r in rows:
            t = str(r.get("time", "")).zfill(6)
            if not t or t in seen_times:
                continue
            seen_times.add(t)
            all_rows.append(r)
            new_count += 1
        logger.debug("[%s] %s 호출 → 신규 %d봉 (누적 %d)", code, time_str, new_count, len(all_rows))
        time.sleep(throttle_sec)
    return all_rows


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


def save_daily(df: pd.DataFrame, code: str, date_str: str) -> Path:
    out_dir = settings.DATA_DIR / "ohlcv" / "minute_archive"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{code}_{date_str}.csv"
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out_path


def append_weekly(df: pd.DataFrame, code: str) -> tuple[Path, str]:
    """
    주차별 CSV에 append (ISO 주차 기준 — 월요일~일요일).
    재실행 안전 — 같은 날짜는 교체.

    Returns:
        (파일 경로, 주차 식별자 'YYYY-Www')
    """
    today = datetime.now()
    iso_year, iso_week, _ = today.isocalendar()
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


def main() -> None:
    args = parse_args()
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    if not codes:
        logger.error("종목코드가 비어있습니다")
        sys.exit(1)

    logger.info(
        "일일 분봉 데이터 수집 시작 | %d종목 × 13페이지 (예상 ~%d초)",
        len(codes), int(len(codes) * 14 * args.throttle_sec),
    )

    api = KISApiClient()
    api.init()
    date_str = datetime.now().strftime("%Y%m%d")

    results: list[dict] = []
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] %s 수집 중...", i, len(codes), code)
        try:
            prev_close = fetch_prev_close(api, code, args.market)
            time.sleep(args.throttle_sec)
            rows = fetch_full_day_minutes(api, code, args.market, args.throttle_sec)
            df = normalize_rows(rows, code, prev_close)

            daily_path = save_daily(df, code, date_str)
            week_id = "-"
            week_days = 1
            if not args.no_append_weekly:
                wp, week_id = append_weekly(df, code)
                # 주차별 누적 일수
                try:
                    week_days = pd.read_csv(wp, dtype={"time": str})["date"].nunique()
                except Exception:
                    pass

            results.append({
                "code": code,
                "rows": len(df),
                "time_range": f"{df['time'].min()}~{df['time'].max()}",
                "prev_close": prev_close,
                "week_id": week_id,
                "week_days": week_days,
                "status": "OK",
            })
            logger.info(
                "[%s] ✓ %d봉 (%s ~ %s) | %s 누적 %d일/5",
                code, len(df), df["time"].min(), df["time"].max(),
                week_id, week_days,
            )
        except Exception as exc:
            logger.error("[%s] ✗ 수집 실패: %s", code, exc)
            results.append({
                "code": code, "rows": 0, "time_range": "",
                "prev_close": 0, "week_id": "-", "week_days": 0,
                "status": f"FAIL: {exc}",
            })

    # ── 요약 출력 ────────────────────────────────────────────────────
    print()
    print("=" * 95)
    print(f"수집 완료: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 날짜={date_str}")
    print("=" * 95)
    print(f"{'종목':<10}{'봉수':<8}{'시간범위':<20}{'전일종가':<12}{'주차':<12}{'주차일수':<10}상태")
    print("-" * 95)
    for r in results:
        print(
            f"{r['code']:<10}{r['rows']:<8}{r['time_range']:<20}"
            f"{r['prev_close']:<12,}{r['week_id']:<12}{r['week_days']:<10}{r['status']}"
        )
    print("=" * 95)

    success = sum(1 for r in results if r["status"] == "OK")
    print(f"성공: {success}/{len(results)}")
    week_id = next((r["week_id"] for r in results if r["status"] == "OK"), "YYYY-Www")
    print()
    print(f"다음 단계 (이번 주 누적 데이터로 백테스트):")
    print(
        f"  python run_intraday_backtest.py "
        f"--csv data/ohlcv/weekly/{codes[0]}_minute_{week_id}.csv "
        f"--strategy S1 --code {codes[0]}"
    )
    print()
    print("주차별 파일 위치: data/ohlcv/weekly/{code}_minute_{YYYY-Www}.csv")
    print("→ 매주 월요일 자동으로 새 파일 생성, 이전 주 파일은 그대로 보존")


if __name__ == "__main__":
    main()
