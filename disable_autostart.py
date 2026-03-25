import subprocess
# Disable the service so it won't auto-start
subprocess.run(["systemctl", "stop", "kalshi-bot"], check=True)
subprocess.run(["systemctl", "disable", "kalshi-bot"], check=True)
result = subprocess.run(["systemctl", "is-active", "kalshi-bot"], capture_output=True, text=True)
result2 = subprocess.run(["systemctl", "is-enabled", "kalshi-bot"], capture_output=True, text=True)
print(f"Status: {result.stdout.strip()}")
print(f"Enabled: {result2.stdout.strip()}")
print("Bot is STOPPED and will NOT auto-restart on deploy or reboot")
