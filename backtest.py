#!/usr/bin/env python3
"""
backtest.py — Backtesting & parameter optimization for the Kalshi trading bot.

Usage:
  python3 backtest.py                       # 90-day lookback, quick sweep
  python3 backtest.py --days 30             # 30-day lookback
  python3 backtest.py --optimize            # full 5-dim parameter grid sweep
  python3 backtest.py --days 60 --optimize
"""
import argparse, itertools, json, logging, math, os, sys, time
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
log = logging.getLogger("backtest")

# ── Part 1: Pull settled markets from Kalshi ──────────────────────────────

def _kalshi_get(path, params=None):
    try:
        from kalshi_client import _get
        return _get(path, params)
    except Exception as e:
        log.error("Kalshi API: %s", e)
        return None

def fetch_settled_markets(series_tickers, max_pages=20):
    if not config.KALSHI_ACCESS_KEY or not config.KALSHI_PRIVATE_KEY_PEM:
        log.warning("No Kalshi credentials — run on server with .env")
        return []
    all_mkts = []
    for series in series_tickers:
        cursor, pages = None, 0
        while pages < max_pages:
            p = {"status": "settled", "limit": 1000, "series_ticker": series}
            if cursor:
                p["cursor"] = cursor
            data = _kalshi_get("/markets", p)
            if not data:
                break
            batch = data.get("markets", [])
            all_mkts.extend(batch)
            pages += 1
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        time.sleep(0.25)
    log.info("Fetched %d settled markets", len(all_mkts))
    return all_mkts

# ── Part 2a: EIA weekly gas prices ────────────────────────────────────────

@dataclass
class EIAPrice:
    week_date: date
    price: float

def fetch_eia_weekly_gas():
    url = ("https://api.eia.gov/v2/petroleum/pri/gnd/data/"
           "?api_key=DEMO_KEY&frequency=weekly&data[0]=value"
           "&facets[product][]=EPMR&facets[duoarea][]=NUS"
           "&sort[0][column]=period&sort[0][direction]=desc&offset=0&length=200")
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "KalshiBacktest/1.0"})
        r.raise_for_status()
        rows = r.json().get("response", {}).get("data", [])
        prices = []
        for row in rows:
            val = row.get("value")
            if val is None:
                continue
            try:
                dt = datetime.strptime(row.get("period", ""), "%Y-%m-%d").date()
                prices.append(EIAPrice(dt, float(val)))
            except (ValueError, TypeError):
                pass
        prices.sort(key=lambda p: p.week_date)
        log.info("EIA: %d points (%s – %s)", len(prices),
                 prices[0].week_date if prices else "?", prices[-1].week_date if prices else "?")
        return prices
    except Exception as e:
        log.error("EIA fetch failed: %s", e)
        return []

def find_eia_price(prices, target):
    """Returns (current, week_ago) prices for the most recent EIA date <= target."""
    if not prices:
        return None
    cur, prev = None, None
    for p in prices:
        if p.week_date <= target:
            prev, cur = cur, p
        else:
            break
    return (cur.price, prev.price if prev else cur.price) if cur else None

# ── Part 2b: Open-Meteo historical weather ────────────────────────────────

def fetch_historical_high(lat, lon, dt):
    try:
        r = requests.get("https://archive-api.open-meteo.com/v1/archive", params={
            "latitude": lat, "longitude": lon, "start_date": dt.isoformat(),
            "end_date": dt.isoformat(), "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit"}, timeout=15)
        r.raise_for_status()
        highs = r.json().get("daily", {}).get("temperature_2m_max", [])
        return float(highs[0]) if highs and highs[0] is not None else None
    except Exception:
        return None

# ── Part 2c: Parse settled markets ────────────────────────────────────────

@dataclass
class SettledMarket:
    ticker: str
    series: str
    category: str          # gas_weekly, gas_monthly, oil_daily, oil_weekly, weather
    strike: float
    settle_date: date
    settled_yes: bool
    last_price: float      # 0–1 dollars
    confidence: float = 0.0
    city_key: Optional[str] = None
    city_lat: Optional[float] = None
    city_lon: Optional[float] = None

def _parse_strike(parts):
    try:
        return float("-".join(parts[2:]))
    except ValueError:
        return None

def parse_settled_markets(raw):
    results = []
    for m in raw:
        ticker = m.get("ticker", "")
        lp = float(m.get("last_price", 0) or 0)
        if lp > 1.0:
            lp /= 100.0
        yes = m.get("result", "").lower() == "yes"
        parts = ticker.split("-")
        if len(parts) < 3:
            continue
        try:
            sd = datetime.strptime(parts[1], "%y%b%d").date()
        except (ValueError, IndexError):
            continue
        tu = ticker.upper()

        # Commodity markets: gas, oil
        for prefix, cat in [("KXAAAGASW", "gas_weekly"), ("KXAAAGASM", "gas_monthly"),
                            ("KXWTIW", "oil_weekly"), ("KXWTI", "oil_daily")]:
            if prefix in tu:
                strike = _parse_strike(parts)
                if strike is not None:
                    results.append(SettledMarket(ticker, prefix, cat, strike, sd, yes, lp))
                break
        else:
            # Weather
            if "KXHIGH" not in tu:
                continue
            bs = parts[-1].upper()
            strike = None
            if bs.startswith("T") and "B" not in bs:
                try: strike = float(bs[1:])
                except ValueError: continue
            elif bs.startswith("B") and "T" in bs:
                try: strike = float(bs[bs.index("T") + 1:])
                except ValueError: continue
            else:
                continue
            ck = cl = clo = None
            for k, c in config.CITIES.items():
                if f"KXHIGH{c['kalshi_suffix'].upper()}" in tu:
                    ck, cl, clo = k, c["lat"], c["lon"]
                    break
            results.append(SettledMarket(ticker, f"KXHIGH{ck or '?'}", "weather",
                                         strike, sd, yes, lp, city_key=ck,
                                         city_lat=cl, city_lon=clo))
    log.info("Parsed %d settled markets from %d raw", len(results), len(raw))
    return results

# ── Part 2d: Reconstruct model confidence ─────────────────────────────────

def reconstruct_gas(markets, eia, dampening=0.6):
    n = 0
    for m in markets:
        if m.category not in ("gas_weekly", "gas_monthly"):
            continue
        pair = find_eia_price(eia, m.settle_date)
        if not pair:
            continue
        cur, wk = pair
        vol = compute_residual_volatility(cur, wk, wk, vol_floor=0.008)
        days = 3 if m.category == "gas_weekly" else 7
        fc = CommodityForecast(cur, cur, wk, wk, 0.0, cur - wk, vol,
                               m.settle_date - timedelta(days=days), days,
                               drift_dampening=dampening)
        m.confidence = fc.confidence_above(m.strike)
        n += 1
    return n

def reconstruct_weather(markets):
    seen, n = {}, 0
    for m in markets:
        if m.category != "weather" or not m.city_lat:
            continue
        key = (m.city_key, m.settle_date.isoformat())
        if key not in seen:
            seen[key] = fetch_historical_high(m.city_lat, m.city_lon, m.settle_date)
            time.sleep(0.15)
        ah = seen[key]
        if ah is None:
            continue
        z = (m.strike - ah) / 2.0  # sigma = 2°F forecast uncertainty
        m.confidence = 0.5 * (1.0 - math.erf(z / math.sqrt(2)))
        n += 1
    return n

# ── Part 3: Trade simulation ──────────────────────────────────────────────

@dataclass
class Trade:
    ticker: str
    category: str
    bought: bool
    pnl: float = 0.0
    edge: float = 0.0
    confidence: float = 0.0

def simulate(markets, params):
    trades = []
    for m in markets:
        conf, ask = m.confidence, m.last_price
        edge = conf - ask
        buy = (edge >= params["min_edge"] and conf >= params["min_confidence"]
               and ask <= params["max_ask"] and ask > 0.01)
        if buy:
            pnl = (1.0 - ask) if m.settled_yes else -ask
            trades.append(Trade(m.ticker, m.category, True, pnl, edge, conf))
        else:
            trades.append(Trade(m.ticker, m.category, False, confidence=conf))
    return trades

# ── Part 4: Parameter optimization ────────────────────────────────────────

GRID = {
    "min_edge":        [0.10, 0.15, 0.20, 0.25, 0.30],
    "min_confidence":  [0.50, 0.55, 0.60, 0.65, 0.70],
    "max_ask":         [0.30, 0.35, 0.40, 0.45, 0.50],
    "drift_dampening": [0.4, 0.5, 0.6, 0.7, 0.8],
    "sell_threshold":  [0.40, 0.45, 0.50, 0.55, 0.60],
}
CURRENT = {
    "min_edge": config.COMMODITY_MIN_EDGE,
    "min_confidence": config.COMMODITY_MIN_CONFIDENCE,
    "max_ask": config.COMMODITY_MAX_ASK,
    "drift_dampening": config.COMMODITY_DRIFT_DAMPENING,
    "sell_threshold": config.COMMODITY_SELL_MIN_PRICE,
}
MIN_TRADES = 10

@dataclass
class ParamResult:
    params: dict
    pnl: float = 0.0
    trades: int = 0
    wins: int = 0
    win_rate: float = 0.0
    avg_edge: float = 0.0
    max_dd: float = 0.0
    sharpe: float = 0.0
    breakdown: dict = field(default_factory=dict)

def evaluate(markets, params, eia):
    reconstruct_gas(markets, eia, params["drift_dampening"])
    results = simulate(markets, params)
    bought = [t for t in results if t.bought]
    pr = ParamResult(params=params, trades=len(bought))
    if not bought:
        return pr
    pr.pnl = sum(t.pnl for t in bought)
    pr.wins = sum(1 for t in bought if t.pnl > 0)
    pr.win_rate = pr.wins / pr.trades
    pr.avg_edge = sum(t.edge for t in bought) / pr.trades
    # Max drawdown
    cum, peak, dd = 0.0, 0.0, 0.0
    for t in bought:
        cum += t.pnl
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    pr.max_dd = dd
    # Sharpe
    if pr.trades >= 2:
        pnls = [t.pnl for t in bought]
        mu = sum(pnls) / len(pnls)
        var = sum((p - mu) ** 2 for p in pnls) / (len(pnls) - 1)
        pr.sharpe = (mu / max(math.sqrt(var), 0.001)) * math.sqrt(len(pnls))
    # Breakdown by category
    cats = {}
    for t in bought:
        cats.setdefault(t.category, []).append(t)
    for cat, ts in cats.items():
        w = sum(1 for t in ts if t.pnl > 0)
        pr.breakdown[cat] = {"pnl": round(sum(t.pnl for t in ts), 4),
                             "trades": len(ts),
                             "win_rate": round(w / len(ts), 4)}
    return pr

def optimize(markets, eia):
    keys = list(GRID.keys())
    combos = list(itertools.product(*GRID.values()))
    log.info("Sweeping %d param combos...", len(combos))
    results = []
    for i, combo in enumerate(combos):
        pr = evaluate(markets, dict(zip(keys, combo)), eia)
        if pr.trades >= MIN_TRADES:
            results.append(pr)
        if (i + 1) % 500 == 0:
            log.info("  %d/%d done", i + 1, len(combos))
    results.sort(key=lambda r: r.pnl, reverse=True)
    log.info("Optimization: %d valid sets (of %d)", len(results), len(combos))
    return results

# ── Part 5: Report ────────────────────────────────────────────────────────

def _pr_dict(pr):
    return {"params": {k: round(v, 3) for k, v in pr.params.items()},
            "total_pnl": round(pr.pnl, 4), "win_rate": round(pr.win_rate, 4),
            "num_trades": pr.trades, "avg_edge": round(pr.avg_edge, 4),
            "sharpe": round(pr.sharpe, 3), "max_drawdown": round(pr.max_dd, 4)}

def build_report(markets, current, all_results):
    counts = {}
    for m in markets:
        c = counts.setdefault(m.category, {"total": 0, "settled_yes": 0, "settled_no": 0})
        c["total"] += 1
        c["settled_yes" if m.settled_yes else "settled_no"] += 1

    opt = all_results[0] if all_results else current
    imp = ""
    if current.pnl != 0:
        imp = f"{(opt.pnl - current.pnl) / abs(current.pnl) * 100:+.1f}%"
    elif opt.pnl > 0:
        imp = "+inf%"

    insights = []
    cats_str = ", ".join(f"{c}={n['total']}" for c, n in sorted(counts.items()))
    insights.append(f"Analyzed {len(markets)} markets: {cats_str}")
    if current.trades > 0:
        insights.append(f"Current: {current.trades} trades, {current.win_rate:.0%} win, ${current.pnl:+.2f}")
    else:
        insights.append("Current params: no trades triggered")
    if opt.pnl > current.pnl:
        insights.append(f"Optimal improves P&L by ${opt.pnl - current.pnl:+.2f} "
                        f"({opt.trades} trades, {opt.win_rate:.0%} win)")
    for cat, bd in sorted(opt.breakdown.items()):
        if bd["trades"] >= 5:
            insights.append(f"  {cat}: {bd['win_rate']:.0%} win, {bd['trades']} trades, ${bd['pnl']:+.2f}")

    return {
        "run_date": date.today().isoformat(),
        "markets_analyzed": counts,
        "current_params": _pr_dict(current),
        "optimal_params": {**_pr_dict(opt), "improvement_vs_current": imp},
        "top_10_param_sets": [_pr_dict(r) for r in all_results[:10]],
        "market_breakdown": opt.breakdown,
        "insights": insights,
    }

def print_summary(rpt):
    print("\n" + "=" * 70)
    print("  KALSHI TRADING BOT — BACKTEST RESULTS")
    print("=" * 70)
    print(f"  Date: {rpt['run_date']}\n")
    print("  MARKETS:")
    for cat, c in rpt["markets_analyzed"].items():
        print(f"    {cat:20s} total={c['total']:4d}  yes={c['settled_yes']:4d}  no={c['settled_no']:4d}")
    for label, key in [("\n  CURRENT:", "current_params"), ("  OPTIMAL:", "optimal_params")]:
        p = rpt[key]
        print(f"{label}")
        print(f"    {p['params']}")
        print(f"    P&L: ${p['total_pnl']:+.2f} | Trades: {p['num_trades']} | "
              f"Win: {p['win_rate']:.1%} | Sharpe: {p['sharpe']:.2f}")
    if "improvement_vs_current" in rpt["optimal_params"]:
        print(f"    Improvement: {rpt['optimal_params']['improvement_vs_current']}")
    if len(rpt["top_10_param_sets"]) > 1:
        print("\n  TOP 5:")
        for i, ps in enumerate(rpt["top_10_param_sets"][:5], 1):
            print(f"    #{i}: ${ps['total_pnl']:+.2f}  {ps['num_trades']}t  "
                  f"{ps['win_rate']:.0%}w  sharpe={ps['sharpe']:.2f}  {ps['params']}")
    print("\n  INSIGHTS:")
    for ins in rpt["insights"]:
        print(f"    * {ins}")
    print("\n" + "=" * 70)

# ── Main ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Backtest the Kalshi trading bot")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--output", default=os.path.join(_dir, "backtest_results.json"))
    args = ap.parse_args()

    log.info("Backtest: lookback=%dd, optimize=%s", args.days, args.optimize)

    # Fetch settled markets
    series = (["KXAAAGASW", "KXAAAGASM", "KXWTI", "KXWTIW"]
              + [f"KXHIGH{c['kalshi_suffix']}" for c in config.CITIES.values()])
    raw = fetch_settled_markets(series)
    markets = parse_settled_markets(raw)
    cutoff = date.today() - timedelta(days=args.days)
    markets = [m for m in markets if m.settle_date >= cutoff]
    log.info("%d markets in %d-day window", len(markets), args.days)

    if not markets:
        log.warning("No markets found — need Kalshi API credentials")
        empty = {"run_date": date.today().isoformat(), "markets_analyzed": {},
                 "current_params": {**_pr_dict(ParamResult(CURRENT)), },
                 "optimal_params": {**_pr_dict(ParamResult(CURRENT)), "improvement_vs_current": "N/A"},
                 "top_10_param_sets": [], "market_breakdown": {},
                 "insights": ["No settled markets — run with Kalshi credentials"]}
        with open(args.output, "w") as f:
            json.dump(empty, f, indent=2)
        print_summary(empty)
        return

    # Reconstruct historical forecasts
    eia = fetch_eia_weekly_gas()
    log.info("Gas reconstructed: %d", reconstruct_gas(markets, eia, CURRENT["drift_dampening"]))
    wcount = sum(1 for m in markets if m.category == "weather")
    if 0 < wcount <= 500:
        log.info("Weather reconstructed: %d", reconstruct_weather(markets))
    elif wcount > 500:
        log.info("Skipping %d weather markets (too slow)", wcount)

    # Evaluate current params
    cur = evaluate(markets, CURRENT, eia)
    log.info("Current: %d trades, $%.2f P&L, %.0f%% win", cur.trades, cur.pnl, cur.win_rate * 100)

    # Optimize
    if args.optimize:
        all_res = optimize(markets, eia)
    else:
        all_res = []
        for me, mc, ma in itertools.product(
            [0.10, 0.15, 0.20, 0.25, 0.30], [0.50, 0.55, 0.60, 0.65], [0.35, 0.40, 0.45, 0.50]):
            pr = evaluate(markets, {**CURRENT, "min_edge": me, "min_confidence": mc, "max_ask": ma}, eia)
            if pr.trades >= MIN_TRADES:
                all_res.append(pr)
        all_res.sort(key=lambda r: r.pnl, reverse=True)

    rpt = build_report(markets, cur, all_res)
    with open(args.output, "w") as f:
        json.dump(rpt, f, indent=2)
    log.info("Saved: %s", args.output)
    print_summary(rpt)

if __name__ == "__main__":
    main()
