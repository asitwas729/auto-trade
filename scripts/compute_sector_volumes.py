"""
SECTOR_UNIVERSE 종목들의 일별 거래대금/회전율을 KIS API 로 받아
섹터별로 집계한 결과를 data/sector_volume_YYYYMMDD.json (또는
data/sector_turnover_YYYYMMDD.json) 으로 저장.

메트릭:
  --metric amount   (기본): 5종목 거래대금(원) 합산 → 시총 큰 종목에 편향
  --metric turnover : 5종목 회전율(거래량/상장주식수) 평균 → 시총 정규화

사용법:
    python scripts/compute_sector_volumes.py --metric turnover --start 2024-04-01 --end 2026-04-30
    python scripts/compute_sector_volumes.py --metric amount --days 14
    python scripts/compute_sector_volumes.py --metric turnover --days 14 --overwrite

저장 형식:
    amount   → {"반도체": 12345678901234, ...}     # 원 단위
    turnover → {"반도체": 0.00342, ...}             # 평균 회전율 (소수)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.constants import SECTOR_UNIVERSE
from config.settings import settings
from core.kis_api_client import KISApiClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = settings.DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
SHARES_CACHE = DATA_DIR / "sector_shares_cache.json"

REQ_INTERVAL_SEC = 0.06  # KIS 모의 ~ 1 req/sec, 실전 ~ 20 req/sec, 안전치


def _load_shares() -> dict[str, int]:
    if not SHARES_CACHE.exists():
        return {}
    try:
        raw = json.loads(SHARES_CACHE.read_text(encoding="utf-8"))
        return raw.get("shares", {})
    except Exception:
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="섹터별 일별 거래대금/회전율 집계")
    parser.add_argument("--days", type=int, default=14, help="오늘 기준 과거 N일 (기본 14)")
    parser.add_argument("--start", type=str, help="시작일 YYYY-MM-DD (--days 와 배타)")
    parser.add_argument("--end", type=str, help="종료일 YYYY-MM-DD")
    parser.add_argument("--metric", choices=["amount", "turnover"], default="amount",
                        help="amount=거래대금합산, turnover=회전율평균(시총정규화)")
    parser.add_argument("--overwrite", action="store_true", help="기존 파일 덮어쓰기")
    args = parser.parse_args()

    shares_map = _load_shares() if args.metric == "turnover" else {}
    if args.metric == "turnover" and not shares_map:
        logger.error("회전율 모드인데 sector_shares_cache.json 없음. fetch_shares_cache.py 먼저 실행")
        sys.exit(1)

    today = datetime.now().date()
    if args.start and args.end:
        start_d = datetime.strptime(args.start, "%Y-%m-%d").date()
        end_d = datetime.strptime(args.end, "%Y-%m-%d").date()
    else:
        end_d = today - timedelta(days=1)
        start_d = end_d - timedelta(days=args.days - 1)

    start_str = start_d.strftime("%Y%m%d")
    end_str = end_d.strftime("%Y%m%d")
    logger.info("거래대금 집계 범위: %s ~ %s", start_d, end_d)

    client = KISApiClient()
    client.init()
    logger.info("KIS API 토큰 초기화 완료 (mode=%s)", settings.TRADING_MODE)

    # date_str(YYYYMMDD) -> sector -> [기여 종목 metric 값들]
    daily_buckets: dict[str, dict[str, list[float]]] = {}
    fetch_errors: list[tuple[str, str, str]] = []

    total_stocks = sum(len(v) for v in SECTOR_UNIVERSE.values())
    done = 0

    # KIS daily-price 는 한 번에 최대 100영업일 → 긴 범위는 청크로 나눠야 함
    def chunked_ranges(s_str: str, e_str: str, max_days: int = 100):
        s_dt = datetime.strptime(s_str, "%Y%m%d")
        e_dt = datetime.strptime(e_str, "%Y%m%d")
        cur = s_dt
        while cur <= e_dt:
            chunk_end = min(cur + timedelta(days=max_days * 2), e_dt)  # 영업일 기준 ~100개
            yield cur.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")
            cur = chunk_end + timedelta(days=1)

    for sector, codes in SECTOR_UNIVERSE.items():
        for code in codes:
            done += 1
            shares = shares_map.get(code, 0) if args.metric == "turnover" else 0
            if args.metric == "turnover" and shares <= 0:
                logger.warning("  [%d/%d] %s %s 상장주식수 미상 → 스킵", done, total_stocks, sector, code)
                continue

            all_rows: list[dict] = []
            try:
                for c_start, c_end in chunked_ranges(start_str, end_str):
                    rows = client.get_daily_ohlcv(code, c_start, c_end, period="D")
                    all_rows.extend(rows)
                    time.sleep(REQ_INTERVAL_SEC)
            except Exception as exc:
                logger.warning("[%s/%s] OHLCV 조회 실패: %s", sector, code, exc)
                fetch_errors.append((sector, code, str(exc)[:120]))
                time.sleep(REQ_INTERVAL_SEC)
                continue

            for r in all_rows:
                d = r["date"]  # YYYYMMDD
                if args.metric == "amount":
                    val = float(r.get("amount", 0))
                else:  # turnover
                    vol = int(r.get("volume", 0))
                    val = vol / shares if shares > 0 else 0.0
                daily_buckets.setdefault(d, {}).setdefault(sector, []).append(val)

            logger.info("  [%d/%d] %s %s : %d일치", done, total_stocks, sector, code, len(all_rows))

    if not daily_buckets:
        logger.error("수집 결과 없음")
        sys.exit(1)

    # 일자별 저장
    written = 0
    skipped = 0
    file_prefix = "sector_volume" if args.metric == "amount" else "sector_turnover"
    for date_str in sorted(daily_buckets):
        path = DATA_DIR / f"{file_prefix}_{date_str}.json"
        if path.exists() and not args.overwrite:
            skipped += 1
            continue

        # amount 는 합산, turnover 는 평균
        if args.metric == "amount":
            agg = {sec: int(sum(vs)) for sec, vs in daily_buckets[date_str].items()}
        else:
            agg = {sec: round(sum(vs) / len(vs), 8) for sec, vs in daily_buckets[date_str].items() if vs}

        ordered = dict(sorted(agg.items(), key=lambda kv: kv[1], reverse=True))
        path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1

    print("\n" + "=" * 60)
    print(f"섹터 {args.metric} 집계 완료: 저장 {written}일, 스킵 {skipped}일, 실패 {len(fetch_errors)}건")
    if fetch_errors:
        print("실패 종목:")
        for s, c, e in fetch_errors[:10]:
            print(f"  - {s} {c}: {e}")
    print("=" * 60)


if __name__ == "__main__":
    main()
