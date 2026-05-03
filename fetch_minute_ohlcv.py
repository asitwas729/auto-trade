"""
Fetch current-day minute OHLCV data from KIS and save it locally.

Usage:
    python fetch_minute_ohlcv.py --code 005930 --name 삼성전자
    python fetch_minute_ohlcv.py --code 005930 --time-str 090000
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from config.settings import settings
from core.kis_api_client import KISApiClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch current-day minute OHLCV data from KIS.")
    parser.add_argument("--code", required=True, help="Stock code, e.g. 005930")
    parser.add_argument("--name", default="", help="Optional stock name")
    parser.add_argument("--time-str", default="090000", help="Start time for minute chart request, HHMMSS")
    parser.add_argument("--market", default="J", help="KIS market code")
    return parser.parse_args()


def fetch_prev_close(client: KISApiClient, code: str, market: str) -> int:
    end = datetime.now()
    start = end - timedelta(days=10)
    rows = client.get_daily_ohlcv(
        code=code,
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        market=market,
        adj_price=True,
    )
    if len(rows) >= 2:
        return int(rows[-2]["close"])
    if len(rows) == 1:
        return int(rows[-1]["close"])
    return 0


def normalize_rows(rows: list[dict], code: str, name: str, prev_close: int) -> pd.DataFrame:
    if not rows:
        raise ValueError("No minute rows returned from KIS API")

    today = datetime.now().strftime("%Y-%m-%d")
    df = pd.DataFrame(rows)
    df["time"] = df["time"].astype(str).str.zfill(6)
    df["date"] = today
    df["datetime"] = pd.to_datetime(df["date"] + " " + df["time"].str.slice(0, 2) + ":" + df["time"].str.slice(2, 4))
    df = df.sort_values("datetime").drop_duplicates("time").reset_index(drop=True)
    df["code"] = code
    df["name"] = name
    df["prev_close"] = prev_close
    return df[["date", "time", "datetime", "code", "name", "open", "high", "low", "close", "volume", "prev_close"]]


def save_csv(df: pd.DataFrame, code: str) -> Path:
    out_dir = settings.DATA_DIR / "ohlcv"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{code}_minute.csv"
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out_path


def main() -> None:
    args = parse_args()
    client = KISApiClient()
    client.init()

    prev_close = fetch_prev_close(client, args.code, args.market)
    rows = client.get_minute_ohlcv(code=args.code, time_str=args.time_str, market=args.market)
    df = normalize_rows(rows, args.code, args.name or args.code, prev_close)
    csv_path = save_csv(df, args.code)

    print(f"Mode         : {settings.TRADING_MODE}")
    print(f"Code         : {args.code}")
    print(f"Rows         : {len(df)}")
    print(f"Time range   : {df['time'].min()} ~ {df['time'].max()}")
    print(f"Prev close   : {prev_close}")
    print(f"CSV saved    : {csv_path}")
    print()
    print("Next command:")
    print(f"python run_intraday_backtest.py --csv {csv_path} --strategy S1 --code {args.code} --name {args.name or args.code}")


if __name__ == "__main__":
    main()
