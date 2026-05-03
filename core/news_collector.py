"""
뉴스 수집기 (S5 전략용)

우선순위:
  1순위: Naver RSS (경제/증권 섹션)
  2순위: Naver Search API
  3순위: 직전일 캐시 or 균등 점수 0.5

데이터 흐름:
  fetch() → list[NewsItem]
  → SectorManager.score_sectors() 에 전달
  → 섹터별 점수 0~1 계산
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests

from config.constants import (
    NAVER_RSS_URL,
    NAVER_SEARCH_API_URL,
    SECTOR_KEYWORDS,
)
from config.settings import settings

logger = logging.getLogger(__name__)

# 캐시 저장 경로
_CACHE_DIR = settings.DATA_DIR
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class NewsItem:
    title: str
    summary: str
    published_at: datetime
    keywords: list[str] = field(default_factory=list)
    source: str = "unknown"   # "naver_rss" | "naver_api" | "cache"


class NewsCollector:
    """
    장전 뉴스 수집 및 섹터 키워드 추출.

    사용 예:
        collector = NewsCollector()
        items = collector.fetch()
        # SectorManager.score_sectors(items) 에 전달
    """

    REQUEST_TIMEOUT = 10  # seconds
    RSS_ARTICLE_LIMIT = 50
    API_ARTICLE_LIMIT = 50

    def fetch(self) -> list[NewsItem]:
        """뉴스 항목 리스트 반환. 소스별 폴백 자동 처리."""
        # 1순위: Naver RSS
        items = self._parse_rss()
        if items:
            logger.info("[뉴스] Naver RSS %d건 수집", len(items))
            self._save_cache(items)
            return items

        # 2순위: Naver Search API
        items = self._search_api("주식 섹터 테마 주목")
        if items:
            logger.info("[뉴스] Naver API %d건 수집", len(items))
            self._save_cache(items)
            return items

        # 3순위: 직전일 캐시
        cached = self._load_cache()
        if cached:
            logger.warning("[뉴스] RSS/API 실패 → 직전일 캐시 %d건 사용", len(cached))
            return cached

        # 4순위: 균등 기본값 (모든 섹터 0.5)
        logger.warning("[뉴스] 캐시도 없음 → 균등 기본 항목 반환")
        return self._uniform_fallback()

    # ─── Naver RSS ────────────────────────────────────────────────

    def _parse_rss(self) -> list[NewsItem]:
        """Naver 금융 RSS를 파싱하여 NewsItem 리스트 반환."""
        try:
            import feedparser  # type: ignore
        except ImportError:
            logger.debug("[뉴스] feedparser 없음 → RSS 스킵")
            return []

        try:
            feed = feedparser.parse(NAVER_RSS_URL)
            if feed.bozo:
                logger.debug("[뉴스] RSS 파싱 오류 (bozo): %s", feed.bozo_exception)
                return []

            items: list[NewsItem] = []
            for entry in feed.entries[: self.RSS_ARTICLE_LIMIT]:
                title = _clean_html(entry.get("title", ""))
                summary = _clean_html(entry.get("summary", entry.get("description", "")))
                published_at = _parse_rss_date(entry.get("published", ""))
                kws = _extract_keywords(title + " " + summary)
                items.append(NewsItem(
                    title=title,
                    summary=summary,
                    published_at=published_at,
                    keywords=kws,
                    source="naver_rss",
                ))
            return items
        except Exception as exc:
            logger.warning("[뉴스] RSS 수집 실패: %s", exc)
            return []

    # ─── Naver Search API ─────────────────────────────────────────

    def _search_api(self, query: str) -> list[NewsItem]:
        """Naver Search API로 뉴스 검색."""
        if not (settings.NAVER_CLIENT_ID and settings.NAVER_CLIENT_SECRET):
            logger.debug("[뉴스] NAVER_CLIENT_ID/SECRET 미설정 → API 스킵")
            return []

        params = {
            "query": query,
            "display": self.API_ARTICLE_LIMIT,
            "sort": "date",
        }
        headers = {
            "X-Naver-Client-Id": settings.NAVER_CLIENT_ID,
            "X-Naver-Client-Secret": settings.NAVER_CLIENT_SECRET,
        }
        try:
            resp = requests.get(
                NAVER_SEARCH_API_URL,
                params=params,
                headers=headers,
                timeout=self.REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            items: list[NewsItem] = []
            for article in data.get("items", []):
                title = _clean_html(article.get("title", ""))
                summary = _clean_html(article.get("description", ""))
                pub_str = article.get("pubDate", "")
                published_at = _parse_rfc_date(pub_str)
                kws = _extract_keywords(title + " " + summary)
                items.append(NewsItem(
                    title=title,
                    summary=summary,
                    published_at=published_at,
                    keywords=kws,
                    source="naver_api",
                ))
            return items
        except Exception as exc:
            logger.warning("[뉴스] Naver API 수집 실패: %s", exc)
            return []

    # ─── 캐시 ─────────────────────────────────────────────────────

    def _cache_path(self, date: Optional[datetime] = None) -> Path:
        d = date or datetime.now()
        return _CACHE_DIR / f"news_cache_{d.strftime('%Y%m%d')}.json"

    def _save_cache(self, items: list[NewsItem]) -> None:
        path = self._cache_path()
        try:
            data = [
                {
                    "title": it.title,
                    "summary": it.summary,
                    "published_at": it.published_at.isoformat(),
                    "keywords": it.keywords,
                    "source": it.source,
                }
                for it in items
            ]
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.debug("[뉴스] 캐시 저장: %s", path)
        except Exception as exc:
            logger.warning("[뉴스] 캐시 저장 실패: %s", exc)

    def _load_cache(self) -> list[NewsItem]:
        """오늘 또는 직전일 캐시 로드."""
        for delta in (0, 1, 2):
            d = datetime.now() - timedelta(days=delta)
            path = self._cache_path(d)
            if path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    items = []
                    for r in raw:
                        items.append(NewsItem(
                            title=r.get("title", ""),
                            summary=r.get("summary", ""),
                            published_at=datetime.fromisoformat(r.get("published_at", datetime.now().isoformat())),
                            keywords=r.get("keywords", []),
                            source="cache",
                        ))
                    return items
                except Exception as exc:
                    logger.warning("[뉴스] 캐시 로드 실패 (%s): %s", path, exc)
        return []

    def _uniform_fallback(self) -> list[NewsItem]:
        """모든 섹터 키워드를 각 1개씩 포함하는 더미 뉴스 반환 (균등 점수 0.5 유도)."""
        items = []
        for sector, keywords in SECTOR_KEYWORDS.items():
            if keywords:
                items.append(NewsItem(
                    title=f"{sector} 시장 동향",
                    summary=" ".join(keywords[:3]),
                    published_at=datetime.now(),
                    keywords=keywords[:3],
                    source="cache",
                ))
        return items


# ─── 유틸 함수 ────────────────────────────────────────────────────

def _clean_html(text: str) -> str:
    """간단한 HTML 태그 제거."""
    import re
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    return text.strip()


def _extract_keywords(text: str) -> list[str]:
    """SECTOR_KEYWORDS 전체 키워드 중 text에 등장하는 것 반환."""
    found = []
    for kws in SECTOR_KEYWORDS.values():
        for kw in kws:
            if kw in text and kw not in found:
                found.append(kw)
    return found


def _parse_rss_date(date_str: str) -> datetime:
    """RSS pubDate 파싱 (다양한 포맷 대응)."""
    if not date_str:
        return datetime.now()
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            import email.utils
            # RFC 2822 파싱 (feedparser 내부와 동일)
            ts = email.utils.parsedate_to_datetime(date_str)
            return ts.replace(tzinfo=None)
        except Exception:
            pass
    return datetime.now()


def _parse_rfc_date(date_str: str) -> datetime:
    """RFC 2822 날짜 파싱 (Naver Search API pubDate)."""
    if not date_str:
        return datetime.now()
    try:
        import email.utils
        ts = email.utils.parsedate_to_datetime(date_str)
        return ts.replace(tzinfo=None)
    except Exception:
        return datetime.now()
