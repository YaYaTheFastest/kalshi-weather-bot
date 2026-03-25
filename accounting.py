#!/usr/bin/env python3
"""
accounting.py — Full portfolio accounting from Kalshi API.
Shows all positions, fills, settlements, and computes actual P&L.
"""
import sys, os, json
from datetime import datetime, timezone
from collections import defaultdict

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path: sys.path.insert(0, _dir)

from kalshi_client import _get

print("=" * 70)
print("  KALSHI PORTFOLIO ACCOUNTING")
print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
print("=" * 70)

# 1. Balance
bal = _get("/portfolio/balance")
cash = bal.get("balance", 0) / 100 if bal else 0
portfolio_value = bal.get("portfolio_value", 0) / 100 if bal else 0
total_equity = cash + portfolio_value
print(f"\n  ACCOUNT BALANCE:")
print(f"    Cash:             ${cash:.2f}")
print(f"    Portfolio value:  ${portfolio_value:.2f}")
print(f"    Total equity:     ${total_equity:.2f}")

# 2. All fills
all_fills = []
cursor = None
while True:
    params = {"limit": 200}
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

print(f"\n  TRADE HISTORY: {len(all_fills)} total fills")

# Group fills by ticker
by_ticker = defaultdict(list)
for f in all_fills:
    by_ticker[f.get("ticker", "unknown")].append(f)

# Categorize
total_buys_cost = 0
total_sells_proceeds = 0
total_fees = 0

# Track by market type
by_type = defaultdict(lambda: {"buy_cost": 0, "sell_proceeds": 0, "fees": 0, "count": 0})

for ticker, fills in sorted(by_ticker.items()):
    tu = ticker.upper()
    if "KXHIGH" in tu:
        mtype = "weather"
    elif "KXAAAGASW" in tu:
        mtype = "gas_weekly"
    elif "KXAAAGASM" in tu:
        mtype = "gas_monthly"
    elif "KXWTI" in tu:
        mtype = "oil"
    else:
        mtype = "other"

    for f in fills:
        action = f.get("action", "")
        side = f.get("side", "")
        count = int(float(f.get("count_fp", 0) or 0))
        yes_price = float(f.get("yes_price_dollars", 0) or 0)
        no_price = float(f.get("no_price_dollars", 0) or 0)
        fee = float(f.get("fee_cost", 0) or f.get("fee_cost_dollars", 0) or 0)
        total_fees += fee
        by_type[mtype]["fees"] += fee
        by_type[mtype]["count"] += 1

        if action == "buy" and side == "yes":
            cost = count * yes_price
            total_buys_cost += cost
            by_type[mtype]["buy_cost"] += cost
        elif action == "buy" and side == "no":
            cost = count * no_price
            total_buys_cost += cost
            by_type[mtype]["buy_cost"] += cost
        elif action == "sell" and side == "yes":
            proceeds = count * yes_price
            total_sells_proceeds += proceeds
            by_type[mtype]["sell_proceeds"] += proceeds
        elif action == "sell" and side == "no":
            proceeds = count * no_price
            total_sells_proceeds += proceeds
            by_type[mtype]["sell_proceeds"] += proceeds

print(f"\n  TRADING ACTIVITY:")
print(f"    Total bought:     ${total_buys_cost:.2f}")
print(f"    Total sold:       ${total_sells_proceeds:.2f}")
print(f"    Total fees:       ${total_fees:.2f}")
print(f"    Net trading P&L:  ${total_sells_proceeds - total_buys_cost:.2f} (before fees)")
print(f"    Net after fees:   ${total_sells_proceeds - total_buys_cost - total_fees:.2f}")

# 3. Settlements
all_settlements = []
cursor = None
while True:
    params = {"limit": 200}
    if cursor:
        params["cursor"] = cursor
    data = _get("/portfolio/settlements", params=params)
    if not data:
        break
    setts = data.get("settlements", [])
    all_settlements.extend(setts)
    cursor = data.get("cursor")
    if not cursor or not setts:
        break

total_settlement_revenue = 0
total_settlement_cost = 0
print(f"\n  SETTLEMENTS: {len(all_settlements)} settled markets")
for s in all_settlements:
    ticker = s.get("ticker", "")
    revenue = float(s.get("revenue_dollars", 0) or s.get("revenue", 0) or 0)
    yes_cost = float(s.get("yes_total_cost_dollars", 0) or 0)
    no_cost = float(s.get("no_total_cost_dollars", 0) or 0)
    cost = yes_cost + no_cost
    pnl = revenue - cost
    total_settlement_revenue += revenue
    total_settlement_cost += cost
    result = s.get("result", "")
    print(f"    {ticker:<40} revenue=${revenue:.2f} cost=${cost:.2f} pnl=${pnl:+.2f} ({result})")

print(f"\n    Settlement total: revenue=${total_settlement_revenue:.2f} cost=${total_settlement_cost:.2f} pnl=${total_settlement_revenue - total_settlement_cost:+.2f}")

# 4. Open positions
print(f"\n  OPEN POSITIONS:")
pos = _get("/portfolio/positions", params={"settlement_status": "unsettled"})
total_exposure = 0
if pos:
    mkt_pos = pos.get("market_positions", [])
    for p in mkt_pos:
        fp = float(p.get("position_fp", 0) or 0)
        if fp == 0:
            continue
        ticker = p.get("ticker", "")
        exposure = float(p.get("market_exposure_dollars", 0) or 0)
        traded = float(p.get("total_traded_dollars", 0) or 0)
        rpnl = float(p.get("realized_pnl_dollars", 0) or 0)
        fees = float(p.get("fees_paid_dollars", 0) or 0)
        total_exposure += exposure
        direction = "LONG" if fp > 0 else "SHORT"
        print(f"    {ticker:<40} {direction} {abs(int(fp)):>4} contracts | exposure ${exposure:.2f} | traded ${traded:.2f} | rpnl ${rpnl:+.2f} | fees ${fees:.2f}")

print(f"\n    Total exposure: ${total_exposure:.2f}")

# 5. BY MARKET TYPE
print(f"\n  BY MARKET TYPE:")
print(f"    {'Type':<15} {'Fills':>6} {'Bought':>10} {'Sold':>10} {'Fees':>8} {'Net':>10}")
print(f"    {'-'*60}")
for mtype in sorted(by_type.keys()):
    d = by_type[mtype]
    net = d["sell_proceeds"] - d["buy_cost"] - d["fees"]
    print(f"    {mtype:<15} {d['count']:>6} ${d['buy_cost']:>8.2f} ${d['sell_proceeds']:>8.2f} ${d['fees']:>6.2f} ${net:>+8.2f}")

# 6. Overall P&L accounting
print(f"\n  OVERALL P&L ACCOUNTING:")
deposited = 250  # $150 + $100
print(f"    Total deposited:  ${deposited:.2f}")
print(f"    Current equity:   ${total_equity:.2f}")
print(f"    Total P&L:        ${total_equity - deposited:+.2f}")
print(f"    Return:           {((total_equity / deposited) - 1) * 100:+.1f}%")

# Breakdown
print(f"\n    Breakdown:")
print(f"      Settled P&L:      ${total_settlement_revenue - total_settlement_cost:+.2f}")
print(f"      Sell proceeds:    ${total_sells_proceeds:.2f}")
print(f"      Fees paid:        ${total_fees:.2f}")
print(f"      Open exposure:    ${total_exposure:.2f}")

print(f"\n" + "=" * 70)
