#!/bin/bash
# Install the Kalshi Weather Bot as a systemd service
# This ensures it runs 24/7 and auto-restarts on crash

cat > /etc/systemd/system/kalshi-bot.service << 'SVCEOF'
[Unit]
Description=Kalshi Weather Trading Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/kalshi-weather-bot
ExecStartPre=/bin/bash -c 'find /root/kalshi-weather-bot -name __pycache__ -exec rm -rf {} + 2>/dev/null; true'
ExecStart=/root/weather-bot/venv/bin/python -B /root/kalshi-weather-bot/main.py
Restart=always
RestartSec=30
StandardOutput=append:/root/kalshi-weather-bot/bot.log
StandardError=append:/root/kalshi-weather-bot/bot.log

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable kalshi-bot
systemctl start kalshi-bot
echo "Service installed and started. Commands:"
echo "  Status:  systemctl status kalshi-bot"
echo "  Stop:    systemctl stop kalshi-bot"
echo "  Start:   systemctl start kalshi-bot"
echo "  Restart: systemctl restart kalshi-bot"
echo "  Log:     tail -50 /root/kalshi-weather-bot/bot.log"
