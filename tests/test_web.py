import pytest

from ads_scrapper import db, parser, runs
from ads_scrapper.scraper import ScrapeResult
from ads_scrapper.web import create_app

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path, payload_text):
    path = tmp_path / "ads.db"
    conn = db.connect(path)
    db.save_ads(conn, db.start_run(conn, "ebook", "ALL", "import"), parser.parse_text(payload_text).ads)
    conn.close()
    return TestClient(create_app(path))


@pytest.fixture()
def app_with_runner(tmp_path, payload_text):
    """App whose searches 'scrape' the fixture instead of opening a browser."""
    ads = parser.parse_text(payload_text).ads
    calls = []

    def scrape(options, on_ads, log=print):
        calls.append(options.query)
        seen, new = on_ads(ads)
        return ScrapeResult(status="completed", ads_seen=seen, ads_new=new)

    path = tmp_path / "ads.db"
    runner = runs.Runner(path, scrape_fn=scrape, gap_seconds=(0, 0), poll_seconds=0.02)
    with TestClient(create_app(path, runner)) as test_client:
        yield test_client, calls, path


def _wait(condition, timeout=5):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not condition():
        time.sleep(0.02)
    return condition()


def test_index_lists_groups_with_direct_links(client):
    html = client.get("/").text
    assert html.count('<article class="card">') == 25
    assert "https://www.facebook.com/ads/library/?id=2109857479853835" in html
    assert "ebook · ALL" in html


def test_search_and_filters_narrow_results(client):
    assert client.get("/", params={"q": "teknobooks"}).text.count('<article class="card">') == 1
    assert client.get("/", params={"min_copies": "2"}).text.count('<article class="card">') == 6
    assert client.get("/", params={"search": "ebook|ALL", "format": "VIDEO"}).status_code == 200
    empty = client.get("/", params={"q": "nadaquecoincida"}).text
    assert "Ningún anuncio coincide" in empty and "Quitar filtros" in empty


def test_bad_parameters_do_not_break_the_page(client):
    response = client.get("/", params={"min_copies": "abc", "page": "-3", "age": "x", "sort": "zzz", "search": "no|existe"})
    assert response.status_code == 200
    assert response.text.count('<article class="card">') == 25


def test_ad_text_is_escaped(tmp_path):
    path = tmp_path / "ads.db"
    conn = db.connect(path)
    ad = parser.normalize_ad({"ad_archive_id": "9", "snapshot": {"body": {"text": "<script>alert(1)</script>"}}})
    db.save_ads(conn, db.start_run(conn, None, None, "import"), [ad])
    conn.close()
    html = TestClient(create_app(path)).get("/").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_group_page_shows_every_stored_copy(client):
    index = client.get("/", params={"q": "teknobooks"}).text
    link = index.split('href="/group/')[1].split('"')[0]
    html = client.get("/group/" + link).text
    assert html.count('<article class="card">') == 3
    assert client.get("/group/noexiste").status_code == 404


def test_empty_database_explains_how_to_start(tmp_path):
    html = TestClient(create_app(tmp_path / "empty.db")).get("/").text
    assert "Todavía no hay anuncios guardados" in html


def test_new_search_from_the_page_runs_and_fills_the_results(app_with_runner):
    client, calls, _ = app_with_runner
    response = client.post(
        "/searches",
        data={"query": "ebook", "country": "co", "max_ads": "100", "interval_hours": "24", "run_now": "1"},
    )
    assert response.status_code == 200 and "ebook" in response.text  # redirected to the list
    assert _wait(lambda: client.get("/api/status").json() == {"current": None, "queued": [], "last_error": None} and calls)
    assert calls == ["ebook"]

    page = client.get("/searches").text
    assert "Cada día" in page and "completa · 29 vistos, 29 nuevos" in page
    assert client.get("/", params={"search": "ebook|CO"}).text.count('<article class="card">') == 25


def test_saving_without_running_does_not_scrape(app_with_runner):
    client, calls, path = app_with_runner
    client.post("/searches", data={"query": "faja", "country": "CO", "interval_hours": "0"})
    assert calls == []
    (search,) = db.saved_searches(db.connect(path))
    assert (search["query"], search["interval_hours"], search["next_run_at"]) == ("faja", None, None)


def test_invalid_search_is_rejected_with_a_reason(app_with_runner):
    client, _, path = app_with_runner
    assert "Escribe una palabra clave" in client.post("/searches", data={"query": "  "}).text
    assert "código de dos letras" in client.post("/searches", data={"query": "faja", "country": "Colombia"}).text
    assert db.saved_searches(db.connect(path)) == []


def test_pause_resume_run_and_delete(app_with_runner):
    client, calls, path = app_with_runner
    client.post("/searches", data={"query": "faja", "country": "CO", "interval_hours": "0"})
    search_id = db.saved_searches(db.connect(path))[0]["id"]

    client.post(f"/searches/{search_id}/pause")
    assert db.get_search(db.connect(path), search_id)["enabled"] == 0
    client.post(f"/searches/{search_id}/resume")
    assert db.get_search(db.connect(path), search_id)["enabled"] == 1
    client.post(f"/searches/{search_id}/run")
    assert _wait(lambda: calls == ["faja"])
    assert _wait(lambda: client.get("/api/status").json()["current"] is None)

    client.post(f"/searches/{search_id}/delete")
    conn = db.connect(path)
    assert db.saved_searches(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM ads").fetchone()[0] == 29  # ads are kept
    assert client.post(f"/searches/{search_id}/run").status_code == 404


def test_other_sites_cannot_trigger_searches(app_with_runner):
    client, calls, path = app_with_runner
    response = client.post(
        "/searches", data={"query": "faja", "run_now": "1"}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    assert calls == [] and db.saved_searches(db.connect(path)) == []
    same = client.post("/searches", data={"query": "faja"}, headers={"Origin": "http://testserver"})
    assert same.status_code == 200


def test_pages_do_not_set_a_document_wide_no_referrer_policy(app_with_runner):
    """With it, browsers send 'Origin: null' on form posts and the same-origin
    check would reject the app's own forms."""
    client, _, _ = app_with_runner
    assert 'name="referrer"' not in client.get("/searches").text
    assert client.post("/searches", data={"query": "faja"}, headers={"Origin": "null"}).status_code == 403
