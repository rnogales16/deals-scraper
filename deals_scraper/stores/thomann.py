"""Scraper para Thomann.de/es — HTML server-rendered con productos en tarjetas.

Thomann sirve HTML completo con precios, nombres y enlaces.
Los productos están en <a class="js-product"> con estructura CSS estable.
Precios tachados y descuentos en .strike-price-with-percentage.

Requiere User-Agent realista (Cloudflare light protection).
client_type: browser (para UA realista y seguir redirects).
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from ..models import Deal
from .base import BaseStore

logger = logging.getLogger(__name__)

_SEL_PRODUCT = "a.js-product"


def _parse_price(text: str | None) -> float | None:
    """Extrae precio de texto como '369 €', '1.599 €' o '1,599 EUR'."""
    if not text:
        return None
    cleaned = text.strip().replace("€", "").replace("EUR", "").replace("\xa0", "").replace(" ", "")
    # Thomann usa punto como separador de miles en español: "1.599"
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "." in cleaned:
        # Verificar si el punto es separador de miles (ej: 1.599) o decimal (ej: 9.99)
        parts = cleaned.split(".")
        if len(parts) == 2 and len(parts[1]) == 3:
            cleaned = cleaned.replace(".", "")  # Miles
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    match = re.search(r"[\d]+\.?\d*", cleaned)
    return float(match.group()) if match else None


class ThomannStore(BaseStore):
    """Scraper para Thomann.de/es (instrumentos musicales y audio)."""

    async def build_urls(self) -> list[str]:
        return list(self.config.scrape_urls)

    def parse_deals(self, html: str, url: str) -> list[Deal]:
        soup = BeautifulSoup(html, "lxml")
        deals: list[Deal] = []

        cards = soup.select(_SEL_PRODUCT)
        if not cards:
            logger.warning("[thomann] No se encontraron productos en %s", url)
            return deals

        for card in cards:
            try:
                deal = self._parse_card(card)
                if deal:
                    deals.append(deal)
            except Exception:
                logger.debug("Error parseando tarjeta Thomann", exc_info=True)

        return deals

    def _parse_card(self, card) -> Deal | None:
        # Nombre del producto
        desc_el = card.select_one(".description.fx-text")
        if not desc_el:
            return None
        # Extraer fabricante y nombre por separado para evitar que se peguen
        manufacturer_el = desc_el.select_one(".description__manufacturer")
        if manufacturer_el:
            manufacturer = manufacturer_el.get_text(strip=True)
            # Eliminar el elemento del fabricante para obtener el resto
            manufacturer_el.extract()
            product_name = desc_el.get_text(strip=True)
            title = f"{manufacturer} {product_name}".strip() if manufacturer else product_name
        else:
            title = desc_el.get_text(strip=True)
        if not title:
            return None

        # URL del producto
        href = card.get("href", "")
        if not href:
            return None
        if href.startswith("http"):
            product_url = href
        elif href.startswith("/"):
            product_url = f"https://www.thomann.de{href}"
        else:
            product_url = f"https://www.thomann.de/es/{href}"

        # Precio actual
        price_el = card.select_one(".price__primary")
        if not price_el:
            return None
        current_price = _parse_price(price_el.get_text())
        if not current_price or current_price <= 0:
            return None

        # Precio original (tachado) y descuento
        original_price = None
        discount_pct = 0.0

        strike_info = card.select_one(".strike-price-with-percentage__info span")
        if strike_info:
            # Texto como "30 días mejor precio: 111 €"
            text = strike_info.get_text()
            # Extraer el precio del texto
            price_match = re.search(r"([\d.,]+)\s*€", text)
            if price_match:
                original_price = _parse_price(price_match.group(1) + "€")

        pct_el = card.select_one(".fx-typography-price-strike-percentage")
        if pct_el:
            pct_text = pct_el.get_text(strip=True)
            pct_match = re.search(r"-?(\d+)%", pct_text)
            if pct_match:
                discount_pct = float(pct_match.group(1))

        # Imagen
        image_url = ""
        source_el = card.select_one("picture source[type='image/webp']")
        if source_el:
            image_url = source_el.get("data-srcset", "") or source_el.get("srcset", "")
        if not image_url:
            img_el = card.select_one("img.product-image")
            if img_el:
                image_url = img_el.get("src", "") or img_el.get("data-src", "")

        return Deal(
            title=title,
            url=product_url,
            store="thomann",
            current_price=current_price,
            original_price=original_price,
            discount_pct=discount_pct,
            category="instrumentos-musica",
            image_url=image_url,
        )
