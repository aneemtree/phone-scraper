"""One-off: add the Gadget Rebirth store metadata row to the `stores` table.

Run where SUPABASE_URL / SUPABASE_SERVICE_KEY are set (same env as the scrapers):
    python3 add_store_gadgetrebirth.py

Idempotent: upserts on `site` so re-running won't create a duplicate. Set the
logo_url afterwards once the logo is uploaded to the Supabase "logos" bucket.
"""
from db import supabase

ROW = {
    "site": "gadgetrebirth",
    "display_name": "Gadget Rebirth",
    "website_url": "https://www.gadgetrebirth.com",
}

if __name__ == "__main__":
    res = supabase.table("stores").upsert(ROW, on_conflict="site").execute()
    print("Upserted stores row:", res.data)
