"""Phase 2 browser walkthrough: commerce management through the real UI (Chromium via Playwright).

    AR_WEB=http://localhost:5173 python eval/ui_walkthrough_phase2.py          # Vite dev stack (default)
    AR_WEB=http://localhost:8080 python eval/ui_walkthrough_phase2.py          # docker stack

The script serves a small fixture shop (backend/app/tests/fixtures/shop) on http://127.0.0.1:8765 so it can change
the website between scans and produce a real website change + conflict. The API must therefore run with the
DEV-ONLY settings ALLOW_PRIVATE_NETWORKS=true and ALLOWED_PORTS=80,443,8765 (for the docker stack, set AR_SHOP to an
address the containers can reach, e.g. http://host.docker.internal:8765).

Covers: register, login, imported products, manual product, edit, price, inventory, variants, category, assignment,
bulk update with preview, scheduled rescan, website change review, conflict resolution, feed, API key access, audit
log, and a 390 px mobile layout check. Screenshots: eval/results/screenshots/p2_*.png
"""
import asyncio
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.tests.sitekit import FIXTURES, SHOP_ROUTES  # noqa: E402

BASE = os.environ.get("AR_WEB", "http://localhost:5173")
SHOP = os.environ.get("AR_SHOP", "http://shop.localtest.me:8765")  # public DNS name resolving to 127.0.0.1
SHOP_HOST = SHOP.split("//")[1].split(":")[0]
OUT = Path(__file__).parent / "results" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)
EMAIL = f"commerce{int(time.time())}@example.com"
PASSWORD = "merchant-pass-2026"
results: list[tuple[str, str, str]] = []
console_errors: list[str] = []
bad_responses: list[str] = []
EXPECTED = [("POST", r"/products/[0-9a-f-]+/variants$", 409)]  # the duplicate-variant check is deliberate

# ------------------------------------------------------------------ fixture shop
overrides: dict[str, str] = {}


class Shop(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        body = overrides.get(path)
        if body is None and path in SHOP_ROUTES:
            body = (FIXTURES / "shop" / SHOP_ROUTES[path]).read_text(encoding="utf-8")
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        host = self.headers.get("Host", "127.0.0.1:8765")
        body = body.replace("https://shop.example", f"http://{host}")
        ctype = "text/plain" if path.endswith(".txt") else "application/xml" if path.endswith(".xml") else "text/html; charset=utf-8"
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_shop():
    srv = ThreadingHTTPServer(("0.0.0.0", 8765), Shop)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def record(step, status, note=""):
    results.append((step, status, note))
    print(f"[{status}] {step} {note}", flush=True)


async def shot(page, name, full=True):
    await page.screenshot(path=str(OUT / f"p2_{name}.png"), full_page=full)


async def step(name, coro):
    try:
        note = await coro
        record(name, "PASS", note or "")
        return True
    except Exception as e:  # noqa: BLE001
        record(name, "FAIL", f"{type(e).__name__}: {str(e)[:400]}")
        return False


async def toast(page, text, timeout=15000):
    await expect(page.get_by_text(text).first).to_be_visible(timeout=timeout)


async def main():
    start_shop()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        page.on("console", lambda m: console_errors.append(f"{page.url} :: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"{page.url} :: PAGEERROR {e}"))

        def on_resp(r):
            if "/api/" in r.url and r.status >= 400:
                path = r.url.split("/api/v1")[-1].split("?")[0]
                if not any(r.request.method == m and re.search(p, path) and r.status == s for m, p, s in EXPECTED):
                    bad_responses.append(f"{r.request.method} {path} -> {r.status}")
        page.on("response", on_resp)
        st: dict = {}

        async def api(method, path, body=None):
            token = await page.evaluate("() => sessionStorage.getItem('ar-token')")
            r = await page.request.fetch(f"{BASE}/api/v1{path}", method=method, data=body,
                                         headers={"Authorization": f"Bearer {token}"})
            return await r.json() if r.status != 204 else None

        # 1 ---------------------------------------------------------------- register
        async def register():
            await page.goto(BASE + "/signup")
            await page.get_by_label("Business name").fill("Commerce Test Shop")
            await page.get_by_label("Your name").fill("Ravi Merchant")
            await page.get_by_label("Work email").fill(EMAIL)
            await page.get_by_label("Password").fill(PASSWORD)
            await page.get_by_role("button", name="Create account").click()
            await page.wait_for_url(re.compile(r"/app/connect$"), timeout=15000)
        await step("1. Register merchant", register())

        # 2 ---------------------------------------------------------------- login
        async def login():
            await page.goto(BASE + "/app")
            await page.get_by_role("button", name=re.compile("Commerce Test Shop")).click()
            await page.get_by_role("menuitem", name="Sign out").click()
            await page.wait_for_url(re.compile(r"/login$"))
            await page.get_by_label("Email").fill(EMAIL)
            await page.get_by_label("Password").fill(PASSWORD)
            await page.get_by_role("button", name="Sign in").click()
            await page.wait_for_url(re.compile(r"/app"), timeout=15000)
            await expect(page.get_by_text("Product Catalog").first).to_be_visible()
            await shot(page, "02_nav_after_login", full=False)
        await step("2. Log in (grouped navigation visible)", login())

        # 3 ---------------------------------------------------------------- imported products
        async def imported():
            await page.goto(BASE + "/app/connect")
            await page.get_by_label("Website URL").fill(SHOP)
            await page.get_by_role("button", name="Check URL").click()
            await expect(page.get_by_text("Website reachable")).to_be_visible(timeout=20000)
            await page.get_by_label("Maximum pages").fill("30")
            await page.get_by_label("Crawl depth").fill("2")
            await page.get_by_role("button", name="Start scan").click()
            await expect(page.get_by_role("link", name="Open catalog")).to_be_visible(timeout=180000)
            await page.goto(BASE + "/app/catalog")
            await expect(page.locator("tbody tr").first).to_be_visible(timeout=15000)
            await expect(page.get_by_text("12V DC Motor").first).to_be_visible()
            await shot(page, "03_catalog_imported")
            total = await page.locator("text=/\\d+–\\d+ of \\d+/").first.inner_text()
            return f"catalog {total}"
        await step("3. View imported products (scan of fixture shop)", imported())

        # 4 ---------------------------------------------------------------- manual product
        async def create():
            await page.goto(BASE + "/app/catalog")
            await page.get_by_role("button", name="New product").click()
            await page.wait_for_url(re.compile(r"/app/catalog/new$"))
            await page.get_by_label(re.compile("Product name")).fill("Organic Cotton T-shirt")
            await page.get_by_label("Description").first.fill("Soft organic cotton crew-neck tee.")
            await page.get_by_label("Brand").fill("Loomwell")
            await page.get_by_label("SKU").fill("LW-TEE")
            await page.get_by_label("Product type").fill("physical")
            await page.get_by_label("Regular price").fill("799")
            await page.get_by_label("Currency").fill("INR")
            await page.get_by_label("Cost price").fill("310")
            await page.get_by_label("Track inventory").click()
            await page.get_by_label("Stock on hand").fill("10")
            await page.get_by_label("Status").select_option("active")
            await shot(page, "04_create_form")
            await page.get_by_role("button", name="Create product").click()
            await page.wait_for_url(re.compile(r"/app/catalog/[0-9a-f-]{36}$"), timeout=15000)
            st["tee"] = page.url
            await expect(page.get_by_role("heading", name="Organic Cotton T-shirt")).to_be_visible()
            await shot(page, "04_created_overview")
        await step("4. Create a manual product", create())

        # 5 ---------------------------------------------------------------- edit
        async def edit():
            await page.goto(st["tee"])
            await page.get_by_role("button", name="Edit details").click()
            dlg = page.get_by_role("dialog")
            await dlg.get_by_label("Description").fill("Soft organic cotton crew-neck tee. Pre-shrunk.")
            await dlg.get_by_label("Material").fill("Organic cotton")
            await dlg.get_by_role("button", name="Save changes").click()
            await expect(dlg).to_be_hidden(timeout=10000)
            await page.get_by_role("tab", name="Basic info").click()
            await expect(page.get_by_text("Pre-shrunk.")).to_be_visible()
        await step("5. Edit product details", edit())

        # 6 ---------------------------------------------------------------- price
        async def price():
            await page.goto(st["tee"])
            await page.get_by_role("tab", name="Pricing").click()
            await page.get_by_label("Regular price").fill("849")
            await page.get_by_label("Sale price").fill("749")
            await page.get_by_label("Reason (recorded in history)").fill("Launch offer")
            await page.get_by_role("button", name="Save pricing").click()
            await toast(page, "Pricing saved")
            await expect(page.get_by_text("Launch offer").first).to_be_visible(timeout=10000)
            await expect(page.get_by_text("On sale").first).to_be_visible()
            await shot(page, "06_pricing_history")
            # sale price above regular is rejected by the backend rules
            EXPECTED.append(("PUT", r"/pricing$", 422))
            await page.get_by_label("Sale price").fill("900")
            await page.get_by_role("button", name="Save pricing").click()
            await expect(page.get_by_text(re.compile("must be lower than the regular price"))).to_be_visible(timeout=10000)
            await page.get_by_role("button", name="Discard").click()
        await step("6. Update price (history, sale, validation)", price())

        # 7 ---------------------------------------------------------------- inventory
        async def inventory():
            await page.goto(st["tee"])
            await page.get_by_role("tab", name="Inventory").click()
            await page.get_by_label("Quantity").fill("5")
            await page.get_by_label("Reason").first.fill("Supplier delivery")
            await page.get_by_role("button", name="Add").click()
            await toast(page, "Stock updated")
            await expect(page.get_by_text("Supplier delivery").first).to_be_visible(timeout=10000)
            onhand = await page.get_by_text("On hand", exact=True).first.locator("xpath=following-sibling::p").inner_text()
            assert onhand.strip() == "15", onhand
            EXPECTED.append(("POST", r"/inventory/adjust$", 422))
            await page.get_by_label("Quantity").fill("20")
            await page.get_by_role("button", name="Remove").click()
            await expect(page.get_by_text(re.compile("Not enough stock"))).to_be_visible(timeout=10000)
            await shot(page, "07_inventory")
            return "on hand 10 -> 15; over-removal refused"
        await step("7. Adjust inventory (transaction recorded, negative refused)", inventory())

        # 8 ---------------------------------------------------------------- variants
        async def variants():
            await page.goto(st["tee"])
            await page.get_by_role("tab", name=re.compile("Variants")).click()
            for size, sku in (("M", "LW-TEE-M-BLK"), ("L", "LW-TEE-L-BLK"), ("M", "DUP")):
                await page.get_by_role("button", name="Add variant").click()
                dlg = page.get_by_role("dialog")
                await dlg.get_by_label("Size value").fill(size)
                await dlg.get_by_label("Colour value").fill("Black")
                await dlg.get_by_label("SKU").fill(sku)
                await dlg.get_by_role("button", name="Create variant").click()
                if sku == "DUP":
                    await expect(dlg.get_by_text(re.compile("already exists"))).to_be_visible(timeout=10000)
                    await dlg.get_by_role("button", name="Cancel").click()
                else:
                    await expect(dlg).to_be_hidden(timeout=10000)
            await expect(page.get_by_text("LW-TEE-L-BLK")).to_be_visible()
            await shot(page, "08_variants")
            return "2 variants; duplicate combination rejected"
        await step("8. Create variants (duplicate prevented)", variants())

        # 9 ---------------------------------------------------------------- category
        async def category():
            await page.goto(BASE + "/app/categories")
            for name, parent in (("Apparel", None), ("T-shirts", "Apparel")):
                await page.get_by_role("button", name="New category").click()
                dlg = page.get_by_role("dialog")
                await dlg.get_by_label("Name").fill(name)
                if parent:
                    await dlg.get_by_label("Parent category").select_option(label=parent)
                await dlg.get_by_role("button", name="Save").click()
                await expect(dlg).to_be_hidden(timeout=10000)
            await expect(page.get_by_role("link", name="T-shirts")).to_be_visible()
            await shot(page, "09_categories")
        await step("9. Create nested categories", category())

        # 10 --------------------------------------------------------------- assign
        async def assign():
            await page.goto(BASE + "/app/catalog?q=organic")
            await expect(page.get_by_text("Organic Cotton T-shirt")).to_be_visible(timeout=10000)
            await page.locator("tbody tr td:first-child button[role=checkbox]").first.click()
            await page.get_by_role("button", name="Category").click()
            dlg = page.get_by_role("dialog")
            await dlg.get_by_label("Category").select_option(label="Apparel / T-shirts")
            await dlg.get_by_role("button", name="Preview changes").click()
            await expect(dlg.get_by_text("1 will change")).to_be_visible(timeout=10000)
            await dlg.get_by_role("button", name=re.compile("Confirm: apply to 1")).click()
            await expect(dlg.get_by_text(re.compile("All 1 product\\(s\\) updated"))).to_be_visible(timeout=15000)
            await dlg.get_by_role("button", name="Close", exact=True).first.click()
            await page.goto(st["tee"])
            await expect(page.get_by_text("Apparel / T-shirts").first).to_be_visible()
        await step("10. Assign product to category (bulk, with preview)", assign())

        # 11 --------------------------------------------------------------- bulk price update
        async def bulk():
            await page.goto(BASE + "/app/pricing")
            await expect(page.locator("tbody tr").first).to_be_visible(timeout=10000)
            boxes = page.locator("tbody tr td:first-child button[role=checkbox]")
            for i in range(4):
                await boxes.nth(i).click()
            await page.get_by_role("button", name="Adjust prices").click()
            dlg = page.get_by_role("dialog")
            await dlg.get_by_label("Change").select_option("percent_increase")
            await dlg.get_by_label("Percent").fill("10")
            await dlg.get_by_role("button", name="Preview changes").click()
            await expect(dlg.get_by_text(re.compile(r"\d+ will change"))).to_be_visible(timeout=10000)
            await shot(page, "11_bulk_preview", full=False)
            prev = await dlg.inner_text()
            await dlg.get_by_role("button", name=re.compile("Confirm: apply to")).click()
            await expect(dlg.get_by_text(re.compile(r"(All \d+ product\(s\) updated|\d+ succeeded, \d+ failed)"))).to_be_visible(timeout=20000)
            res = await dlg.inner_text()
            await shot(page, "11_bulk_result", full=False)
            await dlg.get_by_role("button", name="Close", exact=True).first.click()
            m = re.search(r"(All \d+ product\(s\) updated|\d+ succeeded, \d+ failed)", res)
            return f"{m.group(0) if m else '?'}; preview listed cannot-change: {'cannot be changed' in prev}"
        await step("11. Bulk price update (preview -> confirm -> per-item result)", bulk())

        # 12 --------------------------------------------------------------- schedule rescan
        async def schedule():
            await page.goto(BASE + "/app/sync")
            card = page.locator("div.rounded-lg", has_text=SHOP_HOST).first
            await card.get_by_label("Frequency").select_option("daily")
            await card.get_by_label("Timezone").select_option("Asia/Kolkata")
            await card.get_by_role("button", name="Save settings").click()
            await toast(page, "Sync settings saved")
            await expect(page.get_by_text(re.compile("Next scheduled scan")).first).to_be_visible(timeout=10000)
            sched = await api("GET", "/sync/schedules")
            s = sched["items"][0]
            assert s["enabled"] and s["frequency"] == "daily" and s["next_run_at"], s
            await shot(page, "12_sync_schedule")
            return f"next run {s['next_run_at']}"
        await step("12. Schedule a daily rescan (timezone-aware)", schedule())

        # 13 --------------------------------------------------------------- website change review
        async def changes():
            products = await api("GET", "/products?q=12V&page_size=5")
            motor = products["items"][0]
            st["motor"] = f"{BASE}/app/catalog/{motor['id']}"
            # the merchant manages this price in AgentReady
            await page.goto(st["motor"])
            await page.get_by_role("tab", name="Pricing").click()
            await page.get_by_label("Regular price").fill("460")
            await page.get_by_role("button", name="Save pricing").click()
            await toast(page, "Pricing saved")
            # the website changes price and description
            body = (FIXTURES / "shop" / "dc-motor-12v.html").read_text(encoding="utf-8")
            overrides["/products/dc-motor-12v"] = body.replace('"price":"450"', '"price":"475"').replace("Rs. 450", "Rs. 475")
            pl = (FIXTURES / "shop" / "price-list.html").read_text(encoding="utf-8")
            overrides["/price-list"] = pl.replace("<tr><td>Motor B</td>", "<tr><td>Motor C</td><td>36V</td><td>6000</td><td>990</td><td>DC Motors</td></tr><tr><td>Motor B</td>")
            await page.goto(BASE + "/app/sync")
            await page.locator("div.rounded-lg", has_text=SHOP_HOST).first.get_by_role("button", name="Sync now").click()
            await toast(page, "Rescan started")
            for _ in range(90):
                syncs = await api("GET", "/sync?page_size=1")
                if syncs["items"] and syncs["items"][0]["status"] not in ("queued", "running"):
                    break
                await page.wait_for_timeout(2000)
            job = syncs["items"][0]
            assert job["status"] == "awaiting_review", job["status"]
            await page.goto(f"{BASE}/app/scans/{job['scan_id']}")
            await expect(page.get_by_text("Motor C").first).to_be_visible(timeout=15000)
            await shot(page, "13_change_review")
            await page.get_by_role("button", name=re.compile(r"Apply \d+ change")).click()
            await expect(page.get_by_text(re.compile("applied|Completed")).first).to_be_visible(timeout=15000)
            p = await api("GET", f"/products/{motor['id']}")
            assert p["pricing"]["regular_price"] == "460.00", p["pricing"]
            return f"sync {job['status']}, conflicts {job['conflicts_detected']}; merchant price kept at 460.00 after apply"
        await step("13. Review detected website changes (merchant price not overwritten)", changes())

        # 14 --------------------------------------------------------------- conflict
        async def conflict():
            await page.goto(BASE + "/app/sync")
            row = page.locator("div.rounded-md.border.p-3", has_text="12V DC Motor").first
            await expect(row.get_by_text("475.00")).to_be_visible(timeout=10000)
            await expect(row.get_by_text("460.00")).to_be_visible()
            await shot(page, "14_conflict")
            await row.get_by_role("button", name="Keep AgentReady value").click()
            await toast(page, "Conflict resolved")
            resolved = await api("GET", "/sync/conflicts?status=resolved")
            assert any(c["resolution"] == "keep_agentready" for c in resolved["items"]), resolved
        await step("14. Resolve a sync conflict (keep AgentReady value)", conflict())

        # 15/16 ------------------------------------------------------------ feed + API key
        async def feed_and_key():
            await page.goto(st["tee"])
            await page.get_by_role("button", name="Approve").click()
            await toast(page, "Product approved")
            await page.goto(BASE + "/app/api")
            await page.get_by_label("Key name").fill("Shopping agent")
            await page.get_by_role("button", name="Create key").click()
            banner = page.locator("div.rounded-lg", has_text="Copy your new API key now").first
            await expect(banner).to_be_visible(timeout=10000)
            key = (await banner.locator("pre").first.inner_text()).strip()
            assert key.startswith("ar_cat_"), key[:10]
            await shot(page, "15_api_key_created", full=False)
            r = await page.request.get(f"{BASE}/api/v2/catalog/feed?type=full", headers={"X-Catalog-Key": key})
            feed = await r.json()
            titles = [i["title"] for i in feed["items"]]
            assert r.status == 200 and "Organic Cotton T-shirt" in titles, (r.status, titles)
            text = str(feed)
            assert "310" not in text and "cost" not in text.lower(), "cost price leaked"
            r2 = await page.request.get(f"{BASE}/api/v2/catalog/products", headers={"X-Catalog-Key": "ar_cat_wrong"})
            assert r2.status == 401
            st["key"] = key
            await page.reload()
            await expect(page.get_by_text("Shopping agent")).to_be_visible()
            return f"feed items {len(titles)} (only approved+published), no cost price, invalid key 401"
        await step("15/16. Generate catalog feed and verify API key access", feed_and_key())

        # 17 --------------------------------------------------------------- audit
        async def audit():
            await page.goto(BASE + "/app/audit")
            await expect(page.locator("tbody tr").first).to_be_visible(timeout=10000)
            body = await page.inner_text("body")
            for needle in ("Price changed", "Stock", "Variant", "Conflict on", "API key", "created manually"):
                assert needle in body, needle
            assert st.get("key") and st["key"] not in body
            await shot(page, "17_audit")
        await step("17. Inspect audit logs", audit())

        # mobile --------------------------------------------------------------
        async def mobile():
            m = await browser.new_context(viewport={"width": 390, "height": 844}, storage_state=await ctx.storage_state())
            mp = await m.new_page()
            token = await page.evaluate("() => sessionStorage.getItem('ar-token')")
            await mp.goto(BASE + "/login")
            await mp.evaluate(f"t => sessionStorage.setItem('ar-token', t)", token)
            bad = []
            for path in ("/app", "/app/catalog", st["tee"].replace(BASE, ""), "/app/inventory", "/app/pricing", "/app/sync",
                         "/app/categories", "/app/api", "/app/audit", "/app/integrations"):
                await mp.goto(BASE + path)
                await mp.wait_for_timeout(1200)
                over = await mp.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
                if over > 2:
                    bad.append(f"{path}:{over}px")
            await mp.goto(BASE + "/app/inventory")
            await mp.wait_for_timeout(800)
            await mp.screenshot(path=str(OUT / "p2_mobile_inventory.png"), full_page=True)
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
    print(f"unexpected API errors: {len(bad_responses)}")
    for e in bad_responses[:20]:
        print("  ", e)
    fails = [r for r in results if r[1] != "PASS"]
    sys.exit(1 if fails or bad_responses else 0)


if __name__ == "__main__":
    asyncio.run(main())
