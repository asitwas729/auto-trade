"""
시장 데이터 수집기
- OHLCV 일봉/분봉 저장 (Parquet)
- 실시간 틱 캐싱
- 기술지표 계산 (ATR, RSI, MACD, MA, 볼린저밴드)
- 갭 상승률, 시간외 등락률
- 시세 지연 감지
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from config.settings import settings

logger = logging.getLogger(__name__)

OHLCV_DIR = settings.DATA_DIR / "ohlcv"
REALTIME_DIR = settings.DATA_DIR / "realtime"
PRICE_STALE_THRESHOLD_SEC = 10   # 10초 이상 미수신 시 stale


class MarketDataCollector:

    def __init__(self) -> None:
        self._lock = threading.RLock()

        # 실시간 틱 캐시 {code: deque(최대 1000틱)}
        self._ticks: dict[str, deque[dict]] = defaultdict(lambda: deque(maxlen=1000))

        # 분봉 캐시 {code: list[dict]}  (당일 분봉)
        self._minute_bars: dict[str, list[dict]] = defaultdict(list)

        # 가장 최근 가격 {code: (price, timestamp)}
        self._last_prices: dict[str, tuple[int, datetime]] = {}

        # 당일 OHLCV {code: dict}
        self._daily_bar: dict[str, dict] = {}

        # 호가 캐시 {code: {"ask1", "bid1", "ask_qty_sum", "bid_qty_sum", "ts"}}
        self._orderbook: dict[str, dict] = {}

        OHLCV_DIR.mkdir(parents=True, exist_ok=True)
        REALTIME_DIR.mkdir(parents=True, exist_ok=True)

        logger.info("MarketDataCollector 초기화")

    # ─────────────────────────────────────────────────────────────
    # 실시간 가격 수신 (WebSocket 콜백)
    # ─────────────────────────────────────────────────────────────

    def on_price_tick(self, code: str, data: dict) -> None:
        price = data["current_price"]
        ts = datetime.now()

        with self._lock:
            self._ticks[code].append({**data, "recv_at": ts})
            self._last_prices[code] = (price, ts)

            # 당일 바 갱신
            if code not in self._daily_bar:
                self._daily_bar[code] = {
                    "open": price, "high": price, "low": price,
                    "close": price, "volume": 0, "amount": 0,
                }
            bar = self._daily_bar[code]
            bar["high"] = max(bar["high"], price)
            bar["low"] = min(bar["low"], price)
            bar["close"] = price
            bar["volume"] = data.get("total_volume", bar["volume"])

    def get_last_price(self, code: str) -> Optional[int]:
        with self._lock:
            entry = self._last_prices.get(code)
            return entry[0] if entry else None

    def is_price_stale(self, code: str) -> bool:
        """마지막 수신 이후 PRICE_STALE_THRESHOLD_SEC 초 초과 여부"""
        with self._lock:
            entry = self._last_prices.get(code)
            if not entry:
                return True
            _, ts = entry
            return (datetime.now() - ts).total_seconds() > PRICE_STALE_THRESHOLD_SEC

    def get_today_ohlcv(self, code: str) -> Optional[dict]:
        with self._lock:
            return self._daily_bar.get(code)

    def on_orderbook(self, code: str, data: dict) -> None:
        """WebSocket 호가 콜백."""
        with self._lock:
            self._orderbook[code] = {
                "ask1": data.get("ask1", 0),
                "bid1": data.get("bid1", 0),
                "ask_qty_sum": data.get("ask_qty_sum", 0),
                "bid_qty_sum": data.get("bid_qty_sum", 0),
                "ts": datetime.now(),
            }

    def get_orderbook(self, code: str) -> dict:
        """현재 호가 캐시 반환. 없으면 빈 dict."""
        with self._lock:
            return dict(self._orderbook.get(code, {}))

    # ─────────────────────────────────────────────────────────────
    # 일봉 데이터 저장/로드
    # ─────────────────────────────────────────────────────────────

    def save_daily_ohlcv(self, code: str, df: pd.DataFrame) -> None:
        path = OHLCV_DIR / f"{code}_daily.parquet"
        if path.exists():
            existing = pd.read_parquet(path)
            df = pd.concat([existing, df]).drop_duplicates("date").sort_values("date")
        df.to_parquet(path, index=False)
        logger.debug(f"일봉 저장: {code} ({len(df)}행)")

    def load_daily_ohlcv(self, code: str, days: int = 120) -> Optional[pd.DataFrame]:
        path = OHLCV_DIR / f"{code}_daily.parquet"
        if not path.exists():
            return None
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").tail(days).reset_index(drop=True)
        return df

    # ─────────────────────────────────────────────────────────────
    # 기술지표 계산
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
        """
        입력: date, open, high, low, close, volume 컬럼
        출력: 기술지표 컬럼 추가된 DataFrame
        """
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        # 이동평균
        df["ma5"] = close.rolling(5).mean()
        df["ma20"] = close.rolling(20).mean()
        df["ma60"] = close.rolling(60).mean()

        # RSI (14)
        df["rsi14"] = MarketDataCollector._rsi(close, 14)

        # ATR (14)
        df["atr14"] = MarketDataCollector._atr(high, low, close, 14)
        df["atr_rate"] = df["atr14"] / close

        # MACD (12, 26, 9)
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        df["macd"] = ema12 - ema26
        df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
        df["macd_hist"] = df["macd"] - df["macd_signal"]

        # 볼린저밴드 (20, 2σ)
        df["bb_mid"] = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        df["bb_upper"] = df["bb_mid"] + 2 * bb_std
        df["bb_lower"] = df["bb_mid"] - 2 * bb_std
        df["bb_pct"] = (close - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])

        # 거래량 이동평균
        df["vol_ma20"] = volume.rolling(20).mean()
        df["vol_ratio"] = volume / df["vol_ma20"]

        # 갭 상승률 (당일 시가 vs 전일 종가)
        prev_close = close.shift(1)
        df["gap_rate"] = (df["open"] - prev_close) / prev_close

        # 당일 저점 이후 반등 여부 (단순 계산: close vs low)
        df["bounce_from_low"] = (close - low) / (high - low + 1e-9)

        return df

    @staticmethod
    def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / (loss + 1e-9)
        return 100 - 100 / (1 + rs)

    @staticmethod
    def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    # ─────────────────────────────────────────────────────────────
    # 갭/시간외 계산
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def calc_gap_rate(today_open: int, prev_close: int) -> float:
        if prev_close == 0:
            return 0.0
        return (today_open - prev_close) / prev_close

    @staticmethod
    def calc_after_hours_rate(after_hours_price: int, regular_close: int) -> float:
        if regular_close == 0:
            return 0.0
        return (after_hours_price - regular_close) / regular_close

    # ─────────────────────────────────────────────────────────────
    # 시장 전체 지표 (S4용)
    # ─────────────────────────────────────────────────────────────

    def calc_market_breadth(
        self,
        advance_count: int,
        decline_count: int,
    ) -> float:
        """상승/하락 종목 비율"""
        total = advance_count + decline_count
        if total == 0:
            return 0.5
        return advance_count / total

    def get_recent_ticks(self, code: str, n: int = 10) -> list[dict]:
        with self._lock:
            return list(self._ticks[code])[-n:]

    # ─────────────────────────────────────────────────────────────
    # 3분봉 (S5 전략용)
    # ─────────────────────────────────────────────────────────────

    def get_three_min_bars(self, code: str, n: int = 5) -> list[dict]:
        """
        실시간 틱 데이터를 3분 단위로 집계하여 최근 n개 바 반환.

        반환 형식:
            [{"open": int, "high": int, "low": int, "close": int,
              "volume": int, "bar_time": str "HH:MM"}, ...]
        최신 바가 마지막 요소.
        """
        with self._lock:
            ticks = list(self._ticks.get(code, []))

        if not ticks:
            return []

        # 3분 단위 버킷으로 그룹화
        buckets: dict[str, list[int]] = {}
        for tick in ticks:
            ts: datetime = tick.get("recv_at", datetime.now())
            # 3분 단위 내림차순 정렬 (00, 03, 06, ... 분)
            minute_3 = (ts.minute // 3) * 3
            bar_key = ts.strftime(f"%H:{minute_3:02d}")
            if bar_key not in buckets:
                buckets[bar_key] = []
            buckets[bar_key].append(int(tick.get("current_price", 0)))

        if not buckets:
            return []

        bars = []
        for bar_time in sorted(buckets.keys()):
            prices = [p for p in buckets[bar_time] if p > 0]
            if not prices:
                continue
            bars.append({
                "open": prices[0],
                "high": max(prices),
                "low": min(prices),
                "close": prices[-1],
                "volume": len(prices),  # 틱 수 (정밀 거래량은 별도)
                "bar_time": bar_time,
            })

        return bars[-n:]

    @staticmethod
    def calc_three_min_up_ratio(bars: list[dict]) -> float:
        """
        3분봉 우상향 비율 계산.
        연속 2개 바에서 close[i] > close[i-1] 이면 우상향.

        Args:
            bars: get_three_min_bars() 결과
        Returns:
            0.0~1.0 (우상향 구간 / 전체 구간)
        """
        if len(bars) < 2:
            return 0.0
        up = sum(
            1 for i in range(1, len(bars))
            if bars[i]["close"] > bars[i - 1]["close"]
        )
        return up / (len(bars) - 1)
