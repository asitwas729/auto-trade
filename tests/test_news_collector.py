"""
NewsCollector 단위 테스트

Run:
    python -m pytest tests/test_news_collector.py -v -s
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.news_collector import (
    NewsCollector,
    NewsItem,
    _clean_html,
    _extract_keywords,
)
from config.constants import SECTOR_KEYWORDS


# ─── 유틸 테스트 ──────────────────────────────────────────────────

def test_clean_html():
    assert _clean_html("<b>반도체</b> 강세") == "반도체 강세"
    assert _clean_html("A &amp; B") == "A & B"
    assert _clean_html("") == ""


def test_extract_keywords_hit():
    text = "오늘 반도체 HBM 강세, 2차전지 배터리도 급등"
    kws = _extract_keywords(text)
    assert "반도체" in kws
    assert "HBM" in kws
    assert "배터리" in kws


def test_extract_keywords_no_hit():
    text = "오늘 날씨가 맑습니다"
    kws = _extract_keywords(text)
    assert kws == []


# ─── NewsCollector 테스트 ─────────────────────────────────────────

class TestNewsCollector:

    def test_uniform_fallback_covers_all_sectors(self):
        """균등 폴백이 모든 섹터 커버."""
        nc = NewsCollector()
        items = nc._uniform_fallback()
        sectors_covered = {item.title.replace(" 시장 동향", "") for item in items}
        for sector in SECTOR_KEYWORDS:
            assert sector in sectors_covered, f"섹터 누락: {sector}"

    def test_save_and_load_cache(self, tmp_path, monkeypatch):
        """캐시 저장 후 로드 검증."""
        monkeypatch.setattr("core.news_collector._CACHE_DIR", tmp_path)
        nc = NewsCollector()

        items = [
            NewsItem(
                title="반도체 강세",
                summary="HBM 수요 급증",
                published_at=datetime.now(),
                keywords=["반도체", "HBM"],
                source="naver_rss",
            )
        ]
        nc._save_cache(items)
        loaded = nc._load_cache()
        assert len(loaded) == 1
        assert loaded[0].title == "반도체 강세"
        assert "반도체" in loaded[0].keywords

    def test_fetch_uses_fallback_when_rss_and_api_fail(self, monkeypatch):
        """RSS/API 모두 실패 시 캐시 폴백 사용."""
        nc = NewsCollector()
        monkeypatch.setattr(nc, "_parse_rss", lambda: [])
        monkeypatch.setattr(nc, "_search_api", lambda q: [])
        monkeypatch.setattr(nc, "_load_cache", lambda: [
            NewsItem(title="캐시 뉴스", summary="테스트", published_at=datetime.now(), source="cache")
        ])
        result = nc.fetch()
        assert len(result) == 1
        assert result[0].source == "cache"

    def test_fetch_uses_uniform_fallback_when_all_fail(self, monkeypatch):
        """RSS/API/캐시 모두 실패 시 균등 폴백 사용."""
        nc = NewsCollector()
        monkeypatch.setattr(nc, "_parse_rss", lambda: [])
        monkeypatch.setattr(nc, "_search_api", lambda q: [])
        monkeypatch.setattr(nc, "_load_cache", lambda: [])
        result = nc.fetch()
        assert len(result) > 0
        # 균등 폴백은 모든 섹터에 하나씩
        assert len(result) == len(SECTOR_KEYWORDS)

    def test_fetch_prefers_rss(self, monkeypatch):
        """RSS 성공 시 RSS 결과 반환."""
        nc = NewsCollector()
        rss_item = NewsItem(
            title="RSS 뉴스",
            summary="테스트",
            published_at=datetime.now(),
            source="naver_rss",
        )
        monkeypatch.setattr(nc, "_parse_rss", lambda: [rss_item])
        monkeypatch.setattr(nc, "_search_api", lambda q: [])
        result = nc.fetch()
        assert result[0].source == "naver_rss"


# ─── SectorManager 통합 테스트 ───────────────────────────────────

class TestSectorManagerScoring:

    def test_score_sectors_basic(self):
        from core.sector_manager import SectorManager
        sm = SectorManager(api_client=None)
        items = [
            NewsItem(
                title="반도체 HBM 급등",
                summary="SK하이닉스 삼성전자 주목",
                published_at=datetime.now(),
                source="naver_rss",
            )
        ]
        scores = sm.score_sectors(items)
        assert "반도체" in scores
        assert scores["반도체"] > 0.0
        # 관련 없는 섹터는 낮아야 함
        assert scores["바이오"] == 0.0 or scores["바이오"] < scores["반도체"]

    def test_rotation_competing_reduces_lower_score(self):
        from core.sector_manager import SectorManager
        sm = SectorManager(api_client=None)

        # 반도체 키워드만 가득 채운 뉴스 → 반도체 높음, 2차전지 낮음 → 2차전지 감산
        kws_semi = SECTOR_KEYWORDS["반도체"]
        kws_battery = SECTOR_KEYWORDS["2차전지"][:2]  # 2차전지도 약간 등장

        items = [
            NewsItem(
                title=" ".join(kws_semi) + " " + " ".join(kws_battery),
                summary="",
                published_at=datetime.now(),
                source="naver_rss",
            )
        ]
        scores = sm.score_sectors(items)
        # ("반도체", "2차전지") 쌍이므로 낮은 쪽이 0.5 감산
        if scores["반도체"] > scores["2차전지"]:
            # 2차전지가 더 낮으므로 원래 값의 절반이어야 함
            raw_battery = min(len(kws_battery) / len(SECTOR_KEYWORDS["2차전지"]), 1.0)
            assert scores["2차전지"] <= raw_battery * 0.5 + 1e-6

    def test_select_leaders_returns_candidates(self):
        from core.sector_manager import SectorManager
        sm = SectorManager(api_client=None)
        sector_scores = {"반도체": 0.8, "바이오": 0.1}
        leaders = sm.select_leaders(sector_scores)
        assert len(leaders) == 1  # 바이오는 min_score 미달
        assert leaders[0].sector == "반도체"
