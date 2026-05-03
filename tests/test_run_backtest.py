from __future__ import annotations

from pathlib import Path

import pandas as pd

from run_backtest import build_strategy_adapter, format_report, load_ohlcv
from backtester.backtester import Backtester


def test_load_ohlcv_adds_indicators(tmp_path: Path):
    csv_path = tmp_path / "sample.csv"
    df = pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=30, freq="D"),
            "open": [100 + i for i in range(30)],
            "high": [101 + i for i in range(30)],
            "low": [99 + i for i in range(30)],
            "close": [100 + i for i in range(30)],
            "volume": [100000 + i * 1000 for i in range(30)],
        }
    )
    df.to_csv(csv_path, index=False)

    loaded = load_ohlcv(csv_path)

    assert len(loaded) == 30
    assert {"ma5", "ma20", "vol_ratio", "atr_rate", "prev_close"} <= set(loaded.columns)


def test_s3_backtest_runner_flow(tmp_path: Path):
    csv_path = tmp_path / "trend.csv"
    closes = [100 + i * 2 for i in range(60)]
    df = pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=60, freq="D"),
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [100000 + i * 5000 for i in range(60)],
            "theme_strength": [0.8] * 60,
            "has_news": [True] * 60,
        }
    )
    df.to_csv(csv_path, index=False)

    loaded = load_ohlcv(csv_path)
    backtester = Backtester(initial_capital=10_000_000)
    strategy_func = build_strategy_adapter("S3", "TEST", "TEST")
    result = backtester.run(loaded, strategy_func, "S3", position_ratio=0.25)
    report = format_report(result, 10_000_000)

    assert result.strategy == "S3"
    assert "Backtest Result: S3" in report
