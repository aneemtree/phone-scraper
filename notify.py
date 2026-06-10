"""
Price-drop Web Push notifications.

Runs at the END of a scrape (scrape.yml). For every phone someone subscribed to
(price_alerts table, written by the website's /api/notify/subscribe route), it
recomputes the current lowest in-stock price and, if it dropped below the
subscriber's stored baseline, sends a browser push with the new price and
updates the baseline. Expired/gone subscriptions (404/410) are deleted.

Env: SUPABASE_*, VAPID_PRIVATE_KEY (base64url raw key paired with the website's
NEXT_PUBLIC_VAPID_PUBLIC_KEY), VAPID_SUBJECT (mailto:...). No-op if VAPID isn't
configured, so normal scrape runs are unaffected when push isn't set up.
"""
import json
import os

from db import supabase, _exec
from obs import init_sentry, log_error

SITE = "https://www.whatphone.co"
VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE_KEY")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:hello@whatphone.co")


def current_low_price(vk):
    """Lowest in-stock price for a coalesce key (offers.variant_key = baseKey)."""
    rows = _exec(lambda: supabase.table("offers").select("price")
                 .eq("variant_key", vk).eq("in_stock", True)
                 .eq("availability", "in_stock").execute()).data
    prices = [float(r["price"]) for r in (rows or []) if r.get("price") is not None]
    return min(prices) if prices else None


def inr(n):
    return "₹" + format(int(round(n)), ",d")


def run():
    if not VAPID_PRIVATE:
        print("VAPID_PRIVATE_KEY not set — skipping price-drop notifications.")
        return
    from pywebpush import webpush, WebPushException

    alerts = _exec(lambda: supabase.table("price_alerts").select("*").execute()).data or []
    if not alerts:
        print("No price alerts subscribed.")
        return

    # Compute current price once per watched phone.
    by_vk = {}
    for a in alerts:
        by_vk.setdefault(a["variant_key"], []).append(a)
    current = {vk: current_low_price(vk) for vk in by_vk}

    sent = dropped = removed = 0
    for vk, group in by_vk.items():
        cur = current[vk]
        if cur is None:
            continue
        for a in group:
            base = a.get("last_price")
            base = float(base) if base is not None else None
            if base is not None and cur < base:
                title = "Price drop on " + (a.get("model") or "a phone you're watching")
                body = f"Now {inr(cur)} (was {inr(base)}). Tap to compare stores."
                url = SITE + (a.get("url") or f"/phone/{vk}")
                try:
                    webpush(
                        subscription_info={"endpoint": a["endpoint"],
                                           "keys": {"p256dh": a["p256dh"], "auth": a["auth"]}},
                        data=json.dumps({"title": title, "body": body, "url": url, "tag": vk}),
                        vapid_private_key=VAPID_PRIVATE,
                        vapid_claims={"sub": VAPID_SUBJECT},  # fresh dict (pywebpush mutates it)
                    )
                    sent += 1
                except WebPushException as e:
                    code = getattr(e.response, "status_code", None)
                    if code in (404, 410):  # subscription expired/unsubscribed
                        _exec(lambda: supabase.table("price_alerts").delete().eq("id", a["id"]).execute())
                        removed += 1
                        continue
                    log_error(e, stage="webpush", variant=vk)
                dropped += 1
            # Re-baseline to the current price so the next drop is measured from here.
            if base is None or cur != base:
                _exec(lambda: supabase.table("price_alerts").update({"last_price": cur}).eq("id", a["id"]).execute())

    print(f"Price alerts: {len(alerts)} subscriptions, {dropped} drops, {sent} sent, {removed} expired.")


if __name__ == "__main__":
    init_sentry("notify")
    try:
        run()
    except Exception as exc:
        log_error(exc)
        raise
