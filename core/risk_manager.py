"""
리스크 매니저
- 일일/주간/연속 손실 제한
- 종목별 비중 제한
- 주문 가능 여부 판단
- kill switch
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Optional

from config.constants import (
    CONSECUTIVE_LOSS_LIMIT,
    DAILY_LOSS_LIMIT_RATE,
    MAX_ORDER_AMOUNT,
    MAX_POSITION_RATIO,
    MIN_CASH_RATIO,
    UNFILLED_ORDER_TIMEOUT_SEC,
    WEEKLY_LOSS_LIMIT_RATE,
    WEEKLY_PAUSE_DAYS,
)
from config.settings import settings

logger = logging.getLogger(__name__)


class RejectionReason(Enum):
    KILL_SWITCH = auto()
    API_ERROR = auto()
    DAILY_LOSS_LIMIT = auto()
    WEEKLY_LOSS_LIMIT = auto()
    CONSECUTIVE_LOSS = auto()
    POSITION_LIMIT = auto()
    ORDER_AMOUNT_LIMIT = auto()
    CASH_RATIO_LIMIT = auto()
    MARKET_RISK = auto()
    STALE_PRICE = auto()
    BALANCE_MISMATCH = auto()
    STRATEGY_DISABLED = auto()


@dataclass
class OrderValidation:
    allowed: bool
    reason: Optional[RejectionReason] = None
    message: str = ""


@dataclass
class TradeRecord:
    strategy: str
    code: str
    side: str          # "BUY" | "SELL"
    pnl: float         # 실현손익 (원)
    pnl_rate: float    # 손익률 (%)
    traded_at: datetime = field(default_factory=datetime.now)

    @property
    def is_loss(self) -> bool:
        return self.pnl < 0


class RiskManager:
    """
    모든 주문은 반드시 이 클래스를 통해 허가받아야 함.
    risk_manager.check_order(...) → False이면 주문 불가.
    """

    def __init__(self, initial_capital: float) -> None:
        self._capital = initial_capital        # 초기 자본금
        self._total_eval = initial_capital     # 현재 총 평가금액 (외부에서 갱신)
        self._cash = initial_capital
        self._lock = threading.Lock()

        # 손실 추적
        self._daily_start_eval: dict[date, float] = {}      # 날짜별 시작 평가금액
        self._weekly_start_eval: dict[int, float] = {}       # ISO week별 시작 평가금액
        self._weekly_pause_until: Optional[date] = None      # 주간 손실 후 매매 중단 기한

        # 연속 손실 (전략별)
        self._consecutive_losses: dict[str, int] = defaultdict(int)
        self._strategy_disabled: set[str] = set()

        # 포지션 (코드 → 금액)
        self._positions: dict[str, float] = {}

        # kill switch
        self._kill_switch_file = settings.KILL_SWITCH_FILE
        self._manual_kill = False

        # API/시세 상태 (외부에서 주입)
        self._api_healthy = True
        self._price_stale = False
        self._balance_mismatch = False

        # 시장 위험도 (S4에서 주입)
        self._market_risky = False

        logger.info(f"RiskManager 초기화 | 초기자본={initial_capital:,.0f}원")

    # ─────────────────────────────────────────────────────────────
    # 상태 업데이트 (외부 → RiskManager)
    # ─────────────────────────────────────────────────────────────

    def update_portfolio_state(
        self,
        total_eval: float,
        cash: float,
        positions: dict[str, float],
    ) -> None:
        """포트폴리오 매니저로부터 상태 동기화"""
        with self._lock:
            today = date.today()
            if today not in self._daily_start_eval:
                self._daily_start_eval[today] = total_eval
                logger.info(f"오늘 시작 평가금액 기록: {total_eval:,.0f}원")

            iso_week = today.isocalendar()[1]
            if iso_week not in self._weekly_start_eval:
                self._weekly_start_eval[iso_week] = total_eval
                logger.info(f"이번 주 시작 평가금액 기록: {total_eval:,.0f}원")

            self._total_eval = total_eval
            self._cash = cash
            self._positions = positions

    def set_api_health(self, healthy: bool) -> None:
        self._api_healthy = healthy

    def set_price_stale(self, stale: bool) -> None:
        self._price_stale = stale

    def set_balance_mismatch(self, mismatch: bool) -> None:
        self._balance_mismatch = mismatch

    def set_market_risky(self, risky: bool) -> None:
        with self._lock:
            if risky != self._market_risky:
                self._market_risky = risky
                logger.warning(f"시장 위험도 변경: {'위험' if risky else '정상'}")

    # ─────────────────────────────────────────────────────────────
    # 거래 기록 → 연속 손실 추적
    # ─────────────────────────────────────────────────────────────

    def record_trade(self, record: TradeRecord) -> None:
        with self._lock:
            strategy = record.strategy
            if record.is_loss:
                self._consecutive_losses[strategy] += 1
                cnt = self._consecutive_losses[strategy]
                logger.warning(
                    f"[{strategy}] 연속 손실 {cnt}회 | {record.code} {record.pnl_rate:.2f}%"
                )
                if cnt >= CONSECUTIVE_LOSS_LIMIT:
                    self._strategy_disabled.add(strategy)
                    logger.error(
                        f"[{strategy}] 연속 손실 {cnt}회 도달 → 전략 OFF"
                    )
            else:
                # 수익 발생 시 연속 손실 리셋
                self._consecutive_losses[strategy] = 0
                self._strategy_disabled.discard(strategy)
                logger.info(
                    f"[{strategy}] 수익 발생 → 연속 손실 리셋 | {record.pnl_rate:.2f}%"
                )

    def re_enable_strategy(self, strategy: str) -> None:
        """수동으로 전략 재활성화 (관리자 조작)"""
        with self._lock:
            self._strategy_disabled.discard(strategy)
            self._consecutive_losses[strategy] = 0
            logger.info(f"[{strategy}] 전략 수동 재활성화")

    # ─────────────────────────────────────────────────────────────
    # 주문 허가 검사 (핵심)
    # ─────────────────────────────────────────────────────────────

    def check_order(
        self,
        strategy: str,
        code: str,
        side: str,           # "BUY" | "SELL"
        amount: float,       # 주문 금액 (원)
    ) -> OrderValidation:
        """
        주문 전 필수 호출.
        side=="SELL"이면 손절/익절 매도이므로 일부 제한 면제.
        """
        with self._lock:
            # kill switch (매수/매도 모두 차단)
            if self._is_kill_switch_active():
                return OrderValidation(
                    False, RejectionReason.KILL_SWITCH, "Kill switch 활성화"
                )

            # API / 시세 / 잔고 상태
            if not self._api_healthy:
                return OrderValidation(
                    False, RejectionReason.API_ERROR, "API 오류 상태"
                )
            if self._price_stale:
                return OrderValidation(
                    False, RejectionReason.STALE_PRICE, "시세 지연 감지"
                )
            if self._balance_mismatch:
                return OrderValidation(
                    False, RejectionReason.BALANCE_MISMATCH, "잔고/체결 불일치"
                )

            # 매도는 손절/익절이므로 손실 한도 초과 시에도 허용
            if side == "BUY":
                # 일일 손실 한도
                v = self._daily_loss_rate()
                if v is not None and v <= DAILY_LOSS_LIMIT_RATE:
                    return OrderValidation(
                        False,
                        RejectionReason.DAILY_LOSS_LIMIT,
                        f"일일 손실 한도 도달: {v:.2%}",
                    )

                # 주간 손실 한도
                if self._is_weekly_paused():
                    return OrderValidation(
                        False,
                        RejectionReason.WEEKLY_LOSS_LIMIT,
                        f"주간 손실 한도 후 매매 중단 중 (until {self._weekly_pause_until})",
                    )
                w = self._weekly_loss_rate()
                if w is not None and w <= WEEKLY_LOSS_LIMIT_RATE:
                    self._set_weekly_pause()
                    return OrderValidation(
                        False,
                        RejectionReason.WEEKLY_LOSS_LIMIT,
                        f"주간 손실 한도 도달: {w:.2%}",
                    )

                # 전략 비활성화
                if strategy in self._strategy_disabled:
                    return OrderValidation(
                        False,
                        RejectionReason.STRATEGY_DISABLED,
                        f"[{strategy}] 연속 손실로 비활성화",
                    )

                # 시장 위험
                if self._market_risky and strategy in ("S1", "S2"):
                    return OrderValidation(
                        False,
                        RejectionReason.MARKET_RISK,
                        "시장 위험도 높음 - S1/S2 진입 금지",
                    )

                # 현금 비중
                cash_ratio = self._cash / self._total_eval if self._total_eval > 0 else 0
                if cash_ratio - (amount / self._total_eval) < MIN_CASH_RATIO:
                    return OrderValidation(
                        False,
                        RejectionReason.CASH_RATIO_LIMIT,
                        f"현금 비중 부족: {cash_ratio:.2%}",
                    )

                # 1회 주문 최대 금액
                if amount > MAX_ORDER_AMOUNT:
                    return OrderValidation(
                        False,
                        RejectionReason.ORDER_AMOUNT_LIMIT,
                        f"1회 주문 한도 초과: {amount:,.0f}원 > {MAX_ORDER_AMOUNT:,.0f}원",
                    )

                # 종목 비중
                current_pos = self._positions.get(code, 0.0)
                new_ratio = (current_pos + amount) / self._total_eval
                if new_ratio > MAX_POSITION_RATIO:
                    return OrderValidation(
                        False,
                        RejectionReason.POSITION_LIMIT,
                        f"종목 비중 초과: {new_ratio:.2%} > {MAX_POSITION_RATIO:.2%}",
                    )

            return OrderValidation(True)

    # ─────────────────────────────────────────────────────────────
    # kill switch
    # ─────────────────────────────────────────────────────────────

    def activate_kill_switch(self) -> None:
        """수동 kill switch 활성화"""
        self._manual_kill = True
        self._kill_switch_file.touch()
        logger.critical("Kill switch 수동 활성화 - 모든 주문 차단")

    def deactivate_kill_switch(self) -> None:
        self._manual_kill = False
        if self._kill_switch_file.exists():
            self._kill_switch_file.unlink()
        logger.info("Kill switch 해제")

    def _is_kill_switch_active(self) -> bool:
        return self._manual_kill or self._kill_switch_file.exists()

    # ─────────────────────────────────────────────────────────────
    # 손실률 계산
    # ─────────────────────────────────────────────────────────────

    def _daily_loss_rate(self) -> Optional[float]:
        today = date.today()
        start = self._daily_start_eval.get(today)
        if not start or start == 0:
            return None
        return (self._total_eval - start) / start

    def _weekly_loss_rate(self) -> Optional[float]:
        iso_week = date.today().isocalendar()[1]
        start = self._weekly_start_eval.get(iso_week)
        if not start or start == 0:
            return None
        return (self._total_eval - start) / start

    def _set_weekly_pause(self) -> None:
        pause_until = date.today() + timedelta(days=WEEKLY_PAUSE_DAYS)
        self._weekly_pause_until = pause_until
        logger.error(
            f"주간 손실 한도 도달 → {WEEKLY_PAUSE_DAYS}거래일 매매 중단 (until {pause_until})"
        )

    def _is_weekly_paused(self) -> bool:
        if self._weekly_pause_until is None:
            return False
        return date.today() <= self._weekly_pause_until

    # ─────────────────────────────────────────────────────────────
    # 상태 조회
    # ─────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        with self._lock:
            return {
                "kill_switch": self._is_kill_switch_active(),
                "api_healthy": self._api_healthy,
                "price_stale": self._price_stale,
                "balance_mismatch": self._balance_mismatch,
                "market_risky": self._market_risky,
                "daily_loss_rate": self._daily_loss_rate(),
                "weekly_loss_rate": self._weekly_loss_rate(),
                "weekly_paused_until": str(self._weekly_pause_until),
                "strategy_disabled": list(self._strategy_disabled),
                "consecutive_losses": dict(self._consecutive_losses),
                "total_eval": self._total_eval,
                "cash": self._cash,
            }
