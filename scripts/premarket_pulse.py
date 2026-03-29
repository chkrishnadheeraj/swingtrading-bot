"""
Pre-Market Pulse — Daily Go / No-Go Check
==========================================
Fetches live market data and news headlines before 9:15 AM and prints a
structured go/no-go recommendation for running the momentum bot.

Covers:
  • India VIX + Nifty 50 spot
  • Global indices (S&P 500, Nasdaq, Nikkei, Hang Seng, FTSE)
  • Commodities (Crude Oil, Gold, Natural Gas, Copper, Silver)
  • Forex (USD/INR, DXY Dollar Index)
  • US 10-Year Treasury Yield
  • OSINT: live news headlines from Economic Times, Business Standard,
           Reuters India (RSS — no API key required)
  • Sentiment keyword scan on headlines
  • Scored go/no-go recommendation

Run:
    source venv/bin/activate
    python scripts/premarket_pulse.py
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import httpx
import yfinance as yf

# ── ANSI colours (no extra deps) ───────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

W = 56   # console width


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _color(val: float, *, good_positive: bool = True) -> str:
    """Return ANSI colour based on sign and convention."""
    if val > 0:
        return GREEN if good_positive else RED
    if val < 0:
        return RED if good_positive else GREEN
    return RESET


def _arrow(val: float) -> str:
    return "▲" if val > 0 else ("▼" if val < 0 else "─")


def _pct(val: float, *, good_positive: bool = True) -> str:
    c = _color(val, good_positive=good_positive)
    return f"{c}{_arrow(val)} {val:+.2f}%{RESET}"


def _bar(label: str, char: str = "─") -> None:
    print(f"\n{CYAN}{char * 2}  {label}  {char * (W - len(label) - 5)}{RESET}")


def _row(label: str, value: str, note: str = "") -> None:
    note_str = f"  {DIM}{note}{RESET}" if note else ""
    print(f"  {label:<20}  {value}{note_str}")


def _fetch_quote(ticker: str) -> dict | None:
    """Returns {price, prev_close, change_pct} or None on failure."""
    try:
        t = yf.Ticker(ticker)
        info = t.fast_info
        price      = float(info.last_price)
        prev_close = float(info.previous_close)
        change_pct = (price - prev_close) / prev_close * 100
        return {"price": price, "prev_close": prev_close, "change_pct": change_pct}
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# News / OSINT
# ═══════════════════════════════════════════════════════════════════════════

_RSS_FEEDS = [
    ("ET",      "https://economictimes.indiatimes.com/markets/rss.cms"),
    ("BS",      "https://www.business-standard.com/rss/markets-106.rss"),
    ("Reuters", "https://feeds.reuters.com/reuters/INbusinessNews"),
    ("LiveMint","https://www.livemint.com/rss/markets"),
]

# Keywords that suggest market stress — each hit subtracts from score
_BEARISH_KW = [
    "crash", "crisis", "collapse", "recession", "panic", "plunge",
    "rate hike", "inflation spike", "sanctions", "geopolit", "war",
    "rbi rate", "fed hike", "downgrade", "fii sell", "selloff",
    "default", "bank failure", "contagion",
]
_BULLISH_KW = [
    "rally", "surge", "record high", "all-time high", "rate cut",
    "stimulus", "upgrade", "fii buy", "strong gdp", "recovery",
    "positive outlook", "bull",
]


def fetch_headlines(max_per_feed: int = 4) -> list[tuple[str, str]]:
    """
    Returns list of (source, headline) tuples fetched from RSS feeds.
    Silently skips any feed that fails.
    """
    results = []
    for source, url in _RSS_FEEDS:
        try:
            r = httpx.get(url, timeout=6, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.text)
            items = root.findall(".//item")[:max_per_feed]
            for item in items:
                title_el = item.find("title")
                if title_el is not None and title_el.text:
                    results.append((source, title_el.text.strip()))
        except Exception:
            continue
    return results


def score_headlines(headlines: list[tuple[str, str]]) -> tuple[int, list[str]]:
    """
    Scans headlines for sentiment keywords.
    Returns (score_delta, list_of_flagged_headlines).
    """
    delta  = 0
    flagged = []
    for source, headline in headlines:
        hl_lower = headline.lower()
        bear_hits = [kw for kw in _BEARISH_KW if kw in hl_lower]
        bull_hits = [kw for kw in _BULLISH_KW if kw in hl_lower]
        if bear_hits:
            delta -= 1
            flagged.append(f"{RED}⚠  [{source}] {headline}{RESET}")
        elif bull_hits:
            delta += 1
            flagged.append(f"{GREEN}✓  [{source}] {headline}{RESET}")
        else:
            flagged.append(f"   [{source}] {headline}")
    return delta, flagged


# ═══════════════════════════════════════════════════════════════════════════
# Scoring
# ═══════════════════════════════════════════════════════════════════════════

def build_score(vix: dict | None,
                spx: dict | None,
                crude: dict | None,
                gold: dict | None,
                usdinr: dict | None,
                news_delta: int) -> tuple[int, list[str]]:
    """
    Returns (total_score, list_of_reason_strings).
    Score > 2 = GO, 0–2 = CAUTION, < 0 = NO-GO.
    Hard NO-GO if VIX > 25 regardless of score.
    """
    score   = 0
    reasons = []

    # India VIX
    if vix:
        v = vix["price"]
        if v < 15:
            score += 2
            reasons.append(f"{GREEN}✅ VIX very calm ({v:.1f} < 15){RESET}")
        elif v < 18:
            score += 1
            reasons.append(f"{GREEN}✅ VIX calm ({v:.1f} < 18){RESET}")
        elif v < 20:
            score += 0
            reasons.append(f"{YELLOW}⚠  VIX elevated ({v:.1f}){RESET}")
        elif v < 25:
            score -= 2
            reasons.append(f"{RED}❌ VIX high ({v:.1f} > 20) — momentum edge deteriorates{RESET}")
        else:
            score -= 5   # hard veto
            reasons.append(f"{RED}🛑 VIX EXTREME ({v:.1f} > 25) — DO NOT TRADE{RESET}")

    # S&P 500 overnight
    if spx:
        c = spx["change_pct"]
        if c > 0.5:
            score += 2
            reasons.append(f"{GREEN}✅ US markets strong overnight ({c:+.2f}%){RESET}")
        elif c > 0:
            score += 1
            reasons.append(f"{GREEN}✅ US markets up overnight ({c:+.2f}%){RESET}")
        elif c > -0.5:
            score += 0
            reasons.append(f"{YELLOW}⚠  US markets flat ({c:+.2f}%){RESET}")
        elif c > -1.5:
            score -= 1
            reasons.append(f"{RED}❌ US markets weak overnight ({c:+.2f}%){RESET}")
        else:
            score -= 3
            reasons.append(f"{RED}❌ US markets sold off hard ({c:+.2f}%) — risk-off{RESET}")

    # Crude Oil — large spike is inflationary (bad for India)
    if crude:
        c = crude["change_pct"]
        if c > 3:
            score -= 2
            reasons.append(f"{RED}❌ Crude oil spike ({c:+.2f}%) — inflationary pressure{RESET}")
        elif c > 1.5:
            score -= 1
            reasons.append(f"{YELLOW}⚠  Crude oil rising ({c:+.2f}%){RESET}")
        elif c < -2:
            score += 1
            reasons.append(f"{GREEN}✅ Crude oil falling ({c:+.2f}%) — benign for India{RESET}")

    # Gold — rising gold = risk-off environment
    if gold:
        c = gold["change_pct"]
        if c > 1.5:
            score -= 1
            reasons.append(f"{YELLOW}⚠  Gold surge ({c:+.2f}%) — risk-off signal{RESET}")
        elif c > 0.5:
            reasons.append(f"{YELLOW}⚠  Gold up ({c:+.2f}%) — mild risk-off{RESET}")

    # USD/INR — Rupee weakness hurts FII flows
    if usdinr:
        c = usdinr["change_pct"]
        if c > 0.5:
            score -= 1
            reasons.append(f"{RED}❌ Rupee weakening ({c:+.2f}%) — FII outflow risk{RESET}")
        elif c > 0.2:
            reasons.append(f"{YELLOW}⚠  Rupee slightly weak ({c:+.2f}%){RESET}")
        elif c < -0.3:
            score += 1
            reasons.append(f"{GREEN}✅ Rupee strengthening ({c:+.2f}%){RESET}")

    # News OSINT
    if news_delta >= 2:
        score += 1
        reasons.append(f"{GREEN}✅ News sentiment positive{RESET}")
    elif news_delta <= -2:
        score -= 1
        reasons.append(f"{RED}❌ News sentiment negative{RESET}")
    elif news_delta < 0:
        reasons.append(f"{YELLOW}⚠  News sentiment mixed/bearish{RESET}")

    return score, reasons


# ═══════════════════════════════════════════════════════════════════════════
# Display sections
# ═══════════════════════════════════════════════════════════════════════════

_TICKERS = {
    # Market data
    "vix":    "^INDIAVIX",
    "nifty":  "^NSEI",
    "spx":    "^GSPC",
    "ndx":    "^IXIC",
    "nikkei": "^N225",
    "hsi":    "^HSI",
    "ftse":   "^FTSE",
    # Commodities
    "crude":  "CL=F",
    "gold":   "GC=F",
    "silver": "SI=F",
    "natgas": "NG=F",
    "copper": "HG=F",
    # Forex & rates
    "usdinr": "USDINR=X",
    "dxy":    "DX-Y.NYB",
    "us10y":  "^TNX",
}


def _quote_row(label: str, q: dict | None, unit: str = "",
               good_positive: bool = True, note: str = "") -> None:
    if q is None:
        _row(label, f"{DIM}unavailable{RESET}")
        return
    price_str = f"{unit}{q['price']:,.2f}"
    pct_str   = _pct(q["change_pct"], good_positive=good_positive)
    _row(label, f"{BOLD}{price_str}{RESET}  {pct_str}", note)


def main():
    now = datetime.now()
    print(f"\n{BOLD}{'█' * W}{RESET}")
    print(f"{BOLD}  PRE-MARKET PULSE  —  {now.strftime('%A, %d %b %Y  %H:%M')}{RESET}")
    print(f"{BOLD}{'█' * W}{RESET}")

    # ── Fetch all market data ──────────────────────────────────────────────
    print(f"\n{DIM}  Fetching market data...{RESET}", end="", flush=True)
    quotes = {}
    for key, ticker in _TICKERS.items():
        quotes[key] = _fetch_quote(ticker)
    print(f"\r{DIM}  Market data loaded.    {RESET}")

    # ── India VIX + Nifty ─────────────────────────────────────────────────
    _bar("INDIA VIX  +  NIFTY 50")
    vix = quotes["vix"]
    if vix:
        v = vix["price"]
        if v < 15:   vix_label = f"{GREEN}🟢 CALM (< 15){RESET}"
        elif v < 18: vix_label = f"{GREEN}🟡 NORMAL (15–18){RESET}"
        elif v < 20: vix_label = f"{YELLOW}🟡 ELEVATED (18–20){RESET}"
        elif v < 25: vix_label = f"{RED}🔴 HIGH (> 20)  — caution{RESET}"
        else:        vix_label = f"{RED}🔴 EXTREME (> 25)  — DO NOT TRADE{RESET}"
        _quote_row("India VIX",   vix,          good_positive=False)
        print(f"  {'':20}  {vix_label}")
    else:
        _row("India VIX", f"{DIM}unavailable{RESET}")

    _quote_row("Nifty 50",    quotes["nifty"], "₹")

    # ── Global Indices ─────────────────────────────────────────────────────
    _bar("GLOBAL INDICES  (overnight)")
    _quote_row("S&P 500",      quotes["spx"],    "$")
    _quote_row("Nasdaq",       quotes["ndx"],    "$")
    _quote_row("Nikkei 225",   quotes["nikkei"], "¥")
    _quote_row("Hang Seng",    quotes["hsi"],    "")
    _quote_row("FTSE 100",     quotes["ftse"],   "£")

    # ── Commodities ────────────────────────────────────────────────────────
    _bar("COMMODITIES")
    _quote_row("Crude Oil (WTI)", quotes["crude"],  "$",
               good_positive=False, note="↑ bad for India (imports)")
    _quote_row("Gold",            quotes["gold"],   "$",
               good_positive=False, note="↑ = risk-off signal")
    _quote_row("Silver",          quotes["silver"], "$")
    _quote_row("Natural Gas",     quotes["natgas"], "$")
    _quote_row("Copper",          quotes["copper"], "$",
               note="industrial demand proxy")

    # ── Forex & Rates ──────────────────────────────────────────────────────
    _bar("FOREX  +  RATES")
    _quote_row("USD / INR",     quotes["usdinr"],
               good_positive=False, note="↑ = Rupee weakening")
    _quote_row("Dollar Index",  quotes["dxy"],
               good_positive=False, note="↑ = EM headwind")
    q10y = quotes["us10y"]
    if q10y:
        _row("US 10Y Yield",
             f"{BOLD}{q10y['price']:.2f}%{RESET}  "
             f"{_pct(q10y['change_pct'], good_positive=False)}",
             note="↑ = tighter global liquidity")
    else:
        _row("US 10Y Yield", f"{DIM}unavailable{RESET}")

    # ── OSINT — News Headlines ─────────────────────────────────────────────
    _bar("OSINT  —  MARKET NEWS  (live RSS)")
    print(f"  {DIM}Fetching headlines...{RESET}", end="", flush=True)
    headlines = fetch_headlines(max_per_feed=4)
    news_delta, flagged = score_headlines(headlines)
    print(f"\r{' ' * 30}\r", end="")   # clear the fetching line

    if flagged:
        for line in flagged[:16]:   # cap at 16 total headlines
            print(f"  {line}")
        if len(headlines) > 16:
            print(f"  {DIM}... {len(headlines) - 16} more headlines fetched{RESET}")
    else:
        print(f"  {DIM}No headlines retrieved (check internet connection){RESET}")

    sentiment_label = (
        f"{GREEN}Positive (+{news_delta}){RESET}" if news_delta > 0
        else f"{RED}Bearish ({news_delta}){RESET}" if news_delta < 0
        else f"{YELLOW}Neutral{RESET}"
    )
    print(f"\n  Headline sentiment:  {sentiment_label}")

    # ── Go / No-Go ────────────────────────────────────────────────────────
    _bar("GO / NO-GO  RECOMMENDATION", char="═")
    score, reasons = build_score(
        vix=quotes["vix"],
        spx=quotes["spx"],
        crude=quotes["crude"],
        gold=quotes["gold"],
        usdinr=quotes["usdinr"],
        news_delta=news_delta,
    )

    for r in reasons:
        print(f"  {r}")

    print()
    if score >= 3:
        verdict = f"{GREEN}{BOLD}🟢  GO  — Market conditions look favourable  (score: {score}){RESET}"
    elif score >= 1:
        verdict = f"{YELLOW}{BOLD}🟡  CAUTION  — Proceed carefully, reduce position size  (score: {score}){RESET}"
    elif score >= -1:
        verdict = f"{YELLOW}{BOLD}🟠  CAUTION  — High uncertainty, paper mode recommended  (score: {score}){RESET}"
    else:
        verdict = f"{RED}{BOLD}🔴  NO-GO  — Do NOT run the bot today  (score: {score}){RESET}"

    print(f"  {verdict}")
    print(f"\n{BOLD}{'═' * W}{RESET}\n")


if __name__ == "__main__":
    main()
