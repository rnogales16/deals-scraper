"""Scraper para Lidl.es — usa la API interna de búsqueda (JSON).

Lidl.es expone un endpoint de búsqueda público sin autenticación:
    /q/api/search?assortment=ES&locale=es_ES&version=v2.0.0&q=...

Devuelve JSON con productos, precios, descuentos e imágenes.
No necesita browser (client_type: http).
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote_plus

from ..models import Deal
from .base import BaseStore

logger = logging.getLogger(__name__)

# Headers específicos para la API de Lidl (evita 503)
_API_HEADERS = {
    "Accept": "application/json",
}

_API_BASE = "https://www.lidl.es/q/api/search"
_PARAMS = "assortment=ES&locale=es_ES&version=v2.0.0"


class LidlStore(BaseStore):
    """Scraper para Lidl.es via API de búsqueda interna."""

    async def _fetch(self, url: str) -> str:
        """Override: skip homepage priming (Lidl homepage returns 503)."""
        if not self.http_client:
            raise RuntimeError("[lidl] Necesita http_client")
        return await self.http_client.fetch(url, prime=False)

    async def build_urls(self) -> list[str]:
        """Convierte scrape_urls (queries de búsqueda) en URLs de API."""
        api_urls: list[str] = []
        for raw_url in self.config.scrape_urls:
            # scrape_urls contiene queries como "electronica", "hogar", etc.
            query = raw_url.strip()
            api_url = f"{_API_BASE}?{_PARAMS}&q={quote_plus(query)}"
            api_urls.append(api_url)
        return api_urls

    def parse_deals(self, raw: str, url: str) -> list[Deal]:
        """Parsea la respuesta JSON de la API de Lidl."""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("[lidl] API no devolvió JSON válido")
            return []

        items = data.get("items", [])
        if not items:
            logger.debug("[lidl] 0 items en respuesta API para %s", url)
            return []

        deals: list[Deal] = []
        for item in items:
            try:
                deal = self._parse_item(item)
                if deal:
                    deals.append(deal)
            except Exception:
                logger.debug("[lidl] Error parseando item", exc_info=True)

        return deals

    def _parse_item(self, item: dict) -> Deal | None:
        """Convierte un item de la API en un Deal."""
        gridbox = item.get("gridbox", {}).get("data", {})
        if not gridbox:
            return None

        title = gridbox.get("fullTitle") or gridbox.get("title", "")
        if not title:
            return None

        # URL del producto
        canonical = gridbox.get("canonicalUrl", "")
        if not canonical:
            return None
        product_url = f"https://www.lidl.es{canonical}" if canonical.startswith("/") else canonical

        # Precio
        price_data = gridbox.get("price", {})
        if not price_data:
            return None

        current_price = price_data.get("price")
        if not current_price or current_price <= 0:
            return None

        # Precio original y descuento
        original_price = None
        discount_pct = 0.0
        discount = price_data.get("discount", {})
        if discount:
            deleted_price = discount.get("deletedPrice")
            if deleted_price and deleted_price > current_price:
                original_price = deleted_price
            pct = discount.get("percentageDiscount", 0)
            if pct:
                discount_pct = float(pct)

        # Imagen
        image_url = gridbox.get("image", "")

        # Categoría
        category = ""
        cat_path = gridbox.get("category", "")
        if cat_path:
            # "Categorias/Electronica/..." → última parte
            parts = cat_path.split("/")
            category = parts[-1] if len(parts) > 1 else parts[0]

        return Deal(
            title=title,
            url=product_url,
            store="lidl",
            current_price=current_price,
            original_price=original_price,
            discount_pct=discount_pct,
            image_url=image_url,
            category=category,
        )
