"""
implied_vol.py — Extract implied volatility from Kalshi market prices.

Treats Kalshi contracts as digital (binary) options and backs out the
implied volatility from the observed strike/price ladder.

Digital call price ≈ N(d2) where d2 = (ln(S/K) - σ²T/2) / (σ√T)
With r≈0 for short-duration markets.
"""
import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)


def _normal_cdf(x: float) -> float:
    """Standard normal CDF using math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _digital_call_price(spot: float, strike: float, vol: float, T: float) -> float:
    """Price of a digital call option (pays $1 if S > K at expiry).
    
    Returns probability that spot will be above strike at time T.
    """
    if vol <= 0 or T <= 0:
        return 1.0 if spot > strike else 0.0
    if strike <= 0 or spot <= 0:
        return 0.5

    d2 = (math.log(spot / strike) - 0.5 * vol * vol * T) / (vol * math.sqrt(T))
    return _normal_cdf(d2)


def compute_implied_vol(
    markets: list,
    current_price: float,
    days_to_settlement: float,
    min_ask: float = 0.05,
    max_ask: float = 0.95,
) -> Optional[float]:
    """Compute market-implied volatility from a set of strike/price pairs.
    
    Args:
        markets: List of objects with .strike_price and .yes_ask attributes
        current_price: Current spot price of the underlying
        days_to_settlement: Days until settlement (fractions OK)
        min_ask: Minimum ask price to include (filters noise)
        max_ask: Maximum ask price to include
    
    Returns:
        Implied annual volatility (as a decimal, e.g., 0.25 for 25%), 
        or None if insufficient data.
    """
    if current_price <= 0 or days_to_settlement <= 0:
        return None

    T = days_to_settlement / 365.0  # Convert to years for standard vol

    # Collect valid (strike, market_price) pairs
    pairs = []
    for m in markets:
        ask = getattr(m, 'yes_ask', 0)
        strike = getattr(m, 'strike_price', 0)
        if min_ask <= ask <= max_ask and strike > 0:
            pairs.append((strike, ask))

    if len(pairs) < 3:
        return None  # Need at least 3 strikes for reliable fit

    # Bisection search for sigma that minimizes sum of squared errors
    best_vol = None
    best_error = float('inf')

    # Search in daily vol space (0.1% to 10% daily), convert to annual
    for daily_vol_pct in [x * 0.05 for x in range(1, 201)]:  # 0.05% to 10%
        daily_vol = daily_vol_pct / 100.0
        # For the Gaussian model in price_model.py, sigma = daily_vol * sqrt(days)
        # But for Black-Scholes, we need annual vol
        annual_vol = daily_vol * math.sqrt(252)  # ~15.87x
        
        total_error = 0
        for strike, market_price in pairs:
            model_price = _digital_call_price(current_price, strike, annual_vol, T)
            total_error += (model_price - market_price) ** 2
        
        if total_error < best_error:
            best_error = total_error
            best_vol = daily_vol  # Store as daily vol for compatibility with price_model

    if best_vol is None:
        return None

    avg_error = math.sqrt(best_error / len(pairs))
    if avg_error > 0.25:  # If average error per strike > 25¢, fit is too poor
        logger.warning("Implied vol fit too poor (RMSE=%.3f), ignoring", avg_error)
        return None

    logger.info(
        "Implied vol: %.4f/day (%.1f%% annual) from %d strikes, RMSE=%.3f",
        best_vol, best_vol * math.sqrt(252) * 100, len(pairs), avg_error,
    )
    return best_vol


def vol_edge(model_vol: float, implied_vol: float) -> float:
    """Compare model vol to market-implied vol.
    
    Positive = we think more movement than market → OTM may be underpriced
    Negative = we think less movement → OTM may be overpriced
    """
    if implied_vol <= 0:
        return 0.0
    return (model_vol - implied_vol) / implied_vol


def blend_volatility(model_vol: float, implied_vol: Optional[float], 
                      model_weight: float = 0.6) -> float:
    """Blend model volatility with market-implied volatility.
    
    Default: 60% model, 40% market. If implied_vol is None, use 100% model.
    """
    if implied_vol is None or implied_vol <= 0:
        return model_vol
    return model_vol * model_weight + implied_vol * (1 - model_weight)
