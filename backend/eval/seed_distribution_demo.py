"""Create a demo merchant with approved, distributed products through the real API (no direct DB writes).

    AR_API=http://localhost:8000 python eval/seed_distribution_demo.py   # prints the agent key as JSON

Used by the MCP client check and handy for trying the agent API locally.
"""

from __future__ import annotations

import json
import os
import time

import httpx

API = os.environ.get("AR_API", "http://localhost:8000").rstrip("/")
PRODUCTS = [
    {"name": "Industrial Motor 2HP", "brand": "Havells", "sku": "MOT-230", "regular_price": "12000", "currency": "INR",
     "description": "Three-phase induction motor for pumps and compressors.", "availability": "InStock",
     "product_url": "https://demo.example/p/motor-2hp", "images": [{"url": "https://demo.example/img/motor.jpg"}],
     "attributes": [{"label": "Voltage", "value": "230 V"}, {"label": "Power", "value": "1.5 kW"}]},
    {"name": "DC Motor 12V", "brand": "Bosch", "sku": "MOT-12", "regular_price": "3000", "currency": "INR",
     "description": "Compact DC motor.", "availability": "InStock",
     "attributes": [{"label": "Voltage", "value": "12 V"}, {"label": "Power", "value": "100 W"}]},
    {"name": "UltraBook 14 Laptop", "brand": "Zenon", "sku": "LAP-14", "regular_price": "65000", "currency": "INR",
     "description": "Light 14-inch laptop.", "track_inventory": True, "stock_quantity": 5,
     "attributes": [{"label": "Storage", "value": "512 GB"}]},
]


def main() -> None:
    c = httpx.Client(base_url=API, timeout=30)
    email = f"demo{int(time.time())}@example.com"
    r = c.post("/api/v1/auth/register", json={"email": email, "password": "demo-pass-2026x", "business_name": "Demo Supplies"})
    r.raise_for_status()
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    ids = []
    for p in PRODUCTS:
        r = c.post("/api/v1/products", json={**p, "lifecycle_status": "active"}, headers=h)
        r.raise_for_status()
        pid = r.json()["id"]
        c.patch(f"/api/v1/products/{pid}", json={"approval_status": "approved"}, headers=h).raise_for_status()
        ids.append(pid)
    c.put("/api/v1/distribution/settings", json={"enabled": True, "public_name": "Demo Supplies",
                                                  "public_url": "https://demo.example"}, headers=h).raise_for_status()
    k = c.post("/api/v1/api-keys", json={"name": "MCP client", "scopes": ["agent:read"]}, headers=h)
    k.raise_for_status()
    print(json.dumps({"email": email, "token": h["Authorization"].split()[1], "key": k.json()["key"], "product_ids": ids}))


if __name__ == "__main__":
    main()
