#!/usr/bin/env python3
"""stop_bot.py — Stop and disable the Kalshi bot service."""
import subprocess
import sys

def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, r.stdout.strip() + r.stderr.strip()

print("Stopping kalshi-bot service...")
rc, out = run("systemctl stop kalshi-bot")
print(f"  stop: rc={rc} {out}")

print("Disabling kalshi-bot service...")
rc, out = run("systemctl disable kalshi-bot")
print(f"  disable: rc={rc} {out}")

rc, out = run("systemctl is-active kalshi-bot")
print(f"  status: {out}")

rc, out = run("systemctl is-enabled kalshi-bot")
print(f"  enabled: {out}")

print("\nBot is stopped and disabled.")
