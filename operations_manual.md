# Momentum Swing Bot - Daily Operations Manual

This document outlines the daily routine required to keep the bot functioning optimally and a framework for continuously improving the trading model using autonomous AI research.

---

## 📅 Daily Routine (The Runbook)

*Important: The bot's logic is designed for active market hours (Mon-Fri). You do not need to run the engine on weekends.*

### 1. Pre-Market Pulse Check (8:30 AM - 8:45 AM)
*Momentum gets crushed in highly volatile or crashing markets. Run the automated pulse script first — it makes the go/no-go decision for you.*

```bash
source venv/bin/activate
python scripts/premarket_pulse.py
```

The script checks and scores:
| Signal | Source | Hard NO-GO threshold |
|---|---|---|
| India VIX | yfinance `^INDIAVIX` | VIX > 25 |
| Nifty 50 spot | yfinance `^NSEI` | — |
| US Indices (S&P, Nasdaq, Nikkei, Hang Seng, FTSE) | yfinance | S&P < -2% |
| Commodities (Crude, Gold, Silver, Nat Gas, Copper) | yfinance futures | Crude > +3% |
| Forex (USD/INR, Dollar Index) | yfinance | Rupee > +0.5% |
| US 10Y Treasury Yield | yfinance `^TNX` | — |
| OSINT News Headlines | ET / BS / Reuters / LiveMint RSS | Keyword scan |

**Reading the output:**
- 🟢 **GO (score ≥ 3)** — proceed normally
- 🟡 **CAUTION (score 1–2)** — run paper mode only, reduce size
- 🟠 **CAUTION (score -1 to 0)** — high uncertainty, skip the day
- 🔴 **NO-GO (score < -1)** — do not run the bot

> 🛑 **Rule:** If the pulse prints NO-GO or CAUTION, **do not start the bot**. Wait for a clean, stable trend day.

### 2. Pre-Market Automation (8:45 AM - 9:00 AM)
*The Kite Connect API requires a daily login to issue a fresh access token. This must be done every morning before the market opens.*

1. Open your terminal in the bot directory `~/Desktop/trading-bot`.
2. Activate your environment and launch the authentication script:
   ```bash
   source venv/bin/activate
   python auth.py
   ```
3. A browser window will open automatically. Complete the Zerodha 2FA login.
4. The script will intercept the redirect, save the token to your `.env`, and exit silently.

### 3. Launching the Bot (Before 9:15 AM)
*Once authenticated and the market is deemed stable, start the main engine.*

**To run in Paper Trading Mode (Simulated Execution, Live Data):**
```bash
python main.py --mode paper
```

**To run in Live Tracking Mode (Real Capital, Real Execution):**
```bash
python main.py --mode live
```

### 4. End of Day Shutdown (Post 4:00 PM)
*The bot performs its EOD shutdown around 3:45 PM. You can then safely stop the script.*

1. Press `Ctrl + C` in the terminal to gracefully exit the bot.
2. Open your Notion Dashboard.
3. Review the trades that were opened and closed today. If any trades hit Stop Loss or Target, Notion will light up red or green.


---

## 📈 Paper-to-Live Framework
*Do not transition to live capital based on "time". Transition based on statistical volume.*

### Minimum Requirement Before Going Live
Do not switch to `--mode live` until the bot has completed **at least 20 to 30 closed simulated trades** in Notion. In standard market conditions, this takes roughly **2 to 4 weeks** of paper trading.

### Metrics to Clear for "Live Capital"
Before you risk real Indian Rupees, cross-reference your Notion database against these exact thresholds:
1. **Profit Factor > 1.50:** Your total Gross Gains divided by your Gross Losses.
2. **Win Rate > 45%:** Because Swing Trading relies on scaling big winners (1:2+ R:R), you only need a ~45% win rate to be highly profitable.
3. **Flawless Execution:** Zero API crashes, zero slipped GTT orders, and full confidence that the bot handles 3:30 PM market closures correctly.

If you hit 30 paper trades and the Profit Factor is `< 1.0`, do not go live. Run the **AutoResearch Loop** to optimize the strategy first.

---

## 🤖 Nightly AutoResearch (Post-Trading Hours)

After you shut down the main engine at 4:00 PM, kick off the **Autonomous AI Optimisation Loop**. The loop uses a **walk-forward ratchet**: it only commits a hypothesis if the OOS (out-of-sample) score improves — meaning the agent cannot overfit to data it already backtested.

### How the OOS Ratchet Works
Each backtest splits the data into two windows:
- **In-sample** (first ~4 months): the optimizer's playground.
- **Out-of-sample** (last 60 days): held-out data the LLM never sees. A commit only happens when this window improves.

This means a hypothesis that memorises the in-sample period will be caught and rejected before it pollutes the live strategy.

### 1. Standard Overnight Run (200 iterations, ~₹50–100)
```bash
python scripts/autoresearch.py --model gemini/gemini-2.5-flash-lite
```
- Runs 200 iterations by default.
- Uses only the `MOMENTUM` dict in its prompt (not the full `settings.py`), saving ~67% in input tokens.
- Commits only when OOS score improves.

### 2. Cost-Optimised Run (Gemini Batch API, ~50% cheaper)
If you're comfortable running overnight and don't need real-time output:
```bash
python scripts/autoresearch.py --batch
```
Cuts nightly cost from ~₹100 to ~₹50 at 200 iterations.

### 3. Expand Scope to Strategy Logic
Once you trust the walk-forward backtest is robust, allow the agent to also propose new indicators, conditions, and exit rules in `strategies/momentum.py`:
```bash
python scripts/autoresearch.py --allow-strategy-edits
```
> Only enable this after you have verified the OOS ratchet is working (i.e. you see `❌ OOS score did not beat baseline. Reverting.` messages — that means the ratchet is active).

### 4. Local Maxima Detection & Upscaling
If the agent fails to beat the OOS baseline 15 times in a row, it detects a **Local Maxima** and sends an alert to your Notion Trade Dashboard.

When you see this alert, switch to a heavyweight model to break through the plateau:
```bash
python scripts/autoresearch.py --model anthropic/claude-3-5-sonnet-20241022 --iter 20
```

### Reading the AutoResearch Output
A healthy run looks like this:
```
--- Iteration 1/200 ---
Calling gemini/gemini-2.5-flash-lite to generate hypothesis...
Running backtest for hypothesis...
Hypothesis Result:
  OOS score:  88.10  (WR 54%, PF 1.5, Trades 12)
❌ OOS score 88.10 did not beat baseline 91.40. Reverting.

--- Iteration 7/200 ---
...
✅ OOS hypothesis improved! 91.40 → 96.20. Committing.
```
A commit that happens slowly (e.g. only 1 in 20 iterations) is a **good sign** — it means the ratchet is working and only genuine generalisations are being committed.
