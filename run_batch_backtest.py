"""
Run backtests for multiple CSV files and save a summary report.

Usage:
    python run_batch_backtest.py --codes 005930,000660 --strategy S3
    python run_batch_backtest.py --code-file data/codes.txt --strategy S3 --save-trades
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from backtester.backtester import Backtester
from fetch_many_ohlcv import parse_code_items
from run_backtest import build_strategy_adapter, load_ohlcv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run backtests for multiple local CSV files.")
    parser.add_argument("--codes", help="Comma-separated stock codes")
    parser.add_argument("--code-file", help="Text or CSV file containing stock codes")
    parser.add_argument("--csv-dir", default="data/ohlcv", help="Directory containing <code>_daily.csv files")
    parser.add_argument("--strategy", default="S3", choices=["S1", "S2", "S3"], help="Strategy name")
    parser.add_argument("--capital", type=float, default=10_000_000, help="Initial capital")
    parser.add_argument("--position-ratio", type=float, default=0.25, help="Capital ratio per trade")
    parser.add_argument("--save-trades", action="store_true", help="Save per-code trade logs")
    return parser.parse_args()


def resolve_csv_path(csv_dir: Path, code: str) -> Path:
    candidates = [
        csv_dir / f"{code}_daily.csv",
        csv_dir / f"{code}.csv",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(f"No CSV file found for code {code} in {csv_dir}")


def summarize_result(code: str, name: str, result, capital: float) -> dict:
    total_return = result.total_pnl / capital if capital else 0.0
    return {
        "code": code,
        "name": name,
        "strategy": result.strategy,
        "total_trades": result.total_trades,
        "win_rate": result.win_rate,
        "avg_win_rate": result.avg_win_rate,
        "avg_loss_rate": result.avg_loss_rate,
        "expectancy": result.expectancy,
        "total_pnl": result.total_pnl,
        "total_return": total_return,
        "max_drawdown": result.max_drawdown,
        "sharpe_ratio": result.sharpe_ratio,
    }


def save_trade_log(code: str, result) -> Path:
    report_dir = Path("data") / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    out_path = report_dir / f"{result.strategy.lower()}_{code}_trades.csv"
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
    code_items = parse_code_items(args.codes, args.code_file)
    if not code_items:
        raise ValueError("Provide --codes or --code-file with at least one stock code")

    csv_dir = Path(args.csv_dir)
    if not csv_dir.exists():
        raise FileNotFoundError(f"CSV directory not found: {csv_dir}")

    rows: list[dict] = []
    backtester = Backtester(initial_capital=args.capital)
    for code, name in code_items:
        try:
            csv_path = resolve_csv_path(csv_dir, code)
            df = load_ohlcv(csv_path)
            df["code"] = code
            strategy_func = build_strategy_adapter(args.strategy, code, name)
            result = backtester.run(
                df=df,
                strategy_func=strategy_func,
                strategy_name=args.strategy,
                position_ratio=args.position_ratio,
            )
            row = summarize_result(code, name, result, args.capital)
            row["status"] = "ok"
            row["csv_path"] = str(csv_path)
            row["error"] = ""
            rows.append(row)
            print(
                f"[OK] {code} {name} | trades={result.total_trades} | "
                f"return={row['total_return']:+.2%} | pnl={result.total_pnl:+,.0f}"
            )

            if args.save_trades:
                trade_path = save_trade_log(code, result)
                print(f"     trades saved: {trade_path}")
        except Exception as exc:
            rows.append(
                {
                    "code": code,
                    "name": name,
                    "strategy": args.strategy,
                    "total_trades": None,
                    "win_rate": None,
                    "avg_win_rate": None,
                    "avg_loss_rate": None,
                    "expectancy": None,
                    "total_pnl": None,
                    "total_return": None,
                    "max_drawdown": None,
                    "sharpe_ratio": None,
                    "status": "error",
                    "csv_path": "",
                    "error": str(exc),
                }
            )
            print(f"[SKIP] {code} {name} | {exc}")

    report_dir = Path("data") / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    out_path = report_dir / f"batch_backtest_{args.strategy.lower()}.csv"
    report_df = pd.DataFrame(rows)
    if not report_df.empty and "status" in report_df.columns:
        ok_df = report_df[report_df["status"] == "ok"].sort_values(
            ["total_return", "sharpe_ratio"], ascending=False
        )
        err_df = report_df[report_df["status"] != "ok"]
        report_df = pd.concat([ok_df, err_df], ignore_index=True)
    report_df.to_csv(out_path, index=False, encoding="utf-8-sig")

    print()
    print(f"Strategy     : {args.strategy}")
    print(f"Processed    : {len(report_df)}")
    print(f"Summary saved: {out_path}")


if __name__ == "__main__":
    main()
