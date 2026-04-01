"""
Notion API Integration for trade journaling.
Automatically syncs trades to a Notion Database.
"""

import threading
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> str:
    """Return current time as ISO-8601 string with +05:30 offset."""
    return datetime.now(IST).isoformat()

import notion_client

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


class NotionLogger:
    def __init__(self):
        self.api_key = getattr(settings, "NOTION_API_KEY", None)
        self.db_id = getattr(settings, "NOTION_TRADES_DB_ID", None)
        
        self.enabled = bool(self.api_key and self.db_id)
        
        if self.enabled:
            try:
                self.client = notion_client.Client(auth=self.api_key)
                logger.info("Notion logger initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize Notion client: {e}")
                self.enabled = False
        else:
            logger.info("Notion logger disabled (missing API_KEY or DB_ID)")

    def _run_async(self, func, *args, **kwargs):
        """Run a network task in a background thread to prevent blocking trading engine."""
        if not self.enabled:
            return
            
        thread = threading.Thread(target=self._safe_execute, args=(func,) + args, kwargs=kwargs)
        thread.daemon = True
        thread.start()

    def _safe_execute(self, func, *args, **kwargs):
        try:
            func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Notion API error: {e}")

    def log_entry(self, trade_id: int, stock: str, action: str, entry_price: float, 
                  quantity: int, stop_loss: float, target_price: float, 
                  strategy: str, reason: str, mode: str):
        """Dispatches an async call to create a new page in the Notion DB."""
        if not self.enabled:
            return
            
        self._run_async(self._create_entry_page, trade_id, stock, action, entry_price, 
                        quantity, stop_loss, target_price, strategy, reason, mode)

    def _create_entry_page(self, trade_id: int, stock: str, action: str, entry_price: float, 
                           quantity: int, stop_loss: float, target_price: float, 
                           strategy: str, reason: str, mode: str):
        """Sync blocks creating a new Notion page (row)."""
        properties = {
            "ID": {"title": [{"text": {"content": f"#{trade_id}"}}]},
            "Stock": {"rich_text": [{"text": {"content": stock}}]},
            "Status": {"select": {"name": "OPEN", "color": "blue"}},
            "Action": {"select": {"name": action}},
            "Quantity": {"number": quantity},
            "Entry Price": {"number": entry_price},
            "Stop Loss": {"number": round(stop_loss, 2) if stop_loss else None},
            "Target Price": {"number": round(target_price, 2) if target_price else None},
            "Strategy": {"select": {"name": strategy}},
            "Entry Time": {"date": {"start": _now_ist()}},
            "Reason": {"rich_text": [{"text": {"content": reason}}]},
            "Mode": {"select": {"name": mode.upper()}},
        }
        
        # Risk amount
        if stop_loss > 0:
            risk = (entry_price - stop_loss) * quantity
            properties["Value at Risk"] = {"number": round(risk, 2)}
            
        self.client.pages.create(
            parent={"database_id": self.db_id},
            properties=properties
        )

    def log_exit(self, trade_id: int, exit_price: float, pnl: float, pnl_pct: float, 
                 exit_reason: str, exit_time: str, hold_days: int):
        """Dispatches an async call to update the Notion page."""
        if not self.enabled:
            return
            
        self._run_async(self._update_exit_page, trade_id, exit_price, pnl, pnl_pct, 
                        exit_reason, exit_time, hold_days)

    def _update_exit_page(self, trade_id: int, exit_price: float, pnl: float, pnl_pct: float,
                          exit_reason: str, exit_time: str, hold_days: int):
        """Find the page by ID and update it."""
        # 1. Query the database to find the row with the matching ID
        results = self.client.databases.query(
            database_id=self.db_id,
            filter={"property": "ID", "title": {"equals": f"#{trade_id}"}}
        )

        if not results.get("results"):
            logger.warning(f"Could not find trade #{trade_id} in Notion database")
            return

        page_id = results["results"][0]["id"]

        # 2. Update the page
        status = "WIN" if pnl > 0 else "LOSS"
        color = "green" if pnl > 0 else "red"

        properties = {
            "Status": {"select": {"name": status, "color": color}},
            "Exit Price": {"number": round(exit_price, 2)},
            "P&L": {"number": round(pnl, 2)},
            "P&L %": {"number": round(pnl_pct / 100, 4)}, # Notion takes decimal for %
            "Exit Reason": {"rich_text": [{"text": {"content": exit_reason}}]},
            "Exit Time": {"date": {"start": exit_time}},
            "Hold Days": {"number": hold_days},
        }

        self.client.pages.update(page_id=page_id, properties=properties)

    # ── Pre-Market Pulse ───────────────────────────────────────────────────

    def log_pulse(self, verdict: str, score: int, vix: float | None,
                  reasons: list[str], headlines: list[tuple[str, str]]):
        """
        Logs the daily pre-market pulse verdict to the Notion trades DB.
        Uses the existing schema — no new database required.

        Mapping:
          ID            → PULSE-YYYY-MM-DD
          Stock         → MARKET-PULSE
          Status        → GO / CAUTION / NO-GO  (colour-coded)
          Strategy      → PULSE
          Mode          → SYSTEM
          Entry Price   → India VIX value
          Quantity      → Pulse score
          Reason        → Verdict summary + key signals + top headlines

        Called synchronously (not via _run_async) because premarket_pulse.py
        is a short-lived script that exits immediately after — a daemon thread
        would be killed before the API call completes.
        """
        if not self.enabled:
            return
        self._safe_execute(self._create_pulse_page, verdict, score, vix, reasons, headlines)

    def _create_pulse_page(self, verdict: str, score: int, vix: float | None,
                           reasons: list[str], headlines: list[tuple[str, str]]):
        date_str = datetime.now(IST).strftime("%Y-%m-%d")

        # Strip ANSI escape codes from reason strings
        import re
        _ansi = re.compile(r"\033\[[0-9;]*m")
        clean_reasons = [_ansi.sub("", r) for r in reasons]

        # Build a compact summary for the Reason field (2000 char Notion limit)
        top_headlines = [f"[{src}] {hl}" for src, hl in headlines[:6]]
        summary_parts = clean_reasons + ["", "Headlines:"] + top_headlines
        summary = "\n".join(summary_parts)[:1999]

        if "NO-GO" in verdict:
            status, color = "NO-GO",   "red"
        elif "CAUTION" in verdict:
            status, color = "CAUTION", "yellow"
        else:
            status, color = "GO",      "green"

        properties = {
            "ID":       {"title":     [{"text": {"content": f"PULSE-{date_str}"}}]},
            "Stock":    {"rich_text": [{"text": {"content": "MARKET-PULSE"}}]},
            "Status":   {"select":    {"name": status, "color": color}},
            "Strategy": {"select":    {"name": "PULSE"}},
            "Mode":     {"select":    {"name": "SYSTEM"}},
            "Quantity": {"number":    score},
            "Reason":   {"rich_text": [{"text": {"content": summary}}]},
            "Entry Time": {"date":    {"start": _now_ist()}},
        }
        if vix is not None:
            properties["Entry Price"] = {"number": round(vix, 2)}

        self.client.pages.create(
            parent={"database_id": self.db_id},
            properties=properties,
        )

    # ── Daily Summary ──────────────────────────────────────────────────────

    def log_daily_summary(self, summary: dict) -> None:
        """
        Creates a rich Notion page for the end-of-day trade summary.
        Called synchronously (like log_pulse) so it completes before process exit.
        """
        if not self.enabled:
            return
        self._safe_execute(self._create_summary_page, summary)

    def _create_summary_page(self, summary: dict) -> None:
        """Build and create the daily summary page with rich block content."""
        date_str  = datetime.now(IST).strftime("%Y-%m-%d")
        date_disp = datetime.now(IST).strftime("%d %b %Y")
        pnl       = summary.get("daily_pnl", 0)
        capital   = summary.get("capital", 0)
        drawdown  = summary.get("drawdown", 0)
        mode      = summary.get("mode", "paper")
        signals   = summary.get("signals", {})
        entries   = summary.get("entries", [])
        exits     = summary.get("exits", [])
        open_pos  = summary.get("open", [])

        status = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "NEUTRAL")
        color  = "green" if pnl > 0 else ("red" if pnl < 0 else "gray")
        pnl_sign = "+" if pnl >= 0 else ""

        overview = (
            f"Scanned {signals.get('total_scanned', 0)} signals | "
            f"Executed {len(signals.get('executed', []))} | "
            f"P&L {pnl_sign}₹{pnl:,.0f}"
        )

        properties = {
            "ID":         {"title":     [{"text": {"content": f"SUMMARY-{date_str}"}}]},
            "Stock":      {"rich_text": [{"text": {"content": "DAILY-SUMMARY"}}]},
            "Status":     {"select":    {"name": status, "color": color}},
            "Strategy":   {"select":    {"name": "SUMMARY"}},
            "Mode":       {"select":    {"name": mode.upper()}},
            "Entry Time": {"date":      {"start": _now_ist()}},
            "P&L":        {"number":    round(pnl, 2)},
            "Reason":     {"rich_text": [{"text": {"content": overview}}]},
        }

        # ── Block helpers ──────────────────────────────────────────────────

        def _h(level: int, text: str) -> dict:
            return {"object": "block", "type": f"heading_{level}",
                    f"heading_{level}": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

        def _bullet(text: str) -> dict:
            return {"object": "block", "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

        def _para(text: str) -> dict:
            return {"object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

        def _divider() -> dict:
            return {"object": "block", "type": "divider", "divider": {}}

        def _callout(text: str, emoji: str = "💰") -> dict:
            return {"object": "block", "type": "callout",
                    "callout": {"rich_text": [{"type": "text", "text": {"content": text}}],
                                "icon": {"type": "emoji", "emoji": emoji}}}

        # ── Build page body ────────────────────────────────────────────────

        blocks = [
            _h(1, f"Daily Trade Summary — {date_disp}"),
            _callout(
                f"Capital: ₹{capital:,.0f}  |  P&L: {pnl_sign}₹{pnl:,.0f}"
                f"  |  Drawdown: {drawdown:.1%}  |  Mode: {mode.upper()}"
            ),
            _divider(),
        ]

        # Executed trades
        exec_count = len(entries) + len(exits)
        blocks.append(_h(2, f"✅ Executed Today ({exec_count})"))
        if entries:
            blocks.append(_para("Entries:"))
            for e in entries:
                blocks.append(_bullet(
                    f"{e['stock']} — BUY ₹{e['entry_price']:,.2f} x{e['quantity']}"
                    f" | SL ₹{e['stop_loss']:,.2f} | Target ₹{e['target_price']:,.2f}"
                    f" | {e['strategy']}"
                ))
        if exits:
            blocks.append(_para("Exits:"))
            for x in exits:
                pnl_s = "+" if x["pnl"] >= 0 else ""
                blocks.append(_bullet(
                    f"{x['stock']} — CLOSED ₹{x['entry_price']:,.2f}→₹{x['exit_price']:,.2f}"
                    f" | P&L: {pnl_s}₹{x['pnl']:,.0f} | {x['exit_reason']}"
                ))
        if not entries and not exits:
            blocks.append(_para("No trades executed today."))

        # Open positions
        blocks.append(_divider())
        blocks.append(_h(2, f"📂 Open Positions ({len(open_pos)})"))
        if open_pos:
            for p in open_pos:
                blocks.append(_bullet(
                    f"{p['stock']} — Entry ₹{p['entry_price']:,.2f}"
                    f" | SL ₹{p['stop_loss']:,.2f} | Target ₹{p['target_price']:,.2f}"
                    f" | x{p['quantity']}"
                ))
        else:
            blocks.append(_para("No open positions."))

        # Signal funnel
        blocks.append(_divider())
        blocks.append(_h(2, "🔍 Signal Funnel"))
        total   = signals.get("total_scanned", 0)
        n_exec  = len(signals.get("executed", []))
        n_skip  = len(signals.get("skipped_existing", [])) + len(signals.get("skipped_no_slot", []))
        n_rej   = len(signals.get("rejected", []))
        blocks.append(_para(
            f"Scanned: {total}  |  Executed: {n_exec}"
            f"  |  Skipped: {n_skip}  |  Rejected: {n_rej}"
        ))

        if signals.get("skipped_existing"):
            blocks.append(_h(3, "Skipped — Existing Position Open"))
            for s in signals["skipped_existing"]:
                blocks.append(_bullet(f"{s['stock']} @ ₹{s['entry_price']:,.2f}"))

        if signals.get("skipped_no_slot"):
            blocks.append(_h(3, "Skipped — No Position Slot Available"))
            for s in signals["skipped_no_slot"]:
                blocks.append(_bullet(f"{s['stock']} @ ₹{s['entry_price']:,.2f}"))

        if signals.get("rejected"):
            blocks.append(_h(3, "❌ Rejected"))
            for r in signals["rejected"]:
                blocks.append(_bullet(f"{r['stock']} — {r['reject_reason']}"))

        if total == 0:
            blocks.append(_para("No signals generated today."))

        self.client.pages.create(
            parent={"database_id": self.db_id},
            properties=properties,
            children=blocks,
        )

