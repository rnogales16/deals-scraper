"""Scraper para Worten — parsea article.product-card del HTML renderizado (Nuxt SPA).

Worten NO muestra precio "antes"/referencia, así que estos deals no sirven para el
flujo de descuento normal: alimentan el motor de valor de reventa (resale.py) cuando
la tienda tiene `resale_engine: true` en la config. El parser solo extrae lo que hay:
título, url, precio actual y EAN de marketplace.

Selectores (DOM renderizado):
  - article.product-card             → tarjeta
  - data-cnstrc-item-name            → título
  - data-cnstrc-item-price           → precio (numérico limpio)
  - a.w-app-link[href]               → enlace de producto
  - data-sku="MRKEAN-<ean>"          → EAN de marketplace
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from ..models import Deal
from .base import BaseStore

logger = logging.getLogger(__name__)


class WortenStore(BaseStore):
    """Parsea las product-card de Worten (Nuxt SPA renderizado)."""

    async def build_urls(self) -> list[str]:
        return list(self.config.scrape_urls)

    def parse_deals(self, html: str, url: str) -> list[Deal]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("article.product-card")
        if not cards:
            logger.warning("[%s] No se encontraron tarjetas de producto", self.name)
            return []

        deals: list[Deal] = []
        seen: set[str] = set()
        for art in cards:
            title = (art.get("data-cnstrc-item-name") or "").strip()
            price_str = art.get("data-cnstrc-item-price") or ""
            if not title or not price_str:
                continue
            try:
                price = float(price_str)
            except ValueError:
                continue
            if price <= 0:
                continue

            link = art.select_one("a.w-app-link[href]") or art.select_one("a[href]")
            if not link:
                continue
            href = link.get("href", "")
            if not href or href.startswith(("javascript:", "#")):
                continue
            product_url = href if href.startswith("http") else f"https://www.worten.es{href}"
            if product_url in seen:
                continue
            seen.add(product_url)

            # EAN del data-sku (p.ej. "MRKEAN-0729927153261")
            sku = link.get("data-sku") or art.get("data-sku") or ""
            product_id = None
            m = re.search(r"(\d{8,14})", sku)
            if m:
                product_id = f"ean:{m.group(1)}"

            deals.append(Deal(
                title=title,
                url=product_url,
                store=self.name,
                current_price=price,
                product_id=product_id,
            ))
        return deals
