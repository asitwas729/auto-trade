"""
백테스터
- 한국주식 과거 데이터 기반 시뮬레이션
- 수수료 0.015% + 증권거래세 0.2% + 슬리피지 0.03% 반영
- 상하한가, 호가단위 반영
- 전략별 성과 리포트 생성
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

import numpy as np
import pandas as pd

from config.constants import (
    COMMISSION_RATE,
    SLIPPAGE_RATE,
    TRANSACTION_TAX_RATE,
    PRICE_LIMIT_RATE,
)
from core.order_manager import round_to_tick

logger = logging.getLogger(__name__)


@dataclass
class BTTrade:
    date: str
    code: str
    strategy: str
    side: str
    quantity: int
    price: float
    amount: float
    fee: float
    tax: float
    realized_pnl: float = 0.0
    realized_pnl_rate: float = 0.0


@dataclass
class BTResult:
    strategy: str
    total_trades: int = 0
    win_trades: int = 0
    loss_trades: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    avg_win_rate: float = 0.0
    avg_loss_rate: float = 0.0
    expectancy: float = 0.0
    sharpe_ratio: float = 0.0
    equity_curve: list[float] = field(default_factory=list)
    trades: list[BTTrade] = field(default_factory=list)


class Backtester:
    """
    사용 예:
        bt = Backtester(initial_capital=10_000_000)
        result = bt.run(df_ohlcv, strategy_func, "S1")
        print(bt.report(result))
    """

    def __init__(self, initial_capital: float = 10_000_000) -> None:
        self._capital = initial_capital

    def run(
        self,
        df: pd.DataFrame,
        strategy_func: Callable[[pd.Series, dict], Optional[str]],
        strategy_name: str,
        position_ratio: float = 0.25,
    ) -> BTResult:
        """
        strategy_func(row, state) → "BUY" | "SELL" | None
        df: date, open, high, low, close, volume + 기술지표 컬럼 포함
        """
        cash = self._capital
        position_qty = 0
        avg_price = 0.0
        high_since_entry = 0
        entry_date = None
        result = BTResult(strategy=strategy_name)
        equity_series = []
        daily_returns = []
        prev_equity = cash

        for i, row in df.iterrows():
            current_price = int(row["close"])
            position_value = position_qty * current_price
            equity = cash + position_value
            equity_series.append(equity)

            state = {
                "position_qty": position_qty,
                "avg_price": avg_price,
                "high_since_entry": high_since_entry,
                "entry_date": entry_date,
                "cash": cash,
            }

            action = strategy_func(row, state)

            if action == "BUY" and position_qty == 0 and cash > 0:
                budget = cash * position_ratio
                buy_price = self._apply_slippage(current_price, "BUY")
                buy_price = round_to_tick(buy_price)
                qty = max(1, int(budget // (buy_price * (1 + COMMISSION_RATE))))
                fee = buy_price * qty * COMMISSION_RATE
                cost = buy_price * qty + fee

                if cost <= cash:
                    cash -= cost
                    position_qty = qty
                    avg_price = buy_price
                    high_since_entry = buy_price
                    entry_date = row.get("date")
                    result.trades.append(BTTrade(
                        date=str(row.get("date", i)),
                        code=str(row.get("code", "")),
                        strategy=strategy_name,
                        side="BUY",
                        quantity=qty,
                        price=buy_price,
                        amount=buy_price * qty,
                        fee=fee,
                        tax=0.0,
                    ))

            elif action == "SELL" and position_qty > 0:
                sell_price = self._apply_slippage(current_price, "SELL")
                sell_price = round_to_tick(sell_price)
                fee = sell_price * position_qty * COMMISSION_RATE
                tax = sell_price * position_qty * TRANSACTION_TAX_RATE
                proceed = sell_price * position_qty - fee - tax
                realized_pnl = proceed - avg_price * position_qty
                realized_rate = realized_pnl / (avg_price * position_qty)

                cash += proceed
                result.total_pnl += realized_pnl
                result.total_trades += 1
                if realized_pnl >= 0:
                    result.win_trades += 1
                    result.avg_win_rate = (
                        result.avg_win_rate * (result.win_trades - 1) + realized_rate
                    ) / result.win_trades
                else:
                    result.loss_trades += 1
                    result.avg_loss_rate = (
                        result.avg_loss_rate * (result.loss_trades - 1) + realized_rate
                    ) / result.loss_trades

                result.trades.append(BTTrade(
                    date=str(row.get("date", i)),
                    code=str(row.get("code", "")),
                    strategy=strategy_name,
                    side="SELL",
                    quantity=position_qty,
                    price=sell_price,
                    amount=sell_price * position_qty,
                    fee=fee,
                    tax=tax,
                    realized_pnl=realized_pnl,
                    realized_pnl_rate=realized_rate,
                ))
                position_qty = 0
                avg_price = 0.0
                high_since_entry = 0
                entry_date = None

            # 트레일링 스탑용 최고가 갱신
            if position_qty > 0:
                high_since_entry = max(high_since_entry, current_price)

            # 일별 수익률
            if prev_equity > 0:
                daily_returns.append((equity - prev_equity) / prev_equity)
            prev_equity = equity

        # 미청산 포지션 청산 (마지막 종가)
        if position_qty > 0 and len(df) > 0:
            last_price = int(df.iloc[-1]["close"])
            fee = last_price * position_qty * COMMISSION_RATE
            tax = last_price * position_qty * TRANSACTION_TAX_RATE
            proceed = last_price * position_qty - fee - tax
            realized_pnl = proceed - avg_price * position_qty
            cash += proceed
            result.total_pnl += realized_pnl
            result.total_trades += 1
            if realized_pnl >= 0:
                result.win_trades += 1

        result.equity_curve = equity_series
        result.win_rate = result.win_trades / max(result.total_trades, 1)
        result.expectancy = (
            result.win_rate * result.avg_win_rate
            - (1 - result.win_rate) * abs(result.avg_loss_rate)
        )
        result.max_drawdown = self._calc_mdd(equity_series)
        result.sharpe_ratio = self._calc_sharpe(daily_returns)
        return result

    def report(self, result: BTResult) -> str:
        total_return = result.total_pnl / self._capital
        lines = [
            f"=== 백테스트 결과: {result.strategy} ===",
            f"총 트레이드: {result.total_trades}",
            f"승률: {result.win_rate:.2%}",
            f"평균 수익률(승): {result.avg_win_rate:+.2%}",
            f"평균 손실률(패): {result.avg_loss_rate:+.2%}",
            f"기대값: {result.expectancy:+.2%}",
            f"총 손익: {result.total_pnl:+,.0f}원",
            f"총 수익률: {total_return:+.2%}",
            f"MDD: {result.max_drawdown:.2%}",
            f"샤프비율: {result.sharpe_ratio:.2f}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _apply_slippage(price: int, side: str) -> float:
        if side == "BUY":
            return price * (1 + SLIPPAGE_RATE)
        return price * (1 - SLIPPAGE_RATE)

    @staticmethod
    def _calc_mdd(equity: list[float]) -> float:
        if not equity:
            return 0.0
        arr = np.array(equity)
        peak = np.maximum.accumulate(arr)
        drawdown = (arr - peak) / (peak + 1e-9)
        return float(drawdown.min())

    @staticmethod
    def _calc_sharpe(daily_returns: list[float], risk_free: float = 0.03 / 252) -> float:
        if len(daily_returns) < 2:
            return 0.0
        arr = np.array(daily_returns)
        excess = arr - risk_free
        std = np.std(excess)
        if std < 1e-12:
            return 0.0
        return float(np.mean(excess) / std * np.sqrt(252))
