"""
price_model.py
--------------
Shared commodity price forecast model used by gas, oil, and future markets.

Implements:
  1. Dampened drift — applies mean-reversion dampening to weekly trends
  2. Residual-based volatility — separates noise from trend
  3. Gaussian confidence — P(price > strike) at settlement
  4. Settlement-day handling — tight sigma instead of binary 0/1
"""

import math
from dataclasses import dataclass
from datetime import date


# Default dampening factor — configurable via config.COMMODITY_DRIFT_DAMPENING
_DEFAULT_DRIFT_DAMPENING = 0.60


@dataclass
class CommodityForecast:
    """Holds processed commodity price data and forecast for settlement."""
    current_price: float           # Current price (e.g. AAA avg, WTI spot)
    yesterday_price: float         # Yesterday's price
    week_ago_price: float          # Week ago price
    month_ago_price: float         # Month ago price
    daily_change: float            # Today vs yesterday
    weekly_change: float           # Today vs week ago
    price_std: float               # Residual daily volatility (std dev)
    forecast_date: date            # When we fetched this
    days_to_settlement: int        # Days until market settles

    # Dampening factor for drift (mean reversion). Loaded from config at call
    # sites; default used if not overridden.
    drift_dampening: float = _DEFAULT_DRIFT_DAMPENING

    # Settlement-day sigma (tight Gaussian instead of binary)
    settlement_sigma: float = 0.005

    def confidence_above(self, strike: float) -> float:
        """
        Estimate P(settlement_price > strike) using an improved Gaussian model.

        Improvements over naive random walk:
          - Dampened drift: effective_drift = raw_weekly_drift * DRIFT_DAMPENING
          - Residual volatility: separates noise from trend direction
          - Settlement-day: uses tight sigma ($0.005) instead of binary 0/1
        """
        days = self.days_to_settlement

        # --- Drift: dampened weekly trend ---
        if self.week_ago_price > 0 and self.current_price > 0:
            raw_weekly_drift = self.current_price - self.week_ago_price
        else:
            raw_weekly_drift = self.daily_change * 7.0

        effective_drift = raw_weekly_drift * self.drift_dampening

        # Convert to daily drift and project
        daily_drift = effective_drift / 7.0

        if days <= 0:
            # Settlement day — use tight Gaussian instead of binary
            projected = self.current_price
            sigma = self.settlement_sigma
        else:
            projected = self.current_price + daily_drift * days
            sigma = max(self.price_std, 0.001) * math.sqrt(days)

        if sigma <= 0:
            return 1.0 if projected > strike else 0.0

        # P(price > strike) = 1 - Φ((strike - projected) / sigma)
        z = (strike - projected) / sigma
        prob = 0.5 * (1.0 - math.erf(z / math.sqrt(2)))

        return prob

    def confidence_below(self, strike: float) -> float:
        """P(settlement_price <= strike)"""
        return 1.0 - self.confidence_above(strike)

    def confidence_above_blended(self, strike: float, implied_vol: float = None) -> float:
        """Like confidence_above but blends model vol with market implied vol.
        
        Uses 60% model vol + 40% implied vol. This prevents overconfidence
        when the market disagrees with our volatility estimate.
        If implied_vol is None, falls back to pure model vol.
        """
        if implied_vol is None or implied_vol <= 0:
            return self.confidence_above(strike)
        
        # Save original sigma and compute with blended vol
        days = self.days_to_settlement
        
        # Blend: 60% model, 40% market
        blended_vol = self.price_std * 0.6 + implied_vol * 0.4
        
        # Use the same projection logic as confidence_above
        if self.week_ago_price > 0 and self.current_price > 0:
            raw_weekly_drift = self.current_price - self.week_ago_price
        else:
            raw_weekly_drift = self.daily_change * 7.0
        effective_drift = raw_weekly_drift * self.drift_dampening
        daily_drift = effective_drift / 7.0
        
        if days <= 0:
            projected = self.current_price
            sigma = max(blended_vol, 0.001)
        else:
            projected = self.current_price + daily_drift * days
            sigma = max(blended_vol, 0.001) * math.sqrt(days)
        
        if sigma <= 0:
            return 1.0 if projected > strike else 0.0
        
        z = (strike - projected) / sigma
        return 0.5 * (1.0 - math.erf(z / math.sqrt(2)))


def compute_residual_volatility(
    current_price: float,
    yesterday_price: float,
    week_ago_price: float,
    vol_floor: float = 0.008,
) -> float:
    """
    Compute residual-based daily volatility.

    Instead of |price_change|/time (which conflates trend with noise):
      - raw_daily_vol = |current - yesterday|
      - trend_daily_vol = |weekly_change| / 7
      - residual_vol = max(raw_daily_vol, vol_floor)

    The floor prevents overconfidence when prices are flat.
    """
    raw_daily_vol = abs(current_price - yesterday_price) if yesterday_price > 0 else vol_floor
    # We use raw_daily_vol as the residual — it captures actual day-to-day noise
    # The trend component (weekly_change / 7) is handled by drift, not volatility
    residual_vol = max(raw_daily_vol, vol_floor)
    return residual_vol
