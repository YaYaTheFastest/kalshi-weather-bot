"""
gas_markets.py
--------------
Fetches and parses Kalshi gas price markets (KXAAAGASW weekly, KXAAAGASM monthly).

Market structure:
  - KXAAAGASW: Weekly US gas prices, settles on Sunday based on AAA data
    Ticker format: KXAAAGASW-26MAR30-4.060  (above $4.060)
    Buckets: $0.02 increments

  - KXAAAGASM: Monthly US gas prices, settles on last day of month
    Ticker format: KXAAAGASM-26MAR31-4.10  (above $4.10)
    Buckets: $0.05-$0.10 increments

All markets are directional: "Will gas price be ABOVE $X.XX?"
Settlement source: AAA (gasprices.aaa.com) national regular average.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class GasMarket:
    """Represents a single Kalshi gas price market."""
    ticker: str              # e.g. KXAAAGASW-26MAR30-4.060
    event_ticker: str        # e.g. KXAAAGASW-26MAR30
    title: str
    status: str
    yes_ask: float           # Cost to buy YES (dollars)
    yes_bid: float           # What you sell YES for (dollars)
    strike_price: float      # The gas price strike (e.g. 4.060)
    market_type: str         # "weekly" or "monthly"
    settlement_date: Optional[date] = None  # When this market settles
    days_to_settlement: int = 0


# ---------------------------------------------------------------------------
# Ticker parsing
# ---------------------------------------------------------------------------

def _parse_gas_ticker(ticker: str) -> dict:
    """
    Parse a gas price market ticker.

    Examples:
      KXAAAGASW-26MAR30-4.060  -> weekly, settles 2026-03-30, strike $4.060
      KXAAAGASM-26MAR31-4.10   -> monthly, settles 2026-03-31, strike $4.10
    """
    result = {
        "market_type": None,
        "settlement_date": None,
        "strike_price": None,
    }

    ticker_upper = ticker.upper()

    # Determine market type
    if "KXAAAGASW" in ticker_upper:
        result["market_type"] = "weekly"
    elif "KXAAAGASM" in ticker_upper:
        result["market_type"] = "monthly"
    else:
        return result

    # Split on hyphens
    parts = ticker.split("-")
    if len(parts) < 3:
        return result

    # Parse date from second part (e.g. "26MAR30")
    try:
        date_str = parts[1]
        result["settlement_date"] = datetime.strptime(date_str, "%y%b%d").date()
    except (ValueError, IndexError):
        pass

    # Parse strike price from third part (e.g. "4.060" or "4.10")
    try:
        # Rejoin remaining parts in case the price has hyphens (unlikely but safe)
        price_str = "-".join(parts[2:])
        result["strike_price"] = float(price_str)
    except (ValueError, IndexError):
        pass

    return result


# ---------------------------------------------------------------------------
# Market fetcher
# ---------------------------------------------------------------------------

# Reuse helpers from kalshi_client
from kalshi_client import _get, _fetch_markets_by_series


# Gas price series tickers
_GAS_SERIES = ["KXAAAGASW", "KXAAAGASM"]


def get_gas_markets() -> list[GasMarket]:
    """
    Fetch all open gas price markets from Kalshi.
    Uses series_ticker filter for fast server-side filtering.
    """
    all_markets: list[GasMarket] = []
    today = datetime.now(timezone.utc).date()

    for series in _GAS_SERIES:
        markets_raw = _fetch_markets_by_series(series)
        for m in markets_raw:
            ticker = m.get("ticker", "")
            event_ticker = m.get("event_ticker", "")

            # Parse prices
            yes_ask = float(m.get("yes_ask", "0") or "0")
            yes_bid = float(m.get("yes_bid", "0") or "0")
            if yes_ask == 0:
                yes_ask = float(m.get("yes_ask_cost", "0") or "0")

            # Parse ticker details
            parsed = _parse_gas_ticker(ticker)
            if parsed["strike_price"] is None or parsed["market_type"] is None:
                continue

            settlement_date = parsed["settlement_date"]
            days_to_settlement = (settlement_date - today).days if settlement_date else 0

            market = GasMarket(
                ticker=ticker,
                event_ticker=event_ticker,
                title=m.get("title", ""),
                status=m.get("status", ""),
                yes_ask=yes_ask,
                yes_bid=yes_bid,
                strike_price=parsed["strike_price"],
                market_type=parsed["market_type"],
                settlement_date=settlement_date,
                days_to_settlement=max(0, days_to_settlement),
            )
            all_markets.append(market)

    weekly = [m for m in all_markets if m.market_type == "weekly"]
    monthly = [m for m in all_markets if m.market_type == "monthly"]
    logger.info(
        "Fetched %d gas markets (%d weekly, %d monthly)",
        len(all_markets), len(weekly), len(monthly),
    )
    return all_markets
