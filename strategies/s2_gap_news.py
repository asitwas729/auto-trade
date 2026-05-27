"""
S2. 갭/뉴스/시간외 전략
- 전일 시간외 +2% 이상
- 09:05 이후 눌림 확인 후 진입
- +0.8% 50% 1차 익절, +1.8% 전량 익절 또는 09:30 이전 청산
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
        self._partial_sold: set[str] = set()

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

        # 09:30 시간초과 강제 청산
        if current_time >= self._p["cutoff_time"]:
            logger.info(f"[S2] {name}({code}) 시간초과 청산")
            self._partial_sold.discard(code)
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=qty, reason="시간초과청산", strategy="S2",
                sell_ratio=1.0, is_forced=True,
            )

        # 익절 2: +1.8% 전량
        if pnl_rate >= self._p["profit_target"]:
            logger.info(f"[S2] {name}({code}) 2차익절 {pnl_rate:.2%}")
            self._partial_sold.discard(code)
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=qty, reason=f"2차익절{pnl_rate:.2%}", strategy="S2", sell_ratio=1.0
            )

        # 익절 1: +0.8% 50%
        partial_target = self._p.get("profit_target_partial", 0.008)
        if pnl_rate >= partial_target and qty > 1 and code not in self._partial_sold:
            self._partial_sold.add(code)
            sell_qty = max(1, qty // 2)
            logger.info(f"[S2] {name}({code}) 1차익절 {pnl_rate:.2%}")
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=sell_qty, reason=f"1차익절{pnl_rate:.2%}", strategy="S2", sell_ratio=0.5
            )

        # 손절 -1.5%
        if pnl_rate <= self._p["stop_loss_rate"]:
            logger.info(f"[S2] {name}({code}) 손절 {pnl_rate:.2%}")
            self._partial_sold.discard(code)
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=qty, reason=f"손절{pnl_rate:.2%}", strategy="S2",
                sell_ratio=1.0, is_forced=True,
            )

        return None
