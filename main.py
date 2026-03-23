"""
main.py
-------
Kalshi Trading Bot — main orchestrator.
Supports both weather (KXHIGH) and gas price (KXAAAGASW/KXAAAGASM) markets.

Loop (every SCAN_INTERVAL_SECONDS = 120s by default):
  1. Fetch weather forecasts for all configured cities
  2. Fetch gas price data from AAA
  3. Fetch all open markets from Kalshi (temperature + gas)
  4. Sync open positions with the risk manager
  5. Generate sell signals for existing positions → execute exits
  6. Generate buy signals from forecast vs market comparison → execute buys
  7. Send Telegram scan summary (every N cycles to avoid spam)
  8. Sleep until next cycle

Run modes:
  DRY_RUN=true  (default) — logs all actions, places no real orders
  DRY_RUN=false           — live trading

Usage:
  python main.py

Or with environment overrides:
  DRY_RUN=false SCAN_INTERVAL_SECONDS=60 python main.py
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

# Local modules
import config
import telegram_alerts
from decision_engine import generate_buy_signals, generate_sell_signals
from gas_engine import generate_gas_buy_signals, generate_gas_sell_signals
from gas_markets import get_gas_markets
from gas_scanner import fetch_gas_forecast
from kalshi_client import (
    get_balance,
    get_positions,
    get_temperature_markets,
    place_buy_order,
    place_sell_order,
)
from noaa_scanner import fetch_all_forecasts, fetch_today_forecasts
from risk_manager import RiskLimitExceeded, risk_manager

# ---------------------------------------------------------------------------
# Feature flags — enable/disable market types
# ---------------------------------------------------------------------------
ENABLE_WEATHER: bool = os.getenv("ENABLE_WEATHER", "true").lower() in ("true", "1", "yes")
ENABLE_GAS: bool = os.getenv("ENABLE_GAS", "true").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    """Configure root logger to write to both console and a log file."""
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if config.LOG_FILE:
        handlers.append(logging.FileHandler(config.LOG_FILE, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format=fmt,
        datefmt=date_fmt,
        handlers=handlers,
    )


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    logger.info("Shutdown signal received (%s). Finishing current cycle…", signum)
    _shutdown_requested = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)
# Ignore SIGHUP so nohup works reliably
try:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
except (AttributeError, OSError):
    pass  # SIGHUP not available on all platforms


# ---------------------------------------------------------------------------
# Single scan cycle
# ---------------------------------------------------------------------------

def run_scan_cycle(cycle_number: int) -> dict:
    """
    Execute one full scan-and-trade cycle covering both weather and gas markets.
    Returns a stats dict for the Telegram summary.
    """
    stats = {
        "cities_scanned": 0,
        "weather_markets": 0,
        "gas_markets": 0,
        "buy_signals": 0,
        "sell_signals": 0,
        "buys_executed": 0,
        "sells_executed": 0,
        "errors": 0,
    }

    # ---- 1. Sync positions (shared across all market types) ---------------
    logger.info("=== Cycle %d: Syncing positions ===", cycle_number)
    live_positions = get_positions()
    risk_manager.sync_positions(live_positions)
    held_tickers = {p.ticker for p in live_positions if p.market_exposure > 0}

    # Check kill switch before any trading
    if risk_manager.daily_pnl <= -config.MAX_DAILY_LOSS_USD:
        logger.warning(
            "Daily loss limit reached ($%.2f). Skipping all trading.",
            risk_manager.daily_pnl,
        )
        telegram_alerts.alert_daily_kill_switch(risk_manager.daily_pnl)
        return stats

    balance = get_balance()
    logger.info("Portfolio balance: $%.2f", balance)

    # Collect all buy signals from both market types, then rank them together
    all_buy_signals = []  # list of (signal, market_type) tuples
    all_sell_signals = []  # list of (signal, market_type) tuples

    # ====================================================================
    # WEATHER MARKETS
    # ====================================================================
    if ENABLE_WEATHER:
        logger.info("=== Cycle %d: Weather scan ===", cycle_number)

        # Fetch forecasts
        forecasts_tomorrow = fetch_all_forecasts()
        forecasts_today = fetch_today_forecasts()
        all_forecasts = {}
        all_forecasts.update(forecasts_tomorrow)
        for k, v in forecasts_today.items():
            all_forecasts[f"{k}_today"] = v
        stats["cities_scanned"] = len(forecasts_tomorrow) + len(forecasts_today)
        logger.info(
            "Weather forecasts: %d tomorrow + %d today",
            len(forecasts_tomorrow), len(forecasts_today),
        )

        # Fetch temperature markets
        open_temp_markets = get_temperature_markets()
        stats["weather_markets"] = len(open_temp_markets)

        # Weather sell signals
        weather_sells = generate_sell_signals(live_positions, open_temp_markets)
        for sig in weather_sells:
            all_sell_signals.append((sig, "weather"))

        # Weather buy signals
        weather_buys = generate_buy_signals(all_forecasts, open_temp_markets, held_tickers)
        for sig in weather_buys:
            all_buy_signals.append((sig, "weather"))

    # ====================================================================
    # GAS PRICE MARKETS
    # ====================================================================
    if ENABLE_GAS:
        logger.info("=== Cycle %d: Gas price scan ===", cycle_number)

        # Fetch gas markets from Kalshi
        open_gas_markets = get_gas_markets()
        stats["gas_markets"] = len(open_gas_markets)

        if open_gas_markets:
            # Determine days to settlement for forecast
            # Use the nearest settlement date
            min_days = min(m.days_to_settlement for m in open_gas_markets)

            # Fetch AAA gas price data
            gas_forecast = fetch_gas_forecast(days_to_settlement=min_days)

            if gas_forecast:
                logger.info(
                    "Gas: AAA $%.3f | trend $%+.3f/day | %d markets",
                    gas_forecast.current_price,
                    gas_forecast.daily_change,
                    len(open_gas_markets),
                )

                # Gas sell signals
                gas_sells = generate_gas_sell_signals(live_positions, open_gas_markets)
                for sig in gas_sells:
                    all_sell_signals.append((sig, "gas"))

                # Gas buy signals
                gas_buys = generate_gas_buy_signals(
                    gas_forecast, open_gas_markets, held_tickers
                )
                for sig in gas_buys:
                    all_buy_signals.append((sig, "gas"))
            else:
                logger.warning("Failed to fetch gas price data")
                stats["errors"] += 1
        else:
            logger.info("No open gas markets found")

    # ====================================================================
    # EXECUTE SELLS (exits first — frees up capacity)
    # ====================================================================
    stats["sell_signals"] = len(all_sell_signals)

    for sell_signal, market_type in all_sell_signals:
        ticker = sell_signal.position.ticker
        num_contracts = abs(sell_signal.position.market_exposure)
        bid_cents = max(1, min(99, int(sell_signal.bid_price * 100)))
        proceeds = num_contracts * sell_signal.bid_price

        try:
            risk_manager.check_sell(ticker)
        except RiskLimitExceeded as exc:
            logger.warning("Sell blocked for %s: %s", ticker, exc)
            stats["errors"] += 1
            continue

        result = place_sell_order(
            ticker=ticker,
            yes_price_cents=bid_cents,
            count=num_contracts,
        )

        # Send appropriate alert based on market type
        if market_type == "weather":
            telegram_alerts.alert_sell_executed(sell_signal, result, proceeds)
        else:
            telegram_alerts.alert_gas_sell_executed(sell_signal, result, proceeds)

        if result.success:
            risk_manager.record_sell(ticker, proceeds)
            held_tickers.discard(ticker)
            stats["sells_executed"] += 1
            logger.info("SELL executed: %s × %d @ %d¢", ticker, num_contracts, bid_cents)
        else:
            logger.error("SELL failed for %s: %s", ticker, result.error)
            stats["errors"] += 1

    # ====================================================================
    # EXECUTE BUYS (ranked by edge across all market types)
    # ====================================================================
    # Sort all buy signals by edge descending
    all_buy_signals.sort(key=lambda x: x[0].edge, reverse=True)
    stats["buy_signals"] = len(all_buy_signals)

    for buy_signal, market_type in all_buy_signals:
        if market_type == "weather":
            ticker = buy_signal.market.ticker
            ask_price = buy_signal.market.yes_ask
        else:  # gas
            ticker = buy_signal.market.ticker
            ask_price = buy_signal.market_price  # Could be YES or NO side price

        ask_cents = max(1, min(99, int(ask_price * 100)))

        # Size the position
        num_contracts, cost_usd = risk_manager.compute_position_size(ask_price, balance)
        if num_contracts == 0:
            logger.warning("Position size computed as 0 for %s — skipping", ticker)
            continue

        # Pre-trade risk check
        try:
            risk_manager.check_buy(ticker, cost_usd)
        except RiskLimitExceeded as exc:
            logger.warning("Buy blocked for %s: %s", ticker, exc)
            telegram_alerts.alert_risk_blocked(ticker, str(exc))
            stats["errors"] += 1
            if "Max open positions" in str(exc):
                logger.info("Position limit hit — stopping buy scan for this cycle.")
                break
            continue

        # Execute
        result = place_buy_order(
            ticker=ticker,
            yes_price_cents=ask_cents,
            count=num_contracts,
        )

        # Send appropriate alert
        if market_type == "weather":
            telegram_alerts.alert_buy_executed(buy_signal, result, num_contracts, cost_usd)
        else:
            telegram_alerts.alert_gas_buy_executed(buy_signal, result, num_contracts, cost_usd)

        if result.success:
            risk_manager.record_buy(ticker, cost_usd)
            held_tickers.add(ticker)
            balance -= cost_usd
            stats["buys_executed"] += 1
            logger.info(
                "BUY executed: %s × %d @ %d¢ | cost $%.2f",
                ticker, num_contracts, ask_cents, cost_usd,
            )
        else:
            logger.error("BUY failed for %s: %s", ticker, result.error)
            stats["errors"] += 1

    logger.info("Cycle %d complete: %s", cycle_number, risk_manager.status_summary())
    return stats


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    _setup_logging()

    logger.info("=" * 60)
    logger.info("Kalshi Trading Bot starting up")
    logger.info("Mode: %s", "DRY RUN" if config.DRY_RUN else "LIVE TRADING")
    logger.info("Markets: Weather=%s | Gas=%s", ENABLE_WEATHER, ENABLE_GAS)
    logger.info("Scan interval: %ds", config.SCAN_INTERVAL_SECONDS)
    logger.info("=" * 60)

    telegram_alerts.alert_bot_started()

    cycle = 0
    # Send Telegram summaries every N cycles (not every 2 minutes — too noisy)
    SUMMARY_EVERY_N_CYCLES = 15  # ~30 minutes

    try:
        while not _shutdown_requested:
            cycle += 1
            cycle_start = time.monotonic()

            try:
                stats = run_scan_cycle(cycle)

                # Periodic summary alert
                if cycle % SUMMARY_EVERY_N_CYCLES == 0:
                    telegram_alerts.alert_scan_summary(
                        cities_scanned=stats["cities_scanned"],
                        markets_checked=stats["weather_markets"] + stats["gas_markets"],
                        buy_signals=stats["buy_signals"],
                        sell_signals=stats["sell_signals"],
                        open_positions=risk_manager.open_position_count,
                        daily_pnl=risk_manager.daily_pnl,
                    )

            except Exception as exc:
                logger.exception("Unhandled error in scan cycle %d: %s", cycle, exc)
                telegram_alerts.alert_error(f"scan cycle {cycle}", exc)
                # Back off briefly after an error
                time.sleep(10)

            # Sleep for remainder of scan interval
            elapsed = time.monotonic() - cycle_start
            sleep_time = max(0.0, config.SCAN_INTERVAL_SECONDS - elapsed)
            logger.debug("Cycle %d elapsed %.1fs, sleeping %.1fs", cycle, elapsed, sleep_time)

            # Sleep in small increments to allow graceful shutdown
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
