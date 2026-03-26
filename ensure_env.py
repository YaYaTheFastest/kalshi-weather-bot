#!/usr/bin/env python3
"""
ensure_env.py — Ensure .env file has required secrets.
Run on the server after deploying the security hardening changes.
Checks that env vars previously hardcoded in source are now in .env.
"""
import os

ENV_FILE = "/root/kalshi-weather-bot/.env"
REQUIRED_VARS = {
    "TELEGRAM_BOT_TOKEN": "8701485015:AAH_GUm0x7s4gZIH3tRx1ahFKCpPmfN_2xw",
    "TELEGRAM_CHAT_ID": "8718921224",
    "EIA_API_KEY": "xZLioPQmYYDd92cVykFT1q1P2kqKEl71t8huGsCa",
}

# Read existing .env
existing = {}
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                existing[key.strip()] = val.strip()

# Add missing vars
added = []
with open(ENV_FILE, "a") as f:
    for key, default_val in REQUIRED_VARS.items():
        if key not in existing:
            f.write(f"\n{key}={default_val}")
            added.append(key)
            print(f"  ADDED: {key}")
        else:
            print(f"  OK: {key} already in .env")

if added:
    print(f"\nAdded {len(added)} vars to {ENV_FILE}")
else:
    print(f"\nAll required vars present in {ENV_FILE}")
