import os
env_path = os.path.join(os.path.dirname(__file__), ".env")
try:
    with open(env_path, "r") as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                os.environ[k] = v.strip(' "\'')
except Exception as e:
    print(f"Failed to load .env: {e}")

import klipy
from clients import serper

print("=== KLIPY TEST ===")
gif_url = klipy.resolve("happy")
if gif_url:
    print(f"[SUCCESS] Klipy returned: {gif_url}")
else:
    print("[FAIL] Klipy returned None")

print("\n=== SERPER IMAGES TEST ===")
img_url = serper.search_images("nature photography")
if img_url:
    print(f"[SUCCESS] Serper Images returned: {img_url}")
else:
    print("[FAIL] Serper Images returned None")

print("\n=== SERPER NEWS TEST ===")
news = serper.search_news("React 19 updates")
if news and "No recent news found" not in news:
    print(f"[SUCCESS] Serper News returned:\n{news}")
else:
    print("[FAIL] Serper News returned None or no results")
