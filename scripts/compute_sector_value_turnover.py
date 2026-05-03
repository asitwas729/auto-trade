"""
섹터별 일별 '시총가중 회전율' 계산:
    sector_value_turnover[d] = (섹터 5종목 거래대금 합) / (섹터 5종목 시총 합)

회전율(거래량/상장주식수)의 단점인 종목별 가격/시총 차이로 인한 왜곡을 제거.
중·소형주가 발행주식 대비 활발히 거래되어 회전율이 부풀려지던 편향을 보정.

데이터 흐름:
  - 일봉 OHLCV 로 종목별 (close, amount) 확보
  - sector_shares_cache.json 의 상장주식수로 시총 = close × shares
  - 섹터 합 amount / 섹터 합 market_cap

저장: data/sector_value_turnover_YYYYMMDD.json

사용법:
    python scripts/compute_sector_value_turnover.py --start 2024-04-01 --end 2026-04-30
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

REQ_INTERVAL_SEC = 0.06


def _load_shares() -> dict[str, int]:
    if not SHARES_CACHE.exists():
        return {}
    raw = json.loads(SHARES_CACHE.read_text(encoding="utf-8"))
    return raw.get("shares", {})


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--start", type=str)
    p.add_argument("--end", type=str)
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args()

    shares_map = _load_shares()
    if not shares_map:
        logger.error("sector_shares_cache.json 없음. fetch_shares_cache.py 먼저 실행")
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
    logger.info("범위: %s ~ %s", start_d, end_d)

    client = KISApiClient()
    client.init()

    # date -> sector -> [sum_amount, sum_market_cap]
    daily: dict[str, dict[str, list[float]]] = {}
    fetch_errors: list = []

    def chunked(s: str, e: str, span: int = 200):
        s_dt = datetime.strptime(s, "%Y%m%d")
        e_dt = datetime.strptime(e, "%Y%m%d")
        cur = s_dt
        while cur <= e_dt:
            ce = min(cur + timedelta(days=span), e_dt)
            yield cur.strftime("%Y%m%d"), ce.strftime("%Y%m%d")
            cur = ce + timedelta(days=1)

    total_stocks = sum(len(v) for v in SECTOR_UNIVERSE.values())
    done = 0
    for sector, codes in SECTOR_UNIVERSE.items():
        for code in codes:
            done += 1
            shares = shares_map.get(code, 0)
            if shares <= 0:
                logger.warning("  [%d/%d] %s %s 시총 미상 → 스킵", done, total_stocks, sector, code)
                continue

            all_rows: list[dict] = []
            try:
                for c_s, c_e in chunked(start_str, end_str):
                    rows = client.get_daily_ohlcv(code, c_s, c_e, period="D")
                    all_rows.extend(rows)
                    time.sleep(REQ_INTERVAL_SEC)
            except Exception as exc:
                logger.warning("[%s/%s] 실패: %s", sector, code, exc)
                fetch_errors.append((sector, code, str(exc)[:120]))
                continue

            for r in all_rows:
                d = r["date"]
                close = int(r.get("close", 0))
                amt = float(r.get("amount", 0))
                if close <= 0:
                    continue
                mkt_cap = close * shares
                bucket = daily.setdefault(d, {}).setdefault(sector, [0.0, 0.0])
                bucket[0] += amt
                bucket[1] += mkt_cap

            logger.info("  [%d/%d] %s %s : %d일치", done, total_stocks, sector, code, len(all_rows))

    # 일자별 저장
    written = 0
    skipped = 0
    for date_str in sorted(daily):
        path = DATA_DIR / f"sector_value_turnover_{date_str}.json"
        if path.exists() and not args.overwrite:
            skipped += 1
            continue
        agg = {}
        for sec, (amt, mkt) in daily[date_str].items():
            if mkt > 0:
                agg[sec] = round(amt / mkt, 8)
        ordered = dict(sorted(agg.items(), key=lambda kv: kv[1], reverse=True))
        path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1

    print("\n" + "=" * 60)
    print(f"value_turnover 완료: 저장 {written}일, 스킵 {skipped}일, 실패 {len(fetch_errors)}건")
    print("=" * 60)


if __name__ == "__main__":
    main()
