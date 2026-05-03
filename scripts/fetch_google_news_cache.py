"""
Google News RSS 로 임의 기간 한국어 금융 뉴스를 일자별 캐시로 저장.

저장 경로: data/google_news_cache_YYYYMMDD.json

URL: https://news.google.com/rss/search?q=...+after:D+before:D+1&hl=ko&gl=KR&ceid=KR:ko
  - after:/before: 연산자로 임의 일자 검색 가능 (Naver Search API 와 달리)

사용법:
    python scripts/fetch_google_news_cache.py --days 14
    python scripts/fetch_google_news_cache.py --start 2024-04-01 --end 2026-04-30
    python scripts/fetch_google_news_cache.py --dates 2025-06-15,2025-07-01,...
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
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
DATA_DIR = settings.DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"

# v2 (2026-05): 섹터별 균형 잡힌 쿼리. 이전엔 "AI 플랫폼 네이버 카카오"가
# IT플랫폼 결과를 압도적으로 많이 끌어들여 GN 1순위가 100일 중 73%가 IT플랫폼이
# 되는 편향을 일으켰음. 섹터 1쿼리 + 시장 일반 2쿼리로 재설계.
DEFAULT_QUERIES = [
    # 시장 일반 (매크로/시황) - 2개
    "코스피 코스닥 증시 시황",
    "외국인 기관 수급 거래",
    # 섹터별 1쿼리씩 (10개 섹터)
    "반도체 HBM 메모리 SK하이닉스 엔비디아",
    "2차전지 양극재 음극재 에코프로 배터리",
    "방산 한화에어로 LIG넥스원 K방산",
    "친환경에너지 태양광 원전 SMR 풍력",
    "자동차 현대차 기아 전기차 자율주행",
    "조선 HD현대 삼성중공업 한화오션 LNG선",
    "바이오 셀트리온 삼성바이오 신약 임상",
    "네이버 카카오 게임주 넷마블 엔씨소프트",
    "금리 환율 KB금융 은행주 증권주",
    "화장품 면세점 아모레 LG생활건강",
]

REQUEST_TIMEOUT = 15
SLEEP_BETWEEN = 0.3


def _clean_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
          .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return s.strip()


def _parse_pub(s: str) -> datetime | None:
    if not s:
        return None
    try:
        import email.utils
        dt = email.utils.parsedate_to_datetime(s)
        return dt.astimezone(KST) if dt.tzinfo else dt.replace(tzinfo=KST)
    except Exception:
        return None


def fetch_for_date(query: str, date_obj) -> list[dict]:
    try:
        import feedparser  # type: ignore
    except ImportError:
        raise SystemExit("feedparser 필요: pip install feedparser")

    after = date_obj.strftime("%Y-%m-%d")
    before = (date_obj + timedelta(days=1)).strftime("%Y-%m-%d")
    q = f"{query} after:{after} before:{before}"
    url = f"{GOOGLE_NEWS_RSS}?q={quote_plus(q)}&hl=ko&gl=KR&ceid=KR:ko"

    feed = feedparser.parse(url)
    items = []
    for e in feed.entries:
        pub = _parse_pub(e.get("published", ""))
        if pub is None or pub.date() != date_obj:
            continue
        items.append({
            "title": _clean_html(e.get("title", "")),
            "summary": _clean_html(e.get("summary", e.get("description", ""))),
            "published_at": pub.isoformat(),
            "keywords": [],
            "source": "google_news_rss",
            "link": e.get("link", ""),
        })
    return items


def main() -> None:
    p = argparse.ArgumentParser(description="Google News RSS 일자별 캐시 백필")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--start", type=str)
    p.add_argument("--end", type=str)
    p.add_argument("--dates", type=str, help="콤마구분 YYYY-MM-DD 목록 (특정 일자만)")
    p.add_argument("--queries", nargs="*", default=DEFAULT_QUERIES)
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args()

    today = datetime.now(KST).date()

    if args.dates:
        date_list = [datetime.strptime(d.strip(), "%Y-%m-%d").date() for d in args.dates.split(",") if d.strip()]
    elif args.start and args.end:
        s = datetime.strptime(args.start, "%Y-%m-%d").date()
        e = datetime.strptime(args.end, "%Y-%m-%d").date()
        date_list = [s + timedelta(days=i) for i in range((e - s).days + 1)]
    else:
        e = today - timedelta(days=1)
        s = e - timedelta(days=args.days - 1)
        date_list = [s + timedelta(days=i) for i in range((e - s).days + 1)]

    logger.info("백필 대상 %d일, 쿼리 %d개", len(date_list), len(args.queries))

    written = 0
    skipped = 0
    empty = 0

    for d in date_list:
        date_str = d.strftime("%Y%m%d")
        path = DATA_DIR / f"google_news_cache_{date_str}.json"
        if path.exists() and not args.overwrite:
            skipped += 1
            continue

        link_map: dict[str, dict] = {}
        for q in args.queries:
            try:
                items = fetch_for_date(q, d)
            except SystemExit:
                raise
            except Exception as exc:
                logger.debug("[%s/%s] 실패: %s", d, q, exc)
                items = []
            for it in items:
                link = it.get("link", "") or it.get("title", "")
                if link not in link_map:
                    link_map[link] = it
            time.sleep(SLEEP_BETWEEN)

        items_dedup = list(link_map.values())
        for it in items_dedup:
            it.pop("link", None)

        if not items_dedup:
            logger.warning("[%s] 0건", d)
            empty += 1
            continue

        path.write_text(json.dumps(items_dedup, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("[%s] %d건 저장", d, len(items_dedup))
        written += 1

    print("\n" + "=" * 60)
    print(f"백필 완료: 저장 {written}일, 스킵 {skipped}일, 0건 {empty}일")
    print("=" * 60)


if __name__ == "__main__":
    main()
