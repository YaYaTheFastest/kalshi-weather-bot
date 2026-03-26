#!/usr/bin/env python3
"""
backtest_silver.py — Backtest the silver price model against settled Kalshi markets.

Uses:
- Kalshi API for settled KXSILVERD/KXSILVERW/KXSILVERMON markets
- Yahoo Finance for historical silver prices (SI=F)
- Same CommodityForecast model as live trading

Usage:
  python3 backtest_silver.py              # Default 90 days
  python3 backtest_silver.py --optimize   # Full parameter sweep
  python3 backtest_silver.py --days 180   # Longer lookback
"""
import argparse, json, logging, math, os, sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional
import requests

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path or sys.path[0] != _dir:
    sys.path.insert(0, _dir)

from price_model import CommodityForecast, compute_residual_volatility
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest_silver")


# ── Yahoo Finance historical data ────────────────────────────────────────

@dataclass
class DailyPrice:
    dt: date
    close: float

def fetch_yahoo_history(ticker: str = "SI=F", days: int = 200) -> list[DailyPrice]:
    """Fetch daily closing prices from Yahoo Finance."""
    end = int(datetime.now(timezone.utc).timestamp())
    start = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"period1": start, "period2": end, "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error("Yahoo Finance fetch failed for %s: %s", ticker, e)
        return []
    
    result_data = data.get("chart", {}).get("result", [])
    if not result_data:
        return []
    
    timestamps = result_data[0].get("timestamp", [])
    closes = result_data[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
    
    prices = []
    for ts, close in zip(timestamps, closes):
        if close is not None:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            prices.append(DailyPrice(dt, float(close)))
    
    prices.sort(key=lambda p: p.dt)
    log.info("Yahoo %s: %d daily prices (%s to %s)", ticker, len(prices),
             prices[0].dt if prices else "?", prices[-1].dt if prices else "?")
    return prices


def find_price(prices: list[DailyPrice], target: date, max_lookback: int = 5) -> Optional[float]:
    """Find the closest price on or before target date."""
    for i in range(max_lookback):
        d = target - timedelta(days=i)
        for p in prices:
            if p.dt == d:
                return p.close
    return None


def find_price_pair(prices: list[DailyPrice], target: date, days_before: int = 3):
    """Find (current_price, week_ago_price) relative to days_before settlement."""
    ref_date = target - timedelta(days=days_before)
    current = find_price(prices, ref_date)
    week_ago = find_price(prices, ref_date - timedelta(days=7))
    yesterday = find_price(prices, ref_date - timedelta(days=1))
    if current and week_ago and yesterday:
        return current, yesterday, week_ago
    return None


# ── Kalshi settled markets ───────────────────────────────────────────────

def fetch_settled_silver_markets():
    """Fetch all settled silver markets from Kalshi API."""
    try:
        from kalshi_client import _get
    except:
        log.warning("Kalshi API not available")
        return []
    
    all_markets = []
    series_list = ["KXSILVERD", "KXSILVERW", "KXSILVERMON"]
    
    for series in series_list:
        cursor = None
        while True:
            params = {"status": "settled", "limit": 1000, "series_ticker": series}
            if cursor:
                params["cursor"] = cursor
            data = _get("/markets", params=params)
            if not data:
                break
            batch = data.get("markets", [])
            all_markets.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
    
    log.info("Fetched %d settled silver markets", len(all_markets))
    return all_markets


@dataclass
class SettledMarket:
    ticker: str
    category: str
    strike: float
    settle_date: date
    settled_yes: bool

def parse_silver_markets(raw, lookback_days=90):
    cutoff = date.today() - timedelta(days=lookback_days)
    results = []
    for m in raw:
        ticker = m.get("ticker", "")
        result_str = m.get("result", "").lower()
        if result_str not in ("yes", "no"):
            continue
        parts = ticker.split("-")
        if len(parts) < 3:
            continue
        try:
            # Date part may include extra chars (e.g., "26MAR2617")
            date_str = parts[1][:7]  # Take first 7 chars for YYMmmDD
            sd = datetime.strptime(date_str, "%y%b%d").date()
        except:
            continue
        if sd < cutoff:
            continue
        try:
            price_str = "-".join(parts[2:])
            if price_str.upper().startswith("T"):
                price_str = price_str[1:]
            strike = float(price_str)
        except:
            continue
        
        tu = ticker.upper()
        if "KXSILVERD" in tu:
            cat = "silver_daily"
        elif "KXSILVERW" in tu:
            cat = "silver_weekly"
        elif "KXSILVERMON" in tu:
            cat = "silver_monthly"
        else:
            continue
        
        results.append(SettledMarket(ticker, cat, strike, sd, result_str == "yes"))
    
    log.info("Parsed %d silver markets in %d-day window", len(results), lookback_days)
    return results


# ── Core analysis ────────────────────────────────────────────────────────

def run_analysis(markets, prices, dampening=0.6):
    records = []
    
    for m in markets:
        days_before = {"silver_daily": 1, "silver_weekly": 3, "silver_monthly": 7}.get(m.category, 3)
        pair = find_price_pair(prices, m.settle_date, days_before=days_before)
        if not pair:
            continue
        cur, yest, wk = pair
        vol = compute_residual_volatility(cur, yest, wk, vol_floor=0.15)  # Silver vol floor $0.15
        
        fc = CommodityForecast(cur, yest, wk, wk, cur - yest, cur - wk, vol,
                               m.settle_date - timedelta(days=days_before), days_before,
                               drift_dampening=dampening)
        conf = fc.confidence_above(m.strike)
        
        records.append({
            "ticker": m.ticker,
            "category": m.category,
            "strike": m.strike,
            "settle_date": m.settle_date.isoformat(),
            "settled_yes": m.settled_yes,
            "model_confidence": round(conf, 4),
            "price_at_entry": cur,
            "price_week_ago": wk,
            "days_before": days_before,
        })
    
    return records


def compute_stats(records, min_edge, min_conf, max_ask):
    trades = []
    for r in records:
        conf = r["model_confidence"]
        hypothetical_ask = max(0.01, conf - min_edge)
        if hypothetical_ask > max_ask or conf < min_conf or hypothetical_ask <= 0.01:
            continue
        pnl = (1.0 - hypothetical_ask) if r["settled_yes"] else -hypothetical_ask
        trades.append({
            "ticker": r["ticker"], "category": r["category"],
            "confidence": conf, "hypothetical_ask": round(hypothetical_ask, 4),
            "settled_yes": r["settled_yes"], "pnl": round(pnl, 4),
            "price_at_entry": r["price_at_entry"], "strike": r["strike"],
        })
    return trades


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--optimize", action="store_true")
    args = parser.parse_args()
    
    log.info("Silver Backtest: lookback=%dd, optimize=%s", args.days, args.optimize)
    
    # Fetch data
    raw = fetch_settled_silver_markets()
    markets = parse_silver_markets(raw, args.days)
    prices = fetch_yahoo_history("SI=F", days=args.days + 30)
    
    if not markets:
        log.warning("No settled silver markets found")
        return
    if not prices:
        log.warning("No price data from Yahoo Finance")
        return
    
    # Show market distribution
    by_cat = defaultdict(int)
    for m in markets:
        by_cat[m.category] += 1
    
    print("=" * 70)
    print("  SILVER BACKTEST — MODEL ACCURACY & P&L ANALYSIS")
    print("=" * 70)
    print(f"\n  Markets: {len(markets)} settled ({', '.join(f'{k}={v}' for k, v in sorted(by_cat.items()))})")
    print(f"  Price data: {len(prices)} days ({prices[0].dt} to {prices[-1].dt})")
    print(f"  Current silver: ${prices[-1].close:.2f}/oz")
    
    # Run analysis
    records = run_analysis(markets, prices, dampening=0.6)
    print(f"  Records with matching prices: {len(records)}")
    
    if not records:
        print("\n  No records to analyze (price data gaps)")
        return
    
    # Accuracy by confidence bucket
    buckets = defaultdict(lambda: {"total": 0, "correct": 0})
    for r in records:
        conf = r["model_confidence"]
        for lo, hi, label in [(0, 0.2, "0-20%"), (0.2, 0.4, "20-40%"), (0.4, 0.6, "40-60%"),
                               (0.6, 0.8, "60-80%"), (0.8, 1.01, "80-100%")]:
            if lo <= conf < hi:
                buckets[label]["total"] += 1
                predicted_yes = conf > 0.5
                actual_yes = r["settled_yes"]
                if predicted_yes == actual_yes:
                    buckets[label]["correct"] += 1
                break
    
    print(f"\n  MODEL ACCURACY (dampening=0.6):")
    print(f"  {'Confidence':>12} {'Markets':>8} {'Correct':>8} {'Accuracy':>9}")
    print(f"  " + "-" * 42)
    for label in ["0-20%", "20-40%", "40-60%", "60-80%", "80-100%"]:
        b = buckets[label]
        if b["total"] > 0:
            acc = b["correct"] / b["total"]
            print(f"  {label:>12} {b['total']:>8} {b['correct']:>8} {acc:>8.1%}")
    
    # Win rate for YES buys
    print(f"\n  YES CONTRACT WIN RATE:")
    print(f"  {'Min Conf':>10} {'Contracts':>10} {'Settled Yes':>12} {'Win Rate':>9}")
    print(f"  " + "-" * 45)
    for min_c in [0.5, 0.6, 0.7, 0.8, 0.9]:
        qualifying = [r for r in records if r["model_confidence"] >= min_c]
        yes_count = sum(1 for r in qualifying if r["settled_yes"])
        if qualifying:
            print(f"  {min_c:>9.0%} {len(qualifying):>10} {yes_count:>12} {yes_count/len(qualifying):>8.1%}")
    
    # Parameter sweep
    dampening_values = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8] if args.optimize else [0.5, 0.6, 0.7]
    edge_values = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35] if args.optimize else [0.15, 0.20, 0.25, 0.30]
    conf_values = [0.50, 0.55, 0.60, 0.65, 0.70, 0.80] if args.optimize else [0.50, 0.60, 0.70]
    ask_values = [0.30, 0.40, 0.50, 0.60] if args.optimize else [0.40, 0.50, 0.60]
    
    print(f"\n  SIMULATED P&L BY PARAMETERS:")
    print(f"  {'Edge':>6} {'Conf':>6} {'MaxAsk':>7} {'Damp':>5} {'Trades':>7} {'Wins':>5} {'WinRate':>8} {'P&L':>8} {'$/Trade':>8}")
    print(f"  " + "-" * 68)
    
    all_results = []
    for damp in dampening_values:
        recs = run_analysis(markets, prices, dampening=damp)
        for edge in edge_values:
            for conf in conf_values:
                for ask in ask_values:
                    trades = compute_stats(recs, edge, conf, ask)
                    bought = [t for t in trades if t["pnl"] != 0]
                    if len(bought) < 3:
                        continue
                    total_pnl = sum(t["pnl"] for t in bought)
                    wins = sum(1 for t in bought if t["pnl"] > 0)
                    wr = wins / len(bought)
                    per_trade = total_pnl / len(bought)
                    
                    result = {
                        "min_edge": edge, "min_confidence": conf,
                        "max_ask": ask, "drift_dampening": damp,
                        "trades": len(bought), "wins": wins,
                        "win_rate": round(wr, 3),
                        "total_pnl": round(total_pnl, 2),
                        "per_trade": round(per_trade, 4),
                    }
                    all_results.append(result)
    
    all_results.sort(key=lambda x: x["total_pnl"], reverse=True)
    
    for r in all_results[:20]:
        print(f"  {r['min_edge']:>5.0%} {r['min_confidence']:>5.0%} {r['max_ask']:>6.0%} "
              f"{r['drift_dampening']:>5.1f} {r['trades']:>7} {r['wins']:>5} "
              f"{r['win_rate']:>7.0%} ${r['total_pnl']:>+7.2f} ${r['per_trade']:>+7.4f}")
    
    if all_results:
        best = all_results[0]
        print(f"\n  BEST: edge≥{best['min_edge']:.0%}, conf≥{best['min_confidence']:.0%}, "
              f"ask≤{best['max_ask']:.0%}, damp={best['drift_dampening']}")
        print(f"  P&L: ${best['total_pnl']:+.2f} | {best['trades']} trades | "
              f"{best['win_rate']:.0%} win rate | ${best['per_trade']:+.4f}/trade")
    
    if len(all_results) > 5:
        worst = all_results[-1]
        print(f"\n  WORST: edge≥{worst['min_edge']:.0%}, conf≥{worst['min_confidence']:.0%}, "
              f"ask≤{worst['max_ask']:.0%}, damp={worst['drift_dampening']}")
        print(f"  P&L: ${worst['total_pnl']:+.2f} | {worst['trades']} trades | "
              f"{worst['win_rate']:.0%} win rate")
    
    # Per-category breakdown for best params
    if all_results:
        best = all_results[0]
        recs = run_analysis(markets, prices, dampening=best["drift_dampening"])
        trades = compute_stats(recs, best["min_edge"], best["min_confidence"], best["max_ask"])
        bought = [t for t in trades if t["pnl"] != 0]
        
        print(f"\n  BY CATEGORY (best params):")
        for cat in sorted(set(t["category"] for t in bought)):
            cat_trades = [t for t in bought if t["category"] == cat]
            cat_pnl = sum(t["pnl"] for t in cat_trades)
            cat_wins = sum(1 for t in cat_trades if t["pnl"] > 0)
            cat_wr = cat_wins / len(cat_trades) if cat_trades else 0
            print(f"    {cat:<20} {len(cat_trades):>4} trades | "
                  f"{cat_wins}W/{len(cat_trades)-cat_wins}L ({cat_wr:.0%}) | "
                  f"P&L ${cat_pnl:+.2f}")
    
    print("\n" + "=" * 70)
    
    # Save results
    output = {
        "run_date": date.today().isoformat(),
        "asset": "silver",
        "lookback_days": args.days,
        "markets_analyzed": len(records),
        "price_range": f"${prices[0].close:.2f} - ${prices[-1].close:.2f}",
        "top_params": all_results[:10] if all_results else [],
    }
    outpath = os.path.join(_dir, "backtest_silver_results.json")
    with open(outpath, "w") as f:
        json.dump(output, f, indent=2)
    log.info("Saved: %s", outpath)


if __name__ == "__main__":
    main()
