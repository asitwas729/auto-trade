"""
Run an approximate intraday backtest for S1/S2 using minute CSV data.

Usage:
    python run_intraday_backtest.py --csv data/ohlcv/005930_minute.csv --strategy S1 --code 005930 --name 삼성전자
    python run_intraday_backtest.py --csv data/ohlcv/005930_minute.csv --strategy S2 --code 005930 --name 삼성전자
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from backtester.backtester import BTResult, BTTrade
from config.constants import COMMISSION_RATE, TRANSACTION_TAX_RATE
from core.order_manager import round_to_tick
from strategies.base_strategy import Signal
from strategies.s1_open_volatility import S1OpenVolatility
from strategies.s2_gap_news import S2GapNews


REQUIRED_COLUMNS = {"date", "time", "open", "high", "low", "close", "volume"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an intraday backtest from minute OHLCV CSV.")
    parser.add_argument("--csv", required=True, help="Path to minute OHLCV CSV file")
    parser.add_argument("--strategy", default="S1", choices=["S1", "S2"], help="Intraday strategy name")
    parser.add_argument("--capital", type=float, default=10_000_000, help="Initial capital")
    parser.add_argument("--position-ratio", type=float, default=0.25, help="Capital ratio per entry")
    parser.add_argument("--code", default="TEST", help="Code label")
    parser.add_argument("--name", default="TEST", help="Name label")
    parser.add_argument("--save-trades", action="store_true", help="Save trade log")
    return parser.parse_args()


def load_minute_ohlcv(csv_path: Path) -> pd.DataFrame:
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    df = pd.read_csv(csv_path, dtype={"time": str})
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    df = df.copy()
    df["time"] = df["time"].astype(str).str.zfill(6)
    df["date"] = df["date"].astype(str)
    df["datetime"] = pd.to_datetime(
        df["date"] + " " + df["time"].str.slice(0, 2) + ":" + df["time"].str.slice(2, 4),
        errors="coerce",
    )
    if df["datetime"].isna().any():
        raise ValueError("date/time columns contain invalid values")

    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if df[["open", "high", "low", "close", "volume"]].isna().any().any():
        raise ValueError("OHLCV columns contain non-numeric values")

    df = df.sort_values("datetime").reset_index(drop=True)
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]
    df["prev_close"] = pd.to_numeric(df.get("prev_close", 0), errors="coerce").fillna(0).astype(int)
    df["after_hours_close"] = pd.to_numeric(
        df.get("after_hours_close", df["prev_close"] * 1.03), errors="coerce"
    ).fillna(df["prev_close"] * 1.03)
    df["has_news"] = df["has_news"].fillna(True) if "has_news" in df.columns else True
    return df


def format_report(result: BTResult, initial_capital: float) -> str:
    total_return = result.total_pnl / initial_capital if initial_capital else 0.0
    lines = [
        f"=== Intraday Backtest Result: {result.strategy} ===",
        f"Total trades : {result.total_trades}",
        f"Win rate     : {result.win_rate:.2%}",
        f"Avg win rate : {result.avg_win_rate:+.2%}",
        f"Avg loss rate: {result.avg_loss_rate:+.2%}",
        f"Expectancy   : {result.expectancy:+.2%}",
        f"Total PnL    : {result.total_pnl:+,.0f}",
        f"Total return : {total_return:+.2%}",
    ]
    return "\n".join(lines)


def run_intraday_backtest(
    df: pd.DataFrame,
    strategy_name: str,
    code: str,
    name: str,
    initial_capital: float,
    position_ratio: float,
) -> BTResult:
    if strategy_name == "S1":
        strategy = S1OpenVolatility()
    elif strategy_name == "S2":
        strategy = S2GapNews()
    else:
        raise ValueError(f"Unsupported intraday strategy: {strategy_name}")

    cash = initial_capital
    position_qty = 0
    avg_price = 0.0
    high_since_entry = 0
    result = BTResult(strategy=strategy_name)

    session_open = int(df.iloc[0]["open"])
    session_low = session_open
    session_high = session_open

    for _, row in df.iterrows():
        price = int(row["close"])
        bar_low = int(row["low"])
        bar_high = int(row["high"])
        current_time = str(row["time"]).zfill(6)
        session_low = min(session_low, bar_low)
        session_high = max(session_high, bar_high)

        if position_qty > 0:
            high_since_entry = max(high_since_entry, bar_high)

        if strategy_name == "S1":
            signal = strategy.evaluate(
                code=code,
                name=name,
                current_price=price,
                open_price=session_open,
                high_price=session_high,
                low_price=session_low,
                prev_close=int(row.get("prev_close", 0) or session_open),
                ma5=float(row.get("ma5", 0) or 0),
                ma20=float(row.get("ma20", 0) or 0),
                volume=int(row["volume"]),
                vol_ma20=float(row.get("vol_ma20", 0) or 0),
                vol_ratio=float(row.get("vol_ratio", 0) or 0),
                ask1=price,
                bid1=price,
                bid_qty_sum=max(int(row["volume"]), 1),
                ask_qty_sum=max(int(row["volume"] // 2), 1),
                current_time=current_time,
                position_qty=position_qty,
                avg_entry_price=avg_price,
            )
        else:
            signal = strategy.evaluate(
                code=code,
                name=name,
                current_price=price,
                open_price=session_open,
                prev_close=int(row.get("prev_close", 0) or session_open),
                after_hours_close=int(row.get("after_hours_close", 0) or session_open),
                vol_ratio=float(row.get("vol_ratio", 0) or 0),
                has_news=bool(row.get("has_news", True)),
                current_time=current_time,
                position_qty=position_qty,
                avg_entry_price=avg_price,
            )

        if signal is None:
            continue

        if signal.signal == Signal.BUY and position_qty == 0 and cash > 0:
            buy_price = round_to_tick(price)
            budget = cash * position_ratio
            qty = max(1, int(budget // (buy_price * (1 + COMMISSION_RATE))))
            fee = buy_price * qty * COMMISSION_RATE
            cost = buy_price * qty + fee
            if cost > cash:
                continue

            cash -= cost
            position_qty = qty
            avg_price = buy_price
            high_since_entry = buy_price
            result.trades.append(
                BTTrade(
                    date=str(row["datetime"]),
                    code=code,
                    strategy=strategy_name,
                    side="BUY",
                    quantity=qty,
                    price=buy_price,
                    amount=buy_price * qty,
                    fee=fee,
                    tax=0.0,
                )
            )

        elif signal.signal == Signal.SELL and position_qty > 0:
            sell_qty = signal.quantity if signal.quantity > 0 else position_qty
            sell_qty = min(sell_qty, position_qty)
            sell_price = round_to_tick(price)
            fee = sell_price * sell_qty * COMMISSION_RATE
            tax = sell_price * sell_qty * TRANSACTION_TAX_RATE
            proceed = sell_price * sell_qty - fee - tax
            realized_pnl = proceed - avg_price * sell_qty
            realized_rate = realized_pnl / (avg_price * sell_qty) if avg_price > 0 else 0.0

            cash += proceed
            position_qty -= sell_qty
            result.total_pnl += realized_pnl
            result.total_trades += 1
            if realized_pnl >= 0:
                result.win_trades += 1
                result.avg_win_rate = (
                    result.avg_win_rate * (result.win_trades - 1) + realized_rate
                ) / result.win_trades
            else:
                result.loss_trades += 1
                result.avg_loss_rate = (
                    result.avg_loss_rate * (result.loss_trades - 1) + realized_rate
                ) / result.loss_trades

            result.trades.append(
                BTTrade(
                    date=str(row["datetime"]),
                    code=code,
                    strategy=strategy_name,
                    side="SELL",
                    quantity=sell_qty,
                    price=sell_price,
                    amount=sell_price * sell_qty,
                    fee=fee,
                    tax=tax,
                    realized_pnl=realized_pnl,
                    realized_pnl_rate=realized_rate,
                )
            )

            if position_qty == 0:
                avg_price = 0.0
                high_since_entry = 0

    # session-end liquidation
    if position_qty > 0:
        last_price = round_to_tick(int(df.iloc[-1]["close"]))
        fee = last_price * position_qty * COMMISSION_RATE
        tax = last_price * position_qty * TRANSACTION_TAX_RATE
        proceed = last_price * position_qty - fee - tax
        realized_pnl = proceed - avg_price * position_qty
        realized_rate = realized_pnl / (avg_price * position_qty) if avg_price > 0 else 0.0
        cash += proceed
        result.total_pnl += realized_pnl
        result.total_trades += 1
        if realized_pnl >= 0:
            result.win_trades += 1
        else:
            result.loss_trades += 1
        result.trades.append(
            BTTrade(
                date=str(df.iloc[-1]["datetime"]),
                code=code,
                strategy=strategy_name,
                side="SELL",
                quantity=position_qty,
                price=last_price,
                amount=last_price * position_qty,
                fee=fee,
                tax=tax,
                realized_pnl=realized_pnl,
                realized_pnl_rate=realized_rate,
            )
        )

    result.win_rate = result.win_trades / max(result.total_trades, 1)
    result.expectancy = (
        result.win_rate * result.avg_win_rate
        - (1 - result.win_rate) * abs(result.avg_loss_rate)
    )
    return result


def save_trades(result: BTResult) -> Path:
    report_dir = Path("data") / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    out_path = report_dir / f"intraday_{result.strategy.lower()}_trades.csv"
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


def print_notes(strategy_name: str) -> None:
    print("[WARN] Intraday backtests are approximations and do not model order-book fills exactly.")
    if strategy_name == "S2":
        print("[WARN] S2 requires news and after-hours data. Missing columns default to synthetic values.")


def main() -> None:
    args = parse_args()
    df = load_minute_ohlcv(Path(args.csv))
    print_notes(args.strategy)
    result = run_intraday_backtest(
        df=df,
        strategy_name=args.strategy,
        code=args.code,
        name=args.name,
        initial_capital=args.capital,
        position_ratio=args.position_ratio,
    )

    print(f"Input CSV     : {args.csv}")
    print(f"Rows          : {len(df)}")
    print(f"Time range    : {df['time'].min()} ~ {df['time'].max()}")
    print(format_report(result, args.capital))

    if args.save_trades:
        out_path = save_trades(result)
        print(f"Trades saved  : {out_path}")


if __name__ == "__main__":
    main()
