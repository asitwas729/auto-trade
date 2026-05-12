"""
KIS volume-rank API 기반 동적 워치리스트.

모든 단타 전략(S1/S6/S7/S8) 공유. 5분마다 KIS 거래대금 상위 종목 조회 후
- 가격 ≥ INTRADAY_DYNAMIC_MIN_PRICE (동전주 회피)
- 상한가/하한가 도달 제외
- 채권/단기채/MMF형 ETF 이름 제외 (모멘텀 단타 부적합)
조건 충족 종목 N개를 워치리스트로 반환 (KIS API가 이미 거래대금 정렬해서 줌).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from config.constants import (
    INTRADAY_DYNAMIC_INITIAL_SLOTS,
    INTRADAY_DYNAMIC_MAX_SLOTS,
    INTRADAY_DYNAMIC_MIN_PRICE,
    KIS_PATH_VOLUME_RANK,
    KIS_TR,
    MARKET_KOSPI,
)

if TYPE_CHECKING:
    from core.kis_api_client import KISApiClient
    from core.market_data_collector import MarketDataCollector

logger = logging.getLogger(__name__)


# 모멘텀 단타에 부적합한 자산군 (채권/단기채/현금성 ETF 등).
# KIS volume-rank가 거래대금 기준으로 항상 상위에 노출시키지만 일중 변동이
# 거의 없어 S1/S6/S7 같은 돌파·반등 전략이 잡지도 청산하지도 못함.
# 종목명에 하나라도 포함되면 워치리스트 진입을 차단.
_EXCLUDE_NAME_KEYWORDS = (
    # 채권/단기채/MMF형
    "회사채", "단기채", "국공채", "국고채", "은행채", "특수채", "통안채",
    "통화안정", "단기통안", "단기특수", "단기우량", "단기자금", "MMF",
    "CD금리", "KOFR", "초단기", "머니마켓",
    # ETN — 만기/괴리율 위험으로 단타 부적합
    "ETN",
)

# 우선주 종목명 접미사 (보통주와 함께 거래되며 유동성·변동성 패턴 다름).
_PREFERRED_SUFFIXES = ("우", "우B", "1우", "2우B", "1우B", "2우")


def _is_excluded_by_name(name: str) -> bool:
    if not name:
        return False
    if any(kw in name for kw in _EXCLUDE_NAME_KEYWORDS):
        return True
    # 우선주: 이름이 위 접미사로 끝나는 경우 (예: 삼성전자우, LG화학우)
    stripped = name.strip()
    if any(stripped.endswith(suf) for suf in _PREFERRED_SUFFIXES):
        return True
    return False


class DynamicWatchlist:
    """KIS 거래량 급증 종목 동적 선정."""

    def __init__(
        self,
        api: "KISApiClient",
        data: "MarketDataCollector",
        max_slots: int = INTRADAY_DYNAMIC_INITIAL_SLOTS,
    ) -> None:
        self._api = api
        self._data = data
        self._max_slots = max(1, min(max_slots, INTRADAY_DYNAMIC_MAX_SLOTS))

    def set_max_slots(self, n: int) -> None:
        self._max_slots = max(1, min(n, INTRADAY_DYNAMIC_MAX_SLOTS))

    def refresh(self) -> list[dict]:
        """
        KIS volume-rank API에서 거래대금 상위 종목 조회 → 필터 → 상위 N개 반환.
        (FID_BLNG_CLS_CODE="1" → 거래대금 상위 정렬)

        Returns:
            list of dict: [{"code", "name", "price", "prdy_vrss_rate", "avg_amount"}, ...]
        """
        try:
            ranked = self._api.get_volume_rank()
        except Exception as exc:
            logger.warning("[DynamicWL] volume-rank 조회 실패: %s", exc)
            return []

        selected = []
        for row in ranked:
            code = row.get("code", "").strip()
            if not code:
                continue
            try:
                price = int(row.get("price", 0) or 0)
                prdy_vrss_rate = float(row.get("prdy_vrss_rate", 0) or 0)
            except (ValueError, TypeError):
                continue

            # 1. 가격 하한 (동전주 회피). 상한은 두지 않음.
            if price < INTRADAY_DYNAMIC_MIN_PRICE:
                continue
            # 2. 상한가/하한가 도달 제외
            if str(row.get("upbnd_yn", "")) == "Y" or str(row.get("lwbnd_yn", "")) == "Y":
                continue
            # 2-b. 채권/단기채/MMF형 ETF 제외 (이름 기반)
            name = row.get("name", "") or ""
            if _is_excluded_by_name(name):
                logger.debug("[DynamicWL] %s(%s) 채권/단기채 ETF 제외", code, name)
                continue
            # 3. 평균 거래대금은 정보용으로만 기록 (필터하지 않음).
            # KIS volume-rank가 이미 거래대금 상위를 정렬해 주므로, parquet 캐시
            # 누락 종목도 그대로 통과시켜 워치리스트 비어버리는 문제 회피.
            avg_amount = self._get_avg_amount(code)

            selected.append({
                "code": code,
                "name": row.get("name", code),
                "price": price,
                "prdy_vrss_rate": prdy_vrss_rate,
                "avg_amount": avg_amount,
            })
            if len(selected) >= self._max_slots:
                break

        if selected:
            codes_str = ", ".join(
                f"{s['code']}({s['name'][:6]})" for s in selected[:10]
            )
            logger.info(
                "[DynamicWL] 거래대금 상위 %d종목 선정 (top10: %s%s)",
                len(selected), codes_str,
                "..." if len(selected) > 10 else "",
            )
        else:
            logger.info("[DynamicWL] 조건 충족 종목 없음")
        return selected

    def _get_avg_amount(self, code: str) -> float:
        """최근 20영업일 평균 거래대금 (parquet 캐시 → 없으면 0)."""
        try:
            df = self._data.load_daily_ohlcv(code, days=20)
            if df is None or df.empty:
                return 0.0
            if "amount" in df.columns:
                return float(df["amount"].astype(float).mean())
            # amount 없으면 close × volume 추정
            if "close" in df.columns and "volume" in df.columns:
                est = (df["close"].astype(float) * df["volume"].astype(float)).mean()
                return float(est)
        except Exception as exc:
            logger.debug("[DynamicWL] %s 평균 거래대금 조회 실패: %s", code, exc)
        return 0.0
