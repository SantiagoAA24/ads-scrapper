"""Running saved searches: once from the terminal, or queued and scheduled by
the web app. Only one search ever runs at a time."""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from . import db
from .scraper import ScrapeOptions, ScrapeResult, scrape


def run_search(
    conn,
    search,
    *,
    scrape_fn: Callable[..., ScrapeResult] = scrape,
    log: Callable[[str], None] = print,
    on_progress: Callable[[int], None] | None = None,
    **option_overrides: Any,
) -> ScrapeResult:
    """Scrape one saved search and record the run."""
    options = ScrapeOptions(
        query=search["query"],
        country=search["country"],
        active_status=search["active_status"],
        media_type=search["media_type"],
        max_ads=search["max_ads"],
        **option_overrides,
    )
    run_id = db.start_run(conn, options.query, options.country, "scrape", search["id"])
    total = 0

    def store(ads):
        nonlocal total
        seen, new = db.save_ads(conn, run_id, ads)
        total += seen
        if on_progress:
            on_progress(total)
        return seen, new

    status = "error"
    try:
        result = scrape_fn(options, store, log=log)
        status = result.status
    except KeyboardInterrupt:
        status = "interrupted"
        raise
    finally:
        db.finish_run(conn, run_id, status)
    # Only a full listing of active ads proves that a missing ad has stopped.
    if result.status == "completed" and result.ads_seen and options.active_status == "active":
        db.mark_missing_inactive(conn, search["id"], run_id)
    return result


class Runner:
    """Background worker for the web app: runs queued searches one by one and
    queues scheduled ones when they come due."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        scrape_fn: Callable[..., ScrapeResult] = scrape,
        gap_seconds: tuple[float, float] = (60, 180),
        poll_seconds: float = 5,
        **option_overrides: Any,
    ):
        self.db_path = db_path
        self.scrape_fn = scrape_fn
        # Pause between consecutive searches, so they do not look like a burst.
        self.gap_seconds = gap_seconds
        self.poll_seconds = poll_seconds
        self.option_overrides = option_overrides
        self._queue: deque[int] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._not_before = 0.0

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="ads-runner", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def enqueue(self, search_id: int) -> bool:
        """Queue a search unless it is already queued or running."""
        with self._lock:
            running = self._current and self._current["search_id"] == search_id
            if running or search_id in self._queue:
                return False
            self._queue.append(search_id)
            return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "current": dict(self._current) if self._current else None,
                "queued": list(self._queue),
                "last_error": self._last_error,
            }

    def _loop(self) -> None:
        conn = db.connect(self.db_path)
        try:
            while not self._stop.is_set():
                self._tick(conn)
                self._stop.wait(self.poll_seconds)
        finally:
            conn.close()

    def _tick(self, conn) -> None:
        if db.blocked_until(conn) is None:
            for search in db.due_searches(conn):
                self.enqueue(search["id"])
        if time.monotonic() < self._not_before:
            return
        with self._lock:
            if not self._queue:
                return
            search_id = self._queue.popleft()
            search = db.get_search(conn, search_id)
            if search is None:  # deleted while queued
                return
            label = search["query"] + (f" · {search['country']}" if search["country"] != "ALL" else "")
            self._current = {"search_id": search_id, "label": label, "ads_seen": 0}
        try:
            run_search(
                conn,
                search,
                scrape_fn=self.scrape_fn,
                log=lambda _: None,
                on_progress=self._progress,
                **self.option_overrides,
            )
            self._last_error = None
        except Exception as error:  # keep the worker alive; show it in the UI
            self._last_error = f"{label}: {error}"
        finally:
            with self._lock:
                self._current = None
            self._not_before = time.monotonic() + random.uniform(*self.gap_seconds)

    def _progress(self, ads_seen: int) -> None:
        with self._lock:
            if self._current:
                self._current["ads_seen"] = ads_seen
