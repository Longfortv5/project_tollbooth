import subprocess
import time

names = [
  "spx-regime-selector",
  "qqq-regime-selector",
  "spy-regime-selector",
  "nvda-regime-selector",
  "spacex-regime-selector",
  "spacex-options",
  "spacex-flow",
  "spacex-sentiment",
  "dax-regime-selector",
  "nkd-regime-selector",
  "gld-regime-selector",
  "uso-regime-selector",
  "iwm-regime-selector",
  "aapl-regime-selector",
  "msft-regime-selector",
  "amzn-regime-selector",
  "meta-regime-selector",
  "googl-regime-selector",
  "tsla-regime-selector",
  "btc-regime-selector",
]

import os
env = os.environ.copy()
env["SMITHERY_API_KEY"] = "d79bd21b-8261-4029-b7b5-81068b749f0a"
for name in names:
    qualified_name = f"longfort/{name}"
    print(f"Publishing {qualified_name}...")
    cmd = ["npx", "smithery", "mcp", "publish", f"https://api.longfortpro.com/mcp/{name}", "-n", qualified_name]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True)
    print(res.stdout)
    if res.stderr:
        print(f"Stderr: {res.stderr}")
    time.sleep(2)
