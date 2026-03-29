"""
Telegram alerting for trade notifications.
Sends alerts on: trade entries, exits, SL hits, daily summary, kill switch triggers.
"""

import httpx
from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


class TelegramAlert:
    """Send trading alerts via Telegram bot."""

    def __init__(self):
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id)

        if not self.enabled:
            logger.warning("Telegram alerts disabled (no token/chat_id configured)")

    def send(self, message: str, parse_mode: str = "HTML"):
        """Send a message via Telegram. Fails silently if not configured."""
        if not self.enabled:
            return

        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            httpx.post(url, json={
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode,
            }, timeout=10)
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")

    def trade_entry(self, stock: str, price: float, qty: int, sl: float, target: float, reason: str):
        """Alert on new trade entry."""
        msg = (
            f"<b>NEW TRADE</b>\n"
            f"Stock: <code>{stock}</code>\n"
            f"Entry: ₹{price:,.2f} x {qty}\n"
            f"SL: ₹{sl:,.2f} | Target: ₹{target:,.2f}\n"
            f"Reason: {reason}"
        )
        self.send(msg)

    def trade_exit(self, stock: str, entry: float, exit_price: float, pnl: float, reason: str):
        """Alert on trade exit."""
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg = (
            f"{emoji} <b>TRADE CLOSED</b>\n"
            f"Stock: <code>{stock}</code>\n"
            f"Entry: ₹{entry:,.2f} -> Exit: ₹{exit_price:,.2f}\n"
            f"P&L: <b>₹{pnl:+,.0f}</b>\n"
            f"Reason: {reason}"
        )
        self.send(msg)

    def daily_summary(self, stats: dict):
        """Send end-of-day summary."""
        msg = (
            f"<b>DAILY SUMMARY</b>\n"
            f"Capital: ₹{stats.get('capital', 0):,.0f}\n"
            f"Daily P&L: ₹{stats.get('daily_pnl', 0):+,.0f}\n"
            f"Drawdown: {stats.get('drawdown', 0):.1%}\n"
            f"Open positions: {stats.get('open_positions', 0)}"
        )
        self.send(msg)

    def alert_halt(self, reason: str):
        """Critical alert when bot is halted."""
        msg = f"🚨 <b>BOT HALTED</b>\n\nReason: {reason}\n\nManual restart required."
        self.send(msg)
