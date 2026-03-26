#!/usr/bin/env python3
"""
simulate_model.py — Simulate the model against settled Kalshi markets.

Compares OLD model (raw edge) vs NEW model (with min_ask filter + blended vol)
across all commodity types: gas, oil, gold, silver.

Usage:
  python3 simulate_model.py              # Default 90 days
  python3 simulate_model.py --days 180   # Longer lookback
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
log = logging.getLogger("simulate")

# ── Yahoo Finance ─────────────────────────────────────────────────────

@dataclass
class DailyPrice:
    dt: date
    close: float

def fetch_yahoo(ticker: str, days: int = 200) -> list[DailyPrice]:
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
        log.error("Yahoo fetch %s: %s", ticker, e)
        return []
    result = data.get("chart", {}).get("result", [])
    if not result:
        return []
    ts = result[0].get("timestamp", [])
    cl = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
    prices = []
    for t, c in zip(ts, cl):
        if c is not None:
            prices.append(DailyPrice(datetime.fromtimestamp(t, tz=timezone.utc).date(), float(c)))
    prices.sort(key=lambda p: p.dt)
    log.info("Yahoo %s: %d prices", ticker, len(prices))
    return prices

def find_price(prices, target, lookback=5):
    for i in range(lookback):
        d = target - timedelta(days=i)
        for p in prices:
            if p.dt == d:
                return p.close
    return None

# ── EIA (for gas) ─────────────────────────────────────────────────────

def fetch_eia():
    url = "https://api.eia.gov/v2/petroleum/pri/gnd/data/"
    params = {
        "api_key": config.EIA_API_KEY or "DEMO_KEY",
        "frequency": "weekly", "data[0]": "value",
        "facets[series][]": "EMM_EPMR_PTE_NUS_DPG",
        "sort[0][column]": "period", "sort[0][direction]": "desc", "length": 200,
    }
    try:
        r = requests.get(url, params=params, timeout=30)
        data = r.json().get("response", {}).get("data", [])
    except:
        return []
    prices = []
    for rec in data:
        try:
            dt = datetime.strptime(rec["period"], "%Y-%m-%d").date()
            prices.append(DailyPrice(dt, float(rec["value"])))
        except:
            continue
    prices.sort(key=lambda p: p.dt)
    log.info("EIA: %d weekly gas prices", len(prices))
    return prices

# ── Kalshi settled markets ────────────────────────────────────────────

@dataclass
class Market:
    ticker: str
    cat: str
    strike: float
    settle_date: date
    yes: bool

def fetch_settled(series_list):
    try:
        from kalshi_client import _get
    except:
        return []
    all_m = []
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
            all_m.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
    return all_m

def parse_markets(raw, lookback_days, categories):
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
            date_str = parts[1][:7]
            sd = datetime.strptime(date_str, "%y%b%d").date()
        except:
            continue
        if sd < cutoff:
            continue
        try:
            ps = "-".join(parts[2:])
            if ps.upper().startswith("T"):
                ps = ps[1:]
            strike = float(ps)
        except:
            continue
        tu = ticker.upper()
        cat = None
        for prefix, catname in categories.items():
            if prefix in tu:
                cat = catname
                break
        if not cat:
            continue
        results.append(Market(ticker, cat, strike, sd, result_str == "yes"))
    return results

# ── Simulation ────────────────────────────────────────────────────────

def simulate(markets, prices_map, dampening, min_edge, min_conf, max_ask, min_ask, vol_floor):
    """Run simulation with given parameters. Returns list of trade dicts."""
    trades = []
    for m in markets:
        # Determine price source and days_before
        if "gas" in m.cat:
            prices = prices_map.get("gas", [])
            days_before = 3 if "weekly" in m.cat else 7
        elif "oil" in m.cat:
            prices = prices_map.get("oil", [])
            days_before = 1 if "daily" in m.cat else 3
        elif "gold" in m.cat:
            prices = prices_map.get("gold", [])
            days_before = 1 if "daily" in m.cat else 3
        elif "silver" in m.cat:
            prices = prices_map.get("silver", [])
            days_before = 1 if "daily" in m.cat else 3
        else:
            continue

        ref = m.settle_date - timedelta(days=days_before)
        cur = find_price(prices, ref)
        yest = find_price(prices, ref - timedelta(days=1))
        wk = find_price(prices, ref - timedelta(days=7))
        if not cur or not yest or not wk:
            continue

        vol = compute_residual_volatility(cur, yest, wk, vol_floor=vol_floor)
        fc = CommodityForecast(cur, yest, wk, wk, cur - yest, cur - wk, vol,
                               ref, days_before, drift_dampening=dampening)
        conf = fc.confidence_above(m.strike)
        hyp_ask = max(0.01, conf - min_edge)

        if hyp_ask > max_ask or conf < min_conf or hyp_ask < min_ask:
            continue

        pnl = (1.0 - hyp_ask) if m.yes else -hyp_ask
        trades.append({
            "ticker": m.ticker, "cat": m.cat, "strike": m.strike,
            "conf": round(conf, 3), "ask": round(hyp_ask, 3),
            "yes": m.yes, "pnl": round(pnl, 4),
        })
    return trades


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()

    # Fetch all data
    all_series = [
        "KXAAAGASW", "KXAAAGASM",
        "KXWTI", "KXWTIW",
        "KXGOLDD", "KXGOLDW", "KXGOLDMON",
        "KXSILVERD", "KXSILVERW", "KXSILVERMON",
    ]
    categories = {
        "KXAAAGASW": "gas_weekly", "KXAAAGASM": "gas_monthly",
        "KXWTI": "oil_daily", "KXWTIW": "oil_weekly",
        "KXGOLDD": "gold_daily", "KXGOLDW": "gold_weekly", "KXGOLDMON": "gold_monthly",
        "KXSILVERD": "silver_daily", "KXSILVERW": "silver_weekly", "KXSILVERMON": "silver_monthly",
    }

    raw = fetch_settled(all_series)
    markets = parse_markets(raw, args.days, categories)
    log.info("Total settled markets: %d", len(markets))

    prices_map = {
        "gas": fetch_eia(),
        "oil": fetch_yahoo("CL=F", args.days + 30),
        "gold": fetch_yahoo("GC=F", args.days + 30),
        "silver": fetch_yahoo("SI=F", args.days + 30),
    }

    if not markets:
        print("No markets to simulate")
        return

    # Count by category
    by_cat = defaultdict(int)
    for m in markets:
        by_cat[m.cat] += 1

    print("=" * 75)
    print("  MODEL SIMULATION — OLD vs NEW")
    print("=" * 75)
    print(f"\n  Markets: {len(markets)} settled ({', '.join(f'{k}={v}' for k, v in sorted(by_cat.items()))})")
    print(f"  Lookback: {args.days} days")

    # OLD MODEL: current production params
    old_params = {
        "dampening": 0.6, "min_edge": 0.30, "min_conf": 0.50,
        "max_ask": 0.60, "min_ask": 0.0, "vol_floor": 0.008,
    }

    # NEW MODEL: with min_ask filter + tighter confidence
    new_params = {
        "dampening": 0.6, "min_edge": 0.30, "min_conf": 0.50,
        "max_ask": 0.60, "min_ask": 0.10, "vol_floor": 0.008,
    }

    for label, params in [("OLD MODEL (no min_ask)", old_params), ("NEW MODEL (min_ask=10¢)", new_params)]:
        trades = simulate(markets, prices_map, **params)
        if not trades:
            print(f"\n  {label}: No trades")
            continue

        wins = sum(1 for t in trades if t["pnl"] > 0)
        total_pnl = sum(t["pnl"] for t in trades)
        wr = wins / len(trades) if trades else 0

        print(f"\n  {label}:")
        print(f"    Trades: {len(trades)} | Wins: {wins} | Win Rate: {wr:.0%} | P&L: ${total_pnl:+.2f}")

        # By category
        print(f"    {'Category':<20} {'Trades':>7} {'Wins':>5} {'WR':>6} {'P&L':>8}")
        print(f"    {'-'*50}")
        for cat in sorted(set(t["cat"] for t in trades)):
            ct = [t for t in trades if t["cat"] == cat]
            cw = sum(1 for t in ct if t["pnl"] > 0)
            cp = sum(t["pnl"] for t in ct)
            cwr = cw / len(ct) if ct else 0
            print(f"    {cat:<20} {len(ct):>7} {cw:>5} {cwr:>5.0%} ${cp:>+7.2f}")

    # Parameter sweep for NEW model
    print(f"\n  PARAMETER SWEEP (with min_ask=10¢):")
    print(f"  {'Edge':>6} {'Conf':>6} {'Damp':>5} {'Trades':>7} {'Wins':>5} {'WR':>6} {'P&L':>8} {'$/Trade':>8}")
    print(f"  {'-'*55}")

    sweep_results = []
    for damp in [0.3, 0.5, 0.6, 0.7]:
        for edge in [0.20, 0.25, 0.30, 0.35]:
            for conf in [0.50, 0.60, 0.70, 0.80]:
                trades = simulate(markets, prices_map,
                                dampening=damp, min_edge=edge, min_conf=conf,
                                max_ask=0.60, min_ask=0.10, vol_floor=0.008)
                if len(trades) < 5:
                    continue
                wins = sum(1 for t in trades if t["pnl"] > 0)
                pnl = sum(t["pnl"] for t in trades)
                wr = wins / len(trades)
                per = pnl / len(trades)
                sweep_results.append({
                    "damp": damp, "edge": edge, "conf": conf,
                    "trades": len(trades), "wins": wins, "wr": wr,
                    "pnl": pnl, "per": per,
                })

    sweep_results.sort(key=lambda x: x["pnl"], reverse=True)
    for r in sweep_results[:15]:
        print(f"  {r['edge']:>5.0%} {r['conf']:>5.0%} {r['damp']:>5.1f} "
              f"{r['trades']:>7} {r['wins']:>5} {r['wr']:>5.0%} "
              f"${r['pnl']:>+7.2f} ${r['per']:>+7.4f}")

    if sweep_results:
        best = sweep_results[0]
        print(f"\n  OPTIMAL: edge≥{best['edge']:.0%}, conf≥{best['conf']:.0%}, damp={best['damp']}")
        print(f"  P&L: ${best['pnl']:+.2f} | {best['trades']} trades | {best['wr']:.0%} win rate")

    print("\n" + "=" * 75)

    # Save
    output = {
        "run_date": date.today().isoformat(),
        "markets": len(markets),
        "by_category": dict(by_cat),
        "top_params": sweep_results[:10] if sweep_results else [],
    }
    with open(os.path.join(_dir, "simulation_results.json"), "w") as f:
        json.dump(output, f, indent=2)


if __name__ == "__main__":
    main()
