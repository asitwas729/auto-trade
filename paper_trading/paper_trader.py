"""
페이퍼 트레이딩
- 실시간 시세 기반 가상 주문/체결 시뮬레이션
- 실제 KIS API 주문 없이 동작
- 포트폴리오/리스크 매니저는 실전과 동일하게 동작
- 최소 2~4주 검증 후 실거래 전환
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config.constants import COMMISSION_RATE, TRANSACTION_TAX_RATE
from core.order_manager import round_to_tick
from core.portfolio_manager import PortfolioManager
from core.risk_manager import RiskManager, TradeRecord
from strategies.base_strategy import Signal, StrategySignal

logger = logging.getLogger(__name__)


@dataclass
class PaperOrder:
    strategy: str
    code: str
    name: str
    side: str       # "BUY" | "SELL"
    quantity: int
    order_price: int
    submitted_at: datetime = field(default_factory=datetime.now)
    filled: bool = False
    filled_price: int = 0
    filled_at: Optional[datetime] = None


class PaperTrader:
    """
    실시간 가격이 들어올 때 on_price()를 호출하면
    내부적으로 가상 체결을 처리한다.
    """

    def __init__(
        self,
        portfolio: PortfolioManager,
        risk_manager: RiskManager,
        initial_capital: float,
    ) -> None:
        self._portfolio = portfolio
        self._risk = risk_manager
        self._lock = threading.Lock()
        self._pending_orders: list[PaperOrder] = []
        self._filled_orders: list[PaperOrder] = []

        logger.info(f"PaperTrader 초기화 | 초기자본={initial_capital:,.0f}원")

    def place_order(self, signal: StrategySignal, quantity: int) -> Optional[PaperOrder]:
        """전략 시그널 → 가상 주문 생성"""
        side = "BUY" if signal.signal == Signal.BUY else "SELL"
        amount = signal.price * quantity

        validation = self._risk.check_order(signal.strategy, signal.code, side, amount)
        if not validation.allowed:
            logger.warning(f"[Paper] 주문 거부: {validation.message}")
            return None

        order = PaperOrder(
            strategy=signal.strategy,
            code=signal.code,
            name=signal.name,
            side=side,
            quantity=quantity,
            order_price=signal.price,
        )
        with self._lock:
            self._pending_orders.append(order)

        logger.info(
            f"[Paper] {'매수' if side == 'BUY' else '매도'} 주문 | "
            f"{signal.name}({signal.code}) {quantity}주 @{signal.price:,}원"
        )
        return order

    def on_price(self, code: str, current_price: int) -> None:
        """실시간 가격 수신 → 미체결 주문 체결 처리"""
        with self._lock:
            remaining = []
            for order in self._pending_orders:
                if order.code != code:
                    remaining.append(order)
                    continue
                # 지정가 체결 조건: 매수 ≤ 현재가, 매도 ≥ 현재가
                if order.side == "BUY" and current_price <= order.order_price:
                    self._fill(order, current_price)
                elif order.side == "SELL" and current_price >= order.order_price:
                    self._fill(order, current_price)
                else:
                    remaining.append(order)
            self._pending_orders = remaining

    def _fill(self, order: PaperOrder, price: int) -> None:
        order.filled = True
        order.filled_price = price
        order.filled_at = datetime.now()
        self._filled_orders.append(order)

        fee = price * order.quantity * COMMISSION_RATE
        tax = price * order.quantity * TRANSACTION_TAX_RATE if order.side == "SELL" else 0.0

        if order.side == "BUY":
            self._portfolio.on_buy_filled(
                code=order.code,
                name=order.name,
                strategy=order.strategy,
                quantity=order.quantity,
                filled_price=float(price),
                fee=fee,
            )
            logger.info(
                f"[Paper] 매수 체결 | {order.name}({order.code}) "
                f"{order.quantity}주 @{price:,}원 | 수수료={fee:,.0f}원"
            )
        else:
            pnl = self._portfolio.on_sell_filled(
                code=order.code,
                name=order.name,
                strategy=order.strategy,
                quantity=order.quantity,
                filled_price=float(price),
                fee=fee,
                tax=tax,
            )
            pos = self._portfolio.positions.get(order.code)
            avg_buy = pos.avg_price if pos else 0.0
            pnl_rate = pnl / (avg_buy * order.quantity) if avg_buy > 0 else 0.0

            self._risk.record_trade(TradeRecord(
                strategy=order.strategy,
                code=order.code,
                side=order.side,
                pnl=pnl,
                pnl_rate=pnl_rate,
            ))
            logger.info(
                f"[Paper] 매도 체결 | {order.name}({order.code}) "
                f"{order.quantity}주 @{price:,}원 | 손익={pnl:+,.0f}원 ({pnl_rate:+.2%})"
            )

        # 리스크 매니저 동기화
        self._risk.update_portfolio_state(
            total_eval=self._portfolio.total_eval,
            cash=self._portfolio.cash,
            positions=self._portfolio.position_amounts,
        )

    def get_filled_orders(self) -> list[PaperOrder]:
        with self._lock:
            return list(self._filled_orders)

    def get_summary(self) -> dict:
        return self._portfolio.get_summary()
