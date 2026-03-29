"""
Notion API Integration for trade journaling.
Automatically syncs trades to a Notion Database.
"""

import threading
from typing import Optional, Dict, Any
from datetime import datetime

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
            "Entry Time": {"date": {"start": datetime.now().isoformat()}},
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

