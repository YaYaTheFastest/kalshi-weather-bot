"""
oil_engine.py
-------------
Decision engine for WTI crude oil markets. Compares oil price forecasts
against Kalshi market prices to generate buy/sell signals.

Uses the same edge-based entry logic as gas_engine.py:
  - edge >= COMMODITY_MIN_EDGE
  - confidence >= COMMODITY_MIN_CONFIDENCE
  - ask <= COMMODITY_MAX_ASK
"""

import logging
from dataclasses import dataclass
from typing import Optional

import config
from price_model import CommodityForecast
from oil_markets import OilMarket
from kalshi_client import KalshiPosition

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal data classes
# ---------------------------------------------------------------------------

@dataclass
class OilBuySignal:
    """Recommended buy on a WTI oil market."""
    market: OilMarket
    model_confidence: float     # 0.0 – 1.0
    market_price: float         # yes_ask or no_price in dollars
    current_oil_price: float    # Current WTI price
    projected_price: float      # Model's projected settlement price
    edge: float                 # model_confidence - market_price
    direction: str              # "above" (YES) or "below" (NO)

    def __str__(self) -> str:
        return (
            f"OIL BUY {self.market.ticker} | "
            f"Strike: ${self.market.strike_price:.2f} | "
            f"Current WTI: ${self.current_oil_price:.2f} | "
            f"Model conf: {self.model_confidence:.1%} | "
            f"Ask: ${self.market_price:.2f} | "
            f"Edge: {self.edge:.1%} | "
            f"Type: {self.market.market_type} | "
            f"Settles in {self.market.days_to_settlement}d"
        )


@dataclass
class OilSellSignal:
    """Recommended exit of an oil market position."""
    position: KalshiPosition
    market: Optional[OilMarket]
    bid_price: float
    reason: str

    def __str__(self) -> str:
        return (
            f"OIL SELL {self.position.ticker} | "
            f"Reason: {self.reason} | "
            f"Bid: ${self.bid_price:.2f}"
        )


# ---------------------------------------------------------------------------
# Signal generators
# ---------------------------------------------------------------------------

def generate_oil_buy_signals(
    forecast: CommodityForecast,
    open_markets: list[OilMarket],
    held_tickers: set[str],
) -> list[OilBuySignal]:
    """
    Scan all open oil markets against our price forecast.
    Returns a ranked list of buy signals (highest edge first).
    """
    signals: list[OilBuySignal] = []

    for market in open_markets:
        if market.ticker in held_tickers:
            continue

        if market.yes_ask <= 0:
            continue

        # Favorite-longshot bias filter: reject sub-10¢ contracts
        if market.yes_ask < config.COMMODITY_MIN_ASK:
            continue

        if market.yes_ask > config.COMMODITY_MAX_ASK:
            continue

        # Update forecast with this market's settlement horizon and dampening
        forecast.days_to_settlement = market.days_to_settlement
        forecast.drift_dampening = config.COMMODITY_DRIFT_DAMPENING

        # Compute probability that oil > strike
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
            "OIL %s | strike $%.2f | current $%.2f | projected $%.2f | "
            "P(above)=%.1f%% | ask $%.2f | days=%d",
            market.ticker, market.strike_price, forecast.current_price,
            projected, prob_above * 100, market.yes_ask, market.days_to_settlement,
        )

        # YES side: edge-based entry
        yes_edge = prob_above - market.yes_ask
        if (
            yes_edge >= config.COMMODITY_MIN_EDGE
            and prob_above >= config.COMMODITY_MIN_CONFIDENCE
            and market.yes_ask <= config.COMMODITY_MAX_ASK
        ):
            signal = OilBuySignal(
                market=market,
                model_confidence=prob_above,
                market_price=market.yes_ask,
                current_oil_price=forecast.current_price,
                projected_price=projected,
                edge=yes_edge,
                direction="above",
            )
            signals.append(signal)
            logger.info("OIL BUY SIGNAL: %s", signal)

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
            signal = OilBuySignal(
                market=market,
                model_confidence=prob_below,
                market_price=no_price,
                current_oil_price=forecast.current_price,
                projected_price=projected,
                edge=no_edge,
                direction="below",
            )
            signals.append(signal)
            logger.info("OIL BUY SIGNAL (NO side): %s", signal)

    signals.sort(key=lambda s: s.edge, reverse=True)
    return signals


def generate_oil_sell_signals(
    positions: list[KalshiPosition],
    open_markets: list[OilMarket],
) -> list[OilSellSignal]:
    """
    Check oil positions for exit opportunities.
    Only sells when bid > COMMODITY_SELL_MIN_PRICE AND bid > cost basis per contract.
    This prevents selling at a loss.
    """
    market_by_ticker = {m.ticker: m for m in open_markets}
    signals: list[OilSellSignal] = []

    for position in positions:
        if position.market_exposure <= 0:
            continue

        # Only process oil market positions
        ticker_upper = position.ticker.upper()
        if "KXWTI" not in ticker_upper:
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
            signal = OilSellSignal(
                position=position,
                market=market,
                bid_price=bid,
                reason="take_profit",
            )
            signals.append(signal)
            logger.info("OIL SELL SIGNAL: %s (breakeven $%.3f incl fees)", signal, breakeven)
        elif bid > config.COMMODITY_SELL_MIN_PRICE:
            logger.debug(
                "OIL sell skipped %s: bid $%.2f <= breakeven $%.3f (cost $%.3f + fees)",
                position.ticker, bid, breakeven, cost_per_contract,
            )

    return signals
