"""Adapter Amazon.es — usa Playwright (browser client).

Soporta dos tipos de páginas:
1. Gold Box / ofertas del día  → JSON embebido en mountWidget()
2. Búsquedas (/s?k=...)        → HTML con [data-component-type="s-search-result"]
"""

from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup, Tag

from ..models import Deal
from .base import BaseStore

logger = logging.getLogger(__name__)

# Regex para encontrar el inicio de mountWidget
_MOUNT_WIDGET_START_RE = re.compile(
    r"assets\.mountWidget\(\s*'slot-\d+'\s*,\s*"
)

_PRICE_RE = re.compile(r"([\d.,]+)\s*€")

# ASIN en la URL del producto: /dp/B0XXXXXXXX, /gp/product/B0XXXXXXXX, /gp/aw/d/...
_ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)")


def _extract_asin(url: str) -> str | None:
    """Extrae el ASIN (10 chars) de una URL de producto de Amazon, o None."""
    m = _ASIN_RE.search(url or "")
    return m.group(1) if m else None


def _extract_json_object(text: str, start: int) -> dict | None:
    """Extrae un objeto JSON balanceado desde la posición start en text."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _parse_es_price(text: str) -> float | None:
    """Parsea un precio en formato español: '1.299,99 €' → 1299.99."""
    m = _PRICE_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


class AmazonStore(BaseStore):
    """Scraper para Amazon.es — Gold Box + búsquedas."""

    async def build_urls(self) -> list[str]:
        return list(self.config.scrape_urls)

    def parse_deals(self, html: str, url: str) -> list[Deal]:
        # Estrategia 1: mountWidget (goldbox/deals)
        deals = self._parse_goldbox(html)
        if deals:
            return deals

        # Estrategia 2: search results
        deals = self._parse_search_results(html)
        if deals:
            return deals

        logger.warning("[amazon] No se encontraron ofertas en %s", url)
        return []

    # ------------------------------------------------------------------
    # Estrategia 1: Gold Box (mountWidget JSON)
    # ------------------------------------------------------------------
    def _parse_goldbox(self, html: str) -> list[Deal]:
        deals: list[Deal] = []
        for match in _MOUNT_WIDGET_START_RE.finditer(html):
            json_start = match.end()
            data = _extract_json_object(html, json_start)
            if data is None:
                continue
            try:
                promotions = self._extract_promotions(data)
                for promo in promotions:
                    deal = self._parse_promotion(promo)
                    if deal:
                        deals.append(deal)
            except (KeyError, TypeError):
                continue
        return deals

    @staticmethod
    def _extract_promotions(data: dict) -> list[dict]:
        try:
            prefetched = data.get("prefetchedData") or data.get("config", {}).get("prefetchedData", {})
            entity = prefetched.get("entity", {})
            return entity.get("rankedPromotions", [])
        except (AttributeError, TypeError):
            return []

    @staticmethod
    def _parse_promotion(promo: dict) -> Deal | None:
        try:
            product = promo.get("product", {})
            entity = product.get("entity", {})
            if not entity:
                return None

            title_obj = entity.get("title", {}).get("entity", {})
            title = title_obj.get("displayString", "")
            if not title:
                return None

            links = entity.get("links", {}).get("entity", {})
            view_url = links.get("viewOnAmazon", {}).get("url", "")
            product_url = f"https://www.amazon.es{view_url}" if view_url else ""
            if not product_url:
                return None

            images_entity = entity.get("productImages", {}).get("entity", {})
            images_list = images_entity.get("images", [])
            image_url = ""
            if images_list:
                physical_id = images_list[0].get("lowRes", {}).get("physicalId", "")
                if physical_id:
                    image_url = f"https://m.media-amazon.com/images/I/{physical_id}._AC_SL300_.jpg"

            buying_options = entity.get("buyingOptions", [])
            current_price = None
            original_price = None
            discount_pct = 0.0

            for opt in buying_options:
                price_entity = opt.get("price", {}).get("entity", {})
                if not price_entity:
                    continue
                pay = price_entity.get("priceToPay", {})
                amount_str = (
                    pay.get("moneyValueOrRange", {})
                    .get("value", {})
                    .get("amount", "")
                )
                if amount_str:
                    current_price = float(amount_str)
                basis = price_entity.get("basisPrice", {})
                orig_str = (
                    basis.get("moneyValueOrRange", {})
                    .get("value", {})
                    .get("amount", "")
                )
                if orig_str:
                    original_price = float(orig_str)
                savings = price_entity.get("savings", {})
                pct_val = savings.get("percentage", {}).get("value")
                if pct_val is not None:
                    discount_pct = float(pct_val)
                if current_price is not None:
                    break

            if current_price is None:
                return None

            asin = _extract_asin(product_url)
            return Deal(
                title=title,
                url=product_url,
                store="amazon",
                current_price=current_price,
                original_price=original_price,
                discount_pct=discount_pct,
                image_url=image_url,
                product_id=f"asin:{asin}" if asin else None,
            )
        except (KeyError, TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Estrategia 2: Search results (HTML)
    # ------------------------------------------------------------------
    def _parse_search_results(self, html: str) -> list[Deal]:
        soup = BeautifulSoup(html, "html.parser")
        results = soup.select('[data-component-type="s-search-result"]')
        if not results:
            return []

        deals: list[Deal] = []
        for result in results:
            deal = self._parse_search_item(result)
            if deal:
                deals.append(deal)
        return deals

    @staticmethod
    def _parse_search_item(item: Tag) -> Deal | None:
        try:
            asin = item.get("data-asin", "")
            if not asin:
                return None

            # Título
            h2 = item.select_one("h2")
            title = h2.get_text(strip=True) if h2 else ""
            if not title:
                return None

            # URL
            product_url = f"https://www.amazon.es/dp/{asin}"

            # Precio actual: primer .a-offscreen dentro de .a-price sin tachado
            price_el = item.select_one(".a-price:not([data-a-strike]) .a-offscreen")
            if not price_el:
                return None
            current_price = _parse_es_price(price_el.get_text())
            if not current_price or current_price <= 0:
                return None

            # Precio original (tachado)
            original_price: float | None = None
            orig_el = item.select_one(
                ".a-price[data-a-strike] .a-offscreen, "
                ".a-price.a-text-price .a-offscreen"
            )
            if orig_el:
                original_price = _parse_es_price(orig_el.get_text())
                if original_price and original_price <= current_price:
                    original_price = None

            # Imagen
            img = item.select_one("img.s-image")
            image_url = img.get("src", "") if img else ""

            # asin viene de data-asin y es justo el de la URL /dp/{asin}
            return Deal(
                title=title,
                url=product_url,
                store="amazon",
                current_price=current_price,
                original_price=original_price,
                image_url=image_url,
                product_id=f"asin:{asin}" if asin else None,
            )
        except (KeyError, TypeError, ValueError):
            return None
