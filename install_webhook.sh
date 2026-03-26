#!/bin/bash
# One-time setup: install the deploy webhook as a systemd service

cat > /etc/systemd/system/deploy-webhook.service << 'SVCEOF'
[Unit]
Description=Kalshi Bot Deploy Webhook
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/kalshi-weather-bot
ExecStart=/root/weather-bot/venv/bin/python -B /root/kalshi-weather-bot/deploy_webhook.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SVCEOF

# Open port 9876 in UFW if active
ufw status | grep -q "active" && ufw allow 9876/tcp 2>/dev/null

systemctl daemon-reload
systemctl enable deploy-webhook
systemctl start deploy-webhook

echo ""
echo "✅ Deploy webhook installed and running on port 9876"
echo ""
echo "Endpoints:"
echo "  Status:  GET  http://$(curl -s ifconfig.me):9876/status?token=YOUR_WEBHOOK_TOKEN_HERE"
echo "  Deploy:  POST http://$(curl -s ifconfig.me):9876/deploy?token=YOUR_WEBHOOK_TOKEN_HERE"
echo "  Logs:    GET  http://$(curl -s ifconfig.me):9876/logs?token=YOUR_WEBHOOK_TOKEN_HERE&n=50"
echo ""
echo "Commands:"
echo "  Status:  systemctl status deploy-webhook"
echo "  Restart: systemctl restart deploy-webhook"
echo "  Log:     journalctl -u deploy-webhook -f"
