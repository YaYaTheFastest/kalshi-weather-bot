"""
telegram_alerts.py
------------------
Sends formatted trade alerts to a Telegram chat via the Bot API.

Alert types:
  - Bot started / stopped
  - Buy order placed (or DRY RUN simulated)
  - Sell order placed (or DRY RUN simulated)
  - Risk limit blocked a trade
  - Daily P&L summary
  - Error / exception notifications

All sends are best-effort — a Telegram failure never crashes the main loop.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import requests

import config
from decision_engine import BuySignal, SellSignal
from kalshi_client import OrderResult

# Lazy imports to avoid circular dependency
def _get_gas_signal_classes():
    from gas_engine import GasBuySignal, GasSellSignal
    return GasBuySignal, GasSellSignal

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


# ---------------------------------------------------------------------------
# Low-level sender
# ---------------------------------------------------------------------------

def _send(text: str, parse_mode: str = "HTML") -> bool:
    """
    Send a message to the configured Telegram chat.
    Returns True on success, False on error.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured — skipping alert.")
        return False

    url = f"{TELEGRAM_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
        return False


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _dry_tag() -> str:
    return "🔵 <b>[DRY RUN]</b> " if config.DRY_RUN else ""


# ---------------------------------------------------------------------------
# Alert builders
# ---------------------------------------------------------------------------

def alert_bot_started() -> None:
    import os
    mode = "DRY RUN (simulation only)" if config.DRY_RUN else "LIVE TRADING"
    weather_on = os.getenv("ENABLE_WEATHER", "true").lower() in ("true", "1", "yes")
    gas_on = os.getenv("ENABLE_GAS", "true").lower() in ("true", "1", "yes")
    markets_str = []
    if weather_on:
        markets_str.append(f"Weather ({len(config.CITIES)} cities)")
    if gas_on:
        markets_str.append("Gas Prices (weekly + monthly)")
    oil_on = os.getenv("ENABLE_OIL", "true").lower() in ("true", "1", "yes")
    if oil_on:
        markets_str.append("WTI Oil (daily + weekly)")
    text = (
        f"🤖 <b>Kalshi Trading Bot Started</b>\n"
        f"Mode: <b>{mode}</b>\n"
        f"Markets: {', '.join(markets_str)}\n"
        f"Time: {_now_str()}\n\n"
        f"Scan interval: {config.SCAN_INTERVAL_SECONDS}s\n"
        f"Buy threshold: conf &gt;{config.BUY_CONFIDENCE_THRESHOLD:.0%} @ &lt;${config.BUY_MAX_PRICE:.2f}\n"
        f"Sell threshold: bid &gt;${config.SELL_MIN_PRICE:.2f}\n"
        f"Max position: ${config.MAX_POSITION_USD:.2f} | "
        f"Max positions: {config.MAX_OPEN_POSITIONS} | "
        f"Daily loss limit: -${config.MAX_DAILY_LOSS_USD:.2f}"
    )
    _send(text)


def alert_bot_stopped(reason: str = "Manual shutdown") -> None:
    text = (
        f"🛑 <b>Kalshi Weather Bot Stopped</b>\n"
        f"Reason: {reason}\n"
        f"Time: {_now_str()}"
    )
    _send(text)


def alert_buy_executed(signal: BuySignal, result: OrderResult, num_contracts: int, cost_usd: float) -> None:
    status = "✅ Filled" if result.success else "❌ Failed"
    bucket_str = _bucket_display(signal.market.bucket_low, signal.market.bucket_high)
    text = (
        f"{_dry_tag()}📈 <b>BUY ORDER {status}</b>\n"
        f"Ticker: <code>{signal.market.ticker}</code>\n"
        f"City: {signal.city_name}\n"
        f"Bucket: {bucket_str}\n"
        f"Contracts: {num_contracts}\n"
        f"Price: ${signal.market_price:.2f} | Cost: ${cost_usd:.2f}\n"
        f"NOAA confidence: {signal.noaa_confidence:.1%}\n"
        f"Forecast high: {signal.forecasted_high:.0f}°F\n"
        f"Edge: {signal.edge:.1%}\n"
        f"Order ID: {result.order_id or 'n/a'}\n"
        f"Time: {_now_str()}"
    )
    if not result.success:
        text += f"\nError: {result.error}"
    _send(text)


def alert_sell_executed(signal: SellSignal, result: OrderResult, proceeds_usd: float) -> None:
    status = "✅ Filled" if result.success else "❌ Failed"
    text = (
        f"{_dry_tag()}📉 <b>SELL ORDER {status}</b>\n"
        f"Ticker: <code>{signal.position.ticker}</code>\n"
        f"Reason: {signal.reason}\n"
        f"Bid price: ${signal.bid_price:.2f}\n"
        f"Proceeds: ${proceeds_usd:.2f}\n"
        f"Exposure: ${signal.position.market_exposure_dollars:.2f}\n"
        f"Order ID: {result.order_id or 'n/a'}\n"
        f"Time: {_now_str()}"
    )
    if not result.success:
        text += f"\nError: {result.error}"
    _send(text)


def alert_risk_blocked(ticker: str, reason: str) -> None:
    text = (
        f"⚠️ <b>Trade Blocked by Risk Manager</b>\n"
        f"Ticker: <code>{ticker}</code>\n"
        f"Reason: {reason}\n"
        f"Time: {_now_str()}"
    )
    _send(text)


def alert_daily_kill_switch(daily_pnl: float) -> None:
    text = (
        f"🚨 <b>DAILY LOSS LIMIT HIT — ALL TRADING HALTED</b>\n"
        f"Daily P&amp;L: ${daily_pnl:.2f}\n"
        f"Limit: -${config.MAX_DAILY_LOSS_USD:.2f}\n"
        f"The bot will stop placing orders for the rest of the day.\n"
        f"Time: {_now_str()}"
    )
    _send(text)


def alert_scan_summary(
    cities_scanned: int,
    markets_checked: int,
    buy_signals: int,
    sell_signals: int,
    open_positions: int,
    daily_pnl: float,
) -> None:
    text = (
        f"📊 <b>Scan Complete</b>\n"
        f"Cities: {cities_scanned} | Markets: {markets_checked}\n"
        f"Buy signals: {buy_signals} | Sell signals: {sell_signals}\n"
        f"Open positions: {open_positions}/{config.MAX_OPEN_POSITIONS}\n"
        f"Daily P&amp;L: ${daily_pnl:.2f}\n"
        f"Time: {_now_str()}"
    )
    _send(text)


def alert_error(context: str, exc: Exception) -> None:
    text = (
        f"❗ <b>Bot Error</b>\n"
        f"Context: {context}\n"
        f"Error: {type(exc).__name__}: {str(exc)[:300]}\n"
        f"Time: {_now_str()}"
    )
    _send(text)


def alert_error_with_traceback(context: str, exc: Exception, tb_str: str) -> None:
    # Truncate traceback to fit Telegram's 4096 char limit
    tb_truncated = tb_str[-2000:] if len(tb_str) > 2000 else tb_str
    text = (
        f"❗ <b>Bot Error</b>\n"
        f"Context: {context}\n"
        f"Error: {type(exc).__name__}: {str(exc)[:300]}\n"
        f"Time: {_now_str()}\n\n"
        f"<pre>{tb_truncated}</pre>"
    )
    _send(text)


# ---------------------------------------------------------------------------
# Gas price alerts
# ---------------------------------------------------------------------------

def alert_gas_buy_executed(signal, result: OrderResult, num_contracts: int, cost_usd: float) -> None:
    status = "✅ Filled" if result.success else "❌ Failed"
    text = (
        f"{_dry_tag()}⛽📈 <b>GAS BUY ORDER {status}</b>\n"
        f"Ticker: <code>{signal.market.ticker}</code>\n"
        f"Strike: ${signal.market.strike_price:.3f}\n"
        f"Direction: {signal.direction.upper()}\n"
        f"Type: {signal.market.market_type}\n"
        f"Contracts: {num_contracts}\n"
        f"Price: ${signal.market_price:.2f} | Cost: ${cost_usd:.2f}\n"
        f"Model confidence: {signal.model_confidence:.1%}\n"
        f"Current AAA: ${signal.current_gas_price:.3f}\n"
        f"Projected: ${signal.projected_price:.3f}\n"
        f"Edge: {signal.edge:.1%}\n"
        f"Settles in: {signal.market.days_to_settlement}d\n"
        f"Order ID: {result.order_id or 'n/a'}\n"
        f"Time: {_now_str()}"
    )
    if not result.success:
        text += f"\nError: {result.error}"
    _send(text)


def alert_gas_sell_executed(signal, result: OrderResult, proceeds_usd: float) -> None:
    status = "✅ Filled" if result.success else "❌ Failed"
    text = (
        f"{_dry_tag()}⛽📉 <b>GAS SELL ORDER {status}</b>\n"
        f"Ticker: <code>{signal.position.ticker}</code>\n"
        f"Reason: {signal.reason}\n"
        f"Bid price: ${signal.bid_price:.2f}\n"
        f"Proceeds: ${proceeds_usd:.2f}\n"
        f"Order ID: {result.order_id or 'n/a'}\n"
        f"Time: {_now_str()}"
    )
    if not result.success:
        text += f"\nError: {result.error}"
    _send(text)


# ---------------------------------------------------------------------------
# Oil price alerts
# ---------------------------------------------------------------------------

def alert_oil_buy_executed(signal, result: OrderResult, num_contracts: int, cost_usd: float) -> None:
    status = "\u2705 Filled" if result.success else "\u274c Failed"
    text = (
        f"{_dry_tag()}\U0001f6e2\ufe0f\U0001f4c8 <b>OIL BUY ORDER {status}</b>\n"
        f"Ticker: <code>{signal.market.ticker}</code>\n"
        f"Strike: ${signal.market.strike_price:.2f}\n"
        f"Direction: {signal.direction.upper()}\n"
        f"Type: {signal.market.market_type}\n"
        f"Contracts: {num_contracts}\n"
        f"Price: ${signal.market_price:.2f} | Cost: ${cost_usd:.2f}\n"
        f"Model confidence: {signal.model_confidence:.1%}\n"
        f"Current WTI: ${signal.current_oil_price:.2f}\n"
        f"Projected: ${signal.projected_price:.2f}\n"
        f"Edge: {signal.edge:.1%}\n"
        f"Settles in: {signal.market.days_to_settlement}d\n"
        f"Order ID: {result.order_id or 'n/a'}\n"
        f"Time: {_now_str()}"
    )
    if not result.success:
        text += f"\nError: {result.error}"
    _send(text)


def alert_oil_sell_executed(signal, result: OrderResult, proceeds_usd: float) -> None:
    status = "\u2705 Filled" if result.success else "\u274c Failed"
    text = (
        f"{_dry_tag()}\U0001f6e2\ufe0f\U0001f4c9 <b>OIL SELL ORDER {status}</b>\n"
        f"Ticker: <code>{signal.position.ticker}</code>\n"
        f"Reason: {signal.reason}\n"
        f"Bid price: ${signal.bid_price:.2f}\n"
        f"Proceeds: ${proceeds_usd:.2f}\n"
        f"Order ID: {result.order_id or 'n/a'}\n"
        f"Time: {_now_str()}"
    )
    if not result.success:
        text += f"\nError: {result.error}"
    _send(text)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bucket_display(low: Optional[float], high: Optional[float]) -> str:
    """Human-readable temperature bucket string."""
    import math
    if low is None or high is None:
        return "unknown"
    lo_str = f"{low:.0f}°F" if not math.isinf(low) else "-∞"
    hi_str = f"{high:.0f}°F" if not math.isinf(high) else "+∞"
    return f"{lo_str} – {hi_str}"
