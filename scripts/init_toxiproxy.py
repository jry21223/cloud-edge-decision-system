from __future__ import annotations

import os
import time

import httpx

API = os.getenv("TOXIPROXY_API_URL", "http://toxiproxy:8474").rstrip("/")
UPSTREAM = os.getenv("TOXIPROXY_UPSTREAM", "cloud-node:8000")

for attempt in range(60):
    try:
        with httpx.Client(timeout=1.0) as client:
            existing = client.get(f"{API}/proxies")
            existing.raise_for_status()
            proxies = existing.json()
            if "cloud" in proxies:
                client.delete(f"{API}/proxies/cloud")
            created = client.post(
                f"{API}/proxies",
                json={"name": "cloud", "listen": "0.0.0.0:8666", "upstream": UPSTREAM},
            )
            created.raise_for_status()
            print("Toxiproxy cloud proxy ready")
            raise SystemExit(0)
    except (httpx.HTTPError, OSError):
        time.sleep(1)

raise SystemExit("Toxiproxy API did not become ready")
