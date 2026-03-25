"""
kalshi_client.py
----------------
Handles all communication with the Kalshi Trade API v2.

Authentication uses RSA PKCS1v15 + SHA-256 signatures:
  - KALSHI-ACCESS-KEY: your API key UUID
  - KALSHI-ACCESS-TIMESTAMP: Unix milliseconds as string
  - KALSHI-ACCESS-SIGNATURE: base64(RSA-sign(timestamp_ms + METHOD + path))

Market data fetched:
  - GET /markets?status=open  -> filter for KXHIGH temperature markets
  - GET /portfolio/balance    -> current dollar balance
  - GET /portfolio/positions  -> open positions

Order placement:
  - POST /portfolio/orders    -> place a limit order (YES side)
  - DELETE /portfolio/orders/{order_id} -> cancel order (used for exits)
"""

import base64
import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta, timezone, datetime
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
class KalshiMarket:
    """Represents a single Kalshi market for a temperature bucket."""
    ticker: str              # e.g. KXHIGHNYC-25MAR24-T72
    event_ticker: str        # e.g. KXHIGHNYC-25MAR24
    title: str
    status: str              # "open", "closed", etc.
    yes_ask: float           # cost to buy YES (dollars)
    yes_bid: float           # what you sell YES for (dollars)
    # Parsed temperature bucket (may be None if parsing fails)
    bucket_low: Optional[float] = None
    bucket_high: Optional[float] = None
    city_key: Optional[str] = None
    market_date: Optional[date] = None  # parsed from ticker


@dataclass
class KalshiPosition:
    """An open position we hold."""
    ticker: str
    market_exposure: int     # contracts held (from position_fp)
    realized_pnl: float      # realized P&L in dollars
    total_traded: float      # total traded in dollars
    market_exposure_dollars: float  # market exposure in dollars
    fees_paid: float = 0.0   # total fees paid on this position in dollars


@dataclass
class OrderResult:
    """Result of an order placement attempt."""
    success: bool
    order_id: Optional[str] = None
    error: Optional[str] = None
    filled_price: Optional[float] = None
    filled_count: Optional[int] = None


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _create_signature(timestamp_ms: int, method: str, path: str) -> str:
    """
    Sign the request using the configured RSA private key.
    Message format: <timestamp_ms><METHOD><path>  (no spaces)
    """
    pem = config.KALSHI_PRIVATE_KEY_PEM
    if not pem:
        raise ValueError(
            "KALSHI_PRIVATE_KEY_PEM is not set. "
            "Provide your RSA private key in the .env file."
        )
    private_key = serialization.load_pem_private_key(pem.encode(), password=None)
    # Strip query parameters before signing
    path_without_query = path.split('?')[0]
    message = f"{timestamp_ms}{method}{path_without_query}".encode('utf-8')
    # Kalshi requires RSA-PSS (not PKCS1v15)
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode('utf-8')


def _auth_headers(method: str, path: str) -> dict:
    """Build Kalshi authentication headers for a request."""
    timestamp_ms = int(time.time() * 1000)
    # path must be just the path portion, e.g. /trade-api/v2/markets
    # Strip base URL prefix if accidentally included
    if path.startswith("http"):
        path = "/" + "/".join(path.split("/")[3:])

    signature = _create_signature(timestamp_ms, method.upper(), path)
    return {
        "KALSHI-ACCESS-KEY": config.KALSHI_ACCESS_KEY,
        "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        "KALSHI-ACCESS-SIGNATURE": signature,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

def _get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    """Authenticated GET request to Kalshi API. Returns parsed JSON or None."""
    full_url = f"{config.KALSHI_BASE_URL}{path}"
    headers = _auth_headers("GET", f"/trade-api/v2{path}")
    try:
        resp = requests.get(full_url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as exc:
        logger.error("Kalshi GET %s HTTP error %s: %s", path, exc.response.status_code, exc.response.text)
        return None
    except Exception as exc:
        logger.error("Kalshi GET %s error: %s", path, exc)
        return None


def _post(path: str, body: dict) -> Optional[dict]:
    """Authenticated POST request to Kalshi API. Returns parsed JSON or None."""
    import json
    full_url = f"{config.KALSHI_BASE_URL}{path}"
    headers = _auth_headers("POST", f"/trade-api/v2{path}")
    try:
        resp = requests.post(full_url, headers=headers, json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as exc:
        error_text = exc.response.text if exc.response else str(exc)
        logger.error("Kalshi POST %s HTTP error %s: %s", path, exc.response.status_code, error_text)
        # Return error dict so callers can inspect the reason
        return {"_error": error_text, "_status": exc.response.status_code}
    except Exception as exc:
        logger.error("Kalshi POST %s error: %s", path, exc)
        return {"_error": str(exc)}


def _delete(path: str) -> bool:
    """Authenticated DELETE request. Returns True on success."""
    full_url = f"{config.KALSHI_BASE_URL}{path}"
    headers = _auth_headers("DELETE", f"/trade-api/v2{path}")
    try:
        resp = requests.delete(full_url, headers=headers, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.error("Kalshi DELETE %s error: %s", path, exc)
        return False


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def _parse_bucket_from_ticker(ticker: str, city_suffix: str) -> tuple[Optional[float], Optional[float]]:
    """
    Attempt to extract the temperature bucket from a Kalshi ticker.

    Observed ticker formats:
      KXHIGHNYC-25MAR24-T72          -> high >= 72°F
      KXHIGHNYC-25MAR24-B65T72       -> 65 <= high < 72°F
      KXHIGHNYC-25MAR24-T65B         -> high < 65°F  (below)

    Returns (bucket_low, bucket_high) in °F, using ±inf for open bounds.
    Returns (None, None) if parsing fails.
    """
    try:
        # Strip the event part: after last hyphen
        parts = ticker.split("-")
        if len(parts) < 3:
            return None, None
        bucket_str = parts[-1].upper()

        # Format: T<N>  -> high >= N (i.e., [N, +inf))
        if bucket_str.startswith("T") and "B" not in bucket_str:
            low = float(bucket_str[1:])
            return low, float("inf")

        # Format: T<N>B -> high < N (i.e., (-inf, N))
        if bucket_str.startswith("T") and bucket_str.endswith("B"):
            high = float(bucket_str[1:-1])
            return float("-inf"), high

        # Format: B<LOW>T<HIGH> -> [LOW, HIGH)
        if bucket_str.startswith("B") and "T" in bucket_str:
            b_idx = bucket_str.index("T")
            low = float(bucket_str[1:b_idx])
            high = float(bucket_str[b_idx + 1:])
            return low, high

        # Format: B<N> -> high < N (below N)
        if bucket_str.startswith("B"):
            high = float(bucket_str[1:])
            return float("-inf"), high

        return None, None
    except Exception:
        return None, None


def _identify_city_from_ticker(ticker: str) -> Optional[str]:
    """Return the city_key whose kalshi_suffix appears in the ticker."""
    ticker_upper = ticker.upper()
    for city_key, city_cfg in config.CITIES.items():
        suffix = city_cfg["kalshi_suffix"].upper()
        if f"KXHIGH{suffix}" in ticker_upper:
            return city_key
    return None


# All KXHIGH series tickers — one per city
_TEMP_SERIES = [
    f"KXHIGH{city['kalshi_suffix']}" for city in config.CITIES.values()
]

# WTI crude oil series tickers
_OIL_SERIES = ["KXWTI", "KXWTIW"]


def _fetch_markets_by_series(series_ticker: str) -> list[dict]:
    """Fetch all open markets for a single series ticker. Much faster than scanning all markets."""
    results = []
    cursor = None
    while True:
        params: dict = {"status": "open", "limit": 1000, "series_ticker": series_ticker}
        if cursor:
            params["cursor"] = cursor
        data = _get("/markets", params=params)
        if not data:
            break
        batch = data.get("markets", [])
        results.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return results


def get_temperature_markets(target_date: Optional[date] = None) -> list[KalshiMarket]:
    """
    Fetch all open KXHIGH (temperature high) markets from Kalshi.
    Uses series_ticker filter for fast server-side filtering.

    Returns a list of KalshiMarket objects with parsed bucket info.
    """
    if target_date is None:
        target_date = (datetime.now(timezone.utc) + timedelta(days=1)).date()

    all_markets: list[KalshiMarket] = []

    for series in _TEMP_SERIES:
        markets_raw = _fetch_markets_by_series(series)
        for m in markets_raw:
            ticker = m.get("ticker", "")
            event_ticker = m.get("event_ticker", "")

            # Parse prices — API returns yes_ask_dollars/yes_bid_dollars (strings)
            yes_ask = float(m.get("yes_ask_dollars") or m.get("yes_ask") or "0")
            yes_bid = float(m.get("yes_bid_dollars") or m.get("yes_bid") or "0")

            city_key = _identify_city_from_ticker(ticker)
            bucket_low, bucket_high = _parse_bucket_from_ticker(ticker, "")

            # Parse market date from ticker
            market_date = None
            try:
                parts = ticker.split("-")
                if len(parts) >= 2:
                    date_str = parts[1]
                    market_date = datetime.strptime(date_str, "%y%b%d").date()
            except (ValueError, IndexError):
                pass

            market = KalshiMarket(
                ticker=ticker,
                event_ticker=event_ticker,
                title=m.get("title", ""),
                status=m.get("status", ""),
                yes_ask=yes_ask,
                yes_bid=yes_bid,
                bucket_low=bucket_low,
                bucket_high=bucket_high,
                city_key=city_key,
                market_date=market_date,
            )
            all_markets.append(market)

    logger.info("Fetched %d open KXHIGH markets from Kalshi (%d series)", len(all_markets), len(_TEMP_SERIES))
    return all_markets


def get_markets_for_city(city_key: str, target_date: Optional[date] = None) -> list[KalshiMarket]:
    """Return open temperature markets for a specific city."""
    all_markets = get_temperature_markets(target_date)
    return [m for m in all_markets if m.city_key == city_key]


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

def get_balance() -> float:
    """Return the current portfolio balance in dollars. Returns 0.0 on error."""
    data = _get("/portfolio/balance")
    if not data:
        return 0.0
    # Kalshi API returns balance in cents as an integer
    balance = data.get("balance", 0)
    logger.info("Raw balance from API: %s (type: %s)", balance, type(balance).__name__)
    # Always convert from cents to dollars
    return float(balance) / 100.0


def get_positions() -> list[KalshiPosition]:
    """Fetch all current open positions. Returns list of KalshiPosition.
    
    Uses correct Kalshi API v2 field names:
      - position_fp: number of contracts (fractional, but we cast to int)
      - market_exposure_dollars: exposure in dollars (string)
      - total_traded_dollars: total traded value in dollars (string)
      - realized_pnl_dollars: realized P&L in dollars (string)
    """
    data = _get("/portfolio/positions", params={"settlement_status": "unsettled"})
    if not data:
        return []

    positions: list[KalshiPosition] = []
    for p in data.get("market_positions", []):
        ticker = p.get("ticker", "")
        # position_fp is the contract count (API returns as string like '6.00')
        position_fp = p.get("position_fp", 0)
        exposure = int(float(position_fp)) if position_fp else 0
        if exposure == 0:
            continue  # skip flat positions
        
        # Dollar fields come as strings from the API
        realized_pnl = float(p.get("realized_pnl_dollars", 0) or 0)
        total_traded = float(p.get("total_traded_dollars", 0) or 0)
        market_exp_dollars = float(p.get("market_exposure_dollars", 0) or 0)
        fees_paid = float(p.get("fees_paid_dollars", 0) or 0)
        
        positions.append(
            KalshiPosition(
                ticker=ticker,
                market_exposure=exposure,
                realized_pnl=realized_pnl,
                total_traded=total_traded,
                market_exposure_dollars=market_exp_dollars,
                fees_paid=fees_paid,
            )
        )
    logger.info("Open positions: %d", len(positions))
    return positions


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def place_buy_order(
    ticker: str,
    yes_price_cents: int,
    count: int,
) -> OrderResult:
    """
    Place a limit buy order for YES contracts.

    Args:
        ticker:           Market ticker string
        yes_price_cents:  Price in cents (1-99), e.g. 14 for $0.14
        count:            Number of contracts to buy

    Returns:
        OrderResult with success flag, order_id, and fill info.
    """
    if config.DRY_RUN:
        logger.info(
            "[DRY RUN] Would BUY %d contracts of %s at %d¢",
            count, ticker, yes_price_cents,
        )
        return OrderResult(
            success=True,
            order_id="DRY_RUN",
            filled_price=yes_price_cents / 100.0,
            filled_count=count,
        )

    body = {
        "ticker": ticker,
        "action": "buy",
        "side": "yes",
        "type": "limit",
        "yes_price": yes_price_cents,
        "count": count,
    }
    result = _post("/portfolio/orders", body)
    if result and result.get("order"):
        order = result["order"]
        status = order.get("status", "")
        if status in ("canceled", "cancelled"):
            logger.warning("Buy order for %s was immediately cancelled: %s", ticker, order)
            return OrderResult(success=False, error=f"Order cancelled: {status}")
        return OrderResult(
            success=True,
            order_id=order.get("order_id"),
            filled_price=float(order.get("yes_price", yes_price_cents)) / 100.0,
            filled_count=int(order.get("count", count)),
        )
    error_msg = result.get("_error", str(result)) if isinstance(result, dict) else str(result)
    return OrderResult(success=False, error=error_msg)


def place_sell_order(
    ticker: str,
    yes_price_cents: int,
    count: int,
) -> OrderResult:
    """
    Place a limit sell order to exit a YES position.

    Args:
        ticker:           Market ticker string
        yes_price_cents:  Minimum price to accept in cents (1-99)
        count:            Number of contracts to sell
    """
    if config.DRY_RUN:
        logger.info(
            "[DRY RUN] Would SELL %d contracts of %s at %d¢",
            count, ticker, yes_price_cents,
        )
        return OrderResult(
            success=True,
            order_id="DRY_RUN",
            filled_price=yes_price_cents / 100.0,
            filled_count=count,
        )

    body = {
        "ticker": ticker,
        "action": "sell",
        "side": "yes",
        "type": "limit",
        "yes_price": yes_price_cents,
        "count": count,
    }
    result = _post("/portfolio/orders", body)
    if result and result.get("order"):
        order = result["order"]
        status = order.get("status", "")
        # Only report success if the order is actually active or filled — not if
        # Kalshi immediately cancelled it (e.g. insufficient funds).
        if status in ("canceled", "cancelled"):
            logger.warning("Sell order for %s was immediately cancelled: %s", ticker, order)
            return OrderResult(success=False, error=f"Order cancelled: {status}")
        return OrderResult(
            success=True,
            order_id=order.get("order_id"),
            filled_price=float(order.get("yes_price", yes_price_cents)) / 100.0,
            filled_count=int(order.get("count", count)),
        )
    error_msg = result.get("_error", str(result)) if isinstance(result, dict) else str(result)
    return OrderResult(success=False, error=error_msg)


def cancel_order(order_id: str) -> bool:
    """Cancel an existing open order by ID."""
    if config.DRY_RUN:
        logger.info("[DRY RUN] Would cancel order %s", order_id)
        return True
    return _delete(f"/portfolio/orders/{order_id}")
