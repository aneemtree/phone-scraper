from dotenv import load_dotenv; load_dotenv()
from playwright.sync_api import sync_playwright
import sys
sys.path.insert(0, '/Users/1lessidiot/phone-scraper')
from cashify import get_option_divs, click_option, read_price

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
URL = "https://www.cashify.in/buy-refurbished-mobile-phones/renewed-apple-iphone-14-pro-max/88031"

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False)
    page = browser.new_page(user_agent=UA)
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    conditions = get_option_divs(page, "Condition")
    print("Conditions found:", conditions)

    for cond in conditions:
        if not cond["available"]:
            print(f"  {cond['text']}: SKIPPED (unavailable)")
            continue
        clicked = click_option(page, "Condition", cond["text"])
        page.wait_for_timeout(1000)
        price = read_price(page)
        storages = get_option_divs(page, "Storage")
        print(f"  {cond['text']}: clicked={clicked} price={price} storages={storages}")

    browser.close()