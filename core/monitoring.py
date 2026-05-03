"""
모니터링 / 알림
- 주문/체결/에러 로그 (파일)
- Slack / Telegram 알림
- 일일 손익 리포트
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from config.settings import settings

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    """로깅 설정 (콘솔 + 일별 파일 로테이션)"""
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # 콘솔
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # 파일 (일별 로테이션)
    log_dir = settings.LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    fh = logging.handlers.TimedRotatingFileHandler(
        log_dir / "auto_trade.log",
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


class TradeLogger:
    """체결/주문/에러를 JSON Lines 형식으로 저장"""

    def __init__(self) -> None:
        self._order_log = settings.LOG_DIR / "orders" / "orders.jsonl"
        self._trade_log = settings.LOG_DIR / "trades" / "trades.jsonl"
        self._error_log = settings.LOG_DIR / "errors" / "errors.jsonl"
        for p in (self._order_log, self._trade_log, self._error_log):
            p.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log_order(self, data: dict) -> None:
        self._append(self._order_log, data)

    def log_trade(self, data: dict) -> None:
        self._append(self._trade_log, data)

    def log_error(self, data: dict) -> None:
        self._append(self._error_log, data)

    def _append(self, path: Path, data: dict) -> None:
        record = {"ts": datetime.now().isoformat(), **data}
        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")


class Notifier:
    """Slack / Telegram 알림 발송"""

    def __init__(self) -> None:
        self._slack_url = settings.SLACK_WEBHOOK_URL
        self._tg_token = settings.TELEGRAM_BOT_TOKEN
        self._tg_chat = settings.TELEGRAM_CHAT_ID

    def send(self, message: str, level: str = "INFO") -> None:
        prefix = {"INFO": "ℹ️", "WARN": "⚠️", "ERROR": "🔴", "TRADE": "💹"}.get(level, "")
        text = f"{prefix} {message}"
        logger.info(f"[알림] {text}")
        if self._slack_url:
            self._send_slack(text)
        if self._tg_token and self._tg_chat:
            self._send_telegram(text)

    def _send_slack(self, text: str) -> None:
        try:
            requests.post(
                self._slack_url,
                json={"text": text},
                timeout=5,
            )
        except Exception as exc:
            logger.warning(f"Slack 알림 실패: {exc}")

    def _send_telegram(self, text: str) -> None:
        try:
            url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
            requests.post(
                url,
                json={"chat_id": self._tg_chat, "text": text},
                timeout=5,
            )
        except Exception as exc:
            logger.warning(f"Telegram 알림 실패: {exc}")

    def notify_trade(self, side: str, code: str, name: str, qty: int, price: float, pnl: Optional[float] = None) -> None:
        pnl_str = f" | 손익: {pnl:+,.0f}원" if pnl is not None else ""
        msg = f"{'매수' if side == 'BUY' else '매도'} | {name}({code}) {qty}주 @{price:,.0f}원{pnl_str}"
        self.send(msg, level="TRADE")

    def notify_risk_event(self, event: str) -> None:
        self.send(event, level="ERROR")

    def send_daily_report(self, summary: dict) -> None:
        msg = (
            f"[일일 리포트] {datetime.now().strftime('%Y-%m-%d')}\n"
            f"총 평가금액: {summary.get('total_eval', 0):,.0f}원\n"
            f"현금: {summary.get('cash', 0):,.0f}원\n"
            f"미실현손익: {summary.get('total_unrealized_pnl', 0):+,.0f}원\n"
            f"실현손익: {summary.get('total_realized_pnl', 0):+,.0f}원\n"
            f"총 수익률: {summary.get('total_return_rate', 0):+.2%}"
        )
        self.send(msg, level="INFO")
