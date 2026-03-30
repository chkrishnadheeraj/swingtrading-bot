"""
Momentum Swing Strategy

Signal logic:
  BUY when:
    1. Fast EMA (9) crosses above Slow EMA (21) on daily chart
    2. RSI(14) is between 35-70 (not overbought, recovering from oversold)
    3. Today's volume > 1.5x the 20-day average volume
    4. Price is above VWAP (if available)

  EXIT when:
    1. Trailing stop loss hit (2% from peak after entry)
    2. Target hit (6% from entry)
    3. Max hold period exceeded (7 days)
    4. RSI > 75 (overbought exit)

This is NOT an arbitrage strategy. It's a statistical edge.
Expected win rate: 55-65%. Expected R:R: 1:2.
Profitability comes from the R:R, not the win rate.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Optional

from strategies.base import BaseStrategy, Signal
from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


class MomentumStrategy(BaseStrategy):

    def __init__(self, feed=None):
        self.params = settings.MOMENTUM
        self.feed = feed  # unified DataFeed passed from engine
        self._cache = {}  # Cache historical data to avoid repeated API calls

    def name(self) -> str:
        return "momentum_swing"

    def scan(self, watchlist: list[str]) -> list[Signal]:
        """
        Scan all stocks in watchlist for momentum signals.
        Returns list of BUY signals ranked by confidence.
        Raises ConnectionError if > 50% of symbols fail to return data.
        """
        signals = []
        missing_data_count = 0

        for stock in watchlist:
            try:
                df = self._get_historical_data(stock)
                if df is None or df.empty:
                    missing_data_count += 1
                    continue
                
                signal = self._analyze_stock(stock, df=df)
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.warning(f"Error analyzing {stock}: {e}")
                continue

        # If data fetch failed for more than 50% of the watchlist, it's a network issue
        if len(watchlist) > 0 and missing_data_count > max(1, len(watchlist) * 0.5):
            raise ConnectionError(f"Data unavailable for {missing_data_count}/{len(watchlist)} symbols. Likely network drop.")

        # Sort by confidence (highest first)
        signals.sort(key=lambda s: s.confidence, reverse=True)

        if signals:
            logger.info(f"Momentum scan found {len(signals)} signals: {[s.stock for s in signals]}")
        else:
            logger.info("Momentum scan: no signals today")

        return signals

    def _analyze_stock(self, stock: str, df: Optional[pd.DataFrame] = None) -> Optional[Signal]:
        """
        Analyze a single stock for momentum entry signal.
        Returns Signal if criteria met, None otherwise.
        """
        # Fetch historical data if not provided
        if df is None:
            df = self._get_historical_data(stock)
            
        # Need enough bars for trend EMA (50) + a few warmup bars
        min_bars = self.params.get("trend_ema", 50) + 10
        if df is None or len(df) < min_bars:
            return None

        # Calculate indicators
        df = self._add_indicators(df)
        latest = df.iloc[-1]
        prev = df.iloc[-2]

        # ----- Signal conditions -----

        # 0. Higher-timeframe trend filter (Fix #2)
        # Only take longs when price is above the 50-day EMA.
        # This blocks counter-trend entries during broad downtrends.
        trend_ema_col = f"ema_{self.params.get('trend_ema', 50)}"
        if latest["close"] <= latest[trend_ema_col]:
            return None  # Price below trend EMA — no longs in a downtrend

        # 1. EMA crossover: fast EMA crosses above slow EMA
        ema_cross_up = (
            prev[f"ema_{self.params['fast_ema']}"] <= prev[f"ema_{self.params['slow_ema']}"]
            and latest[f"ema_{self.params['fast_ema']}"] > latest[f"ema_{self.params['slow_ema']}"]
        )

        # Also accept: price is already above both EMAs and EMAs are trending up
        ema_bullish = (
            latest["close"] > latest[f"ema_{self.params['fast_ema']}"]
            and latest[f"ema_{self.params['fast_ema']}"] > latest[f"ema_{self.params['slow_ema']}"]
            and latest[f"ema_{self.params['fast_ema']}"] > prev[f"ema_{self.params['fast_ema']}"]
        )

        if not (ema_cross_up or ema_bullish):
            return None

        # 2. RSI filter
        rsi = latest["rsi"]
        if rsi > self.params["rsi_overbought"] or rsi < 20:
            return None  # Too overbought or too oversold (falling knife)

        # 3. Volume confirmation
        if latest["volume"] < latest["avg_volume"] * self.params["volume_multiplier"]:
            return None  # Insufficient volume to confirm

        # ----- Calculate trade parameters -----

        entry_price = latest["close"]

        # Stop loss: below the recent swing low or fixed percentage, whichever is tighter
        recent_low = df["low"].tail(5).min()
        sl_by_pct = entry_price * (1 - self.params["initial_sl_pct"])
        stop_loss = max(sl_by_pct, recent_low * 0.995)  # Slight buffer below swing low

        # Target
        target_price = entry_price * (1 + self.params["target_pct"])

        # Risk-reward check
        risk = entry_price - stop_loss
        reward = target_price - entry_price
        if risk <= 0 or (reward / risk) < settings.MIN_RISK_REWARD:
            return None

        # Confidence score (0-1)
        confidence = self._calculate_confidence(
            ema_cross=ema_cross_up,
            ema_bullish=ema_bullish,
            rsi=rsi,
            volume_ratio=latest["volume"] / latest["avg_volume"],
        )

        # Build reason string
        reasons = []
        if ema_cross_up:
            reasons.append(f"EMA {self.params['fast_ema']}/{self.params['slow_ema']} crossover")
        if ema_bullish:
            reasons.append("Price above rising EMAs")
        reasons.append(f"Trend EMA({self.params.get('trend_ema', 50)}): above")
        reasons.append(f"RSI: {rsi:.0f}")
        reasons.append(f"Vol: {latest['volume']/latest['avg_volume']:.1f}x avg")

        return Signal(
            stock=stock,
            action="BUY",
            entry_price=round(entry_price, 2),
            stop_loss=round(stop_loss, 2),
            target_price=round(target_price, 2),
            quantity=0,  # Risk manager will calculate
            strategy=self.name(),
            confidence=confidence,
            reason=" | ".join(reasons),
            timestamp=datetime.now().isoformat(),
        )

    def should_exit(
        self,
        stock: str,
        current_price: float,
        entry_price: float,
        entry_date: Optional[str] = None,
        highest_since_entry: Optional[float] = None,
    ) -> Optional[Signal]:
        """
        Check if an open position should be exited.

        Exit conditions:
        1. Stop loss hit
        2. Target hit
        3. Max hold period exceeded
        4. Trailing stop hit (once in profit)
        5. RSI overbought
        """
        if highest_since_entry is None:
            highest_since_entry = max(current_price, entry_price)

        # 1. Initial stop loss
        initial_sl = entry_price * (1 - self.params["initial_sl_pct"])
        if current_price <= initial_sl:
            return Signal(
                stock=stock, action="SELL", entry_price=current_price,
                stop_loss=0, target_price=0, quantity=0,
                strategy=self.name(), confidence=1.0,
                reason=f"Stop loss hit: ₹{current_price} <= ₹{initial_sl:.2f}",
            )

        # 2. Target hit
        target = entry_price * (1 + self.params["target_pct"])
        if current_price >= target:
            return Signal(
                stock=stock, action="SELL", entry_price=current_price,
                stop_loss=0, target_price=0, quantity=0,
                strategy=self.name(), confidence=1.0,
                reason=f"Target hit: ₹{current_price} >= ₹{target:.2f}",
            )

        # 3. Trailing stop — only activates once position gains 'trailing_sl_activation'%
        # This prevents the 2-3 day whipsaw that killed most trades in the initial backtest
        # (Fix #1: activation gate + wider 4% trail)
        activation_pct = self.params.get("trailing_sl_activation", 0.02)
        if current_price >= entry_price * (1 + activation_pct):
            trailing_sl = highest_since_entry * (1 - self.params["trailing_sl_pct"])
            if current_price <= trailing_sl:
                return Signal(
                    stock=stock, action="SELL", entry_price=current_price,
                    stop_loss=0, target_price=0, quantity=0,
                    strategy=self.name(), confidence=1.0,
                    reason=(
                        f"Trailing SL hit: ₹{current_price} <= ₹{trailing_sl:.2f} "
                        f"(peak: ₹{highest_since_entry}, trail: {self.params['trailing_sl_pct']:.0%})"
                    ),
                )

        # 4. Max hold period
        if entry_date:
            days_held = (datetime.now() - datetime.fromisoformat(entry_date)).days
            if days_held >= self.params["max_hold_days"]:
                return Signal(
                    stock=stock, action="SELL", entry_price=current_price,
                    stop_loss=0, target_price=0, quantity=0,
                    strategy=self.name(), confidence=0.7,
                    reason=f"Max hold period: {days_held} days >= {self.params['max_hold_days']}",
                )

        # 5. RSI overbought exit
        try:
            df = self._get_historical_data(stock)
            if df is not None:
                df = self._add_indicators(df)
                if df.iloc[-1]["rsi"] > 78:
                    return Signal(
                        stock=stock, action="SELL", entry_price=current_price,
                        stop_loss=0, target_price=0, quantity=0,
                        strategy=self.name(), confidence=0.6,
                        reason=f"RSI overbought exit: {df.iloc[-1]['rsi']:.0f}",
                    )
        except Exception:
            pass

        return None  # Hold position

    # -----------------------------------------------------------------
    # Data & indicators
    # -----------------------------------------------------------------

    def _get_historical_data(self, stock: str) -> Optional[pd.DataFrame]:
        """Fetch historical daily data via DataFeed (Kite or yfinance fallback)."""
        cache_key = f"{stock}_{datetime.now().strftime('%Y%m%d')}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            if self.feed:
                df = self.feed.history(stock, days=settings.HISTORICAL_DAYS, interval="day")
            else:
                # Fallback if no feed provided
                import yfinance as yf
                ticker = yf.Ticker(f"{stock}.NS")
                df = ticker.history(period=f"{settings.HISTORICAL_DAYS}d")
                if not df.empty:
                    df.columns = [c.lower() for c in df.columns]

            if df is None or df.empty:
                logger.warning(f"No data returned for {stock}")
                return None

            self._cache[cache_key] = df
            return df

        except Exception as e:
            logger.error(f"Failed to fetch data for {stock}: {e}")
            return None

    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add technical indicators to dataframe."""
        # Fast + Slow EMAs (signal)
        df[f"ema_{self.params['fast_ema']}"] = df["close"].ewm(
            span=self.params["fast_ema"], adjust=False
        ).mean()
        df[f"ema_{self.params['slow_ema']}"] = df["close"].ewm(
            span=self.params["slow_ema"], adjust=False
        ).mean()

        # Trend EMA — higher-timeframe filter (Fix #2)
        trend_ema = self.params.get("trend_ema", 50)
        df[f"ema_{trend_ema}"] = df["close"].ewm(
            span=trend_ema, adjust=False
        ).mean()

        # RSI
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0).rolling(window=self.params["rsi_period"]).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=self.params["rsi_period"]).mean()
        rs = gain / loss.replace(0, np.inf)
        df["rsi"] = 100 - (100 / (1 + rs))

        # Average volume (20-day)
        df["avg_volume"] = df["volume"].rolling(window=20).mean()

        return df

    def _calculate_confidence(
        self,
        ema_cross: bool,
        ema_bullish: bool,
        rsi: float,
        volume_ratio: float,
    ) -> float:
        """
        Calculate confidence score (0-1) based on signal quality.
        Higher confidence = stronger signal.
        """
        score = 0.0

        # EMA crossover is a stronger signal than just being above EMAs
        if ema_cross:
            score += 0.35
        elif ema_bullish:
            score += 0.20

        # RSI sweet spot: 40-60 is ideal (recovering, not yet overbought)
        if 40 <= rsi <= 55:
            score += 0.25
        elif 35 <= rsi < 40 or 55 < rsi <= 65:
            score += 0.15
        else:
            score += 0.05

        # Volume confirmation strength
        if volume_ratio >= 2.5:
            score += 0.30
        elif volume_ratio >= 2.0:
            score += 0.25
        elif volume_ratio >= 1.5:
            score += 0.15

        # Normalize to 0-1
        return min(1.0, max(0.0, score))
