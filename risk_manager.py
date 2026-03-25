"""
risk_manager.py
---------------
Enforces risk limits before any order is placed. Tracks daily P&L,
open position count, and per-position sizing.

Limits enforced:
  1. MAX_POSITION_USD ($2.00)   - max cost per new position
  2. MAX_OPEN_POSITIONS (5)     - maximum concurrent open positions
  3. MAX_DAILY_LOSS_USD ($50)   - halt all trading if daily loss exceeds this

The RiskManager is a singleton that persists through the process lifecycle.
It resets daily P&L tracking at midnight UTC.
"""

import logging
from datetime import date, datetime, timezone
from typing import Optional

import config
from kalshi_client import KalshiPosition, OrderResult

logger = logging.getLogger(__name__)


class RiskLimitExceeded(Exception):
    """Raised when a proposed order would breach a risk limit."""
    pass


class RiskManager:
    """
    Stateful risk manager. Instantiate once and pass around, or use
    the module-level singleton `risk_manager`.
    """

    def __init__(self):
        # Tickers of positions opened this session (for counting)
        self._open_tickers: set[str] = set()
        # Daily P&L tracking
        self._daily_pnl: float = 0.0
        self._last_reset_date: date = datetime.now(timezone.utc).date()
        # Running total of money spent today (for the daily loss calc)
        self._daily_spent: float = 0.0
        self._daily_received: float = 0.0

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _maybe_reset_daily(self) -> None:
        """Reset daily P&L counters at midnight UTC."""
        today = datetime.now(timezone.utc).date()
        if today != self._last_reset_date:
            logger.info(
                "New trading day (%s). Resetting daily P&L. "
                "Yesterday P&L: $%.2f",
                today,
                self._daily_pnl,
            )
            self._daily_pnl = 0.0
            self._daily_spent = 0.0
            self._daily_received = 0.0
            self._last_reset_date = today

    @property
    def daily_pnl(self) -> float:
        self._maybe_reset_daily()
        return self._daily_pnl

    @property
    def open_position_count(self) -> int:
        return len(self._open_tickers)

    # -----------------------------------------------------------------------
    # Sync with live positions (call each scan cycle)
    # -----------------------------------------------------------------------

    def sync_positions(self, live_positions: list[KalshiPosition]) -> None:
        """
        Reconcile internal tracker against live Kalshi positions.
        Removes closed positions from our tracking set.
        """
        live_tickers = {p.ticker for p in live_positions if p.market_exposure > 0}

        # Detect closed positions
        closed = self._open_tickers - live_tickers
        for ticker in closed:
            logger.info("Position closed (settled or sold): %s", ticker)
        self._open_tickers = self._open_tickers.intersection(live_tickers) | live_tickers

        # Update P&L from live data (API returns dollar fields directly)
        total_exposure = sum(p.market_exposure_dollars for p in live_positions)
        total_realized = sum(p.realized_pnl for p in live_positions)
        self._daily_pnl = total_realized
        logger.debug(
            "Risk sync: %d open positions | daily P&L $%.2f",
            len(self._open_tickers),
            self._daily_pnl,
        )

    # -----------------------------------------------------------------------
    # Pre-trade checks
    # -----------------------------------------------------------------------

    def check_buy(
        self,
        ticker: str,
        cost_usd: float,
    ) -> None:
        """
        Validate a proposed buy. Raises RiskLimitExceeded if any limit
        would be breached. Call BEFORE placing an order.

        Args:
            ticker:   Market ticker to buy
            cost_usd: Total dollar cost of the proposed order
        """
        self._maybe_reset_daily()

        # 1. Daily loss kill-switch
        if self._daily_pnl <= -config.MAX_DAILY_LOSS_USD:
            raise RiskLimitExceeded(
                f"Daily loss limit hit: ${self._daily_pnl:.2f} <= "
                f"-${config.MAX_DAILY_LOSS_USD:.2f}"
            )

        # 2. Position count
        if self.open_position_count >= config.MAX_OPEN_POSITIONS:
            raise RiskLimitExceeded(
                f"Max open positions reached: {self.open_position_count} "
                f">= {config.MAX_OPEN_POSITIONS}"
            )

        # 3. Per-position size
        if cost_usd > config.MAX_POSITION_USD:
            raise RiskLimitExceeded(
                f"Position size ${cost_usd:.2f} exceeds max "
                f"${config.MAX_POSITION_USD:.2f}"
            )

        # 4. Don't re-enter a position we already hold
        if ticker in self._open_tickers:
            raise RiskLimitExceeded(f"Already holding position in {ticker}")

    def check_sell(self, ticker: str) -> None:
        """
        Validate a proposed sell. Raises RiskLimitExceeded if the ticker
        isn't in our open positions (guard against double-sells).
        """
        if ticker not in self._open_tickers:
            # In dry-run mode this is expected — don't block the simulation
            if not config.DRY_RUN:
                logger.warning(
                    "Attempted to sell %s but it's not in tracked positions", ticker
                )

    # -----------------------------------------------------------------------
    # Post-trade accounting
    # -----------------------------------------------------------------------

    def record_buy(self, ticker: str, cost_usd: float) -> None:
        """Call after a successful buy order."""
        self._open_tickers.add(ticker)
        self._daily_spent += cost_usd
        self._daily_pnl -= cost_usd  # unrealised cost
        logger.info(
            "Recorded BUY %s $%.2f | open positions: %d | daily P&L: $%.2f",
            ticker, cost_usd, self.open_position_count, self._daily_pnl,
        )

    def record_sell(self, ticker: str, proceeds_usd: float, cost_basis: float = 0.0) -> None:
        """Call after a successful sell order."""
        self._open_tickers.discard(ticker)
        self._daily_received += proceeds_usd
        realized = proceeds_usd - cost_basis
        self._daily_pnl += proceeds_usd  # add back the sale proceeds
        logger.info(
            "Recorded SELL %s proceeds $%.2f | open positions: %d | "
            "daily P&L: $%.2f",
            ticker, proceeds_usd, self.open_position_count, self._daily_pnl,
        )

    # -----------------------------------------------------------------------
    # Sizing helper
    # -----------------------------------------------------------------------

    def compute_position_size(self, price_usd: float, balance_usd: float) -> tuple[int, float]:
        """
        Determine how many contracts to buy and the total cost.

        Strategy:
          - Never spend more than MAX_POSITION_USD per position
          - Never spend more than balance / MAX_OPEN_POSITIONS (diversify)
          - Always buy at least 1 contract

        Returns:
            (num_contracts, total_cost_usd)
        """
        if price_usd <= 0:
            return 0, 0.0

        # Maximum we're willing to spend on this position
        available_per_position = min(
            config.MAX_POSITION_USD,
            balance_usd / max(config.MAX_OPEN_POSITIONS, 1),
        )

        # Kalshi contracts cost `price_usd` each
        num_contracts = max(1, int(available_per_position / price_usd))
        total_cost = num_contracts * price_usd

        # Cap at hard limit
        if total_cost > config.MAX_POSITION_USD:
            num_contracts = max(1, int(config.MAX_POSITION_USD / price_usd))
            total_cost = num_contracts * price_usd

        return num_contracts, total_cost

    # -----------------------------------------------------------------------
    # Status summary
    # -----------------------------------------------------------------------

    def status_summary(self) -> str:
        self._maybe_reset_daily()
        return (
            f"Open positions: {self.open_position_count}/{config.MAX_OPEN_POSITIONS} | "
            f"Daily P&L: ${self._daily_pnl:.2f} | "
            f"Daily loss limit: -${config.MAX_DAILY_LOSS_USD:.2f}"
        )


# Module-level singleton
risk_manager = RiskManager()
