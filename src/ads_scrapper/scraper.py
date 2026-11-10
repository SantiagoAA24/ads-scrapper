"""Drives a real browser over the Ad Library and captures the result payloads.

Nothing is read from the rendered page: the ads come from the JSON the page
itself downloads (embedded in the HTML for the first batch, GraphQL responses
for the rest), so layout changes do not break it.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from . import parser

LIBRARY_URL = "https://www.facebook.com/ads/library/"
DEFAULT_PROFILE_DIR = Path("data/browser-profile")
BLOCK_MARKERS = ("/login", "/checkpoint", "/privacy/consent")


@dataclass
class ScrapeOptions:
    query: str
    country: str = "ALL"
    active_status: str = "active"
    media_type: str = "all"
    max_ads: int = 300
    headless: bool = False
    profile_dir: Path = DEFAULT_PROFILE_DIR
    # Seconds to wait between scrolls, picked at random in this range.
    min_delay: float = 2.0
    max_delay: float = 5.0
    # Give up after this many scrolls in a row that bring no new ads.
    max_idle_scrolls: int = 6
    executable_path: str | None = None
    # For tests: point the scraper at a local page instead of Meta.
    base_url: str = LIBRARY_URL


@dataclass
class ScrapeResult:
    status: str  # completed | max_ads | stalled | blocked
    ads_seen: int = 0
    ads_new: int = 0


def build_url(options: ScrapeOptions) -> str:
    params = {
        "active_status": options.active_status,
        "ad_type": "all",
        "country": options.country,
        "media_type": options.media_type,
        "q": options.query,
        "search_type": "keyword_unordered",
    }
    return f"{options.base_url}?{urlencode(params)}"


def scrape(
    options: ScrapeOptions,
    on_ads: Callable[[list[dict[str, Any]]], tuple[int, int]],
    log: Callable[[str], None] = print,
) -> ScrapeResult:
    """Run one search. ``on_ads`` stores a batch and returns (seen, new)."""
    from playwright.sync_api import sync_playwright

    result = ScrapeResult(status="stalled")
    state = {"has_next_page": None, "pending": []}

    def handle_text(text: str) -> None:
        batch = parser.parse_text(text)
        if batch.has_next_page is not None:
            state["has_next_page"] = batch.has_next_page
        if batch.ads:
            state["pending"].append(batch.ads)

    def on_response(response) -> None:
        if response.request.method != "POST" or "graphql" not in response.url:
            return
        try:
            handle_text(response.text())
        except Exception:
            # Body unavailable (navigation, aborted request): nothing to save.
            pass

    def flush() -> int:
        """Store captured batches; returns how many unseen ads they held."""
        added = 0
        while state["pending"]:
            seen, new = on_ads(state["pending"].pop(0))
            result.ads_seen += seen
            result.ads_new += new
            added += seen
        return added

    options.profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(options.profile_dir),
            headless=options.headless,
            executable_path=options.executable_path,
            viewport={"width": 1366, "height": 850},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.on("response", on_response)
            page.goto(build_url(options), wait_until="domcontentloaded")
            if _is_blocked(page.url):
                result.status = "blocked"
                return result

            # First batch: embedded in the HTML rather than fetched.
            for text in page.locator('script[type="application/json"]').all_text_contents():
                handle_text(text)
            _pause(options)
            flush()
            log(f"Carga inicial: {result.ads_seen} anuncios")

            idle = 0
            while True:
                if result.ads_seen >= options.max_ads:
                    result.status = "max_ads"
                    break
                if state["has_next_page"] is False:
                    result.status = "completed"
                    break
                if idle >= options.max_idle_scrolls:
                    result.status = "stalled"
                    break
                if _is_blocked(page.url):
                    result.status = "blocked"
                    break

                page.mouse.move(random.randint(300, 1000), random.randint(200, 700))
                page.mouse.wheel(0, random.randint(1800, 3200))
                _pause(options)
                if flush():
                    idle = 0
                    log(f"{result.ads_seen} anuncios ({result.ads_new} nuevos)")
                else:
                    idle += 1
                    # Nudge to the very bottom in case the wheel fell short.
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            flush()
        finally:
            context.close()
    return result


def _is_blocked(url: str) -> bool:
    return any(marker in url for marker in BLOCK_MARKERS)


def _pause(options: ScrapeOptions) -> None:
    time.sleep(random.uniform(options.min_delay, options.max_delay))
