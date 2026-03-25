import subprocess
subprocess.run(["systemctl", "stop", "kalshi-bot"], check=True)
print("Bot STOPPED")
result = subprocess.run(["systemctl", "is-active", "kalshi-bot"], capture_output=True, text=True)
print(f"Status: {result.stdout.strip()}")
