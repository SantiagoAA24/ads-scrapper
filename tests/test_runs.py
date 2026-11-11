import time

from ads_scrapper import db, parser, runs
from ads_scrapper.scraper import ScrapeResult


def fake_scrape(ads, status="completed", calls=None):
    def scrape(options, on_ads, log=print):
        if calls is not None:
            calls.append((options.query, options.country, options.max_ads, time.monotonic()))
        seen, new = on_ads(ads) if ads else (0, 0)
        return ScrapeResult(status=status, ads_seen=seen, ads_new=new)

    return scrape


def wait_until(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_run_search_records_the_run_and_retires_missing_ads(tmp_path, payload_text):
    conn = db.connect(tmp_path / "ads.db")
    ads = parser.parse_text(payload_text).ads
    search = db.get_search(conn, db.save_search(conn, "ebook", "CO", max_ads=120))
    calls = []

    runs.run_search(conn, search, scrape_fn=fake_scrape(ads, calls=calls), log=lambda _: None)
    assert calls[0][:3] == ("ebook", "CO", 120)
    runs.run_search(conn, search, scrape_fn=fake_scrape(ads[:20]), log=lambda _: None)
    assert conn.execute("SELECT COUNT(*) FROM ads WHERE is_active = 0").fetchone()[0] == 9

    # A run cut short proves nothing about the ads it did not reach.
    runs.run_search(conn, search, scrape_fn=fake_scrape(ads[:5], status="max_ads"), log=lambda _: None)
    assert conn.execute("SELECT COUNT(*) FROM ads WHERE is_active = 0").fetchone()[0] == 9
    assert [r["status"] for r in conn.execute("SELECT status FROM scrape_runs")] == ["completed", "completed", "max_ads"]


def test_failed_scrape_is_recorded_as_error(tmp_path):
    conn = db.connect(tmp_path / "ads.db")
    search = db.get_search(conn, db.save_search(conn, "ebook", "CO"))

    def broken(options, on_ads, log=print):
        raise RuntimeError("sin navegador")

    try:
        runs.run_search(conn, search, scrape_fn=broken, log=lambda _: None)
    except RuntimeError:
        pass
    assert conn.execute("SELECT status FROM scrape_runs").fetchone()[0] == "error"


def test_runner_runs_queued_and_due_searches_one_at_a_time(tmp_path, payload_text):
    path = tmp_path / "ads.db"
    conn = db.connect(path)
    manual = db.save_search(conn, "manual", "CO")
    db.save_search(conn, "diaria", "CO", interval_hours=24)
    calls = []
    runner = runs.Runner(
        path,
        scrape_fn=fake_scrape(parser.parse_text(payload_text).ads, calls=calls),
        gap_seconds=(0.3, 0.3),
        poll_seconds=0.02,
    )
    assert runner.enqueue(manual) is True
    assert runner.enqueue(manual) is False
    runner.start()
    try:
        assert wait_until(lambda: len(calls) == 2)
        assert {c[0] for c in calls} == {"manual", "diaria"}
        assert calls[1][3] - calls[0][3] >= 0.3  # pause between searches
        time.sleep(0.5)
        assert len(calls) == 2  # the daily one is not due again
        assert runner.status() == {"current": None, "queued": [], "last_error": None}
    finally:
        runner.stop()


def test_runner_holds_scheduled_searches_after_a_block(tmp_path):
    path = tmp_path / "ads.db"
    conn = db.connect(path)
    db.save_search(conn, "diaria", "CO", interval_hours=24)
    manual = db.save_search(conn, "manual", "CO")
    db.finish_run(conn, db.start_run(conn, "x", "CO", "scrape"), "blocked")
    calls = []
    runner = runs.Runner(path, scrape_fn=fake_scrape([], calls=calls), gap_seconds=(0, 0), poll_seconds=0.02)
    runner.start()
    try:
        time.sleep(0.4)
        assert calls == []
        runner.enqueue(manual)  # asking explicitly still works
        assert wait_until(lambda: [c[0] for c in calls] == ["manual"])
    finally:
        runner.stop()


def test_runner_survives_a_failing_search(tmp_path):
    path = tmp_path / "ads.db"
    conn = db.connect(path)
    search = db.save_search(conn, "rota", "CO")

    def broken(options, on_ads, log=print):
        raise RuntimeError("sin navegador")

    runner = runs.Runner(path, scrape_fn=broken, gap_seconds=(0, 0), poll_seconds=0.02)
    runner.enqueue(search)
    runner.start()
    try:
        assert wait_until(lambda: runner.status()["last_error"] is not None)
        assert "sin navegador" in runner.status()["last_error"]
        assert runner.enqueue(search) is True
    finally:
        runner.stop()
