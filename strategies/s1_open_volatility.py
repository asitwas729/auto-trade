"""
S1. 장초 변동성 단타
- 09:15~09:25 진입 (시초 5분 노이즈 회피 + 25분 이후 신규 차단)
- 당일 저점 형성 후 반등 + 거래량 증가 + 단기MA 회복 + 호가 매수세
- +1.5% 50% 매도, +3.0% 전량 매도
- 당일 저가 이탈 또는 -1.5% 손절
- 09:30 강제청산 (시초 변동성 끝나는 시점)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from config.constants import S1_PARAMS
from strategies.base_strategy import BaseStrategy, Signal, StrategySignal

logger = logging.getLogger(__name__)


class S1OpenVolatility(BaseStrategy):

    def __init__(self) -> None:
        super().__init__("S1")
        self._p = S1_PARAMS
        self._entry_lows: dict[str, int] = {}
        self._partial_sold: set[str] = set()  # 1차 익절 완료된 종목

    def evaluate(
        self,
        code: str,
        name: str,
        current_price: int,
        open_price: int,
        high_price: int,
        low_price: int,
        prev_close: int,
        ma5: float,
        ma20: float,
        volume: int,
        vol_ma20: float,
        vol_ratio: float,
        ask1: int,           # 매도1호가
        bid1: int,           # 매수1호가
        bid_qty_sum: int,    # 매수호가 수량 합
        ask_qty_sum: int,    # 매도호가 수량 합
        current_time: str,   # "HHmmss"
        position_qty: int = 0,
        avg_entry_price: float = 0.0,
    ) -> Optional[StrategySignal]:
        if not self.enabled:
            return None

        # ── 보유 중이면 EXIT 시그널 검사 (09:25 이후, 09:30 강제청산도 여기서 처리) ──
        if position_qty > 0 and avg_entry_price > 0:
            return self._check_exit(
                code, name, current_price, low_price,
                avg_entry_price, position_qty, current_time
            )

        # 포지션이 비어 있으면 상태 초기화
        self._entry_lows.pop(code, None)
        self._partial_sold.discard(code)

        # ── 진입 윈도우: 09:15 이후 ~ 09:25 이전만 신규 진입 ─────────
        if current_time < self._p["entry_after"]:
            return None
        if current_time >= self._p["entry_before"]:
            return None

        # ── 진입 조건 검사 ────────────────────────────────────────────
        return self._check_entry(
            code, name, current_price, open_price, high_price, low_price,
            prev_close, ma5, ma20, vol_ratio, bid_qty_sum, ask_qty_sum
        )

    def _check_entry(
        self,
        code, name, price, open_price, high, low,
        prev_close, ma5, ma20, vol_ratio, bid_qty_sum, ask_qty_sum
    ) -> Optional[StrategySignal]:
        reasons = []

        # 1. 당일 저점 회복: bounce > 0 필수 (price == low인 무의미 시그널 차단)
        if price <= low:
            return None
        bounce_pct = (price - low) / low
        reasons.append(f"저점반등{bounce_pct:.2%}")

        # 2. 거래량 증가 (1.0배 이상 필수)
        min_vol = self._p.get("min_vol_ratio", 1.0)
        if vol_ratio < min_vol:
            return None
        reasons.append(f"거래량{vol_ratio:.1f}x")

        # 3. 단기 이동평균 회복 (현재가 > MA5)
        if price < ma5:
            return None
        reasons.append("MA5위")

        # 4. 호가 매수세 우세 (매수호가 수량 > 매도호가 수량 × 1.2)
        # 모의 공격형 + 호가 미수신 시 자동 PASS
        if ask_qty_sum > 0 and bid_qty_sum < ask_qty_sum * 1.2:
            return None
        reasons.append("매수세우세")

        # 5. 전일 종가 대비 급락 아님 (모의 공격형: -15% 이내까지 허용)
        gap = (price - prev_close) / prev_close
        if gap < -0.15:
            return None

        # 진입 기록
        self._entry_lows[code] = low
        logger.info(f"[S1] {name}({code}) 진입 시그널 | {', '.join(reasons)}")

        return StrategySignal(
            signal=Signal.BUY,
            code=code,
            name=name,
            price=price,
            reason=", ".join(reasons),
            strategy="S1",
        )

    def _check_exit(
        self,
        code, name, price, low, avg_price, qty, current_time
    ) -> Optional[StrategySignal]:
        pnl_rate = (price - avg_price) / avg_price

        # 시간 강제청산
        if current_time >= self._p["forced_close"]:
            logger.info(f"[S1] {name}({code}) {self._p['forced_close'][:2]}:{self._p['forced_close'][2:4]} 강제청산 ({pnl_rate:.2%})")
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=qty, reason=f"09:30 강제청산({pnl_rate:.2%})",
                strategy="S1", sell_ratio=1.0, is_forced=True,
            )

        # 장 마감 전 강제 청산 (15:20 이후, 안전망)
        if current_time >= "152000":
            logger.info(f"[S1] {name}({code}) 장 마감 전 전량 청산")
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=qty, reason="장 마감 청산", strategy="S1",
                sell_ratio=1.0, is_forced=True,
            )

        # 익절 2: +2% → 전량 매도 (target_1보다 먼저 검사)
        if pnl_rate >= self._p["profit_target_2"]:
            logger.info(f"[S1] {name}({code}) 2차 익절 {pnl_rate:.2%}")
            self._partial_sold.discard(code)
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=qty, reason=f"2차익절{pnl_rate:.2%}",
                strategy="S1", sell_ratio=1.0
            )

        # 익절 1: +1% → 50% 매도 (최초 1회만)
        if pnl_rate >= self._p["profit_target_1"] and code not in self._partial_sold:
            sell_qty = max(1, qty // 2)
            self._partial_sold.add(code)
            logger.info(f"[S1] {name}({code}) 1차 익절 {pnl_rate:.2%}")
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=sell_qty, reason=f"1차익절{pnl_rate:.2%}",
                strategy="S1", sell_ratio=0.5
            )

        # 손절 1: 당일 저가 이탈
        entry_low = self._entry_lows.get(code, low)
        if price < entry_low:
            logger.info(f"[S1] {name}({code}) 저가 이탈 손절")
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=qty, reason="저가이탈손절", strategy="S1",
                sell_ratio=1.0, is_forced=True,
            )

        # 손절 2: -1.5%
        if pnl_rate <= self._p["stop_loss_rate"]:
            logger.info(f"[S1] {name}({code}) 손절 {pnl_rate:.2%}")
            return StrategySignal(
                signal=Signal.SELL, code=code, name=name, price=price,
                quantity=qty, reason=f"손절{pnl_rate:.2%}",
                strategy="S1", sell_ratio=1.0, is_forced=True,
            )

        return None
