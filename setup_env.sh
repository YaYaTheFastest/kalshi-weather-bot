#!/usr/bin/env bash
# =============================================================
# setup_env.sh
# Interactive setup script for the Kalshi Weather Trading Bot.
# Run once to create your .env file with API credentials.
#
# Usage:
#   chmod +x setup_env.sh
#   ./setup_env.sh
# =============================================================

set -euo pipefail

ENV_FILE=".env"
TEMPLATE_FILE=".env.template"

echo ""
echo "=================================================="
echo " Kalshi Weather Bot — Environment Setup"
echo "=================================================="
echo ""

# Check template exists
if [[ ! -f "$TEMPLATE_FILE" ]]; then
    echo "ERROR: .env.template not found. Run this script from the bot directory."
    exit 1
fi

# Warn if .env already exists
if [[ -f "$ENV_FILE" ]]; then
    read -rp ".env already exists. Overwrite? [y/N]: " overwrite
    if [[ "${overwrite,,}" != "y" ]]; then
        echo "Aborting. Existing .env was not modified."
        exit 0
    fi
fi

# Copy template as starting point
cp "$TEMPLATE_FILE" "$ENV_FILE"
echo "Copied .env.template -> .env"
echo ""

# ---- Collect inputs ------------------------------------------

read -rp "Kalshi API Key (UUID): " KALSHI_KEY
while [[ -z "$KALSHI_KEY" ]]; do
    echo "Kalshi API Key cannot be empty."
    read -rp "Kalshi API Key (UUID): " KALSHI_KEY
done

echo ""
echo "Paste your RSA private key in PEM format."
echo "Enter the entire key including -----BEGIN/END----- lines."
echo "When done, press Enter then Ctrl+D:"
KALSHI_PEM_RAW=$(cat)

# Convert actual newlines to \n literals for .env compatibility
KALSHI_PEM_ESCAPED=$(echo "$KALSHI_PEM_RAW" | awk '{printf "%s\\n", $0}' | sed 's/\\n$//')

echo ""
read -rp "Trading mode — enable DRY RUN? [Y/n]: " DRY_MODE_INPUT
DRY_MODE="true"
if [[ "${DRY_MODE_INPUT,,}" == "n" ]]; then
    echo ""
    echo "⚠️  WARNING: You are about to enable LIVE TRADING."
    echo "   Real money will be spent. Make sure your risk limits are correct."
    read -rp "Type 'LIVE' to confirm live mode: " live_confirm
    if [[ "$live_confirm" != "LIVE" ]]; then
        echo "Defaulting to DRY RUN mode."
        DRY_MODE="true"
    else
        DRY_MODE="false"
    fi
fi

read -rp "Your email for NOAA User-Agent [leave blank to keep default]: " NOAA_EMAIL
if [[ -n "$NOAA_EMAIL" ]]; then
    NOAA_UA="KalshiWeatherBot/1.0 ($NOAA_EMAIL)"
else
    NOAA_UA="KalshiWeatherBot/1.0 (bot@example.com)"
fi

# ---- Write to .env -------------------------------------------

# Use Python to do the substitutions safely (handles special chars)
python3 - <<PYEOF
import re

with open("$ENV_FILE", "r") as f:
    content = f.read()

replacements = {
    r"^KALSHI_ACCESS_KEY=.*": "KALSHI_ACCESS_KEY=${KALSHI_KEY}",
    r"^KALSHI_PRIVATE_KEY_PEM=.*": "KALSHI_PRIVATE_KEY_PEM=${KALSHI_PEM_ESCAPED}",
    r"^DRY_RUN=.*": "DRY_RUN=${DRY_MODE}",
    r"^NOAA_USER_AGENT=.*": "NOAA_USER_AGENT=${NOAA_UA}",
}

for pattern, replacement in replacements.items():
    content = re.sub(pattern, replacement, content, flags=re.MULTILINE)

with open("$ENV_FILE", "w") as f:
    f.write(content)

print("✅ .env file written successfully.")
PYEOF

# ---- Permissions ---------------------------------------------
chmod 600 "$ENV_FILE"
echo "Permissions set to 600 (owner read/write only)."
echo ""
echo "=================================================="
echo " Setup complete!"
echo "=================================================="
echo ""
echo "Next steps:"
echo "  1. Review your .env file: cat .env"
echo "  2. Install dependencies: pip install -r requirements.txt"
echo "  3. Start the bot in dry-run mode: python main.py"
echo "  4. Watch the logs: tail -f kalshi_weather_bot.log"
echo ""
echo "To switch to live trading, edit .env and set DRY_RUN=false"
echo "(or re-run this script)."
echo ""
