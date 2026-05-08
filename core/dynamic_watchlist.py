"""
KIS volume-rank API 기반 동적 워치리스트.

모든 단타 전략(S1/S6/S7/S8) 공유. 5분마다 KIS 거래대금 상위 종목 조회 후
- 가격 ≥ INTRADAY_DYNAMIC_MIN_PRICE (동전주 회피)
- 상한가/하한가 도달 제외
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
