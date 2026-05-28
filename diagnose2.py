"""Diagnostic for a PREMIUM (no-table) product: find where the price and the
storage options live in the rendered DOM."""
import re
from playwright.sync_api import sync_playwright

URL = "https://www.controlz.world/products/apple-iphone-11"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(user_agent=UA, viewport={"width": 1366, "height": 900})
    page.goto(URL, timeout=60000, wait_until="domcontentloaded")
    page.wait_for_selector(".variant-options-container", timeout=20000)
    page.wait_for_timeout(2000)

    print("=== tables on page ===", len(page.query_selector_all("table")))

    print("\n=== visible buttons in variant-options-container ===")
    for b in page.query_selector_all(".variant-options-container button"):
        if b.is_visible():
            span = b.query_selector("span")
            txt = (span.inner_text().strip() if span else b.inner_text().strip())
            print("  btn:", txt[:40])

    print("\n=== rupee strings in rendered HTML (first 15) ===")
    prices = re.findall(r'₹\s?[\d,]{3,}', page.content())
    print(" ", prices[:15])

    print("\n=== all section h2 headings ===")
    for h in page.query_selector_all("h2"):
        t = h.inner_text().strip()
        if t:
            print("  h2:", t[:50])

    # Try to locate the storage option buttons by their text
    print("\n=== buttons containing 'GB' (storage options) ===")
    for b in page.query_selector_all("button"):
        t = b.inner_text().strip()
        if re.search(r'\d+\s?GB', t) and len(t) < 30:
            print(f"  storage btn: '{t[:30]}' visible={b.is_visible()}")

    browser.close()
