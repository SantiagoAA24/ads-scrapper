import json

from ads_scrapper import parser


def by_id(batch):
    return {ad["ad_archive_id"]: ad for ad in batch.ads}


def test_extracts_every_ad_from_real_response(payload_text):
    batch = parser.parse_text(payload_text)
    assert batch.found_connection
    assert batch.has_next_page is True
    assert len(batch.ads) == 29
    assert len(by_id(batch)) == 29


def test_basic_fields(payload_text):
    ad = by_id(parser.parse_text(payload_text))["1366790451211212"]
    assert ad["link_url"] == "https://etawaherbku.com/"
    assert ad["link_domain"] == "etawaherbku.com"
    assert ad["display_format"] == "VIDEO"
    assert ad["body_text"].startswith("HIDUP WAS-WAS")
    assert ad["video_urls"] and ad["preview_image_url"]
    assert ad["is_active"] is True
    assert ad["start_date"] == 1766995200
    assert "FACEBOOK" in ad["publisher_platforms"]


def test_group_members_share_the_collation_count(payload_text):
    ads = by_id(parser.parse_text(payload_text))
    group = ["2109857479853835", "4108211809324616", "1625056885336298"]
    assert [ads[i]["collation_count"] for i in group] == [3, 3, 3]
    assert len({ads[i]["collation_id"] for i in group}) == 1
    assert len({ads[i]["content_hash"] for i in group}) == 1


def test_catalog_ads_take_copy_from_cards_not_placeholders(payload_text):
    ad = by_id(parser.parse_text(payload_text))["1420550479410980"]
    assert "{{" not in (ad["body_text"] or "")
    assert "{{" not in (ad["title"] or "")
    assert ad["link_domain"] == "pusatdigital4u.com"
    assert ad["image_urls"]


def test_www_is_stripped_from_domain(payload_text):
    ad = by_id(parser.parse_text(payload_text))["2109857479853835"]
    assert ad["link_domain"] == "teknobooks.shop"


def test_connection_is_found_at_any_depth(payload):
    connection = payload["data"]["ad_library_main"]["search_results_connection"]
    wrapped = {"require": [["x", {"__bbox": {"result": {"data": {"other": {"search_results_connection": connection}}}}}]]}
    assert len(parser.parse_payload(wrapped).ads) == 29


def test_line_delimited_and_prefixed_bodies(payload):
    body = "for (;;);" + json.dumps(payload) + "\n" + json.dumps({"label": "deferred", "data": {}})
    assert len(parser.parse_text(body).ads) == 29


def test_unrelated_or_broken_bodies_yield_nothing():
    assert parser.parse_text('{"data": {"viewer": null}}').ads == []
    assert parser.parse_text("search_results_connection <html>not json").ads == []


def test_content_hash_ignores_case_and_spacing():
    a = parser.content_hash("Compra  YA\n\nenvío gratis")
    b = parser.content_hash("compra ya envío gratis ")
    assert a == b
    assert a != parser.content_hash("compra ya envío gratis hoy")
    assert parser.content_hash(None) is None


def test_group_without_meta_id_still_counts_as_one(payload):
    connection = payload["data"]["ad_library_main"]["search_results_connection"]
    group = next(e for e in connection["edges"] if len(e["node"]["collated_results"]) == 3)
    for result in group["node"]["collated_results"]:
        result["collation_id"] = None
    connection["edges"] = [group]
    ads = parser.parse_payload(payload).ads
    assert len({ad["collation_id"] for ad in ads}) == 1 and ads[0]["collation_id"]
