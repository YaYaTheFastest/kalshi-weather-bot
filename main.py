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
import shutil
import sys
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Ensure local modules are imported from this directory, not stale copies
# ---------------------------------------------------------------------------
_project_dir = os.path.dirname(os.path.abspath(__file__))
# Force this directory to be first on sys.path
if _project_dir not in sys.path or sys.path[0] != _project_dir:
    sys.path.insert(0, _project_dir)
# Clear bytecode cache
for _root, _dirs, _files in os.walk(_project_dir):
    if "__pycache__" in _dirs:
        shutil.rmtree(os.path.join(_root, "__pycache__"), ignore_errors=True)
        _dirs.remove("__pycache__")

# Local modules
import config
import telegram_alerts
from decision_engine import generate_buy_signals, generate_sell_signals
from gas_engine import generate_gas_buy_signals, generate_gas_sell_signals
from gas_markets import get_gas_markets
from gas_scanner import fetch_gas_forecast
from oil_engine import generate_oil_buy_signals, generate_oil_sell_signals
from oil_markets import get_oil_markets
from oil_scanner import fetch_oil_forecast
from spread_engine import find_spread_signals, generate_spread_confirmed_signals
from spread_executor import generate_spread_trades, execute_spread_trade
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
ENABLE_WEATHER: bool = os.getenv("ENABLE_WEATHER", "false").lower() in ("true", "1", "yes")
ENABLE_GAS: bool = os.getenv("ENABLE_GAS", "true").lower() in ("true", "1", "yes")
ENABLE_OIL: bool = os.getenv("ENABLE_OIL", "true").lower() in ("true", "1", "yes")
ENABLE_SPREAD_TRADING: bool = config.ENABLE_SPREAD_TRADING

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
# Local position tracker (prevents duplicate buys when API doesn't return positions)
# ---------------------------------------------------------------------------
_locally_held_tickers: set[str] = set()
_daily_spend: float = 0.0  # Total dollars spent on buys today
_daily_spend_date: str = ""  # Reset when date changes
_cycle_count: int = 0
_failed_sell_tickers: set[str] = set()  # Tickers where sell failed (e.g. cancelled) — skip until next day


# ---------------------------------------------------------------------------
# Weather scan (extracted to its own function to isolate variable scope)
# ---------------------------------------------------------------------------

def _run_weather_scan(
    cycle_number: int,
    live_positions: list,
    held_tickers: set,
    stats: dict,
) -> list:
    """
    Fetch weather forecasts and generate buy/sell signals.
    Returns list of (signal, 'buy'|'sell') tuples.
    """
    result_signals = []

    # Fetch tomorrow's and today's forecasts
    tmrw = fetch_all_forecasts()
    tday = fetch_today_forecasts()

    # Merge into a single dict
    merged = {}
    merged.update(tmrw)
    for city_key, city_forecast in tday.items():
        merged[f"{city_key}_today"] = city_forecast

    stats["cities_scanned"] = len(tmrw) + len(tday)
    logger.info(
        "Weather forecasts: %d tomorrow + %d today",
        len(tmrw), len(tday),
    )

    # Fetch temperature markets
    open_temp_markets = get_temperature_markets()
    stats["weather_markets"] = len(open_temp_markets)

    # Sell signals
    for sig in generate_sell_signals(live_positions, open_temp_markets):
        result_signals.append((sig, "sell"))

    # Buy signals
    for sig in generate_buy_signals(merged, open_temp_markets, held_tickers):
        result_signals.append((sig, "buy"))

    return result_signals


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
        "oil_markets": 0,
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
    api_held = {p.ticker for p in live_positions if p.market_exposure > 0}
    # Merge API positions with locally tracked ones (belt and suspenders)
    held_tickers = api_held | _locally_held_tickers
    logger.info("Held tickers: %d from API, %d local, %d merged",
                len(api_held), len(_locally_held_tickers), len(held_tickers))

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

    # Hard balance floor: never trade if balance < $5
    if balance < 5.0:
        logger.warning("Balance too low ($%.2f). Skipping all trading.", balance)
        return stats

    # Hard daily spend cap: track locally since risk_manager may not see API positions
    global _daily_spend, _daily_spend_date
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _daily_spend_date != today_str:
        _daily_spend = 0.0
        _daily_spend_date = today_str
        _locally_held_tickers.clear()  # Reset local tracker at start of new day
        _failed_sell_tickers.clear()  # Reset failed sell cooldowns
        logger.info("New trading day — reset daily spend and local position tracker")

    if _daily_spend >= config.MAX_DAILY_LOSS_USD:
        logger.warning(
            "Daily spend cap reached ($%.2f >= $%.2f). No more buys today.",
            _daily_spend, config.MAX_DAILY_LOSS_USD,
        )
        return stats

    logger.info("Daily spend: $%.2f / $%.2f limit", _daily_spend, config.MAX_DAILY_LOSS_USD)

    # Collect all buy signals from both market types, then rank them together
    all_buy_signals = []  # list of (signal, market_type) tuples
    all_sell_signals = []  # list of (signal, market_type) tuples
    gas_spreads = []  # spread signals for spread executor
    oil_spreads = []

    # ====================================================================
    # WEATHER MARKETS (every 3rd cycle — NOAA is slower-moving, fewer API calls)
    # ====================================================================
    global _cycle_count
    _cycle_count += 1

    if ENABLE_WEATHER and _cycle_count % 3 == 1:
        logger.info("=== Cycle %d: Weather scan ===", cycle_number)
        weather_signals = _run_weather_scan(cycle_number, live_positions, held_tickers, stats)
        for sig, sig_type in weather_signals:
            if sig_type == "buy":
                all_buy_signals.append((sig, "weather"))
            else:
                all_sell_signals.append((sig, "weather"))
    elif ENABLE_WEATHER:
        logger.info("=== Cycle %d: Weather scan skipped (runs every 3rd cycle) ===", cycle_number)

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

                # Spread analysis — log incoherencies and boost confirmed signals
                gas_spreads = find_spread_signals(open_gas_markets, gas_forecast)
                if gas_spreads:
                    logger.info("Gas spread signals: %d incoherencies detected", len(gas_spreads))
                    for ss in gas_spreads[:3]:
                        logger.info("  %s", ss)
                gas_confirms = generate_spread_confirmed_signals(open_gas_markets, gas_forecast)
                for sig in gas_buys:
                    confirm = gas_confirms.get(sig.market.ticker)
                    if confirm:
                        sig.edge += confirm.boost
                        logger.info("Gas signal boosted: %s +%.2f edge (%s)",
                                    sig.market.ticker, confirm.boost, confirm.reason)
                    all_buy_signals.append((sig, "gas"))
            else:
                logger.warning("Failed to fetch gas price data")
                stats["errors"] += 1
        else:
            logger.info("No open gas markets found")

    # ====================================================================
    # OIL PRICE MARKETS
    # ====================================================================
    if ENABLE_OIL:
        logger.info("=== Cycle %d: Oil price scan ===", cycle_number)

        open_oil_markets = get_oil_markets()
        stats["oil_markets"] = len(open_oil_markets)

        if open_oil_markets:
            min_days = min(m.days_to_settlement for m in open_oil_markets)

            oil_forecast = fetch_oil_forecast(days_to_settlement=min_days)

            if oil_forecast:
                logger.info(
                    "Oil: WTI $%.2f | trend $%+.2f/day | %d markets",
                    oil_forecast.current_price,
                    oil_forecast.daily_change,
                    len(open_oil_markets),
                )

                # Oil sell signals
                oil_sells = generate_oil_sell_signals(live_positions, open_oil_markets)
                for sig in oil_sells:
                    all_sell_signals.append((sig, "oil"))

                # Oil buy signals
                oil_buys = generate_oil_buy_signals(
                    oil_forecast, open_oil_markets, held_tickers
                )

                # Spread analysis — log incoherencies and boost confirmed signals
                oil_spreads = find_spread_signals(open_oil_markets, oil_forecast)
                if oil_spreads:
                    logger.info("Oil spread signals: %d incoherencies detected", len(oil_spreads))
                    for ss in oil_spreads[:3]:
                        logger.info("  %s", ss)
                oil_confirms = generate_spread_confirmed_signals(open_oil_markets, oil_forecast)
                for sig in oil_buys:
                    confirm = oil_confirms.get(sig.market.ticker)
                    if confirm:
                        sig.edge += confirm.boost
                        logger.info("Oil signal boosted: %s +%.2f edge (%s)",
                                    sig.market.ticker, confirm.boost, confirm.reason)
                    all_buy_signals.append((sig, "oil"))
            else:
                logger.warning("Failed to fetch oil price data")
                stats["errors"] += 1
        else:
            logger.info("No open oil markets found")

    # ====================================================================
    # EXECUTE SELLS (exits first — frees up capacity)
    # ====================================================================
    stats["sell_signals"] = len(all_sell_signals)

    for sell_signal, market_type in all_sell_signals:
        ticker = sell_signal.position.ticker

        # Skip tickers where a previous sell was cancelled (e.g. insufficient funds)
        # to avoid spamming Kalshi with repeated failing orders
        if ticker in _failed_sell_tickers:
            logger.debug("Sell skipped %s: previously failed, on cooldown", ticker)
            continue

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
        elif market_type == "oil":
            telegram_alerts.alert_oil_sell_executed(sell_signal, result, proceeds)
        else:
            telegram_alerts.alert_gas_sell_executed(sell_signal, result, proceeds)

        if result.success:
            risk_manager.record_sell(ticker, proceeds)
            held_tickers.discard(ticker)
            stats["sells_executed"] += 1
            logger.info("SELL executed: %s × %d @ %d¢", ticker, num_contracts, bid_cents)
        else:
            logger.error("SELL failed for %s: %s", ticker, result.error)
            _failed_sell_tickers.add(ticker)
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
        else:  # gas or oil — commodity markets
            ticker = buy_signal.market.ticker
            ask_price = buy_signal.market_price  # Could be YES or NO side price

        ask_cents = max(1, min(99, int(ask_price * 100)))

        # Check daily spend cap before sizing
        remaining_budget = config.MAX_DAILY_LOSS_USD - _daily_spend
        if remaining_budget < 0.50:
            logger.info("Daily spend cap nearly reached ($%.2f spent). Stopping buys.", _daily_spend)
            break

        # Size the position (capped by remaining daily budget)
        effective_max = min(config.MAX_POSITION_USD, remaining_budget, balance)
        num_contracts = max(1, int(effective_max / ask_price)) if ask_price > 0 else 0
        cost_usd = num_contracts * ask_price
        if cost_usd > effective_max:
            num_contracts = max(1, int(effective_max / ask_price))
            cost_usd = num_contracts * ask_price
        if num_contracts == 0 or cost_usd > effective_max:
            logger.warning("Position size 0 or exceeds budget for %s — skipping", ticker)
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
        elif market_type == "oil":
            telegram_alerts.alert_oil_buy_executed(buy_signal, result, num_contracts, cost_usd)
        else:
            telegram_alerts.alert_gas_buy_executed(buy_signal, result, num_contracts, cost_usd)

        if result.success:
            risk_manager.record_buy(ticker, cost_usd)
            held_tickers.add(ticker)
            _locally_held_tickers.add(ticker)
            _daily_spend += cost_usd
            balance -= cost_usd
            stats["buys_executed"] += 1
            logger.info(
                "BUY executed: %s × %d @ %d¢ | cost $%.2f",
                ticker, num_contracts, ask_cents, cost_usd,
            )
        else:
            logger.error("BUY failed for %s: %s", ticker, result.error)
            stats["errors"] += 1
            # Stop trying if balance is insufficient — don't spam failed orders
            if result.error and "insufficient_balance" in str(result.error):
                logger.warning("Insufficient balance — stopping all buys for this cycle")
                break

    # ====================================================================
    # EXECUTE SPREAD TRADES (market-neutral arbitrage)
    # ====================================================================
    if ENABLE_SPREAD_TRADING:
        all_spread_signals = []
        if ENABLE_GAS and gas_spreads:
            all_spread_signals.extend(gas_spreads)
        if ENABLE_OIL and oil_spreads:
            all_spread_signals.extend(oil_spreads)

        if all_spread_signals:
            logger.info("=== Spread trading: %d signals to evaluate ===", len(all_spread_signals))
            spread_trades = generate_spread_trades(all_spread_signals, held_tickers, balance)

            if spread_trades:
                logger.info("Generated %d actionable spread trades", len(spread_trades))

            spread_count = 0
            for trade in spread_trades[:config.SPREAD_MAX_TRADES_PER_CYCLE]:
                success, cost = execute_spread_trade(
                    trade=trade,
                    held_tickers=held_tickers,
                    balance=balance,
                    daily_spend=_daily_spend,
                )
                if success:
                    spread_count += 1
                    _daily_spend += cost
                    balance -= cost
                    _locally_held_tickers.add(trade.buy_leg.ticker)
                    logger.info(
                        "SPREAD executed: %s | buy %s | sell %s | profit $%.2f",
                        trade.trade_type,
                        trade.buy_leg.ticker,
                        trade.sell_leg.ticker if trade.sell_leg else "(none)",
                        trade.expected_profit,
                    )

            if spread_count:
                stats["buys_executed"] += spread_count
                logger.info("Spread trades executed this cycle: %d", spread_count)
        else:
            logger.debug("No spread signals to evaluate this cycle")

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
    logger.info("Markets: Weather=%s | Gas=%s | Oil=%s | Spreads=%s",
                ENABLE_WEATHER, ENABLE_GAS, ENABLE_OIL, ENABLE_SPREAD_TRADING)
    logger.info("Scan interval: %ds", config.SCAN_INTERVAL_SECONDS)
    logger.info("PID: %d", os.getpid())
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
                        markets_checked=stats["weather_markets"] + stats["gas_markets"] + stats["oil_markets"],
                        buy_signals=stats["buy_signals"],
                        sell_signals=stats["sell_signals"],
                        open_positions=risk_manager.open_position_count,
                        daily_pnl=risk_manager.daily_pnl,
                    )

            except Exception as exc:
                import traceback
                tb_str = traceback.format_exc()
                logger.exception("Unhandled error in scan cycle %d: %s", cycle, exc)
                # Send full traceback to Telegram for remote debugging
                telegram_alerts.alert_error_with_traceback(f"scan cycle {cycle}", exc, tb_str)
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
