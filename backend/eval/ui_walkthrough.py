"""Drive the real AgentReady UI in Chromium through the full merchant journey.

    pip install -r requirements-playwright.txt && python -m playwright install chromium
    AR_WEB=http://localhost:8080 python eval/ui_walkthrough.py     # docker stack (default: Vite dev server)

Registers a fresh merchant, scans https://books.toscrape.com (a public scraping sandbox; 12 then 20 pages),
reviews/edits/approves/exports, rescans and applies changes, and checks auth flows, dark mode and a 390 px
mobile viewport. Records console errors, page errors and failed API responses; screenshots go to
eval/results/screenshots/. Requires network access.
"""
import asyncio, json, os, re, sys, time
from pathlib import Path
from playwright.async_api import async_playwright, expect

BASE = os.environ.get("AR_WEB", "http://localhost:5173")
OUT = Path(__file__).parent / "results" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)
EMAIL = f"merchant{int(time.time())}@example.com"
PASSWORD = "merchant-pass-2026"
results: list[tuple[str, str, str]] = []
console_errors: list[str] = []
bad_responses: list[str] = []
EXPECTED_4XX = []  # (method, path-regex, status) that a step deliberately triggers


def record(step, status, note=""):
    results.append((step, status, note))
    print(f"[{status}] {step} {note}", flush=True)


async def shot(page, name, full=True):
    await page.screenshot(path=str(OUT / f"{name}.png"), full_page=full)


async def step(name, coro):
    try:
        note = await coro
        record(name, "PASS", note or "")
        return True
    except Exception as e:
        record(name, "FAIL", f"{type(e).__name__}: {str(e)[:300]}")
        return False


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        page = await ctx.new_page()
        page.on("console", lambda m: console_errors.append(f"{page.url} :: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"{page.url} :: PAGEERROR {e}"))

        def on_resp(r):
            if "/api/" in r.url and r.status >= 400:
                bad_responses.append(f"{r.request.method} {r.url.split('/api/v1')[-1]} -> {r.status}")
        page.on("response", on_resp)

        # ---------------------------------------------------------- landing
        async def landing():
            await page.goto(BASE + "/")
            await expect(page.get_by_role("heading", name="Make Your Products Ready for AI Commerce.")).to_be_visible()
            await expect(page.get_by_role("link", name=re.compile("Generate Your AI-Ready Catalog")).first).to_be_visible()
            await shot(page, "01_landing_desktop")
            # no fabricated social proof
            body = await page.inner_text("body")
            for bad in ("testimonial", "trusted by", "customers use", "% accuracy"):
                assert bad not in body.lower(), f"found '{bad}'"
        await step("Landing page renders", landing())

        async def unauth_redirect():
            await page.goto(BASE + "/app/catalog")
            await page.wait_for_url(re.compile(r"/login$"))
        await step("Protected route redirects to login", unauth_redirect())

        # ---------------------------------------------------------- signup
        async def signup():
            await page.goto(BASE + "/signup")
            await page.get_by_role("button", name="Create account").click()
            await expect(page.get_by_text("Enter a valid email")).to_be_visible()  # client validation
            await page.get_by_label("Business name").fill("Tosca Books Ltd")
            await page.get_by_label("Your name").fill("Asha Merchant")
            await page.get_by_label("Work email").fill(EMAIL)
            await page.get_by_label("Password").fill(PASSWORD)
            await shot(page, "02_signup")
            await page.get_by_role("button", name="Create account").click()
            await page.wait_for_url(re.compile(r"/app/connect$"), timeout=15000)
        await step("Register new merchant", signup())

        # ---------------------------------------------------------- empty overview
        async def empty_overview():
            await page.goto(BASE + "/app")
            await expect(page.get_by_text("Connect your first website")).to_be_visible()
            await shot(page, "03_overview_empty")
        await step("Empty dashboard shows onboarding (no fake metrics)", empty_overview())

        # ---------------------------------------------------------- connect + scan
        async def connect():
            await page.goto(BASE + "/app/connect")
            await page.get_by_label("Website URL").fill("localhost:8000")
            await page.get_by_role("button", name="Check URL").click()
            await expect(page.get_by_text(re.compile("cannot be scanned|valid website address"))).to_be_visible(timeout=15000)
            await page.get_by_label("Website URL").fill("books.toscrape.com")
            await page.get_by_role("button", name="Check URL").click()
            await expect(page.get_by_text("Website reachable")).to_be_visible(timeout=20000)
            await page.get_by_label("Maximum pages").fill("12")
            await page.get_by_label("Crawl depth").fill("2")
            await shot(page, "04_connect_checked")
            await page.get_by_role("button", name="Start scan").click()
            await expect(page.get_by_text("Scan progress")).to_be_visible(timeout=15000)
            await page.wait_for_timeout(2500)
            await shot(page, "05_scan_progress_running")
            await expect(page.get_by_role("link", name="Open catalog")).to_be_visible(timeout=240000)
            await shot(page, "06_scan_progress_done")
            txt = await page.inner_text("body")
            m = re.search(r"Products extracted\s*(\d+)", txt)
            return f"products extracted shown: {m.group(1) if m else '?'}"
        await step("Connect website, SSRF rejection, scan with live progress", connect())
        EXPECTED_4XX.append(("POST", "/websites/validate-url", 422))

        # ---------------------------------------------------------- overview
        async def overview():
            await page.goto(BASE + "/app")
            await expect(page.get_by_text("Total products")).to_be_visible()
            await page.wait_for_timeout(800)
            await shot(page, "07_overview")
        await step("Dashboard overview with real metrics", overview())

        # ---------------------------------------------------------- catalog
        state = {}

        async def catalog():
            await page.goto(BASE + "/app/catalog")
            await expect(page.locator("tbody tr").first).to_be_visible(timeout=15000)
            await page.wait_for_timeout(2500)
            thumbs = await page.evaluate("""() => { const imgs = [...document.querySelectorAll('tbody img')];
                return {total: imgs.length, loaded: imgs.filter(i => i.complete && i.naturalWidth > 0).length}; }""")
            state["thumbnails"] = thumbs
            await shot(page, "08_catalog")
            total = await page.locator("text=/\\d+–\\d+ of \\d+/").first.inner_text()
            await page.get_by_placeholder("Search name, SKU, brand, GTIN").fill("light")
            await page.wait_for_timeout(1200)
            names = await page.locator("tbody tr td:nth-child(3) p.font-medium").all_inner_texts()
            assert names and all("light" in n.lower() for n in names), names
            await page.get_by_placeholder("Search name, SKU, brand, GTIN").fill("")
            await page.wait_for_timeout(1000)
            await page.get_by_label("Sort").select_option("price_desc")
            await page.wait_for_timeout(1000)
            prices = await page.locator("tbody tr td:nth-child(6)").all_inner_texts()
            nums = [float(re.sub(r"[^\d.]", "", p)) for p in prices if re.search(r"\d", p)]
            assert nums == sorted(nums, reverse=True), prices[:5]
            return f"pagination '{total}', search+sort OK, thumbnails {state['thumbnails']}"
        await step("Catalog list, search, sort", catalog())

        async def detail_and_edit():
            await page.locator("tbody tr").first.click()
            await page.wait_for_url(re.compile(r"/app/catalog/[0-9a-f-]{36}$"))
            state["product_url"] = page.url
            await expect(page.get_by_role("tab", name=re.compile("evidence", re.I))).to_be_visible()
            await shot(page, "09_product_detail")
            await page.get_by_role("tab", name=re.compile("evidence", re.I)).click()
            await expect(page.get_by_text(re.compile("Deterministic|AI ·")).first).to_be_visible()
            await shot(page, "10_product_evidence")
            # Phase 2: the source page is shown inside the "Source evidence" section
            await expect(page.get_by_text(re.compile("Why this page was treated"))).to_be_visible()
            await shot(page, "11_product_source")
            await page.get_by_role("tab", name="Basic info").click()
            await page.get_by_role("button", name="Edit details").click()
            dlg = page.get_by_role("dialog")
            await expect(dlg).to_be_visible()
            await shot(page, "12_edit_dialog", full=False)
            await dlg.get_by_label("Description").fill("Merchant-written description (UI test).")
            await dlg.get_by_label("Category").fill("Books")
            await dlg.get_by_role("button", name="Save changes").click()
            await expect(dlg).to_be_hidden(timeout=10000)
            await expect(page.get_by_text("Merchant-written description (UI test).")).to_be_visible()
            await expect(page.get_by_text(re.compile("managed in AgentReady.*description"))).to_be_visible()
            # Escape closes dialog (keyboard accessibility)
            await page.get_by_role("button", name="Edit details").click()
            await expect(page.get_by_role("dialog")).to_be_visible()
            await page.keyboard.press("Escape")
            await expect(page.get_by_role("dialog")).to_be_hidden()
        await step("Product detail, evidence, source page, edit dialog", detail_and_edit())

        async def approve_one():
            btn = page.get_by_role("button", name="Approve")
            await btn.click()
            await expect(page.get_by_text("Approved", exact=True).first).to_be_visible(timeout=10000)
        await step("Approve product from detail page", approve_one())

        async def bulk():
            await page.goto(BASE + "/app/catalog")
            await expect(page.locator("tbody tr").first).to_be_visible()
            boxes = page.locator("tbody tr td:first-child button[role=checkbox]")
            for i in range(1, 6):
                await boxes.nth(i).click()
            await expect(page.get_by_text("5 selected")).to_be_visible()
            await page.get_by_role("button", name="Approve").first.click()  # Phase 2: preview -> confirm
            dlg = page.get_by_role("dialog")
            await dlg.get_by_role("button", name="Preview changes").click()
            await dlg.get_by_role("button", name=re.compile("Confirm: apply to")).click()
            await expect(dlg.get_by_text(re.compile(r"(All \d+ product\(s\) updated|\d+ succeeded, \d+ failed)"))).to_be_visible(timeout=15000)
            await dlg.get_by_role("button", name="Close", exact=True).first.click()
            await shot(page, "13_catalog_after_bulk")
        await step("Bulk approval", bulk())

        async def validation():
            await page.goto(BASE + "/app/validation")
            await page.wait_for_timeout(1500)
            await shot(page, "14_validation")
            await page.get_by_label("Severity").select_option("")
            await page.wait_for_timeout(1000)
            body = await page.inner_text("main")
            return "issues listed" if "missing" in body.lower() or "No issues" in body or "Nothing to review" in body else "check screenshot"
        await step("Validation center", validation())

        # ---------------------------------------------------------- exports
        async def exports():
            await page.goto(BASE + "/app/exports")
            await expect(page.get_by_text("New export")).to_be_visible()
            sizes = {}
            for label, key in (("JSON", "json"), ("CSV", "csv"), ("JSON-LD", "jsonld")):
                await page.get_by_role("button", name=re.compile(f"^{re.escape(label)}")).first.click()
                async with page.expect_download() as dl_info:
                    await page.get_by_role("button", name=re.compile("Generate & download")).click()
                dl = await dl_info.value
                path = OUT / f"export.{key}"
                await dl.save_as(str(path))
                data = path.read_text(encoding="utf-8")
                sizes[key] = len(data)
                if key == "jsonld":
                    doc = json.loads(data)
                    assert doc["@type"] == "ItemList" and doc["numberOfItems"] >= 1
                    state["jsonld_items"] = doc["numberOfItems"]
                elif key == "json":
                    state["json_items"] = len(json.loads(data)["products"])
                else:
                    state["csv_rows"] = data.count("\n") - 1
            await page.wait_for_timeout(800)
            await shot(page, "15_exports")
            return f"json={state['json_items']} csv_rows={state['csv_rows']} jsonld={state['jsonld_items']} (approved only)"
        await step("Export JSON / CSV / JSON-LD downloads", exports())

        # ---------------------------------------------------------- settings
        async def settings():
            await page.goto(BASE + "/app/settings")
            await page.get_by_role("tab", name="Security & API").click()
            await page.get_by_role("button", name=re.compile("Create key")).click()
            key_input = page.locator("input[readonly]")
            await expect(key_input).to_be_visible(timeout=10000)
            key = await key_input.input_value()
            assert key.startswith("ar_cat_")
            await shot(page, "16_settings_api_key")
            r = await page.request.get(BASE + "/api/v1/catalog", headers={"X-Catalog-Key": key})
            body = await r.json()
            assert r.status == 200 and body["total"] >= 1, body
            r2 = await page.request.get(BASE + "/api/v1/catalog", headers={"X-Catalog-Key": "ar_cat_wrong"})
            assert r2.status == 401
            state["catalog_total"] = body["total"]
            await page.get_by_role("tab", name="AI usage").click()
            await expect(page.get_by_text("AI provider")).to_be_visible()
            await page.get_by_role("button", name="Verify model").click()
            await expect(page.get_by_text(re.compile("is available|unavailable|not configured", re.I)).first).to_be_visible(timeout=20000)
            await page.wait_for_timeout(500)
            await shot(page, "17_settings_ai")
            return f"catalog API returned {body['total']} approved products"
        await step("Catalog API key + AI usage/verify", settings())

        # ---------------------------------------------------------- rescan & change review
        async def rescan():
            await page.goto(BASE + "/app/connect")
            await expect(page.get_by_role("button", name=re.compile("Rescan"))).to_be_visible()
            # widen the crawl so the rescan finds additional products to review
            await page.goto(BASE + "/app/settings")
            await page.get_by_role("tab", name="Websites").click()
            inp = page.locator("tbody input[type=number]").first
            await inp.fill("20")
            await inp.blur()
            await page.wait_for_timeout(800)
            await page.goto(BASE + "/app/connect")
            await page.get_by_role("button", name=re.compile("Rescan")).click()
            await expect(page.get_by_text("Scan progress")).to_be_visible()
            done = page.get_by_role("link", name="Review changes").or_(page.get_by_role("link", name="Open catalog"))
            await expect(done).to_be_visible(timeout=300000)
            if not await page.get_by_role("link", name="Review changes").is_visible():
                return "rescan completed with no changes to review"
            await page.get_by_role("link", name="Review changes").click()
            await expect(page.get_by_text("Review before applying")).to_be_visible(timeout=15000)
            await shot(page, "18_change_review")
            boxes = page.locator("tbody button[role=checkbox]")
            n = await boxes.count()
            await boxes.first.click()  # deselect one change
            await page.get_by_role("button", name=re.compile(r"Apply \d+ change")).click()
            await expect(page.get_by_text("Changes applied")).to_be_visible(timeout=20000)
            await page.wait_for_timeout(1000)
            await shot(page, "19_change_applied")
            return f"{n} pending changes, applied {n - 1}, skipped 1"
        await step("Rescan, review changes, apply selected", rescan())

        async def edits_preserved():
            await page.goto(state["product_url"])
            await page.get_by_role("tab", name="Basic info").click()  # Phase 2 opens on the Overview section
            await expect(page.get_by_text("Merchant-written description (UI test).")).to_be_visible(timeout=10000)
        await step("Merchant edit preserved after rescan", edits_preserved())

        async def history():
            await page.goto(BASE + "/app/scans")
            await expect(page.locator("tbody tr").nth(1)).to_be_visible()
            await shot(page, "20_scan_history")
            await page.locator("tbody tr").first.click()
            await page.get_by_role("tab", name=re.compile("Pages")).click()
            await page.wait_for_timeout(600)
            await shot(page, "21_scan_pages")
        await step("Scan history and pages", history())

        # ---------------------------------------------------------- logout / login / reset
        async def logout_login():
            await page.locator("aside button").filter(has_text="Tosca Books Ltd").first.click()
            await page.get_by_role("menuitem", name="Sign out").click()
            await page.wait_for_url(re.compile(r"/login$"))
            await page.get_by_label("Email").fill(EMAIL)
            await page.get_by_label("Password").fill("wrong-password-9")
            await page.get_by_role("button", name="Sign in").click()
            await expect(page.get_by_text("Incorrect email or password")).to_be_visible()
            await page.get_by_label("Password").fill(PASSWORD)
            await page.get_by_role("button", name="Sign in").click()
            await page.wait_for_url(re.compile(r"/app$"))
        await step("Logout, failed login, login", logout_login())
        EXPECTED_4XX.append(("POST", "/auth/login", 401))

        async def reset():
            await page.locator("aside button").filter(has_text="Tosca Books Ltd").first.click()
            await page.get_by_role("menuitem", name="Sign out").click()
            await page.goto(BASE + "/forgot-password")
            await page.get_by_label("Email").fill(EMAIL)
            await page.get_by_role("button", name="Send reset link").click()
            link = page.locator("a[href*='reset-password?token=']")
            await expect(link).to_be_visible(timeout=10000)
            await shot(page, "22_forgot_password")
            await link.click()
            await page.get_by_label("New password").fill("brand-new-pass-77")
            await page.get_by_label("Confirm password").fill("brand-new-pass-77")
            await page.get_by_role("button", name="Update password").click()
            await page.wait_for_url(re.compile(r"/login$"))
            await page.get_by_label("Email").fill(EMAIL)
            await page.get_by_label("Password").fill("brand-new-pass-77")
            await page.get_by_role("button", name="Sign in").click()
            await page.wait_for_url(re.compile(r"/app$"))
        await step("Forgot / reset password (dev link) and sign in", reset())

        # ---------------------------------------------------------- dark mode
        async def dark():
            await page.get_by_role("button", name="Toggle theme").click()
            await page.goto(BASE + "/app/catalog")
            await page.wait_for_timeout(1000)
            await shot(page, "23_catalog_dark")
            await page.get_by_role("button", name="Toggle theme").click()
        await step("Dark mode", dark())

        # ---------------------------------------------------------- mobile
        mctx = await browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
        token = await page.evaluate("sessionStorage.getItem('ar-token')")
        m = await mctx.new_page()
        m.on("console", lambda msg: console_errors.append(f"[mobile] {m.url} :: {msg.text}") if msg.type == "error" else None)
        m.on("pageerror", lambda e: console_errors.append(f"[mobile] PAGEERROR {e}"))

        async def mobile():
            notes = []
            await m.goto(BASE + "/")
            await shot(m, "30_mobile_landing")
            await m.evaluate("t => sessionStorage.setItem('ar-token', t)", token)
            for path, name in (("/app", "31_mobile_overview"), ("/app/catalog", "32_mobile_catalog"),
                               (state["product_url"].replace(BASE, ""), "33_mobile_product"), ("/app/exports", "34_mobile_exports")):
                await m.goto(BASE + path)
                await m.wait_for_timeout(1500)
                overflow = await m.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                if overflow > 1:
                    notes.append(f"{path} page overflows by {overflow}px")
                await shot(m, name)
            await m.get_by_role("button", name="Open menu").click()
            await expect(m.get_by_role("link", name="Validation Center")).to_be_visible()
            await shot(m, "35_mobile_menu", full=False)
            assert not notes, "; ".join(notes)
        await step("Mobile layouts (390px) without horizontal overflow", mobile())

        await browser.close()

    # ---------------------------------------------------------- report
    unexpected = []
    for r in bad_responses:
        meth, rest = r.split(" ", 1)
        if not any(meth == em and re.search(ep, rest) and rest.endswith(str(es)) for em, ep, es in EXPECTED_4XX):
            unexpected.append(r)
    print("\n==== SUMMARY ====")
    for s, st, note in results:
        print(f"{st:5} {s}  {note}")
    print("\nConsole/page errors:", len(console_errors))
    for c in console_errors[:20]:
        print("  ", c[:300])
    print("API responses >= 400:", len(bad_responses), "| unexpected:", len(unexpected))
    for r in unexpected[:20]:
        print("  ", r)
    print("state:", state)


asyncio.run(main())
