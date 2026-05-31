from dotenv import load_dotenv; load_dotenv()
from playwright.sync_api import sync_playwright

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
URL = "https://www.controlz.world/products/apple-iphone-11?variant=d9d8c2ff-d6a1-4bf8-80f5-b69a06c37779"

JS_PRICES = """() => {
    const results = [];
    const ps = Array.from(document.querySelectorAll('p'));
    ps.forEach(el => {
        try {
            const t = (el.innerText || '').trim();
            if (t && /\u20b9/.test(t) && /[0-9]/.test(t)) {
                results.push({ text: t.substring(0, 30), cls: (el.className || '').substring(0, 80) });
            }
        } catch(e) {}
    });
    return results;
}"""

JS_CURRENT = """() => {
    const ps = Array.from(document.querySelectorAll('p'));
    const pe = ps.find(function(p) {
        return /text-primary/.test(p.className || '') && /\u20b9/.test(p.textContent || '');
    });
    return pe ? (pe.textContent || '').trim() : 'NOT FOUND';
}"""

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(user_agent=UA)
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    prices = page.evaluate(JS_PRICES)
    print("All rupee <p> elements:")
    for p in prices:
        print(f"  {p['text']:30} cls={p['cls'][:60]}")

    current = page.evaluate(JS_CURRENT)
    print(f"\nCurrent read_price result: {current}")
    browser.close()