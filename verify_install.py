"""Quick verification script — run on the server to check file integrity."""
import hashlib
import os

project_dir = os.path.dirname(os.path.abspath(__file__))
main_py = os.path.join(project_dir, "main.py")

with open(main_py) as f:
    content = f.read()
    lines = content.split("\n")

print(f"main.py: {len(lines)} lines")
print(f"MD5: {hashlib.md5(content.encode()).hexdigest()}")
print()

# Check for the known bug
for i, line in enumerate(lines, 1):
    stripped = line.strip()
    if "generate_buy_signals(forecasts," in stripped:
        print(f"!!! BUG FOUND at line {i}: {stripped}")
    if "for k, v in forecasts." in stripped and "forecasts_today" not in stripped and "forecasts_tomorrow" not in stripped:
        print(f"!!! BUG FOUND at line {i}: {stripped}")

# Show critical lines
print("Lines 165-175:")
for i in range(164, min(175, len(lines))):
    print(f"  {i+1}: {lines[i]}")

print()
print(f"Total lines: {len(lines)}")
print("If you see 409 lines and NO '!!! BUG FOUND', the file is correct.")
