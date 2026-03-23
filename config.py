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
# PEM-encoded RSA private key (newlines as \n in the env file)
KALSHI_PRIVATE_KEY_PEM: str = os.getenv("KALSHI_PRIVATE_KEY_PEM", "").replace(
    "\\n", "\n"
)

# ---------------------------------------------------------------------------
# NOAA API
# ---------------------------------------------------------------------------
NOAA_BASE_URL: str = "https://api.weather.gov"
# NWS requires a descriptive User-Agent
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
# Buy signal thresholds
BUY_CONFIDENCE_THRESHOLD: float = float(
    os.getenv("BUY_CONFIDENCE_THRESHOLD", "0.85")
)  # NOAA confidence > 85 %
BUY_MAX_PRICE: float = float(
    os.getenv("BUY_MAX_PRICE", "0.15")
)  # Only buy if market ask < $0.15

# Exit signal thresholds
SELL_MIN_PRICE: float = float(
    os.getenv("SELL_MIN_PRICE", "0.45")
)  # Exit if market bid > $0.45

# Risk limits
MAX_POSITION_USD: float = float(
    os.getenv("MAX_POSITION_USD", "2.00")
)  # Max spend per position
MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
MAX_DAILY_LOSS_USD: float = float(
    os.getenv("MAX_DAILY_LOSS_USD", "50.00")
)  # Kill switch

# Scan interval
SCAN_INTERVAL_SECONDS: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "120"))  # 2 min

# ---------------------------------------------------------------------------
# Target cities
# ---------------------------------------------------------------------------
# Each entry: city_key -> (display_name, lat, lon, kalshi_ticker_suffix)
# Kalshi temperature market tickers follow pattern KXHIGH<SUFFIX>-<DATE>-<BUCKET>
CITIES: dict = {
    "NYC": {
        "name": "New York City",
        "lat": 40.7128,
        "lon": -74.0060,
        "kalshi_suffix": "NYC",   # used to filter KXHIGHNYC markets
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
