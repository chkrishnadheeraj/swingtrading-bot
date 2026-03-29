"""
Central configuration for the trading bot.
All tunable parameters live here. Change with caution.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

# ---------------------------------------------------------------------------
# Broker credentials
# ---------------------------------------------------------------------------
KITE_API_KEY = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")
KITE_USER_ID = os.getenv("KITE_USER_ID", "")

# ---------------------------------------------------------------------------
# Telegram alerts
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Claude API (Phase 2)
# ---------------------------------------------------------------------------
# Deepmind/Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Notion Settings
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
NOTION_TRADES_DB_ID = os.getenv("NOTION_TRADES_DB_ID", "")

# ---------------------------------------------------------------------------
# Trading mode
# ---------------------------------------------------------------------------
TRADING_MODE = os.getenv("TRADING_MODE", "paper")  # "paper" or "live"

# ---------------------------------------------------------------------------
# Capital & risk management
# ---------------------------------------------------------------------------
INITIAL_CAPITAL = 15_000  # Starting capital in INR

# Per-trade risk: max percentage of total capital to risk on a single trade
MAX_RISK_PER_TRADE = 0.02  # 2%

# Maximum number of concurrent open positions
MAX_POSITIONS = 3

# Maximum percentage of capital in any single position
MAX_POSITION_PCT = 0.20  # 20%

# Daily loss limit: auto-halt trading if daily P&L drops below this
DAILY_LOSS_LIMIT = 0.03  # 3% of current capital

# Maximum drawdown from equity peak: kill switch
MAX_DRAWDOWN = 0.15  # 15%

# Minimum risk:reward ratio to take a trade
MIN_RISK_REWARD = 2.0  # 1:2

# ---------------------------------------------------------------------------
# Strategy parameters: Momentum
# ---------------------------------------------------------------------------
MOMENTUM = {
    # EMA periods for crossover signal
    "fast_ema": 9,
    "slow_ema": 21,

    # Higher-timeframe trend filter: price must be above this EMA to take any long
    # Prevents counter-trend entries during broader downtrends (Fix #2)
    "trend_ema": 50,

    # RSI parameters
    "rsi_period": 14,
    "rsi_oversold": 35,      # Buy signal below this (on reversal)
    "rsi_overbought": 70,    # Avoid new longs above this

    # Volume confirmation: current volume must be this multiple of 20-day avg
    "volume_multiplier": 1.5,

    # Holding period — widened to 10 days to let trades breathe (Fix #1)
    "min_hold_days": 1,
    "max_hold_days": 10,

    # Stop loss parameters (Fix #1)
    "initial_sl_pct": 0.03,           # 3% hard stop from entry
    "trailing_sl_pct": 0.04,          # 4% trailing SL (was 2% — too tight)
    "trailing_sl_activation": 0.02,   # Only activate trailing SL after +2% gain

    # Target profit percentage — raised to 8% to match wider SL and R:R target
    "target_pct": 0.08,      # 8% target (was 6%)
}

# ---------------------------------------------------------------------------
# Strategy parameters: Pairs Trading (Phase 2)
# ---------------------------------------------------------------------------
PAIRS = {
    # Correlated stock pairs to monitor
    "pairs": [
        ("SBIN", "PNB"),
        ("TCS", "INFY"),
        ("HDFCBANK", "ICICIBANK"),
        ("BHARTIARTL", "IDEA"),
        ("RELIANCE", "ONGC"),
    ],

    # Z-score threshold for entry
    "entry_zscore": 2.0,

    # Z-score threshold for exit (mean reversion)
    "exit_zscore": 0.5,

    # Lookback period for spread calculation
    "lookback_days": 60,

    # Hedge ratio recalculation frequency
    "recalc_days": 20,
}

# ---------------------------------------------------------------------------
# Watchlist: high-liquidity NSE stocks for momentum scanning
# ---------------------------------------------------------------------------
WATCHLIST = [
    # Nifty 50 large caps (tight spreads, high volume)
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "SBIN", "BHARTIARTL", "HINDUNILVR", "ITC", "LT",
    "AXISBANK", "KOTAKBANK", "BAJAJFINSV", "MARUTI", "SUNPHARMA",
    "BAJFINANCE", "WIPRO", "HCLTECH", "NTPC", "POWERGRID",
    # Nifty Next 50 (slightly wider spreads but good liquidity)
    "TATASTEEL", "ADANIENT", "ADANIPORTS", "JSWSTEEL", "GRASIM",
    "INDUSINDBK", "BPCL", "ONGC", "COALINDIA", "PNB",
]

# ---------------------------------------------------------------------------
# Market timing
# ---------------------------------------------------------------------------
MARKET_OPEN = "09:15"    # IST
MARKET_CLOSE = "15:30"   # IST
PRE_MARKET_SCAN = "09:00"  # Run scanner before market opens

# Avoid trading in first and last 15 minutes (high volatility, poor fills)
AVOID_FIRST_MINUTES = 15
AVOID_LAST_MINUTES = 15

# ---------------------------------------------------------------------------
# Data settings
# ---------------------------------------------------------------------------
HISTORICAL_DAYS = 90     # Days of historical data to fetch for indicators
TICK_INTERVAL = "day"    # Candle interval for swing trading: "day"
INTRADAY_INTERVAL = "15minute"  # For intraday monitoring

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
TRADES_DB = DATA_DIR / "trades.db"

# Ensure directories exist
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
