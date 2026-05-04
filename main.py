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

# S5 전략 평가 주기 (초). 매 N초마다 워치리스트 종목 평가
_STRATEGY_TICK_SEC = 30

# S5 진입 시작 시각 (모의 공격형: 09:20 → 09:10)
_S5_ENTRY_TIME = "091000"

# S1 진입 시작 시각 (모의 공격형: 09:15 → 09:05)
_S1_ENTRY_TIME = "090500"


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

        # S5 일봉 지표 캐시 {code: {"ma5": float, "vol_ma20": float, "prev_close": int}}
        self._daily_indicators: dict[str, dict] = {}
        self._last_strategy_tick = 0.0  # time.monotonic() 마지막 평가 시각
        self._diag_tick_count = 0       # 진단 로그 주기 카운터 (10 tick = 5분)

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

                # S5 장전 뉴스 수집 (08:50~09:00 정규 OR 09:00 이후 첫 시작 시 회복)
                today_str = now.strftime("%Y%m%d")
                hhmm = now.strftime("%H%M")
                in_premarket_window = _PREMARKET_START_TIME <= hhmm < "0900"
                late_recovery_window = "0900" <= hhmm <= "1430"
                if (
                    settings.STRATEGY_S5_ENABLED
                    and self._premarket_done_today != today_str
                    and (in_premarket_window or late_recovery_window)
                ):
                    self._premarket_done_today = today_str
                    if late_recovery_window:
                        logger.warning(
                            "[main] 장전 윈도우 지나서 시작됨 - 즉시 회복 실행"
                        )
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

                # 전략 평가 (09:15 S1 / 09:20 S5, _STRATEGY_TICK_SEC 간격)
                if time_str >= _S1_ENTRY_TIME:
                    now_mono = time.monotonic()
                    if now_mono - self._last_strategy_tick >= _STRATEGY_TICK_SEC:
                        self._last_strategy_tick = now_mono
                        if settings.STRATEGY_S1_ENABLED:
                            self._evaluate_s1_watchlist(time_str)
                        if (
                            settings.STRATEGY_S5_ENABLED
                            and time_str >= _S5_ENTRY_TIME
                        ):
                            self._evaluate_s5_watchlist(time_str)
                        # 5분마다 워치리스트 상태 한 줄 진단
                        self._diag_tick_count += 1
                        if self._diag_tick_count % 10 == 0:
                            self._log_eval_diagnostic(time_str)

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
                # 일봉 지표 프리로드 (ma5, vol_ma20)
                self._preload_daily_indicators(watchlist)
            self._notifier.send(f"S5 장전 준비 완료 | {len(watchlist)}종목 선정")
        except Exception as exc:
            logger.error("[main] S5 장전 준비 오류: %s", exc, exc_info=True)

    def _preload_daily_indicators(self, codes: list[str]) -> None:
        """워치리스트 종목의 일봉 지표(ma5, vol_ma20, prev_close)를 캐시.
        파케이 캐시 우선, 없으면 KIS API 호출."""
        from datetime import timedelta as _td
        from core.market_data_collector import MarketDataCollector

        for code in codes:
            try:
                df = self._data.load_daily_ohlcv(code, days=30)
                if df is None or len(df) < 20:
                    # 캐시 없으면 API로 직접 조회 (최근 30영업일)
                    today = datetime.now().strftime("%Y%m%d")
                    start = (datetime.now() - _td(days=60)).strftime("%Y%m%d")
                    rows = self._api.get_daily_ohlcv(code, start, today)
                    if not rows:
                        logger.warning("[main] %s 일봉 조회 결과 없음", code)
                        continue
                    import pandas as pd
                    df = pd.DataFrame(rows)
                if df is None or df.empty:
                    continue
                df = MarketDataCollector.calc_indicators(df)
                last = df.iloc[-1]
                self._daily_indicators[code] = {
                    "ma5": float(last.get("ma5", 0) or 0),
                    "ma20": float(last.get("ma20", 0) or 0),
                    "vol_ma20": float(last.get("vol_ma20", 0) or 0),
                    "prev_close": int(last.get("close", 0) or 0),
                }
                logger.info(
                    "[main] %s 지표 로드: ma5=%.0f, ma20=%.0f, vol_ma20=%.0f, prev_close=%d",
                    code,
                    self._daily_indicators[code]["ma5"],
                    self._daily_indicators[code]["ma20"],
                    self._daily_indicators[code]["vol_ma20"],
                    self._daily_indicators[code]["prev_close"],
                )
            except Exception as exc:
                logger.warning("[main] %s 지표 프리로드 실패: %s", code, exc)

    def _evaluate_s5_watchlist(self, time_str: str) -> None:
        """S5 워치리스트 평가 + 시그널 발생 시 주문 발주."""
        watchlist = self._strategy.get_s5_watchlist()
        if not watchlist:
            return

        evaluated = 0
        for code in watchlist:
            try:
                price = self._data.get_last_price(code)
                if not price:
                    continue
                today_bar = self._data.get_today_ohlcv(code) or {}
                indicators = self._daily_indicators.get(code, {})
                prev_close = indicators.get("prev_close", 0)
                vol_ma20 = indicators.get("vol_ma20", 0)
                today_volume = today_bar.get("volume", 0)
                vol_ratio = (today_volume / vol_ma20) if vol_ma20 > 0 else 0.0
                change_rate = (
                    (price - prev_close) / prev_close if prev_close > 0 else 0.0
                )
                gap_rate = (
                    (today_bar.get("open", price) - prev_close) / prev_close
                    if prev_close > 0 else 0.0
                )

                row = {
                    "close": price,
                    "ma5": indicators.get("ma5", 0),
                    "vol_ratio": vol_ratio,
                    "gap_rate": gap_rate,
                    "change_rate": change_rate,
                    "time": time_str,
                }

                pos = self._portfolio.positions.get(code)
                state = {
                    "position_qty": pos.quantity if pos else 0,
                    "avg_price": pos.avg_price if pos else 0.0,
                    "entry_date": getattr(pos, "entry_date", None) if pos else None,
                    "cash": self._portfolio.cash,
                }

                signal = self._strategy.evaluate_s5(code, row, state)
                evaluated += 1
                if signal:
                    self._handle_signal(signal, state)
            except Exception as exc:
                logger.error("[main] %s 전략 평가 오류: %s", code, exc, exc_info=True)
        logger.debug("[main] S5 평가 완료: %d종목", evaluated)

    def _evaluate_s1_watchlist(self, time_str: str) -> None:
        """S1 워치리스트 평가 (옵션A: S5 워치리스트 공유, 호가는 0=조건4 자동통과)."""
        watchlist = self._strategy.get_s5_watchlist()  # 일단 S5와 공유
        if not watchlist:
            return

        for code in watchlist:
            try:
                price = self._data.get_last_price(code)
                if not price:
                    continue
                today_bar = self._data.get_today_ohlcv(code) or {}
                indicators = self._daily_indicators.get(code, {})
                prev_close = indicators.get("prev_close", 0)
                vol_ma20 = indicators.get("vol_ma20", 0)
                today_volume = today_bar.get("volume", 0)
                vol_ratio = (today_volume / vol_ma20) if vol_ma20 > 0 else 0.0

                price_data = {
                    "current_price": price,
                    "open": today_bar.get("open", price),
                    "high": today_bar.get("high", price),
                    "low": today_bar.get("low", price),
                    "prev_close": prev_close,
                    "volume": today_volume,
                }
                ind = {
                    "ma5": indicators.get("ma5", 0),
                    "ma20": indicators.get("ma20", 0),
                    "vol_ma20": vol_ma20,
                    "vol_ratio": vol_ratio,
                }
                # 호가 미수신 → 0으로 전달, S1의 조건 4가 자동 PASS
                ob = {"ask1": 0, "bid1": 0, "bid_qty_sum": 0, "ask_qty_sum": 0}

                pos = self._portfolio.positions.get(code)
                signal = self._strategy.evaluate_s1(
                    code=code,
                    name=pos.name if pos else code,
                    price_data=price_data,
                    indicators=ind,
                    orderbook=ob,
                    current_time=time_str,
                    position_qty=pos.quantity if pos else 0,
                    avg_entry_price=pos.avg_price if pos else 0.0,
                )
                if signal:
                    state = {
                        "position_qty": pos.quantity if pos else 0,
                        "avg_price": pos.avg_price if pos else 0.0,
                        "cash": self._portfolio.cash,
                    }
                    self._handle_signal(signal, state)
            except Exception as exc:
                logger.error("[main] %s S1 평가 오류: %s", code, exc, exc_info=True)

    def _log_eval_diagnostic(self, time_str: str) -> None:
        """5분마다 워치리스트 종목 상태를 INFO 로그로 출력 (진입 차단 원인 추적용)."""
        watchlist = self._strategy.get_s5_watchlist()
        if not watchlist:
            logger.info("[진단] 워치리스트 비어있음")
            return
        for code in watchlist:
            price = self._data.get_last_price(code)
            today_bar = self._data.get_today_ohlcv(code) or {}
            ind = self._daily_indicators.get(code, {})
            ma5 = ind.get("ma5", 0)
            vol_ma20 = ind.get("vol_ma20", 0)
            prev_close = ind.get("prev_close", 0)
            today_volume = today_bar.get("volume", 0)
            vol_ratio = (today_volume / vol_ma20) if vol_ma20 > 0 else 0.0
            change = (
                (price - prev_close) / prev_close * 100
                if (price and prev_close) else 0.0
            )
            ma5_status = (
                "위" if (price and ma5 and price > ma5)
                else ("아래" if (price and ma5) else "?")
            )
            logger.info(
                "[진단 %s] %s 가격=%s ma5=%.0f(%s) vol_ratio=%.2fx (S1≥1.5/S5≥2.0) 변동=%+.1f%%",
                time_str[:6], code,
                f"{price:,}" if price else "수신X",
                ma5, ma5_status, vol_ratio, change,
            )

    def _handle_signal(self, signal, state: dict) -> None:
        """전략 시그널 → 주문 발주 (paper / live mock)."""
        from config.constants import S5_PARAMS
        from strategies.base_strategy import Signal as SignalEnum

        if signal.signal == SignalEnum.BUY:
            # 매수 수량 = 가용현금 × 비중 / 가격 (S1/S5 공통 비중 사용)
            budget = state["cash"] * S5_PARAMS["position_ratio"]
            qty = int(budget // signal.price)
            if qty <= 0:
                logger.warning("[main] %s 매수 수량 0 (현금부족)", signal.code)
                return
        else:
            # 매도: 시그널의 quantity 우선, 없으면 보유 × sell_ratio
            qty = signal.quantity or int(
                state["position_qty"] * (signal.sell_ratio or 1.0)
            )
            if qty <= 0:
                return

        if self._paper:
            self._paper.place_order(signal, qty)
        elif self._order_mgr:
            if signal.signal == SignalEnum.BUY:
                self._order_mgr.place_buy(
                    strategy=signal.strategy,
                    code=signal.code,
                    name=signal.name,
                    quantity=qty,
                    price=signal.price,
                )
            else:
                self._order_mgr.place_sell(
                    strategy=signal.strategy,
                    code=signal.code,
                    name=signal.name,
                    quantity=qty,
                    price=signal.price,
                )

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
