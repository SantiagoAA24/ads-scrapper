"""SQLite storage: one row per ad, plus one observation per time it was seen."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .parser import content_hash

DEFAULT_DB_PATH = Path("data/ads.db")
_SETUP_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id             INTEGER PRIMARY KEY,
    query          TEXT NOT NULL,
    country        TEXT NOT NULL DEFAULT 'ALL',
    active_status  TEXT NOT NULL DEFAULT 'active',
    media_type     TEXT NOT NULL DEFAULT 'all',
    max_ads        INTEGER NOT NULL DEFAULT 300,
    -- NULL: only runs when asked.
    interval_hours INTEGER,
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     INTEGER NOT NULL,
    last_run_at    INTEGER,
    UNIQUE (query, country, active_status, media_type)
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id            INTEGER PRIMARY KEY,
    search_id     INTEGER REFERENCES searches(id) ON DELETE SET NULL,
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
    -- File name inside the media folder once the preview is downloaded.
    preview_file        TEXT,
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

-- Favourites are kept per duplicate group, not per ad.
CREATE TABLE IF NOT EXISTS saved_groups (
    group_key TEXT PRIMARY KEY,
    saved_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS group_tags (
    group_key TEXT NOT NULL REFERENCES saved_groups(group_key) ON DELETE CASCADE,
    tag       TEXT NOT NULL,
    PRIMARY KEY (group_key, tag)
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
# Trend = copies now, plus double weight for copies gained in this window.
TREND_DAYS = 7
BLOCK_COOLDOWN_HOURS = 24
SORTS = {
    "trend": "(g.copies + 2 * g.growth) DESC, g.copies DESC, s.last_start DESC",
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
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # The web UI reads while a scrape writes from another thread.
    conn.execute("PRAGMA journal_mode = WAL")
    # The web app opens connections from several threads; only one of them
    # may create or upgrade the schema.
    with _SETUP_LOCK:
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
    if version < 2:
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(scrape_runs)")]
        if "search_id" not in columns:
            conn.execute("ALTER TABLE scrape_runs ADD COLUMN search_id INTEGER REFERENCES searches(id) ON DELETE SET NULL")
        # Turn searches already run from the terminal into saved searches.
        for row in conn.execute(
            "SELECT query, country, MAX(started_at) AS last FROM scrape_runs "
            "WHERE source = 'scrape' AND query IS NOT NULL GROUP BY query, country"
        ).fetchall():
            search_id = save_search(conn, row["query"], row["country"] or "ALL")
            conn.execute("UPDATE searches SET last_run_at = ? WHERE id = ?", (row["last"], search_id))
            conn.execute(
                "UPDATE scrape_runs SET search_id = ? WHERE source = 'scrape' AND query IS ? AND country IS ?",
                (search_id, row["query"], row["country"]),
            )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    if version < 3:
        if "preview_file" not in [row["name"] for row in conn.execute("PRAGMA table_info(ads)")]:
            conn.execute("ALTER TABLE ads ADD COLUMN preview_file TEXT")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()


def save_search(
    conn: sqlite3.Connection,
    query: str,
    country: str = "ALL",
    active_status: str = "active",
    media_type: str = "all",
    max_ads: int | None = None,
    interval_hours: int | None = None,
    update: bool = False,
) -> int:
    """Create a saved search or return the existing one with the same filters.

    With ``update``, an existing search also takes the given limit and schedule.
    """
    query, country = query.strip(), (country or "ALL").strip().upper()
    key = (query, country, active_status, media_type)
    row = conn.execute(
        "SELECT id FROM searches WHERE query = ? AND country = ? AND active_status = ? AND media_type = ?",
        key,
    ).fetchone()
    if row:
        if update:
            conn.execute(
                "UPDATE searches SET max_ads = COALESCE(?, max_ads), interval_hours = ? WHERE id = ?",
                (max_ads, interval_hours, row["id"]),
            )
        elif max_ads:
            conn.execute("UPDATE searches SET max_ads = ? WHERE id = ?", (max_ads, row["id"]))
        conn.commit()
        return row["id"]
    cursor = conn.execute(
        "INSERT INTO searches (query, country, active_status, media_type, max_ads, interval_hours, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (*key, max_ads or 300, interval_hours, int(time.time())),
    )
    conn.commit()
    return cursor.lastrowid


def get_search(conn: sqlite3.Connection, search_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM searches WHERE id = ?", (search_id,)).fetchone()


def saved_searches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Saved searches with their latest run and when the next one is due."""
    return conn.execute(
        """
        SELECT s.*, r.status AS last_status, r.ads_seen AS last_ads_seen, r.ads_new AS last_ads_new,
               CASE WHEN s.interval_hours IS NOT NULL AND s.enabled = 1
                    THEN COALESCE(s.last_run_at + s.interval_hours * 3600, 0) END AS next_run_at
        FROM searches s
        LEFT JOIN scrape_runs r ON r.id = (
            SELECT id FROM scrape_runs WHERE search_id = s.id ORDER BY id DESC LIMIT 1
        )
        ORDER BY s.query COLLATE NOCASE, s.country
        """
    ).fetchall()


def due_searches(conn: sqlite3.Connection, now: int | None = None) -> list[sqlite3.Row]:
    """Scheduled searches whose interval has elapsed, longest overdue first."""
    now = int(time.time()) if now is None else now
    return conn.execute(
        "SELECT * FROM searches WHERE enabled = 1 AND interval_hours IS NOT NULL "
        "AND COALESCE(last_run_at, 0) + interval_hours * 3600 <= ? ORDER BY COALESCE(last_run_at, 0)",
        (now,),
    ).fetchall()


def set_search_enabled(conn: sqlite3.Connection, search_id: int, enabled: bool) -> None:
    conn.execute("UPDATE searches SET enabled = ? WHERE id = ?", (int(enabled), search_id))
    conn.commit()


def delete_search(conn: sqlite3.Connection, search_id: int) -> None:
    """Forget a saved search. The ads it found stay."""
    conn.execute("DELETE FROM searches WHERE id = ?", (search_id,))
    conn.commit()


def blocked_until(conn: sqlite3.Connection, now: int | None = None) -> int | None:
    """If Meta blocked a recent run, when automatic runs may resume."""
    now = int(time.time()) if now is None else now
    row = conn.execute("SELECT MAX(started_at) FROM scrape_runs WHERE status = 'blocked'").fetchone()
    until = (row[0] or 0) + BLOCK_COOLDOWN_HOURS * 3600
    return until if row[0] and until > now else None


def mark_missing_inactive(conn: sqlite3.Connection, search_id: int, run_id: int) -> int:
    """After a run that listed every active result of a search, ads that this
    search used to return and no longer does have stopped running."""
    cursor = conn.execute(
        """
        UPDATE ads SET is_active = 0
        WHERE is_active = 1
          AND ad_archive_id IN (
              SELECT o.ad_archive_id FROM observations o
              JOIN scrape_runs r ON r.id = o.run_id WHERE r.search_id = ?)
          AND ad_archive_id NOT IN (SELECT ad_archive_id FROM observations WHERE run_id = ?)
        """,
        (search_id, run_id),
    )
    conn.commit()
    return cursor.rowcount


def start_run(
    conn: sqlite3.Connection,
    query: str | None,
    country: str | None,
    source: str,
    search_id: int | None = None,
) -> int:
    now = int(time.time())
    cursor = conn.execute(
        "INSERT INTO scrape_runs (query, country, source, started_at, search_id) VALUES (?, ?, ?, ?, ?)",
        (query, country, source, now, search_id),
    )
    if search_id is not None:
        conn.execute("UPDATE searches SET last_run_at = ? WHERE id = ?", (now, search_id))
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
    min_copies: int = 0,
    saved_only: bool = False,
    tag: str | None = None,
    sort: str = "trend",
    limit: int = 20,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Duplicate groups matching the filters, one representative ad per group.

    ``copies`` adds up Meta's count for every creative still running in the
    group. ``growth`` is how many of those were gained in the last TREND_DAYS:
    against the count observed back then, or counting a creative as new when
    it started inside the window. ``search`` is a (query, country) pair
    restricting to ads seen in those scrapes.
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
    cutoff = int(time.time()) - TREND_DAYS * 86400
    group_conditions, group_params = "", []
    if saved_only:
        group_conditions += " AND ranked.group_key IN (SELECT group_key FROM saved_groups)"
    if tag:
        group_conditions += " AND ranked.group_key IN (SELECT group_key FROM group_tags WHERE tag = ?)"
        group_params.append(normalize_tag(tag))

    sql = f"""
        WITH f AS (
            SELECT a.*, {GROUP_KEY} AS group_key,
                   COALESCE(a.collation_id, a.ad_archive_id) AS collation_key
            FROM ads a WHERE {' AND '.join(conditions)}
        ),
        collations AS (
            SELECT group_key, collation_key, MIN(start_date) AS started,
                   MAX(CASE WHEN is_active = 0 THEN 0 ELSE COALESCE(collation_count, 1) END) AS copies
            FROM f GROUP BY group_key, collation_key
        ),
        history AS (
            SELECT c.*, (
                SELECT o.collation_count FROM observations o
                JOIN ads past ON past.ad_archive_id = o.ad_archive_id
                WHERE (past.collation_id = c.collation_key OR past.ad_archive_id = c.collation_key)
                  AND o.observed_at <= ? AND o.collation_count IS NOT NULL
                ORDER BY o.observed_at DESC LIMIT 1
            ) AS copies_before
            FROM collations c
        ),
        g AS (
            SELECT group_key, SUM(copies) AS copies, COUNT(*) AS creatives,
                   SUM(copies - CASE
                       WHEN copies_before IS NOT NULL THEN copies_before
                       WHEN started > ? THEN 0
                       ELSE copies END) AS growth
            FROM history GROUP BY group_key
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
        SELECT ranked.*, g.copies, g.creatives, g.growth, s.stored, s.pages, s.first_start, s.last_start,
               EXISTS (SELECT 1 FROM saved_groups sg WHERE sg.group_key = ranked.group_key) AS saved,
               (SELECT GROUP_CONCAT(tag) FROM group_tags gt WHERE gt.group_key = ranked.group_key) AS tags
        FROM ranked JOIN g USING (group_key) JOIN s USING (group_key)
        WHERE ranked.position = 1 AND g.copies >= ?{group_conditions}
        ORDER BY {SORTS.get(sort, SORTS['trend'])}
        LIMIT ? OFFSET ?
    """
    return conn.execute(sql, [*params, cutoff, cutoff, min_copies, *group_params, limit, offset]).fetchall()


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


def normalize_tag(tag: str) -> str:
    """Tags are case-insensitive, single-spaced and free of commas."""
    return " ".join(tag.replace(",", " ").casefold().split())[:40]


def save_group(conn: sqlite3.Connection, group_key: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO saved_groups (group_key, saved_at) VALUES (?, ?)", (group_key, int(time.time()))
    )
    conn.commit()


def unsave_group(conn: sqlite3.Connection, group_key: str) -> None:
    """Remove a favourite along with its tags."""
    conn.execute("DELETE FROM saved_groups WHERE group_key = ?", (group_key,))
    conn.commit()


def add_tag(conn: sqlite3.Connection, group_key: str, tag: str) -> None:
    """Tag a group; tagging also saves it."""
    tag = normalize_tag(tag)
    if not tag:
        return
    save_group(conn, group_key)
    conn.execute("INSERT OR IGNORE INTO group_tags (group_key, tag) VALUES (?, ?)", (group_key, tag))
    conn.commit()


def remove_tag(conn: sqlite3.Connection, group_key: str, tag: str) -> None:
    conn.execute("DELETE FROM group_tags WHERE group_key = ? AND tag = ?", (group_key, normalize_tag(tag)))
    conn.commit()


def group_state(conn: sqlite3.Connection, group_key: str) -> tuple[bool, list[str]]:
    """Whether a group is saved, and its tags."""
    saved = conn.execute("SELECT 1 FROM saved_groups WHERE group_key = ?", (group_key,)).fetchone()
    tags = conn.execute("SELECT tag FROM group_tags WHERE group_key = ? ORDER BY tag", (group_key,))
    return bool(saved), [row[0] for row in tags]


def list_tags(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute("SELECT DISTINCT tag FROM group_tags ORDER BY tag")]


def media_dir(conn: sqlite3.Connection) -> Path:
    """Folder for downloaded previews, next to the database file."""
    database_file = conn.execute("PRAGMA database_list").fetchone()["file"]
    return Path(database_file).parent / "media"


def ads_missing_previews(conn: sqlite3.Connection, run_id: int | None = None) -> list[sqlite3.Row]:
    """Ads with a preview URL not downloaded yet, optionally only those of a run."""
    sql = "SELECT ad_archive_id, preview_image_url FROM ads WHERE preview_file IS NULL AND preview_image_url IS NOT NULL"
    params: list[Any] = []
    if run_id is not None:
        sql += " AND ad_archive_id IN (SELECT ad_archive_id FROM observations WHERE run_id = ?)"
        params.append(run_id)
    return conn.execute(sql, params).fetchall()


def set_preview_file(conn: sqlite3.Connection, ad_archive_id: str, file_name: str) -> None:
    conn.execute("UPDATE ads SET preview_file = ? WHERE ad_archive_id = ?", (file_name, ad_archive_id))


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
