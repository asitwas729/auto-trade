"""
포트폴리오 매니저
- 현금/보유종목/전략별 비중 관리
- 평가손익 / 실현손익 추적
- 월간 동적 리밸런싱 (최근 20 트레이드 성과 기반)
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from config.constants import (
    DEFAULT_STRATEGY_WEIGHTS,
    MIN_CASH_RATIO,
    REBALANCE_LOOKBACK_TRADES,
    WEIGHT_ADJUST_STEP,
)

logger = logging.getLogger(__name__)


@dataclass
class Position:
    code: str
    name: str
    strategy: str          # 어느 전략이 진입했는지
    quantity: int
    avg_price: float       # 평균 매수가
    current_price: int = 0
    high_price_since_entry: int = 0   # 진입 후 최고가 (트레일링 스탑용)

    @property
    def eval_amount(self) -> float:
        return self.current_price * self.quantity

    @property
    def cost_amount(self) -> float:
        return self.avg_price * self.quantity

    @property
    def unrealized_pnl(self) -> float:
        return self.eval_amount - self.cost_amount

    @property
    def unrealized_pnl_rate(self) -> float:
        if self.cost_amount == 0:
            return 0.0
        return (self.eval_amount - self.cost_amount) / self.cost_amount


@dataclass
class TradeHistory:
    code: str
    name: str
    strategy: str
    side: str              # "BUY" | "SELL"
    quantity: int
    price: float
    amount: float
    fee: float
    tax: float
    realized_pnl: float = 0.0
    realized_pnl_rate: float = 0.0
    traded_at: datetime = field(default_factory=datetime.now)


@dataclass
class StrategyPerformance:
    strategy: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_drawdown: float = 0.0
    weight: float = 0.0

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.wins / self.total_trades

    @property
    def expectancy(self) -> float:
        """기대값: 승률×평균수익 - 패율×평균손실"""
        loss_rate = 1 - self.win_rate
        return self.win_rate * self.avg_win - loss_rate * abs(self.avg_loss)


class PortfolioManager:

    def __init__(self, initial_capital: float) -> None:
        self._cash = initial_capital
        self._initial_capital = initial_capital
        self._lock = threading.RLock()

        # 보유 포지션 {code: Position}
        self._positions: dict[str, Position] = {}

    def sync_from_balance(
        self,
        cash: float,
        positions: list,
        default_strategy: str = "EXTERNAL",
    ) -> None:
        """KIS 잔고 조회 결과를 PortfolioManager 상태로 반영.
        재시작 후 기존 포지션을 이어받아 관리하기 위함.

        Args:
            cash: KIS 가용 현금 (dnca_tot_amt 등)
            positions: KIS BalanceItem 목록 (code/name/quantity/avg_price/current_price 보유)
            default_strategy: 기존 포지션의 전략 정보가 없을 때 표기. EXTERNAL=수동/이전세션
        """
        with self._lock:
            self._cash = float(cash)
            self._positions = {}
            for item in positions:
                self._positions[item.code] = Position(
                    code=item.code,
                    name=item.name or item.code,
                    strategy=default_strategy,
                    quantity=int(item.quantity),
                    avg_price=float(item.avg_price),
                    current_price=int(item.current_price),
                    high_price_since_entry=int(item.current_price),
                )
        logger.info(
            "[Portfolio] KIS 잔고 동기화 | 현금=%.0f원, 포지션=%d개",
            cash, len(positions),
        )

        # 체결 이력
        self._trade_history: list[TradeHistory] = []

        # 전략별 최근 트레이드 (성과 측정)
        self._strategy_trades: dict[str, deque[TradeHistory]] = defaultdict(
            lambda: deque(maxlen=REBALANCE_LOOKBACK_TRADES)
        )

        # 전략 비중 (S1~S4 + CASH)
        self._weights: dict[str, float] = dict(DEFAULT_STRATEGY_WEIGHTS)

        # 전략별 성과 (S1~S5 + cash)
        self._perf: dict[str, StrategyPerformance] = {
            s: StrategyPerformance(strategy=s, weight=w)
            for s, w in DEFAULT_STRATEGY_WEIGHTS.items()
        }
        # S5가 DEFAULT_STRATEGY_WEIGHTS에 없으면 추가
        if "S5" not in self._perf:
            self._perf["S5"] = StrategyPerformance(strategy="S5", weight=0.0)

        # 실현손익 누적
        self._total_realized_pnl: float = 0.0

        # 리밸런싱 추적
        self._last_rebalance_month: Optional[int] = None

        logger.info(f"PortfolioManager 초기화 | 초기자본={initial_capital:,.0f}원")

    # ─────────────────────────────────────────────────────────────
    # 가격 업데이트 (실시간 WebSocket → 호출)
    # ─────────────────────────────────────────────────────────────

    def update_price(self, code: str, price: int) -> None:
        with self._lock:
            if code in self._positions:
                pos = self._positions[code]
                pos.current_price = price
                if price > pos.high_price_since_entry:
                    pos.high_price_since_entry = price

    # ─────────────────────────────────────────────────────────────
    # 매수 처리
    # ─────────────────────────────────────────────────────────────

    def on_buy_filled(
        self,
        code: str,
        name: str,
        strategy: str,
        quantity: int,
        filled_price: float,
        fee: float,
    ) -> None:
        with self._lock:
            total_cost = filled_price * quantity + fee
            self._cash -= total_cost

            if code in self._positions:
                pos = self._positions[code]
                # 평균 단가 갱신
                total_qty = pos.quantity + quantity
                pos.avg_price = (
                    (pos.avg_price * pos.quantity + filled_price * quantity) / total_qty
                )
                pos.quantity = total_qty
            else:
                self._positions[code] = Position(
                    code=code,
                    name=name,
                    strategy=strategy,
                    quantity=quantity,
                    avg_price=filled_price,
                    current_price=int(filled_price),
                    high_price_since_entry=int(filled_price),
                )

            trade = TradeHistory(
                code=code,
                name=name,
                strategy=strategy,
                side="BUY",
                quantity=quantity,
                price=filled_price,
                amount=filled_price * quantity,
                fee=fee,
                tax=0.0,
            )
            self._trade_history.append(trade)
            self._strategy_trades[strategy].append(trade)

            logger.info(
                f"매수 체결 반영 | {name}({code}) {quantity}주 @{filled_price:,.0f}원"
                f" | 현금={self._cash:,.0f}원"
            )

    # ─────────────────────────────────────────────────────────────
    # 매도 처리
    # ─────────────────────────────────────────────────────────────

    def on_sell_filled(
        self,
        code: str,
        name: str,
        strategy: str,
        quantity: int,
        filled_price: float,
        fee: float,
        tax: float,
    ) -> float:
        """실현손익 반환"""
        with self._lock:
            if code not in self._positions:
                logger.warning(f"매도 체결 처리 중 포지션 없음: {code}")
                return 0.0

            pos = self._positions[code]
            avg_buy = pos.avg_price
            realized_pnl = (filled_price - avg_buy) * quantity - fee - tax
            realized_rate = realized_pnl / (avg_buy * quantity) if avg_buy > 0 else 0.0

            proceed = filled_price * quantity - fee - tax
            self._cash += proceed
            self._total_realized_pnl += realized_pnl

            if quantity >= pos.quantity:
                del self._positions[code]
            else:
                pos.quantity -= quantity

            trade = TradeHistory(
                code=code,
                name=name,
                strategy=strategy,
                side="SELL",
                quantity=quantity,
                price=filled_price,
                amount=filled_price * quantity,
                fee=fee,
                tax=tax,
                realized_pnl=realized_pnl,
                realized_pnl_rate=realized_rate,
            )
            self._trade_history.append(trade)
            self._strategy_trades[strategy].append(trade)

            # 전략 성과 갱신
            perf = self._perf[strategy]
            perf.total_trades += 1
            perf.total_pnl += realized_pnl
            if realized_pnl >= 0:
                perf.wins += 1
                perf.avg_win = (
                    (perf.avg_win * (perf.wins - 1) + realized_rate) / perf.wins
                )
            else:
                perf.losses += 1
                perf.avg_loss = (
                    (perf.avg_loss * (perf.losses - 1) + realized_rate) / perf.losses
                )

            logger.info(
                f"매도 체결 반영 | {name}({code}) {quantity}주 @{filled_price:,.0f}원"
                f" | 실현손익={realized_pnl:+,.0f}원 ({realized_rate:+.2%})"
            )
            return realized_pnl

    # ─────────────────────────────────────────────────────────────
    # 월간 동적 리밸런싱
    # ─────────────────────────────────────────────────────────────

    def monthly_rebalance(self) -> dict[str, float]:
        """
        최근 20 트레이드 기준 전략별 성과 평가 후 비중 조정.
        매월 1회 호출 권장.
        반환: 새 비중 dict
        """
        with self._lock:
            current_month = datetime.now().month
            if self._last_rebalance_month == current_month:
                logger.info("이번 달 리밸런싱 이미 완료")
                return dict(self._weights)

            adjustments: dict[str, float] = {}
            for strategy in ("S1", "S2", "S3", "S5"):
                trades = list(self._strategy_trades[strategy])
                if len(trades) < 5:
                    adjustments[strategy] = 0.0
                    continue
                sell_trades = [t for t in trades if t.side == "SELL"]
                if not sell_trades:
                    adjustments[strategy] = 0.0
                    continue
                avg_pnl = sum(t.realized_pnl_rate for t in sell_trades) / len(sell_trades)
                adjustments[strategy] = WEIGHT_ADJUST_STEP if avg_pnl > 0 else -WEIGHT_ADJUST_STEP

            for strategy, delta in adjustments.items():
                old = self._weights.get(strategy, 0.0)
                self._weights[strategy] = max(0.0, old + delta)

            # 정규화 (현금 최소 10% 보장)
            self._weights = self._normalize_weights(self._weights)
            self._last_rebalance_month = current_month

            logger.info(f"월간 리밸런싱 완료: {self._weights}")
            return dict(self._weights)

    def _normalize_weights(self, weights: dict[str, float]) -> dict[str, float]:
        """전체 합 100%, 현금 최소 10% 보장. CASH/cash 키 모두 처리."""
        cash_key = "cash" if "cash" in weights else "CASH"
        strategy_total = sum(v for k, v in weights.items() if k not in ("CASH", "cash"))
        max_strategy_total = 1.0 - MIN_CASH_RATIO

        if strategy_total > max_strategy_total:
            scale = max_strategy_total / strategy_total
            for k in list(weights.keys()):
                if k not in ("CASH", "cash"):
                    weights[k] *= scale
            weights[cash_key] = MIN_CASH_RATIO
        else:
            weights[cash_key] = max(MIN_CASH_RATIO, 1.0 - strategy_total)

        return weights

    # ─────────────────────────────────────────────────────────────
    # 상태 조회
    # ─────────────────────────────────────────────────────────────

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def positions(self) -> dict[str, Position]:
        with self._lock:
            return dict(self._positions)

    @property
    def position_amounts(self) -> dict[str, float]:
        with self._lock:
            return {code: pos.eval_amount for code, pos in self._positions.items()}

    @property
    def total_eval(self) -> float:
        with self._lock:
            stock_eval = sum(pos.eval_amount for pos in self._positions.values())
            return self._cash + stock_eval

    @property
    def total_unrealized_pnl(self) -> float:
        with self._lock:
            return sum(pos.unrealized_pnl for pos in self._positions.values())

    @property
    def weights(self) -> dict[str, float]:
        with self._lock:
            return dict(self._weights)

    def get_strategy_budget(self, strategy: str) -> float:
        """전략에 할당된 예산 (현재 총 평가금액 × 전략 비중)"""
        with self._lock:
            return self.total_eval * self._weights.get(strategy, 0.0)

    def get_summary(self) -> dict:
        with self._lock:
            total = self.total_eval
            invested = total - self._cash
            return {
                "total_eval": total,
                "cash": self._cash,
                "invested": invested,
                "total_unrealized_pnl": self.total_unrealized_pnl,
                "total_realized_pnl": self._total_realized_pnl,
                "total_return_rate": (total - self._initial_capital) / self._initial_capital,
                "positions": {
                    code: {
                        "name": pos.name,
                        "strategy": pos.strategy,
                        "qty": pos.quantity,
                        "avg_price": pos.avg_price,
                        "current_price": pos.current_price,
                        "unrealized_pnl": pos.unrealized_pnl,
                        "unrealized_pnl_rate": pos.unrealized_pnl_rate,
                    }
                    for code, pos in self._positions.items()
                },
                "weights": self._weights,
            }
