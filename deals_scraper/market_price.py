"""Validación de precios de mercado via cache, cross-store y Idealo.es.

Cadena de validación en 3 niveles:
1. Cache (SQLite, TTL 7 días)
2. Cross-store interno (fuzzy match en nuestra BD, >=2 tiendas)
3. Idealo.es (Playwright stealth, max 10/ciclo, 3 concurrentes)
"""

from __future__ import annotations

import asyncio
import logging
import re
import statistics
import unicodedata
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .browser_client import BrowserClient
    from .database import Database

from .models import Deal

logger = logging.getLogger(__name__)

# Tiendas refurbished: market_price es de producto nuevo, no comparable
_REFURBISHED_STORES = {"backmarket", "apple", "cex"}

# Colores a eliminar de títulos para normalización
_COLORS = {
    "negro", "black", "azul", "blue", "rojo", "red", "blanco", "white",
    "gris", "grey", "gray", "verde", "green", "rosa", "pink", "morado",
    "purple", "amarillo", "yellow", "naranja", "orange", "plata", "silver",
    "oro", "gold", "dorado", "titanio", "titanium", "grafito", "graphite",
    "midnight", "starlight", "lavanda", "lavender", "coral", "cream",
    "crema", "beige", "marfil", "ivory",
}

# Condiciones/estados a eliminar
_CONDITIONS = {
    "reacondicionado", "renewed", "refurbished", "usado", "used",
    "seminuevo", "como nuevo", "like new", "very good", "good",
    "excelente", "excellent", "fair", "aceptable",
}

# Junk patterns a eliminar (brackets, shipping, etc.)
_JUNK_PATTERNS = [
    re.compile(r"\[.*?\]"),              # [Enviado por Amazon]
    re.compile(r"\(.*?\)"),              # (Reacondicionado)
    re.compile(r"env[ií]o\s*gratis", re.IGNORECASE),
    re.compile(r"free\s*shipping", re.IGNORECASE),
    re.compile(r"garantía\s*\d+", re.IGNORECASE),
    re.compile(r"#\w+"),                 # Hashtags
]


# ------------------------------------------------------------------
# Title normalization
# ------------------------------------------------------------------
def normalize_title(title: str) -> str:
    """Normaliza un título de producto para cache/matching.

    Steps:
    1. Lowercase + strip accents
    2. Normalize storage: "256 GB" -> "256gb"
    3. Remove colors, conditions, junk
    4. Sort tokens alphabetically
    """
    # Lowercase
    text = title.lower().strip()

    # Strip accents (á->a, ñ->n, etc.)
    text = "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )

    # Normalize storage: "256 gb" -> "256gb", "1 tb" -> "1tb"
    text = re.sub(r"(\d+)\s*(gb|tb)\b", r"\1\2", text)

    # Remove junk patterns
    for pat in _JUNK_PATTERNS:
        text = pat.sub("", text)

    # Tokenize
    tokens = text.split()

    # Remove colors and conditions
    tokens = [t for t in tokens if t not in _COLORS and t not in _CONDITIONS]

    # Remove single-char tokens and common junk
    tokens = [t for t in tokens if len(t) > 1 or t.isdigit()]

    # Remove non-alphanumeric characters from tokens, keep digits
    cleaned_tokens = []
    for t in tokens:
        cleaned = re.sub(r"[^a-z0-9]", "", t)
        if cleaned:
            cleaned_tokens.append(cleaned)

    # Sort alphabetically for order-independent matching
    cleaned_tokens.sort()

    return " ".join(cleaned_tokens)


# ------------------------------------------------------------------
# Market Price Cache (SQLite)
# ------------------------------------------------------------------
class MarketPriceCache:
    """Cache de precios de mercado en SQLite (tabla market_prices)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, normalized_title: str) -> tuple[float, str] | None:
        """Busca en cache: exacto primero, luego fuzzy >= 90.

        Returns:
            (price, source) or None if not found.
        """
        now = datetime.utcnow().isoformat()
        cur = self.db.conn.cursor()

        # Exact match
        cur.execute(
            "SELECT market_price, source FROM market_prices "
            "WHERE normalized_title = ? AND expires_at > ? "
            "ORDER BY created_at DESC LIMIT 1",
            (normalized_title, now),
        )
        row = cur.fetchone()
        if row:
            return row["market_price"], f"cache({row['source']})"

        # Fuzzy match
        try:
            from thefuzz import fuzz
        except ImportError:
            return None

        cur.execute(
            "SELECT normalized_title, market_price, source FROM market_prices "
            "WHERE expires_at > ?",
            (now,),
        )
        best_ratio = 0
        best_row = None
        for row in cur.fetchall():
            ratio = fuzz.token_set_ratio(normalized_title, row["normalized_title"])
            if ratio >= 90 and ratio > best_ratio:
                best_ratio = ratio
                best_row = row

        if best_row:
            return best_row["market_price"], f"cache({best_row['source']})"

        return None

    def put(
        self, normalized_title: str, price: float, source: str,
        source_detail: str = "", ttl_days: int = 7,
    ) -> None:
        """Guarda un precio de mercado en cache."""
        now = datetime.utcnow()
        expires = now + timedelta(days=ttl_days)
        cur = self.db.conn.cursor()
        cur.execute(
            "INSERT INTO market_prices "
            "(normalized_title, market_price, source, source_detail, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (normalized_title, price, source, source_detail,
             now.isoformat(), expires.isoformat()),
        )
        self.db.conn.commit()

    def cleanup(self) -> None:
        """Borra entradas expiradas."""
        now = datetime.utcnow().isoformat()
        cur = self.db.conn.cursor()
        cur.execute("DELETE FROM market_prices WHERE expires_at < ?", (now,))
        deleted = cur.rowcount
        self.db.conn.commit()
        if deleted:
            logger.debug("Market price cache: %d entradas expiradas eliminadas", deleted)


# ------------------------------------------------------------------
# Cross-store lookup
# ------------------------------------------------------------------
def _lookup_cross_store(
    deal: Deal, db: Database,
    fuzzy_threshold: int = 80,
) -> float | None:
    """Busca el mismo producto en OTRAS tiendas de nuestra BD.

    Requires >= 2 matches from different stores.
    Returns median of current_prices found, or None.
    """
    try:
        from thefuzz import fuzz
    except ImportError:
        return None

    cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
    cur = db.conn.cursor()
    cur.execute(
        "SELECT title, store, current_price FROM deals "
        "WHERE store != ? AND updated_at >= ? AND current_price > 0",
        (deal.store, cutoff),
    )
    rows = cur.fetchall()
    if not rows:
        return None

    deal_variant_tags = db._extract_variant_tags(deal.title)
    matches: list[float] = []
    seen_stores: set[str] = set()

    for row in rows:
        # Skip same store
        if row["store"] == deal.store:
            continue

        ratio = fuzz.token_set_ratio(deal.title, row["title"])
        if ratio < fuzzy_threshold:
            continue

        # Variant tags must match
        row_tags = db._extract_variant_tags(row["title"])
        if deal_variant_tags != row_tags:
            continue

        # Don't count multiple matches from same store
        if row["store"] in seen_stores:
            continue
        seen_stores.add(row["store"])
        matches.append(row["current_price"])

    # Need >= 2 matches from different stores
    if len(matches) < 2:
        return None

    return statistics.median(matches)


# ------------------------------------------------------------------
# Idealo scraper
# ------------------------------------------------------------------
def _build_idealo_query(title: str) -> str:
    """Construye una query limpia para Idealo a partir del título.

    Elimina colores, condiciones, y junk pero mantiene orden natural
    (no ordena alfabéticamente como normalize_title).
    """
    text = title.strip()

    # Strip accents
    text = "".join(
        c for c in unicodedata.normalize("NFD", text.lower())
        if unicodedata.category(c) != "Mn"
    )

    # Normalize storage
    text = re.sub(r"(\d+)\s*(gb|tb)\b", r"\1\2", text)

    # Remove junk patterns
    for pat in _JUNK_PATTERNS:
        text = pat.sub("", text)

    # Tokenize
    tokens = text.split()

    # Remove colors and conditions
    tokens = [t for t in tokens if t not in _COLORS and t not in _CONDITIONS]

    # Remove non-alphanumeric (keep digits and letters)
    cleaned = []
    for t in tokens:
        c = re.sub(r"[^a-z0-9]", "", t)
        if c and len(c) > 1:
            cleaned.append(c)

    return " ".join(cleaned[:10])  # Limit to first 10 tokens


class IdealoScraper:
    """Scraper de precios de Idealo.es via Playwright stealth.

    Usa Google como intermediario para evitar el WAF de Idealo:
    busca "site:idealo.es {product}" y extrae precios de los snippets.
    Si Google no devuelve nada, intenta acceso directo a Idealo.
    """

    def __init__(
        self, browser_client: BrowserClient,
        max_per_cycle: int = 10,
        max_concurrent: int = 3,
    ) -> None:
        self._browser = browser_client
        self._max_per_cycle = max_per_cycle
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._lookups_done = 0

    async def lookup(self, deal: Deal) -> float | None:
        """Busca el precio de mercado de un deal en Idealo via Google.

        Returns:
            Median price found, or None on error/not found.
        """
        # Check limit INSIDE semaphore to avoid race condition
        query = _build_idealo_query(deal.title)
        if not query:
            return None

        async with self._semaphore:
            if self._lookups_done >= self._max_per_cycle:
                return None
            self._lookups_done += 1

            try:
                # Strategy 1: Google search for Idealo prices
                price = await self._lookup_via_google(query, deal.title)
                if price:
                    return price

                # Strategy 2: Direct Idealo access (may be blocked)
                price = await self._lookup_direct(query, deal.title)
                return price
            except Exception:
                logger.debug("Idealo error para: %s", deal.title[:50], exc_info=True)
                return None

    async def _lookup_via_google(self, query: str, title: str) -> float | None:
        """Busca precios de Idealo via Google snippets."""
        from urllib.parse import quote
        google_url = (
            f"https://www.google.es/search?q=site%3Aidealo.es+{quote(query)}"
            f"&hl=es&gl=es"
        )
        try:
            html = await asyncio.wait_for(
                self._browser.fetch(google_url, force_stealth=True),
                timeout=30,
            )
            prices = self._extract_prices_from_google(html)
            if prices:
                logger.debug(
                    "Idealo (via Google): %s -> %d precios encontrados",
                    title[:50], len(prices),
                )
                return self._compute_median(prices)
        except asyncio.TimeoutError:
            logger.warning("Google/Idealo timeout para: %s", title[:50])
        except Exception:
            logger.debug("Google/Idealo error para: %s", title[:50], exc_info=True)
        return None

    async def _lookup_direct(self, query: str, title: str) -> float | None:
        """Acceso directo a Idealo (fallback, puede ser bloqueado por WAF)."""
        from urllib.parse import quote
        url = f"https://www.idealo.es/cat/0/{quote(query)}.html"
        try:
            html = await asyncio.wait_for(
                self._browser.fetch(url, force_stealth=True),
                timeout=30,
            )
            prices = self._extract_prices_from_idealo(html)
            if prices:
                return self._compute_median(prices)
        except asyncio.TimeoutError:
            logger.warning("Idealo directo timeout para: %s", title[:50])
        except Exception:
            logger.debug("Idealo directo error para: %s", title[:50], exc_info=True)
        return None

    @staticmethod
    def _extract_prices_from_google(html: str) -> list[float]:
        """Extrae precios en € de snippets de Google."""
        from .stores.generic import _parse_price

        prices: list[float] = []
        # Google snippets show prices like "desde 299,99 €" or "299,99 €"
        for match in re.finditer(r"(\d[\d.,]*)\s*€", html):
            p = _parse_price(match.group(0))
            if p and p > 1:  # Ignore sub-1€ noise
                prices.append(p)
        return prices

    @staticmethod
    def _extract_prices_from_idealo(html: str) -> list[float]:
        """Extrae precios de la página de resultados de Idealo.

        Estrategia multicapa:
        1. [data-testid="resultItem"] containers
        2. [class*="price"] dentro de items
        3. Regex fallback: /(\\d[\\d.,]*)\\s*€/
        """
        from .stores.generic import _parse_price

        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return []

        soup = BeautifulSoup(html, "lxml")
        prices: list[float] = []

        # Strategy 1: data-testid result items
        items = soup.select("[data-testid='resultItem']")
        if items:
            for item in items[:20]:
                price_el = item.select_one("[class*='price']")
                if price_el:
                    p = _parse_price(price_el.get_text())
                    if p:
                        prices.append(p)

        # Strategy 2: generic price elements if S1 found nothing
        if not prices:
            for el in soup.select("[class*='price']")[:30]:
                text = el.get_text().strip()
                if "\u20ac" in text:
                    p = _parse_price(text)
                    if p:
                        prices.append(p)

        # Strategy 3: regex fallback
        if not prices:
            for match in re.finditer(r"(\d[\d.,]*)\s*\u20ac", html):
                p = _parse_price(match.group(0))
                if p:
                    prices.append(p)

        return prices

    @staticmethod
    def _compute_median(prices: list[float]) -> float | None:
        """Calcula mediana filtrando outliers."""
        if not prices:
            return None
        median = statistics.median(prices)
        filtered = [p for p in prices if median * 0.1 <= p <= median * 10]
        if not filtered:
            return None
        return statistics.median(filtered)

    @property
    def lookups_remaining(self) -> int:
        return max(0, self._max_per_cycle - self._lookups_done)


# ------------------------------------------------------------------
# Orchestrator
# ------------------------------------------------------------------
class MarketPriceChecker:
    """Orquestador de validación de precios de mercado.

    Recorre la cadena: cache -> cross-store -> Idealo
    para enriquecer deals con market_price.
    """

    def __init__(
        self,
        db: Database,
        browser_client: BrowserClient,
        max_idealo_per_cycle: int = 10,
        cache_ttl_days: int = 7,
        cross_store_fuzzy_threshold: int = 80,
    ) -> None:
        self.db = db
        self.cache = MarketPriceCache(db)
        self.idealo = IdealoScraper(
            browser_client, max_per_cycle=max_idealo_per_cycle,
        )
        self.cache_ttl_days = cache_ttl_days
        self.cross_store_fuzzy_threshold = cross_store_fuzzy_threshold

    async def enrich_deals(self, deals: list[Deal]) -> None:
        """Modifica deals in-place, asignando market_price donde posible.

        Agrupa deals por título normalizado para evitar lookups duplicados.
        Salta tiendas refurbished (market_price nuevo no es comparable).
        """
        # Cleanup expired cache entries (1x per cycle)
        self.cache.cleanup()

        # Group deals by normalized title
        groups: dict[str, list[Deal]] = {}
        for deal in deals:
            # Skip refurbished stores
            if deal.store in _REFURBISHED_STORES:
                continue
            key = normalize_title(deal.title)
            if not key:
                continue
            if key not in groups:
                groups[key] = []
            groups[key].append(deal)

        logger.info(
            "Market price: %d grupos únicos de %d deals (excl. refurbished)",
            len(groups), len(deals),
        )

        # Process each group through the chain
        idealo_tasks: list[tuple[str, Deal, list[Deal]]] = []

        for norm_title, group_deals in groups.items():
            representative = group_deals[0]

            # Level 1: Cache
            cached = self.cache.get(norm_title)
            if cached:
                price, source = cached
                if self._sanity_check(price, representative.current_price):
                    for d in group_deals:
                        d.market_price = price
                        d.market_price_source = source
                    logger.debug(
                        "MARKET CACHE: %s -> %.2f€ (%s)",
                        representative.title[:50], price, source,
                    )
                    continue

            # Level 2: Cross-store
            cross_price = _lookup_cross_store(
                representative, self.db,
                fuzzy_threshold=self.cross_store_fuzzy_threshold,
            )
            if cross_price and self._sanity_check(cross_price, representative.current_price):
                for d in group_deals:
                    d.market_price = cross_price
                    d.market_price_source = "cross_store"
                # Save to cache
                self.cache.put(
                    norm_title, cross_price, "cross_store",
                    ttl_days=self.cache_ttl_days,
                )
                logger.debug(
                    "MARKET CROSS-STORE: %s -> %.2f€",
                    representative.title[:50], cross_price,
                )
                continue

            # Level 3: Queue for Idealo (async)
            if self.idealo.lookups_remaining > 0:
                idealo_tasks.append((norm_title, representative, group_deals))

        # Execute Idealo lookups concurrently (cap to max_per_cycle)
        idealo_tasks = idealo_tasks[:self.idealo.lookups_remaining]
        if idealo_tasks:
            logger.info(
                "Market price: %d Idealo lookups a ejecutar (cuota: %d)",
                len(idealo_tasks), self.idealo.lookups_remaining,
            )

            async def _do_idealo(
                norm_title: str, representative: Deal, group_deals: list[Deal],
            ) -> None:
                price = await self.idealo.lookup(representative)
                if price and self._sanity_check(price, representative.current_price):
                    for d in group_deals:
                        d.market_price = price
                        d.market_price_source = "idealo"
                    self.cache.put(
                        norm_title, price, "idealo",
                        ttl_days=self.cache_ttl_days,
                    )
                    logger.info(
                        "MARKET IDEALO: %s -> %.2f€",
                        representative.title[:50], price,
                    )

            await asyncio.gather(
                *[_do_idealo(nt, rep, gd) for nt, rep, gd in idealo_tasks],
                return_exceptions=True,
            )

        # Summary
        enriched = sum(1 for d in deals if d.market_price is not None)
        logger.info(
            "Market price: %d/%d deals enriquecidos con precio de mercado",
            enriched, len(deals),
        )

    @staticmethod
    def _sanity_check(market_price: float, current_price: float) -> bool:
        """Descarta market_price si es absurdamente diferente del current_price."""
        if current_price <= 0 or market_price <= 0:
            return False
        ratio = market_price / current_price
        # Reject if market price is < 10% or > 10x the current price
        return 0.1 <= ratio <= 10
