"""
verify_api.py — Diagnostic to test all Kalshi portfolio API endpoints
with the correct field names from the docs.

Run on the server: python3 verify_api.py
"""
import sys, os, json
_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path: sys.path.insert(0, _dir)

from kalshi_client import _get

print("=" * 60)
print("KALSHI API VERIFICATION")
print("=" * 60)

# 1. Balance
print("\n--- GET /portfolio/balance ---")
bal = _get("/portfolio/balance")
if bal:
    print(f"  Raw response: {json.dumps(bal, indent=2)}")
    print(f"  balance (cents): {bal.get('balance')}")
    print(f"  portfolio_value (cents): {bal.get('portfolio_value')}")
    cash = bal.get('balance', 0) / 100
    pv = bal.get('portfolio_value', 0) / 100
    print(f"  → Cash: ${cash:.2f} | Portfolio Value: ${pv:.2f} | Total: ${cash+pv:.2f}")
else:
    print("  FAILED — no response")

# 2. Positions (default)
print("\n--- GET /portfolio/positions (default) ---")
pos = _get("/portfolio/positions")
if pos:
    mkt_pos = pos.get("market_positions", [])
    evt_pos = pos.get("event_positions", [])
    print(f"  market_positions: {len(mkt_pos)}")
    print(f"  event_positions: {len(evt_pos)}")
    for p in mkt_pos[:5]:
        print(f"    ticker: {p.get('ticker')}")
        print(f"      position_fp: {p.get('position_fp')}")
        print(f"      market_exposure_dollars: {p.get('market_exposure_dollars')}")
        print(f"      total_traded_dollars: {p.get('total_traded_dollars')}")
        print(f"      realized_pnl_dollars: {p.get('realized_pnl_dollars')}")
        print(f"      fees_paid_dollars: {p.get('fees_paid_dollars')}")
    if not mkt_pos:
        print("  (empty — trying with count_filter)")
else:
    print("  FAILED — no response")

# 3. Positions with count_filter
print("\n--- GET /portfolio/positions?count_filter=position,total_traded ---")
pos2 = _get("/portfolio/positions", params={"count_filter": "position,total_traded"})
if pos2:
    mkt_pos = pos2.get("market_positions", [])
    print(f"  market_positions: {len(mkt_pos)}")
    for p in mkt_pos[:10]:
        print(f"    {p.get('ticker')} | pos={p.get('position_fp')} | exposure=${p.get('market_exposure_dollars')} | traded=${p.get('total_traded_dollars')}")
else:
    print("  FAILED")

# 4. Positions with settlement_status
print("\n--- GET /portfolio/positions?settlement_status=unsettled ---")
pos3 = _get("/portfolio/positions", params={"settlement_status": "unsettled"})
if pos3:
    mkt_pos = pos3.get("market_positions", [])
    print(f"  market_positions: {len(mkt_pos)}")
    for p in mkt_pos[:10]:
        print(f"    {p.get('ticker')} | pos={p.get('position_fp')} | exposure=${p.get('market_exposure_dollars')}")
else:
    print("  FAILED")

# 5. Fills (latest 5)
print("\n--- GET /portfolio/fills (latest 5) ---")
fills = _get("/portfolio/fills", params={"limit": 5})
if fills:
    for f in fills.get("fills", []):
        print(f"    {f.get('ticker')} | {f.get('action')} {f.get('side')} | "
              f"count={f.get('count_fp')} | yes_price=${f.get('yes_price_dollars')} | "
              f"no_price=${f.get('no_price_dollars')} | fee=${f.get('fee_cost')}")
else:
    print("  FAILED")

# 6. Settlements
print("\n--- GET /portfolio/settlements (latest 5) ---")
settlements = _get("/portfolio/settlements", params={"limit": 5})
if settlements:
    for s in settlements.get("settlements", []):
        print(f"    {s.get('ticker')} | revenue=${s.get('revenue_dollars', s.get('revenue'))} | "
              f"yes_cost=${s.get('yes_total_cost_dollars')} | no_cost=${s.get('no_total_cost_dollars')}")
else:
    print("  FAILED")

# 7. Account limits
print("\n--- GET /account/limits ---")
limits = _get("/account/limits")
if limits:
    print(f"  Tier: {limits.get('usage_tier')}")
    print(f"  Read limit: {limits.get('read_limit')}/sec")
    print(f"  Write limit: {limits.get('write_limit')}/sec")
else:
    print("  FAILED")

print("\n" + "=" * 60)
print("VERIFICATION COMPLETE")
print("=" * 60)
