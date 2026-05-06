"""
S9. 종가 베팅 (간이 오버나이트, 14:30 ~ 15:15)

- 당일 +1% 이상 + vol_ratio ≥ 1.2 + price > MA20 종목 매수
- 익일 09:01 시초가 시장가 매도 (멀티데이 1박)
- 매수 발주 후 즉시 청산 시그널은 발생시키지 않음 (메인 루프가 익일 처리)
- 종목당 1회/일 (재진입 X)
- 오버나이트 리스크: position_ratio 0.10 (보수적)
"""
from __future__ import annotations

import logging
from typing import Optional

from config.constants import S9_PARAMS
from strategies.base_strategy import BaseStrategy, Signal, StrategySignal

logger = logging.getLogger(__name__)


class S9CloseBet(BaseStrategy):

    def __init__(self) -> None:
        super().__init__("S9")
        self._p = S9_PARAMS
        # 오늘 이미 베팅한 종목 (종목당 1회/일, 날짜 키)
        self._bet_today: dict[str, str] = {}  # code → "YYYYMMDD"

    def evaluate(
        self,
        code: str,
        name: str,
        current_price: int,
        prev_close: int,
        ma20: float,
        vol_ratio: float,
        current_time: str,
        position_qty: int = 0,
        today_str: str = "",
    ) -> Optional[StrategySignal]:
        """
        S9는 진입 전용. 청산은 main loop의 _evaluate_s9_overnight_exit이
        익일 09:00에 처리하므로 여기선 SELL 시그널 안 냄.
        """
        if not self.enabled or current_price <= 0:
            return None
        if position_qty > 0:
            return None  # 이미 보유 중 (익일 청산 대기)
        if not (self._p["entry_start"] <= current_time < self._p["entry_end"]):
            return None
        # 오늘 이미 베팅?
        if today_str and self._bet_today.get(code) == today_str:
            return None
        if prev_close <= 0:
            return None
        change_rate = (current_price - prev_close) / prev_close
        if change_rate < self._p["min_change_rate"]:
            return None
        if vol_ratio < self._p["min_vol_ratio"]:
            return None
        if ma20 <= 0 or current_price < ma20:
            return None

        if today_str:
            self._bet_today[code] = today_str
        reason = (
            f"S9 종가베팅 | 변동{change_rate:+.2%} vol{vol_ratio:.1f}x "
            f"MA20위 (익일 시초가 매도)"
        )
        logger.info("[S9] BUY: %s %s", code, reason)
        return StrategySignal(
            signal=Signal.BUY, code=code, name=name, price=current_price,
            reason=reason, strategy="S9",
        )

    def make_bt_func(
        self,
        ma20_default: float = 0.0,
        vol_ratio_default: float = 1.5,
    ):
        """
        백테스트는 익일 청산 로직을 단순화: 다음 row의 open을 청산가로 가정.
        실제 백테스트 wrapper에서 SELL 시그널을 다음날 첫 봉에서 강제 발생시켜야 함.
        """
        def strategy_func(row, state):
            code = str(row.get("code", "TEST"))
            current_time = str(row.get("time", "143000")).zfill(6)
            sig = self.evaluate(
                code=code,
                name=row.get("name", code),
                current_price=int(row.get("close", 0) or 0),
                prev_close=int(row.get("prev_close", 0) or 0),
                ma20=float(row.get("ma20", ma20_default) or 0),
                vol_ratio=float(row.get("vol_ratio", vol_ratio_default) or 0),
                current_time=current_time,
                position_qty=state.get("position_qty", 0),
                today_str=str(row.get("date", "")),
            )
            return sig.signal.name if sig else None
        return strategy_func
