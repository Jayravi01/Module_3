"""Diagnose one source: shows how it loads with a plain download and with the
headless browser, and saves the raw HTML + extracted text to logs/debug/.

    python scripts/debug_source.py ha_student_500
"""
import os
import sys
from pathlib import Path

import _bootstrap  # noqa: F401
from visa_monitor.config import Settings, load_sources
from visa_monitor.scraper import Scraper

key = sys.argv[1] if len(sys.argv) > 1 else "ha_student_500"
src = next((s for s in load_sources() if s["key"] == key), None)
if not src:
    sys.exit(f"Unknown key {key}. Keys: {[s['key'] for s in load_sources()]}")

out = Path("logs/debug"); out.mkdir(parents=True, exist_ok=True)
os.environ["DEBUG_DIR"] = str(out)
scraper = Scraper(Settings())

print("Plain download:")
try:
    status, html = scraper._get_static(src["url"])
    text = scraper._extract(html, src.get("selector"))
    print(f"  HTTP {status}, {len(html):,} chars of HTML, {len(text):,} chars of text")
except Exception as e:
    print(f"  failed: {e}")

print("Headless browser:")
try:
    status, html = scraper._get_js(src["url"])
    text = scraper._extract(html, src.get("selector"))
    (out / f"{key}_browser.txt").write_text(text, encoding="utf-8")
    print(f"  HTTP {status}, {len(html):,} chars of HTML, {len(text):,} chars of text")
    print("  first lines:", " | ".join(text.splitlines()[:6])[:300])
except Exception as e:
    print(f"  failed: {type(e).__name__}: {e}")
finally:
    scraper.close()
print(f"\nSaved files in {out.resolve()}")
