"""
S2. 갭/뉴스/시간외 전략
- 전일 시간외 +3% 이상
- 다음날 시초가 또는 09:05 이후 눌림 확인 후 진입
- +1.2% 익절 또는 09:20 이전 청산
- -1.5% 손절
"""

from __future__ import annotations

import logging
from typing import Optional

from config.constants import S2_PARAMS
from strategies.base_strategy import BaseStrategy, Signal, StrategySignal

logger = logging.getLogger(__name__)


class S2GapNews(BaseStrategy):

    def __init__(self) -> None:
        super().__init__("S2")
        self._p = S2_PARAMS

    def evaluate(
        self,
        code: str,
        name: str,
        current_price: int,
        open_price: int,
        prev_close: int,
        after_hours_close: int,   # 전일 시간외 종가
        vol_ratio: float,
        has_news: bool,            # 관련 뉴스/테마 존재 여부
        current_time: str,         # "HHmmss"
        position_qty: int = 0,
        avg_entry_price: float = 0.0,
    ) -> Optional[StrategySignal]:
        if not self.enabled:
            return None

        if position_qty > 0 and avg_entry_price > 0:
            return self._check_exit(code, name, current_price, avg_entry_price, position_qty, current_time)

        return self._check_entry(
            code, name, current_price, open_price, prev_close,
            after_hours_close, vol_ratio, has_news, current_time
        )

    def _check_entry(
        self, code, name, price, open_price, prev_close, after_hours_close,
        vol_ratio, has_news, current_time
    ) -> Optional[StrategySignal]:
        # 진입 시간 (09:05 이후)
        if current_time < "090500":
            return None

        # 09:20 이후면 진입 금지
        if current_time >= self._p["cutoff_time"]:
            return None

        # 1. 시간외 상승 +3% 이상
        after_gap = (after_hours_close - prev_close) / prev_close if prev_close > 0 else 0
        if after_gap < self._p["after_hours_gap"]:
            return None

        # 2. 거래량 증가
        if vol_ratio < 1.3:
            return None

        # 3. 뉴스/테마 존재
        if not has_news:
            return None

        # 4. 눌림 확인 (시초가 대비 현재가 소폭 하락 후)
        pullback = (price - open_price) / open_price if open_price > 0 else 0
        if pullback > 0.005:   # 이미 +0.5% 이상 올라있으면 추격 진입 금지
            return None

        logger.info(f"[S2] {name}({code}) 진입 | 시간외갭={after_gap:.2%} 눌림={pullback:.2%}")
        return StrategySignal(
            signal=Signal.BUY,
            code=code, name=name, price=price,
            reason=f"시간외갭{after_gap:.2%}",
            strategy="S2",
        )

    def _check_exit(self, code, name, price, avg_price, qty, current_time) -> Optional[StrategySignal]:
        pnl_rate = (price - avg_price) / avg_price

        # 09:20 이전 강제 청산
        if current_time >= self._p["cutoff_time"]:
            logger.info(f"[S2] {name}({code}) 09:20 청산")
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=qty, reason="시간초과청산", strategy="S2", sell_ratio=1.0
            )

        # 익절 +1.2%
        if pnl_rate >= self._p["profit_target"]:
            logger.info(f"[S2] {name}({code}) 익절 {pnl_rate:.2%}")
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=qty, reason=f"익절{pnl_rate:.2%}", strategy="S2", sell_ratio=1.0
            )

        # 손절 -1.5%
        if pnl_rate <= self._p["stop_loss_rate"]:
            logger.info(f"[S2] {name}({code}) 손절 {pnl_rate:.2%}")
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=qty, reason=f"손절{pnl_rate:.2%}", strategy="S2", sell_ratio=1.0
            )

        return None
