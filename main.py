"""
자동매매 메인 진입점

실행:
    python main.py                    # 기본 (settings.TRADING_MODE 기준)
    TRADING_MODE=mock python main.py  # 모의투자
    TRADING_MODE=paper python main.py # 페이퍼 트레이딩

개발 순서 (절대 건너뛰기 금지):
  1. mock    → KIS 모의투자 API 연결 검증
  2. paper   → 실시간 시세 기반 페이퍼 트레이딩 2~4주
  3. live    → 소액 실거래 전환 (risk_manager 파라미터 보수적으로)
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from datetime import datetime

from config.settings import settings
from core.kis_api_client import KISApiClient
from core.market_data_collector import MarketDataCollector
from core.monitoring import TradeLogger, Notifier, setup_logging
from core.order_manager import OrderManager
from core.portfolio_manager import PortfolioManager
from core.risk_manager import RiskManager
from core.rl_agent_wrapper import RLAgentWrapper
from core.strategy_engine import StrategyEngine
from paper_trading.paper_trader import PaperTrader

setup_logging()
logger = logging.getLogger(__name__)

# 장전 뉴스 수집 시작 시각 (HH:MM)
_PREMARKET_START_TIME = "0850"


class AutoTrader:

    def __init__(self) -> None:
        logger.info(f"AutoTrader 시작 | 모드={settings.TRADING_MODE}")

        self._api = KISApiClient()
        self._portfolio = PortfolioManager(initial_capital=settings.INITIAL_CAPITAL)
        self._risk = RiskManager(initial_capital=settings.INITIAL_CAPITAL)
        self._data = MarketDataCollector()
        self._notifier = Notifier()
        self._trade_logger = TradeLogger()

        self._strategy = StrategyEngine(
            data_collector=self._data,
            portfolio=self._portfolio,
            risk_manager=self._risk,
            api_client=self._api,
        )

        # S5 장전 준비 완료 여부
        self._premarket_done_today: str = ""   # "YYYYMMDD" 저장
        self._rl = RLAgentWrapper(model_path=None)  # 재학습 후 경로 지정

        # 페이퍼 트레이딩 또는 실거래 주문 매니저
        if settings.is_paper:
            self._paper = PaperTrader(
                portfolio=self._portfolio,
                risk_manager=self._risk,
                initial_capital=settings.INITIAL_CAPITAL,
            )
            self._order_mgr = None
        else:
            self._paper = None
            self._order_mgr = OrderManager(
                api=self._api,
                risk_manager=self._risk,
                on_fill=self._on_order_filled,
            )

        self._running = False

    def start(self) -> None:
        # 시그널 핸들러 (Ctrl+C → 안전 종료)
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        logger.info("API 초기화 중...")
        self._api.init()

        # 잔고 동기화
        if not settings.is_paper:
            self._sync_balance()

        # 실시간 구독 (watch list는 별도 설정 필요)
        watch_codes = self._get_watch_list()
        if watch_codes:
            self._api.subscribe_realtime(
                codes=watch_codes,
                on_price=self._on_realtime_price,
            )

        # S5 섹터 대장주 캐시 초기화 (분기 만료 시 갱신)
        if settings.STRATEGY_S5_ENABLED:
            threading.Thread(
                target=self._strategy.sector_manager.refresh_leaders,
                daemon=True,
                name="sector-cache-refresh",
            ).start()

        self._running = True
        self._notifier.send(
            f"AutoTrader 시작 | 모드={settings.TRADING_MODE} | "
            f"자본={settings.INITIAL_CAPITAL:,.0f}원"
        )
        self._main_loop()

    def _main_loop(self) -> None:
        """메인 루프: 1초 간격으로 상태 점검"""
        while self._running:
            try:
                now = datetime.now()
                time_str = now.strftime("%H%M%S")

                # S5 장전 뉴스 수집 (08:50~09:00, 하루 1회)
                today_str = now.strftime("%Y%m%d")
                if (
                    settings.STRATEGY_S5_ENABLED
                    and _PREMARKET_START_TIME <= now.strftime("%H%M") < "0900"
                    and self._premarket_done_today != today_str
                ):
                    self._premarket_done_today = today_str
                    threading.Thread(
                        target=self._run_premarket,
                        daemon=True,
                        name="s5-premarket",
                    ).start()

                # 장 시간 내에만 동작 (09:00~15:35)
                if not ("090000" <= time_str <= "153500"):
                    time.sleep(60)
                    continue

                # kill switch 확인
                if self._risk._is_kill_switch_active():
                    logger.critical("Kill switch 활성화 - 대기 중")
                    if self._order_mgr:
                        self._order_mgr.cancel_all()
                    time.sleep(30)
                    continue

                # 체결 동기화 (실거래)
                if self._order_mgr:
                    self._order_mgr.sync_filled_orders()

                # 장 마감 전 일일 리포트 (15:30)
                if "153000" <= time_str <= "153100":
                    self._send_daily_report()

                time.sleep(1)

            except Exception as exc:
                logger.error(f"메인 루프 오류: {exc}", exc_info=True)
                self._notifier.send(f"메인 루프 오류: {exc}", level="ERROR")
                time.sleep(5)

    def _on_realtime_price(self, code: str, data: dict) -> None:
        """WebSocket 실시간 가격 콜백"""
        price = data["current_price"]

        # 데이터 수집기 갱신
        self._data.on_price_tick(code, data)

        # 포트폴리오 가격 갱신
        self._portfolio.update_price(code, price)

        # 시세 지연 감지
        stale = self._data.is_price_stale(code)
        self._risk.set_price_stale(stale)

        # 페이퍼 트레이딩 체결 처리
        if self._paper:
            self._paper.on_price(code, price)

    def _on_order_filled(self, order) -> None:
        """실거래 체결 콜백"""
        from core.risk_manager import TradeRecord
        from config.constants import COMMISSION_RATE, TRANSACTION_TAX_RATE

        fee = order.avg_filled_price * order.filled_qty * COMMISSION_RATE
        tax = (
            order.avg_filled_price * order.filled_qty * TRANSACTION_TAX_RATE
            if order.side.value == "SELL" else 0.0
        )

        if order.side.value == "BUY":
            self._portfolio.on_buy_filled(
                code=order.code, name=order.name, strategy=order.strategy,
                quantity=order.filled_qty, filled_price=order.avg_filled_price, fee=fee,
            )
        else:
            pnl = self._portfolio.on_sell_filled(
                code=order.code, name=order.name, strategy=order.strategy,
                quantity=order.filled_qty, filled_price=order.avg_filled_price,
                fee=fee, tax=tax,
            )
            pos = self._portfolio.positions.get(order.code)
            avg_buy = pos.avg_price if pos else order.avg_filled_price
            pnl_rate = pnl / (avg_buy * order.filled_qty) if avg_buy > 0 else 0.0
            self._risk.record_trade(TradeRecord(
                strategy=order.strategy, code=order.code, side="SELL",
                pnl=pnl, pnl_rate=pnl_rate,
            ))

        self._risk.update_portfolio_state(
            total_eval=self._portfolio.total_eval,
            cash=self._portfolio.cash,
            positions=self._portfolio.position_amounts,
        )
        self._notifier.notify_trade(
            side=order.side.value,
            code=order.code,
            name=order.name,
            qty=order.filled_qty,
            price=order.avg_filled_price,
        )
        self._trade_logger.log_trade({
            "order_no": order.order_no,
            "code": order.code,
            "side": order.side.value,
            "qty": order.filled_qty,
            "price": order.avg_filled_price,
        })

    def _sync_balance(self) -> None:
        try:
            balance = self._api.get_balance()
            self._risk.update_portfolio_state(
                total_eval=balance.total_eval,
                cash=balance.cash,
                positions={p.code: p.eval_amount for p in balance.positions},
            )
            logger.info(
                f"잔고 동기화 | 총 평가={balance.total_eval:,.0f}원 "
                f"| 현금={balance.cash:,.0f}원"
            )
        except Exception as exc:
            logger.error(f"잔고 동기화 실패: {exc}")

    def _run_premarket(self) -> None:
        """S5 장전 뉴스 수집 → 섹터 점수 → 대장주 선정 → WebSocket 구독 업데이트."""
        try:
            logger.info("[main] S5 장전 준비 시작")
            watchlist = self._strategy.prepare_premarket()
            if watchlist:
                # 기존 구독에 S5 워치리스트 추가
                self._api.subscribe_realtime(
                    codes=watchlist,
                    on_price=self._on_realtime_price,
                )
                logger.info("[main] S5 워치리스트 WebSocket 구독: %s", watchlist)
            self._notifier.send(f"S5 장전 준비 완료 | {len(watchlist)}종목 선정")
        except Exception as exc:
            logger.error("[main] S5 장전 준비 오류: %s", exc, exc_info=True)

    def _get_watch_list(self) -> list[str]:
        """감시 종목 목록. S5 워치리스트는 장전 스레드에서 동적 추가."""
        # S5 섹터 UNIVERSE 전체 종목 초기 구독 (장전 준비 전 임시)
        if settings.STRATEGY_S5_ENABLED:
            return self._strategy.sector_manager.get_watchlist()
        return []

    def _send_daily_report(self) -> None:
        summary = self._portfolio.get_summary()
        self._notifier.send_daily_report(summary)

    def _shutdown(self, signum, frame) -> None:
        logger.info("종료 신호 수신 - 안전 종료 시작")
        self._running = False
        if self._order_mgr:
            self._order_mgr.cancel_all()
        self._notifier.send("AutoTrader 종료", level="WARN")
        sys.exit(0)


if __name__ == "__main__":
    trader = AutoTrader()
    trader.start()
