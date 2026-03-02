"""SQLite: crear tablas, insertar/actualizar ofertas, marcar enviadas, historial de precios."""

from __future__ import annotations

import logging
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Deal

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "deals.db"

_CREATE_DEALS = """
CREATE TABLE IF NOT EXISTS deals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT UNIQUE NOT NULL,
    title           TEXT NOT NULL,
    store           TEXT NOT NULL,
    current_price   REAL NOT NULL,
    original_price  REAL,
    discount_pct    REAL DEFAULT 0,
    category        TEXT DEFAULT '',
    currency        TEXT DEFAULT 'EUR',
    image_url       TEXT DEFAULT '',
    detected_at     TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    sent_to_telegram INTEGER DEFAULT 0
);
"""

_CREATE_PRICE_HISTORY = """
CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id     INTEGER NOT NULL REFERENCES deals(id),
    price       REAL NOT NULL,
    detected_at TEXT NOT NULL
);
"""

_CREATE_MARKET_PRICES = """
CREATE TABLE IF NOT EXISTS market_prices (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_title TEXT NOT NULL,
    market_price     REAL NOT NULL,
    source           TEXT NOT NULL,
    source_detail    TEXT DEFAULT '',
    created_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL
);
"""

_CREATE_WATCHLIST = """
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    max_price REAL NOT NULL,
    min_price REAL DEFAULT 0,
    exclude_keywords TEXT DEFAULT '[]',
    added_at TEXT NOT NULL
);
"""


class Database:
    """Gestiona la base de datos SQLite de ofertas."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute(_CREATE_DEALS)
        cur.execute(_CREATE_PRICE_HISTORY)
        cur.execute(_CREATE_MARKET_PRICES)
        cur.execute(_CREATE_WATCHLIST)
        # Índices para acelerar queries frecuentes
        cur.execute("CREATE INDEX IF NOT EXISTS idx_deals_store_cat ON deals(store, category)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_deals_updated ON deals(updated_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_deals_sent ON deals(sent_to_telegram)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ph_deal ON price_history(deal_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_mp_title ON market_prices(normalized_title)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_mp_expires ON market_prices(expires_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_name ON watchlist(name)")
        self.conn.commit()

    # ------------------------------------------------------------------
    # Upsert — ahora SIEMPRE registra el precio en el historial
    # ------------------------------------------------------------------
    def upsert_deal(self, deal: Deal) -> tuple[int, bool]:
        """Inserta o actualiza una oferta. Siempre registra el precio observado.

        Returns:
            (deal_id, is_new): id de la fila y si es la primera vez que se ve.
        """
        now = datetime.utcnow().isoformat()
        cur = self.conn.cursor()

        cur.execute("SELECT id, current_price FROM deals WHERE url = ?", (deal.url,))
        row = cur.fetchone()

        if row is None:
            # Producto nuevo — insertar y registrar primer precio
            cur.execute(
                """INSERT INTO deals
                   (url, title, store, current_price, original_price,
                    discount_pct, category, currency, image_url,
                    detected_at, updated_at, sent_to_telegram)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    deal.url, deal.title, deal.store,
                    deal.current_price, deal.original_price,
                    deal.discount_pct, deal.category, deal.currency,
                    deal.image_url, now, now,
                ),
            )
            deal_id = cur.lastrowid
            is_new = True
        else:
            deal_id = row["id"]
            is_new = False

            cur.execute(
                """UPDATE deals SET
                     title = ?, current_price = ?, original_price = ?,
                     discount_pct = ?, category = ?, image_url = ?,
                     updated_at = ?,
                     sent_to_telegram = CASE WHEN ? != current_price THEN 0
                                             ELSE sent_to_telegram END
                   WHERE id = ?""",
                (
                    deal.title, deal.current_price, deal.original_price,
                    deal.discount_pct, deal.category, deal.image_url,
                    now, deal.current_price, deal_id,
                ),
            )

        # SIEMPRE registrar el precio en el historial
        cur.execute(
            "INSERT INTO price_history (deal_id, price, detected_at) VALUES (?, ?, ?)",
            (deal_id, deal.current_price, now),
        )

        self.conn.commit()
        return deal_id, is_new  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Historial de precios — para detectar ofertas falsas
    # ------------------------------------------------------------------
    def get_price_stats(self, deal_id: int) -> dict[str, Any] | None:
        """Estadísticas del historial de precios de un producto.

        Returns:
            Dict con: observations, min, max, median, mean, current, real_discount_pct
            o None si no hay historial.
        """
        cur = self.conn.cursor()
        cur.execute(
            "SELECT price FROM price_history WHERE deal_id = ? ORDER BY detected_at",
            (deal_id,),
        )
        rows = cur.fetchall()
        if not rows:
            return None

        prices = [r["price"] for r in rows]
        current = prices[-1]
        median = statistics.median(prices)

        # Descuento real = cuánto ha bajado vs la mediana histórica
        if median > 0 and current < median:
            real_discount = round((1 - current / median) * 100, 1)
        else:
            real_discount = 0.0

        return {
            "observations": len(prices),
            "min": min(prices),
            "max": max(prices),
            "median": median,
            "mean": round(statistics.mean(prices), 2),
            "current": current,
            "real_discount_pct": real_discount,
        }

    def get_price_stats_by_url(self, url: str) -> dict[str, Any] | None:
        """Igual que get_price_stats pero busca por URL."""
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM deals WHERE url = ?", (url,))
        row = cur.fetchone()
        if not row:
            return None
        return self.get_price_stats(row["id"])

    # ------------------------------------------------------------------
    # Percentiles por tienda/categoría (para detección de precios absurdos)
    # ------------------------------------------------------------------
    def get_store_price_percentiles(
        self, store: str, category: str = "",
    ) -> dict[str, Any] | None:
        """Distribución de precios de una tienda (opcionalmente por categoría).

        Returns:
            Dict con: count, min, max, p5, p10, p25, median
            o None si no hay datos.
        """
        cur = self.conn.cursor()
        if category:
            cur.execute(
                "SELECT current_price FROM deals WHERE store = ? AND category = ? ORDER BY current_price",
                (store, category),
            )
        else:
            cur.execute(
                "SELECT current_price FROM deals WHERE store = ? ORDER BY current_price",
                (store,),
            )
        rows = cur.fetchall()
        if not rows:
            return None

        prices = [r["current_price"] for r in rows]
        n = len(prices)

        def _pct(p: float) -> float:
            idx = int(p / 100 * (n - 1))
            return prices[min(idx, n - 1)]

        return {
            "count": n,
            "min": prices[0],
            "max": prices[-1],
            "p5": _pct(5),
            "p10": _pct(10),
            "p25": _pct(25),
            "median": _pct(50),
        }

    # ------------------------------------------------------------------
    # Consultas
    # ------------------------------------------------------------------
    def get_unsent_deals(self, limit: int = 50, max_age_days: int = 7) -> list[Deal]:
        """Devuelve ofertas que aún no se han enviado a Telegram.

        Solo devuelve deals actualizados en los últimos `max_age_days` días
        para evitar enviar descuentos expirados.
        """
        from datetime import timedelta

        cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
        cur = self.conn.cursor()
        cur.execute(
            """SELECT * FROM deals
               WHERE sent_to_telegram = 0 AND updated_at >= ?
               ORDER BY discount_pct DESC
               LIMIT ?""",
            (cutoff, limit),
        )
        return [self._row_to_deal(r) for r in cur.fetchall()]

    def update_verified_deal(self, deal: Deal) -> None:
        """Actualiza descuento y original_price de un deal verificado SIN resetear sent_to_telegram.

        Usado tras verify_real_deals() para persistir el descuento real calculado
        sin provocar re-envío del deal.
        """
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM deals WHERE url = ?", (deal.url,))
        row = cur.fetchone()
        if row is None:
            return
        cur.execute(
            """UPDATE deals SET
                 discount_pct = ?, original_price = ?,
                 sent_to_telegram = 0
               WHERE id = ? AND sent_to_telegram = 0""",
            (deal.discount_pct, deal.original_price, row["id"]),
        )
        self.conn.commit()
        deal.id = row["id"]

    def mark_sent(self, deal_ids: list[int]) -> None:
        """Marca ofertas como enviadas a Telegram."""
        if not deal_ids:
            return
        placeholders = ",".join("?" for _ in deal_ids)
        self.conn.execute(
            f"UPDATE deals SET sent_to_telegram = 1 WHERE id IN ({placeholders})",
            deal_ids,
        )
        self.conn.commit()

    def is_sent(self, deal_id: int) -> bool:
        """Comprueba si un deal ya fue enviado a Telegram."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT sent_to_telegram FROM deals WHERE id = ?", (deal_id,),
        )
        row = cur.fetchone()
        return bool(row and row["sent_to_telegram"])

    def get_top_deals(self, limit: int = 10) -> list[Deal]:
        """Devuelve las mejores ofertas recientes."""
        cur = self.conn.cursor()
        cur.execute(
            """SELECT * FROM deals
               ORDER BY discount_pct DESC
               LIMIT ?""",
            (limit,),
        )
        return [self._row_to_deal(r) for r in cur.fetchall()]

    def get_stats(self) -> dict[str, Any]:
        """Estadísticas generales de la base de datos."""
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) as total FROM deals")
        total = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) as sent FROM deals WHERE sent_to_telegram = 1")
        sent = cur.fetchone()["sent"]
        cur.execute("SELECT COUNT(DISTINCT store) as stores FROM deals")
        stores = cur.fetchone()["stores"]
        cur.execute("SELECT COUNT(*) as history FROM price_history")
        history = cur.fetchone()["history"]
        return {
            "total_deals": total,
            "sent_to_telegram": sent,
            "stores_tracked": stores,
            "price_observations": history,
        }

    # ------------------------------------------------------------------
    # Limpieza de datos antiguos
    # ------------------------------------------------------------------
    def cleanup_old_data(self, days: int = 90) -> None:
        """Elimina deals y price_history más antiguos que `days` días.

        Evita que la base de datos crezca indefinidamente.
        """
        from datetime import timedelta

        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        cur = self.conn.cursor()

        # Primero borrar historial de precios de deals viejos
        cur.execute(
            "DELETE FROM price_history WHERE deal_id IN "
            "(SELECT id FROM deals WHERE updated_at < ?)",
            (cutoff,),
        )
        history_deleted = cur.rowcount

        # Luego borrar los deals
        cur.execute("DELETE FROM deals WHERE updated_at < ?", (cutoff,))
        deals_deleted = cur.rowcount

        self.conn.commit()

        if deals_deleted or history_deleted:
            logger.info(
                "Limpieza DB: %d deals y %d registros de historial eliminados (>%d días)",
                deals_deleted, history_deleted, days,
            )

    # ------------------------------------------------------------------
    # Cross-store: buscar el mismo producto en varias tiendas
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_variant_tags(title: str) -> set[str]:
        """Extrae tags de variante de un título de producto.

        Detecta términos como Pro, Ultra, Plus, Max, Mini, Lite, SE, etc.
        que diferencian productos dentro de la misma familia.
        Devuelve un set normalizado para comparación.
        """
        import re
        title_lower = title.lower()
        # Términos de variante que cambian el producto
        variant_terms = {
            "pro", "ultra", "plus", "max", "mini", "lite", "se",
            "slim", "digital", "disc", "xl", "xs", "fe",
        }
        # Capacidades de almacenamiento (64gb vs 256gb = distinto producto)
        storage_pattern = re.compile(r'\b(\d+)\s*(?:gb|tb)\b', re.IGNORECASE)

        tags: set[str] = set()
        for term in variant_terms:
            # Buscar como palabra completa
            if re.search(rf'\b{term}\b', title_lower):
                tags.add(term)

        # Extraer capacidad de almacenamiento
        storage_matches = storage_pattern.findall(title_lower)
        for cap in storage_matches:
            tags.add(f"{cap}gb")

        return tags

    # Tiendas de reacondicionados/segunda mano (no comparar con tiendas de nuevo)
    _REFURBISHED_STORES = {"backmarket", "apple", "ebay", "cex"}

    def find_cross_store_deals(
        self,
        hours: int = 24,
        fuzzy_threshold: int = 85,
        min_discount_pct: float = 45.0,
    ) -> list[tuple[Deal, Deal]]:
        """Busca el mismo producto en distintas tiendas con precio diferente.

        Agrupa por título exacto primero, luego fuzzy matching con thefuzz.
        Devuelve pares (deal_barato, deal_caro) donde la diferencia supera
        min_discount_pct.

        Filtros de calidad:
        - Títulos cortos (< 10 chars) se ignoran (slugs de búsqueda como "samsung")
        - No compara tiendas de reacondicionados con tiendas de producto nuevo

        Args:
            hours: Ventana temporal para considerar deals recientes.
            fuzzy_threshold: Umbral de similitud para token_set_ratio (0-100).
            min_discount_pct: % mínimo de diferencia de precio para incluir.

        Returns:
            Lista de tuplas (deal_barato, deal_caro).
        """
        from datetime import timedelta

        try:
            from thefuzz import fuzz
        except ImportError:
            logger.warning("thefuzz no instalado, cross-store desactivado")
            return []

        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        cur = self.conn.cursor()
        cur.execute(
            """SELECT * FROM deals
               WHERE updated_at >= ? AND current_price > 0
               ORDER BY title""",
            (cutoff,),
        )
        rows = cur.fetchall()
        if not rows:
            return []

        deals = [self._row_to_deal(r) for r in rows]

        # Filtrar títulos basura (slugs de búsqueda como "samsung", "apple")
        deals = [d for d in deals if len(d.title.strip()) >= 10]

        # Paso 1: agrupar por título exacto (normalizado a minúsculas, strip)
        groups: dict[str, list[Deal]] = {}
        unmatched: list[Deal] = []

        for deal in deals:
            key = deal.title.strip().lower()
            if key not in groups:
                groups[key] = []
            groups[key].append(deal)

        # Separar grupos con >1 tienda vs singletons
        multi_store_groups: list[list[Deal]] = []
        for key, group in groups.items():
            stores_in_group = {d.store for d in group}
            if len(stores_in_group) > 1:
                multi_store_groups.append(group)
            else:
                unmatched.extend(group)

        # Paso 2: fuzzy matching para unmatched — pre-filtrado por bucket
        # Agrupar por (categoría, primera palabra del título) para reducir O(n²)
        if unmatched and len(unmatched) <= 5000:
            buckets: dict[str, list[tuple[int, Deal]]] = {}
            for i, d in enumerate(unmatched):
                words = d.title.strip().lower().split()
                # Bucket key: categoría + primera palabra significativa (>2 chars)
                first_word = ""
                for w in words:
                    if len(w) > 2:
                        first_word = w
                        break
                bucket_key = f"{d.category}|{first_word}"
                if bucket_key not in buckets:
                    buckets[bucket_key] = []
                buckets[bucket_key].append((i, d))

            used: set[int] = set()
            for bucket_deals in buckets.values():
                if len(bucket_deals) < 2:
                    continue
                # Solo fuzzy dentro del mismo bucket
                for bi, (i, d1) in enumerate(bucket_deals):
                    if i in used:
                        continue
                    fuzzy_group = [d1]
                    for bj, (j, d2) in enumerate(bucket_deals):
                        if bj <= bi or j in used:
                            continue
                        if d1.store == d2.store:
                            continue
                        ratio = fuzz.token_set_ratio(d1.title, d2.title)
                        if ratio >= fuzzy_threshold:
                            # Verificar que las variantes coinciden
                            v1 = self._extract_variant_tags(d1.title)
                            v2 = self._extract_variant_tags(d2.title)
                            if v1 != v2:
                                continue  # Variantes distintas → no son el mismo producto
                            fuzzy_group.append(d2)
                            used.add(j)
                    if len(fuzzy_group) > 1:
                        stores_in_group = {d.store for d in fuzzy_group}
                        if len(stores_in_group) > 1:
                            multi_store_groups.append(fuzzy_group)
                            used.add(i)

        # Paso 3: de cada grupo, extraer pares (barato, caro)
        # Sub-agrupar por variante para evitar comparar Pro vs no-Pro
        pairs: list[tuple[Deal, Deal]] = []
        for group in multi_store_groups:
            # Sub-agrupar por variante dentro del grupo
            variant_subgroups: dict[frozenset, list[Deal]] = {}
            for d in group:
                vtags = frozenset(self._extract_variant_tags(d.title))
                if vtags not in variant_subgroups:
                    variant_subgroups[vtags] = []
                variant_subgroups[vtags].append(d)

            for subgroup in variant_subgroups.values():
                stores_in_sub = {d.store for d in subgroup}
                if len(stores_in_sub) < 2:
                    continue

                sorted_group = sorted(subgroup, key=lambda d: d.current_price)
                cheapest = sorted_group[0]
                most_expensive = sorted_group[-1]

                if most_expensive.current_price <= 0:
                    continue
                if cheapest.store == most_expensive.store:
                    continue

                # No comparar reacondicionados vs nuevo (siempre será más barato)
                cheap_is_refurb = cheapest.store in self._REFURBISHED_STORES
                expensive_is_refurb = most_expensive.store in self._REFURBISHED_STORES
                if cheap_is_refurb != expensive_is_refurb:
                    continue

                diff_pct = (1 - cheapest.current_price / most_expensive.current_price) * 100
                if diff_pct >= min_discount_pct:
                    pairs.append((cheapest, most_expensive))

        logger.info(
            "Cross-store: %d pares encontrados (de %d grupos multi-tienda)",
            len(pairs), len(multi_store_groups),
        )
        return pairs

    # ------------------------------------------------------------------
    # Búsquedas para comandos de Telegram
    # ------------------------------------------------------------------
    def search_deals(self, keyword: str, limit: int = 5) -> list[Deal]:
        """Busca deals por keyword en el título."""
        cur = self.conn.cursor()
        cur.execute(
            """SELECT * FROM deals
               WHERE title LIKE ?
               ORDER BY discount_pct DESC
               LIMIT ?""",
            (f"%{keyword}%", limit),
        )
        return [self._row_to_deal(r) for r in cur.fetchall()]

    def get_recent_deals(self, hours: int = 24, limit: int = 10) -> list[Deal]:
        """Deals actualizados en las últimas N horas."""
        from datetime import timedelta

        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        cur = self.conn.cursor()
        cur.execute(
            """SELECT * FROM deals
               WHERE updated_at >= ? AND discount_pct > 0
               ORDER BY updated_at DESC
               LIMIT ?""",
            (cutoff, limit),
        )
        return [self._row_to_deal(r) for r in cur.fetchall()]

    def get_top_deals_since(self, hours: int = 24, limit: int = 10) -> list[Deal]:
        """Top deals por descuento en las últimas N horas."""
        from datetime import timedelta

        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        cur = self.conn.cursor()
        cur.execute(
            """SELECT * FROM deals
               WHERE updated_at >= ? AND discount_pct > 0
               ORDER BY discount_pct DESC
               LIMIT ?""",
            (cutoff, limit),
        )
        return [self._row_to_deal(r) for r in cur.fetchall()]

    def get_price_history(self, deal_id: int) -> list[dict[str, Any]]:
        """Historial de precios de un deal."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT price, detected_at FROM price_history WHERE deal_id = ? ORDER BY detected_at",
            (deal_id,),
        )
        return [{"price": r["price"], "detected_at": r["detected_at"]} for r in cur.fetchall()]

    def get_deal_by_id(self, deal_id: int) -> Deal | None:
        """Obtiene un deal por su ID."""
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM deals WHERE id = ?", (deal_id,))
        row = cur.fetchone()
        return self._row_to_deal(row) if row else None

    def get_store_stats(self) -> list[dict[str, Any]]:
        """Estadísticas por tienda."""
        cur = self.conn.cursor()
        cur.execute(
            """SELECT store,
                      COUNT(*) as count,
                      ROUND(AVG(discount_pct), 1) as avg_discount,
                      MAX(updated_at) as last_update
               FROM deals
               GROUP BY store
               ORDER BY count DESC""",
        )
        return [
            {
                "store": r["store"],
                "count": r["count"],
                "avg_discount": r["avg_discount"],
                "last_update": r["last_update"],
            }
            for r in cur.fetchall()
        ]

    # ------------------------------------------------------------------
    # Watchlist dinámica (SQLite)
    # ------------------------------------------------------------------
    def add_watchlist_item(self, name: str, max_price: float, min_price: float = 0) -> int:
        """Añade un producto a la watchlist. Retorna el ID."""
        now = datetime.utcnow().isoformat()
        cur = self.conn.cursor()
        cur.execute(
            """INSERT OR REPLACE INTO watchlist (name, max_price, min_price, added_at)
               VALUES (?, ?, ?, ?)""",
            (name, max_price, min_price, now),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def remove_watchlist_item(self, name: str) -> bool:
        """Elimina un producto de la watchlist. Retorna True si existía."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM watchlist WHERE LOWER(name) = LOWER(?)", (name,))
        self.conn.commit()
        return cur.rowcount > 0

    def get_watchlist_items(self) -> list[dict[str, Any]]:
        """Retorna todos los productos de la watchlist dinámica."""
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM watchlist ORDER BY added_at DESC")
        import json
        return [
            {
                "name": r["name"],
                "max_price": r["max_price"],
                "min_price": r["min_price"],
                "exclude_keywords": json.loads(r["exclude_keywords"]) if r["exclude_keywords"] else [],
            }
            for r in cur.fetchall()
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_deal(row: sqlite3.Row) -> Deal:
        return Deal(
            id=row["id"],
            title=row["title"],
            url=row["url"],
            store=row["store"],
            current_price=row["current_price"],
            original_price=row["original_price"],
            discount_pct=row["discount_pct"],
            category=row["category"],
            currency=row["currency"],
            image_url=row["image_url"],
            detected_at=datetime.fromisoformat(row["detected_at"]),
            sent_to_telegram=bool(row["sent_to_telegram"]),
        )

    def close(self) -> None:
        self.conn.close()
