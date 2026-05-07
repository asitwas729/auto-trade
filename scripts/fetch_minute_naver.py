"""
Fetch free 1-minute OHLCV for KRX tickers from Naver Finance.

Naver's siseJson endpoint returns up to ~30 trading days of 1-minute bars
(roughly 11,700 rows / ~390 bars per day). This is the longest reliable free
window for Korean equities without a brokerage account.

Usage:
    python scripts/fetch_minute_naver.py --codes 005930 000660 035720
    python scripts/fetch_minute_naver.py --codes 005930 --days 30
    python scripts/fetch_minute_naver.py --codes-file config/krx_top.txt

Output: data/ohlcv/{code}_minute.csv  (compatible with run_intraday_backtest.py)
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests

NAVER_URL = "https://api.finance.naver.com/siseJson.naver"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"}


def fetch_naver_minute(code: str, start: str, end: str, timeout: int = 15) -> pd.DataFrame:
    """Fetch 1-minute OHLCV from Naver. Returns empty DataFrame on failure."""
    params = {
        "symbol": code,
        "requestType": "1",
        "startTime": start,
        "endTime": end,
        "timeframe": "minute",
    }
    resp = requests.get(NAVER_URL, params=params, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    text = resp.text.strip()
    # Naver returns a JS-style array literal with single quotes; sanitize to JSON
    text = re.sub(r"'", '"', text)
    text = re.sub(r"(\w+)\s*:", r'"\1":', text)  # in case of bare keys
    # The payload shape is: [[header...], [row...], ...]
    try:
        rows = json.loads(text)
    except json.JSONDecodeError:
        # Fall back: extract bracketed rows manually
        rows = json.loads(text.replace("\n", ""))
    if not rows or len(rows) < 2:
        return pd.DataFrame()

    header = [str(h).strip() for h in rows[0]]
    data = rows[1:]
    df = pd.DataFrame(data, columns=header)
    # Normalize column names
    rename = {
        "체결시간": "ts",
        "시가": "open",
        "고가": "high",
        "저가": "low",
        "종가": "close",
        "거래량": "volume",
    }
    df = df.rename(columns=rename)
    if "ts" not in df.columns:
        return pd.DataFrame()
    df["ts"] = df["ts"].astype(str)
    df["datetime"] = pd.to_datetime(df["ts"], format="%Y%m%d%H%M", errors="coerce")
    df = df.dropna(subset=["datetime"])
    df["date"] = df["datetime"].dt.strftime("%Y-%m-%d")
    df["time"] = df["datetime"].dt.strftime("%H%M%S")
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0).astype(int)
    return df.sort_values("datetime").reset_index(drop=True)


def attach_prev_close(df: pd.DataFrame) -> pd.DataFrame:
    """Compute prev_close for each day from the last close of the previous day."""
    if df.empty:
        return df
    daily_last = df.groupby("date")["close"].last().shift(1)
    df["prev_close"] = df["date"].map(daily_last).fillna(df["close"]).astype(int)
    return df


def save_csv(df: pd.DataFrame, code: str, name: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out["code"] = code
    out["name"] = name or code
    cols = ["date", "time", "datetime", "code", "name",
            "open", "high", "low", "close", "volume", "prev_close"]
    path = out_dir / f"{code}_minute.csv"
    out[cols].to_csv(path, index=False, encoding="utf-8-sig")
    return path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--codes", nargs="*", default=[], help="KRX codes, e.g. 005930 000660")
    p.add_argument("--codes-file", default="", help="Text file with one code per line (optional name after comma)")
    p.add_argument("--days", type=int, default=30, help="Lookback window in calendar days (Naver caps ~30 trading days)")
    p.add_argument("--out-dir", default="data/ohlcv")
    p.add_argument("--sleep", type=float, default=0.6, help="Polite delay between requests (seconds)")
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


def main() -> None:
    args = parse_args()
    pairs = load_codes(args)
    if not pairs:
        raise SystemExit("No codes provided. Use --codes or --codes-file.")

    end = datetime.now()
    start = end - timedelta(days=args.days)
    s_str = start.strftime("%Y%m%d")
    e_str = end.strftime("%Y%m%d")

    out_dir = Path(args.out_dir)
    print(f"Window: {s_str} ~ {e_str}  |  {len(pairs)} symbols  |  out={out_dir}")

    for i, (code, name) in enumerate(pairs, 1):
        try:
            df = fetch_naver_minute(code, s_str, e_str)
            if df.empty:
                print(f"  [{i}/{len(pairs)}] {code} {name}: no data")
                continue
            df = attach_prev_close(df)
            path = save_csv(df, code, name, out_dir)
            days = df["date"].nunique()
            print(f"  [{i}/{len(pairs)}] {code} {name}: rows={len(df)} days={days} -> {path}")
        except Exception as e:
            print(f"  [{i}/{len(pairs)}] {code} {name}: ERROR {e}")
        time.sleep(args.sleep)


if __name__ == "__main__":
    main()
