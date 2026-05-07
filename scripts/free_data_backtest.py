"""
Pull free 1-minute OHLCV (Naver, ~30 trading days) and backtest a strategy
across multiple KRX tickers. Aggregates per-symbol results.

Usage:
    python scripts/free_data_backtest.py --codes 005930 000660 035720 035420 005380 \
        --strategy S1 --days 30

    python scripts/free_data_backtest.py --codes-file config/krx_top.txt \
        --strategy S6 --skip-fetch
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pandas as pd

from run_intraday_backtest import (
    format_report,
    load_minute_ohlcv,
    run_intraday_backtest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--codes", nargs="*", default=[])
    p.add_argument("--codes-file", default="")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--strategy", default="S1", choices=["S1", "S2", "S6", "S7", "S8", "S9"])
    p.add_argument("--capital", type=float, default=10_000_000)
    p.add_argument("--position-ratio", type=float, default=0.25)
    p.add_argument("--data-dir", default="data/ohlcv")
    p.add_argument("--skip-fetch", action="store_true", help="Reuse existing CSVs without re-downloading")
    return p.parse_args()


def load_codes(args: argparse.Namespace) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = [(c, c) for c in args.codes]
    if args.codes_file:
        for raw in Path(args.codes_file).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "," in line:
                code, name = [s.strip() for s in line.split(",", 1)]
            else:
                code, name = line, line
            pairs.append((code, name))
    return pairs


def fetch(codes: list[tuple[str, str]], days: int, data_dir: str) -> None:
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "fetch_minute_naver.py"),
           "--codes", *[c for c, _ in codes],
           "--days", str(days),
           "--out-dir", data_dir]
    print(">>", " ".join(cmd))
    subprocess.run(cmd, check=False)


def main() -> None:
    args = parse_args()
    pairs = load_codes(args)
    if not pairs:
        raise SystemExit("No codes provided.")

    if not args.skip_fetch:
        fetch(pairs, args.days, args.data_dir)

    summary_rows = []
    for code, name in pairs:
        csv_path = Path(args.data_dir) / f"{code}_minute.csv"
        if not csv_path.exists():
            print(f"\n=== {code} {name} ===  CSV missing, skip")
            continue
        try:
            df = load_minute_ohlcv(csv_path)
        except Exception as e:
            print(f"\n=== {code} {name} ===  load error: {e}")
            continue
        result = run_intraday_backtest(
            df=df, strategy_name=args.strategy,
            code=code, name=name,
            initial_capital=args.capital,
            position_ratio=args.position_ratio,
        )
        days = df["date"].nunique() if "date" in df.columns else 1
        print(f"\n=== {code} {name} ({days} days, {len(df)} bars) ===")
        print(format_report(result, args.capital))
        summary_rows.append({
            "code": code, "name": name, "days": days, "bars": len(df),
            "trades": result.total_trades,
            "win_rate": result.win_rate,
            "expectancy": result.expectancy,
            "total_pnl": result.total_pnl,
            "total_return": result.total_pnl / args.capital if args.capital else 0.0,
        })

    if summary_rows:
        print("\n========== SUMMARY ==========")
        df_sum = pd.DataFrame(summary_rows)
        # Display friendly formats
        with pd.option_context("display.float_format", "{:,.4f}".format):
            print(df_sum.to_string(index=False))
        out = Path("data") / "reports" / f"free_backtest_{args.strategy.lower()}_summary.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        df_sum.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\nSummary saved: {out}")


if __name__ == "__main__":
    main()
