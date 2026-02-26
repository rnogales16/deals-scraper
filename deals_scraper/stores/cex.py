"""Scraper para CEX (es.webuy.com) via browser (Playwright).

Parsea las tarjetas de producto en las páginas de búsqueda/categoría.
Estructura HTML:
  div.wrapper-box                    ← contenedor padre
    div.thumbnail
      div.card-img > a[href*=product-detail] > img (imagen producto)
    div.content
      div.card-subtitle  (categoría)
      div.card-title > a (título + href con id)
      div.product-prices > p.product-main-price (precio, ej: "165.00€")

CEX es segunda mano: original_price siempre es None.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import unquote

from ..models import Deal
from .base import BaseStore

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_PRODUCT_URL_BASE = "https://es.webuy.com/product-detail?id="


class CeXStore(BaseStore):
    """Scraper para CEX (es.webuy.com) via browser.

    En config.yaml, las scrape_urls apuntan a páginas de búsqueda/categoría
    (ej: https://es.webuy.com/search?superCatId=4).

    Requiere client_type: browser + force_stealth: true (SPA + Cloudflare).
    """

    async def build_urls(self) -> list[str]:
        """Devuelve las URLs de config tal cual."""
        return list(self.config.scrape_urls)

    def parse_deals(self, html: str, url: str) -> list[Deal]:
        """Parsea tarjetas de producto del HTML de CEX."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.error("[%s] bs4 no instalado", self.name)
            return []

        soup = BeautifulSoup(html, "lxml")

        # Contenedor padre: div.wrapper-box contiene thumbnail + content
        cards = soup.select("div.wrapper-box")

        if not cards:
            logger.warning(
                "[%s] No se encontraron tarjetas de producto (HTML length: %d, title: %s)",
                self.name, len(html), soup.title.string if soup.title else "?",
            )
            return []

        deals: list[Deal] = []
        for card in cards:
            try:
                deal = self._card_to_deal(card)
                if deal:
                    deals.append(deal)
            except Exception:
                logger.debug("[%s] Error parseando tarjeta CEX", self.name, exc_info=True)

        return deals

    def _card_to_deal(self, card) -> Deal | None:
        """Convierte una tarjeta HTML de CEX en un Deal."""
        # Título y enlace: div.card-title > a
        title_link = card.select_one("div.card-title a")
        if not title_link:
            return None

        title = title_link.get_text(strip=True)
        if not title:
            return None

        # Extraer ID del producto del href
        href = title_link.get("href", "")
        box_id = self._extract_id(href)
        if not box_id:
            return None

        # URL del producto (normalizada)
        product_url = f"{_PRODUCT_URL_BASE}{box_id}"

        # Precio: p.product-main-price (ej: "165.00€")
        price_el = card.select_one("p.product-main-price")
        if not price_el:
            return None

        price = self._parse_price(price_el.get_text(strip=True))
        if not price or price <= 0:
            return None

        # Categoría: div.card-subtitle
        category = ""
        cat_el = card.select_one("div.card-subtitle")
        if cat_el:
            category = cat_el.get_text(strip=True)

        # Fallback: extraer superCatName del href
        if not category:
            cat_match = re.search(r"superCatName=([^&]+)", href)
            if cat_match:
                category = unquote(cat_match.group(1))

        # Imagen: buscar img con product_images en src (dentro de thumbnail)
        image_url = ""
        imgs = card.select("img")
        for img in imgs:
            src = img.get("src", "")
            if src and "product_images" in src:
                image_url = src
                break
        # Fallback: cualquier img que no sea badge/logo
        if not image_url:
            for img in imgs:
                src = img.get("src", "")
                if src and "badge" not in src and "logo" not in src:
                    image_url = src
                    break

        return Deal(
            title=title,
            url=product_url,
            store=self.name,
            current_price=price,
            original_price=None,  # Segunda mano, no hay precio original
            image_url=image_url,
            category=category,
        )

    @staticmethod
    def _extract_id(href: str) -> str:
        """Extrae el ID del producto de un href de CEX.

        Ej: /product-detail?id=SIPH1364GBUNLB&categoryName=... → SIPH1364GBUNLB
        """
        match = re.search(r"[?&]id=([^&]+)", href)
        return match.group(1) if match else ""

    @staticmethod
    def _parse_price(text: str) -> float | None:
        """Parsea precio de texto CEX (ej: '165.00€', '1,299.00€')."""
        if not text:
            return None
        # Eliminar símbolos de moneda y espacios
        cleaned = text.replace("€", "").replace("£", "").replace("$", "").strip()
        # Eliminar separadores de miles (comas)
        cleaned = cleaned.replace(",", "")
        try:
            price = float(cleaned)
            return price if price > 0 else None
        except ValueError:
            return None
