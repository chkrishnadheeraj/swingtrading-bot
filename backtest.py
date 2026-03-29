"""
Momentum Strategy Backtester — 6-month historical simulation
============================================================
Downloads daily OHLCV via yfinance for RELIANCE, TCS, SBIN, HDFCBANK (.NS)
and replays the MomentumStrategy signal logic bar-by-bar.

Outputs:
    • Per-trade table (entry, exit, P&L, reason)
    • Win rate, avg R:R, Sharpe ratio, max drawdown
    • Equity curve chart saved to data/backtest_equity_curve.png
    • JSON summary to data/backtest_results.json

Run:
    source venv/bin/activate
    python backtest.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib
matplotlib.use("Agg")   # non-interactive backend — works without a display
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.gridspec import GridSpec

# ── project imports ────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from config import settings

# ── constants ──────────────────────────────────────────────────────────────
# Issue #3 fix: use the full 30-stock watchlist from settings instead of 4 stocks.
# More stocks → more trades → statistically meaningful win rate.
STOCKS       = settings.WATCHLIST
LOOKBACK_D   = 180          # 6 months of data to download
WARMUP_D     = 60           # days needed to seed 50-EMA + indicators before trading
INITIAL_CAP  = settings.INITIAL_CAPITAL
P            = settings.MOMENTUM   # strategy params

FAST_EMA     = P["fast_ema"]
SLOW_EMA     = P["slow_ema"]
TREND_EMA    = P.get("trend_ema", 50)   # Fix #2: higher-timeframe trend filter
RSI_PERIOD   = P["rsi_period"]
RSI_OB       = P["rsi_overbought"]
VOL_MULT     = P["volume_multiplier"]
INIT_SL_PCT  = P["initial_sl_pct"]
TRAIL_SL_PCT = P["trailing_sl_pct"]                    # Fix #1: now 4%
TRAIL_ACT    = P.get("trailing_sl_activation", 0.02)   # Fix #1: +2% gate
TARGET_PCT   = P["target_pct"]                         # now 8%
MAX_HOLD     = P["max_hold_days"]                       # now 10 days
MIN_RR       = settings.MIN_RISK_REWARD
MAX_POS      = settings.MAX_POSITIONS
MAX_POS_PCT  = settings.MAX_POSITION_PCT
MAX_RISK_PCT = settings.MAX_RISK_PER_TRADE


# ═══════════════════════════════════════════════════════════════════════════
# Data fetching
# ═══════════════════════════════════════════════════════════════════════════

def fetch_data(stocks: list[str], days: int) -> dict[str, pd.DataFrame]:
    end   = datetime.today()
    start = end - timedelta(days=days + 30)   # extra buffer for weekends/holidays

    print(f"\n📥  Downloading {days}-day history for {stocks} …")
    raw = {}
    for sym in stocks:
        ticker = yf.Ticker(f"{sym}.NS")
        df = ticker.history(start=start.strftime("%Y-%m-%d"),
                            end=end.strftime("%Y-%m-%d"))
        if df.empty:
            print(f"   ⚠️  No data for {sym} — skipping")
            continue
        df.columns = [c.lower() for c in df.columns]
        # Keep only standard OHLCV; drop dividends/splits columns
        df = df[["open", "high", "low", "close", "volume"]].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        raw[sym] = df
        print(f"   ✅  {sym}: {len(df)} trading days  "
              f"({df.index[0].date()} → {df.index[-1].date()})")
    return raw


# ═══════════════════════════════════════════════════════════════════════════
# Indicator calculations
# ═══════════════════════════════════════════════════════════════════════════

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # Fast + Slow EMAs (signal)
    df[f"ema_{FAST_EMA}"] = df["close"].ewm(span=FAST_EMA, adjust=False).mean()
    df[f"ema_{SLOW_EMA}"] = df["close"].ewm(span=SLOW_EMA, adjust=False).mean()
    # Trend EMA — higher-timeframe filter (Fix #2)
    df[f"ema_{TREND_EMA}"] = df["close"].ewm(span=TREND_EMA, adjust=False).mean()
    # RSI
    delta = df["close"].diff()
    gain  = delta.where(delta > 0, 0).rolling(RSI_PERIOD).mean()
    loss  = (-delta.where(delta < 0, 0)).rolling(RSI_PERIOD).mean()
    rs    = gain / loss.replace(0, np.inf)
    df["rsi"] = 100 - (100 / (1 + rs))
    # Average volume
    df["avg_vol"] = df["volume"].rolling(20).mean()
    return df


# ═══════════════════════════════════════════════════════════════════════════
# Signal generation (mirrors MomentumStrategy._analyze_stock)
# ═══════════════════════════════════════════════════════════════════════════

def generate_signal(df_full: pd.DataFrame, i: int) -> Optional[dict]:
    """
    Evaluate momentum signal at bar index `i`.
    Returns trade params dict or None.
    """
    if i < TREND_EMA + RSI_PERIOD + 5:
        return None

    cur  = df_full.iloc[i]
    prev = df_full.iloc[i - 1]

    fe, se, te = f"ema_{FAST_EMA}", f"ema_{SLOW_EMA}", f"ema_{TREND_EMA}"

    # 0. Higher-timeframe trend filter (Fix #2): must be above 50-EMA
    if cur["close"] <= cur[te]:
        return None

    # 1. EMA crossover  OR  price above rising EMAs
    cross_up = prev[fe] <= prev[se] and cur[fe] > cur[se]
    bullish  = (cur["close"] > cur[fe] and cur[fe] > cur[se]
                and cur[fe] > prev[fe])
    if not (cross_up or bullish):
        return None

    # 2. RSI filter
    rsi = cur["rsi"]
    if pd.isna(rsi) or rsi > RSI_OB or rsi < 20:
        return None

    # 3. Volume confirmation
    if pd.isna(cur["avg_vol"]) or cur["volume"] < cur["avg_vol"] * VOL_MULT:
        return None

    # 4. Build trade parameters
    entry  = cur["close"]
    recent_low = df_full["low"].iloc[max(0, i-4):i+1].min()
    sl     = max(entry * (1 - INIT_SL_PCT), recent_low * 0.995)
    target = entry * (1 + TARGET_PCT)

    risk   = entry - sl
    reward = target - entry
    if risk <= 0 or (reward / risk) < MIN_RR:
        return None

    confidence = _calc_confidence(cross_up, bullish, rsi,
                                  cur["volume"] / cur["avg_vol"])
    return {
        "cross_up":   cross_up,
        "entry":      round(entry, 2),
        "sl":         round(sl, 2),
        "target":     round(target, 2),
        "risk":       round(risk, 2),
        "reward":     round(reward, 2),
        "rr":         round(reward / risk, 2),
        "confidence": round(confidence, 3),
    }


def _calc_confidence(cross: bool, bullish: bool, rsi: float, vol_ratio: float) -> float:
    score = 0.35 if cross else (0.20 if bullish else 0)
    if 40 <= rsi <= 55:    score += 0.25
    elif 35 <= rsi <= 65:  score += 0.15
    else:                  score += 0.05
    if vol_ratio >= 2.5:   score += 0.30
    elif vol_ratio >= 2.0: score += 0.25
    elif vol_ratio >= 1.5: score += 0.15
    return min(1.0, max(0.0, score))


# ═══════════════════════════════════════════════════════════════════════════
# Trade record
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    stock:        str
    entry_date:   str
    entry_price:  float
    sl:           float
    target:       float
    quantity:     int
    rr_at_entry:  float
    confidence:   float
    exit_date:    str = ""
    exit_price:   float = 0.0
    exit_reason:  str = ""
    pnl:          float = 0.0
    pnl_pct:      float = 0.0
    won:          bool  = False
    realised_rr:  float = 0.0   # actual R multiples achieved


# ═══════════════════════════════════════════════════════════════════════════
# Portfolio state during backtest
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Portfolio:
    capital:   float = INITIAL_CAP
    peak:      float = INITIAL_CAP
    positions: dict  = field(default_factory=dict)   # stock → open trade dict
    equity:    list  = field(default_factory=list)   # [(date, capital)]
    trades:    list  = field(default_factory=list)   # completed Trade objects

    def drawdown(self) -> float:
        if self.peak <= 0: return 0.0
        return (self.peak - self.capital) / self.peak


# ═══════════════════════════════════════════════════════════════════════════
# Core backtest loop
# ═══════════════════════════════════════════════════════════════════════════

def run_backtest(data: dict[str, pd.DataFrame]) -> Portfolio:
    """
    Day-by-day simulation across all stocks.
    One position per stock (consistent with engine logic).
    """
    # Align all series on a common calendar
    all_dates = sorted(set().union(*[set(df.index) for df in data.values()]))
    data_ind  = {sym: add_indicators(df) for sym, df in data.items()}

    port = Portfolio()
    port.equity.append((all_dates[0], INITIAL_CAP))

    def pos_size(entry: float, sl: float) -> int:
        risk_per_share = entry - sl
        if risk_per_share <= 0: return 0
        # Half-Kelly capped at MAX_RISK_PCT
        b, p, q = MIN_RR, 0.55, 0.45
        kelly    = (b * p - q) / b
        fraction = min(max(0, kelly * 0.5), MAX_RISK_PCT)
        risk_amt = port.capital * fraction
        qty      = int(risk_amt / risk_per_share)
        max_qty  = int((port.capital * MAX_POS_PCT) / entry)
        return max(1, min(qty, max_qty))

    for date in all_dates[WARMUP_D:]:
        # ── Exit check (before entry) ───────────────────────────────────
        to_close = []
        for sym, pos in port.positions.items():
            if sym not in data_ind: continue
            df = data_ind[sym]
            if date not in df.index: continue
            row = df.loc[date]
            cur_price  = row["close"]
            low_today  = row["low"]
            high_today = row["high"]

            highest = max(pos["highest"], high_today)
            pos["highest"] = highest

            reason = None
            exit_p = cur_price

            # SL hit — use low of day as proxy
            init_sl = pos["entry"] * (1 - INIT_SL_PCT)
            if low_today <= init_sl:
                reason = "SL hit"
                exit_p = max(low_today, init_sl)   # approximate fill

            # Target hit
            elif high_today >= pos["target"]:
                reason = "Target hit"
                exit_p = pos["target"]

            # Trailing SL (Fix #1): only activates after +TRAIL_ACT% gain
            elif cur_price >= pos["entry"] * (1 + TRAIL_ACT):
                trail_sl = highest * (1 - TRAIL_SL_PCT)
                if low_today <= trail_sl:
                    reason = "Trailing SL"
                    exit_p = max(low_today, trail_sl)

            # Max hold
            elif (pd.Timestamp(date) - pd.Timestamp(pos["entry_date"])).days >= MAX_HOLD:
                reason = "Max hold"

            # RSI overbought
            elif not pd.isna(row.get("rsi", float("nan"))) and row["rsi"] > 78:
                reason = "RSI overbought"

            if reason:
                pnl = (exit_p - pos["entry"]) * pos["qty"]
                port.capital += pnl
                port.peak     = max(port.peak, port.capital)
                risk_per_share = pos["entry"] - pos["sl"]
                realised_rr   = (exit_p - pos["entry"]) / risk_per_share if risk_per_share > 0 else 0
                t = Trade(
                    stock=sym,
                    entry_date=pos["entry_date"],
                    entry_price=pos["entry"],
                    sl=pos["sl"],
                    target=pos["target"],
                    quantity=pos["qty"],
                    rr_at_entry=pos["rr"],
                    confidence=pos["confidence"],
                    exit_date=str(date.date()),
                    exit_price=round(exit_p, 2),
                    exit_reason=reason,
                    pnl=round(pnl, 2),
                    pnl_pct=round((exit_p - pos["entry"]) / pos["entry"] * 100, 2),
                    won=(pnl > 0),
                    realised_rr=round(realised_rr, 2),
                )
                port.trades.append(t)
                to_close.append(sym)

        for sym in to_close:
            del port.positions[sym]

        # ── Entry scan ─────────────────────────────────────────────────
        if len(port.positions) < MAX_POS:
            candidates = []
            for sym, df in data_ind.items():
                if sym in port.positions: continue
                if date not in df.index: continue
                i = df.index.get_loc(date)
                sig = generate_signal(df, i)
                if sig:
                    candidates.append((sym, sig))

            # Sort by confidence (highest first)
            candidates.sort(key=lambda x: x[1]["confidence"], reverse=True)

            slots = MAX_POS - len(port.positions)
            for sym, sig in candidates[:slots]:
                qty = pos_size(sig["entry"], sig["sl"])
                cost = sig["entry"] * qty
                if cost > port.capital * 0.95 or qty <= 0:
                    continue
                port.positions[sym] = {
                    "entry_date": str(date.date()),
                    "entry":      sig["entry"],
                    "sl":         sig["sl"],
                    "target":     sig["target"],
                    "qty":        qty,
                    "rr":         sig["rr"],
                    "confidence": sig["confidence"],
                    "highest":    sig["entry"],
                }

        port.equity.append((date, round(port.capital, 2)))

    # Close any still-open positions at last price
    for sym, pos in port.positions.items():
        df = data_ind.get(sym)
        if df is None or df.empty: continue
        last_price = float(df["close"].iloc[-1])
        last_date  = str(df.index[-1].date())
        pnl = (last_price - pos["entry"]) * pos["qty"]
        port.capital += pnl
        risk_per_share = pos["entry"] - pos["sl"]
        realised_rr = (last_price - pos["entry"]) / risk_per_share if risk_per_share > 0 else 0
        port.trades.append(Trade(
            stock=sym, entry_date=pos["entry_date"], entry_price=pos["entry"],
            sl=pos["sl"], target=pos["target"], quantity=pos["qty"],
            rr_at_entry=pos["rr"], confidence=pos["confidence"],
            exit_date=last_date, exit_price=round(last_price, 2),
            exit_reason="Period end", pnl=round(pnl, 2),
            pnl_pct=round((last_price - pos["entry"]) / pos["entry"] * 100, 2),
            won=(pnl > 0), realised_rr=round(realised_rr, 2),
        ))

    return port


# ═══════════════════════════════════════════════════════════════════════════
# Statistics
# ═══════════════════════════════════════════════════════════════════════════

def compute_stats(port: Portfolio) -> dict:
    trades = port.trades
    if not trades:
        return {"error": "No trades generated"}

    won  = [t for t in trades if t.won]
    lost = [t for t in trades if not t.won]

    win_rate   = len(won) / len(trades) * 100
    avg_win    = np.mean([t.pnl for t in won]) if won else 0
    avg_loss   = np.mean([abs(t.pnl) for t in lost]) if lost else 0
    avg_rr     = np.mean([t.realised_rr for t in trades])
    avg_rr_won = np.mean([t.realised_rr for t in won]) if won else 0

    total_pnl  = sum(t.pnl for t in trades)
    final_cap  = port.capital
    ret_pct    = (final_cap - INITIAL_CAP) / INITIAL_CAP * 100

    # Max drawdown from equity curve
    eq_vals    = [v for _, v in port.equity]
    peak_arr   = np.maximum.accumulate(eq_vals)
    dd_arr     = (peak_arr - np.array(eq_vals)) / np.maximum(peak_arr, 1)
    max_dd     = float(np.max(dd_arr)) * 100

    # Sharpe (daily returns)
    eq_series  = pd.Series(eq_vals)
    daily_ret  = eq_series.pct_change().dropna()
    sharpe     = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)
                  if daily_ret.std() > 0 else 0)

    # Profit factor
    gross_profit = sum(t.pnl for t in won)
    gross_loss   = abs(sum(t.pnl for t in lost))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    return {
        "total_trades":    len(trades),
        "winners":         len(won),
        "losers":          len(lost),
        "win_rate_pct":    round(win_rate, 1),
        "avg_rr":          round(avg_rr, 2),
        "avg_rr_winners":  round(avg_rr_won, 2),
        "avg_win_inr":     round(avg_win, 2),
        "avg_loss_inr":    round(avg_loss, 2),
        "gross_profit":    round(gross_profit, 2),
        "gross_loss":      round(gross_loss, 2),
        "profit_factor":   round(profit_factor, 2),
        "total_pnl_inr":   round(total_pnl, 2),
        "initial_capital": INITIAL_CAP,
        "final_capital":   round(final_cap, 2),
        "return_pct":      round(ret_pct, 2),
        "max_drawdown_pct":round(max_dd, 2),
        "sharpe_ratio":    round(sharpe, 2),
    }


def compute_oos_stats(trades: list) -> dict:
    """
    Compute stats from a list of Trade objects without an equity curve.
    Used for the out-of-sample window — the autoresearcher ratchet uses
    this score so the optimizer never touches the evaluation window.
    """
    if not trades:
        return {
            "total_trades": 0, "win_rate_pct": 0.0,
            "profit_factor": 0.0, "total_pnl_inr": 0.0,
            "return_pct": 0.0, "score": 0.0,
        }

    won  = [t for t in trades if t.won]
    lost = [t for t in trades if not t.won]

    win_rate     = len(won) / len(trades) * 100
    gross_profit = sum(t.pnl for t in won)
    gross_loss   = abs(sum(t.pnl for t in lost))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    pf_capped    = min(profit_factor if profit_factor != float("inf") else 5.0, 5.0)
    total_pnl    = sum(t.pnl for t in trades)
    return_pct   = total_pnl / INITIAL_CAP * 100

    # Same blended score formula as autoresearch.py — with trade-count penalty
    penalty = min(1.0, len(trades) / 10.0)
    score   = (win_rate * pf_capped) * penalty

    return {
        "total_trades":  len(trades),
        "win_rate_pct":  round(win_rate, 1),
        "profit_factor": round(profit_factor if profit_factor != float("inf") else 5.0, 2),
        "total_pnl_inr": round(total_pnl, 2),
        "return_pct":    round(return_pct, 2),
        "score":         round(score, 2),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Plotting — equity curve + trade distribution
# ═══════════════════════════════════════════════════════════════════════════

def plot_results(port: Portfolio, stats: dict, out_path: Path):
    eq_dates  = [d for d, _ in port.equity]
    eq_vals   = [v for _, v in port.equity]
    peak_arr  = np.maximum.accumulate(eq_vals)
    dd_arr    = [(p - v) / p * 100 for p, v in zip(peak_arr, eq_vals)]

    trades    = port.trades
    pnls      = [t.pnl for t in trades]
    rrs       = [t.realised_rr for t in trades]
    colors    = ["#2ecc71" if t.won else "#e74c3c" for t in trades]

    # ── Layout ─────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(16, 11), facecolor="#0d1117")
    gs  = GridSpec(3, 2, figure=fig, hspace=0.45, wspace=0.35,
                   left=0.07, right=0.97, top=0.91, bottom=0.07)

    ax_eq  = fig.add_subplot(gs[0, :])   # equity curve full width
    ax_dd  = fig.add_subplot(gs[1, :])   # drawdown full width
    ax_pnl = fig.add_subplot(gs[2, 0])   # P&L distribution
    ax_rr  = fig.add_subplot(gs[2, 1])   # R:R distribution

    PANEL_BG = "#161b22"
    GRID_C   = "#30363d"
    TEXT_C   = "#c9d1d9"
    ACCENT   = "#58a6ff"
    GREEN    = "#2ecc71"
    RED      = "#e74c3c"
    GOLD     = "#f0b429"

    for ax in [ax_eq, ax_dd, ax_pnl, ax_rr]:
        ax.set_facecolor(PANEL_BG)
        ax.tick_params(colors=TEXT_C, labelsize=9)
        ax.spines[["top","right"]].set_visible(False)
        for sp in ["bottom","left"]:
            ax.spines[sp].set_color(GRID_C)
        ax.grid(True, color=GRID_C, alpha=0.5, linewidth=0.6)

    # ── Equity Curve ───────────────────────────────────────────────────
    ax_eq.fill_between(eq_dates, INITIAL_CAP, eq_vals,
                        where=[v >= INITIAL_CAP for v in eq_vals],
                        color=GREEN, alpha=0.18)
    ax_eq.fill_between(eq_dates, INITIAL_CAP, eq_vals,
                        where=[v < INITIAL_CAP for v in eq_vals],
                        color=RED, alpha=0.18)
    ax_eq.plot(eq_dates, eq_vals, color=ACCENT, linewidth=1.8, label="Portfolio")
    ax_eq.plot(eq_dates, peak_arr, color=GOLD, linewidth=0.9,
               linestyle="--", alpha=0.7, label="Peak")
    ax_eq.axhline(INITIAL_CAP, color=TEXT_C, linewidth=0.6, alpha=0.5,
                  linestyle=":")
    ax_eq.set_title("Equity Curve", color=TEXT_C, fontsize=11, fontweight="bold",
                    pad=8)
    ax_eq.set_ylabel("Capital (₹)", color=TEXT_C, fontsize=9)
    ax_eq.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda x, _: f"₹{x:,.0f}"))
    ax_eq.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax_eq.xaxis.set_major_locator(mdates.MonthLocator())
    ax_eq.legend(facecolor=PANEL_BG, edgecolor=GRID_C,
                 labelcolor=TEXT_C, fontsize=9)

    # Annotate final return
    ret = stats["return_pct"]
    ax_eq.annotate(
        f"{'▲' if ret >= 0 else '▼'} {ret:+.1f}%  |  "
        f"Sharpe {stats['sharpe_ratio']:.2f}  |  "
        f"Max DD {stats['max_drawdown_pct']:.1f}%",
        xy=(0.01, 0.92), xycoords="axes fraction",
        color=GREEN if ret >= 0 else RED, fontsize=10, fontweight="bold",
    )

    # ── Drawdown ───────────────────────────────────────────────────────
    ax_dd.fill_between(eq_dates, 0, dd_arr, color=RED, alpha=0.5)
    ax_dd.plot(eq_dates, dd_arr, color=RED, linewidth=0.9)
    ax_dd.set_title("Drawdown (%)", color=TEXT_C, fontsize=11, fontweight="bold",
                    pad=8)
    ax_dd.set_ylabel("DD %", color=TEXT_C, fontsize=9)
    ax_dd.invert_yaxis()
    ax_dd.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax_dd.xaxis.set_major_locator(mdates.MonthLocator())

    # ── P&L Distribution ───────────────────────────────────────────────
    if pnls:
        bins = max(8, len(pnls) // 2)
        ax_pnl.hist(pnls, bins=bins, color=ACCENT, alpha=0.75, edgecolor=GRID_C)
        ax_pnl.axvline(0, color=TEXT_C, linewidth=0.8, linestyle="--")
        ax_pnl.axvline(np.mean(pnls), color=GOLD, linewidth=1.2,
                       linestyle="-", label=f"Mean ₹{np.mean(pnls):+.0f}")
        ax_pnl.set_title("P&L per Trade (₹)", color=TEXT_C, fontsize=11,
                          fontweight="bold", pad=8)
        ax_pnl.set_xlabel("P&L (₹)", color=TEXT_C, fontsize=9)
        ax_pnl.set_ylabel("# Trades", color=TEXT_C, fontsize=9)
        ax_pnl.legend(facecolor=PANEL_BG, edgecolor=GRID_C,
                      labelcolor=TEXT_C, fontsize=9)

    # ── R:R Distribution ───────────────────────────────────────────────
    if rrs:
        bins = max(8, len(rrs) // 2)
        ax_rr.hist(rrs, bins=bins, color=GOLD, alpha=0.75, edgecolor=GRID_C)
        ax_rr.axvline(0, color=TEXT_C, linewidth=0.8, linestyle="--")
        ax_rr.axvline(np.mean(rrs), color=GREEN, linewidth=1.2,
                      linestyle="-", label=f"Mean R:R {np.mean(rrs):.2f}")
        ax_rr.axvline(MIN_RR, color=RED, linewidth=0.9,
                      linestyle=":", alpha=0.8, label=f"Min R:R {MIN_RR}")
        ax_rr.set_title("Realised R:R per Trade", color=TEXT_C, fontsize=11,
                         fontweight="bold", pad=8)
        ax_rr.set_xlabel("R Multiples", color=TEXT_C, fontsize=9)
        ax_rr.set_ylabel("# Trades", color=TEXT_C, fontsize=9)
        ax_rr.legend(facecolor=PANEL_BG, edgecolor=GRID_C,
                     labelcolor=TEXT_C, fontsize=9)

    # ── Main title ─────────────────────────────────────────────────────
    fig.suptitle(
        f"Momentum Swing Strategy  ·  6-Month Backtest  ·  "
        f"{len(STOCKS)}-Stock Watchlist  ·  Trend EMA({TREND_EMA}) Filter  ·  "
        f"Win Rate {stats['win_rate_pct']}%  ·  Avg R:R {stats['avg_rr']}",
        color=TEXT_C, fontsize=12, fontweight="bold", y=0.97,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"\n📊  Equity curve saved → {out_path}")


# ═══════════════════════════════════════════════════════════════════════════
# Console output helpers
# ═══════════════════════════════════════════════════════════════════════════

def print_trade_table(trades: list[Trade]):
    if not trades:
        print("  (no completed trades)")
        return

    header = (f"{'#':>3}  {'Stock':<10}  {'Entry Date':<12}  "
              f"{'Entry':>8}  {'Exit':>8}  {'Exit Date':<12}  "
              f"{'Qty':>5}  {'P&L':>9}  {'R:R':>6}  Reason")
    print("\n" + "─" * len(header))
    print(header)
    print("─" * len(header))
    for n, t in enumerate(trades, 1):
        mark = "✅" if t.won else "❌"
        print(f"{n:>3}  {t.stock:<10}  {t.entry_date:<12}  "
              f"₹{t.entry_price:>7,.1f}  ₹{t.exit_price:>7,.1f}  {t.exit_date:<12}  "
              f"{t.quantity:>5}  ₹{t.pnl:>+8,.0f}  {t.realised_rr:>5.2f}R  "
              f"{mark} {t.exit_reason}")
    print("─" * len(header))


def print_stats(stats: dict):
    print("\n" + "═" * 56)
    print("  📈  BACKTEST RESULTS — 6-Month Momentum Strategy")
    print("═" * 56)
    pairs = [
        ("Period",           "6 months"),
        ("Universe",         "RELIANCE, TCS, SBIN, HDFCBANK"),
        ("Strategy",         "EMA 9/21 crossover + RSI + Volume"),
        ("",                 ""),
        ("Total trades",     str(stats["total_trades"])),
        ("Winners",          f"{stats['winners']}  ({stats['win_rate_pct']}%)"),
        ("Losers",           str(stats["losers"])),
        ("",                 ""),
        ("Win Rate",         f"{stats['win_rate_pct']}%"),
        ("Avg R:R (all)",    f"{stats['avg_rr']}R"),
        ("Avg R:R (winners)",f"{stats['avg_rr_winners']}R"),
        ("Profit Factor",    str(stats["profit_factor"])),
        ("",                 ""),
        ("Avg Win   (₹)",    f"₹{stats['avg_win_inr']:,.0f}"),
        ("Avg Loss  (₹)",    f"₹{stats['avg_loss_inr']:,.0f}"),
        ("Total P&L (₹)",    f"₹{stats['total_pnl_inr']:+,.0f}"),
        ("",                 ""),
        ("Initial Capital",  f"₹{stats['initial_capital']:,}"),
        ("Final Capital",    f"₹{stats['final_capital']:,.0f}"),
        ("Return",           f"{stats['return_pct']:+.2f}%"),
        ("Max Drawdown",     f"{stats['max_drawdown_pct']:.2f}%"),
        ("Sharpe Ratio",     str(stats["sharpe_ratio"])),
    ]
    for k, v in pairs:
        if k == "":
            print()
        else:
            print(f"  {k:<22}  {v}")
    print("═" * 56)


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Momentum Strategy Backtester")
    parser.add_argument(
        "--eval-window", type=int, default=60,
        help="Days at the END of the backtest period reserved for out-of-sample scoring. "
             "The optimizer never sees this window — only used to compute the final OOS score. "
             "(default: 60)"
    )
    args = parser.parse_args()
    eval_window = args.eval_window

    print("\n" + "█" * 56)
    print("  MOMENTUM SWING — 6-MONTH BACKTEST")
    print("█" * 56)
    print(f"  Stocks   : {len(STOCKS)} stocks (full WATCHLIST)")
    print(f"  Period   : Last {LOOKBACK_D} days  |  OOS window: {eval_window}d")
    print(f"  Capital  : ₹{INITIAL_CAP:,}")
    print(f"  Strategy : EMA {FAST_EMA}/{SLOW_EMA} | Trend EMA({TREND_EMA}) filter")
    print(f"  SL       : {INIT_SL_PCT:.0%} hard | {TRAIL_SL_PCT:.0%} trail (activates at +{TRAIL_ACT:.0%})")
    print(f"  Target   : {TARGET_PCT:.0%} | Max hold: {MAX_HOLD}d | Min R:R: {MIN_RR}")

    # 1. Fetch data
    data = fetch_data(STOCKS, LOOKBACK_D)
    if not data:
        print("❌  No data fetched. Check internet / NSE symbols.")
        sys.exit(1)

    # 2. Run backtest
    print("\n⚙️   Running backtest …")
    port = run_backtest(data)

    # 3. Full-period stats
    stats = compute_stats(port)
    if "error" in stats:
        print(f"⚠️  {stats['error']}")
        print("   Possible reasons: not enough trading days, or signals require more price history.")
        sys.exit(0)

    # 4. Walk-forward OOS split — trades entered on/after split_date are out-of-sample
    all_dates = sorted(set().union(*[set(df.index) for df in data.values()]))
    oos_stats = {}
    split_date_str = None
    if len(all_dates) > eval_window:
        split_date     = all_dates[-eval_window]
        split_date_str = str(split_date.date())
        oos_trades     = [t for t in port.trades if t.entry_date >= split_date_str]
        oos_stats      = compute_oos_stats(oos_trades)
        print(f"\n📐  Walk-forward split: in-sample < {split_date_str}  |  "
              f"OOS ≥ {split_date_str}  ({len(oos_trades)} OOS trades)")

    # 5. Print trade table + stats
    print_trade_table(port.trades)
    print_stats(stats)
    if oos_stats:
        print(f"\n  Out-of-Sample ({eval_window}d):")
        print(f"    Trades : {oos_stats['total_trades']}")
        print(f"    Win Rate : {oos_stats['win_rate_pct']}%   Profit Factor : {oos_stats['profit_factor']}")
        print(f"    OOS Return : {oos_stats['return_pct']:+.2f}%   OOS Score : {oos_stats['score']:.2f}")

    # 6. Save equity curve chart
    chart_path = Path("data/backtest_equity_curve.png")
    plot_results(port, stats, chart_path)

    # 7. Save JSON results
    json_path = Path("data/backtest_results.json")

    def _trade_to_dict(t: Trade) -> dict:
        d = asdict(t)
        d["won"] = bool(d["won"])   # numpy bool_ → Python bool
        return d

    output = {
        "stats":      stats,
        "oos_stats":  oos_stats,
        "split_date": split_date_str,
        "trades":     [_trade_to_dict(t) for t in port.trades],
        "run_at":     datetime.now().isoformat(),
    }
    json_path.write_text(json.dumps(output, indent=2, default=str))
    print(f"💾  Full results saved → {json_path}")

    print("\n✅  Backtest complete. Review results before paper trading.\n")


if __name__ == "__main__":
    main()
