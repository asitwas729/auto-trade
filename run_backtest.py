"""
Simple backtest runner for CSV OHLCV data.

Usage:
    python run_backtest.py --csv data/ohlcv/sample.csv --strategy S3
    python run_backtest.py --csv data/ohlcv/005930.csv --strategy S1 --code 005930 --name 삼성전자
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from backtester.backtester import BTResult, Backtester
from core.market_data_collector import MarketDataCollector
from strategies.base_strategy import Signal
from strategies.s1_open_volatility import S1OpenVolatility
from strategies.s2_gap_news import S2GapNews
from strategies.s3_swing_momentum import S3SwingMomentum


REQUIRED_COLUMNS = {"date", "open", "high", "low", "close", "volume"}
COLUMN_ALIASES = {
    "Date": "date",
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",
    "Volumn": "volume",
    "AdjOpen": "adj_open",
    "AdjHigh": "adj_high",
    "AdjLow": "adj_low",
    "AdjClose": "adj_close",
    "AdjVolumn": "adj_volume",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a backtest from a CSV file.")
    parser.add_argument("--csv", required=True, help="Path to OHLCV CSV file")
    parser.add_argument("--strategy", default="S3", choices=["S1", "S2", "S3"], help="Strategy name")
    parser.add_argument("--capital", type=float, default=10_000_000, help="Initial capital")
    parser.add_argument("--position-ratio", type=float, default=0.25, help="Capital ratio per trade")
    parser.add_argument("--code", default="TEST", help="Code label used in reports")
    parser.add_argument("--name", default="TEST", help="Name label used in reports")
    parser.add_argument(
        "--save-trades",
        action="store_true",
        help="Save executed trades to data/reports/<strategy>_trades.csv",
    )
    return parser.parse_args()


def load_ohlcv(csv_path: Path) -> pd.DataFrame:
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    df = pd.read_csv(csv_path)
    df = df.rename(columns={col: COLUMN_ALIASES.get(col, col) for col in df.columns})

    if "close" not in df.columns and "adj_close" in df.columns:
        df["close"] = df["adj_close"]
    if "low" not in df.columns and "adj_low" in df.columns:
        df["low"] = df["adj_low"]
    if "high" not in df.columns and "adj_high" in df.columns:
        df["high"] = df["adj_high"]
    if "open" not in df.columns and "adj_open" in df.columns:
        df["open"] = df["adj_open"]
    if "volume" not in df.columns and "adj_volume" in df.columns:
        df["volume"] = df["adj_volume"]

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns after header normalization: {sorted(missing)}")

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    if df["date"].isna().any():
        raise ValueError("date column contains invalid values")

    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    required_value_cols = ["open", "high", "low", "close", "volume"]
    if df[required_value_cols].isna().all().any():
        null_cols = [col for col in required_value_cols if df[col].isna().all()]
        raise ValueError(
            "OHLCV dataset is malformed: required columns are entirely empty "
            f"after normalization: {null_cols}"
        )
    if df[required_value_cols].isna().any().any():
        raise ValueError("OHLCV columns contain missing or non-numeric values")

    df = df.sort_values("date").reset_index(drop=True)
    df = MarketDataCollector.calc_indicators(df)
    df["prev_close"] = df["close"].shift(1)
    df["after_hours_close"] = (
        pd.to_numeric(df["after_hours_close"], errors="coerce")
        if "after_hours_close" in df.columns
        else df["prev_close"] * 1.03
    )
    df["has_news"] = df["has_news"].fillna(True) if "has_news" in df.columns else True
    df["theme_strength"] = (
        pd.to_numeric(df["theme_strength"], errors="coerce").fillna(0.7)
        if "theme_strength" in df.columns
        else 0.7
    )
    rolling_high = df["high"].rolling(252, min_periods=20).max()
    df["near_52w_high"] = (
        df["near_52w_high"].fillna(False)
        if "near_52w_high" in df.columns
        else df["close"] >= rolling_high * 0.95
    )
    prev_high_20 = df["high"].rolling(20, min_periods=5).max().shift(1)
    df["resistance_break"] = (
        df["resistance_break"].fillna(False)
        if "resistance_break" in df.columns
        else df["close"] > prev_high_20.fillna(df["close"])
    )
    df["report_target_price"] = (
        pd.to_numeric(df["report_target_price"], errors="coerce").fillna(df["close"] * 1.2)
        if "report_target_price" in df.columns
        else df["close"] * 1.3
    )
    return df


def build_strategy_adapter(strategy_name: str, code: str, name: str) -> Callable[[pd.Series, dict], Optional[str]]:
    if strategy_name == "S1":
        strategy = S1OpenVolatility()

        def strategy_func(row: pd.Series, state: dict) -> Optional[str]:
            signal = strategy.evaluate(
                code=code,
                name=name,
                current_price=int(row["close"]),
                open_price=int(row["open"]),
                high_price=int(row["high"]),
                low_price=int(row["low"]),
                prev_close=int(row.get("prev_close") or row["close"]),
                ma5=float(row.get("ma5", 0) or 0),
                ma20=float(row.get("ma20", 0) or 0),
                volume=int(row["volume"]),
                vol_ma20=float(row.get("vol_ma20", 0) or 0),
                vol_ratio=float(row.get("vol_ratio", 0) or 0),
                ask1=int(row["close"]),
                bid1=int(row["close"]),
                bid_qty_sum=max(int(row["volume"]), 1),
                ask_qty_sum=max(int(row["volume"] // 2), 1),
                current_time="100000",
                position_qty=int(state["position_qty"]),
                avg_entry_price=float(state["avg_price"]),
            )
            return _signal_to_action(signal)

        return strategy_func

    if strategy_name == "S2":
        strategy = S2GapNews()

        def strategy_func(row: pd.Series, state: dict) -> Optional[str]:
            signal = strategy.evaluate(
                code=code,
                name=name,
                current_price=int(row["close"]),
                open_price=int(row["open"]),
                prev_close=int(row.get("prev_close") or row["close"]),
                after_hours_close=int(row.get("after_hours_close") or row["close"]),
                vol_ratio=float(row.get("vol_ratio", 0) or 0),
                has_news=bool(row.get("has_news", True)),
                current_time="091000",
                position_qty=int(state["position_qty"]),
                avg_entry_price=float(state["avg_price"]),
            )
            return _signal_to_action(signal)

        return strategy_func

    if strategy_name == "S3":
        strategy = S3SwingMomentum()

        def strategy_func(row: pd.Series, state: dict) -> Optional[str]:
            entry_date = state.get("entry_date")
            if state["position_qty"] > 0 and not entry_date:
                entry_date = row["date"].to_pydatetime()

            signal = strategy.evaluate(
                code=code,
                name=name,
                current_price=int(row["close"]),
                report_target_price=int(row.get("report_target_price") or 0),
                vol_ratio=float(row.get("vol_ratio", 0) or 0),
                theme_strength=float(row.get("theme_strength", 0.7) or 0.7),
                near_52w_high=bool(row.get("near_52w_high", False)),
                resistance_break=bool(row.get("resistance_break", False)),
                atr_rate=float(row.get("atr_rate", 0) or 0),
                position_qty=int(state["position_qty"]),
                avg_entry_price=float(state["avg_price"]),
                entry_date=entry_date,
                high_since_entry=int(state.get("high_since_entry", 0) or 0),
            )
            return _signal_to_action(signal)

        return strategy_func

    raise ValueError(f"Unsupported strategy: {strategy_name}")


def _signal_to_action(signal) -> Optional[str]:
    if signal is None:
        return None
    if signal.signal == Signal.BUY:
        return "BUY"
    if signal.signal == Signal.SELL:
        return "SELL"
    return None


def format_report(result: BTResult, initial_capital: float) -> str:
    total_return = result.total_pnl / initial_capital if initial_capital else 0.0
    lines = [
        f"=== Backtest Result: {result.strategy} ===",
        f"Total trades : {result.total_trades}",
        f"Win rate     : {result.win_rate:.2%}",
        f"Avg win rate : {result.avg_win_rate:+.2%}",
        f"Avg loss rate: {result.avg_loss_rate:+.2%}",
        f"Expectancy   : {result.expectancy:+.2%}",
        f"Total PnL    : {result.total_pnl:+,.0f}",
        f"Total return : {total_return:+.2%}",
        f"Max drawdown : {result.max_drawdown:.2%}",
        f"Sharpe ratio : {result.sharpe_ratio:.2f}",
    ]
    return "\n".join(lines)


def print_strategy_notes(strategy_name: str) -> None:
    if strategy_name in {"S1", "S2"}:
        print("[WARN] S1/S2 are intraday strategies. Daily OHLCV CSV backtests are approximate.")
        print("[WARN] For more realistic results, use minute bars with intraday-specific fields.")


def save_trades(result: BTResult) -> Path:
    report_dir = Path("data") / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    out_path = report_dir / f"{result.strategy.lower()}_trades.csv"
    rows = [
        {
            "date": trade.date,
            "code": trade.code,
            "strategy": trade.strategy,
            "side": trade.side,
            "quantity": trade.quantity,
            "price": trade.price,
            "amount": trade.amount,
            "fee": trade.fee,
            "tax": trade.tax,
            "realized_pnl": trade.realized_pnl,
            "realized_pnl_rate": trade.realized_pnl_rate,
        }
        for trade in result.trades
    ]
    pd.DataFrame(rows).to_csv(out_path, index=False, encoding="utf-8-sig")
    return out_path


def main() -> None:
    args = parse_args()
    csv_path = Path(args.csv)
    df = load_ohlcv(csv_path)
    df["code"] = args.code
    print_strategy_notes(args.strategy)

    backtester = Backtester(initial_capital=args.capital)
    strategy_func = build_strategy_adapter(args.strategy, args.code, args.name)
    result = backtester.run(
        df=df,
        strategy_func=strategy_func,
        strategy_name=args.strategy,
        position_ratio=args.position_ratio,
    )

    print(f"Input CSV     : {csv_path}")
    print(f"Rows          : {len(df)}")
    print(f"Date range    : {df['date'].min().date()} ~ {df['date'].max().date()}")
    print(format_report(result, args.capital))

    if args.save_trades:
        out_path = save_trades(result)
        print(f"Trades saved  : {out_path}")


if __name__ == "__main__":
    main()
