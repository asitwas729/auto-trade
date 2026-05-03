"""
S4. 변동성 필터 / 헤지 / 거래 ON-OFF
- KOSPI/KOSDAQ 지수 변동성, ATR, 거래대금, 상승/하락 종목 수, 급락 여부
- 시장 위험도 HIGH이면 S1/S2 OFF
- 급락장에서는 신규 매수 금지 + 보유 종목 축소
"""

from __future__ import annotations

import logging
from typing import Optional

from config.constants import S4_PARAMS
from strategies.base_strategy import BaseStrategy, Signal, StrategySignal

logger = logging.getLogger(__name__)


class MarketRiskLevel:
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRASH = "CRASH"


class S4VolatilityFilter(BaseStrategy):

    def __init__(self) -> None:
        super().__init__("S4")
        self._p = S4_PARAMS
        self._current_risk_level = MarketRiskLevel.LOW

    def evaluate(
        self,
        kospi_change_rate: float,       # KOSPI 당일 등락률
        kosdaq_change_rate: float,      # KOSDAQ 당일 등락률
        kospi_atr_rate: float,          # KOSPI ATR 비율
        advance_count: int,             # 상승 종목 수
        decline_count: int,             # 하락 종목 수
        total_market_amount: float,     # 전체 시장 거래대금
        avg_market_amount: float,       # 20일 평균 거래대금
        **kwargs,
    ) -> str:
        """시장 위험도 반환: LOW | MEDIUM | HIGH | CRASH"""
        score = 0

        # 1. 급락 감지 (-2% 이하)
        min_idx = min(kospi_change_rate, kosdaq_change_rate)
        if min_idx <= self._p["circuit_breaker_drop"]:
            self._current_risk_level = MarketRiskLevel.CRASH
            logger.warning(f"[S4] 급락장 감지: 지수변동 {min_idx:.2%}")
            return MarketRiskLevel.CRASH

        # 2. 높은 변동성 (ATR)
        if kospi_atr_rate >= self._p["atr_high_threshold"]:
            score += 2

        # 3. 하락 종목 우세
        total = advance_count + decline_count
        if total > 0:
            adv_ratio = advance_count / total
            if adv_ratio < self._p["market_advance_decline_min"]:
                score += 2

        # 4. 거래대금 급감 (평균 대비 70% 미만)
        if avg_market_amount > 0:
            vol_ratio = total_market_amount / avg_market_amount
            if vol_ratio < self._p["volume_threshold_ratio"]:
                score += 1

        # 5. 지수 하락
        if min_idx < -0.01:
            score += 1

        if score >= 4:
            level = MarketRiskLevel.HIGH
        elif score >= 2:
            level = MarketRiskLevel.MEDIUM
        else:
            level = MarketRiskLevel.LOW

        if level != self._current_risk_level:
            logger.info(f"[S4] 시장 위험도 변경: {self._current_risk_level} → {level} (점수={score})")
            self._current_risk_level = level

        return level

    @property
    def is_market_risky(self) -> bool:
        return self._current_risk_level in (MarketRiskLevel.HIGH, MarketRiskLevel.CRASH)

    @property
    def is_crash(self) -> bool:
        return self._current_risk_level == MarketRiskLevel.CRASH

    @property
    def current_risk_level(self) -> str:
        return self._current_risk_level
