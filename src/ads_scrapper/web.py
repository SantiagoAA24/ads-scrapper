"""Local search UI over the scraped ads. Read-only; meant for localhost."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import db, parser

PAGE_SIZE = 48
AGE_CHOICES = [(7, "Últimos 7 días"), (30, "Últimos 30 días"), (90, "Últimos 90 días")]
SORT_CHOICES = [
    ("copies", "Más duplicados"),
    ("recent", "Más recientes"),
    ("oldest", "Más tiempo activos"),
]
FORMAT_LABELS = {
    "IMAGE": "Imagen",
    "VIDEO": "Video",
    "CAROUSEL": "Carrusel",
    "MULTI_IMAGES": "Varias imágenes",
    "DCO": "Catálogo dinámico",
    "DPA": "Catálogo",
}


def create_app(db_path: str | Path = db.DEFAULT_DB_PATH) -> FastAPI:
    app = FastAPI(title="ads-scrapper", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    templates.env.filters["day"] = _day
    templates.env.filters["days_ago"] = _days_ago
    templates.env.globals.update(ad_url=parser.ad_url, format_labels=FORMAT_LABELS)

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        q: str = "",
        search: str = "",
        format: str = "",
        age: str = "",
        min_copies: str = "",
        active: str = "",
        sort: str = "copies",
        page: str = "1",
    ):
        page_number = max(_int(page, 1), 1)
        copies = max(_int(min_copies, 1), 1)
        searches = {}
        conn = db.connect(db_path)
        try:
            for row in db.list_searches(conn):
                key = f"{row['query']}|{row['country'] or ''}"
                label = row["query"] + (f" · {row['country']}" if row["country"] else "")
                searches[key] = (label, (row["query"], row["country"]))
            rows = db.search_groups(
                conn,
                q,
                search=searches[search][1] if search in searches else None,
                display_format=format or None,
                active_only=active == "1",
                max_age_days=_int(age, 0) or None,
                min_copies=copies,
                sort=sort,
                limit=PAGE_SIZE + 1,
                offset=(page_number - 1) * PAGE_SIZE,
            )
            formats = db.list_formats(conn)
            total_ads = conn.execute("SELECT COUNT(*) FROM ads").fetchone()[0]
        finally:
            conn.close()

        filters = {
            "q": q, "search": search, "format": format, "age": age,
            "min_copies": str(copies) if copies > 1 else "", "active": active, "sort": sort,
        }

        def page_link(number: int) -> str:
            params = {k: v for k, v in filters.items() if v and (k, v) != ("sort", "copies")}
            if number > 1:
                params["page"] = number
            return "/?" + urlencode(params)

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "groups": rows[:PAGE_SIZE],
                "filters": filters,
                "searches": {key: label for key, (label, _) in searches.items()},
                "formats": formats,
                "age_choices": AGE_CHOICES,
                "sort_choices": SORT_CHOICES,
                "total_ads": total_ads,
                "page": page_number,
                "prev_link": page_link(page_number - 1) if page_number > 1 else None,
                "next_link": page_link(page_number + 1) if len(rows) > PAGE_SIZE else None,
                "has_filters": any(v for k, v in filters.items() if k != "sort"),
            },
        )

    @app.get("/group/{group_key}", response_class=HTMLResponse)
    def group(request: Request, group_key: str):
        conn = db.connect(db_path)
        try:
            ads = db.group_ads(conn, group_key)
        finally:
            conn.close()
        if not ads:
            raise HTTPException(status_code=404, detail="Grupo no encontrado")
        return templates.TemplateResponse(
            request,
            "group.html",
            {"ads": ads, "pages": sorted({ad["page_name"] for ad in ads if ad["page_name"]})},
        )

    return app


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _day(timestamp: int | None) -> str:
    if not timestamp:
        return "?"
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")


def _days_ago(timestamp: int | None) -> str:
    if not timestamp:
        return ""
    days = max(int((time.time() - timestamp) // 86400), 0)
    if days == 0:
        return "hoy"
    return "hace 1 día" if days == 1 else f"hace {days} días"
