"""
S7. 눌림목 매수 (10:00 ~ 14:00, 11:30~13:00 점심 신규진입 차단)

- 일봉 추세 (price > ma20 > ma60) 상승 종목에서
- 당일 open 대비 +1% 이상 고점 형성 후
- 고점 대비 -1.5% ~ -4% 조정 + 3분봉 재반등 확인 시 진입
- 익절: +1% 50%, +2.5% 전량
- 손절: today_high × 0.95 이탈 (추세 깨짐), -2% 하드
- 시간 손절: 90분 보유 + 수익 < 0.3%
- 15:15 시장가 강제 청산
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from config.constants import S7_PARAMS
from strategies.base_strategy import BaseStrategy, Signal, StrategySignal

logger = logging.getLogger(__name__)


class S7Pullback(BaseStrategy):

    def __init__(self) -> None:
        super().__init__("S7")
        self._p = S7_PARAMS
        # 진입 시각 (시간 손절용)
        self._entry_times: dict[str, datetime] = {}
        # 당일 진입 시그널 dedup ({code: yyyymmdd})
        self._signaled_today: dict[str, str] = {}
        # 트레일링용 최고가
        self._high_since_entry: dict[str, int] = {}
        # 1차 익절 완료 추적
        self._partial_sold: set[str] = set()

    def evaluate(
        self,
        code: str,
        name: str,
        current_price: int,
        today_open: int,
        today_high: int,
        ma20: float,
        ma60: float,
        vol_ratio: float,
        three_min_up_ratio: float,
        current_time: str,
        position_qty: int = 0,
        avg_entry_price: float = 0.0,
        now_dt: Optional[datetime] = None,
    ) -> Optional[StrategySignal]:
        if not self.enabled or current_price <= 0:
            return None

        # 보유 중이면 청산 검사
        if position_qty > 0 and avg_entry_price > 0:
            return self._check_exit(
                code, name, current_price, today_high,
                avg_entry_price, position_qty, current_time, now_dt,
            )

        # 진입 시간 가드
        if not (self._p["entry_start"] <= current_time < self._p["entry_end"]):
            return None
        # 점심시간 신규 진입 차단
        if self._p["lunch_start"] <= current_time < self._p["lunch_end"]:
            return None

        return self._check_entry(
            code, name, current_price, today_open, today_high,
            ma20, ma60, vol_ratio, three_min_up_ratio, now_dt,
        )

    def _check_entry(
        self,
        code: str,
        name: str,
        price: int,
        today_open: int,
        today_high: int,
        ma20: float,
        ma60: float,
        vol_ratio: float,
        up_ratio: float,
        now_dt: Optional[datetime],
    ) -> Optional[StrategySignal]:
        # 일봉 상승 추세
        if not (price > ma20 > ma60 > 0):
            return None
        # 당일 고점 형성 (open 대비 +1% 이상 갔던 적)
        if today_open <= 0 or today_high < today_open * (1 + self._p["uptrend_min_intraday"]):
            return None
        # 눌림목 범위: 가격대에 따라 소형주/대형주 다른 기준 적용
        pullback_pct = (price - today_high) / today_high if today_high > 0 else 0.0
        large_threshold = self._p.get("large_price_threshold", 100_000)
        if price >= large_threshold:
            pb_min = self._p.get("pullback_min_pct_large", self._p["pullback_min_pct"])
            pb_max = self._p.get("pullback_max_pct_large", self._p["pullback_max_pct"])
        else:
            pb_min = self._p["pullback_min_pct"]
            pb_max = self._p["pullback_max_pct"]
        if not (pb_min <= pullback_pct <= pb_max):
            return None
        # 3분봉 재반등
        if up_ratio < self._p["min_three_min_up_ratio"]:
            return None
        if vol_ratio < self._p["min_vol_ratio"]:
            return None

        # 당일 dedup
        today = (now_dt or datetime.now()).strftime("%Y%m%d")
        if self._signaled_today.get(code) == today:
            return None

        # 진입 시각 기록
        self._entry_times[code] = now_dt or datetime.now()
        self._signaled_today[code] = today
        reason = (
            f"S7 눌림목 | 고점{today_high:,} 현재{price:,} "
            f"({pullback_pct:+.2%}) MA20>MA60 vol{vol_ratio:.1f}x up{up_ratio:.0%}"
        )
        logger.info("[S7] BUY: %s %s", code, reason)
        return StrategySignal(
            signal=Signal.BUY, code=code, name=name, price=price,
            reason=reason, strategy="S7",
        )

    def _check_exit(
        self,
        code: str,
        name: str,
        price: int,
        today_high: int,
        avg_price: float,
        qty: int,
        current_time: str,
        now_dt: Optional[datetime],
    ) -> Optional[StrategySignal]:
        pnl_rate = (price - avg_price) / avg_price if avg_price > 0 else 0.0

        # 트레일링 최고가 갱신
        prev_high = self._high_since_entry.get(code, int(avg_price))
        self._high_since_entry[code] = max(prev_high, price)
        high_since_entry = self._high_since_entry[code]

        # 1. 15:15 시장가 강제 청산
        if current_time >= self._p["forced_close"]:
            return self._sell(code, name, price, qty, "S7 마감 시장가", forced=True)
        # 2. 추세 깨짐 (today_high × 0.96 이탈)
        if today_high > 0 and price < today_high * (1 + self._p["stop_from_high"]):
            return self._sell(code, name, price, qty, f"S7 추세파괴 {pnl_rate:+.2%}")
        # 3. 하드 손절 -1.5%
        if pnl_rate <= self._p["stop_loss"]:
            return self._sell(code, name, price, qty, f"S7 손절 {pnl_rate:+.2%}")
        # 4. 트레일링: 1차 익절 이후 고점 대비 -2% 이탈
        if code in self._partial_sold and high_since_entry > 0:
            trail_pct = (price - high_since_entry) / high_since_entry
            if trail_pct <= -0.02:
                return self._sell(code, name, price, qty, f"S7 트레일 {pnl_rate:+.2%} (고점대비{trail_pct:.1%})")
        # 5. 2차 익절 +4%
        if pnl_rate >= self._p["take_profit_2"]:
            return self._sell(code, name, price, qty, f"S7 2차익절 {pnl_rate:+.2%}")
        # 6. 1차 익절 +1.5% 50%
        if pnl_rate >= self._p["take_profit_1"] and qty > 1 and code not in self._partial_sold:
            self._partial_sold.add(code)
            sell_qty = max(1, qty // 2)
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=sell_qty, reason=f"S7 1차익절 {pnl_rate:+.2%}",
                strategy="S7", sell_ratio=0.5,
            )
        # 7. 시간 손절 (60분 보유 + 수익 < 0.3%)
        entry_dt = self._entry_times.get(code)
        if entry_dt is not None and now_dt is not None:
            held_min = (now_dt - entry_dt).total_seconds() / 60
            if held_min >= self._p["time_stop_min"] and pnl_rate < self._p["weak_min_profit"]:
                return self._sell(code, name, price, qty,
                                  f"S7 시간손절 {held_min:.0f}분 {pnl_rate:+.2%}")
        return None

    def _sell(self, code, name, price, qty, reason, forced=False) -> StrategySignal:
        is_forced = forced or any(
            kw in reason for kw in ("손절", "추세파괴", "시간손절", "마감", "트레일")
        )
        logger.info("[S7] SELL: %s %s%s", code, reason, " (forced)" if is_forced else "")
        sig = StrategySignal(
            signal=Signal.SELL, code=code, name=name, price=price,
            quantity=qty, reason=reason, strategy="S7", sell_ratio=1.0,
            is_forced=is_forced,
        )
        self._entry_times.pop(code, None)
        self._high_since_entry.pop(code, None)
        self._partial_sold.discard(code)
        return sig

    def make_bt_func(
        self,
        ma20_default: float = 0.0,
        ma60_default: float = 0.0,
        vol_ratio_default: float = 1.5,
        up_ratio_default: float = 0.6,
    ):
        """Backtester.run()용 strategy_func."""
        def strategy_func(row, state):
            code = str(row.get("code", "TEST"))
            current_time = str(row.get("time", "110000")).zfill(6)
            sig = self.evaluate(
                code=code,
                name=row.get("name", code),
                current_price=int(row.get("close", 0) or 0),
                today_open=int(row.get("today_open", row.get("open", 0)) or 0),
                today_high=int(row.get("today_high", row.get("high", 0)) or 0),
                ma20=float(row.get("ma20", ma20_default) or 0),
                ma60=float(row.get("ma60", ma60_default) or 0),
                vol_ratio=float(row.get("vol_ratio", vol_ratio_default) or 0),
                three_min_up_ratio=float(row.get("three_min_up_ratio", up_ratio_default) or 0),
                current_time=current_time,
                position_qty=state.get("position_qty", 0),
                avg_entry_price=state.get("avg_price", 0.0),
                now_dt=state.get("now_dt"),
            )
            return sig.signal.name if sig else None
        return strategy_func
