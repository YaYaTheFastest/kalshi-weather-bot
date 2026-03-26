"""
metals_engine.py
----------------
Decision engine for gold and silver markets. Compares spot price forecasts
against Kalshi market prices to generate buy/sell signals.

Uses the same edge-based entry logic as oil_engine.py and gas_engine.py:
  - edge >= COMMODITY_MIN_EDGE
  - confidence >= COMMODITY_MIN_CONFIDENCE
  - ask <= COMMODITY_MAX_ASK

Sell logic uses fee-aware breakeven calculation (same as gas_engine.py):
  - bid > COMMODITY_SELL_MIN_PRICE AND bid > cost basis + fees
"""

import logging
from dataclasses import dataclass
from typing import Optional

import config
from price_model import CommodityForecast
from metals_markets import MetalsMarket
from kalshi_client import KalshiPosition

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal data classes
# ---------------------------------------------------------------------------

@dataclass
class MetalsBuySignal:
    """Recommended buy on a gold or silver market."""
    market: MetalsMarket
    model_confidence: float     # 0.0 – 1.0
    market_price: float         # yes_ask or no_price in dollars
    current_price: float        # Current gold/silver spot price
    projected_price: float      # Model's projected settlement price
    edge: float                 # model_confidence - market_price
    direction: str              # "above" (YES) or "below" (NO)
    metal: str                  # "gold" or "silver"

    def __str__(self) -> str:
        metal_upper = self.metal.upper()
        price_fmt = ":.2f" if self.metal == "gold" else ":.2f"
        return (
            f"{metal_upper} BUY {self.market.ticker} | "
            f"Strike: ${self.market.strike_price:.2f} | "
            f"Current: ${self.current_price:.2f} | "
            f"Model conf: {self.model_confidence:.1%} | "
            f"Ask: ${self.market_price:.2f} | "
            f"Edge: {self.edge:.1%} | "
            f"Type: {self.market.market_type} | "
            f"Settles in {self.market.days_to_settlement}d"
        )


@dataclass
class MetalsSellSignal:
    """Recommended exit of a metals market position."""
    position: KalshiPosition
    market: Optional[MetalsMarket]
    bid_price: float
    reason: str

    def __str__(self) -> str:
        return (
            f"METALS SELL {self.position.ticker} | "
            f"Reason: {self.reason} | "
            f"Bid: ${self.bid_price:.2f}"
        )


# ---------------------------------------------------------------------------
# Metals ticker detection
# ---------------------------------------------------------------------------

# All known metals series prefixes
_GOLD_PREFIXES = ("KXGOLDD", "KXGOLDW", "KXGOLDMON")
_SILVER_PREFIXES = ("KXSILVERD", "KXSILVERW", "KXSILVERMON")
_ALL_METALS_PREFIXES = _GOLD_PREFIXES + _SILVER_PREFIXES


def _is_metals_position(ticker: str) -> bool:
    """Check if a position ticker belongs to a metals market."""
    ticker_upper = ticker.upper()
    return any(ticker_upper.startswith(prefix) for prefix in _ALL_METALS_PREFIXES)


def _is_gold_position(ticker: str) -> bool:
    """Check if a position ticker belongs to a gold market."""
    ticker_upper = ticker.upper()
    return any(ticker_upper.startswith(prefix) for prefix in _GOLD_PREFIXES)


def _is_silver_position(ticker: str) -> bool:
    """Check if a position ticker belongs to a silver market."""
    ticker_upper = ticker.upper()
    return any(ticker_upper.startswith(prefix) for prefix in _SILVER_PREFIXES)


# ---------------------------------------------------------------------------
# Signal generators
# ---------------------------------------------------------------------------

def generate_metals_buy_signals(
    forecast: CommodityForecast,
    open_markets: list[MetalsMarket],
    held_tickers: set[str],
    metal: str = "gold",
) -> list[MetalsBuySignal]:
    """
    Scan all open metals markets against our price forecast.
    Returns a ranked list of buy signals (highest edge first).
    """
    signals: list[MetalsBuySignal] = []

    for market in open_markets:
        if market.ticker in held_tickers:
            continue

        if market.yes_ask <= 0:
            continue

        if market.yes_ask > config.COMMODITY_MAX_ASK:
            continue

        # Update forecast with this market's settlement horizon and dampening
        forecast.days_to_settlement = market.days_to_settlement
        forecast.drift_dampening = config.COMMODITY_DRIFT_DAMPENING

        # Compute probability that price > strike
        prob_above = forecast.confidence_above(market.strike_price)

        # Projected settlement price (for logging)
        days = max(1, market.days_to_settlement)
        if forecast.week_ago_price > 0:
            raw_drift = forecast.current_price - forecast.week_ago_price
            daily_drift = (raw_drift * config.COMMODITY_DRIFT_DAMPENING) / 7.0
        else:
            daily_drift = forecast.daily_change
        projected = forecast.current_price + daily_drift * days

        logger.debug(
            "%s %s | strike $%.2f | current $%.2f | projected $%.2f | "
            "P(above)=%.1f%% | ask $%.2f | days=%d",
            metal.upper(), market.ticker, market.strike_price, forecast.current_price,
            projected, prob_above * 100, market.yes_ask, market.days_to_settlement,
        )

        # YES side: edge-based entry
        yes_edge = prob_above - market.yes_ask
        if (
            yes_edge >= config.COMMODITY_MIN_EDGE
            and prob_above >= config.COMMODITY_MIN_CONFIDENCE
            and market.yes_ask <= config.COMMODITY_MAX_ASK
        ):
            signal = MetalsBuySignal(
                market=market,
                model_confidence=prob_above,
                market_price=market.yes_ask,
                current_price=forecast.current_price,
                projected_price=projected,
                edge=yes_edge,
                direction="above",
                metal=metal,
            )
            signals.append(signal)
            logger.info("%s BUY SIGNAL: %s", metal.upper(), signal)

        # NO side: edge-based entry
        no_price = 1.0 - market.yes_bid if market.yes_bid > 0 else 1.0 - market.yes_ask
        prob_below = 1.0 - prob_above
        no_edge = prob_below - no_price
        if (
            no_edge >= config.COMMODITY_MIN_EDGE
            and prob_below >= config.COMMODITY_MIN_CONFIDENCE
            and no_price <= config.COMMODITY_MAX_ASK
            and no_price > 0
        ):
            signal = MetalsBuySignal(
                market=market,
                model_confidence=prob_below,
                market_price=no_price,
                current_price=forecast.current_price,
                projected_price=projected,
                edge=no_edge,
                direction="below",
                metal=metal,
            )
            signals.append(signal)
            logger.info("%s BUY SIGNAL (NO side): %s", metal.upper(), signal)

    signals.sort(key=lambda s: s.edge, reverse=True)
    return signals


def generate_metals_sell_signals(
    positions: list[KalshiPosition],
    open_markets: list[MetalsMarket],
) -> list[MetalsSellSignal]:
    """
    Check metals positions for exit opportunities.
    Uses fee-aware sell logic: only sells when bid > COMMODITY_SELL_MIN_PRICE
    AND bid > cost basis per contract (including buy fees + estimated sell fee).
    This prevents selling at a loss.
    """
    market_by_ticker = {m.ticker: m for m in open_markets}
    signals: list[MetalsSellSignal] = []

    for position in positions:
        if position.market_exposure <= 0:
            continue

        # Only process metals market positions
        if not _is_metals_position(position.ticker):
            continue

        market = market_by_ticker.get(position.ticker)
        bid = market.yes_bid if market else 0.0

        # Compute cost basis per contract INCLUDING fees to avoid selling at a loss.
        # True cost = (exposure + buy fees) / contracts.
        # Also estimate sell-side fee (~2¢/contract) so we only sell when actually profitable.
        cost_per_contract = 0.0
        if position.market_exposure > 0 and position.market_exposure_dollars > 0:
            total_cost_with_fees = position.market_exposure_dollars + position.fees_paid
            cost_per_contract = total_cost_with_fees / position.market_exposure
        # Add estimated sell fee (~$0.02/contract) to break-even threshold
        breakeven = cost_per_contract + 0.02

        # Only sell if bid exceeds BOTH the min price floor AND our break-even
        if bid > config.COMMODITY_SELL_MIN_PRICE and bid > breakeven:
            signal = MetalsSellSignal(
                position=position,
                market=market,
                bid_price=bid,
                reason="take_profit",
            )
            signals.append(signal)
            logger.info("METALS SELL SIGNAL: %s (breakeven $%.3f incl fees)", signal, breakeven)
        elif bid > config.COMMODITY_SELL_MIN_PRICE:
            logger.debug(
                "METALS sell skipped %s: bid $%.2f <= breakeven $%.3f (cost $%.3f + fees)",
                position.ticker, bid, breakeven, cost_per_contract,
            )

    return signals
