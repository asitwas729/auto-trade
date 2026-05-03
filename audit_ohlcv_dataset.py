"""
Audit a directory of OHLCV CSV files and summarize which files are usable.

Usage:
    python audit_ohlcv_dataset.py --csv-dir data/korea2009
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from run_backtest import load_ohlcv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit a directory of OHLCV CSV files.")
    parser.add_argument("--csv-dir", required=True, help="Directory containing CSV files")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    csv_dir = Path(args.csv_dir)
    if not csv_dir.exists():
        raise FileNotFoundError(f"CSV directory not found: {csv_dir}")

    rows: list[dict] = []
    for csv_path in sorted(csv_dir.glob("*.csv")):
        try:
            df = load_ohlcv(csv_path)
            rows.append(
                {
                    "file": csv_path.name,
                    "status": "ok",
                    "rows": len(df),
                    "start_date": str(df["date"].min().date()),
                    "end_date": str(df["date"].max().date()),
                    "error": "",
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "file": csv_path.name,
                    "status": "error",
                    "rows": 0,
                    "start_date": "",
                    "end_date": "",
                    "error": str(exc),
                }
            )

    report_df = pd.DataFrame(rows)
    out_path = csv_dir / "_audit_summary.csv"
    report_df.to_csv(out_path, index=False, encoding="utf-8-sig")

    print(f"Scanned      : {len(report_df)}")
    print(f"Valid files  : {(report_df['status'] == 'ok').sum()}")
    print(f"Invalid files: {(report_df['status'] == 'error').sum()}")
    print(f"Summary saved: {out_path}")
    print()
    if (report_df["status"] == "error").any():
        print("Top errors:")
        print(report_df.loc[report_df["status"] == "error", "error"].value_counts().head(5).to_string())


if __name__ == "__main__":
    main()
