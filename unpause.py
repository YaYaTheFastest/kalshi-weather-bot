#!/usr/bin/env python3
"""Unpause trading by resetting the risk controls pause flag."""
import json, os
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "risk_state.json")
if os.path.exists(STATE_FILE):
    with open(STATE_FILE) as f:
        data = json.load(f)
    data["paused"] = False
    data["pause_reason"] = ""
    # Reset equity baseline to current so it doesn't immediately re-pause
    data["start_of_day_equity"] = None
    data["equity_date"] = None
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print("Unpaused. Equity baseline will reset on next cycle.")
else:
    print("No risk_state.json found — nothing to unpause.")
