"""Adapter Amazon.es — usa Playwright (browser client).

Amazon renderiza las ofertas con React (SPA). Los datos de las ofertas
vienen prefetched como JSON dentro de un bloque <script> con mountWidget().
Este scraper extrae ese JSON en lugar de usar selectores CSS.
"""

from __future__ import annotations

import json
import logging
import re

from ..models import Deal
from .base import BaseStore

logger = logging.getLogger(__name__)

# Regex para encontrar el inicio de mountWidget
_MOUNT_WIDGET_START_RE = re.compile(
    r"assets\.mountWidget\(\s*'slot-\d+'\s*,\s*"
)


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


class AmazonStore(BaseStore):
    """Scraper para Amazon.es (Gold Box / ofertas del día).

    Extrae ofertas del JSON prefetched embebido en el HTML,
    dentro de la llamada assets.mountWidget().
    """

    async def build_urls(self) -> list[str]:
        return list(self.config.scrape_urls)

    def parse_deals(self, html: str, url: str) -> list[Deal]:
        deals: list[Deal] = []

        # Buscar todos los bloques mountWidget y extraer JSON balanceado
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
                logger.debug("Error procesando bloque mountWidget", exc_info=True)
                continue

        if not deals:
            logger.warning(
                "[amazon] No se encontraron ofertas en el JSON embebido. "
                "Amazon puede haber cambiado la estructura."
            )

        return deals

    @staticmethod
    def _extract_promotions(data: dict) -> list[dict]:
        """Navega el JSON para encontrar rankedPromotions."""
        try:
            prefetched = data.get("prefetchedData") or data.get("config", {}).get("prefetchedData", {})
            entity = prefetched.get("entity", {})
            return entity.get("rankedPromotions", [])
        except (AttributeError, TypeError):
            return []

    @staticmethod
    def _parse_promotion(promo: dict) -> Deal | None:
        """Extrae un Deal de un objeto rankedPromotion."""
        try:
            product = promo.get("product", {})
            entity = product.get("entity", {})
            if not entity:
                return None

            # Título
            title_obj = entity.get("title", {}).get("entity", {})
            title = title_obj.get("displayString", "")
            if not title:
                return None

            # URL del producto
            links = entity.get("links", {}).get("entity", {})
            view_url = links.get("viewOnAmazon", {}).get("url", "")
            product_url = f"https://www.amazon.es{view_url}" if view_url else ""
            if not product_url:
                return None

            # Imagen
            images_entity = entity.get("productImages", {}).get("entity", {})
            images_list = images_entity.get("images", [])
            image_url = ""
            if images_list:
                physical_id = images_list[0].get("lowRes", {}).get("physicalId", "")
                if physical_id:
                    image_url = f"https://m.media-amazon.com/images/I/{physical_id}._AC_SL300_.jpg"

            # Precios — buscar en buyingOptions
            buying_options = entity.get("buyingOptions", [])
            current_price = None
            original_price = None
            discount_pct = 0.0

            for opt in buying_options:
                price_entity = opt.get("price", {}).get("entity", {})
                if not price_entity:
                    continue

                # Precio actual
                pay = price_entity.get("priceToPay", {})
                amount_str = (
                    pay.get("moneyValueOrRange", {})
                    .get("value", {})
                    .get("amount", "")
                )
                if amount_str:
                    current_price = float(amount_str)

                # Precio original
                basis = price_entity.get("basisPrice", {})
                orig_str = (
                    basis.get("moneyValueOrRange", {})
                    .get("value", {})
                    .get("amount", "")
                )
                if orig_str:
                    original_price = float(orig_str)

                # Descuento
                savings = price_entity.get("savings", {})
                pct_val = savings.get("percentage", {}).get("value")
                if pct_val is not None:
                    discount_pct = float(pct_val)

                # Usar el primer buyingOption con precio
                if current_price is not None:
                    break

            if current_price is None:
                return None

            deal = Deal(
                title=title,
                url=product_url,
                store="amazon",
                current_price=current_price,
                original_price=original_price,
                discount_pct=discount_pct,
                image_url=image_url,
            )
            return deal

        except (KeyError, TypeError, ValueError):
            logger.debug("Error parseando promoción de Amazon", exc_info=True)
            return None
