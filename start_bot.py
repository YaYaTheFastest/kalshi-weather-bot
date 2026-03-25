#!/usr/bin/env python3
"""start_bot.py — Enable and start the Kalshi bot service."""
import subprocess

def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, r.stdout.strip() + r.stderr.strip()

print("Enabling kalshi-bot service...")
rc, out = run("systemctl enable kalshi-bot")
print(f"  enable: rc={rc} {out}")

print("Starting kalshi-bot service...")
rc, out = run("systemctl start kalshi-bot")
print(f"  start: rc={rc} {out}")

import time
time.sleep(2)

rc, out = run("systemctl is-active kalshi-bot")
print(f"  status: {out}")

rc, out = run("systemctl is-enabled kalshi-bot")
print(f"  enabled: {out}")

# Show last few log lines to confirm it's running
rc, out = run("tail -5 /root/kalshi-weather-bot/bot.log")
print(f"\nLast log lines:\n{out}")

print("\nBot is enabled and started.")
