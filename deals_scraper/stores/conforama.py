"""Scraper para Conforama.es — HTML server-rendered con data-gtm-detail-* attributes.

Cada producto está en un <a> con atributos GTM que contienen ID, nombre, precio,
marca, categoría y estado de promoción. Precios originales en div.original-price.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from ..models import Deal
from .base import BaseStore

logger = logging.getLogger(__name__)

_SEL_PRODUCT = "a[data-gtm-detail-id]"


def _parse_price(text: str | None) -> float | None:
    """Extrae precio de texto como '28,90 €' o '1.299,99 €'."""
    if not text:
        return None
    cleaned = text.strip().replace("€", "").replace("\xa0", "").replace(" ", "")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    match = re.search(r"[\d]+\.?\d*", cleaned)
    return float(match.group()) if match else None


class ConforamaStore(BaseStore):
    """Scraper para Conforama.es (electrónica, muebles, electrodomésticos)."""

    async def build_urls(self) -> list[str]:
        return list(self.config.scrape_urls)

    def parse_deals(self, html: str, url: str) -> list[Deal]:
        soup = BeautifulSoup(html, "lxml")
        deals: list[Deal] = []

        cards = soup.select(_SEL_PRODUCT)
        if not cards:
            logger.warning("[conforama] No se encontraron productos en %s", url)
            return deals

        for card in cards:
            try:
                deal = self._parse_card(card)
                if deal:
                    deals.append(deal)
            except Exception:
                logger.debug("Error parseando tarjeta Conforama", exc_info=True)

        return deals

    def _parse_card(self, card) -> Deal | None:
        title = card.get("data-gtm-detail-name", "").strip()
        if not title:
            return None

        # Precio actual
        price_str = card.get("data-gtm-detail-price", "")
        current_price = _parse_price(price_str)
        if not current_price or current_price <= 0:
            return None

        # URL del producto
        href = card.get("href", "")
        if not href:
            return None
        product_url = href if href.startswith("http") else f"https://www.conforama.es{href}"

        # Precio original (tachado)
        original_price = None
        original_el = card.select_one("div.original-price")
        if original_el:
            original_price = _parse_price(original_el.get_text())

        # Categoría
        category = card.get("data-gtm-detail-category", "")
        if "/" in category:
            parts = category.split("/")
            category = parts[-1] if len(parts) > 1 else parts[0]

        # Marca
        brand = card.get("data-gtm-detail-brand", "")

        # Imagen
        image_url = ""
        img_el = card.select_one("div.image-holder img")
        if img_el:
            image_url = img_el.get("src", "") or img_el.get("data-src", "")

        # Descuento
        discount_pct = 0.0
        if original_price and original_price > current_price:
            discount_pct = round((1 - current_price / original_price) * 100, 1)

        # Añadir marca al título si no está incluida
        if brand and brand.lower() not in title.lower():
            title = f"{brand.capitalize()} {title}"

        return Deal(
            title=title,
            url=product_url,
            store="conforama",
            current_price=current_price,
            original_price=original_price,
            discount_pct=discount_pct,
            category=category,
            image_url=image_url,
        )
