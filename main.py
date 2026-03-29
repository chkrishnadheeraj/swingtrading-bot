"""
Main entry point for the trading bot.

Usage:
    python main.py --mode paper              # Paper trading (default)
    python main.py --mode live               # Live trading
    python main.py --mode paper --scan-now   # Run one scan immediately
    python main.py --stats                   # Show trading statistics
"""

import argparse
import schedule
import time
from datetime import datetime

from config import settings
from core.engine import TradingEngine
from utils.logger import get_logger

logger = get_logger("main")


def parse_args():
    parser = argparse.ArgumentParser(description="Momentum Swing Trading Bot - NSE")
    parser.add_argument(
        "--mode", choices=["paper", "live"], default="paper",
        help="Trading mode (default: paper)"
    )
    parser.add_argument(
        "--max-position", type=float, default=None,
        help="Max position size in INR (e.g., 3000 for micro-trading)"
    )
    parser.add_argument(
        "--scan-now", action="store_true",
        help="Run one scan immediately and exit"
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Show trading statistics and exit"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    logger.info(f"""
    ╔══════════════════════════════════════════╗
    ║   Momentum Swing Bot - NSE              ║
    ║   Mode: {args.mode.upper():<10}                      ║
    ║   Capital: ₹{settings.INITIAL_CAPITAL:>10,.0f}                ║
    ╚══════════════════════════════════════════╝
    """)

    engine = TradingEngine(
        mode=args.mode,
        max_position=args.max_position,
    )

    # Show stats and exit
    if args.stats:
        engine.journal.print_summary(days=30, mode=args.mode)
        return

    # Run one scan and exit
    if args.scan_now:
        logger.info("Running single scan cycle...")
        engine.run_scan_cycle()
        engine.journal.print_summary(days=7, mode=args.mode)
        return

    # Safety check for live mode
    if args.mode == "live":
        logger.warning("=" * 50)
        logger.warning("  LIVE TRADING MODE - REAL MONEY AT RISK")
        logger.warning("=" * 50)
        confirm = input("\nType 'I UNDERSTAND THE RISKS' to proceed: ")
        if confirm != "I UNDERSTAND THE RISKS":
            logger.info("Aborted. Use --mode paper for paper trading.")
            return

    # Schedule daily scan
    # Pre-market scan at 9:00 AM
    schedule.every().day.at(settings.PRE_MARKET_SCAN).do(engine.run_scan_cycle)

    # Mid-day exit check at 1:00 PM
    schedule.every().day.at("13:00").do(engine._check_exits)

    # End-of-day at 3:45 PM
    schedule.every().day.at("15:45").do(engine.end_of_day)

    logger.info(f"Scheduled scans: {settings.PRE_MARKET_SCAN} (scan), 13:00 (exits), 15:45 (EOD)")
    logger.info("Bot running. Press Ctrl+C to stop.\n")

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("\nBot stopped by user")
        engine.end_of_day()


if __name__ == "__main__":
    main()
