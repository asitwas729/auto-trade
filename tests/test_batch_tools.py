from __future__ import annotations

from pathlib import Path

import pandas as pd

from fetch_many_ohlcv import parse_code_items
from run_batch_backtest import summarize_result
from backtester.backtester import BTResult


def test_parse_code_items_from_text_file(tmp_path: Path):
    code_file = tmp_path / "codes.txt"
    code_file.write_text("005930,삼성전자\n000660,SK하이닉스\n005930,중복\n", encoding="utf-8")

    items = parse_code_items(None, str(code_file))

    assert items == [("005930", "삼성전자"), ("000660", "SK하이닉스")]


def test_parse_code_items_from_csv_file(tmp_path: Path):
    code_file = tmp_path / "codes.csv"
    pd.DataFrame(
        {
            "code": ["005930", "000660"],
            "name": ["삼성전자", "SK하이닉스"],
        }
    ).to_csv(code_file, index=False, encoding="utf-8-sig")

    items = parse_code_items(None, str(code_file))

    assert items == [("005930", "삼성전자"), ("000660", "SK하이닉스")]


def test_summarize_result_fields():
    result = BTResult(
        strategy="S3",
        total_trades=3,
        win_rate=2 / 3,
        total_pnl=12345.0,
        max_drawdown=-0.05,
        sharpe_ratio=1.2,
    )

    summary = summarize_result("005930", "삼성전자", result, 10_000_000)

    assert summary["code"] == "005930"
    assert summary["strategy"] == "S3"
    assert summary["total_return"] == 12345.0 / 10_000_000
