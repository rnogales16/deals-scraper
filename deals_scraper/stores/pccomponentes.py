"""Adapter PcComponentes.com — usa Playwright (browser client).

PcComponentes bloquea requests HTTP directos (Cloudflare 403).
Los productos se renderizan como <a data-product-id="..."> con todos
los datos relevantes en atributos data-*. Esto es más estable que
parsear selectores CSS internos.

Si la web cambia de estructura, actualizar el método _parse_card().
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from ..models import Deal
from .base import BaseStore

logger = logging.getLogger(__name__)

# Selector principal: enlaces con data-product-id
_SEL_PRODUCT_LINK = "a[data-product-id]"

# Selectores internos (fallback para precio original)
_SEL_CROSSED_PRICE = "[data-e2e='crossedPrice']"
_SEL_IMAGE = "img.image-UbNt7e"
_SEL_IMAGE_ALT = "div.imageContainer-Odn8PL img"


def _parse_price(text: str | None) -> float | None:
    """Extrae precio de texto como '599€' o '1.299,99€'."""
    if not text:
        return None
    cleaned = text.strip().replace("€", "").replace("\u20ac", "").replace("\xa0", "").replace(" ", "")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    elif "." in cleaned:
        # Texto en formato español: la coma es el decimal, así que un punto
        # solo (sin coma) que agrupa de 3 en 3 es separador de miles
        # ("1.999€" = 1999, "1.299.000€" = 1299000), no un decimal.
        parts = cleaned.split(".")
        if all(len(p) == 3 for p in parts[1:]):
            cleaned = cleaned.replace(".", "")
    match = re.search(r"[\d]+\.?\d*", cleaned)
    return float(match.group()) if match else None


class PcComponentesStore(BaseStore):
    """Scraper para PcComponentes.com (ofertas especiales)."""

    async def build_urls(self) -> list[str]:
        return list(self.config.scrape_urls)

    def parse_deals(self, html: str, url: str) -> list[Deal]:
        soup = BeautifulSoup(html, "lxml")
        deals: list[Deal] = []

        cards = soup.select(_SEL_PRODUCT_LINK)
        if not cards:
            logger.warning("[pccomponentes] No se encontraron tarjetas de producto")

        for card in cards:
            try:
                deal = self._parse_card(card)
                if deal:
                    deals.append(deal)
            except Exception:
                logger.debug("Error parseando tarjeta en PcComponentes", exc_info=True)

        return deals

    def _parse_card(self, card) -> Deal | None:
        # Datos directos de atributos data-*
        title = card.get("data-product-name", "").strip()
        if not title:
            return None

        href = card.get("href", "")
        product_url = href if href.startswith("http") else f"https://www.pccomponentes.com{href}"

        # Precio actual desde data-product-price
        price_str = card.get("data-product-price", "")
        if not price_str:
            return None
        try:
            current_price = float(price_str)
        except (ValueError, TypeError):
            return None

        # Descuento desde data-product-total-discount
        discount_str = card.get("data-product-total-discount", "0")
        try:
            discount_pct = float(discount_str)
        except (ValueError, TypeError):
            discount_pct = 0.0

        # Precio original — extraer del HTML interno (tachado)
        original_price = None
        crossed_el = card.select_one(_SEL_CROSSED_PRICE)
        if crossed_el:
            original_price = _parse_price(crossed_el.get_text())

        # Si no hay precio tachado pero hay descuento, calcular original
        if original_price is None and discount_pct > 0:
            original_price = round(current_price / (1 - discount_pct / 100), 2)

        # Categoría
        category = card.get("data-product-category", "")

        # Imagen
        img_el = card.select_one(_SEL_IMAGE) or card.select_one(_SEL_IMAGE_ALT)
        image_url = ""
        if img_el:
            image_url = img_el.get("src", "") or img_el.get("data-src", "")

        return Deal(
            title=title,
            url=product_url,
            store="pccomponentes",
            current_price=current_price,
            original_price=original_price,
            discount_pct=discount_pct,
            category=category,
            image_url=image_url,
        )
