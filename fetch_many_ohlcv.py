"""
Fetch daily OHLCV data for multiple stocks from KIS.

Usage:
    python fetch_many_ohlcv.py --codes 005930,000660 --days 180
    python fetch_many_ohlcv.py --code-file data/codes.txt --start-date 20240101 --end-date 20241231
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from fetch_ohlcv import normalize_rows, resolve_dates, save_csv
from config.settings import settings
from core.kis_api_client import KISApiClient
from core.market_data_collector import MarketDataCollector


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch daily OHLCV for multiple stock codes.")
    parser.add_argument("--codes", help="Comma-separated codes, e.g. 005930,000660")
    parser.add_argument("--code-file", help="Text or CSV file containing stock codes")
    parser.add_argument("--start-date", help="Start date in YYYYMMDD")
    parser.add_argument("--end-date", help="End date in YYYYMMDD")
    parser.add_argument("--days", type=int, default=180, help="Lookback days when start-date is omitted")
    parser.add_argument("--market", default="J", help="KIS market code")
    parser.add_argument("--csv-only", action="store_true", help="Save only CSV files")
    return parser.parse_args()


def parse_code_items(codes_arg: str | None, code_file: str | None) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []

    if codes_arg:
        for code in codes_arg.split(","):
            code = code.strip()
            if code:
                items.append((code, code))

    if code_file:
        path = Path(code_file)
        if not path.exists():
            raise FileNotFoundError(f"Code file not found: {path}")

        if path.suffix.lower() == ".csv":
            df = pd.read_csv(path, dtype={"code": str})
            if "code" not in df.columns:
                raise ValueError("CSV code file must contain a 'code' column")
            for _, row in df.iterrows():
                code = str(row["code"]).strip().zfill(6)
                name = str(row["name"]).strip() if "name" in df.columns and pd.notna(row["name"]) else code
                if code:
                    items.append((code, name))
        else:
            for line in path.read_text(encoding="utf-8").splitlines():
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    continue
                if "," in raw:
                    code, name = [part.strip() for part in raw.split(",", 1)]
                else:
                    code, name = raw, raw
                items.append((code, name))

    # preserve order, remove duplicates
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for code, name in items:
        if code not in seen:
            deduped.append((code, name))
            seen.add(code)
    return deduped


def main() -> None:
    args = parse_args()
    code_items = parse_code_items(args.codes, args.code_file)
    if not code_items:
        raise ValueError("Provide --codes or --code-file with at least one stock code")

    start_date, end_date = resolve_dates(args.start_date, args.end_date, args.days)
    client = KISApiClient()
    client.init()
    collector = None if args.csv_only else MarketDataCollector()

    summary_rows: list[dict] = []
    for code, name in code_items:
        rows = client.get_daily_ohlcv(
            code=code,
            start_date=start_date,
            end_date=end_date,
            market=args.market,
            adj_price=True,
        )
        df = normalize_rows(rows, code, name)
        csv_path = save_csv(df, code)

        parquet_path = ""
        if collector is not None:
            collector.save_daily_ohlcv(code, df[["date", "open", "high", "low", "close", "volume", "amount"]])
            parquet_path = str(settings.DATA_DIR / "ohlcv" / f"{code}_daily.parquet")

        summary_rows.append(
            {
                "code": code,
                "name": name,
                "rows": len(df),
                "start_date": str(df["date"].min().date()),
                "end_date": str(df["date"].max().date()),
                "csv_path": str(csv_path),
                "parquet_path": parquet_path,
            }
        )
        print(f"[OK] {code} {name} | {len(df)} rows | {csv_path}")

    report_dir = settings.DATA_DIR / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    summary_path = report_dir / "fetch_ohlcv_summary.csv"
    pd.DataFrame(summary_rows).to_csv(summary_path, index=False, encoding="utf-8-sig")

    print()
    print(f"Mode         : {settings.TRADING_MODE}")
    print(f"Fetched codes: {len(summary_rows)}")
    print(f"Summary saved: {summary_path}")


if __name__ == "__main__":
    main()
