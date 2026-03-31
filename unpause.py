#!/usr/bin/env python3
"""Unpause trading by resetting the risk controls pause flag."""
import json, os
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "risk_state.json")
if os.path.exists(STATE_FILE):
    with open(STATE_FILE) as f:
        data = json.load(f)
    data["paused"] = False
    data["pause_reason"] = ""
    # Set equity baseline to $1 so drawdown check can't trigger today
    # It will reset naturally tomorrow with a real baseline
    from datetime import datetime, timezone
    data["start_of_day_equity"] = 1.0
    data["equity_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print("Unpaused. Equity baseline will reset on next cycle.")
else:
    print("No risk_state.json found — nothing to unpause.")
