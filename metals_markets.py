"""
metals_markets.py
-----------------
Fetches and parses Kalshi gold and silver markets.

Market structure:
  - KXGOLDD:     Daily gold prices
  - KXGOLDW:     Weekly gold prices
  - KXGOLDMON:   Monthly gold prices
  - KXSILVERD:   Daily silver prices
  - KXSILVERW:   Weekly silver prices
  - KXSILVERMON: Monthly silver prices

Ticker format examples:
  KXGOLDD-26MAR26-4430     -> daily gold, strike $4430
  KXGOLDW-26MAR28-4450     -> weekly gold, strike $4450
  KXGOLDMON-26MAR31-4400   -> monthly gold, strike $4400
  KXSILVERD-26MAR26-33.25  -> daily silver, strike $33.25
  KXSILVERW-26MAR28-33.50  -> weekly silver, strike $33.50
  KXSILVERMON-26MAR31-34.00 -> monthly silver, strike $34.00

All markets are directional: "Will gold/silver be ABOVE $XX?"

Gold strikes are whole dollars (e.g., 4430, 4440).
Silver strikes use decimals in 25¢ increments (e.g., 33.25, 33.50).
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
class MetalsMarket:
    """Represents a single Kalshi gold or silver market."""
    ticker: str              # e.g. KXGOLDD-26MAR26-4430
    event_ticker: str        # e.g. KXGOLDD-26MAR26
    title: str
    status: str
    yes_ask: float           # Cost to buy YES (dollars)
    yes_bid: float           # What you sell YES for (dollars)
    strike_price: float      # e.g. 4430.0 for gold, 33.25 for silver
    market_type: str         # "daily", "weekly", or "monthly"
    metal: str               # "gold" or "silver"
    settlement_date: Optional[date] = None
    days_to_settlement: int = 0


# ---------------------------------------------------------------------------
# Ticker parsing
# ---------------------------------------------------------------------------

# Map series prefix to (metal, market_type)
# Order matters: check longer prefixes first to avoid mismatches
_SERIES_MAP = [
    ("KXGOLDMON", "gold", "monthly"),
    ("KXGOLDW", "gold", "weekly"),
    ("KXGOLDD", "gold", "daily"),
    ("KXSILVERMON", "silver", "monthly"),
    ("KXSILVERW", "silver", "weekly"),
    ("KXSILVERD", "silver", "daily"),
]


def _parse_metals_ticker(ticker: str) -> dict:
    """
    Parse a gold or silver market ticker.

    Examples:
      KXGOLDD-26MAR26-4430      -> gold, daily, settles 2026-03-26, strike $4430
      KXGOLDW-26MAR28-4450      -> gold, weekly, settles 2026-03-28, strike $4450
      KXGOLDMON-26MAR31-4400    -> gold, monthly, settles 2026-03-31, strike $4400
      KXSILVERD-26MAR26-33.25   -> silver, daily, settles 2026-03-26, strike $33.25
      KXSILVERW-26MAR28-T33.50  -> silver, weekly, settles 2026-03-28, strike $33.50
    """
    result = {
        "metal": None,
        "market_type": None,
        "settlement_date": None,
        "strike_price": None,
    }

    ticker_upper = ticker.upper()

    # Determine metal and market type
    for prefix, metal, mtype in _SERIES_MAP:
        if ticker_upper.startswith(prefix):
            result["metal"] = metal
            result["market_type"] = mtype
            break

    if result["metal"] is None:
        return result

    # Split on hyphens
    parts = ticker.split("-")
    if len(parts) < 3:
        return result

    # Parse date from second part (e.g. "26MAR26")
    try:
        date_str = parts[1]
        result["settlement_date"] = datetime.strptime(date_str, "%y%b%d").date()
    except (ValueError, IndexError):
        pass

    # Parse strike price from third part onwards
    # May have a "T" prefix like oil tickers (e.g. "T4430" or "T33.25")
    try:
        price_str = "-".join(parts[2:])
        if price_str.upper().startswith("T"):
            price_str = price_str[1:]
        result["strike_price"] = float(price_str)
    except (ValueError, IndexError):
        pass

    return result


# ---------------------------------------------------------------------------
# Market fetcher
# ---------------------------------------------------------------------------

# Gold and silver series tickers
# Weekly gold/silver DISABLED — simulation showed 18%/14% win rates.
# Daily and monthly are profitable (100% and 100% win rates).
_GOLD_SERIES = ["KXGOLDD", "KXGOLDMON"]  # KXGOLDW disabled
_SILVER_SERIES = ["KXSILVERD", "KXSILVERMON"]  # KXSILVERW disabled


def get_gold_markets() -> list[MetalsMarket]:
    """Fetch all open gold markets from Kalshi."""
    return _fetch_metals_markets(_GOLD_SERIES, "gold")


def get_silver_markets() -> list[MetalsMarket]:
    """Fetch all open silver markets from Kalshi."""
    return _fetch_metals_markets(_SILVER_SERIES, "silver")


def get_all_metals_markets() -> list[MetalsMarket]:
    """Fetch all open gold and silver markets from Kalshi."""
    gold = get_gold_markets()
    silver = get_silver_markets()
    return gold + silver


def _fetch_metals_markets(series_list: list[str], metal: str) -> list[MetalsMarket]:
    """
    Fetch and parse metals markets for the given series tickers.
    Uses series_ticker filter for fast server-side filtering.
    """
    all_markets: list[MetalsMarket] = []
    today = datetime.now(timezone.utc).date()

    for series in series_list:
        markets_raw = _fetch_markets_by_series(series)
        for m in markets_raw:
            ticker = m.get("ticker", "")
            event_ticker = m.get("event_ticker", "")

            # Parse prices — API returns yes_ask_dollars/yes_bid_dollars (strings)
            yes_ask = float(m.get("yes_ask_dollars") or m.get("yes_ask") or "0")
            yes_bid = float(m.get("yes_bid_dollars") or m.get("yes_bid") or "0")

            # Parse ticker details
            parsed = _parse_metals_ticker(ticker)
            if parsed["strike_price"] is None or parsed["market_type"] is None:
                continue

            settlement_date = parsed["settlement_date"]
            days_to_settlement = (settlement_date - today).days if settlement_date else 0

            market = MetalsMarket(
                ticker=ticker,
                event_ticker=event_ticker,
                title=m.get("title", ""),
                status=m.get("status", ""),
                yes_ask=yes_ask,
                yes_bid=yes_bid,
                strike_price=parsed["strike_price"],
                market_type=parsed["market_type"],
                metal=parsed["metal"] or metal,
                settlement_date=settlement_date,
                days_to_settlement=max(0, days_to_settlement),
            )
            all_markets.append(market)

    daily = [m for m in all_markets if m.market_type == "daily"]
    weekly = [m for m in all_markets if m.market_type == "weekly"]
    monthly = [m for m in all_markets if m.market_type == "monthly"]
    logger.info(
        "Fetched %d %s markets (%d daily, %d weekly, %d monthly)",
        len(all_markets), metal, len(daily), len(weekly), len(monthly),
    )
    return all_markets
