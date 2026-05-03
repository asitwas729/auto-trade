"""
S5SectorMomentum 전략 단위 테스트

Run:
    python -m pytest tests/test_s5_strategy.py -v -s
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from core.sector_manager import StockRankInfo
from strategies.s5_sector_momentum import S5SectorMomentum
from strategies.base_strategy import Signal


# ─── 픽스처 ───────────────────────────────────────────────────────

def _make_leader(
    code: str = "005930",
    sector: str = "반도체",
    sector_score: float = 0.8,
) -> StockRankInfo:
    return StockRankInfo(
        code=code, name="테스트종목", sector=sector,
        sector_score=sector_score, leader_score=1.0,
        change_rate=0.03, vol_ratio=3.0,
    )


def _make_row(
    close: int = 75_000,
    ma5: float = 72_000,
    vol_ratio: float = 3.0,
    change_rate: float = 0.03,
    time_str: str = "092100",
) -> dict:
    return {
        "close": close,
        "open": 73_000,
        "high": 76_000,
        "low": 72_500,
        "ma5": ma5,
        "vol_ratio": vol_ratio,
        "change_rate": change_rate,
        "time": time_str,
    }


def _make_state(position_qty: int = 0, avg_price: float = 0.0, entry_date=None) -> dict:
    return {
        "position_qty": position_qty,
        "avg_price": avg_price,
        "entry_date": entry_date,
        "cash": 5_000_000,
    }


# ─── 진입 조건 테스트 ─────────────────────────────────────────────

class TestS5Entry:

    def setup_method(self):
        mock_dc = MagicMock()
        mock_dc.get_three_min_bars.return_value = [
            {"close": 70_000}, {"close": 71_000},
            {"close": 72_000}, {"close": 73_000}, {"close": 75_000},
        ]
        mock_dc.calc_three_min_up_ratio.return_value = 1.0  # 전부 우상향
        self.s5 = S5SectorMomentum(data_collector=mock_dc)
        self.s5.update_leaders([_make_leader()])

    def test_buy_signal_all_conditions_met(self):
        row = _make_row()
        state = _make_state()
        sig = self.s5.evaluate("005930", row, state)
        assert sig is not None
        assert sig.signal == Signal.BUY
        assert sig.strategy == "S5"

    def test_no_signal_before_entry_time(self):
        row = _make_row(time_str="091500")
        state = _make_state()
        sig = self.s5.evaluate("005930", row, state)
        assert sig is None

    def test_no_signal_price_below_ma5(self):
        row = _make_row(close=71_000, ma5=72_000)
        state = _make_state()
        sig = self.s5.evaluate("005930", row, state)
        assert sig is None

    def test_no_signal_low_vol_ratio(self):
        row = _make_row(vol_ratio=1.5)
        state = _make_state()
        sig = self.s5.evaluate("005930", row, state)
        assert sig is None

    def test_no_signal_already_in_position(self):
        row = _make_row()
        state = _make_state(position_qty=100, avg_price=72_000)
        sig = self.s5.evaluate("005930", row, state)
        # 포지션 있으면 진입 불가 (청산 조건 미충족이면 None)
        if sig:
            assert sig.signal == Signal.SELL
        else:
            assert sig is None

    def test_no_signal_low_sector_score(self):
        self.s5.update_leaders([_make_leader(sector_score=0.2)])
        row = _make_row()
        state = _make_state()
        sig = self.s5.evaluate("005930", row, state)
        assert sig is None

    def test_no_signal_unknown_code(self):
        row = _make_row()
        state = _make_state()
        sig = self.s5.evaluate("999999", row, state)
        assert sig is None

    def test_no_signal_low_three_min_up_ratio(self):
        self.s5._dc.calc_three_min_up_ratio.return_value = 0.3  # 우상향 부족
        row = _make_row()
        state = _make_state()
        sig = self.s5.evaluate("005930", row, state)
        assert sig is None


# ─── 청산 조건 테스트 ─────────────────────────────────────────────

class TestS5Exit:

    def setup_method(self):
        self.s5 = S5SectorMomentum(data_collector=None)
        self.s5.update_leaders([_make_leader()])

    def _eval(self, close: int, avg_price: float, **kwargs) -> object:
        row = _make_row(close=close, **kwargs)
        state = _make_state(position_qty=100, avg_price=avg_price, entry_date=date.today())
        return self.s5.evaluate("005930", row, state)

    def test_take_profit(self):
        # +16% 익절
        sig = self._eval(close=83_700, avg_price=72_000)
        assert sig is not None
        assert sig.signal == Signal.SELL
        assert "익절" in sig.reason

    def test_stop_loss(self):
        # -6% 손절
        sig = self._eval(close=67_700, avg_price=72_000)
        assert sig is not None
        assert sig.signal == Signal.SELL
        assert "손절" in sig.reason

    def test_ma5_break(self):
        # MA5 이탈
        sig = self._eval(close=71_000, avg_price=72_000, ma5=72_000)
        assert sig is not None
        assert sig.signal == Signal.SELL
        assert "MA5" in sig.reason

    def test_overheat(self):
        # 급등과열 +21%
        sig = self._eval(close=75_000, avg_price=72_000, change_rate=0.21)
        assert sig is not None
        assert sig.signal == Signal.SELL
        assert "과열" in sig.reason

    def test_weak_profit(self):
        from datetime import timedelta
        # 3일 보유 + 수익 2% → 약함
        old_date = date.today() - timedelta(days=3)
        row = _make_row(close=73_440, ma5=70_000)  # +2%
        state = _make_state(position_qty=100, avg_price=72_000, entry_date=old_date)
        sig = self.s5.evaluate("005930", row, state)
        assert sig is not None
        assert sig.signal == Signal.SELL
        assert "약함" in sig.reason

    def test_no_exit_normal_hold(self):
        # 정상 보유 중 (+5%, MA5 위, 적정 등락률)
        sig = self._eval(close=75_600, avg_price=72_000, ma5=70_000, change_rate=0.05)
        assert sig is None


# ─── 전략 ON/OFF 테스트 ───────────────────────────────────────────

def test_disabled_strategy_returns_none():
    s5 = S5SectorMomentum()
    s5.update_leaders([_make_leader()])
    s5.disable()
    row = _make_row()
    state = _make_state()
    assert s5.evaluate("005930", row, state) is None


def test_enabled_after_disable():
    s5 = S5SectorMomentum()
    s5.disable()
    s5.enable()
    assert s5.enabled is True


# ─── 워치리스트 테스트 ────────────────────────────────────────────

def test_get_watchlist():
    s5 = S5SectorMomentum()
    s5.update_leaders([_make_leader("005930"), _make_leader("000660", "반도체")])
    wl = s5.get_watchlist()
    assert "005930" in wl
    assert "000660" in wl


# ─── 백테스트 함수형 인터페이스 테스트 ───────────────────────────

def test_make_bt_func_returns_callable():
    s5 = S5SectorMomentum(data_collector=None)
    bt_func = s5.make_bt_func(sector_score=0.8)
    assert callable(bt_func)

    row = _make_row()
    row["code"] = "005930"
    state = _make_state()
    result = bt_func(row, state)
    # 결과는 None, "BUY", "SELL" 중 하나
    assert result in (None, "BUY", "SELL")
