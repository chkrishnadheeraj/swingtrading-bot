"""
Data Feed — Historical + Live Market Data
==========================================
Single interface for all price data in the bot.

Sources (in priority order):
    1. Kite Connect historical API   — accurate, SEBI-compliant, requires auth
    2. yfinance fallback             — for backtests and paper trading without auth
    3. KiteTicker WebSocket          — live real-time ticks (live mode only)

Usage:
    # With Kite auth (live mode)
    feed = DataFeed(broker=broker_client)

    # Without Kite auth (paper / backtest mode)
    feed = DataFeed(broker=None)

    # Fetch historical daily candles
    df = feed.history("RELIANCE", days=90)

    # Get current prices (live)
    prices = feed.ltp(["RELIANCE", "TCS", "SBIN"])

    # Start live WebSocket stream
    feed.subscribe_live(
        symbols=["RELIANCE", "TCS"],
        on_tick=my_callback,
    )
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, date
from typing import Optional, Callable
from functools import lru_cache

import pandas as pd
import numpy as np
import yfinance as yf

from config import settings

logger = logging.getLogger(__name__)


# ── tick callback type alias ──────────────────────────────────────────────
TickCallback = Callable[[str, float, dict], None]
# Called as: callback(symbol, last_price, full_tick_dict)


# ═══════════════════════════════════════════════════════════════════════════
# Historical Data Feed
# ═══════════════════════════════════════════════════════════════════════════

class HistoricalFeed:
    """
    Fetches OHLCV candle data.

    - In live mode (broker connected): uses Kite historical data API.
      Accurate intraday candles (1min – 1day), no 15-min delay.
    - In paper/backtest mode (no broker): falls back to yfinance.
      Daily candles only; ~15-min delayed intraday.

    All data is returned as a pandas DataFrame with lowercase column names:
        open, high, low, close, volume
    Index: pd.DatetimeIndex (timezone-naive, IST implied)
    """

    # Internal cache: { cache_key → DataFrame }
    # Avoids repeated API calls for the same symbol+date on the same day.
    _cache: dict[str, pd.DataFrame] = {}

    def __init__(self, broker=None):
        """
        Args:
            broker: BrokerClient instance (connected), or None for paper mode.
        """
        self.broker = broker
        self._token_map: dict[str, int] = {}   # symbol → instrument_token

    def get(
        self,
        symbol: str,
        days: int = 90,
        interval: str = "day",
        exchange: str = "NSE",
        force_refresh: bool = False,
    ) -> Optional[pd.DataFrame]:
        """
        Fetch OHLCV history for a symbol.

        Args:
            symbol:        NSE tradingsymbol, e.g. "RELIANCE"
            days:          Number of calendar days to look back.
            interval:      Candle size. One of:
                           "minute", "3minute", "5minute", "10minute",
                           "15minute", "30minute", "60minute", "day"
                           ⚠ For minute intervals, Kite allows max 60 days.
            exchange:      "NSE" (default), "BSE", "NFO", etc.
            force_refresh: Bypass cache and re-fetch.

        Returns:
            DataFrame with columns [open, high, low, close, volume]
            or None on failure.
        """
        cache_key = f"{exchange}:{symbol}:{interval}:{days}:{date.today()}"
        if not force_refresh and cache_key in self._cache:
            return self._cache[cache_key]

        df = None

        # ── Try Kite historical API first ────────────────────────────
        if self.broker and self.broker.is_connected():
            df = self._from_kite(symbol, days, interval, exchange)

        # ── Fallback: yfinance (paper / backtest) ────────────────────
        if df is None or df.empty:
            df = self._from_yfinance(symbol, days)

        if df is None or df.empty:
            logger.warning(f"No historical data available for {symbol}")
            return None

        self._cache[cache_key] = df
        return df

    def get_bulk(
        self,
        symbols: list[str],
        days: int = 90,
        interval: str = "day",
        exchange: str = "NSE",
    ) -> dict[str, pd.DataFrame]:
        """
        Fetch history for multiple symbols.
        Returns { symbol: DataFrame } — missing symbols are omitted.
        """
        result: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            df = self.get(sym, days=days, interval=interval, exchange=exchange)
            if df is not None:
                result[sym] = df
        logger.info(f"Historical data loaded: {len(result)}/{len(symbols)} symbols")
        return result

    # ── Kite historical API ───────────────────────────────────────────

    def _from_kite(
        self, symbol: str, days: int, interval: str, exchange: str
    ) -> Optional[pd.DataFrame]:
        try:
            token = self._resolve_token(symbol, exchange)
            if token is None:
                return None

            end_date   = datetime.now().strftime("%Y-%m-%d")
            start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

            candles = self.broker.historical_data(
                instrument_token=token,
                from_date=start_date,
                to_date=end_date,
                interval=interval,
            )

            if not candles:
                return None

            df = pd.DataFrame(candles)
            df.rename(columns={"date": "datetime"}, inplace=True)
            df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
            df.set_index("datetime", inplace=True)
            df.columns = [c.lower() for c in df.columns]
            df = df[["open", "high", "low", "close", "volume"]]

            logger.debug(
                f"Kite historical | {symbol} | {interval} | {len(df)} candles "
                f"({start_date} → {end_date})"
            )
            return df

        except Exception as exc:
            logger.warning(f"Kite historical failed for {symbol}: {exc}")
            return None

    def _resolve_token(self, symbol: str, exchange: str) -> Optional[int]:
        """Resolve instrument token, using cached map where possible."""
        key = f"{exchange}:{symbol}"
        if key not in self._token_map:
            token = self.broker.get_instrument_token(symbol, exchange)
            if token is None:
                return None
            self._token_map[key] = token
        return self._token_map[key]

    # ── yfinance fallback ─────────────────────────────────────────────

    def _from_yfinance(self, symbol: str, days: int) -> Optional[pd.DataFrame]:
        try:
            ticker = yf.Ticker(f"{symbol}.NS")
            df = ticker.history(
                start=(datetime.now() - timedelta(days=days + 5)).strftime("%Y-%m-%d"),
                end=datetime.now().strftime("%Y-%m-%d"),
            )
            if df.empty:
                return None

            df.columns = [c.lower() for c in df.columns]
            df.index   = pd.to_datetime(df.index).tz_localize(None)
            df = df[["open", "high", "low", "close", "volume"]].copy()

            logger.debug(f"yfinance fallback | {symbol} | {len(df)} daily candles")
            return df

        except Exception as exc:
            logger.error(f"yfinance failed for {symbol}: {exc}")
            return None

    def clear_cache(self):
        """Clear all cached data (call at start of new trading day)."""
        self._cache.clear()
        self._token_map.clear()
        logger.debug("Historical feed cache cleared")


# ═══════════════════════════════════════════════════════════════════════════
# Live Quote Feed — current prices via Kite REST
# ═══════════════════════════════════════════════════════════════════════════

class QuoteFeed:
    """
    Live market snapshots via Kite REST endpoints.
    Ideal for periodic price checks (position monitoring, pre-scan, etc.)
    For continuous real-time data, use TickFeed instead.
    """

    def __init__(self, broker=None):
        self.broker = broker

    def ltp(self, symbols: list[str], exchange: str = "NSE") -> dict[str, float]:
        """
        Last traded price for a list of symbols.

        Uses Kite API if connected, yfinance otherwise.
        Kite allows up to 500 symbols per call.

        Returns:
            { "RELIANCE": 2455.50, "TCS": 3210.00, ... }
        """
        if self.broker and self.broker.is_connected():
            try:
                keys   = [f"{exchange}:{sym}" for sym in symbols]
                prices = self.broker.ltp(*keys)
                # Strip "NSE:" prefix from keys
                return {
                    k.split(":", 1)[1]: v
                    for k, v in prices.items()
                }
            except Exception as exc:
                logger.warning(f"Kite LTP failed: {exc} — falling back to yfinance")

        # yfinance fallback
        return self._ltp_yfinance(symbols)

    def ohlc(self, symbols: list[str], exchange: str = "NSE") -> dict[str, dict]:
        """
        OHLC snapshot for a list of symbols.
        Returns { "RELIANCE": { "open": x, "high": x, "low": x, "close": x,
                                "last_price": x }, ... }
        """
        if self.broker and self.broker.is_connected():
            try:
                keys = [f"{exchange}:{sym}" for sym in symbols]
                raw  = self.broker.ohlc(*keys)
                return {
                    k.split(":", 1)[1]: {
                        **v.get("ohlc", {}),
                        "last_price": v.get("last_price", 0),
                    }
                    for k, v in raw.items()
                }
            except Exception as exc:
                logger.warning(f"Kite OHLC failed: {exc}")

        return {}

    def quote(self, symbol: str, exchange: str = "NSE") -> dict:
        """Full market depth quote for a single symbol (Kite only)."""
        if self.broker and self.broker.is_connected():
            try:
                return self.broker.quote_single(symbol, exchange)
            except Exception as exc:
                logger.warning(f"Kite quote failed for {symbol}: {exc}")
        return {}

    def _ltp_yfinance(self, symbols: list[str]) -> dict[str, float]:
        """Fallback LTP using yfinance (delayed ~15min)."""
        result: dict[str, float] = {}
        for sym in symbols:
            try:
                ticker = yf.Ticker(f"{sym}.NS")
                data   = ticker.history(period="1d")
                if not data.empty:
                    result[sym] = float(data["Close"].iloc[-1])
            except Exception as exc:
                logger.error(f"yfinance LTP failed for {sym}: {exc}")
        return result


# ═══════════════════════════════════════════════════════════════════════════
# Live Tick Feed — real-time WebSocket streaming
# ═══════════════════════════════════════════════════════════════════════════

class TickFeed:
    """
    Real-time tick stream via Kite Connect WebSocket (KiteTicker).

    Kite WebSocket tiers:
        MODE_LTP   : last_price only — ultra-low bandwidth
        MODE_QUOTE : OHLC + last trade + 5-level depth
        MODE_FULL  : full 20-level market depth + OI

    Usage:
        feed = TickFeed(broker)
        feed.start(["RELIANCE", "TCS"], on_tick=my_handler, mode="full")
        # ... trading ...
        feed.stop()
    """

    def __init__(self, broker):
        self.broker   = broker
        self._running = False
        self._token_map: dict[str, int] = {}   # symbol → instrument_token
        self._symbol_map: dict[int, str] = {}  # instrument_token → symbol

        # Latest tick storage: { symbol → tick_dict }
        self._latest: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(
        self,
        symbols: list[str],
        on_tick: Optional[TickCallback] = None,
        exchange: str = "NSE",
        mode: str = "full",               # "ltp", "quote", or "full"
    ):
        """
        Start live tick streaming for the given symbols.

        Args:
            symbols:  List of NSE tradingsymbols.
            on_tick:  Optional callback — called for every tick.
                      Signature: callback(symbol, last_price, tick_dict)
            exchange: Exchange to look up tokens (default: NSE).
            mode:     Tick mode — "ltp", "quote", or "full".
        """
        if not self.broker or not self.broker.is_connected():
            raise RuntimeError(
                "TickFeed requires a connected BrokerClient. "
                "Run auth.py and call broker.connect() first."
            )

        # Resolve all instrument tokens
        token_map = self.broker.get_instrument_tokens_bulk(symbols, exchange)
        self._token_map = token_map
        self._symbol_map = {v: k for k, v in token_map.items()}
        tokens = list(token_map.values())

        if not tokens:
            raise ValueError(f"No instrument tokens found for: {symbols}")

        from core.broker import MODE_FULL, MODE_QUOTE, MODE_LTP
        mode_map = {
            "full":  MODE_FULL,
            "quote": MODE_QUOTE,
            "ltp":   MODE_LTP,
        }
        tick_mode = mode_map.get(mode.lower(), MODE_FULL)

        def _on_tick(ws, ticks: list[dict]):
            with self._lock:
                for tick in ticks:
                    token  = tick.get("instrument_token")
                    symbol = self._symbol_map.get(token, str(token))
                    ltp    = float(tick.get("last_price", 0))
                    self._latest[symbol] = tick

                    if on_tick:
                        try:
                            on_tick(symbol, ltp, tick)
                        except Exception as exc:
                            logger.error(f"on_tick callback error [{symbol}]: {exc}")

        def _on_connect(ws, response):
            logger.info(
                f"WebSocket connected | Subscribed: {list(token_map.keys())} "
                f"| Mode: {mode}"
            )

        def _on_close(ws, code, reason):
            self._running = False
            logger.warning(f"WebSocket closed: {code} — {reason}")

        self.broker.start_ticker(
            instrument_tokens=tokens,
            on_tick=_on_tick,
            on_connect=_on_connect,
            on_close=_on_close,
            mode=tick_mode,
        )
        self._running = True
        logger.info(f"TickFeed started | {len(tokens)} instruments | mode={mode}")

    def get_ltp(self, symbol: str) -> Optional[float]:
        """Get the most recently received price for a symbol (thread-safe)."""
        with self._lock:
            tick = self._latest.get(symbol)
            return float(tick["last_price"]) if tick else None

    def get_tick(self, symbol: str) -> Optional[dict]:
        """Get the full latest tick dict for a symbol (thread-safe)."""
        with self._lock:
            return self._latest.get(symbol)

    def get_all_ltps(self) -> dict[str, float]:
        """Get latest prices for all subscribed symbols."""
        with self._lock:
            return {
                sym: float(tick["last_price"])
                for sym, tick in self._latest.items()
            }

    def add_symbols(self, symbols: list[str], exchange: str = "NSE"):
        """Add more symbols to an already-running WebSocket subscription."""
        new_tokens = self.broker.get_instrument_tokens_bulk(symbols, exchange)
        self._token_map.update(new_tokens)
        self._symbol_map.update({v: k for k, v in new_tokens.items()})
        self.broker.subscribe(list(new_tokens.values()))
        logger.info(f"TickFeed: added {list(new_tokens.keys())}")

    def remove_symbols(self, symbols: list[str]):
        """Unsubscribe symbols from the live WebSocket stream."""
        tokens = [self._token_map.pop(sym, None) for sym in symbols]
        tokens = [t for t in tokens if t is not None]
        if tokens:
            self.broker.unsubscribe(tokens)
            for t in tokens:
                self._symbol_map.pop(t, None)
        logger.info(f"TickFeed: removed {symbols}")

    def is_running(self) -> bool:
        return self._running

    def stop(self):
        """Stop the live WebSocket stream."""
        self.broker.stop_ticker()
        self._running  = False
        self._latest   = {}
        logger.info("TickFeed stopped")


# ═══════════════════════════════════════════════════════════════════════════
# Unified DataFeed — single interface used by the engine
# ═══════════════════════════════════════════════════════════════════════════

class DataFeed:
    """
    Unified data layer for the trading engine.
    Composes HistoricalFeed + QuoteFeed + TickFeed.

    In paper mode  (broker=None): uses yfinance for everything.
    In live mode   (broker connected): uses Kite for everything.

    Example:
        feed = DataFeed(broker=broker_client)   # live
        feed = DataFeed()                        # paper / backtest

        # Historical OHLCV
        df = feed.history("RELIANCE", days=90)

        # Current price
        prices = feed.ltp(["RELIANCE", "TCS"])

        # Real-time stream
        feed.subscribe(["RELIANCE", "TCS"], on_tick=my_cb)
    """

    def __init__(self, broker=None):
        self._broker    = broker
        self.historical = HistoricalFeed(broker)
        self.quotes     = QuoteFeed(broker)
        self.ticks      = TickFeed(broker) if broker else None

        mode = "LIVE (Kite)" if (broker and broker.is_connected()) else "PAPER (yfinance)"
        logger.info(f"DataFeed initialized | Mode: {mode}")

    # ── Historical ───────────────────────────────────────────────────

    def history(
        self,
        symbol: str,
        days: int = 90,
        interval: str = "day",
        exchange: str = "NSE",
        force_refresh: bool = False,
    ) -> Optional[pd.DataFrame]:
        """Fetch OHLCV history. See HistoricalFeed.get() for full docs."""
        return self.historical.get(
            symbol, days=days, interval=interval,
            exchange=exchange, force_refresh=force_refresh
        )

    def history_bulk(
        self,
        symbols: list[str],
        days: int = 90,
        interval: str = "day",
        exchange: str = "NSE",
    ) -> dict[str, pd.DataFrame]:
        """Fetch history for multiple symbols. See HistoricalFeed.get_bulk()."""
        return self.historical.get_bulk(symbols, days=days,
                                        interval=interval, exchange=exchange)

    # ── Live prices ──────────────────────────────────────────────────

    def ltp(
        self, symbols: list[str], exchange: str = "NSE"
    ) -> dict[str, float]:
        """
        Last traded price for a list of symbols.
        Uses Kite REST API if connected, yfinance otherwise.
        """
        # If tick feed is running, serve from in-memory cache (faster, free)
        if self.ticks and self.ticks.is_running():
            cached = {}
            missing = []
            for sym in symbols:
                p = self.ticks.get_ltp(sym)
                if p is not None:
                    cached[sym] = p
                else:
                    missing.append(sym)

            if missing:
                REST = self.quotes.ltp(missing, exchange)
                cached.update(REST)
            return cached

        return self.quotes.ltp(symbols, exchange)

    def ohlc(
        self, symbols: list[str], exchange: str = "NSE"
    ) -> dict[str, dict]:
        """OHLC snapshot for a list of symbols."""
        return self.quotes.ohlc(symbols, exchange)

    def quote(self, symbol: str, exchange: str = "NSE") -> dict:
        """Full market depth quote (Kite only, falls back to {})."""
        return self.quotes.quote(symbol, exchange)

    # ── WebSocket streaming ──────────────────────────────────────────

    def subscribe(
        self,
        symbols: list[str],
        on_tick: Optional[TickCallback] = None,
        exchange: str = "NSE",
        mode: str = "full",
    ):
        """
        Start live WebSocket tick stream for given symbols.
        Requires a connected BrokerClient.

        Callback signature: on_tick(symbol: str, last_price: float, tick: dict)
        """
        if not self.ticks:
            logger.warning(
                "WebSocket unavailable in paper mode. "
                "Provide a connected BrokerClient to DataFeed."
            )
            return
        self.ticks.start(symbols, on_tick=on_tick, exchange=exchange, mode=mode)

    def unsubscribe(self, symbols: list[str]):
        """Remove symbols from the live tick stream."""
        if self.ticks:
            self.ticks.remove_symbols(symbols)

    def stop_stream(self):
        """Stop the live tick WebSocket."""
        if self.ticks:
            self.ticks.stop()

    def add_to_stream(self, symbols: list[str], exchange: str = "NSE"):
        """Add symbols to an already-running WebSocket stream."""
        if self.ticks and self.ticks.is_running():
            self.ticks.add_symbols(symbols, exchange)

    # ── Utilities ────────────────────────────────────────────────────

    def clear_cache(self):
        """Clear historical data cache (call at start of trading day)."""
        self.historical.clear_cache()

    @property
    def is_live(self) -> bool:
        """True if connected to Kite (live mode), False if using yfinance."""
        return self._broker is not None and self._broker.is_connected()
