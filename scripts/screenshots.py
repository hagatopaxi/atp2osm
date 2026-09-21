"""Screenshot every page at desktop, laptop and mobile sizes into screenshots.zip.

Usage: uv run --with playwright scripts/screenshots.py [session-cookie]

Needs the server on localhost:5000 and the system Chrome (no browser download).
With the `session` cookie of a signed-in browser, the brand pages are captured too.
"""

import sys
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import sync_playwright

BASE = "http://localhost:5000/fr"
BRAND = "Q1547738"
SIZES = {"desktop": (1440, 900), "laptop": (1280, 800), "mobile": (390, 844)}
PAGES = {
    "01_home": "/",
    "02_brands": "/brands",
    "03_spiders": "/spiders",
    "04_stats": "/stats",
    "05_docs": "/docs",
    "06_todo": "/todo",
    "07_history": "/history",
}
cookie = sys.argv[1] if len(sys.argv) > 1 else None
if cookie:
    PAGES.update(
        {
            f"{n}_brand_{p}": f"/brands/{BRAND}/{p}"
            for n, p in (("08", "validate"), ("09", "confirm"), ("10", "rejected"))
        }
    )

with sync_playwright() as p, TemporaryDirectory() as out:
    browser = p.chromium.launch(channel="chrome")
    ctx = browser.new_context()
    if cookie:
        ctx.add_cookies([{"name": "session", "value": cookie, "url": BASE}])
    page = ctx.new_page()
    for name, path in PAGES.items():
        for prof, (w, h) in SIZES.items():
            page.set_viewport_size({"width": w, "height": h})
            page.goto(BASE + path)
            page.wait_for_timeout(1500)  # ponytail: Tailwind's runtime compiles after load
            page.screenshot(path=f"{out}/{name}__{prof}_{w}x{h}.png")
            print(name, prof)
    browser.close()
    with zipfile.ZipFile("screenshots.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(Path(out).iterdir()):
            z.write(f, f.name)
print("screenshots.zip written")
