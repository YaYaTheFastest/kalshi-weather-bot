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


def _get_fred_price(series_id: str) -> Optional[float]:
    """Fallback: fetch latest price from FRED API (Federal Reserve)."""
    cache_key = f"fred_{series_id}"
    if cache_key in _cache and time.time() - _cache[cache_key]["ts"] < 3600:  # 1hr cache for FRED
        return _cache[cache_key]["price"]
    try:
        url = f"https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": series_id,
            "api_key": "DEMO_KEY",  # FRED allows limited anonymous access
            "file_type": "json",
            "sort_order": "desc",
            "limit": 5,
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        obs = r.json().get("observations", [])
        for o in obs:
            val = o.get("value", ".")
            if val != ".":
                price = float(val)
                _cache[cache_key] = {"price": price, "ts": time.time()}
                logger.debug("FRED %s: $%.2f", series_id, price)
                return price
    except Exception as e:
        logger.debug("FRED fallback failed for %s: %s", series_id, e)
    return None


def get_gold_spot() -> Optional[float]:
    """Get current gold spot price. Primary: Yahoo. Fallback: FRED."""
    price = _get_yahoo_live("GC=F")
    if price is None:
        logger.warning("Yahoo gold failed, trying FRED fallback")
        price = _get_fred_price("GOLDAMGBD228NLBM")  # London gold fixing
    return price


def get_silver_spot() -> Optional[float]:
    """Get current silver spot price. Primary: Yahoo. Fallback: FRED."""
    price = _get_yahoo_live("SI=F")
    if price is None:
        logger.warning("Yahoo silver failed, trying FRED fallback")
        price = _get_fred_price("SLVPRUSD")  # Silver fixing
    return price


def get_oil_spot() -> Optional[float]:
    """Get current WTI crude spot. Primary: Yahoo. Fallback: FRED."""
    price = _get_yahoo_live("CL=F")
    if price is None:
        logger.warning("Yahoo oil failed, trying FRED fallback")
        price = _get_fred_price("DCOILWTICO")  # WTI daily
    return price


def clear_cache():
    """Clear all cached prices (for testing)."""
    _cache.clear()
