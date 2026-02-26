"""Scraper para Decathlon España — parsea product-cards del HTML renderizado.

Decathlon usa un frontend SPA (Svelte/Next) que renderiza los productos
directamente en el HTML con clases CSS bien definidas:
  - .product-card  → contenedor
  - .vp-price-amount--sale  → precio actual (con descuento)
  - .vp-price-amount  → precio normal (sin descuento)
  - .vp-price-barred-amount  → precio original tachado
  - a[href*="/p/"]  → URL del producto
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from ..models import Deal
from .base import BaseStore

logger = logging.getLogger(__name__)

_PRICE_RE = re.compile(r"([\d.,]+)\s*€")


def _parse_price(text: str) -> float | None:
    """Extrae un precio en euros de un string como '499,99 €'."""
    m = _PRICE_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


class DecathlonStore(BaseStore):
    """Parsea las product-cards del HTML renderizado de Decathlon."""

    async def build_urls(self) -> list[str]:
        return list(self.config.scrape_urls)

    def parse_deals(self, html: str, url: str) -> list[Deal]:
        soup = BeautifulSoup(html, "html.parser")
        base = urlparse(url)
        base_url = f"{base.scheme}://{base.netloc}"

        deals: list[Deal] = []
        seen_urls: set[str] = set()

        # Cada producto está en un contenedor con clase product-card
        cards = soup.select(".product-card-details__item__price")
        for price_container in cards:
            # Subir al contenedor padre del card
            card = self._find_card_parent(price_container)
            if not card:
                continue

            deal = self._card_to_deal(card, base_url)
            if deal and deal.url not in seen_urls:
                seen_urls.add(deal.url)
                deals.append(deal)

        return deals

    @staticmethod
    def _find_card_parent(el: Tag) -> Tag | None:
        """Sube por el DOM hasta encontrar el contenedor .product-card."""
        parent = el
        for _ in range(15):
            parent = parent.parent  # type: ignore[assignment]
            if not parent or not isinstance(parent, Tag):
                return None
            classes = parent.get("class", [])
            if isinstance(classes, list) and "product-card" in classes:
                return parent
        return None

    def _card_to_deal(self, card: Tag, base_url: str) -> Deal | None:
        """Extrae un Deal de un elemento product-card."""
        # --- URL ---
        link = card.select_one('a[href*="/p/"]')
        if not link:
            return None
        href = link.get("href", "")
        if not href:
            return None
        product_url = href if href.startswith("http") else f"{base_url}{href}"

        # --- Título ---
        title = ""
        title_el = card.select_one(
            ".product-card-details__item__title, h2, h3"
        )
        if title_el:
            title = title_el.get_text(strip=True)
        if not title:
            title = link.get_text(strip=True)
        if not title:
            return None

        # --- Precio actual ---
        price_el = card.select_one(".vp-price-amount--sale")
        if not price_el:
            price_el = card.select_one(".vp-price-amount")
        if not price_el:
            return None
        current_price = _parse_price(price_el.get_text())
        if not current_price or current_price <= 0:
            return None

        # --- Precio original (tachado) ---
        original_price: float | None = None
        orig_el = card.select_one(".vp-price-barred-amount")
        if orig_el:
            original_price = _parse_price(orig_el.get_text())
            if original_price and original_price <= current_price:
                original_price = None

        # --- Imagen ---
        image_url = ""
        img = card.select_one("img")
        if img:
            image_url = img.get("src", "") or img.get("data-src", "") or ""

        return Deal(
            title=title,
            url=product_url,
            store=self.name,
            current_price=current_price,
            original_price=original_price,
            image_url=image_url,
        )
