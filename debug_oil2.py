import sys, os, json
_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path: sys.path.insert(0, _dir)
from kalshi_client import _fetch_markets_by_series

markets = _fetch_markets_by_series("KXWTI")
if markets:
    # Show full details of first market
    m = markets[0]
    print("Full market data:")
    for k, v in sorted(m.items()):
        if v is not None and v != "" and v != 0:
            print(f"  {k}: {v}")
    
    print(f"\n\nAll tickers:")
    for m in markets:
        t = m["ticker"]
        ask = m.get("yes_ask") or m.get("yes_ask_dollars") or m.get("yes_ask_cost") or "?"
        bid = m.get("yes_bid") or m.get("yes_bid_dollars") or m.get("yes_bid_cost") or "?"
        status = m.get("status", "?")
        print(f"  {t} | ask={ask} | bid={bid} | status={status}")
