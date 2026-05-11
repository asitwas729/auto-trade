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
    FORCED_CLOSE_RETRY_DELAY_SEC,
    FORCED_CLOSE_RETRY_MAX,
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
    is_forced_close: bool = False    # 강제청산 시그널 (시장가 + 부분체결 재시도)
    retry_count: int = 0             # 부분체결 시장가 재발주 카운트

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
        is_forced_close: bool = False,
    ) -> Optional[Order]:
        """
        매도 주문 (손절/익절).
        is_forced_close=True: KIS API에 시장가 강제 + 부분체결 시 잔량 재발주.
        """
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
            is_forced_close=is_forced_close,
        )
        return self._submit(order)

    def _submit(self, order: Order) -> Optional[Order]:
        # 호가단위 보정 (side-aware): 매수는 ceil(올림)으로 더 높게,
        # 매도는 floor(내림)으로 더 낮게 → 마켓 쪽에 한 틱 가까이 두어
        # 체결률을 높임. 시장가(0)는 보정 안 함.
        if order.price > 0:
            if order.side == OrderSide.BUY:
                adjusted = ceil_to_tick(order.price)
            else:
                adjusted = floor_to_tick(order.price)
            if adjusted != order.price:
                logger.info(
                    f"[tick-fix] {order.code} {order.side.value} "
                    f"{order.price:,} → {adjusted:,}원 (호가단위 보정)"
                )
                order.price = adjusted
                order.amount = adjusted * order.quantity

        if order.side == OrderSide.BUY:
            result: OrderResult = self._api.buy(
                order.code, order.quantity, order.price
            )
        else:
            result: OrderResult = self._api.sell(
                order.code, order.quantity, order.price,
                is_forced_close=order.is_forced_close,
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
        """KIS API로 체결 목록 조회 후 활성 주문 상태 갱신. 주기적 호출 필요.
        강제청산(is_forced_close) 주문이 부분체결로 끝나면 잔량을 시장가로
        자동 재발주 (최대 FORCED_CLOSE_RETRY_MAX 회)."""
        if not self._active_orders:
            return

        try:
            filled_list = self._api.get_filled_orders()
        except Exception as exc:
            logger.error(f"체결 조회 실패: {exc}")
            return

        filled_map = {f.order_no: f for f in filled_list}
        retry_orders: list[Order] = []

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
                    # 강제청산 부분체결 → 잔량 시장가 재발주 큐잉
                    if (
                        order.is_forced_close
                        and order.side == OrderSide.SELL
                        and order.retry_count < FORCED_CLOSE_RETRY_MAX
                    ):
                        retry_orders.append(order)

        # 재발주는 락 밖에서 (HTTP 호출이라 시간 걸림)
        for order in retry_orders:
            self._retry_forced_close(order)

    def _retry_forced_close(self, order: Order) -> None:
        """강제청산 부분체결 → 잔량 시장가 재발주."""
        remaining = order.quantity - order.filled_qty
        if remaining <= 0:
            return
        with self._lock:
            # 기존 active 항목에서 제거 (재발주가 신규 order_no 받음)
            self._active_orders.pop(order.order_no, None)

        time.sleep(FORCED_CLOSE_RETRY_DELAY_SEC)
        new_order = Order(
            strategy=order.strategy,
            code=order.code,
            name=order.name,
            side=OrderSide.SELL,
            quantity=remaining,
            price=0,
            amount=0.0,
            is_forced_close=True,
            retry_count=order.retry_count + 1,
        )
        logger.warning(
            f"강제청산 부분체결 재시도 #{new_order.retry_count}"
            f" | {order.name}({order.code}) 잔량 {remaining}주 시장가"
        )
        result = self._submit(new_order)
        if result is None and new_order.retry_count >= FORCED_CLOSE_RETRY_MAX:
            logger.error(
                f"🔴 강제청산 실패 (재시도 {FORCED_CLOSE_RETRY_MAX}회 초과)"
                f" | {order.name}({order.code}) 잔량 {remaining}주"
            )

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

        # 타임아웃 후보가 하나라도 있으면 우선 체결 동기화 (락 밖에서)
        # — 이미 체결된 주문에 대한 헛 취소(KIS 40330000)를 줄임.
        with self._lock:
            has_candidate = any(
                o.is_active
                and o.submitted_at is not None
                and (now - o.submitted_at).total_seconds() > UNFILLED_ORDER_TIMEOUT_SEC
                for o in self._active_orders.values()
            )
        if has_candidate:
            try:
                self.sync_filled_orders()
            except Exception as exc:
                logger.warning(f"watchdog 사전 체결조회 실패: {exc}")

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

    def pending_sell_qty(self, code: str) -> int:
        """미체결 매도 주문의 남은 수량 합계 (중복 매도 시그널 차단용)."""
        with self._lock:
            return sum(
                max(0, o.quantity - o.filled_qty)
                for o in self._active_orders.values()
                if o.is_active and o.side == OrderSide.SELL and o.code == code
            )

    def pending_buy_qty(self, code: str) -> int:
        """미체결 매수 주문의 남은 수량 합계."""
        with self._lock:
            return sum(
                max(0, o.quantity - o.filled_qty)
                for o in self._active_orders.values()
                if o.is_active and o.side == OrderSide.BUY and o.code == code
            )


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


def ceil_to_tick(price: float) -> int:
    """매수용: 호가단위 올림 (한 틱 위 → 체결 우선)."""
    import math
    tick = calc_tick_size(int(price))
    return int(math.ceil(price / tick) * tick)


def floor_to_tick(price: float) -> int:
    """매도용: 호가단위 내림 (한 틱 아래 → 체결 우선)."""
    import math
    tick = calc_tick_size(int(price))
    return int(math.floor(price / tick) * tick)


def calc_buy_amount(price: int, quantity: int) -> float:
    """매수 총 비용 (수수료 포함)"""
    return price * quantity * (1 + COMMISSION_RATE)


def calc_sell_proceeds(price: int, quantity: int) -> float:
    """매도 실수령액 (수수료 + 세금 차감)"""
    return price * quantity * (1 - COMMISSION_RATE - TRANSACTION_TAX_RATE)
