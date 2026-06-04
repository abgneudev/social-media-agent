import os
import sys

# Load env manually
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
import serpapi

print("=== KLIPY TEST ===")
gif_url = klipy.resolve("happy")
if gif_url:
    print(f"[SUCCESS] Klipy returned: {gif_url}")
else:
    print("[FAIL] Klipy returned None")

print("\n=== SERPAPI TEST ===")
img_url = serpapi.search_image("dashboard UI design dribbble")
if img_url:
    print(f"[SUCCESS] SerpAPI returned: {img_url}")
else:
    print("[FAIL] SerpAPI returned None")
