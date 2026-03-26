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
KALSHI_ACCESS_KEY: str = os.getenv("KALSHI_ACCESS_KEY", "") or os.getenv("KALSHI_API_KEY_ID", "")

# PEM-encoded RSA private key — either inline or read from a file path
_pem_inline: str = os.getenv("KALSHI_PRIVATE_KEY_PEM", "").replace("\\n", "\n")
_pem_path: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")

if _pem_inline:
    KALSHI_PRIVATE_KEY_PEM: str = _pem_inline
elif _pem_path and os.path.isfile(_pem_path):
    with open(_pem_path, "r") as _f:
        KALSHI_PRIVATE_KEY_PEM: str = _f.read().strip()
else:
    KALSHI_PRIVATE_KEY_PEM: str = ""

# ---------------------------------------------------------------------------
# NOAA API
# ---------------------------------------------------------------------------
NOAA_BASE_URL: str = "https://api.weather.gov"
# NWS requires a descriptive User-Agent
_noaa_contact = os.getenv("NOAA_USER_AGENT", "contact@example.com")
NOAA_USER_AGENT: str = f"(KalshiWeatherBot, {_noaa_contact})"

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
MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "20"))
MAX_DAILY_LOSS_USD: float = float(
    os.getenv("MAX_DAILY_LOSS_USD", "50.00")
)  # Kill switch

# Scan interval
SCAN_INTERVAL_SECONDS: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))  # 5 min (avoids Open-Meteo rate limits)

# ---------------------------------------------------------------------------
# Target cities
# ---------------------------------------------------------------------------
# Each entry: city_key -> (display_name, lat, lon, kalshi_ticker_suffix)
# Kalshi temperature market tickers follow pattern KXHIGH<SUFFIX>-<DATE>-<BUCKET>
CITIES: dict = {
    # --- Original 8 ---
    "NYC": {"name": "New York City", "lat": 40.7128, "lon": -74.0060, "kalshi_suffix": "NYC"},
    "CHI": {"name": "Chicago", "lat": 41.8781, "lon": -87.6298, "kalshi_suffix": "CHI"},
    "LA":  {"name": "Los Angeles", "lat": 34.0522, "lon": -118.2437, "kalshi_suffix": "LA"},
    "MIA": {"name": "Miami", "lat": 25.7617, "lon": -80.1918, "kalshi_suffix": "MIA"},
    "AUS": {"name": "Austin", "lat": 30.2672, "lon": -97.7431, "kalshi_suffix": "AUS"},
    "BOS": {"name": "Boston", "lat": 42.3601, "lon": -71.0589, "kalshi_suffix": "BOS"},
    "HOU": {"name": "Houston", "lat": 29.7604, "lon": -95.3698, "kalshi_suffix": "HOU"},
    "DEN": {"name": "Denver", "lat": 39.7392, "lon": -104.9903, "kalshi_suffix": "DEN"},
    # --- Expanded 12 (all Kalshi temperature cities) ---
    "ATL": {"name": "Atlanta", "lat": 33.7490, "lon": -84.3880, "kalshi_suffix": "ATL"},
    "PHL": {"name": "Philadelphia", "lat": 39.9526, "lon": -75.1652, "kalshi_suffix": "PHL"},
    "PHX": {"name": "Phoenix", "lat": 33.4484, "lon": -112.0740, "kalshi_suffix": "PHX"},
    "DCA": {"name": "Washington DC", "lat": 38.9072, "lon": -77.0369, "kalshi_suffix": "DC"},
    "LAS": {"name": "Las Vegas", "lat": 36.1699, "lon": -115.1398, "kalshi_suffix": "LV"},
    "SAT": {"name": "San Antonio", "lat": 29.4241, "lon": -98.4936, "kalshi_suffix": "SA"},
    "MSP": {"name": "Minneapolis", "lat": 44.9778, "lon": -93.2650, "kalshi_suffix": "MIN"},
    "DAL": {"name": "Dallas", "lat": 32.7767, "lon": -96.7970, "kalshi_suffix": "DAL"},
    "SFO": {"name": "San Francisco", "lat": 37.7749, "lon": -122.4194, "kalshi_suffix": "SF"},
    "OKC": {"name": "Oklahoma City", "lat": 35.4676, "lon": -97.5164, "kalshi_suffix": "OKC"},
}

# ---------------------------------------------------------------------------
# Commodity market entry (shared by gas, oil, etc.)
# ---------------------------------------------------------------------------
COMMODITY_MIN_EDGE: float = float(
    os.getenv("COMMODITY_MIN_EDGE", "0.30")
)  # 30¢ minimum edge (backtest optimal: 100% win rate)
COMMODITY_MIN_CONFIDENCE: float = float(
    os.getenv("COMMODITY_MIN_CONFIDENCE", "0.50")
)  # 50% min confidence (backtest: all 50%+ contracts won)
COMMODITY_MAX_ASK: float = float(
    os.getenv("COMMODITY_MAX_ASK", "0.60")
)  # 60¢ max ask (backtest optimal: captures mid-range edge)
COMMODITY_SELL_MIN_PRICE: float = float(
    os.getenv("COMMODITY_SELL_MIN_PRICE", "0.50")
)  # Take profit threshold
COMMODITY_DRIFT_DAMPENING: float = float(
    os.getenv("COMMODITY_DRIFT_DAMPENING", "0.60")
)  # Mean reversion dampening

# ---------------------------------------------------------------------------
# Spread trading parameters
# ---------------------------------------------------------------------------
ENABLE_SPREAD_TRADING: bool = os.getenv("ENABLE_SPREAD_TRADING", "true").lower() in ("true", "1", "yes")
SPREAD_MAX_TRADES_PER_CYCLE: int = int(os.getenv("SPREAD_MAX_TRADES_PER_CYCLE", "2"))
SPREAD_MIN_PROFIT_CENTS: float = float(os.getenv("SPREAD_MIN_PROFIT_CENTS", "0.05"))  # $0.05 min expected profit per contract
SPREAD_MAX_POSITION_USD: float = float(os.getenv("SPREAD_MAX_POSITION_USD", "2.00"))

# ---------------------------------------------------------------------------
# Metals feature flags
# ---------------------------------------------------------------------------
ENABLE_GOLD: bool = os.getenv("ENABLE_GOLD", "true").lower() in ("true", "1", "yes")
ENABLE_SILVER: bool = os.getenv("ENABLE_SILVER", "true").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# EIA API (for gas price cross-referencing)
# ---------------------------------------------------------------------------
EIA_API_KEY: str = os.getenv("EIA_API_KEY", "xZLioPQmYYDd92cVykFT1q1P2kqKEl71t8huGsCa")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: str = os.getenv("LOG_FILE", "kalshi_weather_bot.log")
