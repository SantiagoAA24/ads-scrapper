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


def test_top_ranks_by_copies_one_row_per_group(tmp_path, payload_text):
    conn = db.connect(tmp_path / "ads.db")
    db.save_ads(conn, db.start_run(conn, None, None, "import"), parser.parse_text(payload_text).ads)

    rows = db.top_ads(conn, limit=50)
    copies = [r["copies"] for r in rows]
    assert copies == sorted(copies, reverse=True)
    assert copies[0] == 3
    assert len(rows) == 25  # 29 ads in 25 duplicate groups

    filtered = db.top_ads(conn, "teknobooks")
    assert len(filtered) == 1 and filtered[0]["stored_in_group"] == 3
