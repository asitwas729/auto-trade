"""
S8. 오후 추세 추격 (13:00 ~ 14:30)

- 당일 +2% ~ +12% 상승 종목 (과열 회피)
- 5개 3분봉 우상향 비율 ≥ 0.6 + price > MA5 + vol_ratio ≥ 1.5
- 익절: +2% 전량
- 손절: -1.5% 하드, trail high × 0.985
- 14:50 시장가 강제 청산 (S9 베팅 전 마감)
"""
from __future__ import annotations

import logging
from typing import Optional

from config.constants import S8_PARAMS
from strategies.base_strategy import BaseStrategy, Signal, StrategySignal

logger = logging.getLogger(__name__)


class S8AfternoonTrend(BaseStrategy):

    def __init__(self) -> None:
        super().__init__("S8")
        self._p = S8_PARAMS
        self._entry_highs: dict[str, int] = {}
        # 당일 진입 시그널 dedup ({code: yyyymmdd})
        self._signaled_today: dict[str, str] = {}

    def evaluate(
        self,
        code: str,
        name: str,
        current_price: int,
        prev_close: int,
        ma5: float,
        vol_ratio: float,
        three_min_up_ratio: float,
        current_time: str,
        position_qty: int = 0,
        avg_entry_price: float = 0.0,
    ) -> Optional[StrategySignal]:
        if not self.enabled or current_price <= 0:
            return None

        if position_qty > 0 and avg_entry_price > 0:
            return self._check_exit(
                code, name, current_price,
                avg_entry_price, position_qty, current_time,
            )

        if not (self._p["entry_start"] <= current_time < self._p["entry_end"]):
            return None
        return self._check_entry(
            code, name, current_price, prev_close, ma5, vol_ratio, three_min_up_ratio,
        )

    def _check_entry(
        self,
        code: str,
        name: str,
        price: int,
        prev_close: int,
        ma5: float,
        vol_ratio: float,
        up_ratio: float,
    ) -> Optional[StrategySignal]:
        if prev_close <= 0:
            return None
        change_rate = (price - prev_close) / prev_close
        if not (self._p["min_change_rate"] <= change_rate < self._p["max_change_rate"]):
            return None
        if up_ratio < self._p["min_three_min_up_ratio"]:
            return None
        if ma5 <= 0 or price < ma5:
            return None
        if vol_ratio < self._p["min_vol_ratio"]:
            return None

        # 당일 dedup
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y%m%d")
        if self._signaled_today.get(code) == today:
            return None

        self._entry_highs[code] = price
        self._signaled_today[code] = today
        reason = (
            f"S8 오후추세 | 변동{change_rate:+.2%} MA5위 "
            f"vol{vol_ratio:.1f}x up{up_ratio:.0%}"
        )
        logger.info("[S8] BUY: %s %s", code, reason)
        return StrategySignal(
            signal=Signal.BUY, code=code, name=name, price=price,
            reason=reason, strategy="S8",
        )

    def _check_exit(
        self,
        code: str,
        name: str,
        price: int,
        avg_price: float,
        qty: int,
        current_time: str,
    ) -> Optional[StrategySignal]:
        prev_high = self._entry_highs.get(code, int(avg_price))
        self._entry_highs[code] = max(prev_high, price)
        high_since_entry = self._entry_highs[code]

        pnl_rate = (price - avg_price) / avg_price if avg_price > 0 else 0.0

        # 1. 14:50 강제
        if current_time >= self._p["forced_close"]:
            return self._sell(code, name, price, qty, "S8 마감 시장가", forced=True)
        # 2. 트레일 (high × 0.985)
        if high_since_entry > 0 and price < high_since_entry * (1 - self._p["trail_pullback"]):
            return self._sell(code, name, price, qty, f"S8 트레일 {pnl_rate:+.2%}")
        # 3. 하드 손절
        if pnl_rate <= self._p["stop_loss"]:
            return self._sell(code, name, price, qty, f"S8 손절 {pnl_rate:+.2%}")
        # 4. 익절 +2%
        if pnl_rate >= self._p["take_profit"]:
            return self._sell(code, name, price, qty, f"S8 익절 {pnl_rate:+.2%}")
        return None

    def _sell(self, code, name, price, qty, reason, forced=False) -> StrategySignal:
        is_forced = forced or any(
            kw in reason for kw in ("손절", "트레일", "마감")
        )
        logger.info("[S8] SELL: %s %s%s", code, reason, " (forced)" if is_forced else "")
        sig = StrategySignal(
            signal=Signal.SELL, code=code, name=name, price=price,
            quantity=qty, reason=reason, strategy="S8", sell_ratio=1.0,
            is_forced=is_forced,
        )
        self._entry_highs.pop(code, None)
        return sig

    def make_bt_func(
        self,
        ma5_default: float = 0.0,
        vol_ratio_default: float = 2.0,
        up_ratio_default: float = 0.7,
    ):
        def strategy_func(row, state):
            code = str(row.get("code", "TEST"))
            current_time = str(row.get("time", "133000")).zfill(6)
            sig = self.evaluate(
                code=code,
                name=row.get("name", code),
                current_price=int(row.get("close", 0) or 0),
                prev_close=int(row.get("prev_close", 0) or 0),
                ma5=float(row.get("ma5", ma5_default) or 0),
                vol_ratio=float(row.get("vol_ratio", vol_ratio_default) or 0),
                three_min_up_ratio=float(row.get("three_min_up_ratio", up_ratio_default) or 0),
                current_time=current_time,
                position_qty=state.get("position_qty", 0),
                avg_entry_price=state.get("avg_price", 0.0),
            )
            return sig.signal.name if sig else None
        return strategy_func
