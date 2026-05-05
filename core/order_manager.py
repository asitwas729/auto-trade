"""
주문 매니저
- 주문 생성/실행/취소
- 미체결 주문 감시 (타임아웃 자동 취소)
- 체결 확인
- 수수료/세금 계산
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Callable, Optional

from config.constants import (
    COMMISSION_RATE,
    TICK_SIZE_TABLE,
    TRANSACTION_TAX_RATE,
    UNFILLED_ORDER_TIMEOUT_SEC,
)
from core.kis_api_client import KISApiClient, OrderResult
from core.risk_manager import RiskManager, TradeRecord

logger = logging.getLogger(__name__)


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    PENDING = auto()    # 주문 전
    SUBMITTED = auto()  # 주문 접수
    FILLED = auto()     # 체결
    PARTIAL = auto()    # 부분체결
    CANCELLED = auto()  # 취소
    REJECTED = auto()   # 거부


@dataclass
class Order:
    strategy: str
    code: str
    name: str
    side: OrderSide
    quantity: int
    price: int            # 지정가 (0=시장가)
    amount: float         # 주문 금액 (수량×가격)
    order_no: str = ""
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: int = 0
    avg_filled_price: float = 0.0
    submitted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    cancel_reason: str = ""

    @property
    def is_market_order(self) -> bool:
        return self.price == 0

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL)

    def fee(self) -> float:
        return self.avg_filled_price * self.filled_qty * COMMISSION_RATE

    def tax(self) -> float:
        if self.side == OrderSide.SELL:
            return self.avg_filled_price * self.filled_qty * TRANSACTION_TAX_RATE
        return 0.0


class OrderManager:

    def __init__(
        self,
        api: KISApiClient,
        risk_manager: RiskManager,
        on_fill: Optional[Callable[[Order], None]] = None,
    ) -> None:
        self._api = api
        self._risk = risk_manager
        self._on_fill = on_fill        # 체결 콜백 (portfolio_manager 연동)
        self._lock = threading.RLock()

        # 활성 주문 {order_no: Order}
        self._active_orders: dict[str, Order] = {}

        # 타임아웃 감시 스레드
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True
        )
        self._watchdog_thread.start()

        logger.info("OrderManager 초기화")

    # ─────────────────────────────────────────────────────────────
    # 주문 생성
    # ─────────────────────────────────────────────────────────────

    def place_buy(
        self,
        strategy: str,
        code: str,
        name: str,
        quantity: int,
        price: int = 0,
    ) -> Optional[Order]:
        """매수 주문. 리스크 매니저 통과 후에만 실행."""
        amount = price * quantity if price > 0 else 0.0

        validation = self._risk.check_order(strategy, code, "BUY", amount)
        if not validation.allowed:
            logger.warning(
                f"매수 거부 [{validation.reason}] | {code} {quantity}주: {validation.message}"
            )
            return None

        order = Order(
            strategy=strategy,
            code=code,
            name=name,
            side=OrderSide.BUY,
            quantity=quantity,
            price=price,
            amount=amount,
        )
        return self._submit(order)

    def place_sell(
        self,
        strategy: str,
        code: str,
        name: str,
        quantity: int,
        price: int = 0,
        cancel_reason: str = "",
    ) -> Optional[Order]:
        """매도 주문 (손절/익절). 리스크 매니저 통과 후 실행."""
        amount = price * quantity if price > 0 else 0.0

        validation = self._risk.check_order(strategy, code, "SELL", amount)
        if not validation.allowed:
            logger.warning(
                f"매도 거부 [{validation.reason}] | {code}: {validation.message}"
            )
            return None

        order = Order(
            strategy=strategy,
            code=code,
            name=name,
            side=OrderSide.SELL,
            quantity=quantity,
            price=price,
            amount=amount,
            cancel_reason=cancel_reason,
        )
        return self._submit(order)

    def _submit(self, order: Order) -> Optional[Order]:
        if order.side == OrderSide.BUY:
            result: OrderResult = self._api.buy(
                order.code, order.quantity, order.price
            )
        else:
            result: OrderResult = self._api.sell(
                order.code, order.quantity, order.price
            )

        if not result.success:
            order.status = OrderStatus.REJECTED
            logger.error(f"주문 거부 | {order.code}: {result.message}")
            return None

        order.order_no = result.order_no
        order.status = OrderStatus.SUBMITTED
        order.submitted_at = datetime.now()

        with self._lock:
            self._active_orders[order.order_no] = order

        logger.info(
            f"{'매수' if order.side == OrderSide.BUY else '매도'} 주문 접수"
            f" | {order.name}({order.code}) {order.quantity}주"
            f" @{order.price:,}원 | 주문번호={order.order_no}"
        )
        return order

    # ─────────────────────────────────────────────────────────────
    # 체결 확인 (폴링)
    # ─────────────────────────────────────────────────────────────

    def sync_filled_orders(self) -> None:
        """KIS API로 체결 목록 조회 후 활성 주문 상태 갱신. 주기적 호출 필요."""
        if not self._active_orders:
            return

        try:
            filled_list = self._api.get_filled_orders()
        except Exception as exc:
            logger.error(f"체결 조회 실패: {exc}")
            return

        filled_map = {f.order_no: f for f in filled_list}

        with self._lock:
            for order_no, order in list(self._active_orders.items()):
                if order_no not in filled_map:
                    continue
                filled = filled_map[order_no]
                order.filled_qty = filled.filled_qty
                order.avg_filled_price = filled.avg_filled_price
                order.filled_at = filled.filled_at

                if filled.filled_qty >= order.quantity:
                    order.status = OrderStatus.FILLED
                    del self._active_orders[order_no]
                    logger.info(
                        f"체결 완료 | {order.name}({order.code})"
                        f" {order.filled_qty}주 @{order.avg_filled_price:,.0f}원"
                    )
                    if self._on_fill:
                        self._on_fill(order)
                elif filled.filled_qty > 0:
                    order.status = OrderStatus.PARTIAL

    # ─────────────────────────────────────────────────────────────
    # 미체결 타임아웃 감시
    # ─────────────────────────────────────────────────────────────

    def _watchdog_loop(self) -> None:
        while True:
            time.sleep(30)
            try:
                self._cancel_timed_out_orders()
            except Exception as exc:
                logger.error(f"watchdog 오류: {exc}")

    def _cancel_timed_out_orders(self) -> None:
        now = datetime.now()
        with self._lock:
            for order_no, order in list(self._active_orders.items()):
                if not order.is_active or order.submitted_at is None:
                    continue
                elapsed = (now - order.submitted_at).total_seconds()
                if elapsed > UNFILLED_ORDER_TIMEOUT_SEC:
                    self._cancel_order(order, reason="타임아웃")

    def _cancel_order(self, order: Order, reason: str = "") -> None:
        logger.info(f"주문 취소 시도 | {order.order_no} | 이유: {reason}")
        result = self._api.cancel_order(
            order.order_no, order.code, order.quantity, order.price
        )
        if result.success:
            order.status = OrderStatus.CANCELLED
            if order.order_no in self._active_orders:
                del self._active_orders[order.order_no]
            logger.info(f"주문 취소 완료 | {order.order_no}")
        else:
            # KIS 40330000 "취소할 수량 없음" = 이미 체결/취소된 주문.
            # active_orders에서 제거해 무한 재시도 루프 방지.
            err_msg = str(result.message)
            if "40330000" in err_msg or "취소할 수량" in err_msg:
                order.status = OrderStatus.CANCELLED
                if order.order_no in self._active_orders:
                    del self._active_orders[order.order_no]
                logger.info(
                    f"주문 {order.order_no} 이미 종결 처리됨 - active 목록에서 제거"
                )
            else:
                logger.error(f"주문 취소 실패 | {order.order_no}: {result.message}")

    def cancel_all(self) -> None:
        """모든 미체결 주문 취소 (kill switch 등)"""
        with self._lock:
            for order in list(self._active_orders.values()):
                self._cancel_order(order, reason="전체 취소")

    # ─────────────────────────────────────────────────────────────
    # 유틸
    # ─────────────────────────────────────────────────────────────

    def get_active_orders(self) -> list[Order]:
        with self._lock:
            return list(self._active_orders.values())


def calc_tick_size(price: int) -> int:
    """호가단위 계산"""
    for low, high, tick in TICK_SIZE_TABLE:
        if low <= price < high:
            return tick
    return 1_000


def round_to_tick(price: float) -> int:
    """가격을 호가단위에 맞게 반올림"""
    tick = calc_tick_size(int(price))
    return int(round(price / tick) * tick)


def calc_buy_amount(price: int, quantity: int) -> float:
    """매수 총 비용 (수수료 포함)"""
    return price * quantity * (1 + COMMISSION_RATE)


def calc_sell_proceeds(price: int, quantity: int) -> float:
    """매도 실수령액 (수수료 + 세금 차감)"""
    return price * quantity * (1 - COMMISSION_RATE - TRANSACTION_TAX_RATE)
