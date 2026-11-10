"""Phase 5 browser walkthrough: production-readiness checks through the real UI (Chromium via Playwright).

    AR_WEB=http://localhost:8080 python eval/ui_walkthrough_phase5.py      # docker stack (nginx: CSP, headers)
    AR_WEB=http://localhost:5173 python eval/ui_walkthrough_phase5.py      # Vite dev stack

1. Registers a merchant through the UI and seeds a small catalog through the public API.
2. Opens EVERY page in the sidebar at desktop width and at phone width (390 px) and records, per page:
   console errors (including Content-Security-Policy violations), uncaught page errors, HTTP 5xx responses,
   horizontal overflow and whether the page rendered its heading.
3. System status page: real dependency checks are shown.
4. Session expiry: a removed/invalid session redirects to the sign-in page instead of showing a broken page.
5. Security headers on the HTML document (when served by nginx).
Screenshots: eval/results/screenshots/p5_*.png
"""
import asyncio
import os
import sys
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, expect

BASE = os.environ.get("AR_WEB", "http://localhost:5173").rstrip("/")
OUT = Path(__file__).parent / "results" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)
EMAIL = f"prod{int(time.time())}@example.com"
PASSWORD = "merchant-pass-2026"
results: list[tuple[str, str, str]] = []


def record(step, status, note=""):
    results.append((step, status, note))
    print(f"[{status}] {step} {note}", flush=True)


async def step(name, coro):
    try:
        note = await coro
        record(name, "PASS", note or "")
        return True
    except Exception as e:  # noqa: BLE001
        record(name, "FAIL", f"{type(e).__name__}: {str(e)[:500]}")
        return False


def seed(token: str) -> None:
    h = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=BASE, timeout=60) as http:
        def call(method, path, body=None):
            r = http.request(method, f"/api/v1{path}", json=body, headers=h)
            assert r.status_code < 400, f"{method} {path} -> {r.status_code} {r.text[:200]}"
            return r.json() if r.status_code != 204 else None

        for i, (name, price) in enumerate([("Cordless Drill 18V", "4999"), ("Angle Grinder", "3200"), ("LED Panel 40W", "899")]):
            p = call("POST", "/products", {"name": name, "sku": f"P5-{i}", "regular_price": price, "currency": "INR",
                                           "lifecycle_status": "active", "brand": "Bosch", "availability": "InStock",
                                           "description": f"{name} for workshops and homes."})
            call("PATCH", f"/products/{p['id']}", {"approval_status": "approved"})
        call("PUT", "/distribution/settings", {"enabled": True, "public_name": "Phase 5 Store", "public_url": "https://p5.example"})
        call("POST", "/intelligence/refresh")


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1366, "height": 900})
        page = await ctx.new_page()
        console: list[str] = []
        errors: list[str] = []
        bad: list[str] = []
        page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 500 else None)
        doc_headers: dict = {}

        async def register():
            resp = await page.goto(f"{BASE}/signup")
            doc_headers.update(await resp.all_headers())
            await page.get_by_label("Business name").fill("Phase 5 Store")
            await page.get_by_label("Your name").fill("Prod Check")
            await page.get_by_label("Work email").fill(EMAIL)
            await page.get_by_label("Password").fill(PASSWORD)
            await page.get_by_role("button", name="Create account").click()
            await page.wait_for_url("**/app/connect", timeout=20000)
            token = await page.evaluate("sessionStorage.getItem('ar-token')")
            seed(token)
            return EMAIL

        await step("1. register through the UI + seed catalog", register())

        links = await page.locator("nav a[href^='/app']").evaluate_all(
            "els => els.map(e => [e.getAttribute('href'), e.textContent.trim()])")
        seen = set()
        pages = [(h, t) for h, t in links if not (h in seen or seen.add(h))]
        problems: list[str] = []

        async def visit_all(width: int, label: str):
            await page.set_viewport_size({"width": width, "height": 900})
            n = 0
            for href, text in pages:
                console.clear(); errors.clear(); bad.clear()
                await page.goto(f"{BASE}{href}")
                await page.wait_for_load_state("networkidle")
                h1 = page.locator("main h1").first
                try:
                    await expect(h1).to_be_visible(timeout=15000)
                except AssertionError:
                    problems.append(f"{label} {href}: no page heading rendered")
                overflow = await page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                if overflow > 1:
                    problems.append(f"{label} {href}: horizontal overflow {overflow}px")
                real_console = [c for c in console if "favicon" not in c]
                for c in real_console:
                    problems.append(f"{label} {href}: console error: {c[:160]}")
                for e in errors:
                    problems.append(f"{label} {href}: page error: {e[:160]}")
                for b in bad:
                    problems.append(f"{label} {href}: server error {b}")
                if width < 500 and href in ("/app", "/app/system", "/app/intelligence"):
                    await page.screenshot(path=str(OUT / f"p5_mobile_{href.strip('/').replace('/', '_') or 'app'}.png"))
                n += 1
            return n

        async def desktop():
            n = await visit_all(1366, "desktop")
            bad_pages = [p for p in problems if p.startswith("desktop")]
            assert not bad_pages, "; ".join(bad_pages[:8])
            return f"{n} pages, no console/CSP errors, no 5xx, no overflow"

        async def mobile():
            n = await visit_all(390, "mobile")
            bad_pages = [p for p in problems if p.startswith("mobile")]
            assert not bad_pages, "; ".join(bad_pages[:8])
            return f"{n} pages at 390px"

        await step(f"2a. every sidebar page renders cleanly (desktop)", desktop())
        await step(f"2b. every sidebar page renders cleanly (phone width)", mobile())

        async def system_status():
            await page.set_viewport_size({"width": 1366, "height": 900})
            await page.goto(f"{BASE}/app/system")
            await expect(page.get_by_role("heading", name="System status")).to_be_visible()
            await expect(page.get_by_text("Database (PostgreSQL)")).to_be_visible(timeout=15000)
            body = await page.locator("main").inner_text()
            await page.screenshot(path=str(OUT / "p5_system_status.png"), full_page=True)
            with httpx.Client(base_url=BASE, timeout=30) as http:
                health = http.get("/health").json() if BASE.endswith(":8080") else None
            assert "Background jobs" in body and "Recent failures" in body
            return f"overall={health['status'] if health else 'n/a'}; checks shown: " + ", ".join(
                k for k in ("Database", "Job queue", "Background worker", "Scheduler") if k in body)

        await step("3. system status page shows real dependency checks", system_status())

        async def session_expiry():
            await page.evaluate("sessionStorage.setItem('ar-token', 'expired.invalid.token')")
            await page.goto(f"{BASE}/app/catalog")
            await page.wait_for_url("**/login**", timeout=15000)
            await expect(page.get_by_role("button", name="Sign in")).to_be_visible()
            return "invalid session -> sign-in page"

        await step("4. expired/invalid session redirects to sign-in", session_expiry())

        async def headers():
            if "content-security-policy" not in doc_headers:
                return "skipped: not served by nginx (Vite dev server)"
            csp = doc_headers["content-security-policy"]
            assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp, csp
            for h in ("x-frame-options", "x-content-type-options", "referrer-policy", "permissions-policy"):
                assert h in doc_headers, h
            return "CSP (no inline scripts), frame/nosniff/referrer/permissions headers present"

        await step("5. security headers on the HTML document", headers())
        await browser.close()

    passed = sum(1 for _, s, _ in results if s == "PASS")
    print(f"\nPhase 5 walkthrough: {passed}/{len(results)} steps passed against {BASE}")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
