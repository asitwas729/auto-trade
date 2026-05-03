"""
SECTOR_UNIVERSE 50종목의 상장주식수(lstn_stcn)를 KIS 현재가 API 로 받아
data/sector_shares_cache.json 으로 저장.

회전율 = 일일거래량(주) / 상장주식수 계산용 사전 데이터.
상장주식수는 자주 바뀌지 않으므로 1회 캐시 후 재사용.

사용법:
    python scripts/fetch_shares_cache.py
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.constants import (
    KIS_PATH_PRICE,
    MARKET_KOSPI,
    SECTOR_UNIVERSE,
)
from config.settings import settings
from core.kis_api_client import KISApiClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = settings.DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_PATH = DATA_DIR / "sector_shares_cache.json"


def fetch_raw_inquire_price(client: KISApiClient, code: str) -> dict:
    """get_price() 의 raw output 그대로 반환 (lstn_stcn 포함)."""
    return client._get(  # type: ignore[attr-defined]
        KIS_PATH_PRICE,
        client._tr["price"],  # type: ignore[attr-defined]
        {"fid_cond_mrkt_div_code": MARKET_KOSPI, "fid_input_iscd": code},
    )["output"]


def main() -> None:
    client = KISApiClient()
    client.init()
    logger.info("KIS API 초기화 완료 (mode=%s)", settings.TRADING_MODE)

    cache: dict = {}
    if CACHE_PATH.exists():
        try:
            cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            logger.info("기존 캐시 로드: %d종목", len(cache.get("shares", {})))
        except Exception:
            cache = {}

    shares: dict[str, int] = cache.get("shares", {}) if cache else {}
    sector_map: dict[str, str] = cache.get("sector_of", {}) if cache else {}

    fail = []
    total = sum(len(v) for v in SECTOR_UNIVERSE.values())
    done = 0
    for sector, codes in SECTOR_UNIVERSE.items():
        for code in codes:
            done += 1
            if code in shares and shares[code] > 0:
                logger.info("  [%d/%d] %s %s : 캐시 있음 (%d주)", done, total, sector, code, shares[code])
                continue
            try:
                output = fetch_raw_inquire_price(client, code)
                lstn = int(output.get("lstn_stcn", 0))
                if lstn <= 0:
                    raise ValueError(f"lstn_stcn 없음: {output}")
                shares[code] = lstn
                sector_map[code] = sector
                logger.info("  [%d/%d] %s %s : %d 주", done, total, sector, code, lstn)
            except Exception as exc:
                logger.warning("  [%d/%d] %s %s 조회 실패: %s", done, total, sector, code, exc)
                fail.append((sector, code, str(exc)[:120]))
            time.sleep(0.06)

    out = {"shares": shares, "sector_of": sector_map}
    CACHE_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("저장 완료: %s (%d종목)", CACHE_PATH, len(shares))

    if fail:
        print("\n실패 종목:")
        for s, c, e in fail:
            print(f"  - {s} {c}: {e}")


if __name__ == "__main__":
    main()
