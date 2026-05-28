"""One-off diagnostic: load the Oppo page in Playwright, force desktop viewport,
save a screenshot + the rendered HTML of the category area so we can see what
state the page is actually in."""
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

URL = "https://www.controlz.world/products/oppo-f11-pro"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    # Force a desktop viewport so nothing is collapsed behind mobile toggles
    page = browser.new_page(user_agent=UA, viewport={"width": 1366, "height": 900})
    page.goto(URL, timeout=60000, wait_until="domcontentloaded")
    # Wait for the category section specifically, not for the whole network to idle.
    try:
        page.wait_for_selector(".variant-options-container", timeout=20000)
    except Exception as e:
        print("category container did not appear:", e)
    page.wait_for_timeout(2000)

    page.screenshot(path="oppo_render.png", full_page=True)

    # Count category options and whether a table is present
    btns = page.query_selector_all(".variant-options-container button")
    print("category option buttons:", len(btns))
    for i, b in enumerate(btns):
        span = b.query_selector("span")
        txt = span.inner_text().strip() if span else "?"
        visible = b.is_visible()
        enabled = b.is_enabled()
        print(f"  [{i}] '{txt}' visible={visible} enabled={enabled}")

    tables = page.query_selector_all("table")
    print("tables on page:", len(tables))
    if tables:
        soup = BeautifulSoup(page.content(), "html.parser")
        t = soup.find("table")
        rows = t.select("tbody tr")
        print("first table rows:", len(rows))
        for tr in rows[:3]:
            print("  row:", [td.get_text(strip=True) for td in tr.find_all("td")])

    browser.close()
print("saved screenshot to oppo_render.png")