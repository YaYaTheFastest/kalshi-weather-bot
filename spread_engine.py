"""
spread_engine.py — Pair/spread arbitrage detector for adjacent-strike markets.

Detects monotonicity violations, spread compression, and wide gaps between
adjacent strikes. Works with GasMarket and OilMarket via duck typing.
No two-leg trades — logs incoherencies and boosts confirmed single-leg signals.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from price_model import CommodityForecast

logger = logging.getLogger(__name__)


@dataclass
class SpreadSignal:
    ticker_low: str          # Lower strike ticker
    ticker_high: str         # Higher strike ticker
    strike_low: float
    strike_high: float
    ask_low: float           # YES ask at lower strike
    ask_high: float          # YES ask at higher strike
    bid_low: float
    bid_high: float
    signal_type: str         # "monotonicity", "compression", "wide_gap"
    severity: float          # 0.0–1.0, how severe the incoherency is
    description: str
    market_type: str = ""    # "gas_weekly", "oil_daily", etc.
    event_ticker: str = ""

    def __str__(self) -> str:
        return (
            f"SPREAD {self.signal_type} | {self.ticker_low} vs {self.ticker_high} | "
            f"strikes ${self.strike_low:.2f}/${self.strike_high:.2f} | "
            f"asks ${self.ask_low:.2f}/${self.ask_high:.2f} | "
            f"severity {self.severity:.2f}"
        )


@dataclass
class SpreadConfirmation:
    ticker: str
    boost: float             # Additional edge from spread analysis (0.0–0.15)
    reason: str

    def __str__(self) -> str:
        return f"SPREAD CONFIRM {self.ticker} | boost +{self.boost:.2f} | {self.reason}"


def _group_by_event(markets) -> dict:
    groups = {}
    for m in markets:
        key = getattr(m, "event_ticker", "")
        if not key:
            # Derive from ticker: first two parts (e.g. KXWTI-26MAR25)
            parts = m.ticker.split("-")
            key = "-".join(parts[:2]) if len(parts) >= 2 else m.ticker
        groups.setdefault(key, []).append(m)
    return groups


def find_spread_signals(markets, forecast: Optional[CommodityForecast] = None,
                        min_edge: float = 0.15) -> list[SpreadSignal]:
    """Scan adjacent-strike pairs for pricing incoherencies."""
    signals = []
    groups = _group_by_event(markets)

    for event_key, group in groups.items():
        # Sort by strike ascending
        group.sort(key=lambda m: m.strike_price)

        if len(group) < 2:
            continue

        for i in range(len(group) - 1):
            low, high = group[i], group[i + 1]

            ask_l = low.yes_ask
            ask_h = high.yes_ask
            bid_l = low.yes_bid
            bid_h = high.yes_bid

            # Skip if no liquidity
            if ask_l <= 0 or ask_h <= 0:
                continue

            strike_gap = high.strike_price - low.strike_price
            mtype = getattr(low, "market_type", "")

            # Check 1: Monotonicity violation
            # P(above lower strike) should always >= P(above higher strike)
            # So YES ask for lower strike should be >= YES ask for higher strike
            if ask_h > ask_l + 0.02:  # 2¢ tolerance for spread noise
                severity = min(1.0, (ask_h - ask_l) / 0.20)
                signals.append(SpreadSignal(
                    ticker_low=low.ticker, ticker_high=high.ticker,
                    strike_low=low.strike_price, strike_high=high.strike_price,
                    ask_low=ask_l, ask_high=ask_h,
                    bid_low=bid_l, bid_high=bid_h,
                    signal_type="monotonicity",
                    severity=severity,
                    description=(f"Higher strike ask (${ask_h:.2f}) > lower strike ask "
                                 f"(${ask_l:.2f}) — violates monotonicity"),
                    market_type=mtype, event_ticker=event_key,
                ))

            # Check 2: Spread compression
            # The difference in YES prices between adjacent strikes should
            # reflect the probability mass in that range. If the difference
            # is too small relative to the strike gap, it's suspicious.
            ask_diff = ask_l - ask_h
            if 0 < ask_diff < 0.02 and strike_gap >= 1.0:
                severity = min(1.0, (0.02 - ask_diff) / 0.02 * (strike_gap / 2.0))
                signals.append(SpreadSignal(
                    ticker_low=low.ticker, ticker_high=high.ticker,
                    strike_low=low.strike_price, strike_high=high.strike_price,
                    ask_low=ask_l, ask_high=ask_h,
                    bid_low=bid_l, bid_high=bid_h,
                    signal_type="compression",
                    severity=severity,
                    description=(f"Spread only ${ask_diff:.2f} across "
                                 f"${strike_gap:.2f} gap — likely mispriced"),
                    market_type=mtype, event_ticker=event_key,
                ))

            # Check 3: Wide gap (opportunity)
            # If ask_diff is very large, one side is likely cheap
            if ask_diff > min_edge and strike_gap <= 2.0:
                severity = min(1.0, ask_diff / 0.30)
                signals.append(SpreadSignal(
                    ticker_low=low.ticker, ticker_high=high.ticker,
                    strike_low=low.strike_price, strike_high=high.strike_price,
                    ask_low=ask_l, ask_high=ask_h,
                    bid_low=bid_l, bid_high=bid_h,
                    signal_type="wide_gap",
                    severity=severity,
                    description=(f"${ask_diff:.2f} spread across ${strike_gap:.2f} "
                                 f"gap — one side likely cheap"),
                    market_type=mtype, event_ticker=event_key,
                ))

    signals.sort(key=lambda s: s.severity, reverse=True)
    return signals


def generate_spread_confirmed_signals(
    markets, forecast: Optional[CommodityForecast] = None,
) -> dict[str, SpreadConfirmation]:
    """Return per-ticker confirmations that boost existing directional signals."""
    confirmations: dict[str, SpreadConfirmation] = {}
    spread_signals = find_spread_signals(markets, forecast)

    for ss in spread_signals:
        if ss.signal_type == "monotonicity":
            # The higher-strike is overpriced → boost the lower strike YES
            if ss.ticker_low not in confirmations:
                confirmations[ss.ticker_low] = SpreadConfirmation(
                    ticker=ss.ticker_low,
                    boost=min(0.10, ss.severity * 0.10),
                    reason=f"monotonicity violation vs {ss.ticker_high}",
                )
            # The higher-strike YES is overpriced → boost NO side
            if ss.ticker_high not in confirmations:
                confirmations[ss.ticker_high] = SpreadConfirmation(
                    ticker=ss.ticker_high,
                    boost=min(0.08, ss.severity * 0.08),
                    reason=f"overpriced vs {ss.ticker_low} (monotonicity)",
                )

        elif ss.signal_type == "wide_gap":
            # The higher strike is likely cheap
            if ss.ticker_high not in confirmations:
                confirmations[ss.ticker_high] = SpreadConfirmation(
                    ticker=ss.ticker_high,
                    boost=min(0.05, ss.severity * 0.05),
                    reason=f"wide spread vs {ss.ticker_low}",
                )

    if confirmations:
        logger.info("Spread analysis: %d confirmations from %d signals",
                     len(confirmations), len(spread_signals))
    return confirmations
