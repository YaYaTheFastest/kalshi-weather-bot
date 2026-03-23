"""
gas_scanner.py
--------------
Fetches current and recent gas price data from AAA (gasprices.aaa.com)
and models the probability that the end-of-week or end-of-month national
average will land in a given price bucket.

Data sources:
  - AAA national average (primary — matches Kalshi settlement source)
  - EIA weekly retail gasoline prices (supplementary trend data)

The forecasting model uses:
  1. Current AAA price as the mean
  2. Recent daily price changes to estimate volatility (σ)
  3. Gaussian CDF to compute P(price > strike) at settlement time
"""

import logging
import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from typing import Optional

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class GasPriceForecast:
    """Holds processed gas price data and forecast for settlement."""
    current_price: float           # Current AAA national average ($/gal)
    yesterday_price: float         # Yesterday's AAA average
    week_ago_price: float          # Week ago AAA average
    month_ago_price: float         # Month ago AAA average
    daily_change: float            # Today vs yesterday
    weekly_change: float           # Today vs week ago
    price_std: float               # Estimated daily volatility (std dev)
    forecast_date: date            # When we fetched this
    days_to_settlement: int        # Days until market settles

    def confidence_above(self, strike: float) -> float:
        """
        Estimate P(settlement_price > strike) using a random walk model.

        The price at settlement is modeled as:
          price_settlement ~ N(current_price + drift * days, sigma * sqrt(days))

        Where:
          - drift = recent daily trend (momentum)
          - sigma = daily price volatility
        """
        if self.days_to_settlement <= 0:
            # Settlement day — use current price directly
            return 1.0 if self.current_price > strike else 0.0

        days = max(1, self.days_to_settlement)

        # Drift: use average daily change over the past week
        if self.week_ago_price > 0 and self.current_price > 0:
            daily_drift = (self.current_price - self.week_ago_price) / 7.0
        else:
            daily_drift = self.daily_change

        # Projected price at settlement
        projected_price = self.current_price + daily_drift * days

        # Uncertainty grows with sqrt(days)
        # Floor sigma at $0.01/day to avoid overconfidence
        sigma = max(self.price_std, 0.01) * math.sqrt(days)

        if sigma <= 0:
            return 1.0 if projected_price > strike else 0.0

        # P(price > strike) = 1 - Φ((strike - projected) / sigma)
        z = (strike - projected_price) / sigma
        prob = 0.5 * (1.0 - math.erf(z / math.sqrt(2)))

        return prob

    def confidence_below(self, strike: float) -> float:
        """P(settlement_price <= strike)"""
        return 1.0 - self.confidence_above(strike)


# ---------------------------------------------------------------------------
# AAA gas price fetcher
# ---------------------------------------------------------------------------

def _parse_price(text: str) -> Optional[float]:
    """Extract a dollar amount from text like '$3.956'."""
    match = re.search(r'\$?([\d]+\.[\d]+)', text)
    if match:
        return float(match.group(1))
    return None


def fetch_aaa_prices() -> Optional[GasPriceForecast]:
    """
    Fetch current gas prices from AAA's website.
    Returns a GasPriceForecast object or None on failure.
    """
    try:
        # AAA has a simple page we can parse for the national average
        resp = requests.get(
            "https://gasprices.aaa.com",
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; KalshiGasBot/1.0)"
            },
            timeout=15,
        )
        resp.raise_for_status()
        html = resp.text

        # Parse prices from the HTML
        # Look for the national average table data
        current_price = None
        yesterday_price = None
        week_ago_price = None
        month_ago_price = None

        # Try to find the numb class elements (AAA's price display)
        # First price occurrence is typically the national average
        price_matches = re.findall(r'\$(\d+\.\d{2,3})', html)

        if len(price_matches) >= 1:
            current_price = float(price_matches[0])

        # Look for table with historical comparisons
        # Pattern: Current Avg, Yesterday Avg, Week Ago Avg, Month Ago Avg
        # Each row has Regular, Mid-Grade, Premium, Diesel, E85
        # We want the Regular (first) column

        # Find all price values that look like gas prices ($2.50-$6.00 range)
        gas_prices = [float(p) for p in price_matches if 2.0 <= float(p) <= 7.0]

        if len(gas_prices) >= 4:
            # Typical order: current regular, current mid, current premium, current diesel, current E85
            # yesterday regular, yesterday mid, ...
            # week ago regular, ...
            # month ago regular, ...
            # Each row has 5 fuel types, we want index 0, 5, 10, 15 (Regular column)
            if len(gas_prices) >= 16:
                current_price = gas_prices[0]
                yesterday_price = gas_prices[5]
                week_ago_price = gas_prices[10]
                month_ago_price = gas_prices[15]
            elif current_price and len(gas_prices) >= 6:
                # Fallback: just grab what we can
                yesterday_price = gas_prices[5] if len(gas_prices) > 5 else None

        if current_price is None:
            logger.error("Could not parse current price from AAA")
            return None

        # Estimate daily volatility from available data
        daily_changes = []
        if yesterday_price and yesterday_price > 0:
            daily_changes.append(abs(current_price - yesterday_price))
        if week_ago_price and week_ago_price > 0:
            # Average daily change over the week
            avg_daily = abs(current_price - week_ago_price) / 7.0
            daily_changes.append(avg_daily)
        if month_ago_price and month_ago_price > 0:
            avg_daily_month = abs(current_price - month_ago_price) / 30.0
            daily_changes.append(avg_daily_month)

        # Use the median of available volatility estimates
        if daily_changes:
            price_std = statistics.median(daily_changes)
        else:
            # Default to ~1 cent/day volatility
            price_std = 0.01

        # Floor volatility at 0.5 cents to avoid overconfidence
        price_std = max(price_std, 0.005)

        daily_change = (current_price - yesterday_price) if yesterday_price else 0.0

        logger.info(
            "AAA gas: current $%.3f | yesterday $%.3f | week ago $%.3f | "
            "month ago $%.3f | daily σ $%.4f",
            current_price,
            yesterday_price or 0,
            week_ago_price or 0,
            month_ago_price or 0,
            price_std,
        )

        return GasPriceForecast(
            current_price=current_price,
            yesterday_price=yesterday_price or current_price,
            week_ago_price=week_ago_price or current_price,
            month_ago_price=month_ago_price or current_price,
            daily_change=daily_change,
            weekly_change=(current_price - week_ago_price) if week_ago_price else 0.0,
            price_std=price_std,
            forecast_date=date.today(),
            days_to_settlement=0,  # Will be set by caller
        )

    except Exception as exc:
        logger.error("Failed to fetch AAA gas prices: %s", exc)
        return None


def fetch_gas_forecast(days_to_settlement: int) -> Optional[GasPriceForecast]:
    """
    Fetch AAA gas prices and prepare a forecast for the given settlement horizon.

    Args:
        days_to_settlement: Number of days until the market settles
    """
    forecast = fetch_aaa_prices()
    if forecast:
        forecast.days_to_settlement = days_to_settlement
    return forecast
