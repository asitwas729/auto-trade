"""
당일 Naver RSS / Search API 뉴스를 받아 data/news_cache_YYYYMMDD.json 으로 저장.

매일 오전(예: 08:00 KST) 한 번 실행하면 됨. Windows 작업 스케줄러나 cron 등록용.

사용법:
    python scripts/cache_naver_rss_daily.py

종료 코드:
    0 = 신규/기존 캐시 OK
    1 = 수집 실패 (네트워크/설정 문제)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.news_collector import NewsCollector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    collector = NewsCollector()
    items = collector.fetch()  # 내부에서 RSS 성공 시 자동 _save_cache()
    if not items:
        logger.error("수집 실패: 빈 리스트")
        return 1
    sources = {it.source for it in items}
    if "naver_rss" not in sources and "naver_api" not in sources:
        logger.error("RSS/API 둘 다 실패 (소스=%s) → 캐시 미갱신", sources)
        return 1
    logger.info("캐시 저장 완료: %d건, 소스=%s", len(items), sources)
    return 0


if __name__ == "__main__":
    sys.exit(main())
