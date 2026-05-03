"""
Naver Search API 로 과거 N일치 뉴스를 소급 수집하여
data/news_cache_YYYYMMDD.json 으로 저장.

배경:
  Naver RSS 는 현재 시점 피드만 제공하므로 과거 일자 캐시를 만들 수 없다.
  Search API(news.json) 는 sort=date + start 파라미터로 최대 1000건 페이징 가능.
  여러 금융 키워드 쿼리로 1000건씩 끌어와 pubDate 기준 일자별로 버킷팅.

한계:
  - Search API 는 명시적 날짜 필터가 없어 최신순 1000건만 페이징 가능.
  - 일자가 멀어질수록 누락이 커진다 (보통 최근 7~14일까지가 현실적).
  - 결과는 RSS 와 정확히 일치하지 않으나 SECTOR_KEYWORDS 매칭 용도로는 충분.

사용법:
    python scripts/backfill_naver_news.py --days 14
    python scripts/backfill_naver_news.py --start 2026-04-15 --end 2026-04-30

전제:
    .env 에 NAVER_CLIENT_ID, NAVER_CLIENT_SECRET 설정 필요.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.constants import NAVER_SEARCH_API_URL
from config.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
DATA_DIR = settings.DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 금융/시장 전반을 폭넓게 끌어오기 위한 다중 쿼리 (Search API 는 쿼리 1개당 최대 1000건)
DEFAULT_QUERIES = [
    "코스피",
    "코스닥",
    "주식 시장",
    "증시",
    "외국인 매수 매도",
    "기관 수급",
    "반도체",
    "2차전지",
    "바이오",
    "방산",
    "자동차",
    "조선",
    "금리",
    "환율",
    "AI 반도체",
    "신재생 에너지",
]

REQUEST_TIMEOUT = 10
PAGE_SIZE = 100  # Naver Search API max display
MAX_START = 1000  # API 제한
SLEEP_BETWEEN_PAGES = 0.1


def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = (text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
                 .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return text.strip()


def _parse_pub_date(s: str) -> datetime | None:
    if not s:
        return None
    try:
        import email.utils
        dt = email.utils.parsedate_to_datetime(s)
        return dt.astimezone(KST) if dt.tzinfo else dt.replace(tzinfo=KST)
    except Exception:
        return None


def fetch_query(query: str) -> Iterable[dict]:
    """sort=date 로 최신순 1000건까지 페이징."""
    headers = {
        "X-Naver-Client-Id": settings.NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": settings.NAVER_CLIENT_SECRET,
    }
    start = 1
    while start <= MAX_START:
        params = {"query": query, "display": PAGE_SIZE, "start": start, "sort": "date"}
        try:
            resp = requests.get(NAVER_SEARCH_API_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 400:
                # start 가 허용범위 초과
                break
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("[%s start=%d] 요청 실패: %s", query, start, exc)
            break

        items = data.get("items", [])
        if not items:
            break
        for it in items:
            yield it
        start += PAGE_SIZE
        time.sleep(SLEEP_BETWEEN_PAGES)


def main() -> None:
    parser = argparse.ArgumentParser(description="Naver Search API 과거 뉴스 소급 캐시 생성")
    parser.add_argument("--days", type=int, default=14, help="오늘 기준 과거 N일 (기본 14일)")
    parser.add_argument("--start", type=str, help="시작일 YYYY-MM-DD (--days 와 배타)")
    parser.add_argument("--end", type=str, help="종료일 YYYY-MM-DD")
    parser.add_argument("--queries", nargs="*", default=DEFAULT_QUERIES, help="검색 쿼리 목록")
    parser.add_argument("--overwrite", action="store_true", help="기존 캐시 덮어쓰기")
    args = parser.parse_args()

    if not (settings.NAVER_CLIENT_ID and settings.NAVER_CLIENT_SECRET):
        logger.error(".env 에 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 설정 필요")
        sys.exit(1)

    today_kst = datetime.now(KST).date()
    if args.start and args.end:
        start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
        end_date = datetime.strptime(args.end, "%Y-%m-%d").date()
    else:
        end_date = today_kst - timedelta(days=1)  # 어제까지
        start_date = end_date - timedelta(days=args.days - 1)

    logger.info("백필 범위: %s ~ %s (%d일)", start_date, end_date, (end_date - start_date).days + 1)

    # 일자별 버킷
    buckets: dict[str, dict[str, dict]] = {}  # date_str -> {link: item_dict}
    seen_links: set[str] = set()
    total_fetched = 0
    total_in_range = 0

    for q in args.queries:
        logger.info("쿼리 [%s] 수집 중...", q)
        q_count = 0
        q_in_range = 0
        for raw in fetch_query(q):
            q_count += 1
            total_fetched += 1
            link = raw.get("link") or raw.get("originallink") or ""
            if link in seen_links:
                continue
            pub = _parse_pub_date(raw.get("pubDate", ""))
            if pub is None:
                continue
            d = pub.date()
            if d < start_date or d > end_date:
                continue
            seen_links.add(link)
            q_in_range += 1
            total_in_range += 1
            date_str = d.strftime("%Y%m%d")
            buckets.setdefault(date_str, {})[link] = {
                "title": _clean_html(raw.get("title", "")),
                "summary": _clean_html(raw.get("description", "")),
                "published_at": pub.isoformat(),
                "keywords": [],
                "source": "naver_api_backfill",
            }
        logger.info("  쿼리 [%s]: 가져옴 %d, 범위내 %d", q, q_count, q_in_range)

    logger.info("총 가져옴 %d, 범위내 신규 %d", total_fetched, total_in_range)

    # 일자별 저장
    written = 0
    skipped = 0
    for date_str, link_map in sorted(buckets.items()):
        path = DATA_DIR / f"news_cache_{date_str}.json"
        if path.exists() and not args.overwrite:
            logger.info("  %s : 기존 캐시 존재, 스킵 (--overwrite 로 덮어쓰기 가능)", path.name)
            skipped += 1
            continue
        items = list(link_map.values())
        path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("  %s : %d건 저장", path.name, len(items))
        written += 1

    # 빈 일자 보고
    cur = start_date
    missing = []
    while cur <= end_date:
        ds = cur.strftime("%Y%m%d")
        if ds not in buckets:
            missing.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    if missing:
        logger.warning("뉴스 0건 일자 (%d일): %s", len(missing), ", ".join(missing))

    print("\n" + "=" * 60)
    print(f"백필 완료: 저장 {written}일, 스킵 {skipped}일, 누락 {len(missing)}일")
    print("=" * 60)


if __name__ == "__main__":
    main()
