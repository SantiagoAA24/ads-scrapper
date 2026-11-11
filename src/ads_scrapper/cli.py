"""Command line: scrape a keyword, import a saved response, list or browse top ads."""

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db, media, parser
from .runs import run_search

STATUS_MESSAGES = {
    "completed": "Búsqueda completa: Meta no tiene más resultados.",
    "max_ads": "Se alcanzó el límite de anuncios (--max-ads).",
    "stalled": "La página dejó de entregar anuncios nuevos; se detuvo el scroll.",
    "blocked": "Meta redirigió a login o verificación. No se reintenta: espera antes de volver a probar.",
}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    conn = db.connect(args.db)
    try:
        return args.handler(conn, args)
    finally:
        conn.close()


def _build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ads-scrapper", description=__doc__)
    root.add_argument("--db", default=str(db.DEFAULT_DB_PATH), help="ruta de la base SQLite")
    commands = root.add_subparsers(dest="command", required=True)

    scrape_cmd = commands.add_parser("scrape", help="buscar una palabra clave en la Ad Library")
    scrape_cmd.add_argument("query")
    scrape_cmd.add_argument("--country", default="ALL", help="código de país, p. ej. CO, MX (por defecto ALL)")
    scrape_cmd.add_argument("--max-ads", type=int, default=300)
    scrape_cmd.add_argument("--status", default="active", choices=["active", "inactive", "all"])
    scrape_cmd.add_argument("--media", default="all", choices=["all", "image", "video", "meme", "none"])
    scrape_cmd.set_defaults(handler=_cmd_scrape)

    refresh_cmd = commands.add_parser("refresh", help="repetir las búsquedas guardadas pendientes")
    refresh_cmd.add_argument("--all", action="store_true", help="todas, no solo las programadas que tocan")
    refresh_cmd.set_defaults(handler=_cmd_refresh)

    for command in (scrape_cmd, refresh_cmd):
        command.add_argument("--headless", action="store_true", help="sin ventana (más fácil de detectar)")
        command.add_argument("--min-delay", type=float, default=2.0)
        command.add_argument("--max-delay", type=float, default=5.0)

    import_cmd = commands.add_parser("import", help="cargar respuestas JSON guardadas a mano")
    import_cmd.add_argument("files", nargs="+", type=Path)
    import_cmd.add_argument("--query", help="palabra clave con la que se obtuvieron")
    import_cmd.add_argument("--country")
    import_cmd.set_defaults(handler=_cmd_import)

    top_cmd = commands.add_parser("top", help="anuncios guardados con más duplicados")
    top_cmd.add_argument("text", nargs="?", help="filtrar por texto, anunciante o dominio")
    top_cmd.add_argument("--limit", type=int, default=20)
    top_cmd.set_defaults(handler=_cmd_top)

    media_cmd = commands.add_parser("media", help="descargar las vistas previas que falten")
    media_cmd.set_defaults(handler=_cmd_media)

    web_cmd = commands.add_parser("web", help="abrir el buscador local en el navegador")
    web_cmd.add_argument("--port", type=int, default=8000)
    web_cmd.add_argument("--no-browser", action="store_true", help="no abrir el navegador")
    web_cmd.set_defaults(handler=_cmd_web)
    return root


def _cmd_scrape(conn, args) -> int:
    if args.min_delay > args.max_delay:
        print("--min-delay no puede ser mayor que --max-delay", file=sys.stderr)
        return 2
    # Every search run from here is also kept as a saved search.
    search_id = db.save_search(conn, args.query, args.country, args.status, args.media, args.max_ads)
    return _run(conn, db.get_search(conn, search_id), args)


def _cmd_refresh(conn, args) -> int:
    searches = db.saved_searches(conn) if args.all else db.due_searches(conn)
    if not searches:
        print("No hay búsquedas guardadas pendientes.")
        return 0
    if not args.all and db.blocked_until(conn):
        print("Meta bloqueó una búsqueda hace poco; las automáticas están en pausa.", file=sys.stderr)
        return 1
    for position, search in enumerate(searches):
        if position:
            pause = random.uniform(60, 180)
            print(f"Esperando {pause:.0f} s antes de la siguiente búsqueda…")
            time.sleep(pause)
        print(f"== {search['query']} · {search['country']}")
        code = _run(conn, search, args)
        if code:
            return code  # blocked or interrupted: do not insist
    return 0


def _run(conn, search, args) -> int:
    try:
        result = run_search(
            conn, search, headless=args.headless, min_delay=args.min_delay, max_delay=args.max_delay
        )
    except KeyboardInterrupt:
        print("\nInterrumpido; lo capturado hasta ahora quedó guardado.")
        return 130
    print(STATUS_MESSAGES[result.status])
    print(f"{result.ads_seen} anuncios vistos, {result.ads_new} nuevos en la base.")
    return 1 if result.status == "blocked" else 0


def _cmd_import(conn, args) -> int:
    run_id = db.start_run(conn, args.query, args.country, "import")
    seen = new = 0
    status = "error"
    try:
        for path in args.files:
            batch = parser.parse_text(path.read_text(encoding="utf-8"))
            if not batch.found_connection:
                print(f"{path}: no contiene resultados de la Ad Library", file=sys.stderr)
                continue
            file_seen, file_new = db.save_ads(conn, run_id, batch.ads)
            seen += file_seen
            new += file_new
        status = "completed"
    finally:
        db.finish_run(conn, run_id, status)
    print(f"{seen} anuncios importados, {new} nuevos en la base.")
    return 0 if seen else 1


def _cmd_top(conn, args) -> int:
    rows = db.search_groups(conn, args.text, limit=args.limit)
    if not rows:
        print("No hay anuncios guardados que coincidan.")
        return 0
    for row in rows:
        started = (
            datetime.fromtimestamp(row["start_date"], tz=timezone.utc).strftime("%Y-%m-%d")
            if row["start_date"]
            else "?"
        )
        text = " ".join((row["body_text"] or row["title"] or "").split())
        extra = f"  ({row['creatives']} creativos)" if row["creatives"] > 1 else ""
        if row["growth"]:
            extra += f"  {row['growth']:+d} en {db.TREND_DAYS} días"
        print(f"x{row['copies']:<4} {started}  {row['page_name'] or '?'}  [{row['link_domain'] or '-'}]{extra}")
        print(f"      {text[:110]}")
        print(f"      {parser.ad_url(row['ad_archive_id'])}")
    return 0


def _cmd_media(conn, args) -> int:
    saved, failed = media.download_previews(conn)
    if not saved and not failed:
        print("No falta ninguna vista previa.")
    elif failed:
        print("Las no disponibles tienen la URL caducada; se renuevan al repetir su búsqueda.")
    return 0


def _cmd_web(conn, args) -> int:
    import threading
    import webbrowser

    import uvicorn

    from .web import create_app

    url = f"http://127.0.0.1:{args.port}"
    print(f"Buscador en {url} (Ctrl+C para cerrar)")
    print("Las búsquedas programadas se ejecutan mientras esto siga abierto.")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    # Only this machine can reach it: bound to localhost.
    uvicorn.run(create_app(args.db), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
