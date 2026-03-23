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
  3. Extract all hourly temps for "tomorrow" (next calendar day in local tz)
  4. Use those to estimate a simple Gaussian confidence that the daily high
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

# Cache NOAA grid-point lookups so we don't hammer the /points endpoint
_GRID_CACHE: dict[str, str] = {}  # city_key -> forecastHourly URL


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_headers() -> dict:
    return {"User-Agent": config.NOAA_USER_AGENT, "Accept": "application/geo+json"}


def _get_forecast_hourly_url(city_key: str, lat: float, lon: float) -> Optional[str]:
    """Resolve the forecastHourly endpoint for a city. Cached after first call."""
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


def _fetch_hourly_periods(forecast_url: str) -> list[dict]:
    """Fetch raw hourly forecast periods from NOAA."""
    try:
        resp = requests.get(forecast_url, headers=_get_headers(), timeout=20)
        resp.raise_for_status()
        return resp.json()["properties"]["periods"]
    except Exception as exc:
        logger.error("Failed to fetch hourly forecast from %s: %s", forecast_url, exc)
        return []


def _to_fahrenheit(temp: float, unit: str) -> float:
    """Convert Celsius to Fahrenheit if needed."""
    if unit.upper() in ("C", "CELSIUS"):
        return temp * 9 / 5 + 32
    return temp


def _get_tomorrow_date() -> date:
    """Return tomorrow's date in UTC (conservative choice)."""
    return (datetime.now(timezone.utc) + timedelta(days=1)).date()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class NOAAForecast:
    """Container for a city's processed NOAA forecast."""

    def __init__(
        self,
        city_key: str,
        tomorrow_temps_f: list[float],
    ):
        self.city_key = city_key
        self.tomorrow_temps_f = tomorrow_temps_f
        self.forecasted_high: float = max(tomorrow_temps_f) if tomorrow_temps_f else float("nan")
        self.forecasted_low: float = min(tomorrow_temps_f) if tomorrow_temps_f else float("nan")
        # Standard deviation of hourly temps — used as a proxy for forecast spread
        self.std_dev: float = (
            statistics.stdev(tomorrow_temps_f) if len(tomorrow_temps_f) > 1 else 3.0
        )

    def confidence_in_range(self, bucket_low: float, bucket_high: float) -> float:
        """
        Estimate the probability that tomorrow's actual high temperature
        falls within [bucket_low, bucket_high].

        Method: Model the forecasted high as a Gaussian with mean=forecasted_high
        and sigma=std_dev (clipped to a minimum of 2°F to avoid overconfidence).
        Integrate the PDF over the bucket range.

        Returns a probability in [0.0, 1.0].
        """
        if not self.tomorrow_temps_f or math.isnan(self.forecasted_high):
            return 0.0

        sigma = max(self.std_dev, 2.0)
        mu = self.forecasted_high

        # CDF of normal distribution via math.erf
        def normal_cdf(x: float) -> float:
            return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))

        prob = normal_cdf(bucket_high) - normal_cdf(bucket_low)
        return max(0.0, min(1.0, prob))

    def __repr__(self) -> str:
        return (
            f"NOAAForecast(city={self.city_key}, "
            f"high={self.forecasted_high:.1f}°F, "
            f"low={self.forecasted_low:.1f}°F, "
            f"σ={self.std_dev:.1f}°F, "
            f"n_hours={len(self.tomorrow_temps_f)})"
        )


def get_city_forecast(city_key: str) -> Optional[NOAAForecast]:
    """
    Fetch and parse the NOAA hourly forecast for a city.
    Returns an NOAAForecast object, or None on failure.
    """
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
    tomorrow_temps: list[float] = []

    for period in periods:
        # startTime is ISO-8601, e.g. "2025-03-24T14:00:00-05:00"
        try:
            start_str = period.get("startTime", "")
            # Parse with timezone awareness
            start_dt = datetime.fromisoformat(start_str)
            # Compare date portion (in UTC for consistency)
            period_date = start_dt.astimezone(timezone.utc).date()
            if period_date == tomorrow:
                raw_temp = period.get("temperature")
                unit = period.get("temperatureUnit", "F")
                if raw_temp is not None:
                    temp_f = _to_fahrenheit(float(raw_temp), unit)
                    tomorrow_temps.append(temp_f)
        except Exception as exc:
            logger.debug("Skipping period due to parse error: %s", exc)
            continue

    if not tomorrow_temps:
        logger.warning(
            "No tomorrow (%s) hourly data found for %s. "
            "Periods available: %d",
            tomorrow,
            city_key,
            len(periods),
        )
        return None

    forecast = NOAAForecast(city_key=city_key, tomorrow_temps_f=tomorrow_temps)
    logger.info("NOAA %s: %s", city_key, forecast)
    return forecast


def get_all_forecasts() -> dict[str, Optional[NOAAForecast]]:
    """Fetch forecasts for all configured cities. Returns dict keyed by city_key."""
    results: dict[str, Optional[NOAAForecast]] = {}
    for city_key in config.CITIES:
        results[city_key] = get_city_forecast(city_key)
    return results
