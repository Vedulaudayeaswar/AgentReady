"""Phase 4 browser walkthrough: Commerce Intelligence through the real UI (Chromium via Playwright).

    AR_WEB=http://localhost:5173 python eval/ui_walkthrough_phase4.py      # dev stack
    AR_WEB=http://localhost:8080 python eval/ui_walkthrough_phase4.py      # docker stack

Seeds a small catalog and real agent traffic through the public APIs (no direct DB writes), then walks the 19 merchant
steps. If the API has GROQ_API_KEY configured, the assistant and description steps exercise the real Groq model; the
summary says whether the answer came from Groq or from the deterministic fallback (and whether the grounding check
rejected the model output). Screenshots: eval/results/screenshots/p4_*.png
"""
import asyncio
import os
import re
import sys
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, expect

BASE = os.environ.get("AR_WEB", "http://localhost:5173").rstrip("/")
OUT = Path(__file__).parent / "results" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)
EMAIL = f"intel{int(time.time())}@example.com"
PASSWORD = "merchant-pass-2026"
STORE = "Intelligence Test Store"
results: list[tuple[str, str, str]] = []
console_errors: list[str] = []
bad_responses: list[str] = []


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
    await page.locator("nav").get_by_role("link", name=name, exact=True).first.click()


def seed(http: httpx.Client, token: str) -> dict:
    h = {"Authorization": f"Bearer {token}"}

    def call(method, path, body=None):
        r = http.request(method, f"/api/v1{path}", json=body, headers=h)
        assert r.status_code < 400, f"{method} {path} -> {r.status_code} {r.text[:300]}"
        return r.json() if r.status_code != 204 else None

    cat = call("POST", "/categories", {"name": "Motors"})
    st = {}
    for p in [
        {"name": "Industrial Motor 2HP", "brand": "Havells", "sku": "MOT-1", "regular_price": "12000", "currency": "INR",
         "category_id": cat["id"], "gtin": "4006381333931", "product_url": "https://store.example/motor",
         "description": "Three-phase induction motor for pumps and compressors. Cast iron body suited to continuous duty in workshops.",
         "images": [{"url": "https://store.example/m1.jpg"}, {"url": "https://store.example/m2.jpg"}], "availability": "InStock",
         "attributes": [{"label": "Voltage", "value": "230 V"}, {"label": "Power", "value": "1.5 kW"}]},
        {"name": "Motor 1HP", "sku": "MOT-2", "regular_price": "1000", "currency": "INR", "category_id": cat["id"],
         "description": "Compact motor. Compact motor.\nVoltage: 230 V", "availability": "InStock",
         "attributes": [{"label": "Power", "value": "0.75 kW"}]},
        {"name": "Basic T-shirt", "brand": "Acme", "sku": "TEE", "regular_price": "450", "currency": "INR", "availability": "InStock",
         "description": "Soft cotton t-shirt for everyday wear, pre-shrunk."},
    ]:
        made = call("POST", "/products", {**p, "lifecycle_status": "active"})
        call("PATCH", f"/products/{made['id']}", {"approval_status": "approved"})
        st[p["sku"]] = made["id"]
    call("POST", "/attribute-definitions", {"name": "Voltage", "key": "voltage", "unit": "V", "is_required": True, "category_id": cat["id"]})
    call("PUT", f"/products/{st['MOT-2']}/pricing", {"regular_price": "3000"})  # +200% -> anomaly and a price-change alert
    call("PUT", "/distribution/settings", {"enabled": True, "public_name": STORE, "public_url": "https://store.example"})
    key = call("POST", "/api-keys", {"name": "Walkthrough agent", "scopes": ["agent:read"]})["key"]
    k = {"X-Catalog-Key": key}
    for params in ({"q": "motor"}, {"q": "motor"}, {"q": "1hp motor", "attr": "voltage:415 V"}, {"q": "telescope lens"}):
        assert http.get("/api/v3/agents/search", params=params, headers=k).status_code == 200
    http.get(f"/api/v3/agents/products/{st['MOT-1']}", headers=k)
    http.post("/api/v3/agents/compare", json={"product_ids": [st["MOT-1"], st["MOT-2"]]}, headers=k)
    return st


async def main():
    http = httpx.Client(base_url=BASE, timeout=120)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        page = await ctx.new_page()
        page.on("console", lambda m: console_errors.append(f"{page.url} :: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"{page.url} :: PAGEERROR {e}"))
        page.on("response", lambda r: bad_responses.append(f"{r.request.method} {r.url.split('/api')[-1]} -> {r.status}")
                if "/api/" in r.url and r.status >= 400 else None)
        st: dict = {}

        async def token():
            return await page.evaluate("() => sessionStorage.getItem('ar-token')")

        async def login():
            await page.goto(BASE + "/signup")
            await page.get_by_label("Business name").fill(STORE)
            await page.get_by_label("Your name").fill("Isha Merchant")
            await page.get_by_label("Work email").fill(EMAIL)
            await page.get_by_label("Password").fill(PASSWORD)
            await page.get_by_role("button", name="Create account").click()
            await page.wait_for_url(re.compile(r"/app/connect$"), timeout=15000)
            st.update(seed(http, await token()))
            await page.get_by_role("button", name=re.compile(STORE)).click()
            await page.get_by_role("menuitem", name="Sign out").click()
            await page.wait_for_url(re.compile(r"/login$"))
            await page.get_by_label("Email").fill(EMAIL)
            await page.get_by_label("Password").fill(PASSWORD)
            await page.get_by_role("button", name="Sign in").click()
            await page.wait_for_url(re.compile(r"/app"), timeout=15000)
            return "registered, seeded 3 products + real agent traffic, signed out and back in"
        await step("1. Log in as a merchant", login())

        async def overview():
            await nav(page, "Intelligence Overview")
            stats = page.get_by_test_id("intel-catalog-stats")
            await expect(stats).to_contain_text("Products managed", timeout=20000)
            await expect(stats).to_contain_text("3")
            await expect(page.get_by_text("Observed agent activity").first).to_be_visible()
            await page.screenshot(path=str(OUT / "p4_overview.png"), full_page=True)
        await step("2. Open Commerce Intelligence", overview())

        async def quality():
            await nav(page, "Catalog Quality")
            await expect(page.get_by_test_id("quality-row").first).to_be_visible(timeout=20000)
            return f"{await page.get_by_test_id('quality-row').count()} products scored"
        await step("3. View catalog quality", quality())

        async def product_report():
            row = page.get_by_test_id("quality-row").filter(has_text="Motor 1HP")
            await row.get_by_role("button", name="Quality report").click()
            await expect(page.get_by_test_id("product-quality")).to_be_visible(timeout=15000)
            await page.screenshot(path=str(OUT / "p4_product_quality.png"))
        await step("4. Open a product quality report", product_report())

        async def missing():
            await expect(page.get_by_test_id("missing-required")).to_contain_text("attribute: voltage")
            await page.keyboard.press("Escape")
            return "voltage is required for Motors and missing"
        await step("5. Inspect missing attributes", missing())

        async def categories():
            await expect(page.get_by_test_id("category-quality")).to_contain_text("Motors")
        await step("6. View category quality", categories())

        async def discovery():
            await nav(page, "Discovery Insights")
            await expect(page.get_by_text("Most searched terms")).to_be_visible(timeout=15000)
        await step("7. Open discovery insights", discovery())

        async def zero():
            box = page.get_by_test_id("zero-results")
            await expect(box).to_contain_text("1hp motor")
            await expect(box).to_contain_text("lack the requested voltage")
            await expect(box).to_contain_text("telescope lens")
            await page.screenshot(path=str(OUT / "p4_discovery.png"), full_page=True)
        await step("8. Inspect zero-result searches", zero())

        async def recs():
            await nav(page, "Recommendations")
            cards = page.get_by_test_id("recommendation")
            await expect(cards.first).to_be_visible(timeout=20000)
            await expect(cards.filter(has_text="voltage").first).to_contain_text("Critical")
            await page.screenshot(path=str(OUT / "p4_recommendations.png"), full_page=True)
            return f"{await cards.count()} open recommendations"
        await step("9. Review recommendations", recs())

        async def dismiss():
            card = page.get_by_test_id("recommendation").last
            title = (await card.inner_text()).split("\n")[1]
            await card.get_by_role("button", name="Dismiss").click()
            await expect(page.get_by_text("Recommendation dismissed")).to_be_visible()
            return f"dismissed: {title[:60]}"
        await step("10. Dismiss a recommendation", dismiss())

        async def ask():
            await nav(page, "AI Catalog Assistant")
            await page.get_by_role("button", name="Which products have incomplete specifications?").click()
            ans = page.get_by_test_id("assistant-answer")
            await expect(ans).to_be_visible(timeout=60000)
            text = await ans.inner_text()
            st["assistant_label"] = "Groq" if "AI-generated answer" in text else "deterministic"
            st["rejected"] = "rejected by grounding" in text
            return f"answer source: {st['assistant_label']}{' (model output rejected by grounding check)' if st['rejected'] else ''}"
        await step("11. Ask the AI assistant a catalog question", ask())

        async def evidence():
            ev = page.get_by_test_id("assistant-evidence")
            await expect(ev).to_contain_text("Motor 1HP")
            await page.screenshot(path=str(OUT / "p4_assistant.png"), full_page=True)
        await step("12. Review the evidence in its response", evidence())

        async def suggest():
            await page.get_by_label("Product").select_option(label="Motor 1HP")
            await page.get_by_role("button", name="Suggest description").click()
            await expect(page.get_by_test_id("suggestion").first).to_be_visible(timeout=90000)
            card = page.get_by_test_id("suggestion").first
            st["sug_source"] = "Groq" if "AI-generated" in await card.inner_text() else "deterministic"
            return f"suggestion source: {st['sug_source']}"
        await step("13. Generate a suggested description", suggest())

        async def preview():
            ba = page.get_by_test_id("before-after").first
            await expect(ba).to_contain_text("Before")
            await expect(ba).to_contain_text("Compact motor. Compact motor.")
            st["proposed"] = await page.get_by_label("Proposed description").first.input_value()
            assert st["proposed"].strip() and "warranty" not in st["proposed"].lower()
            await page.screenshot(path=str(OUT / "p4_suggestion.png"), full_page=True)
        await step("14. Preview the proposed edit (before / after)", preview())

        async def approve():
            await page.get_by_role("button", name="Approve & apply").first.click()
            await expect(page.get_by_text("Approved and applied to the product")).to_be_visible(timeout=20000)
            r = http.get(f"/api/v1/products/{st['MOT-2']}", headers={"Authorization": f"Bearer {await token()}"})
            desc = r.json()["description"]
            assert desc.strip() == st["proposed"].strip(), "applied text differs from the approved proposal"
            assert r.json()["pricing"]["regular_price"] == "3000.00"
            return "description updated through the normal edit path; price untouched"
        await step("15. Approve the edit", approve())

        async def anomalies():
            await nav(page, "Anomaly Detection")
            rows = page.get_by_test_id("anomaly-row")
            await expect(rows.filter(has_text="price changed").first).to_be_visible(timeout=20000)
            await rows.filter(has_text="price changed").first.get_by_role("button", name="Investigate").click()
            await expect(page.get_by_test_id("anomaly-detail")).to_contain_text("1000.00")
            await page.screenshot(path=str(OUT / "p4_anomaly.png"))
            await page.keyboard.press("Escape")
        await step("16. View anomaly alerts", anomalies())

        async def report():
            await nav(page, "Reports")
            await page.get_by_label("Report").select_option("catalog_quality")
            await page.get_by_label("Format").select_option("pdf")
            await page.get_by_role("button", name="Generate report").click()
            await expect(page.get_by_text("Report generated")).to_be_visible(timeout=20000)
            await expect(page.get_by_test_id("report-row").first).to_contain_text("Catalog quality report")
        await step("17. Generate a catalog quality report", report())

        async def download():
            async with page.expect_download() as dl:
                await page.get_by_test_id("report-row").first.get_by_role("button").click()
            d = await dl.value
            path = OUT.parent / d.suggested_filename
            await d.save_as(str(path))
            data = path.read_bytes()
            assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF"), data[:20]
            return f"{d.suggested_filename} ({len(data)} bytes)"
        await step("18. Download the report", download())

        async def notifications():
            http.post("/api/v1/intelligence/refresh", headers={"Authorization": f"Bearer {await token()}"})
            await page.reload()
            await nav(page, "Notifications")
            items = page.get_by_test_id("notification")
            await expect(items.first).to_be_visible(timeout=20000)
            await expect(items.filter(has_text="Prices changed").first).to_be_visible()
            await page.screenshot(path=str(OUT / "p4_notifications.png"), full_page=True)
            return f"{await items.count()} notifications (email: not configured)"
        await step("19. Review notifications", notifications())

        async def mobile():
            m = await browser.new_context(viewport={"width": 390, "height": 844})
            mp = await m.new_page()
            await mp.goto(BASE + "/login")
            await mp.evaluate("t => sessionStorage.setItem('ar-token', t)", await token())
            bad = []
            for path in ("/app/intelligence", "/app/intelligence/quality", "/app/intelligence/discovery", "/app/intelligence/recommendations",
                         "/app/intelligence/anomalies", "/app/intelligence/pricing", "/app/intelligence/inventory",
                         "/app/intelligence/reports", "/app/intelligence/notifications", "/app/intelligence/assistant"):
                await mp.goto(BASE + path)
                await mp.wait_for_timeout(1500)
                over = await mp.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
                if over > 2:
                    bad.append(f"{path}:{over}px")
            await mp.screenshot(path=str(OUT / "p4_mobile.png"), full_page=True)
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
    sys.exit(1 if [r for r in results if r[1] != "PASS"] or bad_responses else 0)


if __name__ == "__main__":
    asyncio.run(main())
