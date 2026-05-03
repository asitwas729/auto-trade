from __future__ import annotations

from fetch_ohlcv import normalize_rows, resolve_dates


def test_resolve_dates_from_days():
    start_date, end_date = resolve_dates(None, "20250430", 30)
    assert start_date == "20250331"
    assert end_date == "20250430"


def test_normalize_rows_sorts_and_adds_metadata():
    rows = [
        {"date": "20250430", "open": 102, "high": 105, "low": 101, "close": 104, "volume": 1000, "amount": 100000},
        {"date": "20250429", "open": 100, "high": 103, "low": 99, "close": 102, "volume": 900, "amount": 90000},
    ]

    df = normalize_rows(rows, "005930", "삼성전자")

    assert list(df["date"].dt.strftime("%Y%m%d")) == ["20250429", "20250430"]
    assert set(["code", "name"]) <= set(df.columns)
    assert df.iloc[0]["code"] == "005930"
    assert df.iloc[0]["name"] == "삼성전자"
