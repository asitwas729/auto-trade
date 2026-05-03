"""리스크 매니저 단위 테스트"""

import pytest
from core.risk_manager import RiskManager, TradeRecord


@pytest.fixture
def rm():
    r = RiskManager(initial_capital=10_000_000)
    r.update_portfolio_state(
        total_eval=10_000_000,
        cash=10_000_000,
        positions={},
    )
    return r


def test_normal_order_allowed(rm):
    v = rm.check_order("S1", "005930", "BUY", 1_000_000)
    assert v.allowed is True


def test_kill_switch_blocks(rm):
    rm.activate_kill_switch()
    v = rm.check_order("S1", "005930", "BUY", 1_000_000)
    assert v.allowed is False
    rm.deactivate_kill_switch()


def test_daily_loss_limit(rm):
    # 평가금액을 -2.5% 낮춰 일일 손실 한도 초과 시뮬레이션
    rm.update_portfolio_state(
        total_eval=9_700_000,  # -3%
        cash=9_700_000,
        positions={},
    )
    v = rm.check_order("S1", "005930", "BUY", 1_000_000)
    assert v.allowed is False


def test_consecutive_loss_disables_strategy(rm):
    for _ in range(3):
        rm.record_trade(TradeRecord("S1", "005930", "SELL", pnl=-50000, pnl_rate=-0.05))
    v = rm.check_order("S1", "005930", "BUY", 1_000_000)
    assert v.allowed is False

    rm.re_enable_strategy("S1")
    v2 = rm.check_order("S1", "005930", "BUY", 1_000_000)
    assert v2.allowed is True


def test_position_limit(rm):
    rm.update_portfolio_state(
        total_eval=10_000_000,
        cash=2_000_000,
        positions={"005930": 8_500_000},  # 85% → 20% 초과
    )
    v = rm.check_order("S1", "005930", "BUY", 1_000_000)
    assert v.allowed is False


def test_sell_bypasses_daily_loss_limit(rm):
    """매도는 일일 손실 한도 초과 시에도 허용"""
    rm.update_portfolio_state(
        total_eval=9_700_000,
        cash=9_700_000,
        positions={},
    )
    v = rm.check_order("S1", "005930", "SELL", 0)
    assert v.allowed is True
