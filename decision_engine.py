"""
decision_engine.py
------------------
Compares NOAA hourly forecasts to live Kalshi market prices and
generates buy/sell signals based on configured thresholds.

Buy signal:
  - NOAA confidence that tomorrow's high falls in the market's temp bucket
    is greater than BUY_CONFIDENCE_THRESHOLD (default 85%)
  - The market's YES ask price is below BUY_MAX_PRICE (default $0.15)
  - We do not already hold a position in this market

Sell signal (take profit / exit):
  - We hold a position in a market
  - The market's YES bid price has risen above SELL_MIN_PRICE (default $0.45)
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional

import config
from kalshi_client import KalshiMarket, KalshiPosition
from noaa_scanner import CityForecast

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal data classes
# ---------------------------------------------------------------------------

@dataclass
class BuySignal:
    """Represents a recommended buy on a specific market."""
    market: KalshiMarket
    city_key: str
    city_name: str
    noaa_confidence: float      # 0.0 – 1.0
    market_price: float         # yes_ask in dollars
    forecasted_high: float      # NOAA point estimate of tomorrow's high (°F)
    edge: float                 # noaa_confidence - market_price (approximate edge)

    def __str__(self) -> str:
        return (
            f"BUY {self.market.ticker} | "
            f"City: {self.city_name} | "
            f"NOAA conf: {self.noaa_confidence:.1%} | "
            f"Ask: ${self.market_price:.2f} | "
            f"Forecast high: {self.forecasted_high:.0f}°F | "
            f"Edge: {self.edge:.1%}"
        )


@dataclass
class SellSignal:
    """Represents a recommended exit of an existing position."""
    position: KalshiPosition
    market: Optional[KalshiMarket]   # None if market data unavailable
    bid_price: float                  # current yes_bid in dollars
    reason: str                       # "take_profit" or "stop_loss"

    def __str__(self) -> str:
        ticker = self.position.ticker
        return (
            f"SELL {ticker} | "
            f"Reason: {self.reason} | "
            f"Bid: ${self.bid_price:.2f}"
        )


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

def generate_buy_signals(
    forecasts: dict[str, Optional[CityForecast]],
    open_markets: list[KalshiMarket],
    held_tickers: set[str],
) -> list[BuySignal]:
    """
    Scan all open temperature markets against NOAA forecasts.
    Returns a ranked list of buy signals (highest edge first).

    Args:
        forecasts:      Dict of city_key -> CityForecast (or None on failure)
        open_markets:   All open KXHIGH markets from Kalshi
        held_tickers:   Set of tickers we already hold (skip these)
    """
    signals: list[BuySignal] = []

    for market in open_markets:
        # Skip markets we already own
        if market.ticker in held_tickers:
            continue

        # Skip if we couldn't identify the city
        if not market.city_key:
            continue

        # Skip if no forecast for this city
        forecast = forecasts.get(market.city_key)
        if forecast is None:
            continue

        # Skip if bucket parsing failed
        if market.bucket_low is None or market.bucket_high is None:
            continue

        # Skip if market ask price is zero (no liquidity)
        if market.yes_ask <= 0:
            continue

        # Compute NOAA confidence for this bucket
        confidence = forecast.confidence_in_range(market.bucket_low, market.bucket_high)

        logger.debug(
            "%s | bucket [%.0f, %.0f] | NOAA conf %.1f%% | ask $%.2f",
            market.ticker,
            market.bucket_low if not math.isinf(market.bucket_low) else -999,
            market.bucket_high if not math.isinf(market.bucket_high) else 999,
            confidence * 100,
            market.yes_ask,
        )

        # Buy signal check
        if (
            confidence > config.BUY_CONFIDENCE_THRESHOLD
            and market.yes_ask < config.BUY_MAX_PRICE
        ):
            edge = confidence - market.yes_ask
            city_info = config.CITIES[market.city_key]
            signal = BuySignal(
                market=market,
                city_key=market.city_key,
                city_name=city_info["name"],
                noaa_confidence=confidence,
                market_price=market.yes_ask,
                forecasted_high=forecast.forecasted_high,
                edge=edge,
            )
            signals.append(signal)
            logger.info("BUY SIGNAL: %s", signal)

    # Sort by edge descending — best opportunities first
    signals.sort(key=lambda s: s.edge, reverse=True)
    return signals


def generate_sell_signals(
    positions: list[KalshiPosition],
    open_markets: list[KalshiMarket],
) -> list[SellSignal]:
    """
    Check all held positions against current market bid prices.
    Returns sell signals for any position where bid > SELL_MIN_PRICE.

    Args:
        positions:    List of currently held positions
        open_markets: Current Kalshi market data (for bid prices)
    """
    market_by_ticker: dict[str, KalshiMarket] = {m.ticker: m for m in open_markets}
    signals: list[SellSignal] = []

    for position in positions:
        if position.market_exposure <= 0:
            continue  # only handle long YES positions

        market = market_by_ticker.get(position.ticker)
        bid = market.yes_bid if market else 0.0

        if bid > config.SELL_MIN_PRICE:
            signal = SellSignal(
                position=position,
                market=market,
                bid_price=bid,
                reason="take_profit",
            )
            signals.append(signal)
            logger.info("SELL SIGNAL: %s", signal)

    return signals
