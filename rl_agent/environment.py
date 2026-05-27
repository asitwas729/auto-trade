"""
한국주식 강화학습 환경 (Gymnasium 인터페이스)
- PPO 학습용
- 수수료/세금/슬리피지/호가단위/상하한가 반영
- Adilbai/stock-trading-rl-agent PPO 구조 참고
"""

from __future__ import annotations

from typing import Optional, Tuple

import gymnasium as gym
import numpy as np

from config.constants import (
    COMMISSION_RATE,
    PRICE_LIMIT_RATE,
    SLIPPAGE_RATE,
    TRANSACTION_TAX_RATE,
)
from core.order_manager import round_to_tick
from rl_agent.feature_builder import N_ACTIONS, N_FEATURES, build_feature_vector


class KoreanStockEnv(gym.Env):
    """
    단일 종목 한국주식 거래 환경.
    외부 루프에서 여러 종목 OHLCV DataFrame을 순서대로 주입.

    obs: (N_FEATURES,) float32
    action: Discrete(3)  0=HOLD, 1=BUY, 2=SELL
    reward: 실현+미실현 손익률 변화 (수수료/세금 포함)
    """

    metadata = {"render_modes": []}

    def __init__(
        self,
        df: "pd.DataFrame",  # date, open, high, low, close, volume + 지표 컬럼
        initial_capital: float = 10_000_000,
        position_ratio: float = 0.9,   # 매수 시 가용 현금 비율
    ) -> None:
        super().__init__()
        self._df = df.reset_index(drop=True).copy()
        self._initial_capital = initial_capital
        self._position_ratio = position_ratio

        self.observation_space = gym.spaces.Box(
            low=-np.inf, high=np.inf, shape=(N_FEATURES,), dtype=np.float32
        )
        self.action_space = gym.spaces.Discrete(N_ACTIONS)
        self.reset()

    def reset(self, *, seed=None, options=None) -> Tuple[np.ndarray, dict]:
        super().reset(seed=seed)
        self._step = 0
        self._cash = self._initial_capital
        self._position_qty = 0
        self._avg_price = 0.0
        self._high_since_entry = 0
        self._holding_days = 0
        self._prev_portfolio_value = self._initial_capital
        return self._get_obs(), {}

    def step(self, action: int) -> Tuple[np.ndarray, float, bool, bool, dict]:
        row = self._df.iloc[self._step]
        close = int(row["close"])
        open_price = int(row["open"])
        high = int(row["high"])
        low = int(row["low"])

        # 상하한가 처리
        prev_close = int(self._df.iloc[max(0, self._step - 1)]["close"])
        upper_limit = round_to_tick(prev_close * (1 + PRICE_LIMIT_RATE))
        lower_limit = round_to_tick(prev_close * (1 - PRICE_LIMIT_RATE))
        exec_price = max(lower_limit, min(upper_limit, close))

        reward = 0.0
        _invalid_action_penalty = -0.001

        if action == 1:  # BUY
            if self._position_qty == 0 and self._cash > 0:
                buy_price = round_to_tick(exec_price * (1 + SLIPPAGE_RATE))
                budget = self._cash * self._position_ratio
                qty = max(1, int(budget // (buy_price * (1 + COMMISSION_RATE))))
                fee = buy_price * qty * COMMISSION_RATE
                cost = buy_price * qty + fee
                if cost <= self._cash:
                    self._cash -= cost
                    self._position_qty = qty
                    self._avg_price = float(buy_price)
                    self._high_since_entry = buy_price
                    self._holding_days = 0
            else:
                # 이미 포지션 보유 중이거나 현금 없을 때 BUY → 패널티
                reward += _invalid_action_penalty

        elif action == 2:  # SELL
            if self._position_qty > 0:
                sell_price = round_to_tick(exec_price * (1 - SLIPPAGE_RATE))
                fee = sell_price * self._position_qty * COMMISSION_RATE
                tax = sell_price * self._position_qty * TRANSACTION_TAX_RATE
                proceed = sell_price * self._position_qty - fee - tax
                self._cash += proceed
                self._position_qty = 0
                self._avg_price = 0.0
                self._high_since_entry = 0
                self._holding_days = 0
            else:
                # 포지션 없을 때 SELL → 패널티
                reward += _invalid_action_penalty

        if self._position_qty > 0:
            self._high_since_entry = max(self._high_since_entry, close)
            self._holding_days += 1

        # 포트폴리오 가치
        portfolio_value = self._cash + self._position_qty * exec_price
        unrealized_pnl_rate = 0.0
        if self._position_qty > 0 and self._avg_price > 0:
            unrealized_pnl_rate = (exec_price - self._avg_price) / self._avg_price

        # 포트폴리오 가치 변화를 단일 보상 소스로 사용 (realized + unrealized 이중 반영 제거)
        value_change = (portfolio_value - self._prev_portfolio_value) / self._initial_capital
        reward += value_change
        self._prev_portfolio_value = portfolio_value

        self._step += 1
        done = self._step >= len(self._df)

        obs = self._get_obs()
        info = {
            "portfolio_value": portfolio_value,
            "cash": self._cash,
            "position_qty": self._position_qty,
            "unrealized_pnl_rate": unrealized_pnl_rate,
        }
        return obs, float(reward), done, False, info

    def _get_obs(self) -> np.ndarray:
        idx = min(self._step, len(self._df) - 1)
        row = self._df.iloc[idx]
        close = float(row["close"])
        scale = float(row.get("ma20", close)) or close

        position_ratio = (self._position_qty * close) / (self._cash + self._position_qty * close + 1e-9)
        unrealized_pnl_rate = 0.0
        if self._position_qty > 0 and self._avg_price > 0:
            unrealized_pnl_rate = (close - self._avg_price) / self._avg_price

        indicators = {k: float(row.get(k, 0)) for k in [
            "gap_rate", "bounce_from_low", "rsi14", "macd_hist",
            "bb_pct", "ma5", "ma20", "atr_rate", "vol_ratio",
        ]}
        indicators["macd_hist"] = float(row.get("macd_hist", 0))

        time_val = row.get("time")
        time_str = str(time_val) if (time_val is not None and str(time_val) != "nan") else None
        return build_feature_vector(
            close=close,
            open_price=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            indicators=indicators,
            market_change=float(row.get("market_change_rate", 0)),
            market_breadth=float(row.get("market_breadth", 0.5)),
            position_ratio=position_ratio,
            unrealized_pnl_rate=unrealized_pnl_rate,
            holding_days=self._holding_days,
            current_time=time_str,
            price_scale=scale,
        )
