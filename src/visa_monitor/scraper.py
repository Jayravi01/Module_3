"""Fetch government pages and reduce them to clean, comparable text.

Why the cleaning matters: a page's raw HTML changes constantly (session tokens,
analytics scripts, "page last updated" stamps). Hashing raw HTML would raise a
false "change" every day. We hash only the normalised main-content text, so a
change in the hash means the words a reader sees actually changed.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from .config import Settings

log = logging.getLogger(__name__)
MIN_TEXT = 200   # fewer characters than this = page didn't really load

NOISE_TAGS = ["script", "style", "noscript", "nav", "header", "footer", "aside",
              "svg", "iframe", "button", "select", "input"]
# NOTE: <form> is deliberately NOT removed. Home Affairs (SharePoint) wraps the whole
# page in one <form>, so removing forms deleted all of the content.
MINIMAL_NOISE = ["script", "style", "noscript", "svg", "iframe"]
NOISE_SELECTORS = ['[role="navigation"]', '[aria-label*="breadcrumb" i]', ".breadcrumb",
                   ".cookie", "#cookie", ".skip-link", ".share", ".social"]
# Lines that change without the policy changing
VOLATILE_LINE_PATTERNS = [
    re.compile(r"^(page )?last (updated|reviewed|modified)", re.I),
    re.compile(r"^(updated|reviewed)\s*:?\s*\d", re.I),
    re.compile(r"^©|copyright", re.I),
    re.compile(r"^print this page$|^share this page$|^back to top$", re.I),
]


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int | None
    text: str | None
    content_hash: str | None
    error: str | None = None


def _to_text(soup, root) -> str:
    # Keep table rows on one line so processing-time tables diff cleanly
    for tr in root.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        tr.replace_with(soup.new_string("\n" + " | ".join(cells) + "\n"))
    lines = []
    for raw in root.get_text("\n").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if len(line) < 2 or any(p.search(line) for p in VOLATILE_LINE_PATTERNS):
            continue
        lines.append(line)
    # Collapse consecutive duplicates (repeated menu labels etc.)
    return "\n".join(l for i, l in enumerate(lines) if i == 0 or l != lines[i - 1])


def _extract(html: str, selector: str | None, tags: list[str], selectors: list[str]) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(tags):
        tag.decompose()
    for sel in selectors:
        for el in soup.select(sel):
            el.decompose()
    root = soup.select_one(selector) if selector else None
    root = root or soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
    return _to_text(soup, root)


def normalise_html(html: str, selector: str | None = None) -> str:
    """Extract the main readable content as newline-separated, de-noised text.
    Strict cleaning first; if that leaves almost nothing (a page whose layout
    tags wrap the content), fall back to removing only scripts and styles."""
    text = _extract(html, selector, NOISE_TAGS, NOISE_SELECTORS)
    if len(text) < 200:
        text = _extract(html, selector, MINIMAL_NOISE, [])
    return text


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Scraper:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.settings.user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-AU,en;q=0.9",
        })
        self._robots: dict[str, RobotFileParser | None] = {}

    # -- robots.txt -------------------------------------------------------
    def allowed(self, url: str) -> bool:
        host = urlparse(url)
        base = f"{host.scheme}://{host.netloc}"
        if base not in self._robots:
            rp = RobotFileParser()
            try:
                resp = self.session.get(base + "/robots.txt", timeout=self.settings.request_timeout)
                rp.parse(resp.text.splitlines() if resp.ok else [])
                self._robots[base] = rp
            except requests.RequestException:
                self._robots[base] = None   # robots unreachable: proceed politely
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.settings.user_agent, url)

    # -- fetching ---------------------------------------------------------
    def _get_static(self, url: str) -> tuple[int, str]:
        last_exc: Exception | None = None
        for attempt in range(3):                 # retry with backoff
            try:
                r = self.session.get(url, timeout=self.settings.request_timeout)
                if r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                return r.status_code, r.text
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(2 ** attempt)
        raise last_exc  # type: ignore[misc]

    def _browser(self):
        """Start one headless browser and reuse it for every JS page in the run."""
        if getattr(self, "_pw_browser", None) is None:
            from playwright.sync_api import sync_playwright  # optional dependency
            self._pw = sync_playwright().start()
            try:   # prefer the user's installed Google Chrome; fall back to bundled Chromium
                self._pw_browser = self._pw.chromium.launch(channel="chrome", headless=True)
            except Exception:
                self._pw_browser = self._pw.chromium.launch(headless=True)
            self._pw_context = self._pw_browser.new_context(
                user_agent=self.settings.user_agent, locale="en-AU",
                timezone_id=self.settings.timezone, viewport={"width": 1366, "height": 900})
        return self._pw_context

    def _get_js(self, url: str) -> tuple[int, str]:
        page = self._browser().new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded",
                             timeout=self.settings.request_timeout * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass                       # some pages keep polling; content is usually there
            page.wait_for_timeout(1500)
            return (resp.status if resp else 200), page.content()
        finally:
            page.close()

    def close(self):
        if getattr(self, "_pw_browser", None) is not None:
            self._pw_browser.close()
            self._pw.stop()
            self._pw_browser = None

    def _extract(self, html: str, selector: str | None) -> str:
        text = normalise_html(html, selector)
        if len(text) < MIN_TEXT and selector:      # selector missed the content: use whole page
            text = normalise_html(html, None)
        return text

    def _debug_save(self, source: dict, html: str, method: str):
        folder = os.getenv("DEBUG_DIR")
        if folder:
            Path(folder).mkdir(parents=True, exist_ok=True)
            Path(folder, f"{source['key']}_{method}.html").write_text(html, encoding="utf-8")

    def fetch(self, source: dict) -> FetchResult:
        """Static download first; if the page is a JavaScript shell (little text) or the
        source is marked render: js, load it in a real headless browser instead."""
        url = source["url"]
        try:
            if not self.allowed(url):
                return FetchResult(url, False, None, None, None, "Blocked by robots.txt")

            status, text, method = None, "", "static"
            if source.get("render") != "js":
                status, html = self._get_static(url)
                self._debug_save(source, html, "static")
                if status < 400:
                    text = self._extract(html, source.get("selector"))

            needs_browser = source.get("render") == "js" or status is None or status >= 400 \
                or len(text) < MIN_TEXT
            if needs_browser and self.settings.enable_js_render:
                try:
                    status, html = self._get_js(url)
                    method = "browser"
                    self._debug_save(source, html, "browser")
                    text = self._extract(html, source.get("selector")) if status < 400 else ""
                except ImportError:
                    log.warning("Playwright not installed (pip install playwright && "
                                "playwright install chromium); %s needs it", source["key"])
                    if status is None:
                        status, html = self._get_static(url)
                        text = self._extract(html, source.get("selector")) if status < 400 else ""

            if status is not None and status >= 400:
                hint = " (site blocked the request — see README troubleshooting)" if status == 403 else ""
                return FetchResult(url, False, status, None, None, f"HTTP {status} via {method}{hint}")
            if len(text) < MIN_TEXT:
                hint = "" if self.settings.enable_js_render else " — set ENABLE_JS_RENDER=true"
                return FetchResult(url, False, status, text, None,
                                   f"Very little text extracted via {method}{hint}")
            return FetchResult(url, True, status, text, content_hash(text))
        except Exception as exc:  # never let one source break the whole run
            log.exception("Fetch failed for %s", url)
            return FetchResult(url, False, None, None, None, f"{type(exc).__name__}: {exc}")
        finally:
            time.sleep(self.settings.polite_delay_seconds)
