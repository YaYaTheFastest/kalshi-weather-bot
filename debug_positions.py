import sys, os
_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path: sys.path.insert(0, _dir)
from kalshi_client import get_positions, get_balance

balance = get_balance()
positions = get_positions()
print(f"Balance: ${balance:.2f}")
print(f"Open positions: {len(positions)}")
for p in positions:
    print(f"  {p.ticker} | contracts={p.market_exposure} | exposure=${p.market_exposure_dollars:.2f} | traded=${p.total_traded:.2f} | fees=${p.fees_paid:.2f} | pnl=${p.realized_pnl:.2f}")
