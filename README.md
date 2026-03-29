# Momentum Swing Trading Bot - Indian Markets (NSE)

## Overview

Automated equity swing trading bot for NSE, built with Zerodha Kite Connect API.
Designed for small capital (starting ₹15,000), SEBI-compliant, with phased scaling.

## Architecture

```
trading-bot/
├── config/
│   ├── settings.py          # All configuration (API keys, risk params, watchlist)
│   └── .env.example         # Environment variable template
├── core/
│   ├── broker.py            # Kite Connect wrapper (auth, orders, positions)
│   ├── data_feed.py         # Live tick data via WebSocket + historical via yfinance
│   ├── risk_manager.py      # Position sizing, kill switches, drawdown tracking
│   └── engine.py            # Main trading loop (orchestrates everything)
├── strategies/
│   ├── base.py              # Abstract strategy interface
│   ├── momentum.py          # EMA crossover + RSI + volume breakout
│   └── pairs.py             # Statistical pairs trading (Phase 2)
├── utils/
│   ├── logger.py            # File + console logging
│   ├── telegram_alert.py    # Trade alerts via Telegram bot
│   └── journal.py           # Auto-log trades to CSV/SQLite
├── data/
│   ├── trades.db            # SQLite trade journal (auto-created)
│   └── watchlist.csv        # Stock watchlist with metadata
├── logs/                    # Daily log files
├── main.py                  # Entry point
├── paper_trader.py          # Paper trading simulator
└── requirements.txt         # Python dependencies
```

## Setup

### Prerequisites
- Python 3.11+
- Zerodha trading account with TOTP enabled
- Kite Connect Personal API (free) or paid plan (₹2,000/mo)
- Static IP registered with Zerodha (SEBI compliance)

### Installation

```bash
# Clone or copy this project
cd trading-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # macOS/Linux

# Install dependencies
pip install -r requirements.txt

# Copy and fill environment variables
cp config/.env.example config/.env
# Edit config/.env with your API keys
```

### First Run (Paper Mode)

```bash
# Always start in paper mode
python main.py --mode paper

# Paper trade for minimum 2 weeks / 200+ trades
# Check logs/ and data/trades.db for results
```

### Go Live (Only after paper validation)

```bash
# Micro-trade mode (₹3,000 max per position)
python main.py --mode live --max-position 3000
```

## Risk Parameters (DO NOT CHANGE until you understand them)

| Parameter               | Default   | Description                              |
|-------------------------|-----------|------------------------------------------|
| MAX_RISK_PER_TRADE      | 2%        | Max capital risked on any single trade   |
| MAX_POSITIONS           | 3         | Max concurrent open positions            |
| DAILY_LOSS_LIMIT        | 3%        | Auto-halt if daily loss exceeds this     |
| MAX_DRAWDOWN            | 15%       | Kill switch - disable bot entirely       |
| MAX_POSITION_SIZE       | 20%       | Max % of capital in one stock            |

## SEBI Compliance Notes

- Personal API use on your own account with < 10 OPS does NOT require
  separate Algo-ID registration (as of April 2026 framework)
- Static IP must be registered with Zerodha
- 2FA/TOTP must be enabled
- This bot is for PERSONAL USE ONLY. Selling/distributing requires
  SEBI Research Analyst registration

## Disclaimer

This is an educational project. Trading involves substantial risk of loss.
Past performance does not guarantee future results. Consult a SEBI-registered
advisor before deploying real capital.
