"""End-to-end run of the scraper against a local page that behaves like the
Ad Library: first batch embedded in the HTML, the rest fetched via POST to a
graphql endpoint as the user scrolls."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ads_scrapper import db
from ads_scrapper.scraper import ScrapeOptions, build_url, scrape

pytest.importorskip("playwright.sync_api")

PAGE = """<!doctype html><html><body style="margin:0">
<div id="filler" style="height:4000px"></div>
<script type="application/json" data-sjs>__FIRST__</script>
<script>
let page = 0, busy = false;
addEventListener('scroll', async () => {
  if (busy || innerHeight + scrollY < document.body.scrollHeight - 200) return;
  busy = true;
  const r = await fetch('/api/graphql/', {method: 'POST', body: 'page=' + (++page)});
  if (r.ok) {
    await r.text();
    document.getElementById('filler').style.height = (4000 * (page + 1)) + 'px';
  }
  busy = false;
});
</script></body></html>"""


def _page(payload, edges, has_next):
    doc = json.loads(json.dumps(payload))
    connection = doc["data"]["ad_library_main"]["search_results_connection"]
    connection["edges"] = edges
    connection["page_info"]["has_next_page"] = has_next
    return json.dumps(doc)


@pytest.fixture()
def fake_library(payload):
    edges = payload["data"]["ad_library_main"]["search_results_connection"]["edges"]
    first = _page(payload, edges[:10], True)
    later = [_page(payload, edges[10:20], True), _page(payload, edges[20:], False)]
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            self._send("text/html", PAGE.replace("__FIRST__", first))

        def do_POST(self):
            number = int(self.rfile.read(int(self.headers["Content-Length"])).split(b"=")[1])
            if number > len(later):
                self.send_error(404)
                return
            self._send("application/json", later[number - 1])

        def _send(self, content_type, body):
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/ads/library/", requests
    server.shutdown()


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
    assert 15 <= result.ads_seen < 29
    assert conn.execute("SELECT COUNT(*) FROM ads").fetchone()[0] == result.ads_seen


def test_build_url_has_the_library_filters():
    url = build_url(ScrapeOptions(query="té verde", country="MX", media_type="video"))
    assert url.startswith("https://www.facebook.com/ads/library/?")
    for part in ("country=MX", "media_type=video", "active_status=active", "search_type=keyword_unordered"):
        assert part in url
