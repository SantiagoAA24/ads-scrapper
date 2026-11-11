"""End-to-end run of the scraper against a local page that behaves like the
Ad Library: first batch embedded in the HTML, the rest fetched via POST to a
graphql endpoint as the user scrolls."""

import os
import threading

import pytest

from ads_scrapper import db, runs
from ads_scrapper.scraper import ScrapeOptions, build_url, scrape

pytest.importorskip("playwright.sync_api")

def _options(base_url, tmp_path, **overrides):
    return ScrapeOptions(
        query="faja reductora",
        country="CO",
        headless=True,
        profile_dir=tmp_path / "profile",
        min_delay=0.2,
        max_delay=0.4,
        executable_path=os.environ.get("ADS_SCRAPPER_CHROMIUM"),
        base_url=base_url,
        **overrides,
    )


def _run(options, tmp_path):
    conn = db.connect(tmp_path / "ads.db")
    run_id = db.start_run(conn, options.query, options.country, "scrape")
    result = scrape(options, lambda ads: db.save_ads(conn, run_id, ads), log=lambda _: None)
    return result, conn


def test_captures_embedded_and_paginated_ads(fake_library, tmp_path):
    base_url, requests = fake_library
    result, conn = _run(_options(base_url, tmp_path), tmp_path)

    assert result.status == "completed"
    assert (result.ads_seen, result.ads_new) == (29, 29)
    assert conn.execute("SELECT COUNT(*) FROM ads").fetchone()[0] == 29
    assert "q=faja+reductora" in requests[0] and "country=CO" in requests[0]


def test_stops_at_max_ads(fake_library, tmp_path):
    base_url, _ = fake_library
    result, conn = _run(_options(base_url, tmp_path, max_ads=15), tmp_path)

    assert result.status == "max_ads"
    assert result.ads_seen >= 15  # a batch already on its way may still land
    assert conn.execute("SELECT COUNT(*) FROM ads").fetchone()[0] == result.ads_seen


def test_build_url_has_the_library_filters():
    url = build_url(ScrapeOptions(query="té verde", country="MX", media_type="video"))
    assert url.startswith("https://www.facebook.com/ads/library/?")
    for part in ("country=MX", "media_type=video", "active_status=active", "search_type=keyword_unordered"):
        assert part in url


def test_saved_search_runs_from_a_background_thread(fake_library, tmp_path):
    """The web app scrapes off the main thread; the browser must work there."""
    base_url, requests = fake_library
    conn = db.connect(tmp_path / "ads.db")
    search_id = db.save_search(conn, "faja reductora", "co", max_ads=100)
    conn.close()
    outcome = {}

    def work():
        thread_conn = db.connect(tmp_path / "ads.db")
        options = _options(base_url, tmp_path)
        outcome["result"] = runs.run_search(
            thread_conn,
            db.get_search(thread_conn, search_id),
            log=lambda _: None,
            headless=True,
            profile_dir=options.profile_dir,
            min_delay=0.2,
            max_delay=0.4,
            executable_path=options.executable_path,
            base_url=base_url,
        )
        thread_conn.close()

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(timeout=60)

    assert outcome["result"].status == "completed"
    assert "country=CO" in requests[0]
    conn = db.connect(tmp_path / "ads.db")
    run = conn.execute("SELECT * FROM scrape_runs").fetchone()
    assert (run["search_id"], run["status"], run["ads_seen"]) == (search_id, "completed", 29)
    assert db.get_search(conn, search_id)["last_run_at"]
