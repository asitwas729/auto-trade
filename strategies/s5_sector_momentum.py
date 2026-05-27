"""
S5: 뉴스 섹터 모멘텀 + 대장주 5일선 추종 전략

진입 조건 (09:20 이후):
  1. sector_score >= 0.3
  2. current_price > ma5
  3. 최근 3분봉 우상향 비율 >= 0.6
  4. vol_ratio >= 2.0 (5일 평균 대비 거래량)
  5. 미진입 상태

청산 조건 (우선순위 순):
  1. 익절: +15%
  2. 손절: -5%
  3. MA5 이탈: close < ma5
  4. 급등과열: 당일 등락률 > +20%
  5. 수익 약함: 보유 2일 이상 + 누적 수익 < +3%
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from config.constants import S5_PARAMS
from strategies.base_strategy import BaseStrategy, Signal, StrategySignal

if TYPE_CHECKING:
    from core.market_data_collector import MarketDataCollector
    from core.sector_manager import StockRankInfo

logger = logging.getLogger(__name__)


class S5SectorMomentum(BaseStrategy):
    """
    사용 예:
        s5 = S5SectorMomentum(data_collector)
        # 장전: s5.update_leaders(leader_list)
        # 매 틱: signal = s5.evaluate(code, row, state)
    """

    def __init__(self, data_collector: Optional["MarketDataCollector"] = None) -> None:
        super().__init__("S5")
        self._dc = data_collector

        # 대장주 목록 {code: StockRankInfo} — prepare_premarket()에서 채움
        self._leaders: dict[str, "StockRankInfo"] = {}

        # 포지션 추적 {code: {"entry_price": int, "entry_date": date, "qty": int}}
        self._positions: dict[str, dict] = {}
        # 트레일링용 최고가 추적
        self._high_since_entry: dict[str, float] = {}

    # ─── 외부 인터페이스 ───────────────────────────────────────────

    def update_leaders(self, leaders: list["StockRankInfo"]) -> None:
        """SectorManager.select_leaders() 결과를 등록."""
        self._leaders = {info.code: info for info in leaders}
        logger.info("[S5] 대장주 등록 %d개: %s", len(self._leaders),
                    [f"{v.sector}/{k}" for k, v in self._leaders.items()])

    def get_watchlist(self) -> list[str]:
        """당일 모니터링 종목 코드 목록."""
        return list(self._leaders.keys())

    def evaluate(
        self,
        code: str,
        row: dict,
        state: dict,
    ) -> Optional[StrategySignal]:
        """
        Args:
            code: 종목코드
            row:  {"close": int, "ma5": float, "vol_ratio": float,
                   "gap_rate": float, "change_rate": float, "time": str "HHMMSS"}
            state: {"position_qty": int, "avg_price": float, "entry_date": date, "cash": float}
        """
        if not self.enabled:
            return None

        current_price = int(row.get("close", 0))
        if current_price <= 0:
            return None

        position_qty: int = state.get("position_qty", 0)
        avg_price: float = state.get("avg_price", 0.0)
        entry_date = state.get("entry_date")

        # ── 청산 우선 체크 ─────────────────────────────────────────
        if position_qty > 0 and avg_price > 0:
            sell_signal = self._check_exit(code, row, current_price, avg_price, entry_date)
            if sell_signal:
                return sell_signal

        # ── 진입 체크 ──────────────────────────────────────────────
        if position_qty == 0:
            buy_signal = self._check_entry(code, row, current_price)
            if buy_signal:
                return buy_signal

        return None

    # ─── 진입 로직 ────────────────────────────────────────────────

    def _check_entry(self, code: str, row: dict, current_price: int) -> Optional[StrategySignal]:
        if code not in self._leaders:
            return None

        leader = self._leaders[code]

        # 조건 1: 진입 가능 시각 (09:20 이후)
        time_str = str(row.get("time", "000000"))
        if time_str < S5_PARAMS["entry_time"]:
            return None

        # 조건 2: sector_score 충족
        if leader.sector_score < S5_PARAMS["min_sector_score"]:
            return None

        # 조건 3: 현재가 > MA5
        ma5 = float(row.get("ma5", 0) or 0)
        if ma5 <= 0 or current_price <= ma5:
            return None

        # 조건 4: 거래량 2배 이상
        vol_ratio = float(row.get("vol_ratio", 0) or 0)
        if vol_ratio < S5_PARAMS["min_vol_ratio"]:
            return None

        # 조건 5: 3분봉 우상향 비율 >= 0.6
        up_ratio = self._get_three_min_up_ratio(code)
        if up_ratio < S5_PARAMS["three_min_up_ratio"]:
            return None

        reason = (
            f"S5 진입 | 섹터={leader.sector}({leader.sector_score:.2f}) | "
            f"MA5+{(current_price/ma5-1)*100:.1f}% | "
            f"거래량{vol_ratio:.1f}x | 3분봉우상향{up_ratio:.0%}"
        )
        logger.info("[S5] BUY: %s %s", code, reason)
        return StrategySignal(
            signal=Signal.BUY,
            code=code,
            name=leader.name,
            price=current_price,
            reason=reason,
            strategy="S5",
            sell_ratio=1.0,
        )

    # ─── 청산 로직 ────────────────────────────────────────────────

    def _check_exit(
        self,
        code: str,
        row: dict,
        current_price: int,
        avg_price: float,
        entry_date,
    ) -> Optional[StrategySignal]:
        pnl_rate = (current_price - avg_price) / avg_price
        ma5 = float(row.get("ma5", 0) or 0)
        change_rate = float(row.get("change_rate", 0) or 0)

        # 트레일링 최고가 갱신
        prev_high = self._high_since_entry.get(code, avg_price)
        self._high_since_entry[code] = max(prev_high, current_price)
        high_since_entry = self._high_since_entry[code]
        trail_from_high = (current_price - high_since_entry) / high_since_entry if high_since_entry > 0 else 0

        reason = None

        # 1. 익절 +12%
        if pnl_rate >= S5_PARAMS["take_profit"]:
            reason = f"S5 익절 {pnl_rate:+.1%}"

        # 2. 손절 -5%
        elif pnl_rate <= S5_PARAMS["stop_loss"]:
            reason = f"S5 손절 {pnl_rate:+.1%}"
            self._high_since_entry.pop(code, None)

        # 3. 트레일링 스탑: +5% 이상 수익 중에 최고가 대비 -3% 이탈
        elif pnl_rate >= 0.05 and trail_from_high <= -0.03:
            reason = f"S5 트레일 {pnl_rate:+.1%} (고점대비{trail_from_high:.1%})"
            self._high_since_entry.pop(code, None)

        # 4. MA5 이탈
        elif ma5 > 0 and current_price < ma5:
            reason = f"S5 MA5 이탈 (현재가={current_price:,} < MA5={ma5:,.0f})"

        # 5. 급등과열
        elif change_rate >= S5_PARAMS["overheat_rate"]:
            reason = f"S5 급등과열 (당일등락률 {change_rate:+.1%})"

        # 6. 수익 약함 (보유 N일+ & 수익 < 2%)
        elif entry_date is not None:
            holding_days = (datetime.now().date() - entry_date).days if hasattr(entry_date, "days") else 0
            if (holding_days >= S5_PARAMS["weak_hold_days"]
                    and pnl_rate < S5_PARAMS["weak_min_profit"]):
                reason = f"S5 수익약함 ({holding_days}일 보유, {pnl_rate:+.1%})"

        if reason:
            logger.info("[S5] SELL: %s %s", code, reason)
            self._high_since_entry.pop(code, None)
            return StrategySignal(
                signal=Signal.SELL,
                code=code,
                price=current_price,
                reason=reason,
                strategy="S5",
                sell_ratio=1.0,
            )
        return None

    # ─── 3분봉 우상향 비율 ────────────────────────────────────────

    def _get_three_min_up_ratio(self, code: str) -> float:
        """MarketDataCollector에서 3분봉 데이터 가져와 우상향 비율 계산."""
        if self._dc is None:
            return 0.0
        try:
            bars = self._dc.get_three_min_bars(code, n=5)
            return self._dc.calc_three_min_up_ratio(bars)
        except Exception as exc:
            logger.debug("[S5] 3분봉 계산 오류 %s: %s", code, exc)
            return 0.0

    # ─── 백테스트용 함수형 인터페이스 ────────────────────────────

    def make_bt_func(self, sector_score: float = 0.5, vol_ratio_override: Optional[float] = None):
        """
        Backtester.run()에 넘길 strategy_func 생성.

        사용 예:
            bt_func = s5.make_bt_func(sector_score=0.6)
            result = bt.run(df, bt_func, "S5")
        """
        def strategy_func(row, state):
            synthetic_row = dict(row)
            synthetic_row["time"] = "092100"  # 백테스트에서는 진입 시각 고정
            if "vol_ratio" not in synthetic_row or vol_ratio_override:
                synthetic_row["vol_ratio"] = vol_ratio_override or 2.5
            synthetic_row["change_rate"] = float(
                (row.get("close", 0) - row.get("open", row.get("close", 0)))
                / max(row.get("open", 1), 1)
            )

            # 대장주가 없으면 임시 등록
            if not self._leaders:
                from core.sector_manager import StockRankInfo
                code = str(row.get("code", "000000"))
                self._leaders[code] = StockRankInfo(
                    code=code, name=code, sector="기타",
                    sector_score=sector_score, leader_score=1.0,
                    change_rate=0.0, vol_ratio=2.5,
                )

            code = str(row.get("code", list(self._leaders.keys())[0] if self._leaders else "000000"))
            sig = self.evaluate(code, synthetic_row, state)
            if sig is None:
                return None
            return sig.signal.name  # "BUY" | "SELL"

        return strategy_func
