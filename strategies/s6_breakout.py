"""
S6. 돌파매매 (09:30 ~ 11:00)

- 전일고 또는 09:30 직전 intraday high 돌파 시 진입
- 거래량 1.5배 이상 확인 + price > MA20 + 3분봉 우상향
- 익절: +1.5% 50%, +3.5% 전량
- 손절: opening_range_high × 0.985 이탈 (돌파 실패 재테스트), -2% 하드
- 트레일: 진입 후 high × 0.98 이탈
- 15:15 시장가 강제 청산
"""
from __future__ import annotations

import logging
from typing import Optional

from config.constants import S6_PARAMS
from strategies.base_strategy import BaseStrategy, Signal, StrategySignal

logger = logging.getLogger(__name__)


class S6Breakout(BaseStrategy):

    def __init__(self) -> None:
        super().__init__("S6")
        self._p = S6_PARAMS
        # 진입 후 최고가 추적 (트레일링용)
        self._entry_highs: dict[str, int] = {}
        # 당일 진입 시그널 dedup ({code: yyyymmdd}). 동일 종목 매 tick
        # 같은 시그널 반복 발사를 차단 — main.py 한도와 중복 안전망.
        self._signaled_today: dict[str, str] = {}

    def evaluate(
        self,
        code: str,
        name: str,
        current_price: int,
        prev_day_high: int,
        opening_range_high: int,    # 09:30 시점 today_high 스냅샷
        ma20: float,
        vol_ratio: float,
        three_min_up_ratio: float,
        current_time: str,
        position_qty: int = 0,
        avg_entry_price: float = 0.0,
    ) -> Optional[StrategySignal]:
        if not self.enabled or current_price <= 0:
            return None

        # ── 보유 중이면 청산 검사 ────────────────────────────────────
        if position_qty > 0 and avg_entry_price > 0:
            return self._check_exit(
                code, name, current_price, opening_range_high,
                avg_entry_price, position_qty, current_time,
            )

        # ── 진입 검사 ────────────────────────────────────────────────
        if not (self._p["entry_start"] <= current_time < self._p["entry_end"]):
            return None
        return self._check_entry(
            code, name, current_price, prev_day_high, opening_range_high,
            ma20, vol_ratio, three_min_up_ratio,
        )

    def _check_entry(
        self,
        code: str,
        name: str,
        price: int,
        prev_day_high: int,
        opening_range_high: int,
        ma20: float,
        vol_ratio: float,
        up_ratio: float,
    ) -> Optional[StrategySignal]:
        # 당일 이미 진입 시그널 발사 → 재발사 차단 (dedup)
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y%m%d")
        if self._signaled_today.get(code) == today:
            return None

        breakout_level = max(prev_day_high or 0, opening_range_high or 0)
        if breakout_level <= 0 or price <= breakout_level:
            return None
        if vol_ratio < self._p["min_vol_ratio"]:
            return None
        if ma20 <= 0 or price < ma20:
            return None
        if up_ratio < self._p["min_three_min_up_ratio"]:
            return None

        # 돌파 품질 점수: 돌파율 × 거래량배율 ≥ min_breakout_quality
        breakout_pct = (price / breakout_level - 1)
        quality_score = breakout_pct * vol_ratio
        min_quality = self._p.get("min_breakout_quality", 0.020)
        if quality_score < min_quality:
            return None

        # 진입 high 초기화 + dedup 마킹
        self._entry_highs[code] = price
        self._signaled_today[code] = today
        reason = (
            f"S6 돌파 | level={breakout_level:,} 현재{price:,} "
            f"(+{(price/breakout_level-1)*100:.2f}%) "
            f"vol{vol_ratio:.1f}x MA20위 up{up_ratio:.0%}"
        )
        logger.info("[S6] BUY: %s %s", code, reason)
        return StrategySignal(
            signal=Signal.BUY, code=code, name=name, price=price,
            reason=reason, strategy="S6",
        )

    def _check_exit(
        self,
        code: str,
        name: str,
        price: int,
        opening_range_high: int,
        avg_price: float,
        qty: int,
        current_time: str,
    ) -> Optional[StrategySignal]:
        # 진입 후 최고가 갱신
        prev_high = self._entry_highs.get(code, int(avg_price))
        self._entry_highs[code] = max(prev_high, price)
        high_since_entry = self._entry_highs[code]

        pnl_rate = (price - avg_price) / avg_price if avg_price > 0 else 0.0

        # 1. 15:15 시장가 강제 청산
        if current_time >= self._p["forced_close"]:
            return self._sell(code, name, price, qty, "S6 마감 시장가", forced=True)
        # 2. 돌파 실패 재테스트 (opening_range_high × 0.985 이탈)
        if opening_range_high > 0 and price < opening_range_high * self._p["stop_below_or_high_pct"]:
            return self._sell(code, name, price, qty, f"S6 돌파실패 재테스트 {pnl_rate:+.2%}")
        # 3. 트레일 (high × 0.98)
        if high_since_entry > 0 and price < high_since_entry * (1 - self._p["trail_pullback"]):
            return self._sell(code, name, price, qty, f"S6 트레일 {pnl_rate:+.2%}")
        # 4. 하드 손절 -2%
        if pnl_rate <= self._p["stop_loss"]:
            return self._sell(code, name, price, qty, f"S6 손절 {pnl_rate:+.2%}")
        # 5. 2차 익절 +3.5% 전량
        if pnl_rate >= self._p["take_profit_2"]:
            return self._sell(code, name, price, qty, f"S6 2차익절 {pnl_rate:+.2%}")
        # 6. 1차 익절 +1.5% 50%
        if pnl_rate >= self._p["take_profit_1"] and qty > 1:
            sell_qty = max(1, qty // 2)
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=sell_qty, reason=f"S6 1차익절 {pnl_rate:+.2%}",
                strategy="S6", sell_ratio=0.5,
            )
        return None

    def _sell(self, code, name, price, qty, reason, forced=False) -> StrategySignal:
        # 손절/트레일/돌파실패는 즉시 체결 필요 → forced=True (시장가).
        # 1차/2차 익절만 지정가 유지 (가격 욕심).
        is_forced = forced or any(
            kw in reason for kw in ("손절", "트레일", "돌파실패", "재테스트", "마감")
        )
        logger.info("[S6] SELL: %s %s%s", code, reason, " (forced)" if is_forced else "")
        sig = StrategySignal(
            signal=Signal.SELL, code=code, name=name, price=price,
            quantity=qty, reason=reason, strategy="S6", sell_ratio=1.0,
            is_forced=is_forced,
        )
        # 진입 high 캐시 정리
        self._entry_highs.pop(code, None)
        return sig

    # ─── 백테스트 헬퍼 (Step 0) ───────────────────────────────────
    def make_bt_func(
        self,
        prev_day_high_default: int = 0,
        ma20_default: float = 0.0,
        vol_ratio_default: float = 2.0,
        up_ratio_default: float = 0.6,
    ):
        """
        Backtester.run()용 strategy_func 생성.
        row dict에 prev_day_high/ma20/vol_ratio/up_ratio가 없으면 default 사용.
        """
        def strategy_func(row, state):
            code = str(row.get("code", "TEST"))
            current_time = str(row.get("time", "100000")).zfill(6)
            price = int(row.get("close", 0) or 0)
            opening_range_high = int(row.get("opening_range_high", row.get("high", 0)) or 0)
            sig = self.evaluate(
                code=code,
                name=row.get("name", code),
                current_price=price,
                prev_day_high=int(row.get("prev_day_high", prev_day_high_default) or 0),
                opening_range_high=opening_range_high,
                ma20=float(row.get("ma20", ma20_default) or 0),
                vol_ratio=float(row.get("vol_ratio", vol_ratio_default) or 0),
                three_min_up_ratio=float(row.get("three_min_up_ratio", up_ratio_default) or 0),
                current_time=current_time,
                position_qty=state.get("position_qty", 0),
                avg_entry_price=state.get("avg_price", 0.0),
            )
            return sig.signal.name if sig else None
        return strategy_func
