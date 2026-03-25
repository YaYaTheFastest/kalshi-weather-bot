#!/usr/bin/env python3
"""
backtest_v2.py — Improved backtesting that doesn't rely on last_price.

Instead of simulating at the final trading price (which is near $0/$1),
this backtest asks: "For contracts at various hypothetical ask prices,
what would the P&L have been across all settled markets?"

This gives us:
1. The MODEL's accuracy at different confidence levels
2. The expected P&L for buying contracts at different price points
3. The optimal entry thresholds based on actual outcomes
"""
import argparse, json, logging, math, os, sys, time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional
import requests

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path or sys.path[0] != _dir:
    sys.path.insert(0, _dir)

from price_model import CommodityForecast, compute_residual_volatility
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest_v2")

# ── EIA data (same as v1) ──────────────────────────────────────────────

@dataclass
class EIAPrice:
    week_date: date
    price: float

def fetch_eia_weekly():
    """Fetch weekly gas prices from EIA API v2 (DEMO_KEY, no registration)."""
    url = "https://api.eia.gov/v2/petroleum/pri/gnd/data/"
    params = {
        "api_key": config.EIA_API_KEY,
        "frequency": "weekly",
        "data[0]": "value",
        "facets[series][]": "EMM_EPMR_PTE_NUS_DPG",
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": 200,
    }
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        records = data.get("response", {}).get("data", [])
    except Exception as e:
        log.error("EIA API fetch failed: %s", e)
        return []
    
    prices = []
    for rec in records:
        try:
            period = rec.get("period", "")
            value = rec.get("value")
            if not period or value is None:
                continue
            dt = datetime.strptime(period, "%Y-%m-%d").date()
            prices.append(EIAPrice(dt, float(value)))
        except (ValueError, TypeError):
            continue
    
    prices.sort(key=lambda p: p.week_date)
    log.info("EIA: %d weekly prices loaded (%s to %s)",
             len(prices),
             prices[0].week_date if prices else "?",
             prices[-1].week_date if prices else "?")
    return prices

def find_eia_price(prices, target, days_before=0):
    target_adj = target - timedelta(days=days_before)
    cur, prev = None, None
    for p in prices:
        if p.week_date <= target_adj:
            prev, cur = cur, p
        else:
            break
    if cur:
        return (cur.price, prev.price if prev else cur.price)
    return None

# ── Kalshi settled markets ──────────────────────────────────────────────

def fetch_settled_markets():
    try:
        from kalshi_client import _get
    except:
        log.warning("Kalshi API not available")
        return []
    
    all_markets = []
    series_list = ["KXAAAGASW", "KXAAAGASM"]
    
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
    
    log.info("Fetched %d settled markets", len(all_markets))
    return all_markets

# ── Parse markets ──────────────────────────────────────────────────────

@dataclass
class SettledMarket:
    ticker: str
    category: str  # gas_weekly, gas_monthly
    strike: float
    settle_date: date
    settled_yes: bool

def parse_markets(raw, lookback_days=90):
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
            sd = datetime.strptime(parts[1], "%y%b%d").date()
        except:
            continue
        if sd < cutoff:
            continue
        try:
            strike = float("-".join(parts[2:]))
        except:
            continue
        
        tu = ticker.upper()
        if "KXAAAGASW" in tu:
            cat = "gas_weekly"
        elif "KXAAAGASM" in tu:
            cat = "gas_monthly"
        else:
            continue
        
        results.append(SettledMarket(ticker, cat, strike, sd, result_str == "yes"))
    
    log.info("Parsed %d markets in %d-day window", len(results), lookback_days)
    return results

# ── Core analysis ──────────────────────────────────────────────────────

def run_analysis(markets, eia, dampening=0.6):
    """
    For each settled market:
    1. Reconstruct the forecast (what the model would have predicted)
    2. Record confidence and actual outcome
    3. Compute hypothetical P&L at various entry prices
    """
    records = []
    
    for m in markets:
        # Find EIA price from ~3-7 days before settlement
        days_before = 3 if m.category == "gas_weekly" else 7
        pair = find_eia_price(eia, m.settle_date, days_before=days_before)
        if not pair:
            continue
        cur, wk = pair
        vol = compute_residual_volatility(cur, cur, wk, vol_floor=0.008)
        
        fc = CommodityForecast(cur, cur, wk, wk, 0.0, cur - wk, vol,
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
            "eia_price": cur,
            "eia_week_ago": wk,
            "days_before": days_before,
        })
    
    return records

def compute_stats(records, min_edge, min_conf, max_ask):
    """
    Simulate trades: buy when model confidence exceeds hypothetical ask by min_edge.
    
    Since we don't know the actual ask price at the time, we simulate:
    "If the market had been priced at (model_confidence - min_edge), would we buy?"
    
    This is equivalent to: "For all contracts where our model was confident,
    how often were we right, and what was the P&L?"
    """
    trades = []
    
    for r in records:
        conf = r["model_confidence"]
        
        # The max price we'd pay is (confidence - min_edge)
        # But we also need the market to actually be priced below our confidence
        # Simulate buying at various price points
        hypothetical_ask = max(0.01, conf - min_edge)  # what we'd need the ask to be
        
        if hypothetical_ask > max_ask:
            continue
        if conf < min_conf:
            continue
        if hypothetical_ask <= 0.01:
            continue
        
        # P&L: if settled yes, we get $1 - ask. If no, we lose ask.
        if r["settled_yes"]:
            pnl = 1.0 - hypothetical_ask
        else:
            pnl = -hypothetical_ask
        
        trades.append({
            "ticker": r["ticker"],
            "category": r["category"],
            "confidence": conf,
            "hypothetical_ask": round(hypothetical_ask, 4),
            "settled_yes": r["settled_yes"],
            "pnl": round(pnl, 4),
        })
    
    return trades

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--optimize", action="store_true")
    args = parser.parse_args()
    
    log.info("Backtest v2: lookback=%dd, optimize=%s", args.days, args.optimize)
    
    # Fetch data
    raw = fetch_settled_markets()
    markets = parse_markets(raw, args.days)
    eia = fetch_eia_weekly()
    
    if not markets:
        log.warning("No markets found")
        return
    
    # Analyze with different dampening values
    best_result = None
    results_summary = {}
    
    dampening_values = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8] if args.optimize else [0.6]
    edge_values = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30] if args.optimize else [0.10, 0.15, 0.20]
    conf_values = [0.50, 0.55, 0.60, 0.65, 0.70, 0.80] if args.optimize else [0.55, 0.60, 0.65]
    ask_values = [0.30, 0.40, 0.50, 0.60] if args.optimize else [0.40, 0.50]
    
    print("=" * 70)
    print("  KALSHI BACKTEST V2 — MODEL ACCURACY & P&L ANALYSIS")
    print("=" * 70)
    
    # First: show model accuracy at different confidence buckets
    records = run_analysis(markets, eia, dampening=0.6)
    
    print(f"\n  Markets analyzed: {len(records)} ({len(markets)} raw)")
    
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
    
    # Win rate for YES buys at different confidence levels
    print(f"\n  YES CONTRACT WIN RATE:")
    print(f"  {'Min Conf':>10} {'Contracts':>10} {'Settled Yes':>12} {'Win Rate':>9}")
    print(f"  " + "-" * 45)
    for min_c in [0.5, 0.6, 0.7, 0.8, 0.9]:
        qualifying = [r for r in records if r["model_confidence"] >= min_c]
        yes_count = sum(1 for r in qualifying if r["settled_yes"])
        if qualifying:
            print(f"  {min_c:>9.0%} {len(qualifying):>10} {yes_count:>12} {yes_count/len(qualifying):>8.1%}")
    
    # P&L simulation at different parameter combos
    print(f"\n  SIMULATED P&L BY PARAMETERS:")
    print(f"  {'Edge':>6} {'Conf':>6} {'MaxAsk':>7} {'Damp':>5} {'Trades':>7} {'Wins':>5} {'WinRate':>8} {'P&L':>8} {'$/Trade':>8}")
    print(f"  " + "-" * 68)
    
    all_results = []
    
    for damp in dampening_values:
        recs = run_analysis(markets, eia, dampening=damp)
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
    
    # Sort by P&L
    all_results.sort(key=lambda x: x["total_pnl"], reverse=True)
    
    # Show top 15
    for r in all_results[:15]:
        print(f"  {r['min_edge']:>5.0%} {r['min_confidence']:>5.0%} {r['max_ask']:>6.0%} "
              f"{r['drift_dampening']:>5.1f} {r['trades']:>7} {r['wins']:>5} "
              f"{r['win_rate']:>7.0%} ${r['total_pnl']:>+7.2f} ${r['per_trade']:>+7.4f}")
    
    # Category breakdown for best params
    if all_results:
        best = all_results[0]
        print(f"\n  BEST: edge≥{best['min_edge']:.0%}, conf≥{best['min_confidence']:.0%}, "
              f"ask≤{best['max_ask']:.0%}, damp={best['drift_dampening']}")
        print(f"  P&L: ${best['total_pnl']:+.2f} | {best['trades']} trades | "
              f"{best['win_rate']:.0%} win rate | ${best['per_trade']:+.4f}/trade")
    
    # Also show worst to understand risk
    if len(all_results) > 5:
        worst = all_results[-1]
        print(f"\n  WORST: edge≥{worst['min_edge']:.0%}, conf≥{worst['min_confidence']:.0%}, "
              f"ask≤{worst['max_ask']:.0%}, damp={worst['drift_dampening']}")
        print(f"  P&L: ${worst['total_pnl']:+.2f} | {worst['trades']} trades | "
              f"{worst['win_rate']:.0%} win rate")
    
    print("\n" + "=" * 70)
    
    # Save results
    output = {
        "run_date": date.today().isoformat(),
        "lookback_days": args.days,
        "markets_analyzed": len(records),
        "model_accuracy": {k: v for k, v in buckets.items()},
        "top_params": all_results[:10] if all_results else [],
        "all_results_count": len(all_results),
    }
    
    outpath = os.path.join(_dir, "backtest_results.json")
    with open(outpath, "w") as f:
        json.dump(output, f, indent=2)
    log.info("Saved: %s", outpath)

if __name__ == "__main__":
    main()
