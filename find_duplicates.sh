#!/bin/bash
echo "=== Searching for ANY main.py files ==="
find / -name "main.py" -path "*kalshi*" 2>/dev/null

echo ""
echo "=== Searching for ANY main.pyc files ==="
find / -name "main*.pyc" -path "*kalshi*" 2>/dev/null

echo ""
echo "=== Searching for ANY __pycache__ in project ==="
find /root/kalshi-weather-bot -name "__pycache__" 2>/dev/null

echo ""
echo "=== Searching for .pyc files anywhere in project ==="
find /root/kalshi-weather-bot -name "*.pyc" 2>/dev/null

echo ""
echo "=== Check venv site-packages for any kalshi modules ==="
find /root/weather-bot/venv -name "*kalshi*" -o -name "decision_engine*" -o -name "noaa_scanner*" -o -name "gas_*" 2>/dev/null | head -20

echo ""
echo "=== Line count of main.py ==="
wc -l /root/kalshi-weather-bot/main.py
