from __future__ import annotations

from pathlib import Path

import pandas as pd

from fetch_minute_ohlcv import normalize_rows
from run_intraday_backtest import load_minute_ohlcv, run_intraday_backtest


def test_normalize_minute_rows():
    rows = [
        {"time": "090100", "open": 101, "high": 102, "low": 100, "close": 101, "volume": 1000},
        {"time": "090000", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 900},
    ]
    df = normalize_rows(rows, "005930", "삼성전자", 99000)

    assert list(df["time"]) == ["090000", "090100"]
    assert df.iloc[0]["prev_close"] == 99000


def test_run_intraday_backtest_s1(tmp_path: Path):
    csv_path = tmp_path / "minute.csv"
    rows = []
    for i in range(25):
        hh = 9
        mm = i
        close = 1000 if i < 15 else 1025
        low = 995 if i < 15 else 998
        rows.append(
            {
                "date": "2025-04-30",
                "time": f"{hh:02d}{mm:02d}00",
                "open": 1000,
                "high": close + 3,
                "low": low,
                "close": close,
                "volume": 20000 if i >= 15 else 1000,
                "prev_close": 1000,
            }
        )
    pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")

    df = load_minute_ohlcv(csv_path)
    result = run_intraday_backtest(
        df=df,
        strategy_name="S1",
        code="005930",
        name="삼성전자",
        initial_capital=10_000_000,
        position_ratio=0.25,
    )

    assert result.strategy == "S1"
    assert isinstance(result.total_trades, int)
