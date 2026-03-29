import sys
import time
from utils.notion_logger import NotionLogger
from utils.logger import get_logger

logger = get_logger("test_notion")

notion = NotionLogger()
if not notion.enabled:
    logger.error("Notion logger is disabled. Check API Key and DB ID.")
    sys.exit(1)

logger.info("Attempting to insert a mock trade row...")
notion._create_entry_page(
    trade_id=999,
    stock="TESTMOCK",
    action="BUY",
    entry_price=150.5,
    quantity=10,
    stop_loss=140.0,
    target_price=170.0,
    strategy="TestStrategy",
    reason="Mock trade to verify Notion integration",
    mode="PAPER"
)
logger.info("✅ Entry inserted successfully. Check Notion!")

time.sleep(2)

logger.info("Attempting to update the mock trade row to CLOSED...")
notion._update_exit_page(
    trade_id=999,
    exit_price=175.0,
    pnl=245.0,
    pnl_pct=16.27,
    exit_reason="Target Hit Test",
    exit_time="2026-03-29T15:30:00",
    hold_days=1
)
logger.info("✅ Row updated to WIN successfully. Check Notion!")
