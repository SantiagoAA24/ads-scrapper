"""Keeps a local copy of each ad's preview image.

Meta's image URLs are signed and stop working after a few days, so previews
are downloaded right after a scrape, while the URLs are fresh. Videos are not
downloaded: only their still frame.
"""

from __future__ import annotations

import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable
from urllib.parse import urlsplit

from . import db

# Imported files can contain any URL; only Meta's CDN is ever contacted.
ALLOWED_HOST_SUFFIX = ".fbcdn.net"
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
MAX_BYTES = 8 * 1024 * 1024
TIMEOUT_SECONDS = 15
WORKERS = 4


def download_previews(conn, run_id: int | None = None, log: Callable[[str], None] = print) -> tuple[int, int]:
    """Download missing previews (for one run, or all). Returns (saved, failed)."""
    pending = [
        (row["ad_archive_id"], row["preview_image_url"])
        for row in db.ads_missing_previews(conn, run_id)
        if row["ad_archive_id"].isdigit() and _allowed(row["preview_image_url"])
    ]
    if not pending:
        return 0, 0
    folder = db.media_dir(conn)
    folder.mkdir(parents=True, exist_ok=True)
    saved = failed = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for (ad_id, _), image in zip(pending, pool.map(_try_fetch, [url for _, url in pending])):
            if image is None:
                failed += 1
                continue
            data, extension = image
            file_name = f"{ad_id}{extension}"
            (folder / file_name).write_bytes(data)
            db.set_preview_file(conn, ad_id, file_name)
            saved += 1
    conn.commit()
    log(f"Vistas previas: {saved} guardadas" + (f", {failed} no disponibles" if failed else ""))
    return saved, failed


def _allowed(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").endswith(ALLOWED_HOST_SUFFIX)


def _try_fetch(url: str) -> tuple[bytes, str] | None:
    """Image bytes and file extension, or None if it could not be downloaded."""
    try:
        data, content_type = _fetch(url)
    except (OSError, ValueError):
        return None
    extension = EXTENSIONS.get(content_type.split(";")[0].strip().lower())
    if extension is None or not data or len(data) > MAX_BYTES:
        return None
    return data, extension


def _fetch(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read(MAX_BYTES + 1), response.headers.get("Content-Type", "")
