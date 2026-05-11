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
    """로깅 설정 (콘솔 + 일별 파일 로테이션). LOG_LEVEL 환경변수로 레벨 제어."""
    import os
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)

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

    # 너무 시끄러운 외부 라이브러리는 WARNING으로 억제
    if level <= logging.DEBUG:
        for noisy in ("urllib3", "websocket", "requests"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


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
        date_str = datetime.now().strftime("%Y-%m-%d")
        lines = [f"[일일 리포트] {date_str}", ""]

        # ── 손익 요약 ──────────────────────────────────────────
        realized = summary.get("today_realized_pnl", 0.0)
        unrealized = summary.get("total_unrealized_pnl", 0.0)
        today_pnl = summary.get("today_pnl", 0.0)
        realized_rate = summary.get("today_realized_rate", 0.0)
        unrealized_rate = summary.get("today_unrealized_rate", 0.0)
        today_rate = summary.get("today_return_rate", 0.0)

        cum_rate = summary.get("total_return_rate", 0.0)
        initial = summary.get("initial_capital", 0.0)
        total_eval = summary.get("total_eval", 0.0)
        cum_pnl = total_eval - initial if initial > 0 else 0.0

        lines.append("【손익 요약】")
        lines.append(f"  실현손익     {realized:+,.0f}원  ({realized_rate:+.2%})")
        lines.append(f"  미실현손익   {unrealized:+,.0f}원  ({unrealized_rate:+.2%})")
        lines.append(f"  ─────────────────────────────")
        lines.append(f"  오늘 손익    {today_pnl:+,.0f}원  ({today_rate:+.2%})")
        lines.append(f"  누적 손익    {cum_pnl:+,.0f}원  ({cum_rate:+.2%})")

        # ── 매매 내역 ──────────────────────────────────────────
        sells = summary.get("today_realized_trades", []) or []
        positions = summary.get("positions_detail", []) or []
        buys_cnt = summary.get("today_buys_count", 0)
        wins = summary.get("today_wins", 0)
        losses = summary.get("today_losses", 0)

        if sells or positions:
            lines.append("")
            lines.append(
                f"【매매 내역】 (체결 {len(sells)}건, 승 {wins} / 패 {losses}"
                f" / 보유 {len(positions)})"
            )
            for t in sells:
                mark = "✅" if t["realized_pnl"] >= 0 else "❌"
                lines.append(
                    f"  {mark} {t['code']} [{t['strategy']}]  "
                    f"{t['realized_pnl']:+,.0f}원 ({t['realized_pnl_rate']:+.2%})  "
                    f"{t['qty']}주 @{t['price']:,.0f}원  {t['time']}"
                )
            for p in positions:
                lines.append(
                    f"  📌 {p['code']} [{p.get('strategy', '?')}]  "
                    f"{p['unrealized_pnl']:+,.0f}원 ({p['pnl_rate']:+.2%})  "
                    f"{p['quantity']}주 @{p['avg_price']:,.0f}원 → "
                    f"{p['current_price']:,}원 (보유중)"
                )
            if buys_cnt and not sells and not positions:
                lines.append(f"  (오늘 매수 {buys_cnt}건, 청산 없음)")

        # ── 포트폴리오 ──────────────────────────────────────────
        total_eval = summary.get("total_eval", 0.0)
        cash = summary.get("cash", 0.0)
        invested = summary.get("invested", 0.0)
        cash_ratio = summary.get("cash_ratio", 0.0)
        stock_ratio = summary.get("stock_ratio", 0.0)
        lines.append("")
        lines.append("【포트폴리오】")
        lines.append(f"  총 평가     {total_eval:>13,.0f}원")
        lines.append(f"  현금        {cash:>13,.0f}원  ({cash_ratio:.0%})")
        lines.append(f"  주식        {invested:>13,.0f}원  ({stock_ratio:.0%})")

        if initial > 0:
            lines.append("")
            lines.append(f"  (시작자본 {initial:,.0f}원)")

        msg = "\n".join(lines)
        self.send(msg, level="INFO")
