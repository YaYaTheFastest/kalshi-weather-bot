"""
oil_markets.py
--------------
Fetches and parses Kalshi WTI crude oil markets (KXWTI daily, KXWTIW weekly).

Market structure:
  - KXWTI: Daily WTI crude oil prices
    Ticker format: KXWTI-26MAR25-98.50  (above $98.50)

  - KXWTIW: Weekly WTI crude oil prices
    Ticker format: KXWTIW-26MAR28-95.00  (above $95.00)

All markets are directional: "Will WTI crude be ABOVE $XX.XX?"
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from kalshi_client import _fetch_markets_by_series

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class OilMarket:
    """Represents a single Kalshi WTI oil market."""
    ticker: str              # e.g. KXWTI-26MAR25-98.50
    event_ticker: str        # e.g. KXWTI-26MAR25
    title: str
    status: str
    yes_ask: float           # Cost to buy YES (dollars)
    yes_bid: float           # What you sell YES for (dollars)
    strike_price: float      # The oil price strike (e.g. 98.50)
    market_type: str         # "daily" or "weekly"
    settlement_date: Optional[date] = None
    days_to_settlement: int = 0


# ---------------------------------------------------------------------------
# Ticker parsing
# ---------------------------------------------------------------------------

def _parse_oil_ticker(ticker: str) -> dict:
    """
    Parse a WTI oil market ticker.

    Examples:
      KXWTI-26MAR25-98.50   -> daily, settles 2026-03-25, strike $98.50
      KXWTIW-26MAR28-95.00  -> weekly, settles 2026-03-28, strike $95.00
    """
    result = {
        "market_type": None,
        "settlement_date": None,
        "strike_price": None,
    }

    ticker_upper = ticker.upper()

    # Determine market type (check weekly first — KXWTIW contains KXWTI)
    if "KXWTIW" in ticker_upper:
        result["market_type"] = "weekly"
    elif "KXWTI" in ticker_upper:
        result["market_type"] = "daily"
    else:
        return result

    # Split on hyphens
    parts = ticker.split("-")
    if len(parts) < 3:
        return result

    # Parse date from second part (e.g. "26MAR25")
    try:
        date_str = parts[1]
        result["settlement_date"] = datetime.strptime(date_str, "%y%b%d").date()
    except (ValueError, IndexError):
        pass

    # Parse strike price from third part (e.g. "98.50")
    try:
        price_str = "-".join(parts[2:])
        result["strike_price"] = float(price_str)
    except (ValueError, IndexError):
        pass

    return result


# ---------------------------------------------------------------------------
# Market fetcher
# ---------------------------------------------------------------------------

# Oil price series tickers
_OIL_SERIES = ["KXWTI", "KXWTIW"]


def get_oil_markets() -> list[OilMarket]:
    """
    Fetch all open WTI oil markets from Kalshi.
    Uses series_ticker filter for fast server-side filtering.
    """
    all_markets: list[OilMarket] = []
    today = datetime.now(timezone.utc).date()

    for series in _OIL_SERIES:
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
            parsed = _parse_oil_ticker(ticker)
            if parsed["strike_price"] is None or parsed["market_type"] is None:
                continue

            settlement_date = parsed["settlement_date"]
            days_to_settlement = (settlement_date - today).days if settlement_date else 0

            market = OilMarket(
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

    daily = [m for m in all_markets if m.market_type == "daily"]
    weekly = [m for m in all_markets if m.market_type == "weekly"]
    logger.info(
        "Fetched %d oil markets (%d daily, %d weekly)",
        len(all_markets), len(daily), len(weekly),
    )
    return all_markets
