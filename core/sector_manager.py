"""
섹터 매니저 (S5 전략용)

역할:
  1. 뉴스 항목 → 섹터별 점수(0~1) 계산
  2. 섹터 로테이션(ROTATION_COMPETING) 적용 → 경쟁 섹터 score × 0.5 감산
  3. 대장주 선정: 시가총액 + 거래량 기준, 분기 캐시(90일) 자동 갱신
  4. 당일 진입 후보 StockRankInfo 리스트 반환

캐시:
  data/sector_leaders_cache.json
  형식: {"updated_at": "YYYY-MM-DD", "next_update": "YYYY-MM-DD", "leaders": {...}}
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from config.constants import (
    ROTATION_COMPETING,
    SECTOR_KEYWORDS,
    SECTOR_UNIVERSE,
)
from config.settings import settings

if TYPE_CHECKING:
    from core.kis_api_client import KISApiClient
    from core.news_collector import NewsItem

logger = logging.getLogger(__name__)

_CACHE_PATH = settings.DATA_DIR / "sector_leaders_cache.json"
_CACHE_TTL_DAYS = 90


@dataclass
class StockRankInfo:
    code: str
    name: str
    sector: str
    sector_score: float       # 0~1 뉴스 키워드 매칭 점수
    leader_score: float       # change_rate×0.4 + vol_ratio×0.3 + trade_value_ratio×0.3
    change_rate: float        # 당일 등락률
    vol_ratio: float          # 5일 평균 대비 거래량 배수
    market_cap: float = 0.0   # 시가총액 (캐시 기준)


class SectorManager:
    """
    사용 예:
        sm = SectorManager(api_client)
        sector_scores = sm.score_sectors(news_items)
        leaders = sm.select_leaders(sector_scores)
    """

    def __init__(self, api_client: Optional["KISApiClient"] = None) -> None:
        self._api = api_client
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._leaders: dict[str, list[dict]] = self._load_or_refresh_cache()

    # ─── 섹터 점수 계산 ────────────────────────────────────────────

    def score_sectors(self, news_items: list["NewsItem"]) -> dict[str, float]:
        """
        뉴스 항목에서 섹터별 키워드 매칭 점수 0~1 계산.
        → ROTATION_COMPETING 적용: 경쟁 쌍 중 낮은 쪽 score × 0.5 감산.
        """
        all_text = " ".join(
            f"{item.title} {item.summary}" for item in news_items
        )
        raw: dict[str, float] = {}

        for sector, keywords in SECTOR_KEYWORDS.items():
            if not keywords:
                raw[sector] = 0.0
                continue
            hit = sum(1 for kw in keywords if kw in all_text)
            # 최대 점수 = 키워드 전체 매칭, 정규화
            raw[sector] = min(hit / len(keywords), 1.0)

        # 섹터 로테이션 적용
        scores = dict(raw)
        for sec_a, sec_b in ROTATION_COMPETING:
            sa = scores.get(sec_a, 0.0)
            sb = scores.get(sec_b, 0.0)
            if sa > 0 and sb > 0:
                # 낮은 쪽을 50% 감산
                if sa >= sb:
                    scores[sec_b] = sb * 0.5
                else:
                    scores[sec_a] = sa * 0.5

        logger.debug("[섹터] 점수: %s", {k: f"{v:.2f}" for k, v in scores.items()})
        return scores

    # ─── 대장주 선정 ───────────────────────────────────────────────

    def select_leaders(
        self,
        sector_scores: dict[str, float],
        price_cache: Optional[dict[str, dict]] = None,
    ) -> list[StockRankInfo]:
        """
        sector_score ≥ min_sector_score 인 섹터에서 leader_score 1위 종목 선정.

        Args:
            sector_scores: score_sectors() 결과
            price_cache: {code: {"change_rate": float, "vol_ratio": float, "trade_value": float}}
                         None이면 vol_ratio=1, change_rate=0 으로 대체 (백테스트/테스트용)
        """
        from config.constants import S5_PARAMS
        min_score = S5_PARAMS["min_sector_score"]

        results: list[StockRankInfo] = []
        for sector, score in sector_scores.items():
            if score < min_score:
                continue
            candidates = self._leaders.get(sector, [])
            if not candidates:
                candidates = [
                    {"code": c, "name": c, "market_cap": 0.0}
                    for c in SECTOR_UNIVERSE.get(sector, [])
                ]

            best: Optional[StockRankInfo] = None
            best_leader_score = -1.0
            for cand in candidates:
                code = cand["code"]
                name = cand.get("name", code)
                market_cap = cand.get("market_cap", 0.0)

                pdata = (price_cache or {}).get(code, {})
                change_rate = pdata.get("change_rate", 0.0)
                vol_ratio = pdata.get("vol_ratio", 1.0)
                trade_value_ratio = pdata.get("trade_value_ratio", 1.0)

                leader_score = (
                    change_rate * 0.4
                    + vol_ratio * 0.3
                    + trade_value_ratio * 0.3
                )
                if leader_score > best_leader_score:
                    best_leader_score = leader_score
                    best = StockRankInfo(
                        code=code,
                        name=name,
                        sector=sector,
                        sector_score=score,
                        leader_score=leader_score,
                        change_rate=change_rate,
                        vol_ratio=vol_ratio,
                        market_cap=market_cap,
                    )
            if best:
                results.append(best)

        results.sort(key=lambda x: x.sector_score, reverse=True)
        logger.info(
            "[섹터] 대장주 선정 %d개: %s",
            len(results),
            [(r.sector, r.code, f"{r.sector_score:.2f}") for r in results],
        )
        return results

    # ─── 분기 캐시 관리 ────────────────────────────────────────────

    def _load_or_refresh_cache(self) -> dict[str, list[dict]]:
        """캐시 파일 로드. 만료됐거나 없으면 기본 SECTOR_UNIVERSE 사용."""
        if _CACHE_PATH.exists():
            try:
                raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
                next_upd = datetime.strptime(raw.get("next_update", "2000-01-01"), "%Y-%m-%d")
                if datetime.now() < next_upd:
                    logger.debug("[섹터] 캐시 로드: %s", _CACHE_PATH)
                    return raw.get("leaders", {})
                else:
                    logger.info("[섹터] 캐시 만료 → 기본 SECTOR_UNIVERSE 사용 (refresh_leaders() 호출 필요)")
            except Exception as exc:
                logger.warning("[섹터] 캐시 로드 실패: %s", exc)

        # 캐시 없거나 만료 → SECTOR_UNIVERSE 로 기본값 생성
        return {
            sector: [{"code": c, "name": c, "market_cap": 0.0, "avg_volume_20d": 0}
                     for c in codes]
            for sector, codes in SECTOR_UNIVERSE.items()
        }

    def refresh_leaders(self) -> None:
        """
        KIS API로 섹터별 시가총액·거래량 조회 → 상위 5개 선정 → 캐시 저장.
        API 없이 호출 시 SECTOR_UNIVERSE 그대로 저장.
        """
        leaders: dict[str, list[dict]] = {}
        for sector, codes in SECTOR_UNIVERSE.items():
            ranked = []
            for code in codes:
                entry: dict = {"code": code, "name": code, "market_cap": 0.0, "avg_volume_20d": 0}
                if self._api:
                    try:
                        price_info = self._api.get_price(code)
                        # KIS get_price 반환값에 market_cap 포함 여부 확인
                        market_cap = getattr(price_info, "market_cap", 0) or 0
                        entry["name"] = getattr(price_info, "name", code) or code
                        entry["market_cap"] = market_cap
                    except Exception as exc:
                        logger.debug("[섹터] %s 시가총액 조회 실패: %s", code, exc)
                ranked.append(entry)
            # 시가총액 내림차순 정렬, 상위 5개
            ranked.sort(key=lambda x: x["market_cap"], reverse=True)
            leaders[sector] = ranked[:5]

        data = {
            "updated_at": datetime.now().strftime("%Y-%m-%d"),
            "next_update": (datetime.now() + timedelta(days=_CACHE_TTL_DAYS)).strftime("%Y-%m-%d"),
            "leaders": leaders,
        }
        try:
            _CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self._leaders = leaders
            logger.info("[섹터] 대장주 캐시 갱신 완료 → %s", _CACHE_PATH)
        except Exception as exc:
            logger.error("[섹터] 캐시 저장 실패: %s", exc)

    def get_watchlist(self) -> list[str]:
        """섹터 UNIVERSE 전체 종목 코드 (WebSocket 구독용)."""
        codes: list[str] = []
        for lst in self._leaders.values():
            for item in lst:
                if item["code"] not in codes:
                    codes.append(item["code"])
        return codes
