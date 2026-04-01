"""
Trade Journal - logs every trade to SQLite for review and analysis.
This is your most valuable asset after your capital.
"""

import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

IST = timezone(timedelta(hours=5, minutes=30))

def _now_ist() -> str:
    """Return current IST datetime as ISO-8601 string with +05:30 offset."""
    return datetime.now(IST).isoformat()
from config import settings
from utils.logger import get_logger
from utils.notion_logger import NotionLogger
from utils.tax_calculator import net_pnl as calc_net_pnl

logger = get_logger(__name__)


class TradeJournal:
    """Persistent trade journal backed by SQLite & Notion."""

    def __init__(self):
        self.db_path = settings.TRADES_DB
        self.notion = NotionLogger()
        self._init_db()

    def _init_db(self):
        """Create trades table if it doesn't exist."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock TEXT NOT NULL,
                action TEXT NOT NULL,
                strategy TEXT,
                entry_price REAL,
                exit_price REAL,
                stop_loss REAL,
                target_price REAL,
                quantity INTEGER,
                pnl REAL,
                pnl_pct REAL,
                gross_pnl REAL,
                total_charges REAL,
                tax_deducted REAL,
                net_in_hand REAL,
                confidence REAL,
                reason TEXT,
                exit_reason TEXT,
                entry_time TEXT,
                exit_time TEXT,
                hold_days INTEGER,
                mode TEXT DEFAULT 'paper',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migrate existing databases that predate the tax columns
        for col, coltype in [
            ("gross_pnl",      "REAL"),
            ("total_charges",  "REAL"),
            ("tax_deducted",   "REAL"),
            ("net_in_hand",    "REAL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {coltype}")
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                stock         TEXT    NOT NULL,
                strategy      TEXT,
                entry_price   REAL,
                stop_loss     REAL,
                target_price  REAL,
                confidence    REAL,
                reason        TEXT,
                status        TEXT    NOT NULL,
                reject_reason TEXT,
                trade_id      INTEGER,
                scan_time     TEXT    NOT NULL,
                mode          TEXT    DEFAULT 'paper'
            )
        """)
        conn.commit()
        conn.close()
        logger.info(f"Trade journal initialized at {self.db_path}")

    def log_entry(
        self,
        stock: str,
        entry_price: float,
        stop_loss: float,
        target_price: float,
        quantity: int,
        strategy: str,
        confidence: float,
        reason: str,
        mode: str = "paper",
    ) -> int:
        """Log a new trade entry. Returns the trade ID."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            """INSERT INTO trades
            (stock, action, strategy, entry_price, stop_loss, target_price,
             quantity, confidence, reason, entry_time, mode)
            VALUES (?, 'BUY', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (stock, strategy, entry_price, stop_loss, target_price,
             quantity, confidence, reason, _now_ist(), mode),
        )
        trade_id = cursor.lastrowid
        conn.commit()
        conn.close()

        logger.info(f"Journal: ENTRY #{trade_id} | {stock} @ ₹{entry_price} | Qty: {quantity} | {reason}")
        
        # Background sync to Notion
        self.notion.log_entry(
            trade_id=trade_id, stock=stock, action="BUY", entry_price=entry_price,
            quantity=quantity, stop_loss=stop_loss, target_price=target_price,
            strategy=strategy, reason=reason, mode=mode
        )
        
        return trade_id

    def log_exit(
        self,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
    ):
        """Log a trade exit and calculate P&L."""
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT entry_price, quantity, entry_time FROM trades WHERE id = ?",
            (trade_id,),
        ).fetchone()

        if not row:
            logger.error(f"Trade #{trade_id} not found in journal")
            return

        entry_price, quantity, entry_time = row
        pnl     = (exit_price - entry_price) * quantity
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100

        hold_days = 0
        if entry_time:
            hold_days = (datetime.now(IST) - datetime.fromisoformat(entry_time).astimezone(IST)).days

        # Tax & charges breakdown
        tax_data       = calc_net_pnl(entry_price, exit_price, quantity)
        gross_pnl      = tax_data["gross_pnl"]
        total_charges  = tax_data["total_charges"]
        tax_deducted   = tax_data["tax_deducted"]
        net_in_hand    = tax_data["net_in_hand"]

        conn.execute(
            """UPDATE trades SET
                action = 'CLOSED', exit_price = ?, pnl = ?, pnl_pct = ?,
                gross_pnl = ?, total_charges = ?, tax_deducted = ?, net_in_hand = ?,
                exit_reason = ?, exit_time = ?, hold_days = ?
            WHERE id = ?""",
            (exit_price, pnl, pnl_pct,
             gross_pnl, total_charges, tax_deducted, net_in_hand,
             exit_reason, _now_ist(), hold_days, trade_id),
        )
        conn.commit()
        conn.close()

        # Background sync to Notion
        self.notion.log_exit(
            trade_id=trade_id, exit_price=exit_price, pnl=pnl, pnl_pct=pnl_pct,
            exit_reason=exit_reason, exit_time=_now_ist(), hold_days=hold_days
        )

        emoji = "+" if pnl >= 0 else ""
        logger.info(
            f"Journal: EXIT #{trade_id} | @ ₹{exit_price} | "
            f"Gross P&L: {emoji}₹{gross_pnl:,.0f} | Charges: ₹{total_charges:,.0f} | "
            f"Tax ({tax_data['tax_type']}): ₹{tax_deducted:,.0f} | "
            f"Net in-hand: ₹{net_in_hand:+,.0f} | "
            f"Held: {hold_days}d | {exit_reason}"
        )

    def get_stats(self, days: int = 30, mode: str = "paper") -> dict:
        """Calculate trading statistics for the last N days."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            """SELECT pnl, pnl_pct FROM trades
            WHERE action = 'CLOSED' AND mode = ?
            AND exit_time >= datetime('now', ?)""",
            (mode, f"-{days} days"),
        ).fetchall()
        conn.close()

        if not rows:
            return {"total_trades": 0}

        pnls = [r[0] for r in rows if r[0] is not None]
        pnl_pcts = [r[1] for r in rows if r[1] is not None]

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        return {
            "total_trades": len(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(pnls) if pnls else 0,
            "total_pnl": sum(pnls),
            "avg_win": sum(wins) / len(wins) if wins else 0,
            "avg_loss": sum(losses) / len(losses) if losses else 0,
            "best_trade": max(pnls) if pnls else 0,
            "worst_trade": min(pnls) if pnls else 0,
            "avg_pnl_pct": sum(pnl_pcts) / len(pnl_pcts) if pnl_pcts else 0,
        }

    def print_summary(self, days: int = 30, mode: str = "paper"):
        """Print a formatted summary of recent trading stats."""
        stats = self.get_stats(days, mode)
        if stats["total_trades"] == 0:
            logger.info(f"No closed trades in last {days} days ({mode} mode)")
            return

        logger.info(f"\n{'='*50}")
        logger.info(f"  Trading Summary - Last {days} days ({mode})")
        logger.info(f"{'='*50}")
        logger.info(f"  Total trades:  {stats['total_trades']}")
        logger.info(f"  Wins/Losses:   {stats['wins']}/{stats['losses']}")
        logger.info(f"  Win rate:      {stats['win_rate']:.1%}")
        logger.info(f"  Total P&L:     ₹{stats['total_pnl']:+,.0f}")
        logger.info(f"  Avg win:       ₹{stats['avg_win']:+,.0f}")
        logger.info(f"  Avg loss:      ₹{stats['avg_loss']:+,.0f}")
        logger.info(f"  Best trade:    ₹{stats['best_trade']:+,.0f}")
        logger.info(f"  Worst trade:   ₹{stats['worst_trade']:+,.0f}")
        logger.info(f"{'='*50}\n")

    # ── Daily Summary ──────────────────────────────────────────────────────

    def log_signal(
        self,
        stock: str,
        strategy: str,
        entry_price: float,
        stop_loss: float,
        target_price: float,
        confidence: float,
        reason: str,
        status: str,
        reject_reason: Optional[str] = None,
        trade_id: Optional[int] = None,
        mode: str = "paper",
    ) -> None:
        """Persist a generated signal and its outcome to the signals table."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """INSERT INTO signals
                (stock, strategy, entry_price, stop_loss, target_price,
                 confidence, reason, status, reject_reason, trade_id, scan_time, mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (stock, strategy, entry_price, stop_loss, target_price,
                 confidence, reason, status, reject_reason, trade_id, _now_ist(), mode),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to log signal for {stock}: {e}")

    def get_today_signals(self, mode: str = "paper") -> Dict[str, Any]:
        """Return today's signals grouped by status (IST date boundary)."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            """SELECT stock, strategy, entry_price, stop_loss, target_price,
                      confidence, reason, status, reject_reason, trade_id, scan_time
               FROM   signals
               WHERE  mode = ?
               AND    DATE(scan_time) = DATE('now', '+5:30')
               ORDER  BY scan_time ASC""",
            (mode,),
        ).fetchall()
        conn.close()

        keys = ("stock", "strategy", "entry_price", "stop_loss", "target_price",
                "confidence", "reason", "status", "reject_reason", "trade_id", "scan_time")
        executed, skipped_existing, skipped_no_slot, rejected = [], [], [], []
        for row in rows:
            r = dict(zip(keys, row))
            if r["status"] == "EXECUTED":
                executed.append(r)
            elif r["status"] == "SKIPPED_EXISTING":
                skipped_existing.append(r)
            elif r["status"] == "SKIPPED_NO_SLOT":
                skipped_no_slot.append(r)
            else:
                rejected.append(r)

        return {
            "executed":         executed,
            "skipped_existing": skipped_existing,
            "skipped_no_slot":  skipped_no_slot,
            "rejected":         rejected,
            "total_scanned":    len(rows),
        }

    def get_today_trades(self, mode: str = "paper") -> Dict[str, List]:
        """Return today's trade entries, exits, and currently open positions."""
        conn = sqlite3.connect(self.db_path)

        entries = conn.execute(
            """SELECT stock, entry_price, stop_loss, target_price, quantity, strategy
               FROM   trades
               WHERE  action = 'BUY' AND mode = ?
               AND    DATE(entry_time) = DATE('now', '+5:30')""",
            (mode,),
        ).fetchall()

        exits = conn.execute(
            """SELECT stock, entry_price, exit_price, pnl, exit_reason
               FROM   trades
               WHERE  action = 'CLOSED' AND mode = ?
               AND    DATE(exit_time) = DATE('now', '+5:30')""",
            (mode,),
        ).fetchall()

        open_pos = conn.execute(
            """SELECT stock, entry_price, stop_loss, target_price, quantity
               FROM   trades
               WHERE  action = 'BUY' AND mode = ?
               AND    exit_time IS NULL""",
            (mode,),
        ).fetchall()

        conn.close()

        return {
            "entries": [dict(zip(("stock","entry_price","stop_loss","target_price","quantity","strategy"), r)) for r in entries],
            "exits":   [dict(zip(("stock","entry_price","exit_price","pnl","exit_reason"), r)) for r in exits],
            "open":    [dict(zip(("stock","entry_price","stop_loss","target_price","quantity"), r)) for r in open_pos],
        }

    def print_daily_summary(self, risk_status: dict, mode: str = "paper") -> dict:
        """Print a rich daily summary and return the data dict for Notion/Telegram."""
        signals = self.get_today_signals(mode)
        trades  = self.get_today_trades(mode)

        capital   = risk_status.get("capital", 0)
        daily_pnl = risk_status.get("daily_pnl", 0)
        drawdown  = risk_status.get("drawdown", 0)
        date_str  = datetime.now(IST).strftime("%d %b %Y")

        sep = "=" * 60
        logger.info(f"\n{sep}")
        logger.info(f"  DAILY TRADE SUMMARY — {date_str} ({mode.upper()} MODE)")
        logger.info(sep)

        # Entries
        entries = trades["entries"]
        logger.info(f"\nENTRIES TODAY ({len(entries)})")
        if entries:
            for e in entries:
                logger.info(
                    f"  {e['stock']:<12} BUY ₹{e['entry_price']:,.2f} x{e['quantity']}"
                    f" | SL ₹{e['stop_loss']:,.2f} | T ₹{e['target_price']:,.2f}"
                    f" | {e['strategy']}"
                )
        else:
            logger.info("  (none)")

        # Exits
        exits = trades["exits"]
        logger.info(f"\nEXITS TODAY ({len(exits)})")
        if exits:
            for x in exits:
                pnl_sign = "+" if x["pnl"] >= 0 else ""
                logger.info(
                    f"  {x['stock']:<12} ₹{x['entry_price']:,.2f}→₹{x['exit_price']:,.2f}"
                    f" | P&L: {pnl_sign}₹{x['pnl']:,.0f} | {x['exit_reason']}"
                )
        else:
            logger.info("  (none)")

        # Open positions
        open_pos = trades["open"]
        logger.info(f"\nOPEN POSITIONS ({len(open_pos)})")
        if open_pos:
            for p in open_pos:
                logger.info(
                    f"  {p['stock']:<12} Entry ₹{p['entry_price']:,.2f}"
                    f" | SL ₹{p['stop_loss']:,.2f} | Target ₹{p['target_price']:,.2f}"
                    f" | x{p['quantity']}"
                )
        else:
            logger.info("  (none)")

        # Non-executed signals
        total_not_exec = (len(signals["skipped_existing"]) +
                          len(signals["skipped_no_slot"]) +
                          len(signals["rejected"]))
        logger.info(f"\nSIGNALS NOT EXECUTED ({total_not_exec})")
        if signals["skipped_existing"]:
            names = ", ".join(s["stock"] for s in signals["skipped_existing"])
            logger.info(f"  SKIPPED (existing position) : {names}")
        if signals["skipped_no_slot"]:
            names = ", ".join(s["stock"] for s in signals["skipped_no_slot"])
            logger.info(f"  SKIPPED (no slot)           : {names}")
        if signals["rejected"]:
            logger.info("  REJECTED:")
            for r in signals["rejected"]:
                logger.info(f"    {r['stock']:<12} — {r['reject_reason']}")
        if total_not_exec == 0 and signals["total_scanned"] == 0:
            logger.info("  (no signals generated today)")

        pnl_sign = "+" if daily_pnl >= 0 else ""
        logger.info(
            f"\nCapital: ₹{capital:,.0f} | Daily P&L: {pnl_sign}₹{daily_pnl:,.0f}"
            f" | Drawdown: {drawdown:.1%}"
        )
        logger.info(f"{sep}\n")

        return {
            "date":      date_str,
            "mode":      mode,
            "capital":   capital,
            "daily_pnl": daily_pnl,
            "drawdown":  drawdown,
            "entries":   entries,
            "exits":     exits,
            "open":      open_pos,
            "signals":   signals,
        }
