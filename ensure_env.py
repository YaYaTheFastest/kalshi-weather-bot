#!/usr/bin/env python3
"""
ensure_env.py — Check that required secrets are set in the server .env file.
Does NOT contain any actual secret values — those must be set manually.
"""
import os, sys

ENV_FILE = "/root/kalshi-weather-bot/.env"
REQUIRED_VARS = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "EIA_API_KEY",
    "KALSHI_ACCESS_KEY",
    "KALSHI_PRIVATE_KEY_PEM",
    "DEPLOY_WEBHOOK_TOKEN",
]

existing = {}
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key = line.split("=", 1)[0].strip()
                existing[key] = True

missing = [v for v in REQUIRED_VARS if v not in existing]
if missing:
    print(f"MISSING env vars in {ENV_FILE}:")
    for v in missing:
        print(f"  - {v}")
    print(f"\nAdd these to {ENV_FILE} before restarting services.")
    sys.exit(1)
else:
    print(f"All {len(REQUIRED_VARS)} required vars present in {ENV_FILE}")
