"""
oil_scanner.py
--------------
Fetches WTI crude oil price data from Yahoo Finance and returns a
CommodityForecast for use by the oil trading engine.

Data source:
  - Yahoo Finance chart API (no API key needed)
  - Ticker: CL=F (WTI Crude Oil Futures)

WTI prices are in $/barrel, typically $60-100 range.
Daily volatility is typically $1-3/barrel.
"""

import logging
from datetime import date
from typing import Optional

import requests

from price_model import CommodityForecast, compute_residual_volatility

logger = logging.getLogger(__name__)

# Yahoo Finance chart endpoint for WTI crude oil futures
_YAHOO_WTI_URL = "https://query1.finance.yahoo.com/v8/finance/chart/CL=F"


def fetch_wti_prices() -> Optional[CommodityForecast]:
    """
    Fetch WTI crude oil prices from Yahoo Finance.
    Returns a CommodityForecast object or None on failure.
    """
    try:
        resp = requests.get(
            _YAHOO_WTI_URL,
            params={"interval": "1d", "range": "1mo"},
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; KalshiOilBot/1.0)",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        # Navigate Yahoo Finance response structure
        result = data.get("chart", {}).get("result", [])
        if not result:
            logger.error("No chart data in Yahoo Finance response")
            return None

        chart = result[0]
        meta = chart.get("meta", {})
        indicators = chart.get("indicators", {}).get("quote", [{}])[0]

        # Current price from meta
        current_price = meta.get("regularMarketPrice")
        if current_price is None:
            logger.error("No regularMarketPrice in Yahoo Finance response")
            return None
        current_price = float(current_price)

        # Historical closes
        closes = indicators.get("close", [])
        # Filter out None values
        closes = [float(c) for c in closes if c is not None]

        if len(closes) < 2:
            logger.error("Insufficient historical data from Yahoo Finance (%d closes)", len(closes))
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
        # Oil vol floor: $0.50/barrel (much larger than gas since prices are ~$70-100)
        price_std = compute_residual_volatility(
            current_price=current_price,
            yesterday_price=yesterday_price,
            week_ago_price=week_ago_price,
            vol_floor=0.50,
        )

        logger.info(
            "WTI oil: current $%.2f | yesterday $%.2f | week ago $%.2f | "
            "month ago $%.2f | daily σ $%.2f",
            current_price, yesterday_price, week_ago_price,
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
            settlement_sigma=0.50,  # Oil uses $0.50 settlement sigma
        )

    except Exception as exc:
        logger.error("Failed to fetch WTI oil prices: %s", exc)
        return None


def fetch_oil_forecast(days_to_settlement: int) -> Optional[CommodityForecast]:
    """
    Fetch WTI oil prices and prepare a forecast for the given settlement horizon.
    """
    forecast = fetch_wti_prices()
    if forecast:
        forecast.days_to_settlement = days_to_settlement
    return forecast
