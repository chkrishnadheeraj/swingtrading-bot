"""
Trade Journal - logs every trade to SQLite for review and analysis.
This is your most valuable asset after your capital.
"""

import sqlite3
from datetime import datetime
from typing import Optional
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
             quantity, confidence, reason, datetime.now().isoformat(), mode),
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
            hold_days = (datetime.now() - datetime.fromisoformat(entry_time)).days

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
             exit_reason, datetime.now().isoformat(), hold_days, trade_id),
        )
        conn.commit()
        conn.close()

        # Background sync to Notion
        self.notion.log_exit(
            trade_id=trade_id, exit_price=exit_price, pnl=pnl, pnl_pct=pnl_pct,
            exit_reason=exit_reason, exit_time=datetime.now().isoformat(), hold_days=hold_days
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
