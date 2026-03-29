"""
Trading Engine - orchestrates the scan-signal-validate-execute loop.

This is the main brain. It:
1. Runs the strategy scanner on schedule
2. Passes signals through risk manager
3. Executes trades (paper or live)
4. Monitors open positions for exit signals
5. Logs everything to the journal
"""

import time
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

from config import settings
from core.broker import BrokerClient
from core.data_feed import DataFeed
from core.risk_manager import RiskManager
from strategies.base import BaseStrategy, Signal
from strategies.momentum import MomentumStrategy
from utils.journal import TradeJournal
from utils.telegram_alert import TelegramAlert
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class OpenPosition:
    """Tracks an open position."""
    trade_id: int
    stock: str
    entry_price: float
    stop_loss: float
    target_price: float
    quantity: int
    strategy: str
    entry_time: str
    highest_price: float = 0.0  # For trailing SL


class TradingEngine:
    """
    Main trading loop.

    In paper mode: simulates trades using live price data.
    In live mode: executes via Kite Connect API.
    """

    def __init__(self, mode: str = "paper", max_position: float = None):
        self.mode = mode
        self.max_position_override = max_position

        # Components
        self.risk_manager = RiskManager()
        self.journal      = TradeJournal()
        self.alerts       = TelegramAlert()

        # Broker + data feed
        # Always attempt to use Kite for accurate live data, even in paper mode.
        # Real orders are blocked later by the mode check.
        self.broker: Optional[BrokerClient] = None
        self.feed: DataFeed = DataFeed(broker=None)  # paper default
        self._init_broker()

        # Strategies
        self.strategies: list[BaseStrategy] = [
            MomentumStrategy(feed=self.feed),
        ]

        # State
        self.positions: dict[str, OpenPosition] = {}

        logger.info(
            f"Engine initialized | Mode: {self.mode} | "
            f"Broker: {'Kite' if self.broker else 'Paper/yfinance'} | "
            f"Capital: ₹{self.risk_manager.state.current_capital:,.0f}"
        )

    def _init_broker(self):
        """Connect to Kite Connect. Falls back to paper on failure."""
        try:
            broker = BrokerClient()
            broker.connect()
            self.broker = broker
            self.feed   = DataFeed(broker=broker)
            logger.info("Kite Connect broker initialised")
        except (EnvironmentError, ConnectionError) as exc:
            logger.warning(f"Kite unavailable ({exc}) — running paper mode with yfinance.")

    def run_scan_cycle(self):
        """
        Main scan cycle. Called once per day (or per scan interval).
        1. Check if trading is allowed
        2. Run all strategies
        3. Filter and validate signals
        4. Execute approved trades
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"  SCAN CYCLE | {datetime.now().strftime('%Y-%m-%d %H:%M')} | {self.mode.upper()} MODE")
        logger.info(f"{'='*60}")

        # Pre-flight check
        can_trade, reason = self.risk_manager.can_trade()
        if not can_trade:
            logger.warning(f"Trading blocked: {reason}")
            return

        # Check existing positions for exits first
        self._check_exits()

        # Run strategy scanners
        all_signals: list[Signal] = []
        for strategy in self.strategies:
            try:
                signals = strategy.scan(settings.WATCHLIST)
                all_signals.extend(signals)
            except Exception as e:
                logger.error(f"Strategy {strategy.name()} scan failed: {e}")

        if not all_signals:
            logger.info("No signals generated this cycle")
            return

        # Filter out stocks we already have positions in
        new_signals = [s for s in all_signals if s.stock not in self.positions]

        # Process top signals (limited by available position slots)
        available_slots = settings.MAX_POSITIONS - len(self.positions)
        for signal in new_signals[:available_slots]:
            self._process_signal(signal)

    def _process_signal(self, signal: Signal):
        """Validate and execute a single signal."""
        # Calculate position size
        quantity = self.risk_manager.calculate_position_size(
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
        )

        if quantity <= 0:
            logger.info(f"Skipping {signal.stock}: position size = 0")
            return

        # Override max position if specified
        if self.max_position_override:
            max_qty = int(self.max_position_override / signal.entry_price)
            quantity = min(quantity, max(1, max_qty))

        # Validate through risk manager
        approved, reason = self.risk_manager.validate_trade(
            stock=signal.stock,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            target_price=signal.target_price,
            quantity=quantity,
        )

        if not approved:
            logger.info(f"Trade rejected for {signal.stock}: {reason}")
            return

        # Execute
        self._execute_entry(signal, quantity)

    def _execute_entry(self, signal: Signal, quantity: int):
        """Execute a trade entry (paper or live)."""
        if self.mode == "live" and self.broker:
            try:
                order_id = self.broker.buy(
                    symbol=signal.stock, qty=quantity,
                    product="CNC", tag="momentum_bot",
                )
                logger.info(f"LIVE BUY: {signal.stock} x{quantity} @ MARKET | id={order_id}")
                # Place GTT OCO so stop + target are active even if bot restarts
                try:
                    gtt_id = self.broker.place_gtt_oco(
                        tradingsymbol=signal.stock, exchange="NSE",
                        quantity=quantity, entry_price=signal.entry_price,
                        stop_price=signal.stop_loss, target_price=signal.target_price,
                    )
                    logger.info(f"GTT OCO: SL={signal.stop_loss} T={signal.target_price} id={gtt_id}")
                except Exception as exc:
                    logger.warning(f"GTT failed (set manual SL): {exc}")
            except Exception as e:
                logger.error(f"Live order failed for {signal.stock}: {e}")
                return # abort tracking the position if entry failed
        else:
            logger.info(f"PAPER TRADE: BUY {signal.stock} x{quantity} @ ₹{signal.entry_price}")

        # Log to journal
        trade_id = self.journal.log_entry(
            stock=signal.stock,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            target_price=signal.target_price,
            quantity=quantity,
            strategy=signal.strategy,
            confidence=signal.confidence,
            reason=signal.reason,
            mode=self.mode,
        )

        # Track position
        self.positions[signal.stock] = OpenPosition(
            trade_id=trade_id,
            stock=signal.stock,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            target_price=signal.target_price,
            quantity=quantity,
            strategy=signal.strategy,
            entry_time=datetime.now().isoformat(),
            highest_price=signal.entry_price,
        )

        self.risk_manager.update_position_count(len(self.positions))

        # Alert
        self.alerts.trade_entry(
            stock=signal.stock,
            price=signal.entry_price,
            qty=quantity,
            sl=signal.stop_loss,
            target=signal.target_price,
            reason=signal.reason,
        )

    def _check_exits(self):
        """Check all open positions for exit signals."""
        stocks_to_close = []

        for stock, pos in self.positions.items():
            try:
                # Get current price (in paper mode, use yfinance)
                current_price = self._get_current_price(stock)
                if current_price is None:
                    continue

                # Update highest price for trailing SL
                pos.highest_price = max(pos.highest_price, current_price)

                # Check exit conditions
                for strategy in self.strategies:
                    if strategy.name() == pos.strategy:
                        exit_signal = strategy.should_exit(
                            stock=stock,
                            current_price=current_price,
                            entry_price=pos.entry_price,
                            entry_date=pos.entry_time,
                            highest_since_entry=pos.highest_price,
                        )

                        if exit_signal:
                            self._execute_exit(pos, current_price, exit_signal.reason)
                            stocks_to_close.append(stock)
                        break

            except Exception as e:
                logger.error(f"Error checking exit for {stock}: {e}")

        # Remove closed positions
        for stock in stocks_to_close:
            del self.positions[stock]

        self.risk_manager.update_position_count(len(self.positions))

    def _execute_exit(self, pos: OpenPosition, exit_price: float, reason: str):
        """Execute a trade exit."""
        pnl = (exit_price - pos.entry_price) * pos.quantity

        if self.mode == "live" and self.broker:
            try:
                order_id = self.broker.sell(
                    symbol=pos.stock, qty=pos.quantity,
                    product="CNC", tag="momentum_bot",
                )
                logger.info(f"LIVE SELL: {pos.stock} x{pos.quantity} @ MARKET | id={order_id}")
            except Exception as e:
                logger.error(f"Live sell failed for {pos.stock}: {e}")
                return # Don't record exit if failed
        else:
            logger.info(f"PAPER EXIT: SELL {pos.stock} x{pos.quantity} @ ₹{exit_price}")

        # Log to journal
        self.journal.log_exit(
            trade_id=pos.trade_id,
            exit_price=exit_price,
            exit_reason=reason,
        )

        # Update risk state
        self.risk_manager.record_trade_result(pnl)

        # Alert
        self.alerts.trade_exit(
            stock=pos.stock,
            entry=pos.entry_price,
            exit_price=exit_price,
            pnl=pnl,
            reason=reason,
        )

    def _get_current_price(self, stock: str) -> Optional[float]:
        """
        Get current price via DataFeed.
        In live mode: Kite LTP (real-time, no delay).
        In paper mode: yfinance (15-min delayed).
        """
        try:
            prices = self.feed.ltp([stock])
            return prices.get(stock)
        except Exception as exc:
            logger.error(f"Price fetch failed for {stock}: {exc}")
            return None

    def end_of_day(self):
        """End-of-day housekeeping."""
        status = self.risk_manager.get_status()
        self.alerts.daily_summary(status)
        self.journal.print_summary(days=30, mode=self.mode)
        self.risk_manager.reset_daily()
        logger.info("End of day processing complete")
