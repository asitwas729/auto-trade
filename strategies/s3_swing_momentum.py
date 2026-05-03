"""
S3. 스윙/모멘텀 전략
- 리포트 목표가 대비 현재가 -20% 이상 할인
- 거래량 증가 + 테마 강도 상승 + 신고가/저항선 돌파
- 보유 3~10일
- +5%~+15% 익절 또는 트레일링 스탑 -3%
- -5% 손절
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from config.constants import S3_PARAMS
from strategies.base_strategy import BaseStrategy, Signal, StrategySignal

logger = logging.getLogger(__name__)


class S3SwingMomentum(BaseStrategy):

    def __init__(self) -> None:
        super().__init__("S3")
        self._p = S3_PARAMS
        # 진입 정보 {code: {entry_date, high_since_entry}}
        self._entries: dict[str, dict] = {}

    def evaluate(
        self,
        code: str,
        name: str,
        current_price: int,
        report_target_price: int,   # 증권사 목표가 (0이면 미제공)
        vol_ratio: float,
        theme_strength: float,      # 테마 강도 점수 (0~1)
        near_52w_high: bool,        # 52주 신고가 근접
        resistance_break: bool,     # 저항선 돌파 여부
        atr_rate: float,
        position_qty: int = 0,
        avg_entry_price: float = 0.0,
        entry_date: Optional[datetime] = None,
        high_since_entry: int = 0,
    ) -> Optional[StrategySignal]:
        if not self.enabled:
            return None

        if position_qty > 0 and avg_entry_price > 0:
            return self._check_exit(
                code, name, current_price, avg_entry_price, position_qty,
                entry_date, high_since_entry
            )

        return self._check_entry(
            code, name, current_price, report_target_price,
            vol_ratio, theme_strength, near_52w_high, resistance_break
        )

    def _check_entry(
        self, code, name, price, target_price, vol_ratio,
        theme_strength, near_52w_high, resistance_break
    ) -> Optional[StrategySignal]:
        reasons = []

        # 1. 목표가 대비 할인율 -20% 이상 (목표가 제공 시)
        if target_price > 0:
            discount = (price - target_price) / target_price
            if discount > self._p["target_discount_from_report"]:
                return None
            reasons.append(f"목표가할인{discount:.2%}")

        # 2. 거래량 증가
        if vol_ratio < 1.5:
            return None
        reasons.append(f"거래량{vol_ratio:.1f}x")

        # 3. 테마 강도
        if theme_strength < 0.5:
            return None
        reasons.append(f"테마{theme_strength:.2f}")

        # 4. 신고가 근접 또는 저항선 돌파 (둘 중 하나)
        if not (near_52w_high or resistance_break):
            return None
        reasons.append("신고가돌파" if near_52w_high else "저항선돌파")

        logger.info(f"[S3] {name}({code}) 진입 | {', '.join(reasons)}")
        return StrategySignal(
            signal=Signal.BUY,
            code=code, name=name, price=price,
            reason=", ".join(reasons),
            strategy="S3",
        )

    def _check_exit(
        self, code, name, price, avg_price, qty,
        entry_date, high_since_entry
    ) -> Optional[StrategySignal]:
        pnl_rate = (price - avg_price) / avg_price

        # 보유 기간 초과 (10일 이상)
        if entry_date and (datetime.now() - entry_date).days >= self._p["hold_days_max"]:
            logger.info(f"[S3] {name}({code}) 보유기간 초과 청산")
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=qty, reason="보유기간초과", strategy="S3", sell_ratio=1.0
            )

        # 트레일링 스탑 (진입 후 최고가 대비 -3%)
        if high_since_entry > 0:
            trailing = (price - high_since_entry) / high_since_entry
            if trailing <= self._p["trailing_stop"]:
                logger.info(f"[S3] {name}({code}) 트레일링 스탑 {trailing:.2%}")
                return StrategySignal(
                    signal=Signal.SELL, code=code, name=name, price=price,
                    quantity=qty, reason=f"트레일링{trailing:.2%}",
                    strategy="S3", sell_ratio=1.0
                )

        # 익절 +15%
        if pnl_rate >= self._p["profit_target_max"]:
            logger.info(f"[S3] {name}({code}) 최대 익절 {pnl_rate:.2%}")
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=qty, reason=f"익절{pnl_rate:.2%}",
                strategy="S3", sell_ratio=1.0
            )

        # 손절 -5%
        if pnl_rate <= self._p["stop_loss_rate"]:
            logger.info(f"[S3] {name}({code}) 손절 {pnl_rate:.2%}")
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=qty, reason=f"손절{pnl_rate:.2%}",
                strategy="S3", sell_ratio=1.0
            )

        return None
