"""Clase base abstracta para scrapers de tiendas."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from ..browser_client import BrowserClient
    from ..http_client import HttpClient
    from ..models import StoreConfig

from ..filters import infer_category_from_title, infer_category_from_url, normalize_category
from ..models import Deal

logger = logging.getLogger(__name__)

# Semáforo por defecto para URLs dentro de una tienda
_DEFAULT_URL_CONCURRENCY = 3


class BaseStore(ABC):
    """Interfaz común para todos los scrapers de tiendas.

    Patrón Template Method:
        scrape() orquesta: build_urls() → fetch cada URL → parse_deals()
    """

    def __init__(
        self,
        config: StoreConfig,
        http_client: HttpClient | None = None,
        browser_client: BrowserClient | None = None,
        max_concurrent_urls: int = _DEFAULT_URL_CONCURRENCY,
    ) -> None:
        self.config = config
        self.http_client = http_client
        self.browser_client = browser_client
        self._max_concurrent_urls = max_concurrent_urls
        self._url_semaphore: asyncio.Semaphore | None = None

    @property
    def name(self) -> str:
        return self.config.name

    # ------------------------------------------------------------------
    # Métodos abstractos — cada tienda los implementa
    # ------------------------------------------------------------------
    @abstractmethod
    async def build_urls(self) -> list[str]:
        """Construye la lista de URLs a scrapear."""
        ...

    @abstractmethod
    def parse_deals(self, html: str, url: str) -> list[Deal]:
        """Parsea el HTML y extrae ofertas."""
        ...

    # ------------------------------------------------------------------
    # Template method
    # ------------------------------------------------------------------
    async def scrape(self) -> list[Deal]:
        """Ejecuta el ciclo completo: build_urls → fetch → parse → categorizar (URLs en paralelo)."""
        urls = await self.build_urls()

        async def _scrape_url(url: str) -> list[Deal]:
            if self._url_semaphore is None:
                self._url_semaphore = asyncio.Semaphore(self._max_concurrent_urls)
            async with self._url_semaphore:
                try:
                    html = await self._fetch(url)
                    deals = self.parse_deals(html, url)
                    # Inferir categorías para deals sin categoría
                    url_category = infer_category_from_url(url)
                    for deal in deals:
                        if deal.category:
                            deal.category = normalize_category(deal.category)
                        if not deal.category and url_category:
                            deal.category = url_category
                        if not deal.category:
                            deal.category = infer_category_from_title(deal.title)
                    logger.info("[%s] %d ofertas encontradas en %s", self.name, len(deals), url)
                    return deals
                except Exception:
                    logger.exception("[%s] Error al scrapear %s", self.name, url)
                    return []

        results = await asyncio.gather(*[_scrape_url(u) for u in urls])

        all_deals: list[Deal] = []
        for deals in results:
            all_deals.extend(deals)

        # Filtrar deals cuya URL parece una página de listado/categoría
        before = len(all_deals)
        all_deals = [d for d in all_deals if _looks_like_product_url(d.url)]
        filtered = before - len(all_deals)
        if filtered:
            logger.info(
                "[%s] %d deals descartados por URL no-producto (listado/categoría)",
                self.name, filtered,
            )
        return all_deals

    async def _fetch(self, url: str) -> str:
        """Elige el cliente correcto según client_type."""
        if self.config.client_type == "browser":
            if not self.browser_client:
                raise RuntimeError(f"[{self.name}] Necesita browser_client pero no se proporcionó")
            return await self.browser_client.fetch(
                url,
                force_stealth=self.config.force_stealth,
                wait_for_selector=self.config.wait_for_selector,
            )
        else:
            if not self.http_client:
                raise RuntimeError(f"[{self.name}] Necesita http_client pero no se proporcionó")
            return await self.http_client.fetch(url)


# Segmentos de URL que indican páginas de listado/categoría (no productos)
_LISTING_SEGMENTS = {
    "ofertas", "offers", "deals", "destacados", "featured",
    "mas-vendidos", "best-sellers", "bestsellers", "top-ventas",
    "novedades", "new-arrivals", "rebajas", "sale", "sales",
    "outlet", "promociones", "promos",
}


def _looks_like_product_url(url: str) -> bool:
    """Heurística: ¿parece una URL de producto individual?

    Rechaza URLs que son claramente páginas de listado/categoría/homepage.
    Solo filtra URLs obviamente no-producto; en caso de duda, deja pasar.
    """
    parsed = urlparse(url)
    path = parsed.path.strip("/")

    # Homepage sin path → no es un producto
    if not path:
        return False

    segments = path.split("/")

    # Path de un solo segmento genérico (ej: /destacados/, /mas-vendidos/)
    if len(segments) == 1 and segments[0].lower() in _LISTING_SEGMENTS:
        return False

    return True
