"""Dashboard web con FastAPI — vista read-only de la BD de ofertas."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _get_db(db_path: Path) -> sqlite3.Connection:
    """Abre la BD en modo read-only con WAL."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def create_app(db_path: str | Path) -> FastAPI:
    """Crea la aplicación FastAPI del dashboard."""
    db_path = Path(db_path)
    app = FastAPI(title="Deals Scraper Dashboard")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        conn = _get_db(db_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) as total FROM deals")
            total = cur.fetchone()["total"]
            cur.execute("SELECT COUNT(DISTINCT store) as stores FROM deals")
            stores = cur.fetchone()["stores"]
            cur.execute("SELECT COUNT(*) as sent FROM deals WHERE sent_to_telegram = 1")
            sent = cur.fetchone()["sent"]
            cur.execute("SELECT COUNT(*) as history FROM price_history")
            history = cur.fetchone()["history"]

            cur.execute(
                """SELECT * FROM deals
                   WHERE discount_pct > 0
                   ORDER BY updated_at DESC LIMIT 10""",
            )
            recent = [dict(r) for r in cur.fetchall()]

            return templates.TemplateResponse("index.html", {
                "request": request,
                "total": total,
                "stores": stores,
                "sent": sent,
                "history": history,
                "recent": recent,
            })
        finally:
            conn.close()

    @app.get("/deals", response_class=HTMLResponse)
    async def deals_list(request: Request, q: str = "", page: int = 1):
        conn = _get_db(db_path)
        per_page = 25
        offset = (page - 1) * per_page
        try:
            cur = conn.cursor()
            if q:
                cur.execute(
                    """SELECT * FROM deals WHERE title LIKE ?
                       ORDER BY discount_pct DESC LIMIT ? OFFSET ?""",
                    (f"%{q}%", per_page, offset),
                )
            else:
                cur.execute(
                    """SELECT * FROM deals
                       ORDER BY discount_pct DESC LIMIT ? OFFSET ?""",
                    (per_page, offset),
                )
            deals = [dict(r) for r in cur.fetchall()]

            return templates.TemplateResponse("deals.html", {
                "request": request,
                "deals": deals,
                "q": q,
                "page": page,
            })
        finally:
            conn.close()

    @app.get("/deal/{deal_id}", response_class=HTMLResponse)
    async def deal_detail(request: Request, deal_id: int):
        conn = _get_db(db_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM deals WHERE id = ?", (deal_id,))
            deal = cur.fetchone()
            if not deal:
                return HTMLResponse("Deal no encontrado", status_code=404)
            deal = dict(deal)

            cur.execute(
                "SELECT price, detected_at FROM price_history WHERE deal_id = ? ORDER BY detected_at",
                (deal_id,),
            )
            history = [dict(r) for r in cur.fetchall()]

            return templates.TemplateResponse("deal_detail.html", {
                "request": request,
                "deal": deal,
                "history": history,
            })
        finally:
            conn.close()

    @app.get("/watchlist", response_class=HTMLResponse)
    async def watchlist_page(request: Request):
        conn = _get_db(db_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM watchlist ORDER BY added_at DESC")
            items = [dict(r) for r in cur.fetchall()]
            return templates.TemplateResponse("watchlist.html", {
                "request": request,
                "items": items,
            })
        except Exception:
            return templates.TemplateResponse("watchlist.html", {
                "request": request,
                "items": [],
            })
        finally:
            conn.close()

    @app.get("/stores", response_class=HTMLResponse)
    async def stores_page(request: Request):
        conn = _get_db(db_path)
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT store,
                          COUNT(*) as count,
                          ROUND(AVG(discount_pct), 1) as avg_discount,
                          MAX(updated_at) as last_update
                   FROM deals GROUP BY store ORDER BY count DESC""",
            )
            store_stats = [dict(r) for r in cur.fetchall()]
            return templates.TemplateResponse("stores.html", {
                "request": request,
                "stores": store_stats,
            })
        finally:
            conn.close()

    # --- API endpoints ---
    @app.get("/api/deals")
    async def api_deals(q: str = "", limit: int = Query(default=25, le=100)):
        conn = _get_db(db_path)
        try:
            cur = conn.cursor()
            if q:
                cur.execute(
                    "SELECT * FROM deals WHERE title LIKE ? ORDER BY discount_pct DESC LIMIT ?",
                    (f"%{q}%", limit),
                )
            else:
                cur.execute("SELECT * FROM deals ORDER BY discount_pct DESC LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    @app.get("/api/stats")
    async def api_stats():
        conn = _get_db(db_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) as total FROM deals")
            total = cur.fetchone()["total"]
            cur.execute("SELECT COUNT(DISTINCT store) as stores FROM deals")
            stores = cur.fetchone()["stores"]
            return {"total_deals": total, "stores": stores}
        finally:
            conn.close()

    @app.get("/api/deal/{deal_id}/chart")
    async def api_deal_chart(deal_id: int):
        from .charts import generate_price_chart
        conn = _get_db(db_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT title FROM deals WHERE id = ?", (deal_id,))
            deal = cur.fetchone()
            if not deal:
                return Response("Not found", status_code=404)

            cur.execute(
                "SELECT price, detected_at FROM price_history WHERE deal_id = ? ORDER BY detected_at",
                (deal_id,),
            )
            history = [dict(r) for r in cur.fetchall()]
            chart = generate_price_chart(deal["title"], history)
            if chart:
                return Response(content=chart, media_type="image/png")
            return Response("No data", status_code=404)
        finally:
            conn.close()

    return app
