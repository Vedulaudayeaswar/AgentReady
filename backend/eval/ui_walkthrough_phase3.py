"""Phase 3 browser walkthrough: AI commerce distribution through the real UI (Chromium via Playwright).

    AR_WEB=http://localhost:5173 python eval/ui_walkthrough_phase3.py                       # dev stack
    AR_WEB=http://localhost:8080 AR_HOOK=http://host.docker.internal:8766 python eval/ui_walkthrough_phase3.py  # docker

It runs a local webhook receiver on port 8766 (verifies Standard-Webhooks signatures with the secret the UI shows).
Webhook destinations on private addresses are refused in production, so the API must run with the DEV-ONLY settings
ALLOW_PRIVATE_NETWORKS=true and ALLOWED_PORTS=80,443,8766.

Step 14 connects the OFFICIAL MCP Python SDK client (eval/mcp_client_check.py) using the interpreter in AR_MCP_PY
(an environment with `pip install mcp`); the result line shows the negotiated protocol version.

Screenshots: eval/results/screenshots/p3_*.png
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = os.environ.get("AR_WEB", "http://localhost:5173").rstrip("/")
HOOK = os.environ.get("AR_HOOK", "http://hooks.localtest.me:8766").rstrip("/")  # public DNS name -> 127.0.0.1
MCP_PY = os.environ.get("AR_MCP_PY", sys.executable)
IMAGE = "http://hooks.localtest.me:8766/motor.png"  # served by the local receiver (the browser runs on this machine)
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ff"
                    "ff3f0005fe02fea7d6a8f90000000049454e44ae426082")
OUT = Path(__file__).parent / "results" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)
EMAIL = f"distribution{int(time.time())}@example.com"
PASSWORD = "merchant-pass-2026"
STORE = "Distribution Test Store"
results: list[tuple[str, str, str]] = []
console_errors: list[str] = []
bad_responses: list[str] = []
received: list[tuple[dict, bytes]] = []


class Hook(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200 if self.path == "/motor.png" else 404)
        self.send_header("Content-Type", "image/png")
        self.end_headers()
        if self.path == "/motor.png":
            self.wfile.write(PNG)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        received.append(({k.lower(): v for k, v in self.headers.items()}, body))
        self.send_response(204)
        self.end_headers()


def record(step, status, note=""):
    results.append((step, status, note))
    print(f"[{status}] {step} {note}", flush=True)


async def step(name, coro):
    try:
        note = await coro
        record(name, "PASS", note or "")
        return True
    except Exception as e:  # noqa: BLE001
        record(name, "FAIL", f"{type(e).__name__}: {str(e)[:400]}")
        return False


async def nav(page, name):
    """Click a sidebar navigation link (exact name, inside the app's nav)."""
    await page.locator("nav").get_by_role("link", name=name, exact=True).first.click()


async def main():
    srv = ThreadingHTTPServer(("0.0.0.0", 8766), Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    http = httpx.Client(base_url=BASE, timeout=60)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        page.on("console", lambda m: console_errors.append(f"{page.url} :: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"{page.url} :: PAGEERROR {e}"))
        page.on("response", lambda r: bad_responses.append(f"{r.request.method} {r.url.split('/api')[-1]} -> {r.status}")
                if "/api/" in r.url and r.status >= 400 else None)
        st: dict = {}

        async def api(method, path, body=None):
            token = await page.evaluate("() => sessionStorage.getItem('ar-token')")
            r = await page.request.fetch(f"{BASE}/api/v1{path}", method=method, data=body,
                                         headers={"Authorization": f"Bearer {token}"})
            assert r.status < 400, f"{method} {path} -> {r.status} {await r.text()}"
            return await r.json() if r.status != 204 else None

        def agent(path, method="GET", **kw):
            r = http.request(method, f"/api/v3/agents{path}", headers={"X-Catalog-Key": st["key"]}, **kw)
            assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:300]}"
            return r.json()

        # 1 ---------------------------------------------------------------- register (+ approved catalog)
        async def register():
            await page.goto(BASE + "/signup")
            await page.get_by_label("Business name").fill(STORE)
            await page.get_by_label("Your name").fill("Asha Merchant")
            await page.get_by_label("Work email").fill(EMAIL)
            await page.get_by_label("Password").fill(PASSWORD)
            await page.get_by_role("button", name="Create account").click()
            await page.wait_for_url(re.compile(r"/app/connect$"), timeout=15000)
            products = [
                {"name": "Industrial Motor 2HP", "brand": "Havells", "sku": "MOT-230", "regular_price": "12000", "currency": "INR",
                 "description": "Three-phase induction motor.", "availability": "InStock", "cost_price": "8100",
                 "product_url": "https://store.example/p/motor", "images": [{"url": IMAGE}],
                 "attributes": [{"label": "Voltage", "value": "230 V"}, {"label": "Power", "value": "1.5 kW"}]},
                {"name": "DC Motor 12V", "brand": "Bosch", "sku": "MOT-12", "regular_price": "3000", "currency": "INR",
                 "description": "Compact DC motor.", "availability": "InStock",
                 "attributes": [{"label": "Voltage", "value": "12 V"}, {"label": "Power", "value": "100 W"}]},
                {"name": "Prototype Gearbox", "sku": "GEAR-X", "regular_price": "999", "currency": "INR"},
            ]
            for p in products:
                created = await api("POST", "/products", {**p, "lifecycle_status": "active"})
                await api("PATCH", f"/products/{created['id']}", {"approval_status": "approved"})
                st[p["sku"]] = created["id"]
            return "3 products created and approved via the merchant API"
        await step("1. Register merchant", register())

        # 2 ---------------------------------------------------------------- open Distribution
        async def open_distribution():
            await nav(page, "Distribution Overview")
            await page.wait_for_url(re.compile(r"/app/distribution$"))
            await expect(page.get_by_text("Public distribution off")).to_be_visible()
            await expect(page.get_by_text("Distribution is off", exact=False)).to_be_visible()
            await page.screenshot(path=str(OUT / "p3_overview_off.png"), full_page=True)
        await step("2. Open Distribution (off by default)", open_distribution())

        # 3 ---------------------------------------------------------------- select approved products
        async def select_products():
            await nav(page, "Public Products")
            await page.get_by_label("Which products").select_option("selected")
            await page.get_by_label("Public store name").fill(STORE)
            await page.get_by_label("Website").fill("https://store.example")
            await page.get_by_role("button", name="Save settings").click()
            await expect(page.get_by_text("Distribution settings saved")).to_be_visible()
            await page.get_by_label("Select Industrial Motor 2HP").click()
            await page.get_by_label("Select DC Motor 12V").click()
            await page.get_by_role("button", name="Distribute 2").click()
            await expect(page.get_by_text(re.compile(r"Distribution enabled \(2 changed\)"))).to_be_visible()
        await step("3. Select approved products for distribution", select_products())

        # 4 ---------------------------------------------------------------- enable public distribution
        async def enable():
            await nav(page, "Distribution Overview")
            await page.get_by_role("switch", name="Public distribution").click()
            await expect(page.get_by_text("Public distribution is on")).to_be_visible()
            card = page.get_by_text("Distributed products", exact=True).locator("..")
            await expect(card).to_contain_text("2")
            ov = await api("GET", "/distribution/overview")
            assert ov["products"]["distributed"] == 2, ov["products"]
            await page.screenshot(path=str(OUT / "p3_overview_on.png"), full_page=True)
            return "2 distributed (the unselected gearbox stays private)"
        await step("4. Enable public distribution", enable())

        # 5 ---------------------------------------------------------------- preview
        async def preview():
            await nav(page, "Public Products")
            row = page.get_by_role("row", name=re.compile("Industrial Motor 2HP"))
            await row.get_by_role("button", name="Preview").click()
            pv = page.get_by_test_id("public-preview")
            await expect(pv).to_be_visible()
            await expect(page.get_by_test_id("preview-price")).to_contain_text("12,000")
            await page.get_by_role("button", name="Show JSON agents receive").click()
            text = await pv.inner_text()
            assert "8100" not in text and "cost" not in text.lower(), "cost price leaked into the preview"
            await page.screenshot(path=str(OUT / "p3_preview.png"))
            await page.keyboard.press("Escape")
        await step("5. Preview a product (public serializer)", preview())

        # 6/7 -------------------------------------------------------------- feeds
        async def make_feed(name, fmt_label):
            await nav(page, "Product Feeds")
            await page.get_by_role("button", name="New feed").first.click()
            await page.locator("#nf-name").fill(name)
            await page.get_by_label("Format").select_option(label=fmt_label)
            await page.get_by_role("button", name="Create feed").click()
            await expect(page.get_by_text("Feed created")).to_be_visible()
            card = page.get_by_test_id("feed-card").filter(has_text=name)
            await card.get_by_role("button", name="Generate now").click()
            await expect(card.get_by_text("v1 Completed")).to_be_visible(timeout=30000)
            await expect(card).to_contain_text("2 records")
            return card

        async def json_feed():
            await make_feed("Partner JSON", "JSON")
            feeds = await api("GET", "/distribution/feeds")
            f = next(x for x in feeds["items"] if x["name"] == "Partner JSON")
            assert f["latest"]["product_count"] == 2 and f["latest"]["status"] == "completed"
            return f"v{f['latest']['version']}, {f['latest']['record_count']} records, {f['latest']['size_bytes']} bytes"
        await step("6. Generate a JSON feed", json_feed())

        async def jsonld_feed():
            card = await make_feed("Schema.org", "Schema.org JSON-LD")
            st["ld_card"] = card
            await page.screenshot(path=str(OUT / "p3_feeds.png"), full_page=True)
        await step("7. Generate a JSON-LD feed", jsonld_feed())

        # 8 ---------------------------------------------------------------- feed token
        async def feed_token():
            card = st["ld_card"]
            await card.get_by_label("Feed URL name").fill("Search partner")
            await card.get_by_role("button", name="Create feed URL").click()
            url = (await page.get_by_test_id("secret-once").inner_text()).strip()
            assert "/api/v3/feeds/arf_" in url
            path = "/api/v3/feeds/" + url.split("/api/v3/feeds/")[1]
            r = http.get(path)
            assert r.status_code == 200 and r.headers["content-type"].startswith("application/ld+json"), r.status_code
            doc = r.json()
            assert doc["@type"] == "ItemList" and doc["numberOfItems"] == 2 and "8100" not in r.text
            await page.get_by_role("button", name="I've saved it").click()
            return f"public URL served {doc['numberOfItems']} products (ETag {r.headers['etag'][:10]}…)"
        await step("8. Create a feed token (public URL)", feed_token())

        # 9 ---------------------------------------------------------------- agent API
        async def agent_api():
            await nav(page, "Agent API")
            await page.get_by_label("Key name").fill("Walkthrough agent")
            await page.get_by_role("button", name="Create agent key").click()
            st["key"] = (await page.get_by_test_id("new-agent-key").inner_text()).strip()
            await page.screenshot(path=str(OUT / "p3_agent_api.png"), full_page=True)
            data = agent("/products")
            names = sorted(i["product"]["name"] for i in data["items"])
            assert names == ["DC Motor 12V", "Industrial Motor 2HP"], names
            return f"{data['total']} products via /api/v3/agents/products"
        await step("9. Retrieve products through the agent API", agent_api())

        async def search():
            data = agent("/search", params={"q": "motor", "attr": "voltage:230 V", "availability": "available",
                                            "max_price": "20000", "currency": "INR"})
            assert [i["product"]["name"] for i in data["items"]] == ["Industrial Motor 2HP"]
            return f"score {data['items'][0]['ranking']['score']} signals {data['items'][0]['ranking']['signals']}"
        await step("10. Search products using filters", search())

        async def compare():
            data = agent("/compare", method="POST", json={"product_ids": [st["MOT-230"], st["MOT-12"]]})
            rows = {r["field"]: r for r in data["comparison"]["rows"]}
            assert rows["attribute:power"]["comparable"] and rows["attribute:power"]["highest"] == [st["MOT-230"]]
            assert rows["price"]["lowest"] == [st["MOT-12"]]
            return f"power {rows['attribute:power']['normalized']}"
        await step("11. Compare two products", compare())

        async def availability():
            data = agent(f"/products/{st['MOT-230']}/availability")
            assert data["availability"]["status"] == "available" and "quantity" not in json.dumps(data)
            return data["availability"]["status"]
        await step("12. Check availability", availability())

        async def pricing():
            data = agent(f"/products/{st['MOT-230']}/pricing")
            assert data["price"]["current"] == "12000.00" and data["price"]["currency"] == "INR" and "8100" not in json.dumps(data)
            return f"{data['price']['current']} {data['price']['currency']} (cost price not exposed)"
        await step("13. Verify pricing", pricing())

        # 14 --------------------------------------------------------------- MCP client
        async def mcp_client():
            env = {**os.environ, "AR_MCP_URL": f"{BASE}/api/v3/mcp", "AR_KEY": st["key"], "AR_QUERY": "motor", "PYTHONIOENCODING": "utf-8"}
            proc = await asyncio.to_thread(subprocess.run, [MCP_PY, str(Path(__file__).parent / "mcp_client_check.py")],
                                           env=env, capture_output=True, text=True, timeout=180)
            out = proc.stdout + proc.stderr
            (Path(__file__).parent / "results" / "mcp_client_check.txt").write_text(out.replace(st["key"], "[key]"), encoding="utf-8")
            assert proc.returncode == 0 and "PASS" in out, out[-800:]
            versions = re.findall(r"negotiated protocol version: (\S+)", out)
            sdk = re.search(r"official MCP Python SDK (\S+)", out)
            return f"SDK {sdk.group(1) if sdk else '?'}; protocol {', '.join(dict.fromkeys(versions))}"
        await step("14. Connect an MCP-compatible client (official SDK)", mcp_client())

        # 15 --------------------------------------------------------------- webhook subscription
        async def webhook():
            await nav(page, "Webhooks")
            await page.get_by_label("Endpoint URL").fill(f"{HOOK}/agentready")
            await page.get_by_role("button", name="Create webhook").click()
            await expect(page.get_by_text("Webhook created")).to_be_visible()
            st["secret"] = (await page.get_by_test_id("secret-once").inner_text()).strip()
            assert st["secret"].startswith("whsec_")
            await page.get_by_role("button", name="I've saved it").click()
        await step("15. Create a webhook subscription", webhook())

        # 16 --------------------------------------------------------------- product update in the UI
        async def update():
            received.clear()
            await page.goto(f"{BASE}/app/catalog/{st['MOT-230']}")
            await page.get_by_role("tab", name="Distribution").click()
            await page.get_by_label("Public description").fill("Rugged 2HP motor for pumps — 2 year warranty.")
            await page.get_by_role("button", name="Save description").click()
            await expect(page.get_by_text("Public description saved")).to_be_visible()
            await expect(page.get_by_test_id("public-preview")).to_contain_text("2 year warranty")
        await step("16. Trigger a product update (public description)", update())

        # 17 --------------------------------------------------------------- webhook delivery
        async def delivery():
            from app.services.distribution.webhooks import verify

            deadline = time.time() + 45
            while time.time() < deadline and not any(json.loads(b).get("type") == "product.updated" for _, b in received):
                await asyncio.sleep(1)
            hit = [(h, b) for h, b in received if json.loads(b).get("type") == "product.updated"]
            assert hit, f"no product.updated webhook received (got {[json.loads(b).get('type') for _, b in received]})"
            h, b = hit[0]
            assert verify(st["secret"], h, b), "signature did not verify"
            payload = json.loads(b)
            assert "2 year warranty" in payload["data"]["product"]["description"] and "8100" not in b.decode()
            await nav(page, "Webhooks")
            await page.get_by_role("button", name="Deliveries").click()
            await expect(page.get_by_test_id("delivery-row").filter(has_text="product.updated").first).to_contain_text("Succeeded", timeout=15000)
            await page.screenshot(path=str(OUT / "p3_webhooks.png"), full_page=True)
            return f"signed delivery verified (webhook-id {h['webhook-id'][:8]}…)"
        await step("17. Verify webhook delivery", delivery())

        # 18 --------------------------------------------------------------- analytics
        async def analytics():
            await nav(page, "Distribution Analytics")
            stats = page.get_by_test_id("analytics-stats")
            await expect(stats).to_contain_text("Agent requests")
            data = await api("GET", "/distribution/analytics?days=1")
            o = data["observed"]
            assert o["agent_requests"] >= 6 and o["mcp_tool_calls"] >= 8 and o["feed_downloads"] >= 1, o
            assert o["webhooks"]["succeeded"] >= 1
            await expect(page.get_by_test_id("analytics-consumers")).to_contain_text("Walkthrough agent")
            await page.screenshot(path=str(OUT / "p3_analytics.png"), full_page=True)
            return (f"{o['agent_requests']} agent requests, {o['mcp_tool_calls']} MCP calls, {o['feed_downloads']} feed downloads, "
                    f"webhook success {o['webhooks']['success_rate']}")
        await step("18. Review distribution analytics", analytics())

        async def platforms():
            await nav(page, "AI Platforms")
            card = page.get_by_test_id("platform-openai_chatgpt")
            await card.get_by_role("button", name="Generate & validate feed").click()
            await expect(card.get_by_test_id("platform-validation")).to_be_visible(timeout=30000)
            mcp = page.get_by_test_id("platform-mcp_clients")
            await expect(mcp).to_contain_text("Active")
            await page.screenshot(path=str(OUT / "p3_platforms.png"), full_page=True)
            text = await card.get_by_test_id("platform-validation").inner_text()
            return text.split("\n")[0][:160]
        await step("Extra: AI platform feed validation (submission honestly blocked)", platforms())

        async def mobile():
            m = await browser.new_context(viewport={"width": 390, "height": 844})
            mp = await m.new_page()
            token = await page.evaluate("() => sessionStorage.getItem('ar-token')")
            await mp.goto(BASE + "/login")
            await mp.evaluate("t => sessionStorage.setItem('ar-token', t)", token)
            bad = []
            for path in ("/app/distribution", "/app/distribution/products", "/app/distribution/agent-api", "/app/distribution/feeds",
                         "/app/distribution/platforms", "/app/distribution/webhooks", "/app/distribution/analytics"):
                await mp.goto(BASE + path)
                await mp.wait_for_timeout(1200)
                over = await mp.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
                if over > 2:
                    bad.append(f"{path}:{over}px")
            await mp.screenshot(path=str(OUT / "p3_mobile_analytics.png"), full_page=True)
            await m.close()
            assert not bad, bad
        await step("Mobile 390px: no horizontal page overflow", mobile())
        await browser.close()

    print("\n==== SUMMARY ====")
    for s, status, note in results:
        print(f"{status:5} {s} {note}")
    print(f"\nconsole errors: {len(console_errors)}")
    for e in console_errors[:20]:
        print("  ", e[:300])
    print(f"API errors seen by the browser: {len(bad_responses)}")
    for e in bad_responses[:20]:
        print("  ", e)
    fails = [r for r in results if r[1] != "PASS"]
    sys.exit(1 if fails or bad_responses else 0)


if __name__ == "__main__":
    asyncio.run(main())
