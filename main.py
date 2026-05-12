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

from config.constants import INTRADAY_DYNAMIC_REFRESH_SEC
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
# S6 opening_range 스냅샷 최소 관측 윈도우: 워치 진입 후 N초가 지난 종목만
# 스냅샷 대상에 포함. 09:30 정시 스냅 외에 1분마다 추가 진입자에 대해 재시도.
_S6_ORH_MIN_WINDOW_SEC = 30 * 60

# S5 진입 시작 시각 (모의 공격형: 정규장 개장 직후)
_S5_ENTRY_TIME = "090000"

# S1 진입 시작 시각 (모의 공격형: 정규장 개장 직후)
_S1_ENTRY_TIME = "090000"


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

        # 일봉 지표 캐시 {code: {ma5, ma20, ma60, vol_ma20, prev_close, prev_day_high}}
        self._daily_indicators: dict[str, dict] = {}
        self._last_strategy_tick = 0.0  # time.monotonic() 마지막 평가 시각
        self._diag_tick_count = 0       # 진단 로그 주기 카운터 (10 tick = 5분)
        self._last_balance_sync = 0.0   # KIS 잔고 마지막 재동기화 시각
        self._day_start_eval = 0.0      # 오늘 시작 평가금액 (오늘 수익률 기준)
        self._day_start_realized = 0.0  # 오늘 시작 누적실현손익 (오늘 실현손익 기준)

        # ─── 시간대별 전략용 상태 ────────────────────────────────────
        # 단타 동적 워치리스트 (거래대금 상위 20). S1/S6/S7/S8 공유.
        # S5(섹터 멀티데이)는 별도 워치리스트 유지.
        self._intraday_watchlist: set[str] = set()
        self._last_dyn_refresh = 0.0
        # S6 opening_range_high (09:30 또는 워치 진입 +30분, 둘 중 늦은 쪽 기준)
        self._opening_range_high: dict[str, int] = {}
        self._opening_range_snapped_today: str = ""
        # 종목별 워치리스트 진입 시각 — late entrant의 ORH 스냅샷 지연용
        self._intraday_added_at: dict[str, datetime] = {}

        # 매매 빈도 제한
        self._last_sell_at: dict[str, datetime] = {}
        self._buy_count_today: dict[str, int] = {}
        # 전략별 종목당 매수 카운터: {(code, strategy): count}
        self._buy_count_today_strategy: dict[tuple[str, str], int] = {}
        self._total_buy_count_today: int = 0
        self._buy_limit_warned_today: str = ""
        self._buy_count_date: str = ""

        # 마감 sweep + S9 익일 청산
        self._closeout_sweep_done_today: str = ""
        self._s9_overnight_exit_done_today: str = ""
        # 일일 리포트 중복 발송 방지 (15:30 1회 + 마감 종료시 fallback)
        self._daily_report_sent_today: str = ""

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

        # 오늘 시작 평가금액 로드/저장 (today 수익률 기준점)
        self._initialize_day_start()

        # 단타 동적 워치리스트 초기화 (api 준비 후)
        from core.dynamic_watchlist import DynamicWatchlist
        from config.constants import INTRADAY_DYNAMIC_INITIAL_SLOTS
        self._dynamic_watchlist = DynamicWatchlist(
            api=self._api, data=self._data, max_slots=INTRADAY_DYNAMIC_INITIAL_SLOTS,
        )

        # 실시간 구독은 장전 준비(_run_premarket)에서 워치리스트만 체결가+호가
        # 동시 구독. 시작 시점엔 WS 미연결 상태로 둠 (KIS WebSocket 한도 절약).

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

                # 장 마감 후: 일일 리포트 1회 발송 + 정상 종료
                # (GHA 6h 타임아웃까지 무한 대기 방지 — 14:25 핸드오프 잡이
                #  20:30까지 도는 문제 차단)
                if time_str > "153500":
                    logger.info("[main] 장 마감 - 정상 종료")
                    if self._daily_report_sent_today != today_str:
                        self._send_daily_report()
                        self._daily_report_sent_today = today_str
                    self._running = False
                    break
                # 장 시작 전: 1분 슬립 후 다시 체크
                if time_str < "090000":
                    time.sleep(60)
                    continue

                # kill switch 확인
                if self._risk._is_kill_switch_active():
                    logger.critical("Kill switch 활성화 - 대기 중")
                    if self._order_mgr:
                        self._order_mgr.cancel_all()
                    time.sleep(30)
                    continue

                # 일일 매매 카운터 자정 리셋
                if self._buy_count_date != today_str:
                    self._buy_count_date = today_str
                    self._buy_count_today.clear()
                    self._buy_count_today_strategy.clear()
                    self._total_buy_count_today = 0
                    self._buy_limit_warned_today = ""

                # S9 보유 종목 익일 시초가 청산 (09:00~09:01 사이 1회)
                if (
                    not settings.is_paper
                    and self._order_mgr
                    and self._s9_overnight_exit_done_today != today_str
                    and "090000" <= time_str < "090200"
                ):
                    self._s9_overnight_exit_done_today = today_str
                    threading.Thread(
                        target=self._evaluate_s9_overnight_exit,
                        daemon=True,
                        name="s9-overnight-exit",
                    ).start()

                # 체결 동기화 (실거래)
                if self._order_mgr:
                    self._order_mgr.sync_filled_orders()

                # KIS 잔고 주기적 재동기화 (5분 간격)
                if (
                    not settings.is_paper
                    and time.monotonic() - self._last_balance_sync >= 300
                ):
                    self._last_balance_sync = time.monotonic()
                    threading.Thread(
                        target=self._sync_balance,
                        daemon=True,
                        name="balance-resync",
                    ).start()

                # 단타 동적 워치리스트 갱신 (5분 간격, 09:00~14:30)
                # S1/S6/S7/S8 공유 — 거래대금 상위 20종목
                if (
                    "090000" <= time_str < "143000"
                    and time.monotonic() - self._last_dyn_refresh >= INTRADAY_DYNAMIC_REFRESH_SEC
                ):
                    self._last_dyn_refresh = time.monotonic()
                    threading.Thread(
                        target=self._refresh_intraday_watchlist,
                        daemon=True,
                        name="intraday-dyn-watchlist",
                    ).start()

                # opening_range_high 스냅샷 (S6용).
                # 09:30 이후 매 tick에서 호출하되, 종목별로 워치 진입 +30분이
                # 지난 종목만 스냅. 한 번 잡힌 ORH는 갱신하지 않음.
                if time_str >= "093000":
                    self._snap_opening_range_high(today_str)

                # 전략 평가 (시간대별 라우팅, _STRATEGY_TICK_SEC 간격)
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
                        if (
                            settings.STRATEGY_S6_ENABLED
                            and "093000" <= time_str < "110000"
                        ):
                            self._evaluate_s6_watchlist(time_str)
                        if (
                            settings.STRATEGY_S7_ENABLED
                            and "100000" <= time_str < "140000"
                        ):
                            self._evaluate_s7_watchlist(time_str)
                        if (
                            settings.STRATEGY_S8_ENABLED
                            and "130000" <= time_str < "143000"
                        ):
                            self._evaluate_s8_watchlist(time_str)
                        if (
                            settings.STRATEGY_S9_ENABLED
                            and "143000" <= time_str < "151500"
                        ):
                            self._evaluate_s9_watchlist(time_str, today_str)

                        # 5분마다 진단 로그
                        self._diag_tick_count += 1
                        if self._diag_tick_count % 10 == 0:
                            self._log_eval_diagnostic(time_str)

                # 마감 안전망 sweep (15:25~)
                if (
                    "152500" <= time_str < "152600"
                    and self._closeout_sweep_done_today != today_str
                ):
                    self._closeout_sweep_done_today = today_str
                    threading.Thread(
                        target=self._closeout_sweep,
                        daemon=True,
                        name="closeout-sweep",
                    ).start()

                # 장 마감 전 일일 리포트 (15:30 1회만)
                if (
                    "153000" <= time_str <= "153100"
                    and self._daily_report_sent_today != today_str
                ):
                    self._send_daily_report()
                    self._daily_report_sent_today = today_str

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
            # 1. PortfolioManager에 포지션/현금 복원 (재시작 후 이어받기)
            self._portfolio.sync_from_balance(
                cash=balance.cash,
                positions=balance.positions,
            )
            # 2. RiskManager도 동기화
            self._risk.update_portfolio_state(
                total_eval=balance.total_eval,
                cash=balance.cash,
                positions={p.code: p.eval_amount for p in balance.positions},
            )
            logger.info(
                f"잔고 동기화 | 총 평가={balance.total_eval:,.0f}원 "
                f"| 현금={balance.cash:,.0f}원 "
                f"| 포지션={len(balance.positions)}개"
            )
            for p in balance.positions:
                # KIS evlu_pfls_rt 는 이미 %단위(0.84 = 0.84%) 라서
                # f-string의 :% (×100)을 사용하지 않고 :+.2f 로 그대로 표시
                logger.info(
                    f"  보유: {p.name}({p.code}) {p.quantity}주 "
                    f"@평단{p.avg_price:,.0f}원 (현재 {p.current_price:,}원, "
                    f"손익 {p.profit_loss_rate:+.2f}%)"
                )
        except Exception as exc:
            logger.error(f"잔고 동기화 실패: {exc}")

    def _run_premarket(self) -> None:
        """S5 장전 뉴스 수집 → 섹터 점수 → 대장주 선정 → WebSocket 구독 업데이트."""
        try:
            logger.info("[main] S5 장전 준비 시작")
            watchlist = self._strategy.prepare_premarket()
            if watchlist:
                # 워치리스트 종목만 체결가 + 호가 동시 구독
                # (KIS WebSocket 한도 절약 + S1 매수세 필터 활성화)
                self._api.subscribe_realtime(
                    codes=watchlist,
                    on_price=self._on_realtime_price,
                    on_orderbook=self._on_realtime_orderbook,
                )
                logger.info(
                    "[main] 워치리스트 WebSocket 구독 (체결가+호가): %s", watchlist
                )
                # 일봉 지표 프리로드 (ma5, ma20, vol_ma20)
                self._preload_daily_indicators(watchlist)
            # 장전 준비 완료는 로컬 로그만 (슬랙 푸시 X - 노이즈 줄임)
            logger.info("[main] S5 장전 준비 완료 | %d종목 선정", len(watchlist))
        except Exception as exc:
            logger.error("[main] S5 장전 준비 오류: %s", exc, exc_info=True)

    def _on_realtime_orderbook(self, code: str, data: dict) -> None:
        """WebSocket 호가 콜백 → MarketDataCollector 캐시 갱신."""
        self._data.on_orderbook(code, data)

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
                    "ma60": float(last.get("ma60", 0) or 0),
                    "vol_ma20": float(last.get("vol_ma20", 0) or 0),
                    "prev_close": int(last.get("close", 0) or 0),
                    "prev_day_high": int(last.get("high", 0) or 0),
                }
                logger.info(
                    "[main] %s 지표 로드: ma5=%.0f, ma20=%.0f, ma60=%.0f, "
                    "vol_ma20=%.0f, prev_close=%d, prev_high=%d",
                    code,
                    self._daily_indicators[code]["ma5"],
                    self._daily_indicators[code]["ma20"],
                    self._daily_indicators[code]["ma60"],
                    self._daily_indicators[code]["vol_ma20"],
                    self._daily_indicators[code]["prev_close"],
                    self._daily_indicators[code]["prev_day_high"],
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

    def _intraday_candidates(self) -> set[str]:
        """S1/S6/S7/S8 평가 후보: 거래대금 상위 20 단타 워치리스트."""
        return set(self._intraday_watchlist)

    def _evaluate_s1_watchlist(self, time_str: str) -> None:
        """S1 평가. 단타 동적 워치리스트(거래대금 상위 20)."""
        watchlist = list(self._intraday_watchlist)
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
                # 호가 캐시에서 읽어옴. 미수신 시 0(자동 PASS) 폴백.
                ob_cache = self._data.get_orderbook(code)
                ob = {
                    "ask1": ob_cache.get("ask1", 0),
                    "bid1": ob_cache.get("bid1", 0),
                    "bid_qty_sum": ob_cache.get("bid_qty_sum", 0),
                    "ask_qty_sum": ob_cache.get("ask_qty_sum", 0),
                }

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

    # ─── S6/S7/S8/S9 평가 ──────────────────────────────────────────

    def _eval_state(self, code: str):
        """공통 state 빌더 (pos, indicators, today_bar)."""
        pos = self._portfolio.positions.get(code)
        indicators = self._daily_indicators.get(code, {})
        today_bar = self._data.get_today_ohlcv(code) or {}
        return pos, indicators, today_bar

    def _make_state(self, pos) -> dict:
        return {
            "position_qty": pos.quantity if pos else 0,
            "avg_price": pos.avg_price if pos else 0.0,
            "cash": self._portfolio.cash,
        }

    def _evaluate_s6_watchlist(self, time_str: str) -> None:
        """S6 돌파매매 평가."""
        for code in self._intraday_candidates():
            try:
                price = self._data.get_last_price(code)
                if not price:
                    continue
                pos, indicators, _ = self._eval_state(code)
                signal = self._strategy.evaluate_s6(
                    code=code,
                    name=pos.name if pos else code,
                    current_price=price,
                    prev_day_high=indicators.get("prev_day_high", 0),
                    opening_range_high=self._opening_range_high.get(code, 0),
                    ma20=indicators.get("ma20", 0),
                    vol_ratio=self._compute_vol_ratio(code, indicators),
                    three_min_up_ratio=self._compute_up_ratio(code, n=5),
                    current_time=time_str,
                    position_qty=pos.quantity if pos else 0,
                    avg_entry_price=pos.avg_price if pos else 0.0,
                )
                if signal:
                    self._handle_signal(signal, self._make_state(pos))
            except Exception as exc:
                logger.error("[main] %s S6 평가 오류: %s", code, exc, exc_info=True)

    def _evaluate_s7_watchlist(self, time_str: str) -> None:
        """S7 눌림목 평가."""
        for code in self._intraday_candidates():
            try:
                price = self._data.get_last_price(code)
                if not price:
                    continue
                pos, indicators, today_bar = self._eval_state(code)
                signal = self._strategy.evaluate_s7(
                    code=code,
                    name=pos.name if pos else code,
                    current_price=price,
                    today_open=int(today_bar.get("open", price) or price),
                    today_high=int(today_bar.get("high", price) or price),
                    ma20=indicators.get("ma20", 0),
                    ma60=indicators.get("ma60", 0),
                    vol_ratio=self._compute_vol_ratio(code, indicators),
                    three_min_up_ratio=self._compute_up_ratio(code, n=3),
                    current_time=time_str,
                    position_qty=pos.quantity if pos else 0,
                    avg_entry_price=pos.avg_price if pos else 0.0,
                    now_dt=datetime.now(),
                )
                if signal:
                    self._handle_signal(signal, self._make_state(pos))
            except Exception as exc:
                logger.error("[main] %s S7 평가 오류: %s", code, exc, exc_info=True)

    def _evaluate_s8_watchlist(self, time_str: str) -> None:
        """S8 오후 추세 평가."""
        for code in self._intraday_candidates():
            try:
                price = self._data.get_last_price(code)
                if not price:
                    continue
                pos, indicators, _ = self._eval_state(code)
                signal = self._strategy.evaluate_s8(
                    code=code,
                    name=pos.name if pos else code,
                    current_price=price,
                    prev_close=indicators.get("prev_close", 0),
                    ma5=indicators.get("ma5", 0),
                    vol_ratio=self._compute_vol_ratio(code, indicators),
                    three_min_up_ratio=self._compute_up_ratio(code, n=5),
                    current_time=time_str,
                    position_qty=pos.quantity if pos else 0,
                    avg_entry_price=pos.avg_price if pos else 0.0,
                )
                if signal:
                    self._handle_signal(signal, self._make_state(pos))
            except Exception as exc:
                logger.error("[main] %s S8 평가 오류: %s", code, exc, exc_info=True)

    def _evaluate_s9_watchlist(self, time_str: str, today_str: str) -> None:
        """S9 종가베팅 평가 (진입만)."""
        for code in self._intraday_candidates():
            try:
                price = self._data.get_last_price(code)
                if not price:
                    continue
                pos, indicators, _ = self._eval_state(code)
                signal = self._strategy.evaluate_s9(
                    code=code,
                    name=pos.name if pos else code,
                    current_price=price,
                    prev_close=indicators.get("prev_close", 0),
                    ma20=indicators.get("ma20", 0),
                    vol_ratio=self._compute_vol_ratio(code, indicators),
                    current_time=time_str,
                    position_qty=pos.quantity if pos else 0,
                    today_str=today_str,
                )
                if signal:
                    self._handle_signal(signal, self._make_state(pos))
            except Exception as exc:
                logger.error("[main] %s S9 평가 오류: %s", code, exc, exc_info=True)

    def _evaluate_s9_overnight_exit(self) -> None:
        """익일 09:00에 S9 strategy 마킹 보유분을 시장가 일괄 매도."""
        if not self._order_mgr:
            return
        for code, pos in list(self._portfolio.positions.items()):
            if pos.strategy != "S9" or pos.quantity <= 0:
                continue
            logger.info("[S9] 익일 시초가 청산: %s %d주", code, pos.quantity)
            self._order_mgr.place_sell(
                strategy="S9",
                code=code,
                name=pos.name,
                quantity=pos.quantity,
                price=0,
                cancel_reason="S9 익일 시초가",
                is_forced_close=True,
            )
            self._notifier.send(
                f"매도 주문 | {pos.name}({code}) {pos.quantity}주 시장가 [S9] - 익일 청산",
                level="TRADE",
            )

    def _closeout_sweep(self) -> None:
        """15:25 단타 잔여 포지션 시장가 강제 청산 (안전망)."""
        if not self._order_mgr:
            return
        intraday_strats = {"S1", "S6", "S7", "S8"}
        for code, pos in list(self._portfolio.positions.items()):
            if pos.strategy not in intraday_strats or pos.quantity <= 0:
                continue
            logger.warning(
                "[main] 15:25 sweep: %s %s %d주 시장가 청산",
                pos.strategy, code, pos.quantity,
            )
            self._order_mgr.place_sell(
                strategy=pos.strategy,
                code=code,
                name=pos.name,
                quantity=pos.quantity,
                price=0,
                cancel_reason=f"{pos.strategy} 마감 sweep",
                is_forced_close=True,
            )
            self._notifier.send(
                f"매도 주문 | {pos.name}({code}) {pos.quantity}주 시장가 [{pos.strategy}] - 마감 sweep",
                level="TRADE",
            )

    # ─── 단타 동적 워치리스트 + opening range ────────────────────

    def _refresh_intraday_watchlist(self) -> None:
        """5분마다 KIS volume-rank API 폴링 → 거래대금 상위 20종목 워치리스트 갱신.
        S1/S6/S7/S8 모두 이 워치리스트를 공유."""
        try:
            selected = self._dynamic_watchlist.refresh()
            new_codes = {s["code"] for s in selected}
        except Exception as exc:
            logger.warning("[main] 동적 워치리스트 갱신 오류: %s", exc)
            return

        added = new_codes - self._intraday_watchlist
        removed = self._intraday_watchlist - new_codes

        # 보유 중인 종목은 제거 보류 (청산 추적 위해)
        held_codes = {
            c for c, p in self._portfolio.positions.items() if p.quantity > 0
        }
        removed = removed - held_codes

        self._intraday_watchlist = (self._intraday_watchlist | added) - removed

        now = datetime.now()
        for c in added:
            self._intraday_added_at[c] = now
        for c in removed:
            self._intraday_added_at.pop(c, None)
            self._opening_range_high.pop(c, None)

        if added:
            logger.info("[단타 동적] 신규 추가 %d종목: %s", len(added), sorted(added))
            self._preload_daily_indicators(list(added))
            try:
                self._api.subscribe_realtime(
                    codes=list(added),
                    on_price=self._on_realtime_price,
                    on_orderbook=self._on_realtime_orderbook,
                )
            except Exception as exc:
                logger.warning("[main] 단타 동적 종목 구독 실패: %s", exc)
        if removed:
            logger.info("[단타 동적] 제거 %d종목: %s", len(removed), sorted(removed))

    def _snap_opening_range_high(self, today_str: str) -> None:
        """워치리스트 진입 후 _S6_ORH_MIN_WINDOW_SEC가 지난 종목에 대해
        today_high를 스냅샷(한 번만). 09:30 첫 호출 후에도 매 tick 재호출되며
        뒤늦게 진입한 종목은 충분한 관측 윈도우가 쌓이면 그 시점에 스냅됨."""
        codes = self._intraday_candidates()
        if not codes:
            return
        now = datetime.now()
        newly_snapped: list[tuple[str, int]] = []
        for code in codes:
            if code in self._opening_range_high:
                continue  # 이미 스냅됨
            added_at = self._intraday_added_at.get(code)
            # added_at 미상(=어제부터 보유 등) → 09:30부터 즉시 가능 / 신규는 +30분 대기
            if added_at is not None:
                elapsed = (now - added_at).total_seconds()
                if elapsed < _S6_ORH_MIN_WINDOW_SEC:
                    continue
            today_bar = self._data.get_today_ohlcv(code) or {}
            high = int(today_bar.get("high", 0) or 0)
            if high > 0:
                self._opening_range_high[code] = high
                newly_snapped.append((code, high))
        if newly_snapped:
            self._opening_range_snapped_today = today_str
            logger.info(
                "[main] opening_range_high 스냅샷 +%d종목: %s (누계 %d)",
                len(newly_snapped),
                {c: f"{h:,}" for c, h in newly_snapped[:5]},
                len(self._opening_range_high),
            )

    def _compute_vol_ratio(self, code: str, indicators: dict) -> float:
        """today_volume / vol_ma20 계산 (장중 누적, 시간 비례 보정 X)."""
        today_bar = self._data.get_today_ohlcv(code) or {}
        vol_ma20 = float(indicators.get("vol_ma20", 0) or 0)
        today_volume = float(today_bar.get("volume", 0) or 0)
        return today_volume / vol_ma20 if vol_ma20 > 0 else 0.0

    def _compute_up_ratio(self, code: str, n: int = 5) -> float:
        """최근 N개 3분봉 우상향 비율."""
        from core.market_data_collector import MarketDataCollector
        bars = self._data.get_three_min_bars(code, n=n)
        return MarketDataCollector.calc_three_min_up_ratio(bars)

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

    # ─── 전략별 position_ratio 룩업 ─────────────────────────────────
    @staticmethod
    def _strategy_position_ratio(strategy: str) -> float:
        """전략별 1회 매수 비중 (가용 현금 대비)."""
        from config.constants import (
            S1_PARAMS, S5_PARAMS, S6_PARAMS, S7_PARAMS, S8_PARAMS, S9_PARAMS,
        )
        params_map = {
            "S1": S1_PARAMS, "S5": S5_PARAMS, "S6": S6_PARAMS,
            "S7": S7_PARAMS, "S8": S8_PARAMS, "S9": S9_PARAMS,
        }
        p = params_map.get(strategy, {})
        return float(p.get("position_ratio", 0.20))

    def _handle_signal(self, signal, state: dict) -> None:
        """전략 시그널 → 주문 발주 (paper / live mock).

        매수 가드 5중:
          1. 우선순위 락 (다른 전략이 같은 종목 보유 중이면 스킵)
          2. 30분 재매수 쿨다운
          3. 종목당 일일 매수 한도 (3회)
          4. 일일 전체 매수 한도 (20회)
          5. 가용 현금 + 1% 버퍼 체크
        매수 시 _portfolio.cash 즉시 차감(낙관적 reservation).
        """
        from config.constants import (
            MAX_BUYS_PER_CODE_PER_DAY,
            MAX_BUYS_PER_CODE_PER_STRATEGY,
            MAX_TOTAL_BUYS_PER_DAY,
            REBUY_COOLDOWN_SEC,
        )
        from strategies.base_strategy import Signal as SignalEnum

        if signal.signal == SignalEnum.BUY:
            # ── 1. 동일 종목 락 (전략 무관하게 1종목 1전략만) ─────────
            # 다른 전략이 이미 보유 중이면 우선순위 비교 없이 스킵.
            # (포지션 분리 트래킹이 없는 현재 구조에서 청산 책임이 모호해지는 것을 막음)
            existing = self._portfolio.positions.get(signal.code)
            if existing is not None and existing.quantity > 0:
                if existing.strategy != signal.strategy:
                    logger.info(
                        "[main] %s %s 보유중 → %s 매수 스킵 (cross-strategy 락)",
                        signal.code, existing.strategy, signal.strategy,
                    )
                    return
            # 미체결 매수 주문이 있으면 동일 코드 재진입 차단 (티어 동시발사 방지)
            if self._order_mgr is not None:
                if self._order_mgr.pending_buy_qty(signal.code) > 0:
                    logger.info(
                        "[main] %s 미체결 매수 존재 → %s 매수 스킵",
                        signal.code, signal.strategy,
                    )
                    return

            # ── 2. 재매수 쿨다운 ─────────────────────────────────────
            last_sell = self._last_sell_at.get(signal.code)
            if last_sell is not None:
                elapsed = (datetime.now() - last_sell).total_seconds()
                if elapsed < REBUY_COOLDOWN_SEC:
                    logger.info(
                        "[main] %s 매도 후 %.0fs 미경과 (쿨다운 %ds) → %s 매수 스킵",
                        signal.code, elapsed, REBUY_COOLDOWN_SEC, signal.strategy,
                    )
                    return

            # ── 3. 종목당 일일 매수 한도 ─────────────────────────────
            # 3-a. 전략별 한도 (S5/S9는 종목당 1회 등)
            strat_limit = MAX_BUYS_PER_CODE_PER_STRATEGY.get(
                signal.strategy, MAX_BUYS_PER_CODE_PER_DAY,
            )
            strat_key = (signal.code, signal.strategy)
            cnt_for_strat = self._buy_count_today_strategy.get(strat_key, 0)
            if cnt_for_strat >= strat_limit:
                logger.info(
                    "[main] %s %s 전략 종목당 한도(%d/%d) 도달 → 매수 스킵",
                    signal.code, signal.strategy, cnt_for_strat, strat_limit,
                )
                return
            # 3-b. 종목 전체 한도 (모든 전략 합산)
            cnt_for_code = self._buy_count_today.get(signal.code, 0)
            if cnt_for_code >= MAX_BUYS_PER_CODE_PER_DAY:
                logger.info(
                    "[main] %s 일일 종목 매수 한도(%d/%d) 도달 → %s 매수 스킵",
                    signal.code, cnt_for_code, MAX_BUYS_PER_CODE_PER_DAY,
                    signal.strategy,
                )
                return

            # ── 4. 일일 전체 매수 한도 ───────────────────────────────
            if self._total_buy_count_today >= MAX_TOTAL_BUYS_PER_DAY:
                today_str = datetime.now().strftime("%Y%m%d")
                if self._buy_limit_warned_today != today_str:
                    self._buy_limit_warned_today = today_str
                    self._notifier.send(
                        f"⚠️ 일일 매수 한도 도달 ({MAX_TOTAL_BUYS_PER_DAY}회) - 추가 매수 스킵",
                        level="WARN",
                    )
                logger.info(
                    "[main] 일일 전체 매수 한도(%d) 도달 → %s 매수 스킵",
                    MAX_TOTAL_BUYS_PER_DAY, signal.strategy,
                )
                return

            # ── 5. 가용 현금 검증 + 비중 계산 ────────────────────────
            available = self._portfolio.cash
            ratio = self._strategy_position_ratio(signal.strategy)
            budget = min(state["cash"] * ratio, available)
            qty = int(budget // signal.price)
            required = qty * signal.price * 1.01  # 1% 수수료/슬리피지 버퍼
            if qty <= 0 or required > available:
                logger.warning(
                    "[main] %s 매수 스킵 (가용=%d, 필요=%d, 전략=%s 비중=%.2f)",
                    signal.code, int(available), int(required),
                    signal.strategy, ratio,
                )
                return

            # 가용현금 즉시 차감 + 매수 카운터 증가
            self._portfolio._cash -= required
            self._buy_count_today[signal.code] = cnt_for_code + 1
            self._buy_count_today_strategy[strat_key] = cnt_for_strat + 1
            self._total_buy_count_today += 1
        else:
            # 매도: 시그널의 quantity 우선, 없으면 보유 × sell_ratio
            qty = signal.quantity or int(
                state["position_qty"] * (signal.sell_ratio or 1.0)
            )
            if qty <= 0:
                return
            # 미체결 매도 주문이 있으면 잔여 보유로 한도 캡 (KIS 40240000 회피)
            if self._order_mgr is not None:
                pending = self._order_mgr.pending_sell_qty(signal.code)
                if pending > 0:
                    available_qty = max(0, state["position_qty"] - pending)
                    if available_qty <= 0:
                        logger.info(
                            "[main] %s 매도 스킵: 미체결 매도 %d주 / 보유 %d주",
                            signal.code, pending, state["position_qty"],
                        )
                        return
                    if qty > available_qty:
                        logger.info(
                            "[main] %s 매도 수량 캡 %d→%d (미체결 %d주)",
                            signal.code, qty, available_qty, pending,
                        )
                        qty = available_qty
            # 매도 시각 기록 (재매수 쿨다운용)
            self._last_sell_at[signal.code] = datetime.now()

        side_str = "매수" if signal.signal == SignalEnum.BUY else "매도"

        if self._paper:
            paper_order = self._paper.place_order(signal, qty)
            if paper_order:
                self._notifier.send(
                    f"{side_str} 주문 | {signal.name}({signal.code}) "
                    f"{qty}주 @{signal.price:,}원 [{signal.strategy}]",
                    level="TRADE",
                )
        elif self._order_mgr:
            if signal.signal == SignalEnum.BUY:
                order = self._order_mgr.place_buy(
                    strategy=signal.strategy,
                    code=signal.code,
                    name=signal.name,
                    quantity=qty,
                    price=signal.price,
                )
            else:
                # 강제청산/장마감/손절/시장가 키워드 포함 시 시장가 발주.
                # 지정가로 나가면 mock 호가 부족·체결 실패→타임아웃 취소→재발사
                # 무한루프 위험이 있어 청산 의도 시그널은 무조건 시장가로 정리.
                reason = signal.reason or ""
                forced = any(
                    kw in reason
                    for kw in ("마감", "시장가", "강제청산", "청산", "손절", "급등과열")
                )
                order = self._order_mgr.place_sell(
                    strategy=signal.strategy,
                    code=signal.code,
                    name=signal.name,
                    quantity=qty,
                    price=signal.price,
                    is_forced_close=forced,
                )
            # 발주 성공 시 즉시 슬랙 (체결 콜백 notify_trade는 별도로 발송)
            if order is not None:
                self._notifier.send(
                    f"{side_str} 주문 | {signal.name}({signal.code}) "
                    f"{qty}주 @{signal.price:,}원 [{signal.strategy}] - {signal.reason}",
                    level="TRADE",
                )
            # 매도 주문 실패 시 KIS 잔고와 어긋났을 가능성 → 즉시 재동기화
            # (대표 사례: KIS 40240000 "모의투자 잔고내역이 없습니다")
            if order is None and signal.signal == SignalEnum.SELL:
                logger.warning(
                    "[main] 매도 주문 실패 → KIS 잔고 재동기화 트리거"
                )
                self._last_balance_sync = time.monotonic()
                threading.Thread(
                    target=self._sync_balance,
                    daemon=True,
                    name="balance-resync-on-fail",
                ).start()

    def _get_watch_list(self) -> list[str]:
        """감시 종목 목록. S5 워치리스트는 장전 스레드에서 동적 추가."""
        # S5 섹터 UNIVERSE 전체 종목 초기 구독 (장전 준비 전 임시)
        if settings.STRATEGY_S5_ENABLED:
            return self._strategy.sector_manager.get_watchlist()
        return []

    def _initialize_day_start(self) -> None:
        """오늘 시작 평가금액/실현손익 기준 로드 또는 신규 저장.
        14:25 핸드오프 잡에서도 같은 날짜면 같은 기준값 유지."""
        import json as _json
        today_str = datetime.now().strftime("%Y%m%d")
        cache_path = settings.DATA_DIR / "day_start_eval.json"
        try:
            if cache_path.exists():
                data = _json.loads(cache_path.read_text(encoding="utf-8"))
                if data.get("date") == today_str:
                    self._day_start_eval = float(data["eval"])
                    self._day_start_realized = float(data.get("realized", 0.0))
                    logger.info(
                        "[main] 오늘 시작 평가금액 로드: %.0f원 (실현 %+.0f)",
                        self._day_start_eval, self._day_start_realized,
                    )
                    return
        except Exception as exc:
            logger.warning(f"[main] day_start 로드 실패: {exc}")

        # 신규 저장 (날짜 바뀜 또는 첫 실행)
        self._day_start_eval = float(self._portfolio.total_eval)
        self._day_start_realized = float(
            self._portfolio.get_summary().get("total_realized_pnl", 0.0)
        )
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                _json.dumps({
                    "date": today_str,
                    "eval": self._day_start_eval,
                    "realized": self._day_start_realized,
                }),
                encoding="utf-8",
            )
            logger.info(
                "[main] 오늘 시작 기준 저장: 평가=%.0f원, 실현=%+.0f원",
                self._day_start_eval, self._day_start_realized,
            )
        except Exception as exc:
            logger.warning(f"[main] day_start 저장 실패: {exc}")

    def _send_daily_report(self) -> None:
        summary = self._portfolio.get_summary()

        # 오늘 손익 (시작 평가금액 대비 = 실현 + 미실현 합)
        if self._day_start_eval > 0:
            today_pnl = summary["total_eval"] - self._day_start_eval
            summary["today_pnl"] = today_pnl
            summary["today_return_rate"] = today_pnl / self._day_start_eval
            summary["day_start_eval"] = self._day_start_eval
        else:
            summary["today_pnl"] = 0.0
            summary["today_return_rate"] = 0.0
            summary["day_start_eval"] = 0.0

        # 오늘 실현손익 = 누적실현 - 오늘 시작 누적실현
        today_realized = (
            summary.get("total_realized_pnl", 0.0) - self._day_start_realized
        )
        summary["today_realized_pnl"] = today_realized
        if self._day_start_eval > 0:
            summary["today_realized_rate"] = today_realized / self._day_start_eval
            summary["today_unrealized_rate"] = (
                summary.get("total_unrealized_pnl", 0.0) / self._day_start_eval
            )
        else:
            summary["today_realized_rate"] = 0.0
            summary["today_unrealized_rate"] = 0.0

        # 보유 포지션 상세 (미실현)
        summary["positions_detail"] = [
            {
                "code": p.code,
                "name": p.name,
                "strategy": p.strategy,
                "quantity": p.quantity,
                "avg_price": p.avg_price,
                "current_price": p.current_price,
                "unrealized_pnl": p.unrealized_pnl,
                "pnl_rate": (
                    (p.current_price - p.avg_price) / p.avg_price
                    if p.avg_price > 0 else 0.0
                ),
            }
            for p in self._portfolio.positions.values()
        ]

        # 오늘 청산된 거래 (실현손익 포함). _trade_history는 in-memory.
        today_start_dt = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        today_trades = self._portfolio.get_trade_history(since=today_start_dt)
        sells = [t for t in today_trades if t.side == "SELL"]
        wins = sum(1 for t in sells if t.realized_pnl > 0)
        losses = sum(1 for t in sells if t.realized_pnl < 0)
        summary["today_realized_trades"] = [
            {
                "time": t.traded_at.strftime("%H:%M"),
                "code": t.code,
                "name": t.name,
                "strategy": t.strategy,
                "qty": t.quantity,
                "price": t.price,
                "realized_pnl": t.realized_pnl,
                "realized_pnl_rate": t.realized_pnl_rate,
            }
            for t in sells
        ]
        summary["today_buys_count"] = sum(
            1 for t in today_trades if t.side == "BUY"
        )
        summary["today_wins"] = wins
        summary["today_losses"] = losses

        # 보유 비중 (현금/주식)
        total = summary.get("total_eval", 0.0) or 1.0
        summary["cash_ratio"] = summary.get("cash", 0.0) / total
        summary["stock_ratio"] = summary.get("invested", 0.0) / total

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
