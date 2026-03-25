"""
noaa_scanner.py
---------------
Fetches hourly weather forecasts for configured cities and estimates the
probability (confidence) that tomorrow's high temperature will fall within
a given temperature range (a Kalshi market bucket).

Uses Open-Meteo API (free, no key, cloud-friendly) as primary source.
Falls back to NOAA/NWS if available. Both use the same underlying GFS/HRRR
weather models, so accuracy is equivalent.

Flow:
  1. GET Open-Meteo hourly forecast for each city (next 48h)
  2. Extract all hourly temps for "tomorrow" (next calendar day)
  3. Estimate the forecasted daily high
  4. Use a Gaussian model to estimate confidence that the high lands
     within a given [bucket_low, bucket_high] range
"""

import logging
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class CityForecast:
    """Holds a processed forecast for one city."""

    def __init__(self, city_key: str, forecast_date: date,
                 hourly_temps: list[float], forecasted_high: float):
        self.city_key = city_key
        self.forecast_date = forecast_date
        self.hourly_temps = hourly_temps
        self.forecasted_high = forecasted_high
        # Standard deviation of hourly temps — proxy for forecast uncertainty
        self.temp_std = statistics.stdev(hourly_temps) if len(hourly_temps) > 1 else 3.0

    def confidence_for_range(self, low: float, high: float) -> float:
        return self._gaussian_confidence(low, high)

    # Alias for compatibility
    confidence_in_range = confidence_for_range

    def _gaussian_confidence(self, low: float, high: float) -> float:
        """
        Estimate P(daily_high in [low, high]) using a Gaussian centered on
        the forecasted high with sigma = max(temp_std, 3.5).
        Floor sigma at 3.5°F to account for spring weather volatility.
        """
        sigma = max(self.temp_std, 3.5)
        mu = self.forecasted_high

        def _phi(x: float) -> float:
            """Standard normal CDF approximation."""
            return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2))))

        return _phi(high) - _phi(low)


# ---------------------------------------------------------------------------
# Open-Meteo fetcher (primary — works from cloud servers)
# ---------------------------------------------------------------------------

OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

# Timezone mapping for cities
CITY_TIMEZONES = {
    "NYC": "America/New_York",
    "CHI": "America/Chicago",
    "LA": "America/Los_Angeles",
    "MIA": "America/New_York",
    "AUS": "America/Chicago",
    "BOS": "America/New_York",
    "HOU": "America/Chicago",
    "DEN": "America/Denver",
    "ATL": "America/New_York",
    "PHL": "America/New_York",
    "PHX": "America/Phoenix",
    "DCA": "America/New_York",
    "LAS": "America/Los_Angeles",
    "SAT": "America/Chicago",
    "MSP": "America/Chicago",
    "DAL": "America/Chicago",
    "SFO": "America/Los_Angeles",
    "OKC": "America/Chicago",
}


def _fetch_open_meteo(city_key: str, lat: float, lon: float) -> Optional[CityForecast]:
    """Fetch hourly forecast from Open-Meteo for a city."""
    import time as _time
    _time.sleep(0.5)  # Rate limit: max 2 requests/second to avoid 429s
    tz = CITY_TIMEZONES.get(city_key, "America/New_York")
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "forecast_days": 2,
        "timezone": tz,
    }

    try:
        resp = requests.get(OPEN_METEO_BASE, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Open-Meteo error for %s: %s", city_key, exc)
        return None

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])

    if not times or not temps:
        logger.warning("Open-Meteo returned empty data for %s", city_key)
        return None

    # Determine "tomorrow" in the city's local timezone
    # Open-Meteo returns times in the requested timezone as strings like "2026-03-24T14:00"
    today_str = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # Filter for tomorrow's hours
    tomorrow_temps = []
    for t_str, temp in zip(times, temps):
        if temp is None:
            continue
        if t_str.startswith(tomorrow):
            tomorrow_temps.append(temp)

    # If no tomorrow data yet (late in the day), use today's remaining data
    if not tomorrow_temps:
        logger.info("No tomorrow data for %s, using today's forecast", city_key)
        for t_str, temp in zip(times, temps):
            if temp is None:
                continue
            if t_str.startswith(today_str):
                tomorrow_temps.append(temp)

    if not tomorrow_temps:
        logger.warning("No hourly temps found for %s", city_key)
        return None

    forecasted_high = max(tomorrow_temps)
    forecast_date = (datetime.now() + timedelta(days=1)).date()

    logger.info(
        "Open-Meteo %s: forecasted high %.1f°F (%d hourly points)",
        city_key, forecasted_high, len(tomorrow_temps),
    )

    return CityForecast(
        city_key=city_key,
        forecast_date=forecast_date,
        hourly_temps=tomorrow_temps,
        forecasted_high=forecasted_high,
    )


# ---------------------------------------------------------------------------
# NOAA/NWS fetcher (fallback — blocked on some cloud IPs)
# ---------------------------------------------------------------------------

_GRID_CACHE: dict[str, str] = {}


def _get_noaa_headers() -> dict:
    return {"User-Agent": config.NOAA_USER_AGENT, "Accept": "application/geo+json"}


def _fetch_noaa(city_key: str, lat: float, lon: float) -> Optional[CityForecast]:
    """Fetch hourly forecast from NOAA/NWS. May fail from cloud servers."""
    # Resolve grid point
    if city_key not in _GRID_CACHE:
        url = f"{config.NOAA_BASE_URL}/points/{lat},{lon}"
        try:
            resp = requests.get(url, headers=_get_noaa_headers(), timeout=15)
            resp.raise_for_status()
            props = resp.json().get("properties", {})
            hourly_url = props.get("forecastHourly")
            if not hourly_url:
                logger.error("NOAA: no forecastHourly URL for %s", city_key)
                return None
            _GRID_CACHE[city_key] = hourly_url
        except Exception as exc:
            logger.error("NOAA grid resolve failed for %s: %s", city_key, exc)
            return None

    # Fetch hourly forecast
    try:
        resp = requests.get(_GRID_CACHE[city_key], headers=_get_noaa_headers(), timeout=15)
        resp.raise_for_status()
        periods = resp.json().get("properties", {}).get("periods", [])
    except Exception as exc:
        logger.error("NOAA hourly fetch failed for %s: %s", city_key, exc)
        return None

    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date()
    tomorrow_temps = []
    for p in periods:
        try:
            start = datetime.fromisoformat(p["startTime"])
            if start.date() == tomorrow:
                temp = p.get("temperature")
                if temp is not None:
                    tomorrow_temps.append(float(temp))
        except (KeyError, ValueError):
            continue

    if not tomorrow_temps:
        logger.warning("NOAA: no tomorrow temps for %s", city_key)
        return None

    forecasted_high = max(tomorrow_temps)
    logger.info("NOAA %s: forecasted high %.1f°F (%d points)", city_key, forecasted_high, len(tomorrow_temps))

    return CityForecast(
        city_key=city_key,
        forecast_date=tomorrow,
        hourly_temps=tomorrow_temps,
        forecasted_high=forecasted_high,
    )


# ---------------------------------------------------------------------------
# Public API — tries Open-Meteo first, falls back to NOAA
# ---------------------------------------------------------------------------

def _fetch_open_meteo_day(city_key: str, lat: float, lon: float, target_date: str) -> Optional[CityForecast]:
    """Fetch forecast for a specific date (today or tomorrow)."""
    import time as _time
    _time.sleep(0.5)  # Rate limit: max 2 requests/second to avoid 429s
    tz = CITY_TIMEZONES.get(city_key, "America/New_York")
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "forecast_days": 2,
        "timezone": tz,
    }

    try:
        resp = requests.get(OPEN_METEO_BASE, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Open-Meteo error for %s: %s", city_key, exc)
        return None

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])

    if not times or not temps:
        return None

    day_temps = []
    for t_str, temp in zip(times, temps):
        if temp is not None and t_str.startswith(target_date):
            day_temps.append(temp)

    if not day_temps:
        return None

    forecasted_high = max(day_temps)
    forecast_dt = datetime.strptime(target_date, "%Y-%m-%d").date()

    logger.info(
        "Open-Meteo %s [%s]: forecasted high %.1f°F (%d hourly points)",
        city_key, target_date, forecasted_high, len(day_temps),
    )

    return CityForecast(
        city_key=city_key,
        forecast_date=forecast_dt,
        hourly_temps=day_temps,
        forecasted_high=forecasted_high,
    )


def fetch_all_forecasts() -> dict[str, CityForecast]:
    """
    Fetch forecasts for all configured cities — BOTH today AND tomorrow.
    Returns {city_key: CityForecast} for tomorrow's forecast.
    Also returns today's forecasts in a second dict.
    """
    results: dict[str, CityForecast] = {}

    for city_key, city_info in config.CITIES.items():
        lat = city_info["lat"]
        lon = city_info["lon"]

        # Try Open-Meteo first (works from cloud)
        forecast = _fetch_open_meteo(city_key, lat, lon)

        # Fall back to NOAA if Open-Meteo fails
        if forecast is None:
            logger.info("Trying NOAA fallback for %s", city_key)
            forecast = _fetch_noaa(city_key, lat, lon)

        if forecast is not None:
            results[city_key] = forecast
        else:
            logger.warning("No forecast available for %s from any source", city_key)

    return results


def fetch_today_forecasts() -> dict[str, CityForecast]:
    """
    Fetch TODAY's forecasts for all configured cities.
    Same-day trading: compare forecast against today's Kalshi markets.
    """
    results: dict[str, CityForecast] = {}
    today_str = datetime.now().strftime("%Y-%m-%d")

    for city_key, city_info in config.CITIES.items():
        forecast = _fetch_open_meteo_day(city_key, city_info["lat"], city_info["lon"], today_str)
        if forecast is not None:
            results[city_key] = forecast

    return results
