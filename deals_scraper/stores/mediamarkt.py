"""Adapter MediaMarkt.es — usa Playwright (browser client).

MediaMarkt usa una SPA React. Los productos aparecen en la página de búsqueda
con atributos data-test estables. Si MediaMarkt cambia de estructura,
actualiza los selectores _SEL_* de abajo.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from ..models import Deal
from .base import BaseStore

logger = logging.getLogger(__name__)

# =====================================================================
# Selectores basados en data-test (más estables que clases CSS)
# =====================================================================
_SEL_PRODUCT_CARD = "[data-test='mms-product-card']"
_SEL_TITLE = "[data-test='product-title']"
_SEL_LINK = "[data-test='mms-router-link-product-list-item-link']"
_SEL_IMAGE = "[data-test='product-image'] img"
_SEL_PRICE_BLOCK = "[data-test='mms-price']"
_SEL_STRIKE_PRICE_XOP = "[data-test='mms-strike-price-type-xop']"
_SEL_STRIKE_PRICE_RRP = "[data-test='mms-strike-price-type-rrp']"


def _parse_price(text: str | None) -> float | None:
    """Extrae precio de texto como '329,00EUR' o '499,-- EUR'."""
    if not text:
        return None
    cleaned = text.strip().replace("EUR", "").replace("€", "").replace("\xa0", "").replace(" ", "")
    # Reemplazar '--' por '00' (formato MediaMarkt: "329,--")
    cleaned = cleaned.replace("--", "00")
    # Formato español: 1.299,99
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    match = re.search(r"[\d]+\.?\d*", cleaned)
    return float(match.group()) if match else None


def _parse_discount(text: str | None) -> float:
    """Extrae porcentaje de texto como '-34%'."""
    if not text:
        return 0.0
    match = re.search(r"(\d+)\s*%", text)
    return float(match.group(1)) if match else 0.0


class MediaMarktStore(BaseStore):
    """Scraper para MediaMarkt.es (búsqueda de ofertas)."""

    async def build_urls(self) -> list[str]:
        return list(self.config.scrape_urls)

    def parse_deals(self, html: str, url: str) -> list[Deal]:
        soup = BeautifulSoup(html, "lxml")
        deals: list[Deal] = []

        cards = soup.select(_SEL_PRODUCT_CARD)
        if not cards:
            logger.warning("[mediamarkt] No se encontraron tarjetas de producto")

        for card in cards:
            try:
                deal = self._parse_card(card)
                if deal:
                    deals.append(deal)
            except Exception:
                logger.debug("Error parseando tarjeta en MediaMarkt", exc_info=True)

        return deals

    def _parse_card(self, card) -> Deal | None:
        # Título
        title_el = card.select_one(_SEL_TITLE)
        if not title_el:
            return None
        title = title_el.get_text(strip=True)

        # Enlace
        link_el = card.select_one(_SEL_LINK)
        href = link_el.get("href", "") if link_el else ""
        product_url = href if href.startswith("http") else f"https://www.mediamarkt.es{href}"

        # Precios — usar los spans screen-reader (clase mms-ui-sr_true) que tienen texto limpio
        price_block = card.select_one(_SEL_PRICE_BLOCK)
        if not price_block:
            return None

        # Precio original (tachado) — puede ser tipo xop o rrp
        original_price = None
        for sel in (_SEL_STRIKE_PRICE_XOP, _SEL_STRIKE_PRICE_RRP):
            strike_el = price_block.select_one(sel)
            if strike_el:
                # Preferir el span screen-reader para parseo limpio
                sr_span = strike_el.select_one("span.mms-ui-sr_true")
                if sr_span:
                    original_price = _parse_price(sr_span.get_text())
                else:
                    # Fallback: span con aria-hidden
                    vis_span = strike_el.select_one("span[aria-hidden='true']")
                    if vis_span:
                        original_price = _parse_price(vis_span.get_text())
                if original_price:
                    break

        # Precio actual — buscar spans screen-reader directos dentro del price block
        current_price = None
        sr_spans = price_block.select("span.mms-ui-sr_true")
        for span in sr_spans:
            # Saltar los que están dentro de strike-price (ya procesados)
            if span.find_parent(attrs={"data-test": re.compile("mms-strike-price")}):
                continue
            price = _parse_price(span.get_text())
            if price:
                current_price = price
                break

        # Fallback: buscar el span visible del precio actual
        if current_price is None:
            visible_spans = price_block.select("span[aria-hidden='true']")
            for span in visible_spans:
                if span.find_parent(attrs={"data-test": re.compile("mms-strike-price")}):
                    continue
                price = _parse_price(span.get_text())
                if price:
                    current_price = price
                    break

        if current_price is None:
            return None

        # Descuento badge — span dentro del badge wrapper
        discount_pct = 0.0
        # El badge no tiene data-test, buscar por texto que contenga %
        for span in price_block.select("span"):
            text = span.get_text(strip=True)
            if "%" in text and text.startswith("-"):
                discount_pct = _parse_discount(text)
                break

        # Imagen
        img_el = card.select_one(_SEL_IMAGE)
        image_url = img_el.get("src", "") if img_el else ""

        deal = Deal(
            title=title,
            url=product_url,
            store="mediamarkt",
            current_price=current_price,
            original_price=original_price,
            image_url=image_url,
        )
        if deal.discount_pct == 0.0 and discount_pct > 0:
            deal.discount_pct = discount_pct

        return deal
