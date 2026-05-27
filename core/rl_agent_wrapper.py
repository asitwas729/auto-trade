"""
RL Agent 래퍼
- PPO 모델 로드 (Stable-Baselines3)
- 한국주식 특징 벡터 생성 → action 추출 → 주문 의사결정 변환
- 반드시 risk_manager.check_order() 통과 후에만 주문 가능
- 미국주식 학습 agent를 한국주식 실거래에 직접 연결하지 않음
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np

from rl_agent.feature_builder import N_ACTIONS, build_feature_vector

try:
    import torch
    _TORCH_AVAILABLE = True
except ImportError:
    _TORCH_AVAILABLE = False

from strategies.base_strategy import Signal, StrategySignal

logger = logging.getLogger(__name__)

# RL action 정의
ACTION_HOLD = 0
ACTION_BUY = 1
ACTION_SELL = 2

# 모델 로드 임계값 (신뢰도)
CONFIDENCE_THRESHOLD = 0.6


class RLAgentWrapper:
    """
    PPO 모델 기반 의사결정 래퍼.

    경고:
    - 미국주식으로 학습된 모델을 model_path에 직접 사용하지 말 것.
    - 반드시 한국주식 데이터로 재학습 또는 fine-tuning 후 사용.
    - predict()의 결과는 strategy_engine 시그널과 앙상블 가능.
    """

    def __init__(self, model_path: Optional[str] = None) -> None:
        self._model = None
        self._model_path = model_path
        self._is_loaded = False

        if model_path and Path(model_path).exists():
            self._load_model(model_path)
        else:
            logger.warning(
                "RL 모델 미로드 상태. 한국주식 재학습 후 model_path를 지정하세요. "
                "현재는 규칙 기반 전략만 사용됩니다."
            )

    def _load_model(self, path: str) -> None:
        try:
            from stable_baselines3 import PPO
            self._model = PPO.load(path)
            self._is_loaded = True
            logger.info(f"PPO 모델 로드 완료: {path}")
        except Exception as exc:
            logger.error(f"PPO 모델 로드 실패: {exc}")
            self._model = None
            self._is_loaded = False

    def predict(
        self,
        obs: np.ndarray,
        deterministic: bool = True,
    ) -> tuple[int, float]:
        """
        obs: build_feature_vector()의 출력 (N_FEATURES,)
        반환: (action, confidence)
        모델 미로드 시 HOLD 반환
        """
        if not self._is_loaded or self._model is None:
            return ACTION_HOLD, 0.0

        # obs shape 검증
        expected_shape = self._model.observation_space.shape
        if obs.shape != expected_shape:
            logger.error(f"obs shape 불일치: expected {expected_shape}, got {obs.shape}")
            return ACTION_HOLD, 0.0

        try:
            action, _states = self._model.predict(obs, deterministic=deterministic)
            confidence = 0.0

            if _TORCH_AVAILABLE:
                try:
                    with torch.no_grad():
                        obs_tensor = self._model.policy.obs_to_tensor(obs)[0]
                        dist = self._model.policy.get_distribution(obs_tensor)
                        probs = dist.distribution.probs.cpu().numpy()
                        if probs.ndim > 1:
                            probs = probs[0]
                        action_idx = int(action)
                        if 0 <= action_idx < len(probs):
                            confidence = float(probs[action_idx])
                except Exception as conf_exc:
                    logger.debug(f"신뢰도 추출 실패 (기본값 0.0 사용): {conf_exc}")

            return int(action), confidence
        except Exception as exc:
            logger.error(f"RL predict 오류: {exc}")
            return ACTION_HOLD, 0.0

    def action_to_signal(
        self,
        action: int,
        confidence: float,
        code: str,
        name: str,
        current_price: int,
        position_qty: int = 0,
    ) -> Optional[StrategySignal]:
        """
        RL action → StrategySignal 변환.
        신뢰도가 CONFIDENCE_THRESHOLD 미만이면 HOLD.
        """
        if confidence < CONFIDENCE_THRESHOLD:
            return None

        if action == ACTION_BUY and position_qty == 0:
            return StrategySignal(
                signal=Signal.BUY,
                code=code,
                name=name,
                price=current_price,
                reason=f"RL_BUY conf={confidence:.2f}",
                strategy="RL",
            )
        elif action == ACTION_SELL and position_qty > 0:
            return StrategySignal(
                signal=Signal.SELL,
                code=code,
                name=name,
                price=current_price,
                quantity=position_qty,
                reason=f"RL_SELL conf={confidence:.2f}",
                strategy="RL",
                sell_ratio=1.0,
            )
        return None

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded
