"""SQLite storage: one row per ad, plus one observation per time it was seen."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .parser import content_hash

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

# Full-text index kept in sync by triggers. Diacritics are ignored so that
# "termica" finds "térmica".
FTS_SCHEMA = """
CREATE VIRTUAL TABLE ads_fts USING fts5(
    body_text, title, caption, page_name, link_domain,
    content='ads', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER ads_fts_insert AFTER INSERT ON ads BEGIN
    INSERT INTO ads_fts (rowid, body_text, title, caption, page_name, link_domain)
    VALUES (new.rowid, new.body_text, new.title, new.caption, new.page_name, new.link_domain);
END;
CREATE TRIGGER ads_fts_delete AFTER DELETE ON ads BEGIN
    INSERT INTO ads_fts (ads_fts, rowid, body_text, title, caption, page_name, link_domain)
    VALUES ('delete', old.rowid, old.body_text, old.title, old.caption, old.page_name, old.link_domain);
END;
CREATE TRIGGER ads_fts_update AFTER UPDATE ON ads BEGIN
    INSERT INTO ads_fts (ads_fts, rowid, body_text, title, caption, page_name, link_domain)
    VALUES ('delete', old.rowid, old.body_text, old.title, old.caption, old.page_name, old.link_domain);
    INSERT INTO ads_fts (rowid, body_text, title, caption, page_name, link_domain)
    VALUES (new.rowid, new.body_text, new.title, new.caption, new.page_name, new.link_domain);
END;
"""

# Ads with the same copy are one group, whatever page, creative or
# destination they use. Meta's own grouping (collation) is narrower: it splits the
# same text into one collation per creative.
GROUP_KEY = "COALESCE(a.content_hash, 'c' || a.collation_id, 'a' || a.ad_archive_id)"
SORTS = {
    "copies": "g.copies DESC, s.last_start DESC",
    "recent": "s.last_start DESC, g.copies DESC",
    "oldest": "s.first_start ASC, g.copies DESC",
}

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
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'ads_fts'").fetchone():
        conn.executescript(FTS_SCHEMA)
        # Index ads stored before the search index existed.
        conn.execute("INSERT INTO ads_fts (ads_fts) VALUES ('rebuild')")
        conn.commit()
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        # The fingerprint used to include the destination domain.
        for row in conn.execute("SELECT ad_archive_id, body_text FROM ads").fetchall():
            conn.execute(
                "UPDATE ads SET content_hash = ? WHERE ad_archive_id = ?",
                (content_hash(row["body_text"]), row["ad_archive_id"]),
            )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()


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


def search_groups(
    conn: sqlite3.Connection,
    text: str | None = None,
    *,
    search: tuple[str | None, str | None] | None = None,
    display_format: str | None = None,
    active_only: bool = False,
    max_age_days: int | None = None,
    min_copies: int = 1,
    sort: str = "copies",
    limit: int = 20,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Duplicate groups matching the filters, one representative ad per group.

    ``copies`` adds up Meta's count for every creative in the group; ``search``
    is a (query, country) pair restricting to ads seen in those scrapes.
    """
    conditions, params = ["1 = 1"], []
    match = _fts_query(text)
    if match:
        conditions.append("a.rowid IN (SELECT rowid FROM ads_fts WHERE ads_fts MATCH ?)")
        params.append(match)
    elif text and text.strip():
        return []  # only punctuation: nothing can match
    if search:
        conditions.append(
            "a.ad_archive_id IN (SELECT o.ad_archive_id FROM observations o "
            "JOIN scrape_runs r ON r.id = o.run_id WHERE r.query IS ? AND r.country IS ?)"
        )
        params.extend(search)
    if display_format:
        conditions.append("a.display_format = ?")
        params.append(display_format)
    if active_only:
        conditions.append("a.is_active = 1")
    if max_age_days:
        conditions.append("a.start_date >= ?")
        params.append(int(time.time()) - max_age_days * 86400)

    sql = f"""
        WITH f AS (
            SELECT a.*, {GROUP_KEY} AS group_key,
                   COALESCE(a.collation_id, a.ad_archive_id) AS collation_key
            FROM ads a WHERE {' AND '.join(conditions)}
        ),
        collations AS (
            SELECT group_key, MAX(COALESCE(collation_count, 1)) AS copies
            FROM f GROUP BY group_key, collation_key
        ),
        g AS (
            SELECT group_key, SUM(copies) AS copies, COUNT(*) AS creatives
            FROM collations GROUP BY group_key
        ),
        s AS (
            SELECT group_key, COUNT(*) AS stored, COUNT(DISTINCT page_id) AS pages,
                   MIN(start_date) AS first_start, MAX(start_date) AS last_start
            FROM f GROUP BY group_key
        ),
        ranked AS (
            SELECT f.*, ROW_NUMBER() OVER (
                PARTITION BY group_key
                ORDER BY COALESCE(collation_count, 1) DESC, start_date DESC
            ) AS position
            FROM f
        )
        SELECT ranked.*, g.copies, g.creatives, s.stored, s.pages, s.first_start, s.last_start
        FROM ranked JOIN g USING (group_key) JOIN s USING (group_key)
        WHERE ranked.position = 1 AND g.copies >= ?
        ORDER BY {SORTS.get(sort, SORTS['copies'])}
        LIMIT ? OFFSET ?
    """
    return conn.execute(sql, [*params, min_copies, limit, offset]).fetchall()


def group_ads(conn: sqlite3.Connection, group_key: str) -> list[sqlite3.Row]:
    """Every stored ad of one duplicate group, newest first."""
    return conn.execute(
        f"SELECT a.* FROM ads a WHERE {GROUP_KEY} = ? ORDER BY a.start_date DESC, a.ad_archive_id",
        (group_key,),
    ).fetchall()


def list_searches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Distinct (query, country) pairs that have been scraped or imported."""
    return conn.execute(
        "SELECT query, country, MAX(started_at) AS last_run FROM scrape_runs "
        "WHERE query IS NOT NULL AND ads_seen > 0 GROUP BY query, country ORDER BY last_run DESC"
    ).fetchall()


def list_formats(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT display_format FROM ads WHERE display_format IS NOT NULL ORDER BY 1"
    )
    return [row[0] for row in rows]


def _fts_query(text: str | None) -> str | None:
    """All words must appear; each one matches as a prefix."""
    words = re.findall(r"\w+", text or "")
    return " ".join(f'"{word}"*' for word in words) or None


def _encode(column: str, value: Any) -> Any:
    if column in _JSON_COLUMNS:
        return json.dumps(value or [], ensure_ascii=False)
    if isinstance(value, bool):
        return int(value)
    return value
