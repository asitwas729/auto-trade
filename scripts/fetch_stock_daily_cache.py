"""
SECTOR_UNIVERSE 50종목의 일별 OHLCV (open/high/low/close/volume/amount) 를
data/stock_daily/<code>.csv 로 저장. 수익률·백테스트·알파 분석용.

사용법:
    python scripts/fetch_stock_daily_cache.py --start 2024-04-01 --end 2026-04-30
"""

from __future__ import annotations

import argparse
import csv
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

DATA_DIR = settings.DATA_DIR / "stock_daily"
DATA_DIR.mkdir(parents=True, exist_ok=True)

REQ_INTERVAL = 0.06


def chunked(s: str, e: str, span: int = 200):
    s_dt = datetime.strptime(s, "%Y%m%d")
    e_dt = datetime.strptime(e, "%Y%m%d")
    cur = s_dt
    while cur <= e_dt:
        ce = min(cur + timedelta(days=span), e_dt)
        yield cur.strftime("%Y%m%d"), ce.strftime("%Y%m%d")
        cur = ce + timedelta(days=1)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args()

    s_str = datetime.strptime(args.start, "%Y-%m-%d").strftime("%Y%m%d")
    e_str = datetime.strptime(args.end, "%Y-%m-%d").strftime("%Y%m%d")

    client = KISApiClient()
    client.init()

    total = sum(len(v) for v in SECTOR_UNIVERSE.values())
    done = 0
    fetched = 0
    skipped = 0
    failed = []

    for sector, codes in SECTOR_UNIVERSE.items():
        for code in codes:
            done += 1
            out_path = DATA_DIR / f"{code}.csv"
            if out_path.exists() and not args.overwrite:
                skipped += 1
                logger.info("[%d/%d] %s %s : 캐시 있음 스킵", done, total, sector, code)
                continue
            rows = []
            try:
                for c_s, c_e in chunked(s_str, e_str):
                    chunk = client.get_daily_ohlcv(code, c_s, c_e, period="D")
                    rows.extend(chunk)
                    time.sleep(REQ_INTERVAL)
            except Exception as exc:
                logger.warning("[%d/%d] %s %s 실패: %s", done, total, sector, code, exc)
                failed.append((sector, code, str(exc)[:80]))
                continue
            if not rows:
                continue
            # 중복 제거 + 정렬
            seen = {}
            for r in rows:
                seen[r["date"]] = r
            ordered = sorted(seen.values(), key=lambda r: r["date"])
            with open(out_path, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["date", "open", "high", "low", "close", "volume", "amount"])
                for r in ordered:
                    w.writerow([r["date"], r["open"], r["high"], r["low"],
                                r["close"], r["volume"], r["amount"]])
            fetched += 1
            logger.info("[%d/%d] %s %s : %d일 저장", done, total, sector, code, len(ordered))

    print("\n" + "=" * 60)
    print(f"fetch_stock_daily 완료: 저장 {fetched}, 스킵 {skipped}, 실패 {len(failed)}")
    if failed:
        for s, c, e in failed:
            print(f"  - {s} {c}: {e}")
    print("=" * 60)


if __name__ == "__main__":
    main()
