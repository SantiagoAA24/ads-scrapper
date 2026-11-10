"""SQLite storage: one row per ad, plus one observation per time it was seen."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

DEFAULT_DB_PATH = Path("data/ads.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS scrape_runs (
    id            INTEGER PRIMARY KEY,
    query         TEXT,
    country       TEXT,
    source        TEXT NOT NULL,
    started_at    INTEGER NOT NULL,
    finished_at   INTEGER,
    status        TEXT NOT NULL DEFAULT 'running',
    ads_seen      INTEGER NOT NULL DEFAULT 0,
    ads_new       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ads (
    ad_archive_id       TEXT PRIMARY KEY,
    collation_id        TEXT,
    collation_count     INTEGER,
    page_id             TEXT,
    page_name           TEXT,
    page_profile_uri    TEXT,
    page_like_count     INTEGER,
    page_categories     TEXT,
    body_text           TEXT,
    title               TEXT,
    caption             TEXT,
    link_description    TEXT,
    cta_text            TEXT,
    cta_type            TEXT,
    link_url            TEXT,
    link_domain         TEXT,
    display_format      TEXT,
    publisher_platforms TEXT,
    start_date          INTEGER,
    end_date            INTEGER,
    is_active           INTEGER,
    image_urls          TEXT,
    video_urls          TEXT,
    preview_image_url   TEXT,
    content_hash        TEXT,
    first_seen_at       INTEGER NOT NULL,
    last_seen_at        INTEGER NOT NULL,
    raw_json            TEXT
);
CREATE INDEX IF NOT EXISTS idx_ads_collation ON ads(collation_id);
CREATE INDEX IF NOT EXISTS idx_ads_content_hash ON ads(content_hash);
CREATE INDEX IF NOT EXISTS idx_ads_link_domain ON ads(link_domain);

CREATE TABLE IF NOT EXISTS observations (
    id              INTEGER PRIMARY KEY,
    ad_archive_id   TEXT NOT NULL REFERENCES ads(ad_archive_id),
    run_id          INTEGER NOT NULL REFERENCES scrape_runs(id),
    observed_at     INTEGER NOT NULL,
    collation_count INTEGER,
    is_active       INTEGER,
    UNIQUE (ad_archive_id, run_id)
);
"""

_JSON_COLUMNS = ("page_categories", "publisher_platforms", "image_urls", "video_urls")
_AD_COLUMNS = (
    "ad_archive_id", "collation_id", "collation_count", "page_id", "page_name",
    "page_profile_uri", "page_like_count", "page_categories", "body_text", "title",
    "caption", "link_description", "cta_text", "cta_type", "link_url", "link_domain",
    "display_format", "publisher_platforms", "start_date", "end_date", "is_active",
    "image_urls", "video_urls", "preview_image_url", "content_hash",
)


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def start_run(conn: sqlite3.Connection, query: str | None, country: str | None, source: str) -> int:
    cursor = conn.execute(
        "INSERT INTO scrape_runs (query, country, source, started_at) VALUES (?, ?, ?, ?)",
        (query, country, source, int(time.time())),
    )
    conn.commit()
    return cursor.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, status: str) -> None:
    conn.execute(
        "UPDATE scrape_runs SET finished_at = ?, status = ? WHERE id = ?",
        (int(time.time()), status, run_id),
    )
    conn.commit()


def save_ads(conn: sqlite3.Connection, run_id: int, ads: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """Upsert ads and record an observation for each. Returns (seen, new) for
    this call, counting each ad at most once per run."""
    now = int(time.time())
    seen = new = 0
    update_columns = [c for c in _AD_COLUMNS if c != "ad_archive_id"]
    # Never overwrite a known value with NULL: a later sighting can be poorer
    # (e.g. only the first ad of a group carries collation_count).
    assignments = ", ".join(f"{c} = COALESCE(excluded.{c}, ads.{c})" for c in update_columns)
    sql = (
        f"INSERT INTO ads ({', '.join(_AD_COLUMNS)}, first_seen_at, last_seen_at, raw_json) "
        f"VALUES ({', '.join('?' for _ in _AD_COLUMNS)}, ?, ?, ?) "
        f"ON CONFLICT(ad_archive_id) DO UPDATE SET {assignments}, "
        "last_seen_at = excluded.last_seen_at, raw_json = excluded.raw_json"
    )
    for ad in ads:
        ad_id = ad["ad_archive_id"]
        already_in_run = conn.execute(
            "SELECT 1 FROM observations WHERE ad_archive_id = ? AND run_id = ?", (ad_id, run_id)
        ).fetchone()
        existed = conn.execute("SELECT 1 FROM ads WHERE ad_archive_id = ?", (ad_id,)).fetchone()
        values = [_encode(column, ad.get(column)) for column in _AD_COLUMNS]
        conn.execute(sql, [*values, now, now, json.dumps(ad.get("raw"), ensure_ascii=False)])
        conn.execute(
            "INSERT INTO observations (ad_archive_id, run_id, observed_at, collation_count, is_active) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(ad_archive_id, run_id) DO UPDATE SET "
            "collation_count = COALESCE(excluded.collation_count, observations.collation_count)",
            (ad_id, run_id, now, ad.get("collation_count"), _encode("is_active", ad.get("is_active"))),
        )
        if not already_in_run:
            seen += 1
            new += 0 if existed else 1
    conn.execute(
        "UPDATE scrape_runs SET ads_seen = ads_seen + ?, ads_new = ads_new + ? WHERE id = ?",
        (seen, new, run_id),
    )
    conn.commit()
    return seen, new


def top_ads(conn: sqlite3.Connection, text: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
    """Ads ranked by how many copies are running. One row per duplicate group."""
    where, params = "", []
    if text:
        like = f"%{text}%"
        where = "WHERE body_text LIKE ? OR title LIKE ? OR page_name LIKE ? OR link_domain LIKE ?"
        params = [like] * 4
    sql = f"""
        SELECT *, MAX(COALESCE(collation_count, 1)) AS copies,
               COUNT(*) AS stored_in_group
        FROM ads {where}
        GROUP BY COALESCE(collation_id, ad_archive_id)
        ORDER BY copies DESC, start_date DESC
        LIMIT ?
    """
    return conn.execute(sql, [*params, limit]).fetchall()


def _encode(column: str, value: Any) -> Any:
    if column in _JSON_COLUMNS:
        return json.dumps(value or [], ensure_ascii=False)
    if isinstance(value, bool):
        return int(value)
    return value
