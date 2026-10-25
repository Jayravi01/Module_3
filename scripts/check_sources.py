"""Check every source in config/sources.yaml before the first run:
reachable? allowed by robots.txt? enough text extracted?"""
import _bootstrap  # noqa: F401
from visa_monitor.config import Settings, load_sources
from visa_monitor.scraper import Scraper

settings = Settings()
scraper = Scraper(settings)
for src in load_sources():
    r = scraper.fetch(src)
    mark = "OK " if r.ok else "ERR"
    size = len(r.text or "")
    print(f"[{mark}] {src['key']:<28} status={r.status} chars={size:<6} {r.error or ''}")
