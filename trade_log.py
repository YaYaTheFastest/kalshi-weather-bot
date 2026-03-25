#!/usr/bin/env python3
"""
trade_log.py — Transaction log and P&L tracker for the Kalshi trading bot.

Reads trade history from Kalshi API and maintains a persistent JSON log.
Run periodically or on-demand to update.

Usage:
  python3 trade_log.py           # Update log and print summary
  python3 trade_log.py --full    # Show all individual trades
"""
import argparse, json, logging, os, sys
from datetime import datetime, timezone, date
from dataclasses import dataclass, asdict

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)

from kalshi_client import _get, get_balance, get_positions

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("trade_log")

LOG_FILE = os.path.join(_dir, "trade_history.json")


def load_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            return json.load(f)
    return {"trades": [], "last_updated": None, "summary": {}}


def save_log(data):
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(LOG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def fetch_fills():
    """Fetch trade fills from Kalshi API."""
    all_fills = []
    cursor = None
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = _get("/portfolio/fills", params=params)
        if not data:
            break
        fills = data.get("fills", [])
        all_fills.extend(fills)
        cursor = data.get("cursor")
        if not cursor or not fills:
            break
    return all_fills


def fetch_settled_positions():
    """Fetch settlement history from portfolio."""
    data = _get("/portfolio/settlements")
    if not data:
        return []
    return data.get("settlements", [])


def update_log():
    """Pull latest fills and settlements, update the trade log."""
    trade_log = load_log()
    existing_ids = {t.get("fill_id") for t in trade_log["trades"] if t.get("fill_id")}

    # Fetch fills
    fills = fetch_fills()
    log.info("Fetched %d fills from Kalshi", len(fills))

    new_count = 0
    for f in fills:
        fill_id = f.get("trade_id") or f.get("order_id") or f.get("id", "")
        if fill_id in existing_ids:
            continue

        ticker = f.get("ticker", "")
        action = f.get("action", "")  # buy or sell
        side = f.get("side", "")  # yes or no
        # API v2 returns count_fp and prices in dollars (as strings)
        count = int(float(f.get("count_fp", 0) or f.get("count", 0) or 0))
        # Prices are already in dollars (strings like "0.14")
        yes_price_str = f.get("yes_price_dollars") or f.get("yes_price", 0)
        no_price_str = f.get("no_price_dollars") or f.get("no_price", 0)
        price_dollars = float(yes_price_str or 0) or float(no_price_str or 0)
        cost = count * price_dollars
        created = f.get("created_time", "")

        # Determine market type
        tu = ticker.upper()
        if "KXHIGH" in tu:
            market_type = "weather"
        elif "KXAAAGASW" in tu:
            market_type = "gas_weekly"
        elif "KXAAAGASM" in tu:
            market_type = "gas_monthly"
        elif "KXWTI" in tu:
            market_type = "oil"
        else:
            market_type = "other"

        trade = {
            "fill_id": fill_id,
            "ticker": ticker,
            "market_type": market_type,
            "action": action,
            "side": side,
            "contracts": count,
            "price": price_dollars,
            "cost": round(cost, 4),
            "time": created,
            "settled": False,
            "settlement_result": None,
            "pnl": None,
        }
        trade_log["trades"].append(trade)
        new_count += 1

    log.info("Added %d new trades", new_count)

    # Check for settlements on existing trades
    positions = get_positions()
    pos_tickers = {p.ticker for p in positions}

    # Also check settled markets for any of our tickers
    for trade in trade_log["trades"]:
        if trade["settled"]:
            continue
        ticker = trade["ticker"]
        # Try to get market status
        market_data = _get(f"/markets/{ticker}")
        if market_data and market_data.get("market"):
            m = market_data["market"]
            status = m.get("status", "")
            result = m.get("result", "")
            if status in ("settled", "determined") and result:
                trade["settled"] = True
                trade["settlement_result"] = result
                # Calculate P&L
                if trade["action"] == "buy" and trade["side"] == "yes":
                    if result == "yes":
                        trade["pnl"] = round(trade["contracts"] * 1.0 - trade["cost"], 4)
                    else:
                        trade["pnl"] = round(-trade["cost"], 4)
                elif trade["action"] == "buy" and trade["side"] == "no":
                    if result == "no":
                        trade["pnl"] = round(trade["contracts"] * 1.0 - trade["cost"], 4)
                    else:
                        trade["pnl"] = round(-trade["cost"], 4)

    # Compute summary
    all_trades = trade_log["trades"]
    settled = [t for t in all_trades if t["settled"]]
    open_trades = [t for t in all_trades if not t["settled"]]
    wins = [t for t in settled if t.get("pnl", 0) and t["pnl"] > 0]
    losses = [t for t in settled if t.get("pnl") is not None and t["pnl"] <= 0]

    total_cost = sum(t["cost"] for t in all_trades if t["action"] == "buy")
    settled_pnl = sum(t["pnl"] for t in settled if t["pnl"] is not None)
    open_cost = sum(t["cost"] for t in open_trades if t["action"] == "buy")

    balance = get_balance()

    summary = {
        "total_trades": len(all_trades),
        "settled_trades": len(settled),
        "open_trades": len(open_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(settled) * 100, 1) if settled else 0,
        "total_invested": round(total_cost, 2),
        "settled_pnl": round(settled_pnl, 2),
        "open_cost": round(open_cost, 2),
        "current_balance": round(balance, 2),
        "by_market": {},
    }

    # Breakdown by market type
    for mtype in set(t["market_type"] for t in all_trades):
        mt_trades = [t for t in all_trades if t["market_type"] == mtype]
        mt_settled = [t for t in mt_trades if t["settled"]]
        mt_wins = [t for t in mt_settled if t.get("pnl", 0) and t["pnl"] > 0]
        mt_pnl = sum(t["pnl"] for t in mt_settled if t["pnl"] is not None)
        summary["by_market"][mtype] = {
            "trades": len(mt_trades),
            "settled": len(mt_settled),
            "wins": len(mt_wins),
            "win_rate": round(len(mt_wins) / len(mt_settled) * 100, 1) if mt_settled else 0,
            "pnl": round(mt_pnl, 2),
        }

    trade_log["summary"] = summary
    save_log(trade_log)
    return trade_log


def print_summary(trade_log, show_full=False):
    s = trade_log["summary"]
    print("=" * 60)
    print("  KALSHI TRADING BOT — P&L REPORT")
    print("=" * 60)
    print(f"  Last updated: {trade_log['last_updated']}")
    print(f"  Current balance: ${s['current_balance']:.2f}")
    print()
    print(f"  OVERALL:")
    print(f"    Total trades:    {s['total_trades']}")
    print(f"    Settled:         {s['settled_trades']} ({s['wins']}W / {s['losses']}L)")
    print(f"    Win rate:        {s['win_rate']}%")
    print(f"    Open:            {s['open_trades']}")
    print(f"    Total invested:  ${s['total_invested']:.2f}")
    print(f"    Settled P&L:     ${s['settled_pnl']:+.2f}")
    print(f"    Open cost:       ${s['open_cost']:.2f}")
    print()

    if s.get("by_market"):
        print(f"  BY MARKET TYPE:")
        print(f"    {'Type':<15} {'Trades':>7} {'W/L':>7} {'WR%':>6} {'P&L':>8}")
        print(f"    {'-'*45}")
        for mtype, ms in sorted(s["by_market"].items()):
            wl = f"{ms['wins']}/{ms['settled'] - ms['wins']}"
            print(f"    {mtype:<15} {ms['trades']:>7} {wl:>7} {ms['win_rate']:>5.0f}% ${ms['pnl']:>+7.2f}")
    print()

    if show_full:
        print(f"  ALL TRADES:")
        print(f"    {'Ticker':<35} {'Type':<12} {'Cost':>7} {'Result':>8} {'P&L':>8}")
        print(f"    {'-'*75}")
        for t in trade_log["trades"]:
            result = t.get("settlement_result", "open") or "open"
            pnl = f"${t['pnl']:+.2f}" if t.get("pnl") is not None else "—"
            print(f"    {t['ticker']:<35} {t['market_type']:<12} ${t['cost']:>6.2f} {result:>8} {pnl:>8}")

    print("=" * 60)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Show all individual trades")
    args = parser.parse_args()

    trade_log = update_log()
    print_summary(trade_log, show_full=args.full)


if __name__ == "__main__":
    main()
