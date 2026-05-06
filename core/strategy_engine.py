"""
전략 엔진
- S1/S2/S3/S4/S5 전략 판단 통합
- BUY/SELL/HOLD 시그널 생성
- S4 위험도에 따라 S1/S2/S5 자동 ON/OFF
- S5: 장전 뉴스 수집 → 섹터 점수 → 대장주 선정 → 09:20 진입
"""

from __future__ import annotations

import logging
import threading
from typing import Optional, TYPE_CHECKING

from config.settings import settings
from core.market_data_collector import MarketDataCollector
from core.news_collector import NewsCollector
from core.portfolio_manager import PortfolioManager
from core.risk_manager import RiskManager
from core.sector_manager import SectorManager
from strategies.s1_open_volatility import S1OpenVolatility
from strategies.s2_gap_news import S2GapNews
from strategies.s3_swing_momentum import S3SwingMomentum
from strategies.s4_volatility_filter import S4VolatilityFilter
from strategies.s5_sector_momentum import S5SectorMomentum
from strategies.s6_breakout import S6Breakout
from strategies.s7_pullback import S7Pullback
from strategies.s8_afternoon_trend import S8AfternoonTrend
from strategies.s9_close_bet import S9CloseBet
from strategies.base_strategy import StrategySignal

if TYPE_CHECKING:
    from core.kis_api_client import KISApiClient

logger = logging.getLogger(__name__)


class StrategyEngine:

    def __init__(
        self,
        data_collector: MarketDataCollector,
        portfolio: PortfolioManager,
        risk_manager: RiskManager,
        api_client: Optional["KISApiClient"] = None,
    ) -> None:
        self._data = data_collector
        self._portfolio = portfolio
        self._risk = risk_manager

        self._s1 = S1OpenVolatility()
        self._s2 = S2GapNews()
        self._s3 = S3SwingMomentum()
        self._s4 = S4VolatilityFilter()
        self._s5 = S5SectorMomentum(data_collector=data_collector)
        self._s6 = S6Breakout()
        self._s7 = S7Pullback()
        self._s8 = S8AfternoonTrend()
        self._s9 = S9CloseBet()

        # S5 뉴스/섹터 컴포넌트
        self._news = NewsCollector()
        self._sector = SectorManager(api_client=api_client)

        # 전략별 enabled 설정 반영
        if not settings.STRATEGY_S5_ENABLED:
            self._s5.disable()
        if not settings.STRATEGY_S6_ENABLED:
            self._s6.disable()
        if not settings.STRATEGY_S7_ENABLED:
            self._s7.disable()
        if not settings.STRATEGY_S8_ENABLED:
            self._s8.disable()
        if not settings.STRATEGY_S9_ENABLED:
            self._s9.disable()

        # 장전 준비 완료 플래그
        self._premarket_ready = threading.Event()

        logger.info("StrategyEngine 초기화 (S1~S9)")

    def update_market_filter(
        self,
        kospi_change: float,
        kosdaq_change: float,
        kospi_atr_rate: float,
        advance: int,
        decline: int,
        today_amount: float,
        avg_amount: float,
    ) -> str:
        """S4 시장 위험도 갱신. 반환: 위험도 문자열."""
        level = self._s4.evaluate(
            kospi_change_rate=kospi_change,
            kosdaq_change_rate=kosdaq_change,
            kospi_atr_rate=kospi_atr_rate,
            advance_count=advance,
            decline_count=decline,
            total_market_amount=today_amount,
            avg_market_amount=avg_amount,
        )
        # 위험도를 리스크 매니저에 동기화
        self._risk.set_market_risky(self._s4.is_market_risky)

        # S1/S2/S5 자동 ON/OFF
        if self._s4.is_market_risky:
            if self._s1.enabled:
                self._s1.disable()
                logger.warning("[S4] S1 자동 OFF (시장 위험)")
            if self._s2.enabled:
                self._s2.disable()
                logger.warning("[S4] S2 자동 OFF (시장 위험)")
            if self._s5.enabled:
                self._s5.disable()
                logger.warning("[S4] S5 자동 OFF (시장 위험)")
        else:
            if not self._s1.enabled:
                self._s1.enable()
                logger.info("[S4] S1 자동 ON")
            if not self._s2.enabled:
                self._s2.enable()
                logger.info("[S4] S2 자동 ON")
            if not self._s5.enabled and settings.STRATEGY_S5_ENABLED:
                self._s5.enable()
                logger.info("[S4] S5 자동 ON")

        return level

    def evaluate_s1(
        self,
        code: str,
        name: str,
        price_data: dict,
        indicators: dict,
        orderbook: dict,
        current_time: str,
        position_qty: int = 0,
        avg_entry_price: float = 0.0,
    ) -> Optional[StrategySignal]:
        return self._s1.evaluate(
            code=code,
            name=name,
            current_price=price_data["current_price"],
            open_price=price_data["open"],
            high_price=price_data["high"],
            low_price=price_data["low"],
            prev_close=price_data["prev_close"],
            ma5=indicators.get("ma5", 0),
            ma20=indicators.get("ma20", 0),
            volume=price_data.get("volume", 0),
            vol_ma20=indicators.get("vol_ma20", 1),
            vol_ratio=indicators.get("vol_ratio", 1),
            ask1=orderbook.get("ask1", 0),
            bid1=orderbook.get("bid1", 0),
            bid_qty_sum=orderbook.get("bid_qty_sum", 0),
            ask_qty_sum=orderbook.get("ask_qty_sum", 0),
            current_time=current_time,
            position_qty=position_qty,
            avg_entry_price=avg_entry_price,
        )

    def evaluate_s2(
        self,
        code: str,
        name: str,
        price_data: dict,
        indicators: dict,
        after_hours_close: int,
        has_news: bool,
        current_time: str,
        position_qty: int = 0,
        avg_entry_price: float = 0.0,
    ) -> Optional[StrategySignal]:
        return self._s2.evaluate(
            code=code,
            name=name,
            current_price=price_data["current_price"],
            open_price=price_data["open"],
            prev_close=price_data["prev_close"],
            after_hours_close=after_hours_close,
            vol_ratio=indicators.get("vol_ratio", 1),
            has_news=has_news,
            current_time=current_time,
            position_qty=position_qty,
            avg_entry_price=avg_entry_price,
        )

    def evaluate_s3(
        self,
        code: str,
        name: str,
        price_data: dict,
        indicators: dict,
        report_target: int,
        theme_strength: float,
        near_52w_high: bool,
        resistance_break: bool,
        position_qty: int = 0,
        avg_entry_price: float = 0.0,
        entry_date=None,
        high_since_entry: int = 0,
    ) -> Optional[StrategySignal]:
        return self._s3.evaluate(
            code=code,
            name=name,
            current_price=price_data["current_price"],
            report_target_price=report_target,
            vol_ratio=indicators.get("vol_ratio", 1),
            theme_strength=theme_strength,
            near_52w_high=near_52w_high,
            resistance_break=resistance_break,
            atr_rate=indicators.get("atr_rate", 0),
            position_qty=position_qty,
            avg_entry_price=avg_entry_price,
            entry_date=entry_date,
            high_since_entry=high_since_entry,
        )

    # ─── S5 장전 준비 ────────────────────────────────────────────

    def prepare_premarket(self, price_cache: Optional[dict] = None) -> list[str]:
        """
        장전 뉴스 수집 → 섹터 점수 → 대장주 선정 → S5 워치리스트 반환.
        main.py 장전 스레드에서 08:50경 호출.

        Args:
            price_cache: {code: {"change_rate", "vol_ratio", "trade_value_ratio"}}
                         None이면 기본값 사용
        Returns:
            당일 S5 워치리스트 종목 코드 리스트
        """
        if not settings.STRATEGY_S5_ENABLED:
            return []

        try:
            # 1. 뉴스 수집
            news_items = self._news.fetch()
            logger.info("[S5] 장전 뉴스 %d건 수집", len(news_items))

            # 2. 섹터 점수 계산
            sector_scores = self._sector.score_sectors(news_items)

            # 3. 대장주 선정
            leaders = self._sector.select_leaders(sector_scores, price_cache)

            # 4. S5 전략에 대장주 등록
            self._s5.update_leaders(leaders)

            # 5. 장전 준비 완료 플래그
            self._premarket_ready.set()

            watchlist = self._s5.get_watchlist()
            logger.info("[S5] 장전 준비 완료 | 워치리스트 %d종목: %s", len(watchlist), watchlist)
            return watchlist

        except Exception as exc:
            logger.error("[S5] 장전 준비 실패: %s", exc, exc_info=True)
            return []

    def evaluate_s5(
        self,
        code: str,
        row: dict,
        state: dict,
    ) -> Optional[StrategySignal]:
        """
        S5 전략 평가. prepare_premarket() 이후 호출.

        Args:
            code: 종목코드
            row:  {"close", "ma5", "vol_ratio", "change_rate", "time"} dict
            state: {"position_qty", "avg_price", "entry_date", "cash"}
        """
        if not self._premarket_ready.is_set():
            return None
        return self._s5.evaluate(code, row, state)

    def update_stock_rank(self, price_cache: dict) -> None:
        """
        장중 대장주 leader_score 업데이트 (선택적 호출).
        price_cache 형식: {code: {"change_rate", "vol_ratio", "trade_value_ratio"}}
        """
        if not settings.STRATEGY_S5_ENABLED:
            return
        try:
            sector_scores = {
                info.sector: info.sector_score
                for info in self._s5._leaders.values()
            }
            if sector_scores:
                leaders = self._sector.select_leaders(sector_scores, price_cache)
                self._s5.update_leaders(leaders)
        except Exception as exc:
            logger.debug("[S5] 대장주 업데이트 오류: %s", exc)

    def get_s5_watchlist(self) -> list[str]:
        """S5 워치리스트 반환 (WebSocket 구독용)."""
        return self._s5.get_watchlist()

    # ─── S6/S7/S8/S9 thin wrappers ───────────────────────────────
    def evaluate_s6(self, **kwargs) -> Optional[StrategySignal]:
        return self._s6.evaluate(**kwargs)

    def evaluate_s7(self, **kwargs) -> Optional[StrategySignal]:
        return self._s7.evaluate(**kwargs)

    def evaluate_s8(self, **kwargs) -> Optional[StrategySignal]:
        return self._s8.evaluate(**kwargs)

    def evaluate_s9(self, **kwargs) -> Optional[StrategySignal]:
        return self._s9.evaluate(**kwargs)

    @property
    def s4_risk_level(self) -> str:
        return self._s4.current_risk_level

    @property
    def s1(self) -> S1OpenVolatility:
        return self._s1

    @property
    def s2(self) -> S2GapNews:
        return self._s2

    @property
    def s3(self) -> S3SwingMomentum:
        return self._s3

    @property
    def s5(self) -> S5SectorMomentum:
        return self._s5

    @property
    def s6(self) -> S6Breakout:
        return self._s6

    @property
    def s7(self) -> S7Pullback:
        return self._s7

    @property
    def s8(self) -> S8AfternoonTrend:
        return self._s8

    @property
    def s9(self) -> S9CloseBet:
        return self._s9

    @property
    def sector_manager(self) -> SectorManager:
        return self._sector
