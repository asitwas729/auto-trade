"""
S5 전략용 강화학습 환경 (35-feature)
KoreanStockEnv 상속 + S5 전용 특징 16개 추가
"""

from __future__ import annotations

from typing import Tuple, Optional

import numpy as np

from rl_agent.environment import KoreanStockEnv
from rl_agent.feature_builder import (
    N_FEATURES_S5,
    N_ACTIONS,
    build_feature_vector_s5,
)

try:
    import gymnasium as gym
except ImportError:
    import gym  # type: ignore


class S5KoreanStockEnv(KoreanStockEnv):
    """
    S5 뉴스 섹터 모멘텀 전략 전용 환경.
    observation_space: Box(35,)  (기존 19 + S5 16)

    추가 초기화 파라미터:
        sector_scores: {date_str: {"sector": score, ...}} — 날짜별 섹터 점수
        leader_scores: {date_str: {code: leader_score}} — 날짜별 대장주 점수
    이 정보가 없으면 0.5 기본값 사용.
    """

    def __init__(
        self,
        df: "pd.DataFrame",
        initial_capital: float = 10_000_000,
        position_ratio: float = 0.9,
        sector_scores: Optional[dict] = None,   # {date_str: {"sector": score}}
        leader_scores: Optional[dict] = None,   # {date_str: {code: float}}
        code: str = "",
    ) -> None:
        # 부모 __init__ 호출 전에 observation_space를 교체해야 함
        # → super().__init__ 후 덮어씀
        super().__init__(df=df, initial_capital=initial_capital, position_ratio=position_ratio)

        self._sector_scores_map = sector_scores or {}
        self._leader_scores_map = leader_scores or {}
        self._code = code

        # 35-feature 공간으로 덮어쓰기
        self.observation_space = gym.spaces.Box(
            low=-np.inf, high=np.inf, shape=(N_FEATURES_S5,), dtype=np.float32
        )

    def _get_obs(self) -> np.ndarray:
        idx = min(self._step, len(self._df) - 1)
        row = self._df.iloc[idx]
        close = float(row["close"])
        open_price = float(row["open"])
        scale = float(row.get("ma20", close)) or close

        position_ratio = (self._position_qty * close) / (
            self._cash + self._position_qty * close + 1e-9
        )
        unrealized_pnl_rate = 0.0
        if self._position_qty > 0 and self._avg_price > 0:
            unrealized_pnl_rate = (close - self._avg_price) / self._avg_price

        indicators = {k: float(row.get(k, 0) or 0) for k in [
            "gap_rate", "bounce_from_low", "rsi14", "macd_hist",
            "bb_pct", "ma5", "ma20", "atr_rate", "vol_ratio",
        ]}

        time_str = str(row.get("time", "090000"))
        date_str = str(row.get("date", ""))

        # S5 전용 특징 — DataFrame에 컬럼이 있으면 사용, 없으면 기본값
        sector_score = float(row.get("sector_score", 0) or 0)
        leader_score_val = float(row.get("leader_score", 0) or 0)

        # 날짜별 섹터/대장주 점수 오버라이드 (학습 데이터에 삽입된 경우)
        if date_str and date_str in self._sector_scores_map:
            sector_score = max(
                sector_score,
                max(self._sector_scores_map[date_str].values(), default=0.0),
            )
        if date_str and self._code and date_str in self._leader_scores_map:
            leader_score_val = self._leader_scores_map[date_str].get(
                self._code, leader_score_val
            )

        # 기타 S5 특징
        vol_ratio = float(indicators.get("vol_ratio", 1.0))
        ma5 = indicators.get("ma5", close) or close
        three_min_up_ratio = float(row.get("three_min_up_ratio", 0) or 0)
        bid_ask_ratio = float(row.get("bid_ask_ratio", 0.5) or 0.5)
        market_cap_trillion = float(row.get("market_cap_trillion", 0) or 0)
        foreign_buy_ratio = float(row.get("foreign_buy_ratio", 0) or 0)
        inst_buy_ratio = float(row.get("inst_buy_ratio", 0) or 0)
        related_sector_avg = float(row.get("related_sector_avg", 0) or 0)

        return build_feature_vector_s5(
            close=close,
            open_price=open_price,
            high=float(row["high"]),
            low=float(row["low"]),
            indicators=indicators,
            market_change=float(row.get("market_change_rate", 0) or 0),
            market_breadth=float(row.get("market_breadth", 0.5) or 0.5),
            position_ratio=position_ratio,
            unrealized_pnl_rate=unrealized_pnl_rate,
            holding_days=self._holding_days,
            current_time=time_str,
            price_scale=scale,
            sector_score=sector_score,
            leader_score=leader_score_val,
            three_min_up_ratio=three_min_up_ratio,
            news_freshness_min=0.0,
            sector_rank=float(row.get("sector_rank", 0) or 0),
            bid_ask_ratio=bid_ask_ratio,
            market_cap_trillion=market_cap_trillion,
            foreign_buy_ratio=foreign_buy_ratio,
            inst_buy_ratio=inst_buy_ratio,
            related_sector_avg=related_sector_avg,
            holding_days_s5=self._holding_days,
            unrealized_s5=unrealized_pnl_rate,
        )
