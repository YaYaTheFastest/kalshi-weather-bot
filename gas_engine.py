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
import math
from dataclasses import dataclass
from typing import Optional

import config
from gas_scanner import GasPriceForecast
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

        # Skip if too expensive
        if market.yes_ask >= config.GAS_BUY_MAX_PRICE:
            continue

        # Update forecast with this market's settlement horizon
        forecast.days_to_settlement = market.days_to_settlement

        # Compute our model's probability that gas > strike
        prob_above = forecast.confidence_above(market.strike_price)

        # Projected settlement price (for logging)
        days = max(1, market.days_to_settlement)
        if forecast.week_ago_price > 0:
            daily_drift = (forecast.current_price - forecast.week_ago_price) / 7.0
        else:
            daily_drift = forecast.daily_change
        projected = forecast.current_price + daily_drift * days

        logger.debug(
            "GAS %s | strike $%.3f | current $%.3f | projected $%.3f | "
            "P(above)=%.1f%% | ask $%.2f | days=%d",
            market.ticker, market.strike_price, forecast.current_price,
            projected, prob_above * 100, market.yes_ask, market.days_to_settlement,
        )

        # YES side: our model says high probability above strike,
        # but market is pricing it low (cheap YES contract)
        if prob_above > config.GAS_BUY_CONFIDENCE_THRESHOLD and market.yes_ask < config.GAS_BUY_MAX_PRICE:
            edge = prob_above - market.yes_ask
            signal = GasBuySignal(
                market=market,
                model_confidence=prob_above,
                market_price=market.yes_ask,
                current_gas_price=forecast.current_price,
                projected_price=projected,
                edge=edge,
                direction="above",
            )
            signals.append(signal)
            logger.info("GAS BUY SIGNAL: %s", signal)

        # NO side: our model says low probability above strike,
        # but market YES price is high (so NO is cheap)
        # NO price = 1.00 - yes_ask (approximately)
        no_price = 1.0 - market.yes_bid if market.yes_bid > 0 else 1.0 - market.yes_ask
        prob_below = 1.0 - prob_above
        if (
            prob_below > config.GAS_BUY_CONFIDENCE_THRESHOLD
            and no_price < config.GAS_BUY_MAX_PRICE
            and no_price > 0
        ):
            edge = prob_below - no_price
            signal = GasBuySignal(
                market=market,
                model_confidence=prob_below,
                market_price=no_price,
                current_gas_price=forecast.current_price,
                projected_price=projected,
                edge=edge,
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
    Returns sell signals when bid > GAS_SELL_MIN_PRICE.
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

        if bid > config.GAS_SELL_MIN_PRICE:
            signal = GasSellSignal(
                position=position,
                market=market,
                bid_price=bid,
                reason="take_profit",
            )
            signals.append(signal)
            logger.info("GAS SELL SIGNAL: %s", signal)

    return signals
