"""
gas_engine.py
-------------
Decision engine for gas price markets. Compares AAA gas price forecasts
against Kalshi market prices to generate buy/sell signals.

Buy signal logic:
  - Kalshi market asks "Will gas be ABOVE $X.XX?" at settlement
  - We estimate P(gas > X.XX) from current AAA data + trend model
  - If our confidence >> market price, there's an edge → buy YES
  - If our confidence << (1 - market price), the price is too high → buy NO
    (but we only trade YES side for simplicity in v1)

Sell signal logic:
  - Same as weather bot: exit when bid price rises above SELL_MIN_PRICE
"""

import logging
from dataclasses import dataclass
from typing import Optional

import config
from gas_scanner import GasPriceForecast  # alias for CommodityForecast
from gas_markets import GasMarket
from kalshi_client import KalshiPosition

logger = logging.getLogger(__name__)


# Gas-specific thresholds are defined in config.py and can be
# overridden via environment variables in .env.


# ---------------------------------------------------------------------------
# Signal data classes
# ---------------------------------------------------------------------------

@dataclass
class GasBuySignal:
    """Recommended buy on a gas price market."""
    market: GasMarket
    model_confidence: float     # 0.0 – 1.0, P(price > strike) or P(price <= strike)
    market_price: float         # yes_ask in dollars
    current_gas_price: float    # Current AAA national average
    projected_price: float      # Model's projected settlement price
    edge: float                 # model_confidence - market_price
    direction: str              # "above" (YES) or "below" (NO)

    def __str__(self) -> str:
        return (
            f"GAS BUY {self.market.ticker} | "
            f"Strike: ${self.market.strike_price:.3f} | "
            f"Current AAA: ${self.current_gas_price:.3f} | "
            f"Model conf: {self.model_confidence:.1%} | "
            f"Ask: ${self.market_price:.2f} | "
            f"Edge: {self.edge:.1%} | "
            f"Type: {self.market.market_type} | "
            f"Settles in {self.market.days_to_settlement}d"
        )


@dataclass
class GasSellSignal:
    """Recommended exit of a gas market position."""
    position: KalshiPosition
    market: Optional[GasMarket]
    bid_price: float
    reason: str

    def __str__(self) -> str:
        return (
            f"GAS SELL {self.position.ticker} | "
            f"Reason: {self.reason} | "
            f"Bid: ${self.bid_price:.2f}"
        )


# ---------------------------------------------------------------------------
# Signal generators
# ---------------------------------------------------------------------------

def generate_gas_buy_signals(
    forecast: GasPriceForecast,
    open_markets: list[GasMarket],
    held_tickers: set[str],
) -> list[GasBuySignal]:
    """
    Scan all open gas price markets against our price forecast.
    Returns a ranked list of buy signals (highest edge first).
    """
    signals: list[GasBuySignal] = []

    for market in open_markets:
        # Skip markets we already own
        if market.ticker in held_tickers:
            continue

        # Skip if no liquidity
        if market.yes_ask <= 0:
            continue

        # Skip if ask exceeds max
        if market.yes_ask > config.COMMODITY_MAX_ASK:
            continue

        # Update forecast with this market's settlement horizon and dampening
        forecast.days_to_settlement = market.days_to_settlement
        forecast.drift_dampening = config.COMMODITY_DRIFT_DAMPENING

        # Compute our model's probability that gas > strike
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
            "GAS %s | strike $%.3f | current $%.3f | projected $%.3f | "
            "P(above)=%.1f%% | ask $%.2f | days=%d",
            market.ticker, market.strike_price, forecast.current_price,
            projected, prob_above * 100, market.yes_ask, market.days_to_settlement,
        )

        # YES side: edge-based entry
        # edge = model_confidence - market_ask_price
        yes_edge = prob_above - market.yes_ask
        if (
            yes_edge >= config.COMMODITY_MIN_EDGE
            and prob_above >= config.COMMODITY_MIN_CONFIDENCE
            and market.yes_ask <= config.COMMODITY_MAX_ASK
        ):
            signal = GasBuySignal(
                market=market,
                model_confidence=prob_above,
                market_price=market.yes_ask,
                current_gas_price=forecast.current_price,
                projected_price=projected,
                edge=yes_edge,
                direction="above",
            )
            signals.append(signal)
            logger.info("GAS BUY SIGNAL: %s", signal)

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
            signal = GasBuySignal(
                market=market,
                model_confidence=prob_below,
                market_price=no_price,
                current_gas_price=forecast.current_price,
                projected_price=projected,
                edge=no_edge,
                direction="below",
            )
            signals.append(signal)
            logger.info("GAS BUY SIGNAL (NO side): %s", signal)

    # Sort by edge descending
    signals.sort(key=lambda s: s.edge, reverse=True)
    return signals


def generate_gas_sell_signals(
    positions: list[KalshiPosition],
    open_markets: list[GasMarket],
) -> list[GasSellSignal]:
    """
    Check gas positions for exit opportunities.
    Only sells when bid > COMMODITY_SELL_MIN_PRICE AND bid > cost basis per contract.
    This prevents selling at a loss (buy at 60¢, sell at 59¢ bug).
    """
    market_by_ticker = {m.ticker: m for m in open_markets}
    signals: list[GasSellSignal] = []

    for position in positions:
        if position.market_exposure <= 0:
            continue

        # Only process gas market positions
        ticker_upper = position.ticker.upper()
        if "KXAAAGASW" not in ticker_upper and "KXAAAGASM" not in ticker_upper:
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
            signal = GasSellSignal(
                position=position,
                market=market,
                bid_price=bid,
                reason="take_profit",
            )
            signals.append(signal)
            logger.info("GAS SELL SIGNAL: %s (breakeven $%.3f incl fees)", signal, breakeven)
        elif bid > config.COMMODITY_SELL_MIN_PRICE:
            logger.debug(
                "GAS sell skipped %s: bid $%.2f <= breakeven $%.3f (cost $%.3f + fees)",
                position.ticker, bid, breakeven, cost_per_contract,
            )

    return signals
