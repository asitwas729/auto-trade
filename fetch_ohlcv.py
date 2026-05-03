"""
Fetch real daily OHLCV data from KIS and save it locally.

Usage:
    python fetch_ohlcv.py --code 005930 --days 180
    python fetch_ohlcv.py --code 005930 --start-date 20240101 --end-date 20241231 --name 삼성전자
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from config.settings import settings
from core.kis_api_client import KISApiClient
from core.market_data_collector import MarketDataCollector


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch daily OHLCV data from KIS.")
    parser.add_argument("--code", required=True, help="Stock code, e.g. 005930")
    parser.add_argument("--name", default="", help="Optional stock name for file naming/logging")
    parser.add_argument("--start-date", help="Start date in YYYYMMDD")
    parser.add_argument("--end-date", help="End date in YYYYMMDD")
    parser.add_argument("--days", type=int, default=180, help="Lookback days when start-date is omitted")
    parser.add_argument(
        "--market",
        default="J",
        help="KIS market code. Domestic stocks usually use J.",
    )
    parser.add_argument(
        "--csv-only",
        action="store_true",
        help="Save only CSV and skip parquet cache update.",
    )
    return parser.parse_args()


def resolve_dates(start_date: str | None, end_date: str | None, days: int) -> tuple[str, str]:
    if end_date is None:
        end = datetime.now()
    else:
        end = datetime.strptime(end_date, "%Y%m%d")

    if start_date is None:
        start = end - timedelta(days=days)
    else:
        start = datetime.strptime(start_date, "%Y%m%d")

    if start > end:
        raise ValueError("start-date must be earlier than or equal to end-date")

    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def normalize_rows(rows: list[dict], code: str, name: str) -> pd.DataFrame:
    if not rows:
        raise ValueError("No OHLCV rows returned from KIS API")

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="raise")
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    df["code"] = code
    df["name"] = name
    return df


def save_csv(df: pd.DataFrame, code: str) -> Path:
    out_dir = settings.DATA_DIR / "ohlcv"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{code}_daily.csv"
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out_path


def main() -> None:
    args = parse_args()
    start_date, end_date = resolve_dates(args.start_date, args.end_date, args.days)

    client = KISApiClient()
    client.init()

    rows = client.get_daily_ohlcv(
        code=args.code,
        start_date=start_date,
        end_date=end_date,
        market=args.market,
        adj_price=True,
    )
    df = normalize_rows(rows, args.code, args.name)
    csv_path = save_csv(df, args.code)

    parquet_path = None
    if not args.csv_only:
        collector = MarketDataCollector()
        collector.save_daily_ohlcv(args.code, df[["date", "open", "high", "low", "close", "volume", "amount"]])
        parquet_path = settings.DATA_DIR / "ohlcv" / f"{args.code}_daily.parquet"

    print(f"Mode         : {settings.TRADING_MODE}")
    print(f"Code         : {args.code}")
    print(f"Rows         : {len(df)}")
    print(f"Date range   : {df['date'].min().date()} ~ {df['date'].max().date()}")
    print(f"CSV saved    : {csv_path}")
    if parquet_path:
        print(f"Parquet saved: {parquet_path}")
    print()
    print("Next command:")
    print(f"python run_backtest.py --csv {csv_path} --strategy S3 --code {args.code} --name {args.name or args.code}")


if __name__ == "__main__":
    main()
