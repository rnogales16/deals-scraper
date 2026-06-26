"""Scraper para VSGamers.es — HTML server-rendered con data-info JSON.

VSGamers sirve HTML completo. Los productos están en div.vs-product-card
con un atributo data-info que contiene JSON con precio, nombre, descuento, etc.
Precios también disponibles en span con data-price y .vs-product-card-prices-previous.

client_type: browser (Cloudflare light protection).
"""

from __future__ import annotations

import html
import json
import logging
import re

from bs4 import BeautifulSoup

from ..models import Deal
from .base import BaseStore

logger = logging.getLogger(__name__)


def _parse_price(text: str | None) -> float | None:
    """Extrae precio de texto como '330,50 €' o '1.599,00 €'."""
    if not text:
        return None
    cleaned = text.strip().replace("€", "").replace("\xa0", "").replace(" ", "")
    # Formato español: 1.599,00 → punto miles, coma decimales
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    elif "." in cleaned:
        parts = cleaned.split(".")
        if len(parts) == 2 and len(parts[1]) == 3:
            cleaned = cleaned.replace(".", "")  # Miles
    match = re.search(r"[\d]+\.?\d*", cleaned)
    return float(match.group()) if match else None


class VSGamersStore(BaseStore):
    """Scraper para VSGamers.es (componentes, periféricos gaming)."""

    async def build_urls(self) -> list[str]:
        return list(self.config.scrape_urls)

    def parse_deals(self, html_content: str, url: str) -> list[Deal]:
        soup = BeautifulSoup(html_content, "lxml")
        deals: list[Deal] = []

        cards = soup.select("div.vs-product-card")
        if not cards:
            logger.warning("[vsgamers] No se encontraron productos en %s", url)
            return deals

        for card in cards:
            try:
                deal = self._parse_card(card)
                if deal:
                    deals.append(deal)
            except Exception:
                logger.debug("Error parseando tarjeta VSGamers", exc_info=True)

        return deals

    def _parse_card(self, card) -> Deal | None:
        # Intentar parsear del data-info JSON primero (más fiable)
        data_info = card.get("data-info")
        if data_info:
            deal = self._parse_from_json(card, data_info)
            if deal:
                return deal

        # Fallback: parsear del HTML
        return self._parse_from_html(card)

    def _parse_from_json(self, card, data_info_raw: str) -> Deal | None:
        try:
            data = json.loads(html.unescape(data_info_raw))
        except (json.JSONDecodeError, TypeError):
            return None

        name = data.get("name", "")
        brand = data.get("brand", "")
        if brand and not name.lower().startswith(brand.lower()):
            name = f"{brand} {name}"
        if not name:
            return None

        price = data.get("price")
        if not price or price <= 0:
            return None

        # Stock
        if data.get("stockAvailability") == "no":
            return None

        # URL del producto
        title_el = card.select_one("div.vs-product-card-title a")
        href = title_el.get("href", "") if title_el else ""
        if not href:
            return None
        if href.startswith("/"):
            product_url = f"https://www.vsgamers.es{href}"
        elif href.startswith("http"):
            product_url = href
        else:
            product_url = f"https://www.vsgamers.es/{href}"

        # Precio original y descuento
        original_price = None
        discount_pct = 0.0
        discount_ratio = data.get("discount", 0)
        if discount_ratio and discount_ratio > 0:
            discount_pct = round(discount_ratio * 100, 1)
            # Calcular precio original desde el descuento
            if discount_ratio < 1:
                original_price = round(price / (1 - discount_ratio), 2)

        # También verificar precio tachado del HTML
        prev_el = card.select_one("span.vs-product-card-prices-previous")
        if prev_el:
            prev_price = _parse_price(prev_el.get_text())
            if prev_price and prev_price > price:
                original_price = prev_price
                discount_pct = round((1 - price / prev_price) * 100, 1)

        # Imagen
        image_url = ""
        img_el = card.select_one("div.vs-product-card-image img")
        if img_el:
            image_url = img_el.get("src", "") or img_el.get("data-src", "")
            if image_url and image_url.startswith("/"):
                image_url = f"https://www.vsgamers.es{image_url}"

        # Categoría
        category = data.get("category", "")

        return Deal(
            title=name.strip(),
            url=product_url,
            store="vsgamers",
            current_price=float(price),
            original_price=original_price,
            discount_pct=discount_pct,
            category=category,
            image_url=image_url,
        )

    def _parse_from_html(self, card) -> Deal | None:
        # Nombre
        title_el = card.select_one("div.vs-product-card-title a")
        if not title_el:
            return None
        title = title_el.get_text(strip=True)
        if not title:
            return None

        # URL
        href = title_el.get("href", "")
        if not href:
            return None
        if href.startswith("/"):
            product_url = f"https://www.vsgamers.es{href}"
        else:
            product_url = href

        # Precio actual
        price_el = card.select_one("span.vs-product-card-prices-price")
        if not price_el:
            return None
        current_price = None
        data_price = price_el.get("data-price")
        if data_price:
            try:
                current_price = float(data_price)
            except (ValueError, TypeError):
                pass
        if not current_price:
            current_price = _parse_price(price_el.get_text())
        if not current_price or current_price <= 0:
            return None

        # Precio original
        original_price = None
        discount_pct = 0.0
        prev_el = card.select_one("span.vs-product-card-prices-previous")
        if prev_el:
            original_price = _parse_price(prev_el.get_text())
            if original_price and original_price > current_price:
                discount_pct = round((1 - current_price / original_price) * 100, 1)

        # Descuento directo
        disc_el = card.select_one("span.vs-product-label-discount-percent")
        if disc_el and not discount_pct:
            try:
                discount_pct = float(disc_el.get_text(strip=True))
            except (ValueError, TypeError):
                pass

        # Imagen
        image_url = ""
        img_el = card.select_one("div.vs-product-card-image img")
        if img_el:
            image_url = img_el.get("src", "") or img_el.get("data-src", "")
            if image_url and image_url.startswith("/"):
                image_url = f"https://www.vsgamers.es{image_url}"

        # Categoría
        cat_el = card.select_one("a.vs-product-card-detail-category")
        category = cat_el.get_text(strip=True) if cat_el else ""

        return Deal(
            title=title,
            url=product_url,
            store="vsgamers",
            current_price=current_price,
            original_price=original_price,
            discount_pct=discount_pct,
            category=category,
            image_url=image_url,
        )
