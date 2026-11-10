"""Turns Ad Library GraphQL payloads into flat ad records.

The page delivers results in two ways: the first batch is embedded in the HTML
as JSON blobs, later batches come from the GraphQL endpoint. Both contain a
``search_results_connection`` object somewhere inside, so everything here works
by looking for that key instead of assuming a fixed path.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urlsplit

CONNECTION_KEY = "search_results_connection"
_ANTI_HIJACK_PREFIX = "for (;;);"
_TEMPLATE = re.compile(r"\{\{.*?\}\}")
_WHITESPACE = re.compile(r"\s+")


@dataclass
class Batch:
    """Ads found in one payload, plus what it says about pagination."""

    ads: list[dict[str, Any]] = field(default_factory=list)
    # None when the payload had no page_info at all.
    has_next_page: bool | None = None
    found_connection: bool = False


def load_payloads(text: str) -> list[Any]:
    """Parse a response body that may hold one JSON document or one per line."""
    text = text.strip()
    if text.startswith(_ANTI_HIJACK_PREFIX):
        text = text[len(_ANTI_HIJACK_PREFIX):]
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    payloads = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payloads.append(json.loads(line))
        except ValueError:
            continue
    return payloads


def find_connections(obj: Any) -> Iterator[dict[str, Any]]:
    """Yield every ``search_results_connection`` dict nested anywhere in obj."""
    stack = [obj]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            connection = current.get(CONNECTION_KEY)
            if isinstance(connection, dict):
                yield connection
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def parse_text(text: str) -> Batch:
    """Extract ads from a raw response body. Cheap no-op for unrelated bodies."""
    batch = Batch()
    if CONNECTION_KEY not in text:
        return batch
    for payload in load_payloads(text):
        merge(batch, parse_payload(payload))
    return batch


def parse_payload(payload: Any) -> Batch:
    batch = Batch()
    for connection in find_connections(payload):
        batch.found_connection = True
        page_info = connection.get("page_info") or {}
        if "has_next_page" in page_info:
            batch.has_next_page = bool(page_info["has_next_page"])
        for edge in connection.get("edges") or []:
            node = (edge or {}).get("node") or {}
            group = [r for r in node.get("collated_results") or [] if r]
            # Only the first ad of a group carries the count; share it.
            counts = [r["collation_count"] for r in group if r.get("collation_count")]
            group_count = max(counts) if counts else None
            for result in group:
                ad = normalize_ad(result, group_count)
                if ad:
                    batch.ads.append(ad)
    return batch


def merge(target: Batch, other: Batch) -> None:
    target.ads.extend(other.ads)
    target.found_connection = target.found_connection or other.found_connection
    if other.has_next_page is not None:
        target.has_next_page = other.has_next_page


def normalize_ad(result: dict[str, Any], group_count: int | None = None) -> dict[str, Any] | None:
    ad_id = result.get("ad_archive_id")
    if not ad_id:
        return None
    snapshot = result.get("snapshot") or {}
    cards = [c for c in snapshot.get("cards") or [] if c]
    first_card = cards[0] if cards else {}

    # Catalog/carousel ads have placeholders like {{product.name}} at the top
    # level; the real copy lives in the cards.
    body = _clean((snapshot.get("body") or {}).get("text")) or _clean(first_card.get("body"))
    title = _clean(snapshot.get("title")) or _clean(first_card.get("title"))
    link_url = snapshot.get("link_url") or first_card.get("link_url")
    caption = _clean(snapshot.get("caption")) or _clean(first_card.get("caption"))

    image_urls: list[str] = []
    video_urls: list[str] = []
    preview_urls: list[str] = []
    for media in [*(snapshot.get("images") or []), *(snapshot.get("videos") or []), *cards]:
        image = media.get("original_image_url") or media.get("resized_image_url")
        video = media.get("video_hd_url") or media.get("video_sd_url")
        if image:
            image_urls.append(image)
        if video:
            video_urls.append(video)
        preview = media.get("resized_image_url") or image or media.get("video_preview_image_url")
        if preview:
            preview_urls.append(preview)

    link_domain = _domain(link_url)
    return {
        "ad_archive_id": str(ad_id),
        "collation_id": result.get("collation_id"),
        "collation_count": result.get("collation_count") or group_count,
        "page_id": result.get("page_id") or snapshot.get("page_id"),
        "page_name": result.get("page_name") or snapshot.get("page_name"),
        "page_profile_uri": snapshot.get("page_profile_uri"),
        "page_like_count": snapshot.get("page_like_count"),
        "page_categories": snapshot.get("page_categories") or [],
        "body_text": body,
        "title": title,
        "caption": caption,
        "link_description": _clean(snapshot.get("link_description"))
        or _clean(first_card.get("link_description")),
        "cta_text": snapshot.get("cta_text") or first_card.get("cta_text"),
        "cta_type": snapshot.get("cta_type") or first_card.get("cta_type"),
        "link_url": link_url,
        "link_domain": link_domain,
        "display_format": snapshot.get("display_format"),
        "publisher_platforms": result.get("publisher_platform") or [],
        "start_date": result.get("start_date"),
        "end_date": result.get("end_date"),
        "is_active": result.get("is_active"),
        "image_urls": image_urls,
        "video_urls": video_urls,
        "preview_image_url": preview_urls[0] if preview_urls else None,
        "content_hash": content_hash(body, link_domain),
        "raw": result,
    }


def content_hash(body: str | None, link_domain: str | None) -> str | None:
    """Fingerprint of copy + destination, to spot the same ad across pages."""
    if not body:
        return None
    text = unicodedata.normalize("NFKC", body).casefold()
    text = _WHITESPACE.sub(" ", text).strip()
    return hashlib.sha1(f"{text}|{link_domain or ''}".encode()).hexdigest()


def ad_url(ad_archive_id: str) -> str:
    return f"https://www.facebook.com/ads/library/?id={ad_archive_id}"


def _clean(value: Any) -> str | None:
    """Drop empty strings and unresolved {{template}} placeholders."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or not _TEMPLATE.sub("", value).strip():
        return None
    return value


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    host = urlsplit(url).hostname
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host
