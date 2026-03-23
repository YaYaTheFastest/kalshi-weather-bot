#!/usr/bin/env bash
# =============================================================
# deploy.sh — Kalshi Weather Bot single-paste deployment script
#
# Paste this entire file into your server terminal.
# It will create all bot files in ~/kalshi-weather-bot/
#
# Usage:
#   bash deploy.sh
# =============================================================

set -euo pipefail

BOT_DIR="$HOME/kalshi-weather-bot"
mkdir -p "$BOT_DIR"
cd "$BOT_DIR"
echo "Working in: $BOT_DIR"

# =============================================================
# requirements.txt
# =============================================================
cat > requirements.txt << 'HEREDOC'
requests>=2.31.0
python-dotenv>=1.0.0
cryptography>=41.0.0
HEREDOC

# =============================================================
# .env.template
# =============================================================
cat > .env.template << 'HEREDOC'
# ============================================================
# Kalshi Weather Bot — Environment Variable Template
# Copy this file to .env and fill in your values:
#   cp .env.template .env
#   nano .env
# ============================================================

# ---- Bot Mode -----------------------------------------------
DRY_RUN=true

# ---- Kalshi API ---------------------------------------------
KALSHI_ACCESS_KEY=your-kalshi-api-key-uuid-here
KALSHI_PRIVATE_KEY_PEM=-----BEGIN RSA PRIVATE KEY-----\nYOUR_KEY_CONTENT_HERE\n-----END RSA PRIVATE KEY-----
KALSHI_BASE_URL=https://api.elections.kalshi.com/trade-api/v2

# ---- Telegram -----------------------------------------------
TELEGRAM_BOT_TOKEN=8701485015:AAH_GUm0x7s4gZIH3tRx1ahFKCpPmfN_2xw
TELEGRAM_CHAT_ID=8718921224

# ---- NOAA ---------------------------------------------------
NOAA_USER_AGENT=KalshiWeatherBot/1.0 (your-email@example.com)

# ---- Trading Parameters -------------------------------------
BUY_CONFIDENCE_THRESHOLD=0.85
BUY_MAX_PRICE=0.15
SELL_MIN_PRICE=0.45

# ---- Risk Limits --------------------------------------------
MAX_POSITION_USD=2.00
MAX_OPEN_POSITIONS=5
MAX_DAILY_LOSS_USD=50.00

# ---- Scan Settings ------------------------------------------
SCAN_INTERVAL_SECONDS=120

# ---- Logging ------------------------------------------------
LOG_LEVEL=INFO
LOG_FILE=kalshi_weather_bot.log
HEREDOC

# =============================================================
# config.py
# =============================================================
cat > config.py << 'HEREDOC'
"""
config.py
---------
Central configuration for the Kalshi Weather Trading Bot.
All constants, city coordinates, Kalshi ticker mappings, and trading
parameters live here. Override via environment variables in .env.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Kalshi API
# ---------------------------------------------------------------------------
KALSHI_BASE_URL: str = os.getenv(
    "KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"
)
KALSHI_ACCESS_KEY: str = os.getenv("KALSHI_ACCESS_KEY", "")
KALSHI_PRIVATE_KEY_PEM: str = os.getenv("KALSHI_PRIVATE_KEY_PEM", "").replace(
    "\\n", "\n"
)

# ---------------------------------------------------------------------------
# NOAA API
# ---------------------------------------------------------------------------
NOAA_BASE_URL: str = "https://api.weather.gov"
NOAA_USER_AGENT: str = os.getenv(
    "NOAA_USER_AGENT", "KalshiWeatherBot/1.0 (contact@example.com)"
)

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN: str = os.getenv(
    "TELEGRAM_BOT_TOKEN", "8701485015:AAH_GUm0x7s4gZIH3tRx1ahFKCpPmfN_2xw"
)
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "8718921224")

# ---------------------------------------------------------------------------
# Trading parameters
# ---------------------------------------------------------------------------
BUY_CONFIDENCE_THRESHOLD: float = float(
    os.getenv("BUY_CONFIDENCE_THRESHOLD", "0.85")
)
BUY_MAX_PRICE: float = float(os.getenv("BUY_MAX_PRICE", "0.15"))
SELL_MIN_PRICE: float = float(os.getenv("SELL_MIN_PRICE", "0.45"))

# Risk limits
MAX_POSITION_USD: float = float(os.getenv("MAX_POSITION_USD", "2.00"))
MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
MAX_DAILY_LOSS_USD: float = float(os.getenv("MAX_DAILY_LOSS_USD", "50.00"))

# Scan interval
SCAN_INTERVAL_SECONDS: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "120"))

# ---------------------------------------------------------------------------
# Target cities
# ---------------------------------------------------------------------------
CITIES: dict = {
    "NYC": {
        "name": "New York City",
        "lat": 40.7128,
        "lon": -74.0060,
        "kalshi_suffix": "NYC",
    },
    "CHI": {
        "name": "Chicago",
        "lat": 41.8781,
        "lon": -87.6298,
        "kalshi_suffix": "CHI",
    },
    "LA": {
        "name": "Los Angeles",
        "lat": 34.0522,
        "lon": -118.2437,
        "kalshi_suffix": "LA",
    },
    "MIA": {
        "name": "Miami",
        "lat": 25.7617,
        "lon": -80.1918,
        "kalshi_suffix": "MIA",
    },
    "AUS": {
        "name": "Austin",
        "lat": 30.2672,
        "lon": -97.7431,
        "kalshi_suffix": "AUS",
    },
    "BOS": {
        "name": "Boston",
        "lat": 42.3601,
        "lon": -71.0589,
        "kalshi_suffix": "BOS",
    },
    "HOU": {
        "name": "Houston",
        "lat": 29.7604,
        "lon": -95.3698,
        "kalshi_suffix": "HOU",
    },
    "DEN": {
        "name": "Denver",
        "lat": 39.7392,
        "lon": -104.9903,
        "kalshi_suffix": "DEN",
    },
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: str = os.getenv("LOG_FILE", "kalshi_weather_bot.log")
HEREDOC

# =============================================================
# noaa_scanner.py
# =============================================================
cat > noaa_scanner.py << 'HEREDOC'
"""
noaa_scanner.py
---------------
Fetches NOAA/NWS hourly weather forecasts for configured cities and
estimates the probability (confidence) that tomorrow's high temperature
will fall within a given temperature range (a Kalshi market bucket).

Flow:
  1. GET https://api.weather.gov/points/{lat},{lon}
     -> returns JSON with forecastHourly URL
  2. GET forecastHourly URL
     -> returns hourly periods with temperature + temperatureUnit
  3. Extract all hourly temps for "tomorrow" (next calendar day in UTC)
  4. Use those to estimate a Gaussian confidence that the daily high
     lands within [bucket_low, bucket_high]
"""

import logging
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

_GRID_CACHE: dict = {}


def _get_headers() -> dict:
    return {"User-Agent": config.NOAA_USER_AGENT, "Accept": "application/geo+json"}


def _get_forecast_hourly_url(city_key: str, lat: float, lon: float) -> Optional[str]:
    if city_key in _GRID_CACHE:
        return _GRID_CACHE[city_key]
    url = f"{config.NOAA_BASE_URL}/points/{lat},{lon}"
    try:
        resp = requests.get(url, headers=_get_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        forecast_url = data["properties"]["forecastHourly"]
        _GRID_CACHE[city_key] = forecast_url
        logger.debug("Resolved forecastHourly URL for %s: %s", city_key, forecast_url)
        return forecast_url
    except Exception as exc:
        logger.error("Failed to resolve NOAA grid for %s: %s", city_key, exc)
        return None


def _fetch_hourly_periods(forecast_url: str) -> list:
    try:
        resp = requests.get(forecast_url, headers=_get_headers(), timeout=20)
        resp.raise_for_status()
        return resp.json()["properties"]["periods"]
    except Exception as exc:
        logger.error("Failed to fetch hourly forecast: %s", exc)
        return []


def _to_fahrenheit(temp: float, unit: str) -> float:
    if unit.upper() in ("C", "CELSIUS"):
        return temp * 9 / 5 + 32
    return temp


def _get_tomorrow_date() -> date:
    return (datetime.now(timezone.utc) + timedelta(days=1)).date()


class NOAAForecast:
    def __init__(self, city_key: str, tomorrow_temps_f: list):
        self.city_key = city_key
        self.tomorrow_temps_f = tomorrow_temps_f
        self.forecasted_high: float = max(tomorrow_temps_f) if tomorrow_temps_f else float("nan")
        self.forecasted_low: float = min(tomorrow_temps_f) if tomorrow_temps_f else float("nan")
        self.std_dev: float = (
            statistics.stdev(tomorrow_temps_f) if len(tomorrow_temps_f) > 1 else 3.0
        )

    def confidence_in_range(self, bucket_low: float, bucket_high: float) -> float:
        if not self.tomorrow_temps_f or math.isnan(self.forecasted_high):
            return 0.0
        sigma = max(self.std_dev, 2.0)
        mu = self.forecasted_high

        def normal_cdf(x: float) -> float:
            return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))

        prob = normal_cdf(bucket_high) - normal_cdf(bucket_low)
        return max(0.0, min(1.0, prob))

    def __repr__(self) -> str:
        return (
            f"NOAAForecast(city={self.city_key}, "
            f"high={self.forecasted_high:.1f}F, "
            f"low={self.forecasted_low:.1f}F, "
            f"sigma={self.std_dev:.1f}F, "
            f"n_hours={len(self.tomorrow_temps_f)})"
        )


def get_city_forecast(city_key: str) -> Optional[NOAAForecast]:
    city_cfg = config.CITIES.get(city_key)
    if not city_cfg:
        logger.error("Unknown city key: %s", city_key)
        return None
    lat, lon = city_cfg["lat"], city_cfg["lon"]
    forecast_url = _get_forecast_hourly_url(city_key, lat, lon)
    if not forecast_url:
        return None
    periods = _fetch_hourly_periods(forecast_url)
    if not periods:
        return None
    tomorrow = _get_tomorrow_date()
    tomorrow_temps = []
    for period in periods:
        try:
            start_str = period.get("startTime", "")
            start_dt = datetime.fromisoformat(start_str)
            period_date = start_dt.astimezone(timezone.utc).date()
            if period_date == tomorrow:
                raw_temp = period.get("temperature")
                unit = period.get("temperatureUnit", "F")
                if raw_temp is not None:
                    tomorrow_temps.append(_to_fahrenheit(float(raw_temp), unit))
        except Exception as exc:
            logger.debug("Skipping period: %s", exc)
            continue
    if not tomorrow_temps:
        logger.warning("No tomorrow hourly data found for %s", city_key)
        return None
    forecast = NOAAForecast(city_key=city_key, tomorrow_temps_f=tomorrow_temps)
    logger.info("NOAA %s: %s", city_key, forecast)
    return forecast


def get_all_forecasts() -> dict:
    results = {}
    for city_key in config.CITIES:
        results[city_key] = get_city_forecast(city_key)
    return results
HEREDOC

# =============================================================
# kalshi_client.py
# =============================================================
cat > kalshi_client.py << 'HEREDOC'
"""
kalshi_client.py
----------------
Handles all communication with the Kalshi Trade API v2.

Authentication uses RSA PKCS1v15 + SHA-256 signatures.
"""

import base64
import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta, timezone, datetime
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config

logger = logging.getLogger(__name__)


@dataclass
class KalshiMarket:
    ticker: str
    event_ticker: str
    title: str
    status: str
    yes_ask: float
    yes_bid: float
    bucket_low: Optional[float] = None
    bucket_high: Optional[float] = None
    city_key: Optional[str] = None


@dataclass
class KalshiPosition:
    ticker: str
    market_exposure: int
    realized_pnl: float
    unrealized_pnl: float
    position_value: float


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str] = None
    error: Optional[str] = None
    filled_price: Optional[float] = None
    filled_count: Optional[int] = None


def _create_signature(timestamp_ms: int, method: str, path: str) -> str:
    pem = config.KALSHI_PRIVATE_KEY_PEM
    if not pem:
        raise ValueError("KALSHI_PRIVATE_KEY_PEM is not set.")
    private_key = serialization.load_pem_private_key(pem.encode(), password=None)
    message = f"{timestamp_ms}{method}{path}".encode()
    signature = private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode()


def _auth_headers(method: str, path: str) -> dict:
    timestamp_ms = int(time.time() * 1000)
    if path.startswith("http"):
        path = "/" + "/".join(path.split("/")[3:])
    signature = _create_signature(timestamp_ms, method.upper(), path)
    return {
        "KALSHI-ACCESS-KEY": config.KALSHI_ACCESS_KEY,
        "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        "KALSHI-ACCESS-SIGNATURE": signature,
        "Content-Type": "application/json",
    }


def _get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    full_url = f"{config.KALSHI_BASE_URL}{path}"
    headers = _auth_headers("GET", f"/trade-api/v2{path}")
    try:
        resp = requests.get(full_url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as exc:
        logger.error("Kalshi GET %s HTTP %s: %s", path, exc.response.status_code, exc.response.text[:200])
        return None
    except Exception as exc:
        logger.error("Kalshi GET %s error: %s", path, exc)
        return None


def _post(path: str, body: dict) -> Optional[dict]:
    full_url = f"{config.KALSHI_BASE_URL}{path}"
    headers = _auth_headers("POST", f"/trade-api/v2{path}")
    try:
        resp = requests.post(full_url, headers=headers, json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as exc:
        logger.error("Kalshi POST %s HTTP %s: %s", path, exc.response.status_code, exc.response.text[:200])
        return None
    except Exception as exc:
        logger.error("Kalshi POST %s error: %s", path, exc)
        return None


def _delete(path: str) -> bool:
    full_url = f"{config.KALSHI_BASE_URL}{path}"
    headers = _auth_headers("DELETE", f"/trade-api/v2{path}")
    try:
        resp = requests.delete(full_url, headers=headers, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.error("Kalshi DELETE %s error: %s", path, exc)
        return False


def _parse_bucket_from_ticker(ticker: str, city_suffix: str):
    try:
        parts = ticker.split("-")
        if len(parts) < 3:
            return None, None
        bucket_str = parts[-1].upper()
        if bucket_str.startswith("T") and "B" not in bucket_str:
            return float(bucket_str[1:]), float("inf")
        if bucket_str.startswith("T") and bucket_str.endswith("B"):
            return float("-inf"), float(bucket_str[1:-1])
        if bucket_str.startswith("B") and "T" in bucket_str:
            b_idx = bucket_str.index("T")
            return float(bucket_str[1:b_idx]), float(bucket_str[b_idx + 1:])
        if bucket_str.startswith("B"):
            return float("-inf"), float(bucket_str[1:])
        return None, None
    except Exception:
        return None, None


def _identify_city_from_ticker(ticker: str) -> Optional[str]:
    ticker_upper = ticker.upper()
    for city_key, city_cfg in config.CITIES.items():
        suffix = city_cfg["kalshi_suffix"].upper()
        if f"KXHIGH{suffix}" in ticker_upper:
            return city_key
    return None


def get_temperature_markets(target_date=None) -> list:
    if target_date is None:
        target_date = (datetime.now(timezone.utc) + timedelta(days=1)).date()
    all_markets = []
    cursor = None
    while True:
        params = {"status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _get("/markets", params=params)
        if not data:
            break
        markets_raw = data.get("markets", [])
        for m in markets_raw:
            ticker = m.get("ticker", "")
            if "KXHIGH" not in ticker.upper():
                continue
            yes_ask = float(m.get("yes_ask", "0") or "0")
            yes_bid = float(m.get("yes_bid", "0") or "0")
            if yes_ask == 0:
                yes_ask = float(m.get("yes_ask_cost", "0") or "0")
            city_key = _identify_city_from_ticker(ticker)
            bucket_low, bucket_high = _parse_bucket_from_ticker(ticker, "")
            all_markets.append(KalshiMarket(
                ticker=ticker,
                event_ticker=m.get("event_ticker", ""),
                title=m.get("title", ""),
                status=m.get("status", ""),
                yes_ask=yes_ask,
                yes_bid=yes_bid,
                bucket_low=bucket_low,
                bucket_high=bucket_high,
                city_key=city_key,
            ))
        cursor = data.get("cursor")
        if not cursor or not markets_raw:
            break
    logger.info("Fetched %d open KXHIGH markets from Kalshi", len(all_markets))
    return all_markets


def get_markets_for_city(city_key: str, target_date=None) -> list:
    return [m for m in get_temperature_markets(target_date) if m.city_key == city_key]


def get_balance() -> float:
    data = _get("/portfolio/balance")
    if not data:
        return 0.0
    balance = data.get("balance", 0)
    if isinstance(balance, (int, float)) and balance > 1000:
        return balance / 100.0
    return float(balance)


def get_positions() -> list:
    data = _get("/portfolio/positions")
    if not data:
        return []
    positions = []
    for p in data.get("market_positions", []):
        ticker = p.get("ticker", "")
        exposure = int(p.get("market_exposure", 0))
        if exposure == 0:
            continue
        positions.append(KalshiPosition(
            ticker=ticker,
            market_exposure=exposure,
            realized_pnl=float(p.get("realized_pnl", 0)) / 100.0,
            unrealized_pnl=float(p.get("unrealized_pnl", 0)) / 100.0,
            position_value=float(p.get("total_traded", 0)) / 100.0,
        ))
    logger.info("Open positions: %d", len(positions))
    return positions


def place_buy_order(ticker: str, yes_price_cents: int, count: int) -> OrderResult:
    if config.DRY_RUN:
        logger.info("[DRY RUN] Would BUY %d x %s @ %dc", count, ticker, yes_price_cents)
        return OrderResult(success=True, order_id="DRY_RUN",
                           filled_price=yes_price_cents / 100.0, filled_count=count)
    body = {"ticker": ticker, "action": "buy", "side": "yes",
            "type": "limit", "yes_price": yes_price_cents, "count": count}
    result = _post("/portfolio/orders", body)
    if result and result.get("order"):
        order = result["order"]
        return OrderResult(success=True, order_id=order.get("order_id"),
                           filled_price=float(order.get("yes_price", yes_price_cents)) / 100.0,
                           filled_count=int(order.get("count", count)))
    return OrderResult(success=False, error=str(result))


def place_sell_order(ticker: str, yes_price_cents: int, count: int) -> OrderResult:
    if config.DRY_RUN:
        logger.info("[DRY RUN] Would SELL %d x %s @ %dc", count, ticker, yes_price_cents)
        return OrderResult(success=True, order_id="DRY_RUN",
                           filled_price=yes_price_cents / 100.0, filled_count=count)
    body = {"ticker": ticker, "action": "sell", "side": "yes",
            "type": "limit", "yes_price": yes_price_cents, "count": count}
    result = _post("/portfolio/orders", body)
    if result and result.get("order"):
        order = result["order"]
        return OrderResult(success=True, order_id=order.get("order_id"),
                           filled_price=float(order.get("yes_price", yes_price_cents)) / 100.0,
                           filled_count=int(order.get("count", count)))
    return OrderResult(success=False, error=str(result))


def cancel_order(order_id: str) -> bool:
    if config.DRY_RUN:
        logger.info("[DRY RUN] Would cancel order %s", order_id)
        return True
    return _delete(f"/portfolio/orders/{order_id}")
HEREDOC

# =============================================================
# decision_engine.py
# =============================================================
cat > decision_engine.py << 'HEREDOC'
"""
decision_engine.py
------------------
Compares NOAA forecasts to Kalshi market prices, generates buy/sell signals.

Buy: NOAA confidence > BUY_CONFIDENCE_THRESHOLD AND market ask < BUY_MAX_PRICE
Sell: Market bid > SELL_MIN_PRICE for a held position
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional

import config
from kalshi_client import KalshiMarket, KalshiPosition
from noaa_scanner import NOAAForecast

logger = logging.getLogger(__name__)


@dataclass
class BuySignal:
    market: KalshiMarket
    city_key: str
    city_name: str
    noaa_confidence: float
    market_price: float
    forecasted_high: float
    edge: float

    def __str__(self) -> str:
        return (
            f"BUY {self.market.ticker} | {self.city_name} | "
            f"NOAA {self.noaa_confidence:.1%} | ask ${self.market_price:.2f} | "
            f"high {self.forecasted_high:.0f}F | edge {self.edge:.1%}"
        )


@dataclass
class SellSignal:
    position: KalshiPosition
    market: Optional[KalshiMarket]
    bid_price: float
    reason: str

    def __str__(self) -> str:
        return (
            f"SELL {self.position.ticker} | {self.reason} | bid ${self.bid_price:.2f}"
        )


def generate_buy_signals(
    forecasts: dict,
    open_markets: list,
    held_tickers: set,
) -> list:
    signals = []
    for market in open_markets:
        if market.ticker in held_tickers:
            continue
        if not market.city_key:
            continue
        forecast = forecasts.get(market.city_key)
        if forecast is None:
            continue
        if market.bucket_low is None or market.bucket_high is None:
            continue
        if market.yes_ask <= 0:
            continue
        confidence = forecast.confidence_in_range(market.bucket_low, market.bucket_high)
        logger.debug(
            "%s bucket [%.0f, %.0f] conf %.1f%% ask $%.2f",
            market.ticker,
            market.bucket_low if not math.isinf(market.bucket_low) else -999,
            market.bucket_high if not math.isinf(market.bucket_high) else 999,
            confidence * 100, market.yes_ask,
        )
        if confidence > config.BUY_CONFIDENCE_THRESHOLD and market.yes_ask < config.BUY_MAX_PRICE:
            edge = confidence - market.yes_ask
            city_info = config.CITIES[market.city_key]
            signal = BuySignal(
                market=market, city_key=market.city_key, city_name=city_info["name"],
                noaa_confidence=confidence, market_price=market.yes_ask,
                forecasted_high=forecast.forecasted_high, edge=edge,
            )
            signals.append(signal)
            logger.info("BUY SIGNAL: %s", signal)
    signals.sort(key=lambda s: s.edge, reverse=True)
    return signals


def generate_sell_signals(positions: list, open_markets: list) -> list:
    market_by_ticker = {m.ticker: m for m in open_markets}
    signals = []
    for position in positions:
        if position.market_exposure <= 0:
            continue
        market = market_by_ticker.get(position.ticker)
        bid = market.yes_bid if market else 0.0
        if bid > config.SELL_MIN_PRICE:
            signal = SellSignal(
                position=position, market=market, bid_price=bid, reason="take_profit"
            )
            signals.append(signal)
            logger.info("SELL SIGNAL: %s", signal)
    return signals
HEREDOC

# =============================================================
# risk_manager.py
# =============================================================
cat > risk_manager.py << 'HEREDOC'
"""
risk_manager.py
---------------
Enforces trading risk limits. Tracks daily P&L, open positions, sizing.

Limits:
  1. MAX_POSITION_USD   — max cost per new position
  2. MAX_OPEN_POSITIONS — max concurrent open positions
  3. MAX_DAILY_LOSS_USD — kill switch
"""

import logging
from datetime import date, datetime, timezone

import config
from kalshi_client import KalshiPosition

logger = logging.getLogger(__name__)


class RiskLimitExceeded(Exception):
    pass


class RiskManager:
    def __init__(self):
        self._open_tickers: set = set()
        self._daily_pnl: float = 0.0
        self._last_reset_date: date = datetime.now(timezone.utc).date()
        self._daily_spent: float = 0.0
        self._daily_received: float = 0.0

    def _maybe_reset_daily(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._last_reset_date:
            logger.info("New trading day (%s). Resetting daily P&L: $%.2f", today, self._daily_pnl)
            self._daily_pnl = 0.0
            self._daily_spent = 0.0
            self._daily_received = 0.0
            self._last_reset_date = today

    @property
    def daily_pnl(self) -> float:
        self._maybe_reset_daily()
        return self._daily_pnl

    @property
    def open_position_count(self) -> int:
        return len(self._open_tickers)

    def sync_positions(self, live_positions: list) -> None:
        live_tickers = {p.ticker for p in live_positions if p.market_exposure > 0}
        closed = self._open_tickers - live_tickers
        for ticker in closed:
            logger.info("Position closed: %s", ticker)
        self._open_tickers = self._open_tickers.intersection(live_tickers) | live_tickers
        total_unrealized = sum(p.unrealized_pnl for p in live_positions)
        total_realized = sum(p.realized_pnl for p in live_positions)
        self._daily_pnl = total_realized + total_unrealized
        logger.debug("Risk sync: %d positions | P&L $%.2f", len(self._open_tickers), self._daily_pnl)

    def check_buy(self, ticker: str, cost_usd: float) -> None:
        self._maybe_reset_daily()
        if self._daily_pnl <= -config.MAX_DAILY_LOSS_USD:
            raise RiskLimitExceeded(
                f"Daily loss limit: ${self._daily_pnl:.2f} <= -${config.MAX_DAILY_LOSS_USD:.2f}"
            )
        if self.open_position_count >= config.MAX_OPEN_POSITIONS:
            raise RiskLimitExceeded(
                f"Max open positions: {self.open_position_count} >= {config.MAX_OPEN_POSITIONS}"
            )
        if cost_usd > config.MAX_POSITION_USD:
            raise RiskLimitExceeded(
                f"Position size ${cost_usd:.2f} > max ${config.MAX_POSITION_USD:.2f}"
            )
        if ticker in self._open_tickers:
            raise RiskLimitExceeded(f"Already holding {ticker}")

    def check_sell(self, ticker: str) -> None:
        if ticker not in self._open_tickers and not config.DRY_RUN:
            logger.warning("Attempted to sell %s but not in tracked positions", ticker)

    def record_buy(self, ticker: str, cost_usd: float) -> None:
        self._open_tickers.add(ticker)
        self._daily_spent += cost_usd
        self._daily_pnl -= cost_usd
        logger.info("Recorded BUY %s $%.2f | positions: %d | P&L: $%.2f",
                    ticker, cost_usd, self.open_position_count, self._daily_pnl)

    def record_sell(self, ticker: str, proceeds_usd: float, cost_basis: float = 0.0) -> None:
        self._open_tickers.discard(ticker)
        self._daily_received += proceeds_usd
        self._daily_pnl += proceeds_usd
        logger.info("Recorded SELL %s $%.2f | positions: %d | P&L: $%.2f",
                    ticker, proceeds_usd, self.open_position_count, self._daily_pnl)

    def compute_position_size(self, price_usd: float, balance_usd: float):
        if price_usd <= 0:
            return 0, 0.0
        available_per_position = min(
            config.MAX_POSITION_USD,
            balance_usd / max(config.MAX_OPEN_POSITIONS, 1),
        )
        num_contracts = max(1, int(available_per_position / price_usd))
        total_cost = num_contracts * price_usd
        if total_cost > config.MAX_POSITION_USD:
            num_contracts = max(1, int(config.MAX_POSITION_USD / price_usd))
            total_cost = num_contracts * price_usd
        return num_contracts, total_cost

    def status_summary(self) -> str:
        self._maybe_reset_daily()
        return (
            f"Positions: {self.open_position_count}/{config.MAX_OPEN_POSITIONS} | "
            f"Daily P&L: ${self._daily_pnl:.2f} | "
            f"Loss limit: -${config.MAX_DAILY_LOSS_USD:.2f}"
        )


risk_manager = RiskManager()
HEREDOC

# =============================================================
# telegram_alerts.py
# =============================================================
cat > telegram_alerts.py << 'HEREDOC'
"""
telegram_alerts.py
------------------
Sends formatted trade alerts to Telegram. All sends are best-effort.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)
TELEGRAM_API_BASE = "https://api.telegram.org"


def _send(text: str, parse_mode: str = "HTML") -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured.")
        return False
    url = f"{TELEGRAM_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
        }, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
        return False


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _dry_tag() -> str:
    return "BLUE [DRY RUN] " if config.DRY_RUN else ""


def _bucket_display(low: Optional[float], high: Optional[float]) -> str:
    if low is None or high is None:
        return "unknown"
    lo_str = f"{low:.0f}F" if not math.isinf(low) else "-inf"
    hi_str = f"{high:.0f}F" if not math.isinf(high) else "+inf"
    return f"{lo_str} to {hi_str}"


def alert_bot_started() -> None:
    mode = "DRY RUN" if config.DRY_RUN else "LIVE TRADING"
    _send(
        f"Kalshi Weather Bot Started\n"
        f"Mode: {mode}\n"
        f"Time: {_now_str()}\n"
        f"Cities: {', '.join(config.CITIES.keys())}\n"
        f"Buy: NOAA >{config.BUY_CONFIDENCE_THRESHOLD:.0%} @ &lt;${config.BUY_MAX_PRICE:.2f}\n"
        f"Sell: bid &gt;${config.SELL_MIN_PRICE:.2f}\n"
        f"Max position: ${config.MAX_POSITION_USD:.2f} | "
        f"Max positions: {config.MAX_OPEN_POSITIONS} | "
        f"Daily loss limit: -${config.MAX_DAILY_LOSS_USD:.2f}"
    )


def alert_bot_stopped(reason: str = "Manual shutdown") -> None:
    _send(f"Kalshi Weather Bot Stopped\nReason: {reason}\nTime: {_now_str()}")


def alert_buy_executed(signal, result, num_contracts: int, cost_usd: float) -> None:
    status = "Filled" if result.success else "FAILED"
    bucket_str = _bucket_display(signal.market.bucket_low, signal.market.bucket_high)
    text = (
        f"{_dry_tag()}BUY ORDER {status}\n"
        f"Ticker: {signal.market.ticker}\n"
        f"City: {signal.city_name}\n"
        f"Bucket: {bucket_str}\n"
        f"Contracts: {num_contracts} | Price: ${signal.market_price:.2f} | Cost: ${cost_usd:.2f}\n"
        f"NOAA conf: {signal.noaa_confidence:.1%} | Forecast high: {signal.forecasted_high:.0f}F\n"
        f"Edge: {signal.edge:.1%} | Order: {result.order_id or 'n/a'}\n"
        f"Time: {_now_str()}"
    )
    if not result.success:
        text += f"\nError: {result.error}"
    _send(text)


def alert_sell_executed(signal, result, proceeds_usd: float) -> None:
    status = "Filled" if result.success else "FAILED"
    text = (
        f"{_dry_tag()}SELL ORDER {status}\n"
        f"Ticker: {signal.position.ticker}\n"
        f"Reason: {signal.reason} | Bid: ${signal.bid_price:.2f}\n"
        f"Proceeds: ${proceeds_usd:.2f}\n"
        f"Order: {result.order_id or 'n/a'} | Time: {_now_str()}"
    )
    if not result.success:
        text += f"\nError: {result.error}"
    _send(text)


def alert_risk_blocked(ticker: str, reason: str) -> None:
    _send(f"Trade Blocked: {ticker}\nReason: {reason}\nTime: {_now_str()}")


def alert_daily_kill_switch(daily_pnl: float) -> None:
    _send(
        f"DAILY LOSS LIMIT HIT - TRADING HALTED\n"
        f"P&L: ${daily_pnl:.2f} | Limit: -${config.MAX_DAILY_LOSS_USD:.2f}\n"
        f"Time: {_now_str()}"
    )


def alert_scan_summary(cities_scanned, markets_checked, buy_signals,
                       sell_signals, open_positions, daily_pnl) -> None:
    _send(
        f"Scan Summary\n"
        f"Cities: {cities_scanned} | Markets: {markets_checked}\n"
        f"Buy signals: {buy_signals} | Sell signals: {sell_signals}\n"
        f"Open positions: {open_positions}/{config.MAX_OPEN_POSITIONS}\n"
        f"Daily P&L: ${daily_pnl:.2f} | Time: {_now_str()}"
    )


def alert_error(context: str, exc: Exception) -> None:
    _send(
        f"Bot Error\nContext: {context}\n"
        f"Error: {type(exc).__name__}: {str(exc)[:300]}\n"
        f"Time: {_now_str()}"
    )
HEREDOC

# =============================================================
# main.py
# =============================================================
cat > main.py << 'HEREDOC'
"""
main.py
-------
Kalshi Weather Trading Bot — main orchestrator loop.
Runs every SCAN_INTERVAL_SECONDS (default 120s).

Usage:
  python main.py
  DRY_RUN=false python main.py    # live trading
"""

import logging
import signal
import sys
import time

import config
import telegram_alerts
from decision_engine import generate_buy_signals, generate_sell_signals
from kalshi_client import get_balance, get_positions, get_temperature_markets, place_buy_order, place_sell_order
from noaa_scanner import get_all_forecasts
from risk_manager import RiskLimitExceeded, risk_manager


def _setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers = [logging.StreamHandler(sys.stdout)]
    if config.LOG_FILE:
        handlers.append(logging.FileHandler(config.LOG_FILE, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format=fmt, datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers,
    )


logger = logging.getLogger(__name__)
_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    logger.info("Shutdown signal received. Finishing current cycle...")
    _shutdown_requested = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def run_scan_cycle(cycle_number: int) -> dict:
    stats = {"cities_scanned": 0, "markets_checked": 0, "buy_signals": 0,
             "sell_signals": 0, "buys_executed": 0, "sells_executed": 0, "errors": 0}

    logger.info("=== Cycle %d: NOAA forecasts ===", cycle_number)
    forecasts = get_all_forecasts()
    successful = {k: v for k, v in forecasts.items() if v is not None}
    stats["cities_scanned"] = len(successful)
    logger.info("NOAA: %d/%d cities", len(successful), len(config.CITIES))

    logger.info("=== Cycle %d: Kalshi markets ===", cycle_number)
    open_markets = get_temperature_markets()
    stats["markets_checked"] = len(open_markets)

    logger.info("=== Cycle %d: Positions ===", cycle_number)
    live_positions = get_positions()
    risk_manager.sync_positions(live_positions)
    held_tickers = {p.ticker for p in live_positions if p.market_exposure > 0}

    # --- Sells first ---
    sell_signals = generate_sell_signals(live_positions, open_markets)
    stats["sell_signals"] = len(sell_signals)
    for signal in sell_signals:
        ticker = signal.position.ticker
        num_contracts = abs(signal.position.market_exposure)
        bid_cents = max(1, min(99, int(signal.bid_price * 100)))
        proceeds = num_contracts * signal.bid_price
        try:
            risk_manager.check_sell(ticker)
        except RiskLimitExceeded as exc:
            logger.warning("Sell blocked %s: %s", ticker, exc)
            stats["errors"] += 1
            continue
        result = place_sell_order(ticker=ticker, yes_price_cents=bid_cents, count=num_contracts)
        telegram_alerts.alert_sell_executed(signal, result, proceeds)
        if result.success:
            risk_manager.record_sell(ticker, proceeds)
            held_tickers.discard(ticker)
            stats["sells_executed"] += 1
        else:
            logger.error("SELL failed %s: %s", ticker, result.error)
            stats["errors"] += 1

    # --- Kill switch ---
    if risk_manager.daily_pnl <= -config.MAX_DAILY_LOSS_USD:
        logger.warning("Daily loss limit reached $%.2f. Skipping buys.", risk_manager.daily_pnl)
        telegram_alerts.alert_daily_kill_switch(risk_manager.daily_pnl)
        return stats

    # --- Buys ---
    buy_signals = generate_buy_signals(forecasts, open_markets, held_tickers)
    stats["buy_signals"] = len(buy_signals)
    balance = get_balance()
    logger.info("Balance: $%.2f", balance)

    for signal in buy_signals:
        ticker = signal.market.ticker
        ask_cents = max(1, min(99, int(signal.market.yes_ask * 100)))
        num_contracts, cost_usd = risk_manager.compute_position_size(signal.market.yes_ask, balance)
        if num_contracts == 0:
            continue
        try:
            risk_manager.check_buy(ticker, cost_usd)
        except RiskLimitExceeded as exc:
            logger.warning("Buy blocked %s: %s", ticker, exc)
            telegram_alerts.alert_risk_blocked(ticker, str(exc))
            stats["errors"] += 1
            if "Max open positions" in str(exc):
                break
            continue
        result = place_buy_order(ticker=ticker, yes_price_cents=ask_cents, count=num_contracts)
        telegram_alerts.alert_buy_executed(signal, result, num_contracts, cost_usd)
        if result.success:
            risk_manager.record_buy(ticker, cost_usd)
            held_tickers.add(ticker)
            balance -= cost_usd
            stats["buys_executed"] += 1
        else:
            logger.error("BUY failed %s: %s", ticker, result.error)
            stats["errors"] += 1

    logger.info("Cycle %d done: %s", cycle_number, risk_manager.status_summary())
    return stats


def main() -> None:
    _setup_logging()
    logger.info("=" * 60)
    logger.info("Kalshi Weather Bot starting | Mode: %s",
                "DRY RUN" if config.DRY_RUN else "LIVE TRADING")
    logger.info("=" * 60)
    telegram_alerts.alert_bot_started()

    cycle = 0
    SUMMARY_EVERY_N = 15  # ~30 minutes

    try:
        while not _shutdown_requested:
            cycle += 1
            cycle_start = time.monotonic()
            try:
                stats = run_scan_cycle(cycle)
                if cycle % SUMMARY_EVERY_N == 0:
                    telegram_alerts.alert_scan_summary(
                        cities_scanned=stats["cities_scanned"],
                        markets_checked=stats["markets_checked"],
                        buy_signals=stats["buy_signals"],
                        sell_signals=stats["sell_signals"],
                        open_positions=risk_manager.open_position_count,
                        daily_pnl=risk_manager.daily_pnl,
                    )
            except Exception as exc:
                logger.exception("Unhandled error in cycle %d: %s", cycle, exc)
                telegram_alerts.alert_error(f"cycle {cycle}", exc)
                time.sleep(10)

            elapsed = time.monotonic() - cycle_start
            sleep_time = max(0.0, config.SCAN_INTERVAL_SECONDS - elapsed)
            deadline = time.monotonic() + sleep_time
            while time.monotonic() < deadline and not _shutdown_requested:
                time.sleep(min(1.0, deadline - time.monotonic()))
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Bot shutting down after %d cycles.", cycle)
        telegram_alerts.alert_bot_stopped("Normal shutdown")


if __name__ == "__main__":
    main()
HEREDOC

# =============================================================
# setup_env.sh
# =============================================================
cat > setup_env.sh << 'HEREDOC'
#!/usr/bin/env bash
# Interactive setup: prompts for API keys and writes .env file.
# Usage: chmod +x setup_env.sh && ./setup_env.sh

set -euo pipefail
ENV_FILE=".env"
TEMPLATE_FILE=".env.template"

echo ""
echo "=================================================="
echo " Kalshi Weather Bot - Environment Setup"
echo "=================================================="
echo ""

if [[ ! -f "$TEMPLATE_FILE" ]]; then
    echo "ERROR: .env.template not found. Run from the bot directory."
    exit 1
fi

if [[ -f "$ENV_FILE" ]]; then
    read -rp ".env exists. Overwrite? [y/N]: " ow
    [[ "${ow,,}" != "y" ]] && { echo "Aborting."; exit 0; }
fi

cp "$TEMPLATE_FILE" "$ENV_FILE"

read -rp "Kalshi API Key UUID: " KALSHI_KEY
while [[ -z "$KALSHI_KEY" ]]; do
    read -rp "Cannot be empty. Kalshi API Key: " KALSHI_KEY
done

echo "Paste RSA private key PEM (all lines). Ctrl+D when done:"
KALSHI_PEM_RAW=$(cat)
KALSHI_PEM_ESCAPED=$(echo "$KALSHI_PEM_RAW" | awk '{printf "%s\\n", $0}' | sed 's/\\n$//')

read -rp "Enable DRY_RUN? [Y/n]: " dr_input
DRY_MODE="true"
if [[ "${dr_input,,}" == "n" ]]; then
    read -rp "Type LIVE to confirm live trading: " live_c
    [[ "$live_c" == "LIVE" ]] && DRY_MODE="false" || echo "Defaulting to DRY RUN."
fi

read -rp "Your email for NOAA User-Agent [optional]: " NOAA_EMAIL
NOAA_UA="KalshiWeatherBot/1.0 (${NOAA_EMAIL:-bot@example.com})"

python3 - <<PYEOF
import re
with open("$ENV_FILE") as f:
    c = f.read()
replacements = {
    r"^KALSHI_ACCESS_KEY=.*": "KALSHI_ACCESS_KEY=${KALSHI_KEY}",
    r"^KALSHI_PRIVATE_KEY_PEM=.*": "KALSHI_PRIVATE_KEY_PEM=${KALSHI_PEM_ESCAPED}",
    r"^DRY_RUN=.*": "DRY_RUN=${DRY_MODE}",
    r"^NOAA_USER_AGENT=.*": "NOAA_USER_AGENT=${NOAA_UA}",
}
for pattern, replacement in replacements.items():
    c = re.sub(pattern, replacement, c, flags=re.MULTILINE)
with open("$ENV_FILE", "w") as f:
    f.write(c)
print("OK: .env written.")
PYEOF

chmod 600 "$ENV_FILE"
echo ""
echo "Setup complete! Next steps:"
echo "  pip install -r requirements.txt"
echo "  python main.py"
HEREDOC

chmod +x setup_env.sh

# =============================================================
# Install dependencies
# =============================================================
echo ""
echo "Installing Python dependencies..."
pip install -r requirements.txt --quiet

echo ""
echo "============================================================"
echo " Kalshi Weather Bot deployed to: $BOT_DIR"
echo "============================================================"
echo ""
echo "Files created:"
ls -1 "$BOT_DIR"
echo ""
echo "Next steps:"
echo ""
echo "  1. Fill in your credentials:"
echo "     cp .env.template .env && nano .env"
echo ""
echo "     OR run the interactive setup:"
echo "     ./setup_env.sh"
echo ""
echo "  2. Required in .env:"
echo "     KALSHI_ACCESS_KEY=<your-uuid>"
echo "     KALSHI_PRIVATE_KEY_PEM=<pem-with-\\n-newlines>"
echo ""
echo "  3. Start in dry-run mode (default, safe):"
echo "     python main.py"
echo ""
echo "  4. Watch logs:"
echo "     tail -f kalshi_weather_bot.log"
echo ""
echo "  5. Switch to live trading when ready:"
echo "     Edit .env: DRY_RUN=false"
echo "     Then: python main.py"
echo ""
echo "  6. Run as a background service:"
echo "     nohup python main.py > kalshi_weather_bot.log 2>&1 &"
echo "     echo 'PID:' \$!"
echo ""
echo "  Telegram alerts will be sent to chat ID: 8718921224"
echo "============================================================"
