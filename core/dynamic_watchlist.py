"""
KIS volume-rank API 기반 동적 워치리스트.

S1 단타 종목 선정용. 5분마다 KIS 거래대금/거래량 급증 종목 조회 후
- prdy_vrss_rate ≥ 500.0 (전일 거래량 대비 5배+)
- 가격 1,000~500,000원
- 최근 20영업일 평균 거래대금 ≥ 1억원
- 상한가/하한가 도달 제외
조건 충족 종목 N개를 워치리스트로 반환.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from config.constants import (
    KIS_PATH_VOLUME_RANK,
    KIS_TR,
    MARKET_KOSPI,
    S1_DYNAMIC_INITIAL_SLOTS,
    S1_DYNAMIC_MAX_SLOTS,
    S1_DYNAMIC_MAX_PRICE,
    S1_DYNAMIC_MIN_AVG_AMOUNT,
    S1_DYNAMIC_MIN_PRICE,
    S1_DYNAMIC_PRDY_VRSS_RATE_MIN,
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
        max_slots: int = S1_DYNAMIC_INITIAL_SLOTS,
    ) -> None:
        self._api = api
        self._data = data
        self._max_slots = max(1, min(max_slots, S1_DYNAMIC_MAX_SLOTS))

    def set_max_slots(self, n: int) -> None:
        self._max_slots = max(1, min(n, S1_DYNAMIC_MAX_SLOTS))

    def refresh(self) -> list[dict]:
        """
        KIS volume-rank API에서 거래대금 급증 종목 조회 → 필터 → 상위 N개 반환.

        Returns:
            list of dict: [{"code", "name", "price", "prdy_vrss_rate"}, ...]
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

            # 1. 전일 대비 거래량 비율
            if prdy_vrss_rate < S1_DYNAMIC_PRDY_VRSS_RATE_MIN:
                continue
            # 2. 가격 범위
            if not (S1_DYNAMIC_MIN_PRICE <= price <= S1_DYNAMIC_MAX_PRICE):
                continue
            # 3. 상한가/하한가 도달 제외 (KIS 응답 hts_kor_isnm/upbnd_yn 등으로 판단)
            if str(row.get("upbnd_yn", "")) == "Y" or str(row.get("lwbnd_yn", "")) == "Y":
                continue
            # 4. 최근 20영업일 평균 거래대금 (parquet 캐시)
            avg_amount = self._get_avg_amount(code)
            if avg_amount < S1_DYNAMIC_MIN_AVG_AMOUNT:
                continue

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
                f"{s['code']}({s['prdy_vrss_rate']:.0f}%)" for s in selected
            )
            logger.info(
                "[DynamicWL] 선정 %d종목: %s",
                len(selected), codes_str,
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
