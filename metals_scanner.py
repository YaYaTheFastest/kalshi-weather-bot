"""
metals_scanner.py
-----------------
Fetches current gold and silver spot prices from Yahoo Finance and returns
CommodityForecast objects for use by the metals trading engine.

Data sources:
  - Yahoo Finance chart API (no API key needed)
  - Gold: GC=F (gold futures)
  - Silver: SI=F (silver futures)

Gold prices are in $/oz, typically $2000-4500 range.
Silver prices are in $/oz, typically $20-50 range.
Daily volatility: gold ~$20-30/day (~0.5%), silver ~$0.50-1.00/day (~1%).
"""

import logging
from datetime import date
from typing import Optional

import requests

from price_model import CommodityForecast, compute_residual_volatility

logger = logging.getLogger(__name__)

# Yahoo Finance chart endpoints
_YAHOO_GOLD_URL = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
_YAHOO_SILVER_URL = "https://query1.finance.yahoo.com/v8/finance/chart/SI=F"


def _fetch_yahoo_commodity(url: str, label: str, vol_floor: float) -> Optional[CommodityForecast]:
    """
    Generic fetcher for Yahoo Finance commodity data.
    Returns a CommodityForecast or None on failure.
    """
    try:
        resp = requests.get(
            url,
            params={"interval": "1d", "range": "1mo"},
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; KalshiMetalsBot/1.0)",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        # Navigate Yahoo Finance response structure
        result = data.get("chart", {}).get("result", [])
        if not result:
            logger.error("No chart data in Yahoo Finance response for %s", label)
            return None

        chart = result[0]
        meta = chart.get("meta", {})
        indicators = chart.get("indicators", {}).get("quote", [{}])[0]

        # Current price from meta
        current_price = meta.get("regularMarketPrice")
        if current_price is None:
            logger.error("No regularMarketPrice in Yahoo Finance response for %s", label)
            return None
        current_price = float(current_price)

        # Historical closes
        closes = indicators.get("close", [])
        # Filter out None values
        closes = [float(c) for c in closes if c is not None]

        if len(closes) < 2:
            logger.error("Insufficient historical data from Yahoo Finance for %s (%d closes)", label, len(closes))
            return None

        # Yesterday's close is the second-to-last value
        yesterday_price = closes[-2] if len(closes) >= 2 else current_price

        # Week ago (5 trading days back)
        week_ago_price = closes[-6] if len(closes) >= 6 else closes[0]

        # Month ago (first available close in the range)
        month_ago_price = closes[0]

        daily_change = current_price - yesterday_price
        weekly_change = current_price - week_ago_price

        # Compute residual-based volatility
        price_std = compute_residual_volatility(
            current_price=current_price,
            yesterday_price=yesterday_price,
            week_ago_price=week_ago_price,
            vol_floor=vol_floor,
        )

        logger.info(
            "%s: current $%.2f | yesterday $%.2f | week ago $%.2f | "
            "month ago $%.2f | daily σ $%.2f",
            label, current_price, yesterday_price, week_ago_price,
            month_ago_price, price_std,
        )

        return CommodityForecast(
            current_price=current_price,
            yesterday_price=yesterday_price,
            week_ago_price=week_ago_price,
            month_ago_price=month_ago_price,
            daily_change=daily_change,
            weekly_change=weekly_change,
            price_std=price_std,
            forecast_date=date.today(),
            days_to_settlement=0,  # Will be set by caller
            settlement_sigma=vol_floor,
        )

    except Exception as exc:
        logger.error("Failed to fetch %s prices: %s", label, exc)
        return None


def fetch_gold_prices() -> Optional[CommodityForecast]:
    """
    Fetch gold spot prices from Yahoo Finance.
    Returns a CommodityForecast object or None on failure.
    """
    # Gold vol floor: ~$5/oz (conservative floor for daily moves)
    return _fetch_yahoo_commodity(_YAHOO_GOLD_URL, "Gold", vol_floor=5.0)


def fetch_silver_prices() -> Optional[CommodityForecast]:
    """
    Fetch silver spot prices from Yahoo Finance.
    Returns a CommodityForecast object or None on failure.
    """
    # Silver vol floor: ~$0.15/oz (conservative floor for daily moves)
    return _fetch_yahoo_commodity(_YAHOO_SILVER_URL, "Silver", vol_floor=0.15)


def fetch_gold_forecast(days_to_settlement: int = 1) -> Optional[CommodityForecast]:
    """Fetch gold prices and prepare a forecast for the given settlement horizon."""
    forecast = fetch_gold_prices()
    if forecast:
        forecast.days_to_settlement = days_to_settlement
    return forecast


def fetch_silver_forecast(days_to_settlement: int = 1) -> Optional[CommodityForecast]:
    """Fetch silver prices and prepare a forecast for the given settlement horizon."""
    forecast = fetch_silver_prices()
    if forecast:
        forecast.days_to_settlement = days_to_settlement
    return forecast
