"""
Kite Connect Broker Wrapper — Full API Implementation
======================================================
Covers all three Kite Connect API tiers available in the plan:

  1. Trading, Investing & Reports
     - Orders     : market, limit, SL, SL-M, CO, ICO
     - GTT orders : single-leg and two-leg (OCO)
     - Portfolio   : positions, holdings, auction instruments
     - Reports     : tradebook, orderbook, order history
     - Margins     : available funds, order margin calc, basket margins

  2. Historical Chart Data
     - OHLCV candles for any instrument × any interval
     - Instrument token lookup (cached, one fetch per exchange per day)

  3. Live Market Quotes
     - LTP   (single & bulk, up to 500 instruments)
     - OHLC  (open/high/low/close snapshot)
     - Quote (full market depth + last trade)
     - WebSocket ticker (full, quote, or LTP mode)

Auth flow (daily):
    python auth.py   →  saves KITE_ACCESS_TOKEN to config/.env
    BrokerClient().connect()  →  verifies token, ready to trade
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Callable

from kiteconnect import KiteConnect, KiteTicker
from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# KiteTicker tick modes
MODE_FULL  = KiteTicker.MODE_FULL   # Full market depth
MODE_QUOTE = KiteTicker.MODE_QUOTE  # OHLC + last trade + market depth (top 5)
MODE_LTP   = KiteTicker.MODE_LTP    # Last traded price only

# Exchange identifiers
NSE = "NSE"
BSE = "BSE"
NFO = "NFO"   # NSE F&O
MCX = "MCX"   # Commodity

# Product types
CNC  = KiteConnect.PRODUCT_CNC    # Cash-and-carry (delivery)
MIS  = KiteConnect.PRODUCT_MIS    # Intraday margin
NRML = KiteConnect.PRODUCT_NRML   # Normal (for F&O)

# Order types
MARKET = KiteConnect.ORDER_TYPE_MARKET
LIMIT  = KiteConnect.ORDER_TYPE_LIMIT
SL     = KiteConnect.ORDER_TYPE_SL     # Stop-Loss Limit
SLM    = KiteConnect.ORDER_TYPE_SLM    # Stop-Loss Market

# Transaction types
BUY  = KiteConnect.TRANSACTION_TYPE_BUY
SELL = KiteConnect.TRANSACTION_TYPE_SELL

# Order varieties
REGULAR = KiteConnect.VARIETY_REGULAR
CO      = KiteConnect.VARIETY_CO     # Cover order
AMO     = KiteConnect.VARIETY_AMO    # After-market order
ICEBERG = KiteConnect.VARIETY_ICEBERG

# Historical candle intervals
INTERVAL_MINUTE   = "minute"
INTERVAL_3MIN     = "3minute"
INTERVAL_5MIN     = "5minute"
INTERVAL_10MIN    = "10minute"
INTERVAL_15MIN    = "15minute"
INTERVAL_30MIN    = "30minute"
INTERVAL_60MIN    = "60minute"
INTERVAL_DAY      = "day"

# GTT trigger types
GTT_TYPE_SINGLE = KiteConnect.GTT_TYPE_SINGLE  # SL-style, one trigger
GTT_TYPE_OCO    = KiteConnect.GTT_TYPE_OCO     # One-cancels-other (SL + target)

# Validity
VALIDITY_DAY = KiteConnect.VALIDITY_DAY
VALIDITY_IOC = KiteConnect.VALIDITY_IOC
VALIDITY_TTL = KiteConnect.VALIDITY_TTL


# ---------------------------------------------------------------------------
# Auth helper (run once per day via auth.py)
# ---------------------------------------------------------------------------

class KiteAuth:
    """
    One-shot daily authentication helper.
    Typically called by auth.py, not the bot directly.

    Usage:
        auth = KiteAuth()
        url  = auth.get_login_url()       # open in browser
        tok  = auth.generate_session(request_token)  # paste from redirect URL
    """

    def __init__(self):
        if not settings.KITE_API_KEY:
            raise EnvironmentError(
                "KITE_API_KEY not set. Add it to config/.env\n"
                "Get your API key from: https://developers.kite.trade/apps"
            )
        self.kite = KiteConnect(api_key=settings.KITE_API_KEY)

    def get_login_url(self) -> str:
        url = self.kite.login_url()
        logger.info(f"Kite login URL: {url}")
        return url

    def generate_session(self, request_token: str) -> str:
        """
        Exchange the request_token (from redirect URL) for an access_token.
        Saves the token to config/.env and returns it.
        Token is valid for the current trading day only.
        """
        if not settings.KITE_API_SECRET:
            raise EnvironmentError("KITE_API_SECRET not set in config/.env")

        data = self.kite.generate_session(
            request_token, api_secret=settings.KITE_API_SECRET
        )
        access_token = data["access_token"]
        _write_env("KITE_ACCESS_TOKEN", access_token)

        logger.info(
            f"Session OK | User: {data.get('user_name')} ({data.get('user_id')}) "
            f"| Login: {data.get('login_time')}"
        )
        return access_token


# ---------------------------------------------------------------------------
# Main broker client
# ---------------------------------------------------------------------------

class BrokerClient:
    """
    Full Kite Connect v3 API wrapper.

    Initialise once at engine startup:
        broker = BrokerClient()
        broker.connect()        # verifies token from KITE_ACCESS_TOKEN env var

    All methods raise RuntimeError if connect() hasn't been called.
    """

    def __init__(self):
        if not settings.KITE_API_KEY:
            raise EnvironmentError("KITE_API_KEY not set in config/.env")

        self.kite = KiteConnect(api_key=settings.KITE_API_KEY)
        self._token: str = os.getenv("KITE_ACCESS_TOKEN", "")
        self._connected = False
        self._ticker: Optional[KiteTicker] = None

        # Cache: {exchange → [{instrument dict}, ...]} refreshed once per day
        self._instruments_cache: dict[str, list[dict]] = {}
        self._instruments_date: str = ""

    # ===================================================================
    # Connection
    # ===================================================================

    def connect(self) -> dict:
        """
        Authenticate with the saved access token.
        Must be called once before any other method.

        Returns:
            User profile dict from Zerodha.

        Raises:
            EnvironmentError if KITE_ACCESS_TOKEN is not set.
            ConnectionError if the token is invalid/expired.
        """
        if not self._token:
            raise EnvironmentError(
                "KITE_ACCESS_TOKEN not set.\n"
                "Run:  python auth.py\n"
                "This generates a fresh token valid for today's session."
            )

        self.kite.set_access_token(self._token)

        try:
            profile = self.kite.profile()
            self._connected = True
            logger.info(
                f"Kite connected | User: {profile['user_name']} ({profile['user_id']}) "
                f"| Exchanges: {', '.join(profile.get('exchanges', []))}"
            )
            return profile
        except Exception as exc:
            self._connected = False
            raise ConnectionError(
                f"Kite auth failed — token is invalid or expired: {exc}\n"
                f"Run  python auth.py  to get a fresh token."
            ) from exc

    def is_connected(self) -> bool:
        return self._connected

    def _check(self):
        if not self._connected:
            raise RuntimeError("BrokerClient not connected. Call connect() first.")

    # ===================================================================
    # ── 1. TRADING, INVESTING & REPORTS APIs ──────────────────────────
    # ===================================================================

    # -------------------------------------------------------------------
    # Orders
    # -------------------------------------------------------------------

    def place_order(
        self,
        tradingsymbol: str,
        transaction_type: str,           # "BUY" or "SELL"
        quantity: int,
        order_type: str = MARKET,
        price: float = 0.0,              # required for LIMIT / SL
        trigger_price: float = 0.0,      # required for SL / SL-M
        product: str = CNC,
        exchange: str = NSE,
        variety: str = REGULAR,
        validity: str = VALIDITY_DAY,
        disclosed_quantity: int = 0,
        squareoff: float = 0.0,          # for CO orders
        stoploss: float = 0.0,           # for CO orders
        trailing_stoploss: float = 0.0,  # for CO orders
        iceberg_legs: int = 0,           # for iceberg orders (2-10)
        iceberg_quantity: int = 0,       # quantity per iceberg leg
        tag: str = "momentum_bot",
    ) -> str:
        """
        Unified order placement supporting all varieties and types.

        Returns:
            order_id (str)

        Raises:
            kiteconnect.exceptions.KiteException on API errors.

        Examples:
            # Market buy (delivery)
            broker.place_order("RELIANCE", "BUY", 1)

            # Limit sell
            broker.place_order("TCS", "SELL", 1, order_type=LIMIT, price=3500)

            # Stop-loss market (exit trigger)
            broker.place_order("SBIN", "SELL", 5,
                               order_type=SLM, trigger_price=580)

            # Cover order (intraday with built-in SL)
            broker.place_order("HDFCBANK", "BUY", 1,
                               variety=CO, product=MIS,
                               price=1650, stoploss=30)
        """
        self._check()

        txn = BUY if transaction_type.upper() == "BUY" else SELL

        kwargs: dict = dict(
            variety=variety,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=txn,
            quantity=quantity,
            product=product,
            order_type=order_type,
            validity=validity,
            tag=tag,
        )

        if order_type in (LIMIT, SL):
            kwargs["price"] = price
        if order_type in (SL, SLM):
            kwargs["trigger_price"] = trigger_price
        if disclosed_quantity:
            kwargs["disclosed_quantity"] = disclosed_quantity

        # Cover order extras
        if variety == CO:
            kwargs["price"]    = price
            kwargs["stoploss"] = stoploss
            if trailing_stoploss:
                kwargs["trailing_stoploss"] = trailing_stoploss

        # Iceberg
        if variety == ICEBERG:
            kwargs["iceberg_legs"]     = iceberg_legs
            kwargs["iceberg_quantity"] = iceberg_quantity

        order_id = str(self.kite.place_order(**kwargs))
        logger.info(
            f"ORDER PLACED | {transaction_type} {tradingsymbol} ×{quantity} "
            f"[{order_type}/{variety}/{product}] | ID: {order_id}"
        )
        return order_id

    # Convenience wrappers -------------------------------------------------

    def buy(self, symbol: str, qty: int, exchange: str = NSE,
            product: str = CNC, tag: str = "momentum_bot") -> str:
        """Market BUY — simplest entry."""
        return self.place_order(symbol, "BUY", qty,
                                exchange=exchange, product=product, tag=tag)

    def sell(self, symbol: str, qty: int, exchange: str = NSE,
             product: str = CNC, tag: str = "momentum_bot") -> str:
        """Market SELL — simplest exit."""
        return self.place_order(symbol, "SELL", qty,
                                exchange=exchange, product=product, tag=tag)

    def buy_limit(self, symbol: str, qty: int, price: float,
                  exchange: str = NSE, product: str = CNC,
                  tag: str = "momentum_bot") -> str:
        """Limit BUY."""
        return self.place_order(symbol, "BUY", qty,
                                order_type=LIMIT, price=price,
                                exchange=exchange, product=product, tag=tag)

    def sell_sl_market(self, symbol: str, qty: int, trigger_price: float,
                       exchange: str = NSE, product: str = CNC,
                       tag: str = "momentum_bot") -> str:
        """Stop-Loss Market SELL — used for stop-loss orders."""
        return self.place_order(symbol, "SELL", qty,
                                order_type=SLM, trigger_price=trigger_price,
                                exchange=exchange, product=product, tag=tag)

    def modify_order(
        self,
        order_id: str,
        variety: str = REGULAR,
        quantity: Optional[int] = None,
        price: Optional[float] = None,
        order_type: Optional[str] = None,
        trigger_price: Optional[float] = None,
        validity: Optional[str] = None,
        disclosed_quantity: Optional[int] = None,
    ) -> str:
        """Modify a pending order. Pass only the fields you want to change."""
        self._check()
        kwargs: dict = {"variety": variety, "order_id": order_id}
        if quantity is not None:         kwargs["quantity"]           = quantity
        if price is not None:            kwargs["price"]              = price
        if order_type is not None:       kwargs["order_type"]         = order_type
        if trigger_price is not None:    kwargs["trigger_price"]      = trigger_price
        if validity is not None:         kwargs["validity"]           = validity
        if disclosed_quantity is not None:kwargs["disclosed_quantity"] = disclosed_quantity

        self.kite.modify_order(**kwargs)
        logger.info(f"Order modified: {order_id} | {kwargs}")
        return order_id

    def cancel_order(self, order_id: str, variety: str = REGULAR) -> str:
        """Cancel a pending order."""
        self._check()
        self.kite.cancel_order(variety=variety, order_id=order_id)
        logger.info(f"Order cancelled: {order_id}")
        return order_id

    # -------------------------------------------------------------------
    # Reports — Orderbook, Tradebook, Order history
    # -------------------------------------------------------------------

    def get_orders(self) -> list[dict]:
        """
        All orders for today (placed, pending, cancelled, rejected).
        Returns list of order dicts with fields:
          order_id, tradingsymbol, status, transaction_type, quantity,
          price, average_price, filled_quantity, pending_quantity, etc.
        """
        self._check()
        return self.kite.orders()

    def get_order_history(self, order_id: str) -> list[dict]:
        """
        Full state history of a single order (every status transition).
        Useful for debugging partial fills and rejections.
        """
        self._check()
        return self.kite.order_history(order_id)

    def get_trades(self) -> list[dict]:
        """
        Tradebook — all executed (filled) trades for today.
        Each fill is a separate entry with fill_timestamp, average_price, etc.
        """
        self._check()
        return self.kite.trades()

    def get_order_trades(self, order_id: str) -> list[dict]:
        """Fills for a specific order (handles partial fills)."""
        self._check()
        return self.kite.order_trades(order_id)

    # -------------------------------------------------------------------
    # Portfolio — Positions & Holdings
    # -------------------------------------------------------------------

    def get_positions(self) -> dict:
        """
        Intraday (day) and net (multi-day) positions.

        Returns:
            {
              "day": [{ tradingsymbol, buy_quantity, sell_quantity,
                        buy_price, sell_price, pnl, ... }],
              "net": [{ ... }]
            }
        """
        self._check()
        return self.kite.positions()

    def get_holdings(self) -> list[dict]:
        """
        Long-term delivery holdings (CNC positions).
        Includes average cost, last price, P&L, collateral value.
        """
        self._check()
        return self.kite.holdings()

    def get_auction_instruments(self) -> list[dict]:
        """Instruments available in the current auction session."""
        self._check()
        return self.kite.auction_instruments()

    def convert_position(
        self,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        position_type: str,
        quantity: int,
        old_product: str,
        new_product: str,
    ) -> bool:
        """
        Convert a position between MIS ↔ CNC (intraday ↔ delivery).
        Must be called before 3:30 PM on the same day.
        """
        self._check()
        return self.kite.convert_position(
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=BUY if transaction_type.upper() == "BUY" else SELL,
            position_type=position_type,
            quantity=quantity,
            old_product=old_product,
            new_product=new_product,
        )

    # -------------------------------------------------------------------
    # Margins — Available funds & order margin estimation
    # -------------------------------------------------------------------

    def get_margins(self, segment: Optional[str] = None) -> dict:
        """
        Available margin / funds.

        Args:
            segment: "equity" or "commodity". None returns both.

        Returns:
            {
              "equity": {
                "available": { "cash": float, "intraday_payin": float, ... },
                "utilised":  { "span": float, "exposure": float, ... }
              },
              "commodity": { ... }
            }
        """
        self._check()
        return self.kite.margins(segment=segment)

    def get_available_cash(self) -> float:
        """Convenience: available cash in the equity segment."""
        return float(
            self.get_margins("equity")
            .get("available", {})
            .get("cash", 0.0)
        )

    def get_order_margins(self, orders: list[dict]) -> list[dict]:
        """
        Calculate the margin required for a list of orders BEFORE placing them.

        Each order dict should contain:
            exchange, tradingsymbol, transaction_type, variety,
            product, order_type, quantity, price, trigger_price (opt)

        Returns list of margin breakdowns (one per order):
            { type, tradingsymbol, exposure, span, option_premium,
              additional, bo_additional, cash, var, pnl, total }
        """
        self._check()
        return self.kite.order_margins(orders)

    def get_basket_margins(
        self, orders: list[dict], consider_positions: bool = True
    ) -> dict:
        """
        Margin required for a basket of orders (considers netting benefit).
        Useful for multi-leg F&O strategies.

        Returns:
            { initial: {total, span, ...}, spread: {total, ...}, orders: [...] }
        """
        self._check()
        return self.kite.basket_order_margins(
            orders, consider_positions=consider_positions
        )

    # -------------------------------------------------------------------
    # GTT — Good Till Triggered Orders
    # -------------------------------------------------------------------

    def place_gtt(
        self,
        trigger_type: str,          # GTT_TYPE_SINGLE or GTT_TYPE_OCO
        tradingsymbol: str,
        exchange: str,
        trigger_values: list[float], # [sl_price] for SINGLE; [sl, target] for OCO
        last_price: float,
        orders: list[dict],          # order specs (same as place_order params)
    ) -> int:
        """
        Place a GTT (Good Till Triggered) order — persists across sessions
        until triggered or cancelled. Useful for long-held delivery positions.

        For SINGLE (stop-loss protection):
            trigger_values = [stop_price]
            orders = [{ transaction_type, quantity, price, order_type, product }]

        For OCO (target + stop-loss):
            trigger_values = [stop_price, target_price]
            orders = [
                { transaction_type: "SELL", quantity, price: stop_price, ... },
                { transaction_type: "SELL", quantity, price: target_price, ... },
            ]

        Returns:
            trigger_id (int)
        """
        self._check()
        trigger_id = self.kite.place_gtt(
            trigger_type=trigger_type,
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            trigger_values=trigger_values,
            last_price=last_price,
            orders=orders,
        )
        logger.info(
            f"GTT placed | {trigger_type} | {tradingsymbol} | "
            f"triggers={trigger_values} | ID={trigger_id}"
        )
        return trigger_id

    def place_gtt_oco(
        self,
        tradingsymbol: str,
        exchange: str,
        quantity: int,
        entry_price: float,
        stop_price: float,
        target_price: float,
        product: str = CNC,
    ) -> int:
        """
        Convenience: place an OCO GTT (stop + target) for a long position.
        Both legs are SELL orders.
        """
        orders = [
            {
                "transaction_type": "SELL",
                "quantity": quantity,
                "price": round(stop_price * 0.995, 1),  # slight slippage buffer
                "order_type": LIMIT,
                "product": product,
            },
            {
                "transaction_type": "SELL",
                "quantity": quantity,
                "price": round(target_price * 1.001, 1),
                "order_type": LIMIT,
                "product": product,
            },
        ]
        return self.place_gtt(
            trigger_type=GTT_TYPE_OCO,
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            trigger_values=[stop_price, target_price],
            last_price=entry_price,
            orders=orders,
        )

    def modify_gtt(self, trigger_id: int, **kwargs) -> int:
        """Modify an existing GTT order."""
        self._check()
        self.kite.modify_gtt(trigger_id, **kwargs)
        logger.info(f"GTT modified: {trigger_id}")
        return trigger_id

    def cancel_gtt(self, trigger_id: int) -> int:
        """Cancel a GTT order."""
        self._check()
        self.kite.delete_gtt(trigger_id)
        logger.info(f"GTT cancelled: {trigger_id}")
        return trigger_id

    def get_gtt(self, trigger_id: int) -> dict:
        """Get a single GTT order."""
        self._check()
        return self.kite.get_gtt(trigger_id)

    def get_gtts(self) -> list[dict]:
        """Get all active GTT orders."""
        self._check()
        return self.kite.get_gtts()

    # ===================================================================
    # ── 2. HISTORICAL CHART DATA API ──────────────────────────────────
    # ===================================================================

    def historical_data(
        self,
        instrument_token: int,
        from_date: str,          # "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS"
        to_date: str,
        interval: str = INTERVAL_DAY,
        continuous: bool = False,
        oi: bool = False,        # include open interest (for F&O)
    ) -> list[dict]:
        """
        Fetch OHLCV candles from Kite historical data API.

        Args:
            instrument_token: Integer token (get via get_instrument_token()).
            from_date / to_date: Date range as "YYYY-MM-DD".
            interval: One of INTERVAL_* constants.
                      Day candles have no per-day call limit.
                      Minute candles: max 60 days per request.
            continuous: For F&O, stitch across expiry boundaries.
            oi: Include open interest in response (F&O only).

        Returns:
            List of dicts:
            [{ "date": datetime, "open": float, "high": float,
               "low": float, "close": float, "volume": int,
               "oi": int (if oi=True) }, ...]

        Rate limits:
            3 req/sec  |  historical data endpoint
            No per-day limit for day candles.
            Minute candles: max 60 calendar days per request.
        """
        self._check()
        data = self.kite.historical_data(
            instrument_token, from_date, to_date,
            interval, continuous=continuous, oi=oi
        )
        logger.debug(
            f"Historical data: token={instrument_token} | {interval} | "
            f"{from_date} → {to_date} | {len(data)} candles"
        )
        return data

    def historical_data_by_symbol(
        self,
        symbol: str,
        from_date: str,
        to_date: str,
        interval: str = INTERVAL_DAY,
        exchange: str = NSE,
        continuous: bool = False,
    ) -> list[dict]:
        """
        Convenience: fetch historical data using symbol name (auto-resolves token).
        """
        token = self.get_instrument_token(symbol, exchange)
        if token is None:
            raise ValueError(f"Instrument token not found for {exchange}:{symbol}")
        return self.historical_data(token, from_date, to_date, interval, continuous)

    # ===================================================================
    # ── 3. LIVE MARKET QUOTES & INSTRUMENTS ───────────────────────────
    # ===================================================================

    def ltp(self, *symbols: str) -> dict[str, float]:
        """
        Last Traded Price for one or more instruments.
        Fast, minimal data — best for price checks.

        Args:
            symbols: "NSE:RELIANCE", "NSE:TCS", etc. Up to 500 per call.

        Returns:
            { "NSE:RELIANCE": 2455.50, "NSE:TCS": 3210.00, ... }

        Example:
            prices = broker.ltp("NSE:RELIANCE", "NSE:TCS")
        """
        self._check()
        raw = self.kite.ltp(list(symbols))
        return {k: float(v["last_price"]) for k, v in raw.items()}

    def ltp_single(self, symbol: str, exchange: str = NSE) -> float:
        """LTP for a single NSE symbol."""
        prices = self.ltp(f"{exchange}:{symbol}")
        return prices[f"{exchange}:{symbol}"]

    def ohlc(self, *symbols: str) -> dict[str, dict]:
        """
        OHLC snapshot for one or more instruments.

        Returns:
            {
              "NSE:RELIANCE": {
                "last_price": 2455.5,
                "ohlc": { "open": 2440, "high": 2460, "low": 2430, "close": 2445 }
              }, ...
            }
        """
        self._check()
        return self.kite.ohlc(list(symbols))

    def quote(self, *symbols: str) -> dict[str, dict]:
        """
        Full market snapshot including order book depth, 52-week high/low,
        circuit limits, VWAP, OI (for F&O), last trade info, etc.

        Returns a dict keyed by "EXCHANGE:SYMBOL" containing all available
        market data fields.

        Note: Heavier than ltp() / ohlc() — use for detailed analysis,
              not for real-time price loops (prefer WebSocket for that).
        """
        self._check()
        return self.kite.quote(list(symbols))

    def quote_single(self, symbol: str, exchange: str = NSE) -> dict:
        """Full quote for a single symbol."""
        q = self.quote(f"{exchange}:{symbol}")
        return q[f"{exchange}:{symbol}"]

    # -------------------------------------------------------------------
    # Instruments (token lookup)
    # -------------------------------------------------------------------

    def get_instruments(self, exchange: str = NSE) -> list[dict]:
        """
        Download the full instruments CSV for an exchange (parsed to list of dicts).
        Cached for the current calendar day to avoid repeated downloads.

        Instrument dict keys:
            instrument_token, exchange_token, tradingsymbol, name,
            last_price, expiry, strike, tick_size, lot_size,
            instrument_type, segment, exchange
        """
        self._check()
        today = date.today().isoformat()
        if self._instruments_date != today or exchange not in self._instruments_cache:
            logger.info(f"Downloading instruments for {exchange} …")
            instruments = self.kite.instruments(exchange)
            self._instruments_cache[exchange] = instruments
            self._instruments_date = today
            logger.info(f"Loaded {len(instruments):,} instruments for {exchange}")

        return self._instruments_cache[exchange]

    def get_instrument_token(
        self, tradingsymbol: str, exchange: str = NSE
    ) -> Optional[int]:
        """
        Look up the integer instrument token for a symbol.
        Required for historical_data() calls.

        Returns None if the symbol is not found.
        """
        instruments = self.get_instruments(exchange)
        for inst in instruments:
            if inst["tradingsymbol"] == tradingsymbol:
                return int(inst["instrument_token"])
        logger.warning(f"Instrument not found: {exchange}:{tradingsymbol}")
        return None

    def get_instrument_tokens_bulk(
        self, symbols: list[str], exchange: str = NSE
    ) -> dict[str, int]:
        """
        Look up tokens for multiple symbols in a single instruments download.
        Returns { "RELIANCE": 738561, "TCS": 2953217, ... }
        """
        instruments = self.get_instruments(exchange)
        sym_set = set(symbols)
        result: dict[str, int] = {}
        for inst in instruments:
            if inst["tradingsymbol"] in sym_set:
                result[inst["tradingsymbol"]] = int(inst["instrument_token"])
        missing = sym_set - set(result.keys())
        if missing:
            logger.warning(f"Tokens not found for: {missing}")
        return result

    # ===================================================================
    # ── WEBSOCKET — Live Ticks ─────────────────────────────────────────
    # ===================================================================

    def start_ticker(
        self,
        instrument_tokens: list[int],
        on_tick: Callable,               # func(ws, ticks: list[dict])
        on_connect: Optional[Callable] = None,
        on_close: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
        on_reconnect: Optional[Callable] = None,
        mode: str = MODE_FULL,           # MODE_FULL / MODE_QUOTE / MODE_LTP
        reconnect: bool = True,
        max_reconnect_delay: int = 300,
    ) -> KiteTicker:
        """
        Start WebSocket live tick stream.

        Kite Connect WebSocket limits:
            - Max 3 simultaneous connections per app.
            - Max 3,000 instruments per connection.
            - Reconnects automatically (handles network drops).

        Tick modes:
            MODE_LTP   : last_price only (~few bytes/tick)
            MODE_QUOTE : OHLC + last trade + top-5 depth (~1.5 KB/tick)
            MODE_FULL  : full 20-level market depth (~5 KB/tick)

        Tick dict (MODE_FULL) contains:
            instrument_token, tradable, mode, last_price, last_quantity,
            average_price, volume, buy_quantity, sell_quantity,
            ohlc: {open, high, low, close},
            change, last_trade_time, oi, oi_day_high, oi_day_low,
            timestamp, depth: {buy: [{price, quantity, orders}×20],
                               sell: [{...}×20]}

        Args:
            instrument_tokens: List of instrument token integers.
            on_tick:     Callback(ws, ticks) — called on every tick batch.
            on_connect:  Optional callback(ws, response) on first connect.
            on_close:    Optional callback(ws, code, reason) on disconnect.
            on_error:    Optional callback(ws, code, reason) on error.
            on_reconnect: Optional callback(ws, attempts_count).
            mode:        Tick subscription mode (default: full depth).
            reconnect:   Auto-reconnect on network drops (default: True).

        Returns:
            KiteTicker instance (runs in background thread — non-blocking).
        """
        self._check()
        tokens = list(instrument_tokens)

        ticker = KiteTicker(
            api_key=settings.KITE_API_KEY,
            access_token=self._token,
            reconnect=reconnect,
            reconnect_max_delay=max_reconnect_delay,
        )

        # ── internal connect handler — subscribes on every (re)connect ──
        def _on_connect(ws, response):
            ws.subscribe(tokens)
            ws.set_mode(mode, tokens)
            logger.info(
                f"Ticker connected | {len(tokens)} instruments | mode={mode}"
            )
            if on_connect:
                on_connect(ws, response)

        def _on_close(ws, code, reason):
            logger.warning(f"Ticker closed: {code} — {reason}")
            if on_close:
                on_close(ws, code, reason)

        def _on_error(ws, code, reason):
            logger.error(f"Ticker error: {code} — {reason}")
            if on_error:
                on_error(ws, code, reason)

        def _on_reconnect(ws, attempts):
            logger.info(f"Ticker reconnecting … attempt {attempts}")
            if on_reconnect:
                on_reconnect(ws, attempts)

        def _on_noreconnect(ws):
            logger.critical("Ticker gave up reconnecting — check network/token!")

        ticker.on_ticks     = on_tick
        ticker.on_connect   = _on_connect
        ticker.on_close     = _on_close
        ticker.on_error     = _on_error
        ticker.on_reconnect = _on_reconnect
        ticker.on_noreconnect = _on_noreconnect

        ticker.connect(threaded=True)
        self._ticker = ticker
        logger.info("Ticker thread started")
        return ticker

    def subscribe(self, tokens: list[int], mode: str = MODE_FULL):
        """
        Add new instruments to an already-running ticker subscription.
        Useful for adding stocks dynamically during a session.
        """
        if self._ticker is None:
            raise RuntimeError("Ticker not started. Call start_ticker() first.")
        self._ticker.subscribe(tokens)
        self._ticker.set_mode(mode, tokens)
        logger.info(f"Subscribed {len(tokens)} new instruments | mode={mode}")

    def unsubscribe(self, tokens: list[int]):
        """Remove instruments from the WebSocket subscription."""
        if self._ticker:
            self._ticker.unsubscribe(tokens)
            logger.info(f"Unsubscribed {len(tokens)} instruments")

    def stop_ticker(self):
        """Disconnect the WebSocket ticker gracefully."""
        if self._ticker:
            self._ticker.close()
            self._ticker = None
            logger.info("Ticker stopped")

    # ===================================================================
    # ── User profile ───────────────────────────────────────────────────
    # ===================================================================

    def profile(self) -> dict:
        """
        User profile from Zerodha.
        Contains: user_id, user_name, email, user_type,
                  broker, exchanges (list), products (list),
                  order_types (list), avatar_url
        """
        self._check()
        return self.kite.profile()

    # ===================================================================
    # ── Internal helpers ───────────────────────────────────────────────
    # ===================================================================

    def __repr__(self) -> str:
        status = "connected" if self._connected else "disconnected"
        return f"<BrokerClient [{status}] api_key={settings.KITE_API_KEY[:6]}…>"


# ---------------------------------------------------------------------------
# .env writer (used by KiteAuth and auth.py)
# ---------------------------------------------------------------------------

def _write_env(key: str, value: str) -> None:
    """Update or insert KEY=VALUE in config/.env."""
    env_path = Path(__file__).parent.parent / "config" / ".env"
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text().splitlines()

    updated = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
            lines[i] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")

    env_path.write_text("\n".join(lines) + "\n")
    os.environ[key] = value
    logger.debug(f"{key} written to {env_path}")
