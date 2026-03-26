"""
price_feed.py — Unified real-time price feed for all commodities.
Fetches live intraday prices matching Kalshi settlement sources.
Caches for 60 seconds to avoid rate limits.
"""
import logging
import time
from typing import Optional
import requests

logger = logging.getLogger(__name__)

_cache: dict = {}
_CACHE_TTL = 60  # seconds


def _get_yahoo_live(ticker: str) -> Optional[float]:
    """Fetch latest price from Yahoo Finance v8 chart API (5-min candles)."""
    cache_key = f"yahoo_{ticker}"
    if cache_key in _cache and time.time() - _cache[cache_key]["ts"] < _CACHE_TTL:
        return _cache[cache_key]["price"]

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"range": "1d", "interval": "5m"}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None
        closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
        # Get last non-None close
        price = None
        for c in reversed(closes):
            if c is not None:
                price = float(c)
                break
        if price:
            _cache[cache_key] = {"price": price, "ts": time.time()}
            logger.debug("Live %s: $%.2f", ticker, price)
        return price
    except Exception as e:
        logger.warning("Yahoo live fetch failed for %s: %s", ticker, e)
        # Return cached value if available (even if stale)
        if cache_key in _cache:
            return _cache[cache_key]["price"]
        return None


def get_gold_spot() -> Optional[float]:
    """Get current gold spot price (XAU/USD). Returns None on failure."""
    return _get_yahoo_live("GC=F")


def get_silver_spot() -> Optional[float]:
    """Get current silver spot price (XAG/USD). Returns None on failure."""
    return _get_yahoo_live("SI=F")


def get_oil_spot() -> Optional[float]:
    """Get current WTI crude spot price. Returns None on failure."""
    return _get_yahoo_live("CL=F")


def clear_cache():
    """Clear all cached prices (for testing)."""
    _cache.clear()
