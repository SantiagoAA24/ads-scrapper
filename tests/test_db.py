import copy

from ads_scrapper import db, parser


def test_reimport_updates_instead_of_duplicating(tmp_path, payload_text):
    conn = db.connect(tmp_path / "ads.db")
    ads = parser.parse_text(payload_text).ads

    first = db.start_run(conn, "ebook", "ALL", "import")
    assert db.save_ads(conn, first, ads) == (29, 29)
    second = db.start_run(conn, "ebook", "ALL", "import")
    assert db.save_ads(conn, second, ads) == (29, 0)

    assert conn.execute("SELECT COUNT(*) FROM ads").fetchone()[0] == 29
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 58
    run = conn.execute("SELECT * FROM scrape_runs WHERE id = ?", (second,)).fetchone()
    assert (run["ads_seen"], run["ads_new"]) == (29, 0)


def test_same_ad_twice_in_one_run_counts_once(tmp_path, payload_text):
    conn = db.connect(tmp_path / "ads.db")
    ads = parser.parse_text(payload_text).ads
    run = db.start_run(conn, None, None, "import")
    db.save_ads(conn, run, ads[:10])
    assert db.save_ads(conn, run, ads[5:15]) == (5, 5)
    assert conn.execute("SELECT ads_seen FROM scrape_runs").fetchone()[0] == 15


def test_history_tracks_growth_and_keeps_known_values(tmp_path, payload_text):
    conn = db.connect(tmp_path / "ads.db")
    ad = next(a for a in parser.parse_text(payload_text).ads if a["ad_archive_id"] == "2109857479853835")

    db.save_ads(conn, db.start_run(conn, None, None, "import"), [ad])
    grown = copy.deepcopy(ad)
    grown["collation_count"] = 9
    db.save_ads(conn, db.start_run(conn, None, None, "import"), [grown])
    poorer = copy.deepcopy(ad)
    poorer["collation_count"] = None
    poorer["link_url"] = None
    db.save_ads(conn, db.start_run(conn, None, None, "import"), [poorer])

    row = conn.execute("SELECT * FROM ads").fetchone()
    assert row["collation_count"] == 9
    assert row["link_url"] == ad["link_url"]
    history = [r[0] for r in conn.execute("SELECT collation_count FROM observations ORDER BY id")]
    assert history == [3, 9, None]


def _loaded(tmp_path, payload_text):
    conn = db.connect(tmp_path / "ads.db")
    run = db.start_run(conn, "ebook", "ALL", "import")
    db.save_ads(conn, run, parser.parse_text(payload_text).ads)
    return conn


def test_groups_rank_by_copies_one_row_per_group(tmp_path, payload_text):
    conn = _loaded(tmp_path, payload_text)

    rows = db.search_groups(conn, limit=50)
    copies = [r["copies"] for r in rows]
    assert copies == sorted(copies, reverse=True)
    assert copies[0] == 3
    assert sum(r["stored"] for r in rows) == 29
    assert len({r["group_key"] for r in rows}) == len(rows)

    filtered = db.search_groups(conn, "teknobooks")
    assert len(filtered) == 1 and filtered[0]["stored"] == 3 and filtered[0]["copies"] == 3
    assert len(db.group_ads(conn, filtered[0]["group_key"])) == 3


def test_same_copy_in_several_creatives_is_one_group_with_summed_copies(tmp_path, payload_text):
    """What Meta shows as separate x3, x1, x1 rows is one ad running 5 times."""
    conn = db.connect(tmp_path / "ads.db")
    base = next(a for a in parser.parse_text(payload_text).ads if a["ad_archive_id"] == "1366790451211212")
    ads = []
    for ad_id, collation, count in [("1", "A", 3), ("2", "A", None), ("3", "B", 1), ("4", "C", 1)]:
        ads.append({**base, "ad_archive_id": ad_id, "collation_id": collation, "collation_count": count})
    db.save_ads(conn, db.start_run(conn, None, None, "import"), ads)

    (row,) = db.search_groups(conn)
    assert (row["copies"], row["creatives"], row["stored"]) == (5, 3, 4)
    assert row["ad_archive_id"] == "1"  # representative: the most duplicated creative


def test_text_search_ignores_accents_and_matches_prefixes(tmp_path, payload_text):
    conn = _loaded(tmp_path, payload_text)
    assert len(db.search_groups(conn, "drenagem inchaco")) == 1  # stored as "inchaço"
    assert len(db.search_groups(conn, "drena")) == 1
    assert db.search_groups(conn, "drenagem zzzz") == []
    assert db.search_groups(conn, '"') == []
    assert db.search_groups(conn, "AND OR (") is not None  # operators are plain words


def test_filters(tmp_path, payload_text):
    conn = _loaded(tmp_path, payload_text)
    assert all(r["display_format"] == "VIDEO" for r in db.search_groups(conn, display_format="VIDEO", limit=50))
    assert all(r["copies"] >= 2 for r in db.search_groups(conn, min_copies=2, limit=50))
    assert len(db.search_groups(conn, min_copies=2, limit=50)) == 6
    assert db.search_groups(conn, max_age_days=1) == []  # fixture ads started in 2025
    assert len(db.search_groups(conn, search=("ebook", "ALL"), limit=50)) == 25
    assert db.search_groups(conn, search=("otra", "CO")) == []
    assert [(r["query"], r["country"]) for r in db.list_searches(conn)] == [("ebook", "ALL")]


def test_search_index_is_built_for_databases_created_before_it(tmp_path, payload_text):
    conn = _loaded(tmp_path, payload_text)
    conn.executescript(
        "DROP TRIGGER ads_fts_insert; DROP TRIGGER ads_fts_update; DROP TRIGGER ads_fts_delete; DROP TABLE ads_fts;"
    )
    conn.close()
    assert len(db.search_groups(db.connect(tmp_path / "ads.db"), "teknobooks")) == 1


def test_old_fingerprints_are_recomputed(tmp_path, payload_text):
    conn = _loaded(tmp_path, payload_text)
    conn.execute("UPDATE ads SET content_hash = 'stale'")
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    conn = db.connect(tmp_path / "ads.db")
    assert len(db.search_groups(conn, limit=50)) == 25
