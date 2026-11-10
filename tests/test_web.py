import pytest

from ads_scrapper import db, parser
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
