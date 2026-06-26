"""Scraper para El Corte Inglés — parsea article.product_preview del HTML renderizado.

El Corte Inglés usa Vue.js SPA. Los precios se cargan via AJAX, por lo que
se requiere wait_for_selector en la config para esperar a que aparezcan.

Selectores clave:
  - article.product_preview  → contenedor
  - .price-sale              → precio con descuento
  - .price-unit--normal      → precio sin descuento
  - .price-unit--original    → precio original (tachado)
  - .price-discount          → porcentaje de descuento

Requiere use_system_chrome: true (Akamai bloquea Chromium headless).
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup, Tag

from ..models import Deal
from .base import BaseStore

logger = logging.getLogger(__name__)

_PRICE_RE = re.compile(r"([\d.,]+)\s*€")


def _parse_price(text: str) -> float | None:
    """Extrae un precio en euros de un string como '349,99 €'."""
    m = _PRICE_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


class ElCorteInglesStore(BaseStore):
    """Parsea las product_preview cards del HTML renderizado de El Corte Inglés."""

    async def build_urls(self) -> list[str]:
        return list(self.config.scrape_urls)

    def parse_deals(self, html: str, url: str) -> list[Deal]:
        soup = BeautifulSoup(html, "html.parser")

        deals: list[Deal] = []
        seen_urls: set[str] = set()

        cards = soup.select("article.product_preview")
        if not cards:
            logger.warning("[%s] No se encontraron tarjetas de producto", self.name)
            return []

        for card in cards:
            deal = self._card_to_deal(card)
            if deal and deal.url not in seen_urls:
                seen_urls.add(deal.url)
                deals.append(deal)

        return deals

    def _card_to_deal(self, card: Tag) -> Deal | None:
        # --- Title ---
        title = card.get("aria-label", "")
        if not title:
            link = card.select_one("a[title]")
            if link:
                title = link.get("title", "")
        if not title:
            title_el = card.select_one(".title a")
            if title_el:
                title = title_el.get_text(strip=True)
        if not title:
            return None

        # --- URL ---
        link = card.select_one("a[href]")
        if not link:
            return None
        href = link.get("href", "")
        if not href:
            return None
        product_url = href if href.startswith("http") else f"https://www.elcorteingles.es{href}"

        # --- Current price (sale or normal) ---
        price_el = card.select_one(".price-sale")
        if not price_el:
            price_el = card.select_one(".price-unit--normal")
        if not price_el:
            return None
        current_price = _parse_price(price_el.get_text())
        if not current_price or current_price <= 0:
            return None

        # --- Original price (strikethrough) ---
        original_price: float | None = None
        orig_el = card.select_one(".price-unit--original")
        if orig_el:
            original_price = _parse_price(orig_el.get_text())
            if original_price and original_price <= current_price:
                original_price = None

        # --- Image ---
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
