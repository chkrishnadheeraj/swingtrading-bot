"""
Risk Manager - The most critical module in the system.

This is what separates a profitable bot from a blown account.
The March 2026 Claude vs OpenClaw experiment showed the difference:
Claude's bot had proper risk management and returned +1,322%.
OpenClaw's bot overleveraged and was fully liquidated.

Every trade passes through this module before execution.
"""

import sqlite3
from datetime import datetime, date
from dataclasses import dataclass, field
from typing import Optional
from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class RiskState:
    """Current risk state of the portfolio."""
    current_capital: float = settings.INITIAL_CAPITAL
    peak_capital: float = settings.INITIAL_CAPITAL
    daily_pnl: float = 0.0
    open_positions: int = 0
    today: str = field(default_factory=lambda: date.today().isoformat())
    is_halted: bool = False
    halt_reason: str = ""


class RiskManager:
    """
    Enforces all risk rules. No trade executes without passing through here.

    Kill switches:
    - Per-trade: max 2% of capital at risk
    - Daily: halt if daily loss > 3%
    - Total: disable bot if drawdown > 15% from peak

    Position sizing:
    - Kelly fraction based on historical win rate (capped at half-Kelly)
    - Max 20% of capital in any single position
    - Max 3 concurrent positions
    """

    def __init__(self):
        self.state = RiskState()
        self._load_state()

    def _load_state(self):
        """Load last known state from database."""
        try:
            conn = sqlite3.connect(settings.TRADES_DB)
            cursor = conn.cursor()

            # Create risk_state table if not exists
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS risk_state (
                    id INTEGER PRIMARY KEY,
                    current_capital REAL,
                    peak_capital REAL,
                    updated_at TEXT
                )
            """)

            row = cursor.execute(
                "SELECT current_capital, peak_capital FROM risk_state ORDER BY id DESC LIMIT 1"
            ).fetchone()

            if row:
                self.state.current_capital = row[0]
                self.state.peak_capital = row[1]
                logger.info(
                    f"Loaded risk state: capital=₹{self.state.current_capital:,.0f}, "
                    f"peak=₹{self.state.peak_capital:,.0f}"
                )
            else:
                self._save_state()
                logger.info(f"Initialized risk state with capital=₹{settings.INITIAL_CAPITAL:,.0f}")

            conn.close()
        except Exception as e:
            logger.error(f"Failed to load risk state: {e}")

    def _save_state(self):
        """Persist current state to database."""
        try:
            conn = sqlite3.connect(settings.TRADES_DB)
            conn.execute(
                "INSERT INTO risk_state (current_capital, peak_capital, updated_at) VALUES (?, ?, ?)",
                (self.state.current_capital, self.state.peak_capital, datetime.now().isoformat())
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to save risk state: {e}")

    # -----------------------------------------------------------------
    # Pre-trade checks
    # -----------------------------------------------------------------

    def can_trade(self) -> tuple[bool, str]:
        """
        Master check: should the bot be trading at all right now?
        Returns (allowed, reason).
        """
        # Check if manually halted
        if self.state.is_halted:
            return False, f"Bot halted: {self.state.halt_reason}"

        # Check max drawdown kill switch
        drawdown = self._current_drawdown()
        if drawdown >= settings.MAX_DRAWDOWN:
            self.halt(f"Max drawdown breached: {drawdown:.1%} >= {settings.MAX_DRAWDOWN:.1%}")
            return False, self.state.halt_reason

        # Check daily loss limit
        if self.state.daily_pnl < 0:
            daily_loss_pct = abs(self.state.daily_pnl) / self.state.current_capital
            if daily_loss_pct >= settings.DAILY_LOSS_LIMIT:
                return False, (
                    f"Daily loss limit hit: ₹{abs(self.state.daily_pnl):,.0f} "
                    f"({daily_loss_pct:.1%} >= {settings.DAILY_LOSS_LIMIT:.1%})"
                )

        # Check max positions
        if self.state.open_positions >= settings.MAX_POSITIONS:
            return False, f"Max positions reached: {self.state.open_positions}/{settings.MAX_POSITIONS}"

        return True, "OK"

    def validate_trade(
        self,
        stock: str,
        entry_price: float,
        stop_loss: float,
        target_price: float,
        quantity: int,
    ) -> tuple[bool, str]:
        """
        Validate a specific trade before execution.
        Returns (approved, reason).
        """
        # Basic sanity
        if entry_price <= 0 or stop_loss <= 0 or quantity <= 0:
            return False, "Invalid trade parameters"

        if stop_loss >= entry_price:
            return False, f"Stop loss (₹{stop_loss}) must be below entry (₹{entry_price})"

        # Risk per trade
        risk_per_share = entry_price - stop_loss
        total_risk = risk_per_share * quantity
        max_allowed_risk = self.state.current_capital * settings.MAX_RISK_PER_TRADE

        if total_risk > max_allowed_risk:
            return False, (
                f"Trade risk ₹{total_risk:,.0f} exceeds max ₹{max_allowed_risk:,.0f} "
                f"({settings.MAX_RISK_PER_TRADE:.0%} of capital)"
            )

        # Position size check
        position_value = entry_price * quantity
        max_position = self.state.current_capital * settings.MAX_POSITION_PCT

        if position_value > max_position:
            return False, (
                f"Position ₹{position_value:,.0f} exceeds max ₹{max_position:,.0f} "
                f"({settings.MAX_POSITION_PCT:.0%} of capital)"
            )

        # Risk-reward ratio
        reward_per_share = target_price - entry_price
        rr_ratio = reward_per_share / risk_per_share if risk_per_share > 0 else 0

        if rr_ratio < settings.MIN_RISK_REWARD:
            return False, (
                f"R:R ratio {rr_ratio:.1f} below minimum {settings.MIN_RISK_REWARD:.1f}"
            )

        logger.info(
            f"Trade approved: {stock} | Entry: ₹{entry_price} | SL: ₹{stop_loss} | "
            f"Target: ₹{target_price} | Qty: {quantity} | Risk: ₹{total_risk:,.0f} | R:R: {rr_ratio:.1f}"
        )
        return True, "Approved"

    # -----------------------------------------------------------------
    # Position sizing
    # -----------------------------------------------------------------

    def calculate_position_size(
        self,
        entry_price: float,
        stop_loss: float,
        win_rate: float = 0.55,
    ) -> int:
        """
        Calculate optimal position size using half-Kelly criterion.

        Half-Kelly is used instead of full Kelly because:
        1. Our win rate estimate has uncertainty
        2. Half-Kelly gives ~75% of full Kelly returns with much less volatility
        3. It's more forgiving of estimation errors

        Args:
            entry_price: planned entry price
            stop_loss: planned stop loss price
            win_rate: historical win rate (default 0.55 = 55%)

        Returns:
            Number of shares to buy
        """
        risk_per_share = abs(entry_price - stop_loss)
        if risk_per_share <= 0:
            return 0

        # Kelly fraction: f* = (bp - q) / b
        # where b = reward/risk ratio, p = win rate, q = 1 - p
        # Using average R:R of 2.0 as estimate
        b = settings.MIN_RISK_REWARD
        p = win_rate
        q = 1 - p
        kelly = (b * p - q) / b

        # Cap at half-Kelly for safety
        fraction = max(0, kelly * 0.5)

        # Cap at max risk per trade
        fraction = min(fraction, settings.MAX_RISK_PER_TRADE)

        # Calculate max risk amount
        risk_amount = self.state.current_capital * fraction

        # Shares = risk_amount / risk_per_share
        quantity = int(risk_amount / risk_per_share)

        # Ensure position value doesn't exceed max position size
        max_qty_by_position = int(
            (self.state.current_capital * settings.MAX_POSITION_PCT) / entry_price
        )
        quantity = min(quantity, max_qty_by_position)

        # Minimum 1 share
        return max(1, quantity)

    # -----------------------------------------------------------------
    # Post-trade updates
    # -----------------------------------------------------------------

    def record_trade_result(self, pnl: float):
        """Update state after a trade closes."""
        self.state.current_capital += pnl
        self.state.daily_pnl += pnl

        # Update peak
        if self.state.current_capital > self.state.peak_capital:
            self.state.peak_capital = self.state.current_capital

        self._save_state()

        logger.info(
            f"Trade P&L: ₹{pnl:+,.0f} | Capital: ₹{self.state.current_capital:,.0f} | "
            f"Daily P&L: ₹{self.state.daily_pnl:+,.0f} | Drawdown: {self._current_drawdown():.1%}"
        )

    def update_position_count(self, count: int):
        """Update the number of open positions."""
        self.state.open_positions = count

    def reset_daily(self):
        """Reset daily P&L counter. Call at start of each trading day."""
        self.state.daily_pnl = 0.0
        self.state.today = date.today().isoformat()
        logger.info("Daily P&L reset")

    # -----------------------------------------------------------------
    # Kill switches
    # -----------------------------------------------------------------

    def halt(self, reason: str):
        """Emergency halt. Requires manual restart."""
        self.state.is_halted = True
        self.state.halt_reason = reason
        logger.critical(f"BOT HALTED: {reason}")

    def resume(self):
        """Manually resume after halt. Use with caution."""
        self.state.is_halted = False
        self.state.halt_reason = ""
        logger.warning("Bot resumed from halt state")

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _current_drawdown(self) -> float:
        """Calculate current drawdown from peak."""
        if self.state.peak_capital <= 0:
            return 0.0
        return (self.state.peak_capital - self.state.current_capital) / self.state.peak_capital

    def get_status(self) -> dict:
        """Return current risk state as dict (for dashboard/alerts)."""
        return {
            "capital": self.state.current_capital,
            "peak": self.state.peak_capital,
            "drawdown": self._current_drawdown(),
            "daily_pnl": self.state.daily_pnl,
            "open_positions": self.state.open_positions,
            "is_halted": self.state.is_halted,
            "halt_reason": self.state.halt_reason,
        }
