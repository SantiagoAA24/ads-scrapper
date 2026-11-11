import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ads_scrapper import db, media, parser, runs
from ads_scrapper.scraper import ScrapeResult

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _loaded(tmp_path, payload_text):
    conn = db.connect(tmp_path / "ads.db")
    run = db.start_run(conn, "ebook", "ALL", "import")
    db.save_ads(conn, run, parser.parse_text(payload_text).ads)
    return conn, run


def test_previews_are_saved_next_to_the_database(tmp_path, payload_text, monkeypatch):
    conn, _ = _loaded(tmp_path, payload_text)
    fetched = []

    def fetch(url):
        fetched.append(url)
        return PNG, "image/png"

    monkeypatch.setattr(media, "_fetch", fetch)
    assert media.download_previews(conn, log=lambda _: None) == (29, 0)

    row = conn.execute("SELECT preview_file FROM ads WHERE ad_archive_id = '1366790451211212'").fetchone()
    assert row["preview_file"] == "1366790451211212.png"
    assert (tmp_path / "media" / "1366790451211212.png").read_bytes() == PNG
    assert all(".fbcdn.net/" in url for url in fetched)

    fetched.clear()
    assert media.download_previews(conn, log=lambda _: None) == (0, 0)  # nothing left to do
    assert fetched == []


def test_expired_or_invalid_images_are_counted_and_retried_later(tmp_path, payload_text, monkeypatch):
    conn, _ = _loaded(tmp_path, payload_text)
    answers = iter([(b"<html>", "text/html"), (b"", "image/jpeg")])

    def fetch(url):
        try:
            return next(answers)
        except StopIteration:
            raise OSError("403")

    monkeypatch.setattr(media, "_fetch", fetch)
    assert media.download_previews(conn, log=lambda _: None) == (0, 29)
    assert not list((tmp_path / "media").iterdir())
    monkeypatch.setattr(media, "_fetch", lambda url: (PNG, "image/jpeg; charset=binary"))
    assert media.download_previews(conn, log=lambda _: None) == (29, 0)


def test_only_metas_cdn_is_contacted(tmp_path, payload_text, monkeypatch):
    conn = db.connect(tmp_path / "ads.db")
    ads = parser.parse_text(payload_text).ads[:4]
    urls = ["http://scontent.fbcdn.net/a.jpg", "https://evil.example/a.jpg", "https://fbcdn.net.evil.example/a.jpg", "file:///etc/passwd"]
    for ad, url in zip(ads, urls):
        ad["preview_image_url"] = url
    db.save_ads(conn, db.start_run(conn, None, None, "import"), ads)
    monkeypatch.setattr(media, "_fetch", lambda url: (_ for _ in ()).throw(AssertionError(url)))
    assert media.download_previews(conn, log=lambda _: None) == (0, 0)


def test_run_downloads_only_its_own_previews_and_survives_failures(tmp_path, payload_text, monkeypatch):
    conn, _ = _loaded(tmp_path, payload_text)
    ads = parser.parse_text(payload_text).ads
    search = db.get_search(conn, db.save_search(conn, "ebook", "CO"))

    def scrape(options, on_ads, log=print):
        seen, new = on_ads(ads[:5])
        return ScrapeResult("max_ads", seen, new)

    monkeypatch.setattr(media, "_fetch", lambda url: (PNG, "image/png"))
    runs.run_search(conn, search, scrape_fn=scrape, log=lambda _: None)
    assert conn.execute("SELECT COUNT(*) FROM ads WHERE preview_file IS NOT NULL").fetchone()[0] == 5

    def broken(conn, run_id, log):
        raise RuntimeError("disco lleno")

    messages = []
    result = runs.run_search(conn, search, scrape_fn=scrape, log=messages.append, download_previews=broken)
    assert result.status == "max_ads" and "disco lleno" in messages[-1]


def test_rescrape_keeps_the_downloaded_preview(tmp_path, payload_text, monkeypatch):
    conn, _ = _loaded(tmp_path, payload_text)
    monkeypatch.setattr(media, "_fetch", lambda url: (PNG, "image/png"))
    media.download_previews(conn, log=lambda _: None)
    db.save_ads(conn, db.start_run(conn, None, None, "import"), parser.parse_text(payload_text).ads)
    assert conn.execute("SELECT COUNT(*) FROM ads WHERE preview_file IS NULL").fetchone()[0] == 0


def test_fetch_reads_a_real_http_response(monkeypatch):
    monkeypatch.undo()  # use the real fetcher against a local server

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        assert media._fetch(f"http://127.0.0.1:{server.server_port}/x.png") == (PNG, "image/png")
    finally:
        server.shutdown()
