"""
deploy_webhook.py
-----------------
Lightweight HTTP webhook that allows remote git pull + service restart.
Listens on port 9876. Requires a secret token in the URL to authenticate.

Usage:
  POST http://server-ip:9876/deploy?token=SECRET
  GET  http://server-ip:9876/status?token=SECRET

Runs as a systemd service alongside the trading bot.
"""

import http.server
import json
import logging
import os
import subprocess
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT = 9876
SECRET_TOKEN = os.getenv("DEPLOY_WEBHOOK_TOKEN", "")
if not SECRET_TOKEN:
    # SECURITY: Do not hardcode tokens. Set DEPLOY_WEBHOOK_TOKEN env var.
    # For backward compat during transition, use the legacy token.
    # TODO: Remove this fallback after rotating the token on the server.
    SECRET_TOKEN = "lUYhlQEuTMDCtP7VFQ7wlrqF9hZbsIIS4sHx464Ob90"
BOT_DIR = "/root/kalshi-weather-bot"
SERVICE_NAME = "kalshi-bot"
LOG_FILE = "/root/kalshi-weather-bot/webhook.log"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def _run_cmd(cmd: str, cwd: str = BOT_DIR, timeout: int = 60) -> tuple[int, str]:
    """Run a shell command and return (returncode, output)."""
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        output = result.stdout + result.stderr
        return result.returncode, output.strip()
    except subprocess.TimeoutExpired:
        return -1, "Command timed out"
    except Exception as exc:
        return -1, str(exc)


def do_deploy() -> dict:
    """Pull latest code and restart the bot service."""
    steps = []

    # 1. Git pull
    rc, out = _run_cmd("git pull")
    steps.append({"step": "git_pull", "rc": rc, "output": out})
    if rc != 0:
        return {"success": False, "steps": steps, "error": "git pull failed"}

    # 2. Clear pycache
    _run_cmd("find . -name __pycache__ -exec rm -rf {} + 2>/dev/null; true")
    steps.append({"step": "clear_pycache", "rc": 0})

    # 3. Only restart bot service if it's currently enabled
    #    This prevents a deploy from accidentally overriding a manual pause/disable.
    rc_enabled, enabled_out = _run_cmd(f"systemctl is-enabled {SERVICE_NAME}")
    bot_enabled = enabled_out.strip() == "enabled"
    
    if bot_enabled:
        rc, out = _run_cmd(f"systemctl restart {SERVICE_NAME}")
        steps.append({"step": "restart_service", "rc": rc, "output": out})
        if rc != 0:
            return {"success": False, "steps": steps, "error": "restart failed"}
    else:
        steps.append({"step": "restart_service", "rc": 0, "output": "SKIPPED — service is disabled (paused). Will not auto-restart."})
        logger.info("Bot service is disabled — skipping restart on deploy")

    # 3b. Restart webhook service (to pick up code changes)
    # Do this in background so the current request can still respond
    import time as _time
    def _restart_webhook():
        _time.sleep(2)
        os.system("systemctl restart deploy-webhook")
    threading.Thread(target=_restart_webhook, daemon=True).start()
    steps.append({"step": "webhook_restart_scheduled", "rc": 0})

    # 4. Check service status
    rc, out = _run_cmd(f"systemctl is-active {SERVICE_NAME}")
    steps.append({"step": "verify_active", "rc": rc, "output": out})

    return {
        "success": True,  # deploy itself succeeded even if bot stays stopped
        "bot_running": out.strip() == "active",
        "bot_enabled": bot_enabled,
        "steps": steps,
        "time": datetime.now(timezone.utc).isoformat(),
    }


def get_status() -> dict:
    """Get current bot status."""
    # Service status
    rc, active = _run_cmd(f"systemctl is-active {SERVICE_NAME}")

    # Last 5 log lines
    _, log_tail = _run_cmd(f"tail -5 {BOT_DIR}/bot.log")

    # Process info
    _, ps_info = _run_cmd(f"ps -p $(systemctl show -p MainPID --value {SERVICE_NAME}) -o pid,etime,rss --no-headers")

    # Git info
    _, git_head = _run_cmd("git log --oneline -1")

    # Last gas scan
    _, gas_last = _run_cmd(f"grep 'Gas:' {BOT_DIR}/bot.log | tail -1")

    # Positions
    _, positions = _run_cmd(f"grep 'positions:' {BOT_DIR}/bot.log | tail -1")

    return {
        "active": active.strip() == "active",
        "service_status": active.strip(),
        "git_head": git_head,
        "process": ps_info,
        "last_gas_scan": gas_last,
        "last_positions": positions,
        "log_tail": log_tail,
        "time": datetime.now(timezone.utc).isoformat(),
    }


class WebhookHandler(http.server.BaseHTTPRequestHandler):
    def _check_token(self, params: dict) -> bool:
        import hmac
        token = params.get("token", [None])[0]
        if not token:
            return False
        # Use constant-time comparison to prevent timing attacks
        return hmac.compare_digest(token, SECRET_TOKEN)

    def _respond(self, code: int, data: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if not self._check_token(params):
            self._respond(403, {"error": "Invalid token"})
            return

        if parsed.path == "/status":
            status = get_status()
            self._respond(200, status)
        elif parsed.path == "/logs":
            n = int(params.get("n", [30])[0])
            _, logs = _run_cmd(f"tail -{n} {BOT_DIR}/bot.log")
            self._respond(200, {"logs": logs})
        else:
            self._respond(404, {"error": "Not found. Use /status or /deploy"})

    def do_POST(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if not self._check_token(params):
            self._respond(403, {"error": "Invalid token"})
            return

        if parsed.path == "/deploy":
            logger.info("Deploy triggered via webhook")
            result = do_deploy()
            logger.info("Deploy result: %s", result["success"])
            self._respond(200, result)
        elif parsed.path == "/run":
            # Run a WHITELISTED script in the project directory
            # Security: only allow specific scripts, no args injection
            ALLOWED_SCRIPTS = {
                "verify_api.py", "debug_positions.py", "trade_log.py",
                "accounting.py", "stop_bot.py", "start_bot.py",
            }
            script = params.get("script", [None])[0]
            if not script or not script.endswith(".py"):
                self._respond(400, {"error": "Provide ?script=filename.py"})
                return
            # Security: strip path separators to prevent traversal
            script_name = os.path.basename(script)
            if script_name not in ALLOWED_SCRIPTS:
                self._respond(403, {"error": f"Script '{script_name}' not in whitelist. Allowed: {sorted(ALLOWED_SCRIPTS)}"})
                return
            # Security: use shell=False with explicit arg list — no injection
            import shlex
            raw_args = params.get("args", [""])[0]
            try:
                args_list = shlex.split(raw_args) if raw_args else []
            except ValueError:
                args_list = []
            cmd_list = ["/root/weather-bot/venv/bin/python", "-B", f"{BOT_DIR}/{script_name}"] + args_list
            logger.info("Running whitelisted script: %s", cmd_list)
            try:
                result = subprocess.run(
                    cmd_list, cwd=BOT_DIR, capture_output=True, text=True, timeout=300
                )
                out = (result.stdout + result.stderr).strip()
                rc = result.returncode
            except subprocess.TimeoutExpired:
                rc, out = -1, "Command timed out"
            except Exception as exc:
                rc, out = -1, str(exc)
            self._respond(200, {"rc": rc, "output": out[-5000:]})
        else:
            self._respond(404, {"error": "Not found. POST to /deploy or /run"})

    def log_message(self, format, *args):
        """Suppress default HTTP logs — we use our own logger."""
        logger.debug(format, *args)


def main():
    server = http.server.HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    logger.info("Deploy webhook listening on port %d", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Webhook server shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
