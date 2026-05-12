"""전략 기반 클래스"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


class Signal(Enum):
    BUY = auto()
    SELL = auto()
    HOLD = auto()


@dataclass
class StrategySignal:
    signal: Signal
    code: str
    name: str = ""
    price: int = 0
    quantity: int = 0
    reason: str = ""
    strategy: str = ""
    sell_ratio: float = 1.0   # 매도 비율 (1.0=전량, 0.5=반매도)
    # True면 시장가로 발주 (손절/트레일/마감/강제청산 등 즉시 체결 필요).
    # 키워드 매칭 폴백이 main.py에 있지만 명시적 플래그가 더 안전.
    is_forced: bool = False


class BaseStrategy(ABC):
    def __init__(self, name: str) -> None:
        self.name = name
        self.enabled = True

    @abstractmethod
    def evaluate(self, *args, **kwargs) -> Optional[StrategySignal]:
        """시그널 생성. None이면 HOLD."""
        ...

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False
