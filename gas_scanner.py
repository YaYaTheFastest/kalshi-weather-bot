"""
gas_scanner.py
--------------
Fetches current and recent gas price data from AAA (gasprices.aaa.com)
and returns a CommodityForecast for use by the gas trading engine.

Data sources:
  - AAA national average (primary — matches Kalshi settlement source)

The forecasting model lives in price_model.py (shared with oil and future markets).
"""

import logging
import re
from datetime import date
from typing import Optional

import requests

from price_model import CommodityForecast, compute_residual_volatility

logger = logging.getLogger(__name__)


# Keep GasPriceForecast as an alias for backwards compatibility with gas_engine imports
GasPriceForecast = CommodityForecast


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

        # Extract all dollar amounts that look like gas prices
        price_matches = re.findall(r'\$(\d+\.\d{2,3})', html)
        gas_prices = [float(p) for p in price_matches if 2.0 <= float(p) <= 7.0]

        if len(gas_prices) < 1:
            logger.error("No gas prices found in AAA HTML")
            return None

        # The first price is the national average hero number
        current_price = gas_prices[0]

        # Find the comparison table: 4 rows × 5 fuel types (Regular, Mid, Premium, Diesel, E85)
        # The table starts where we see the current regular price followed by
        # ascending values (mid > regular, premium > mid).
        # Row 0 = Current, Row 1 = Yesterday, Row 2 = Week Ago, Row 3 = Month Ago
        table_start = None
        for i in range(len(gas_prices) - 4):
            if abs(gas_prices[i] - current_price) < 0.002:  # matches national avg
                # Check if next values look like mid-grade > regular, premium > mid
                if (i + 2 < len(gas_prices)
                        and gas_prices[i + 1] > gas_prices[i]
                        and gas_prices[i + 2] > gas_prices[i + 1]):
                    table_start = i
                    break

        if table_start is not None and table_start + 20 <= len(gas_prices):
            # Each row has 5 columns; we want the first column (Regular)
            current_price = gas_prices[table_start]          # Row 0, col 0
            yesterday_price = gas_prices[table_start + 5]    # Row 1, col 0
            week_ago_price = gas_prices[table_start + 10]    # Row 2, col 0
            month_ago_price = gas_prices[table_start + 15]   # Row 3, col 0
            logger.info(
                "AAA table found at index %d: current $%.3f, yesterday $%.3f, "
                "week_ago $%.3f, month_ago $%.3f",
                table_start, current_price, yesterday_price,
                week_ago_price, month_ago_price,
            )
        else:
            # Fallback: use just the national average, estimate others
            logger.warning(
                "Could not find AAA comparison table (table_start=%s, prices=%d). "
                "Using current price only.",
                table_start, len(gas_prices),
            )

        if current_price is None:
            logger.error("Could not parse current price from AAA")
            return None

        # Compute residual-based volatility (separates noise from trend)
        # Floor at $0.008/day for gas prices to prevent overconfidence
        price_std = compute_residual_volatility(
            current_price=current_price,
            yesterday_price=yesterday_price or current_price,
            week_ago_price=week_ago_price or current_price,
            vol_floor=0.008,
        )

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

        return CommodityForecast(
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


def fetch_gas_forecast(days_to_settlement: int) -> Optional[CommodityForecast]:
    """
    Fetch AAA gas prices and prepare a forecast for the given settlement horizon.

    Args:
        days_to_settlement: Number of days until the market settles
    """
    forecast = fetch_aaa_prices()
    if forecast:
        forecast.days_to_settlement = days_to_settlement
    return forecast
