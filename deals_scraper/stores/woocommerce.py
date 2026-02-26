"""Scraper para tiendas WooCommerce que exponen la Store API pública.

Usa el endpoint /wp-json/wc/store/products que devuelve JSON limpio
con precios, permalinks, categorías e imágenes — sin necesidad de
parsear HTML.

Tiendas compatibles: lifeinformatica (y cualquier WooCommerce con Store API).
"""

from __future__ import annotations

import html
import json
import logging
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from ..models import Deal
from .base import BaseStore

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Máximo de productos por página que acepta la WooCommerce Store API
_MAX_PER_PAGE = 100

# Máximo de páginas a recorrer (seguridad contra loops infinitos)
_MAX_PAGES = 10


class WooCommerceStore(BaseStore):
    """Scraper para tiendas WooCommerce via Store REST API.

    En config.yaml, las scrape_urls deben apuntar al dominio base de la
    tienda (ej: "https://www.lifeinformatica.com/"). El scraper construye
    automáticamente la URL de la API.

    Requiere client_type: http (no necesita browser).
    """

    async def build_urls(self) -> list[str]:
        """Construye URLs de la API paginadas."""
        api_urls: list[str] = []
        for base_url in self.config.scrape_urls:
            api_base = self._build_api_url(base_url)
            # Empezar con página 1; se pagina en scrape() override
            api_urls.append(api_base)
        return api_urls

    def _build_api_url(self, base_url: str) -> str:
        """Construye la URL de la WooCommerce Store API desde una URL base."""
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return f"{origin}/wp-json/wc/store/products"

    async def scrape(self) -> list[Deal]:
        """Override de scrape() para manejar paginación de la API."""
        all_deals: list[Deal] = []

        for base_url in self.config.scrape_urls:
            api_base = self._build_api_url(base_url)
            page = 1

            while page <= _MAX_PAGES:
                url = f"{api_base}?per_page={_MAX_PER_PAGE}&page={page}"
                try:
                    raw = await self._fetch(url)
                    products = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(
                        "[%s] API no devolvió JSON válido (página %d)",
                        self.name, page,
                    )
                    break
                except Exception:
                    logger.exception("[%s] Error fetching API page %d", self.name, page)
                    break

                if not isinstance(products, list) or not products:
                    break

                deals = self._parse_products(products)
                all_deals.extend(deals)
                logger.info(
                    "[%s] API página %d: %d productos → %d deals",
                    self.name, page, len(products), len(deals),
                )

                # Si devolvió menos del máximo, no hay más páginas
                if len(products) < _MAX_PER_PAGE:
                    break
                page += 1

        logger.info("[%s] Total: %d deals via WooCommerce API", self.name, len(all_deals))
        return all_deals

    def _parse_products(self, products: list[dict]) -> list[Deal]:
        """Convierte productos de la API en Deal objects."""
        deals: list[Deal] = []
        for product in products:
            try:
                deal = self._product_to_deal(product)
                if deal:
                    deals.append(deal)
            except Exception:
                logger.debug(
                    "[%s] Error parseando producto API: %s",
                    self.name, product.get("name", "?"), exc_info=True,
                )
        return deals

    def _product_to_deal(self, product: dict) -> Deal | None:
        """Convierte un producto de la WooCommerce Store API en un Deal."""
        name = html.unescape(product.get("name", "")).strip()
        if not name:
            return None

        permalink = product.get("permalink", "")
        if not permalink:
            return None

        # Precios: vienen en centavos (currency_minor_unit: 2)
        prices = product.get("prices", {})
        if not prices:
            return None

        minor_unit = prices.get("currency_minor_unit", 2)
        divisor = 10 ** minor_unit

        raw_price = prices.get("price", "")
        raw_regular = prices.get("regular_price", "")
        raw_sale = prices.get("sale_price", "")

        try:
            current_price = int(raw_price) / divisor
        except (ValueError, TypeError):
            return None

        if current_price <= 0:
            return None

        # Original price: solo si es mayor que el precio actual
        original_price: float | None = None
        try:
            regular = int(raw_regular) / divisor
            if regular > current_price:
                original_price = regular
        except (ValueError, TypeError):
            pass

        # Solo productos en stock
        if not product.get("is_in_stock", True):
            return None

        # Imagen
        images = product.get("images", [])
        image_url = ""
        if images and isinstance(images, list):
            image_url = images[0].get("src", "") or images[0].get("thumbnail", "")

        # Categoría (primera categoría del producto)
        categories = product.get("categories", [])
        category = ""
        if categories and isinstance(categories, list):
            category = categories[-1].get("name", "")  # La más específica suele ser la última

        return Deal(
            title=name,
            url=permalink,
            store=self.name,
            current_price=current_price,
            original_price=original_price,
            image_url=image_url,
            category=category,
        )

    def parse_deals(self, html: str, url: str) -> list[Deal]:
        """No usado directamente — scrape() override maneja todo."""
        # Fallback por si se llama directamente
        try:
            products = json.loads(html)
            if isinstance(products, list):
                return self._parse_products(products)
        except (json.JSONDecodeError, TypeError):
            pass
        return []
