"""Scraper para Cecotec.es — Next.js SSR con __NEXT_DATA__ JSON.

Cecotec usa Next.js con server-side rendering. Los datos de productos
están en el JSON de __NEXT_DATA__ dentro de props.pageProps.products.
Solo las categorías hoja (leaf) devuelven productos.

client_type: http (no necesita browser, el JSON está en el HTML estático).
"""

from __future__ import annotations

import json
import logging
import re

from ..models import Deal
from .base import BaseStore

logger = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', re.DOTALL)


class CecotecStore(BaseStore):
    """Scraper para Cecotec.es via __NEXT_DATA__ JSON."""

    async def build_urls(self) -> list[str]:
        return list(self.config.scrape_urls)

    def parse_deals(self, html: str, url: str) -> list[Deal]:
        match = _NEXT_DATA_RE.search(html)
        if not match:
            logger.warning("[cecotec] No se encontró __NEXT_DATA__ en %s", url)
            return []

        try:
            data = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            logger.warning("[cecotec] JSON inválido en __NEXT_DATA__")
            return []

        products = data.get("props", {}).get("pageProps", {}).get("products", [])
        if not products:
            logger.debug("[cecotec] 0 productos en %s", url)
            return []

        deals: list[Deal] = []
        for product in products:
            try:
                deal = self._parse_product(product)
                if deal:
                    deals.append(deal)
            except Exception:
                logger.debug("[cecotec] Error parseando producto", exc_info=True)

        return deals

    def _parse_product(self, product: dict) -> Deal | None:
        name = product.get("name") or product.get("extendedTitle", "")
        slug = product.get("slug", "")
        if not name or not slug:
            return None

        pricing = product.get("pricing", {})
        if not pricing:
            return None

        # Precio actual (IVA incluido)
        try:
            current_price = float(pricing.get("inclTax", 0))
        except (ValueError, TypeError):
            return None
        if current_price <= 0:
            return None

        # Stock
        stock = pricing.get("isInStock")
        if stock is not None and stock <= 0:
            return None

        # Precio original y descuento
        original_price = None
        discount_pct = 0.0
        try:
            orig = float(pricing.get("originalPrice", 0))
            if orig > current_price:
                original_price = orig
                discount_pct = round((1 - current_price / orig) * 100, 1)
        except (ValueError, TypeError):
            pass

        # URL del producto
        product_url = f"https://www.cecotec.es/es/{slug}"

        # Imagen
        image_url = product.get("mainImage", "")

        # Categoría
        category = product.get("category", "")

        return Deal(
            title=name,
            url=product_url,
            store="cecotec",
            current_price=current_price,
            original_price=original_price,
            discount_pct=discount_pct,
            category=category,
            image_url=image_url,
        )
