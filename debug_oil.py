"""Debug script to find correct oil market tickers on Kalshi."""
import sys, os, json
_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path: sys.path.insert(0, _dir)

from kalshi_client import _get, _fetch_markets_by_series

# Test various series tickers for oil
candidates = ["KXWTI", "KXWTIW", "KXWTID", "KXCRUDE", "KXOIL", "WTI"]
for series in candidates:
    markets = _fetch_markets_by_series(series)
    print(f"  {series}: {len(markets)} markets")
    if markets:
        for m in markets[:3]:
            print(f"    {m.get('ticker')} | {m.get('status')} | ask={m.get('yes_ask')}")

# Also search all open markets for anything with "WTI" or "oil" or "crude"
print("\nSearching ALL open markets for oil-related tickers...")
cursor = None
oil_tickers = []
pages = 0
while pages < 5:  # limit pages
    params = {"status": "open", "limit": 1000}
    if cursor:
        params["cursor"] = cursor
    data = _get("/markets", params=params)
    if not data:
        break
    for m in data.get("markets", []):
        t = m.get("ticker", "").upper()
        title = m.get("title", "").upper()
        if any(k in t or k in title for k in ["WTI", "OIL", "CRUDE", "PETROL"]):
            oil_tickers.append({
                "ticker": m["ticker"],
                "event_ticker": m.get("event_ticker", ""),
                "series_ticker": m.get("series_ticker", ""),
                "title": m.get("title", ""),
                "status": m.get("status", ""),
                "yes_ask": m.get("yes_ask", ""),
            })
    cursor = data.get("cursor")
    if not cursor:
        break
    pages += 1

print(f"Found {len(oil_tickers)} oil-related markets")
# Get unique series tickers
series_set = set()
for t in oil_tickers:
    if t["series_ticker"]:
        series_set.add(t["series_ticker"])
    # Also try to extract from event_ticker
    if t["event_ticker"]:
        parts = t["event_ticker"].split("-")
        if parts:
            series_set.add(parts[0])

print(f"Unique series: {sorted(series_set)}")
for t in oil_tickers[:10]:
    print(f"  {t['ticker']} | series={t.get('series_ticker','')} | {t['title'][:60]}")
