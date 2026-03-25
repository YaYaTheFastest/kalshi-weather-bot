"""
spread_executor.py — Spread trade execution layer.

Converts SpreadSignal detections from spread_engine into actionable paired
buy/sell orders executed through the existing kalshi_client order functions.

Trade types:
  - monotonicity_arb: Buy YES on lower strike, sell YES on higher strike
    (only when bid_high > ask_low = locked-in profit)
  - wide_gap_buy: Single-leg buy of the cheaper side
  - compression: Log only, don't trade in v1

Position tracking via spread_positions.json for monitoring.
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

import config
import telegram_alerts
from kalshi_client import OrderResult, place_buy_order, place_sell_order
from risk_manager import RiskLimitExceeded, risk_manager
from spread_engine import SpreadSignal

logger = logging.getLogger(__name__)

_POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spread_positions.json")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SpreadLeg:
    ticker: str
    side: str           # "yes" or "no"
    action: str         # "buy" or "sell"
    price_cents: int    # limit price in cents
    count: int          # number of contracts


@dataclass
class SpreadTrade:
    signal: SpreadSignal
    buy_leg: SpreadLeg          # The underpriced leg we're buying
    sell_leg: Optional[SpreadLeg]  # The overpriced leg (None for single-leg)
    expected_profit: float      # Expected profit per contract in dollars
    max_loss: float             # Max loss if only one leg fills
    trade_type: str             # "monotonicity_arb", "wide_gap_buy", "compression"


# ---------------------------------------------------------------------------
# Position tracking (monitoring only)
# ---------------------------------------------------------------------------

def _load_positions() -> dict:
    """Load spread positions from JSON file."""
    if os.path.exists(_POSITIONS_FILE):
        try:
            with open(_POSITIONS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read %s — starting fresh", _POSITIONS_FILE)
    return {"active_spreads": []}


def _save_positions(data: dict) -> None:
    """Save spread positions to JSON file."""
    try:
        with open(_POSITIONS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except OSError as exc:
        logger.error("Failed to save spread positions: %s", exc)


def _record_spread_position(
    trade: SpreadTrade,
    buy_result: OrderResult,
    sell_result: Optional[OrderResult],
) -> None:
    """Record an executed spread in the positions file."""
    positions = _load_positions()

    buy_cost = (trade.buy_leg.count * trade.buy_leg.price_cents / 100.0) if buy_result.success else 0.0
    sell_proceeds = 0.0
    if sell_result and sell_result.success and trade.sell_leg:
        sell_proceeds = trade.sell_leg.count * trade.sell_leg.price_cents / 100.0

    # Determine status
    buy_ok = buy_result.success
    sell_ok = sell_result.success if sell_result else False
    if buy_ok and sell_ok:
        status = "open"
    elif buy_ok:
        status = "one_leg"
    else:
        status = "failed"

    entry = {
        "id": str(uuid.uuid4()),
        "trade_type": trade.trade_type,
        "buy_ticker": trade.buy_leg.ticker,
        "sell_ticker": trade.sell_leg.ticker if trade.sell_leg else None,
        "buy_filled": buy_ok,
        "sell_filled": sell_ok,
        "buy_cost": round(buy_cost, 4),
        "sell_proceeds": round(sell_proceeds, 4),
        "expected_profit": round(trade.expected_profit, 4),
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
    }
    positions["active_spreads"].append(entry)
    _save_positions(positions)


# ---------------------------------------------------------------------------
# Trade generation
# ---------------------------------------------------------------------------

def generate_spread_trades(
    spread_signals: list[SpreadSignal],
    held_tickers: set[str],
    balance: float,
) -> list[SpreadTrade]:
    """
    Convert SpreadSignal detections into actionable SpreadTrade objects.

    Filters by:
      - Trade type eligibility (monotonicity = arb, wide_gap = single-leg, compression = skip)
      - Minimum profit threshold
      - Position limits and held ticker conflicts
      - Balance availability

    Returns trades sorted by expected profit descending.
    """
    trades: list[SpreadTrade] = []

    for signal in spread_signals:

        # --- Monotonicity violation: purest arb ---
        if signal.signal_type == "monotonicity":
            trade = _build_monotonicity_trade(signal, held_tickers, balance)
            if trade:
                trades.append(trade)

        # --- Wide gap: single-leg buy of the cheap side ---
        elif signal.signal_type == "wide_gap":
            trade = _build_wide_gap_trade(signal, held_tickers, balance)
            if trade:
                trades.append(trade)

        # --- Compression: log only in v1 ---
        elif signal.signal_type == "compression":
            logger.info("Spread compression detected (log only): %s", signal)
            continue

    # Sort by expected profit descending
    trades.sort(key=lambda t: t.expected_profit, reverse=True)
    return trades


def _build_monotonicity_trade(
    signal: SpreadSignal,
    held_tickers: set[str],
    balance: float,
) -> Optional[SpreadTrade]:
    """
    Build a monotonicity arb trade.

    Buy YES on lower strike (cheap), sell YES on higher strike (expensive).
    Only execute if bid_high > ask_low (locked-in profit per contract).
    """
    # Must have locked-in profit: bid on the high strike > ask on the low strike
    profit_per_contract = signal.bid_high - signal.ask_low
    if profit_per_contract <= 0:
        logger.debug(
            "Monotonicity skip (no locked profit): bid_high=$%.2f <= ask_low=$%.2f",
            signal.bid_high, signal.ask_low,
        )
        return None

    # Check minimum profit threshold
    if profit_per_contract < config.SPREAD_MIN_PROFIT_CENTS:
        logger.debug(
            "Monotonicity skip (profit $%.3f < min $%.3f)",
            profit_per_contract, config.SPREAD_MIN_PROFIT_CENTS,
        )
        return None

    # Both legs need liquidity
    if signal.ask_low <= 0 or signal.bid_high <= 0:
        return None

    # Skip if we already hold either ticker
    if signal.ticker_low in held_tickers:
        logger.debug("Monotonicity skip: already hold buy leg %s", signal.ticker_low)
        return None
    if signal.ticker_high in held_tickers:
        logger.debug("Monotonicity skip: already hold sell leg %s", signal.ticker_high)
        return None

    # Size: each leg capped at SPREAD_MAX_POSITION_USD
    ask_low_price = signal.ask_low
    max_contracts_by_cost = int(config.SPREAD_MAX_POSITION_USD / ask_low_price) if ask_low_price > 0 else 0
    max_contracts_by_balance = int(balance / ask_low_price) if ask_low_price > 0 else 0
    count = max(1, min(max_contracts_by_cost, max_contracts_by_balance))

    if count == 0:
        return None

    buy_leg = SpreadLeg(
        ticker=signal.ticker_low,
        side="yes",
        action="buy",
        price_cents=max(1, min(99, int(ask_low_price * 100))),
        count=count,
    )
    sell_leg = SpreadLeg(
        ticker=signal.ticker_high,
        side="yes",
        action="sell",
        price_cents=max(1, min(99, int(signal.bid_high * 100))),
        count=count,
    )

    return SpreadTrade(
        signal=signal,
        buy_leg=buy_leg,
        sell_leg=sell_leg,
        expected_profit=round(profit_per_contract * count, 4),
        max_loss=round(ask_low_price * count, 4),  # if sell leg fails, we hold directional
        trade_type="monotonicity_arb",
    )


def _build_wide_gap_trade(
    signal: SpreadSignal,
    held_tickers: set[str],
    balance: float,
) -> Optional[SpreadTrade]:
    """
    Build a wide-gap single-leg buy.

    If ask_diff > $0.20 AND the higher strike ask < $0.40, buy YES on higher strike.
    No sell leg — this is a confidence-boosted directional bet.
    """
    ask_diff = signal.ask_low - signal.ask_high
    if ask_diff <= 0.20:
        return None

    # Higher strike ask must be cheap enough to be worth buying
    if signal.ask_high <= 0 or signal.ask_high >= 0.40:
        return None

    # Check minimum edge (use ask_diff as proxy for expected profit)
    if ask_diff < config.SPREAD_MIN_PROFIT_CENTS:
        return None

    # Skip if we already hold the target ticker
    if signal.ticker_high in held_tickers:
        logger.debug("Wide gap skip: already hold %s", signal.ticker_high)
        return None

    ask_price = signal.ask_high
    max_contracts_by_cost = int(config.SPREAD_MAX_POSITION_USD / ask_price) if ask_price > 0 else 0
    max_contracts_by_balance = int(balance / ask_price) if ask_price > 0 else 0
    count = max(1, min(max_contracts_by_cost, max_contracts_by_balance))

    if count == 0:
        return None

    buy_leg = SpreadLeg(
        ticker=signal.ticker_high,
        side="yes",
        action="buy",
        price_cents=max(1, min(99, int(ask_price * 100))),
        count=count,
    )

    return SpreadTrade(
        signal=signal,
        buy_leg=buy_leg,
        sell_leg=None,
        expected_profit=round(ask_diff * count, 4),
        max_loss=round(ask_price * count, 4),
        trade_type="wide_gap_buy",
    )


# ---------------------------------------------------------------------------
# Trade execution
# ---------------------------------------------------------------------------

def execute_spread_trade(
    trade: SpreadTrade,
    held_tickers: set[str],
    balance: float,
    daily_spend: float,
) -> tuple[bool, float]:
    """
    Execute a spread trade through the existing order functions.

    Execution order: buy leg first, then sell leg.
    If sell leg fails, we're left with an acceptable directional position.

    Args:
        trade: The SpreadTrade to execute
        held_tickers: Set of currently held tickers (mutated on success)
        balance: Current portfolio balance
        daily_spend: Current daily spend (for budget checks)

    Returns:
        (success, cost_usd) — success is True if at least the buy leg filled
    """
    buy_leg = trade.buy_leg
    sell_leg = trade.sell_leg

    buy_cost = buy_leg.count * buy_leg.price_cents / 100.0

    # Final safety checks
    remaining_budget = config.MAX_DAILY_LOSS_USD - daily_spend
    if buy_cost > remaining_budget:
        logger.info("Spread skip: buy cost $%.2f > remaining budget $%.2f", buy_cost, remaining_budget)
        return False, 0.0

    if buy_cost > balance:
        logger.info("Spread skip: buy cost $%.2f > balance $%.2f", buy_cost, balance)
        return False, 0.0

    # Double-check held tickers (may have changed since generation)
    if buy_leg.ticker in held_tickers:
        logger.info("Spread skip: already hold buy leg %s", buy_leg.ticker)
        return False, 0.0
    if sell_leg and sell_leg.ticker in held_tickers:
        logger.info("Spread skip: already hold sell leg %s", sell_leg.ticker)
        return False, 0.0

    # Risk check for buy leg
    try:
        risk_manager.check_buy(buy_leg.ticker, buy_cost)
    except RiskLimitExceeded as exc:
        logger.warning("Spread buy leg blocked: %s", exc)
        return False, 0.0

    # === Execute buy leg ===
    logger.info(
        "SPREAD %s: Executing buy leg — %s × %d @ %d¢",
        trade.trade_type, buy_leg.ticker, buy_leg.count, buy_leg.price_cents,
    )
    buy_result = place_buy_order(
        ticker=buy_leg.ticker,
        yes_price_cents=buy_leg.price_cents,
        count=buy_leg.count,
    )

    if not buy_result.success:
        logger.error("Spread buy leg FAILED for %s: %s", buy_leg.ticker, buy_result.error)
        _record_spread_position(trade, buy_result, None)
        return False, 0.0

    # Record buy in risk manager
    risk_manager.record_buy(buy_leg.ticker, buy_cost)
    held_tickers.add(buy_leg.ticker)
    logger.info("Spread buy leg FILLED: %s × %d @ %d¢ ($%.2f)",
                buy_leg.ticker, buy_leg.count, buy_leg.price_cents, buy_cost)

    # === Execute sell leg (if present) ===
    sell_result = None
    if sell_leg:
        logger.info(
            "SPREAD %s: Executing sell leg — %s × %d @ %d¢",
            trade.trade_type, sell_leg.ticker, sell_leg.count, sell_leg.price_cents,
        )
        sell_result = place_sell_order(
            ticker=sell_leg.ticker,
            yes_price_cents=sell_leg.price_cents,
            count=sell_leg.count,
        )

        if sell_result.success:
            sell_proceeds = sell_leg.count * sell_leg.price_cents / 100.0
            risk_manager.record_sell(sell_leg.ticker, sell_proceeds)
            held_tickers.discard(sell_leg.ticker)
            logger.info("Spread sell leg FILLED: %s × %d @ %d¢ ($%.2f)",
                        sell_leg.ticker, sell_leg.count, sell_leg.price_cents, sell_proceeds)
        else:
            logger.warning(
                "Spread sell leg FAILED for %s: %s — left with directional position on buy leg",
                sell_leg.ticker, sell_result.error,
            )

    # Record to positions file
    _record_spread_position(trade, buy_result, sell_result)

    # Send telegram alert
    telegram_alerts.alert_spread_executed(trade, buy_result, sell_result)

    return True, buy_cost
