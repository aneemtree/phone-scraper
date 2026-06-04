"""One-off: add the Budli store metadata row to the `stores` table.

Run where SUPABASE_URL / SUPABASE_SERVICE_KEY are set (same env as the scrapers):
    python3 add_store_budli.py

Idempotent: upserts on `site` so re-running won't create a duplicate. Set the
logo_url afterwards once the logo is uploaded to the Supabase "logos" bucket.
"""
from db import supabase

ROW = {
    "site": "budli",
    "display_name": "Budli",
    "website_url": "https://buy.budli.in",
}

if __name__ == "__main__":
    res = supabase.table("stores").upsert(ROW, on_conflict="site").execute()
    print("Upserted stores row:", res.data)
