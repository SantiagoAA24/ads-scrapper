"""Local UI: search the scraped ads and manage saved searches. Localhost only."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, parser
from .runs import Runner

PAGE_SIZE = 48
AGE_CHOICES = [(7, "Últimos 7 días"), (30, "Últimos 30 días"), (90, "Últimos 90 días")]
SORT_CHOICES = [
    ("trend", "Tendencia"),
    ("copies", "Más duplicados"),
    ("recent", "Más recientes"),
    ("oldest", "Más tiempo activos"),
]
INTERVAL_CHOICES = [
    (0, "Solo manual"),
    (12, "Cada 12 horas"),
    (24, "Cada día"),
    (72, "Cada 3 días"),
    (168, "Cada semana"),
]
RUN_STATUS_LABELS = {
    "running": "en curso",
    "completed": "completa",
    "max_ads": "llegó al límite",
    "stalled": "se detuvo sin terminar",
    "blocked": "bloqueada por Meta",
    "interrupted": "interrumpida",
    "error": "falló",
}
FORMAT_LABELS = {
    "IMAGE": "Imagen",
    "VIDEO": "Video",
    "CAROUSEL": "Carrusel",
    "MULTI_IMAGES": "Varias imágenes",
    "DCO": "Catálogo dinámico",
    "DPA": "Catálogo",
}


def create_app(db_path: str | Path = db.DEFAULT_DB_PATH, runner: Runner | None = None) -> FastAPI:
    runner = runner or Runner(db_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        runner.start()
        yield
        runner.stop()

    app = FastAPI(title="ads-scrapper", docs_url=None, redoc_url=None, lifespan=lifespan)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    templates.env.filters["day"] = _day
    templates.env.filters["days_ago"] = _days_ago
    templates.env.filters["moment"] = _moment
    templates.env.globals.update(
        ad_url=parser.ad_url, format_labels=FORMAT_LABELS, trend_days=db.TREND_DAYS,
        run_status_labels=RUN_STATUS_LABELS,
    )

    setup = db.connect(db_path)
    media_folder = db.media_dir(setup)
    setup.close()
    media_folder.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=media_folder), name="media")

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        q: str = "",
        search: str = "",
        format: str = "",
        age: str = "",
        min_copies: str = "",
        active: str = "",
        saved: str = "",
        tag: str = "",
        sort: str = "trend",
        page: str = "1",
    ):
        page_number = max(_int(page, 1), 1)
        copies = max(_int(min_copies, 0), 0)
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
                saved_only=saved == "1",
                tag=tag or None,
                sort=sort,
                limit=PAGE_SIZE + 1,
                offset=(page_number - 1) * PAGE_SIZE,
            )
            formats = db.list_formats(conn)
            tags = db.list_tags(conn)
            total_ads = conn.execute("SELECT COUNT(*) FROM ads").fetchone()[0]
        finally:
            conn.close()

        filters = {
            "q": q, "search": search, "format": format, "age": age,
            "min_copies": str(copies) if copies else "", "active": active, "saved": saved, "tag": tag, "sort": sort,
        }

        def page_link(number: int) -> str:
            params = {k: v for k, v in filters.items() if v and (k, v) != ("sort", "trend")}
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
                "tags": tags,
                "here": _here(request),
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
            is_saved, group_tags = db.group_state(conn, group_key)
            tags = db.list_tags(conn)
        finally:
            conn.close()
        if not ads:
            raise HTTPException(status_code=404, detail="Grupo no encontrado")
        return templates.TemplateResponse(
            request,
            "group.html",
            {
                "ads": ads,
                "pages": sorted({ad["page_name"] for ad in ads if ad["page_name"]}),
                "group_key": group_key,
                "saved": is_saved,
                "group_tags": group_tags,
                "tags": tags,
                "here": _here(request),
            },
        )

    @app.get("/searches", response_class=HTMLResponse)
    def searches_page(request: Request, error: str = ""):
        conn = db.connect(db_path)
        try:
            rows = db.saved_searches(conn)
            blocked = db.blocked_until(conn)
        finally:
            conn.close()
        return templates.TemplateResponse(
            request,
            "searches.html",
            {
                "searches": rows,
                "status": runner.status(),
                "blocked_until": blocked,
                "interval_choices": INTERVAL_CHOICES,
                "interval_labels": dict(INTERVAL_CHOICES),
                "error": error,
                "now": int(time.time()),
            },
        )

    @app.get("/api/status")
    def status():
        return runner.status()

    def same_origin(request: Request) -> None:
        # Other sites open in the browser must not be able to trigger scrapes.
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(status_code=403, detail="Origen no permitido")

    def back(error: str = "") -> RedirectResponse:
        return RedirectResponse("/searches" + (f"?{urlencode({'error': error})}" if error else ""), status_code=303)

    @app.post("/saved/{group_key}", dependencies=[Depends(same_origin)])
    def save_action(group_key: str, action: str = Form(""), tag: str = Form(""), next: str = Form("/")):
        conn = db.connect(db_path)
        try:
            if not db.group_ads(conn, group_key):
                raise HTTPException(status_code=404, detail="Grupo no encontrado")
            if action == "save":
                db.save_group(conn, group_key)
            elif action == "unsave":
                db.unsave_group(conn, group_key)
            elif action == "tag":
                db.add_tag(conn, group_key, tag)
            elif action == "untag":
                db.remove_tag(conn, group_key, tag)
            else:
                raise HTTPException(status_code=400, detail="Acción desconocida")
        finally:
            conn.close()
        # Only ever send the browser back to a page of this app.
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        return RedirectResponse(f"{target.split('#')[0]}#g-{group_key}", status_code=303)

    @app.post("/searches", dependencies=[Depends(same_origin)])
    def add_search(
        query: str = Form(""),
        country: str = Form("ALL"),
        max_ads: str = Form("300"),
        interval_hours: str = Form("0"),
        run_now: str = Form(""),
    ):
        query, country = query.strip(), country.strip().upper() or "ALL"
        if not query:
            return back("Escribe una palabra clave.")
        if country != "ALL" and not (len(country) == 2 and country.isalpha()):
            return back("El país es un código de dos letras, como CO o MX, o ALL.")
        limit = min(max(_int(max_ads, 300), 10), 2000)
        interval = _int(interval_hours, 0)
        if interval not in dict(INTERVAL_CHOICES):
            interval = 0
        conn = db.connect(db_path)
        try:
            search_id = db.save_search(
                conn, query, country, max_ads=limit, interval_hours=interval or None, update=True
            )
        finally:
            conn.close()
        if run_now:
            runner.enqueue(search_id)
        return back()

    @app.post("/searches/{search_id}/{action}", dependencies=[Depends(same_origin)])
    def search_action(search_id: int, action: str):
        conn = db.connect(db_path)
        try:
            if db.get_search(conn, search_id) is None:
                raise HTTPException(status_code=404, detail="Búsqueda no encontrada")
            if action == "run":
                runner.enqueue(search_id)
            elif action in ("pause", "resume"):
                db.set_search_enabled(conn, search_id, action == "resume")
            elif action == "delete":
                db.delete_search(conn, search_id)
            else:
                raise HTTPException(status_code=404)
        finally:
            conn.close()
        return back()

    return app


def _here(request: Request) -> str:
    """Current path and query, so actions can return to the same view."""
    return request.url.path + (f"?{request.url.query}" if request.url.query else "")


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _day(timestamp: int | None) -> str:
    if not timestamp:
        return "?"
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")


def _moment(timestamp: int | None) -> str:
    """Local date and time, for things that happened on this machine."""
    if not timestamp:
        return "nunca"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _days_ago(timestamp: int | None) -> str:
    if not timestamp:
        return ""
    days = max(int((time.time() - timestamp) // 86400), 0)
    if days == 0:
        return "hoy"
    return "hace 1 día" if days == 1 else f"hace {days} días"
