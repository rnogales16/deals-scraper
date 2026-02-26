"""Generic store scraper that works with most e-commerce sites.

Tries multiple extraction strategies in order until one produces results:
    1. JSON-LD structured data
    2. Data attributes (analytics tracking: impressiondata, GA events, etc.)
    3. Microdata (itemprop attributes)
    4. Open Graph / meta tags
    5. Common CSS patterns (fallback)
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..models import Deal
from .base import BaseStore

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Price parsing helper
# ---------------------------------------------------------------------------

def _parse_price(text: str | None) -> float | None:
    """Parse a price string into a float.

    Handles formats such as:
        "299,99€"        "€299.99"        "299.99 EUR"
        "1.299,99 €"     "1,299.99"       "USD 1,299.99"
        "299.99"         "1 299,99 €"     "$ 4.999"
    """
    if not text:
        return None

    # Remove currency symbols and words, non-breaking spaces, and extra whitespace
    cleaned = text.strip()
    cleaned = re.sub(r"[€$£¥₹]", "", cleaned)
    cleaned = re.sub(r"\b(EUR|USD|GBP|JPY|INR)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("\xa0", "").replace("\u202f", "").replace(" ", "")

    if not cleaned:
        return None

    # European format: 1.299,99  →  has both dot and comma, dot is thousands separator
    if "," in cleaned and "." in cleaned:
        # Determine which is the decimal separator by its position from the right
        comma_pos = cleaned.rfind(",")
        dot_pos = cleaned.rfind(".")
        if comma_pos > dot_pos:
            # Comma is decimal separator: "1.299,99"
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            # Dot is decimal separator: "1,299.99"
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        # Ambiguous: could be "299,99" (European decimal) or "1,299" (thousands)
        # If the part after comma is exactly 3 digits, treat as thousands separator
        after_comma = cleaned.split(",")[-1]
        if len(after_comma) == 3 and after_comma.isdigit() and cleaned.count(",") == 1:
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")
    elif "." in cleaned:
        # Ambiguous dot-only: "4.999" (European thousands) vs "3.5" (decimal)
        # If the part after the last dot is exactly 3 digits → thousands separator
        after_dot = cleaned.split(".")[-1]
        if len(after_dot) == 3 and after_dot.isdigit():
            cleaned = cleaned.replace(".", "")

    match = re.search(r"\d+\.?\d*", cleaned)
    if not match:
        return None
    try:
        val = float(match.group())
        # Sanity check: reject prices <= 0 or unrealistically high (likely parse error)
        if val <= 0 or val > 500000:
            return None
        return val
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Image helper
# ---------------------------------------------------------------------------

def _extract_price_text(el: Tag) -> str:
    """Extract price text from an element, handling <sup> decimal patterns.

    Many French/EU sites use:  649€<sup>95</sup>  →  should become "649.95"
    Also handles nested wrappers like <div class="price"><div class="price">649€<sup>95</sup></div></div>.
    Non-destructive: does not modify the soup tree.
    """
    sup = el.find("sup")
    if sup and isinstance(sup, Tag):
        sup_text = sup.get_text(strip=True)
        # Use the direct parent of <sup> to avoid nested wrapper issues
        price_container = sup.parent
        if not isinstance(price_container, Tag):
            price_container = el

        # Collect text from siblings BEFORE the <sup>
        parts = []
        for child in price_container.children:
            if child is sup:
                break
            if hasattr(child, "get_text"):
                parts.append(child.get_text(strip=True))
            else:
                parts.append(str(child).strip())
        int_text = "".join(parts)

        if sup_text and int_text:
            # Clean the integer part (remove currency symbols and thousands separators)
            int_clean = re.sub(r"[€$£¥₹\s\xa0]", "", int_text)
            int_clean = int_clean.replace(".", "").replace(",", "")
            dec_clean = re.sub(r"[^0-9]", "", sup_text)
            if int_clean and dec_clean:
                return f"{int_clean}.{dec_clean}"
    return el.get_text(strip=True)


def _resolve_image(src: str | None, base_url: str) -> str:
    if not src:
        return ""
    src = str(src).strip()
    if not src or src.startswith("data:"):
        return ""
    if src.startswith("http"):
        return src
    return urljoin(base_url, src)


# ---------------------------------------------------------------------------
# Strategy helpers — JSON-LD
# ---------------------------------------------------------------------------

def _extract_from_jsonld_object(obj: dict, page_url: str, store_name: str) -> list[Deal]:
    """Extract Deal objects from a single JSON-LD object."""
    deals: list[Deal] = []
    obj_type = obj.get("@type", "")

    # Normalise type to a list for uniform handling
    if isinstance(obj_type, str):
        obj_type = [obj_type]

    # Recurse into @graph arrays
    if "@graph" in obj:
        graph = obj["@graph"]
        if isinstance(graph, list):
            for item in graph:
                if isinstance(item, dict):
                    deals.extend(_extract_from_jsonld_object(item, page_url, store_name))

    # ItemList / OfferCatalog: recurse into itemListElement
    if any(t in ("ItemList", "OfferCatalog") for t in obj_type):
        elements = obj.get("itemListElement", [])
        if not isinstance(elements, list):
            elements = [elements]
        for element in elements:
            if isinstance(element, dict):
                # ListItem wraps the real object in "item"
                inner = element.get("item", element)
                if isinstance(inner, dict):
                    deals.extend(_extract_from_jsonld_object(inner, page_url, store_name))
        return deals

    # Product
    if "Product" in obj_type:
        deal = _deal_from_jsonld_product(obj, page_url, store_name)
        if deal:
            deals.append(deal)
        return deals

    # Offer
    if "Offer" in obj_type:
        deal = _deal_from_jsonld_offer(obj, page_url, store_name, title="")
        if deal:
            deals.append(deal)

    return deals


def _resolve_url(href: str | None, base: str) -> str:
    if not href:
        return base
    href = str(href).strip()
    if href.startswith("http"):
        return href
    return urljoin(base, href)


def _jsonld_image(obj: dict) -> str:
    img = obj.get("image", "")
    if isinstance(img, list):
        img = img[0] if img else ""
    if isinstance(img, dict):
        img = img.get("url", "")
    return str(img) if img else ""


def _deal_from_jsonld_product(obj: dict, page_url: str, store_name: str) -> Deal | None:
    title = obj.get("name", "")
    if not title:
        return None
    title = str(title).strip()

    product_url = _resolve_url(obj.get("url") or obj.get("@id"), page_url)
    image_url = _jsonld_image(obj)

    offers = obj.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    if not isinstance(offers, dict):
        offers = {}

    return _deal_from_jsonld_offer(offers, page_url, store_name, title=title, product_url=product_url, image_url=image_url)


def _deal_from_jsonld_offer(
    offers: dict,
    page_url: str,
    store_name: str,
    title: str,
    product_url: str = "",
    image_url: str = "",
) -> Deal | None:
    if not title:
        title = str(offers.get("name", "")).strip()
    if not title:
        return None

    if not product_url:
        product_url = _resolve_url(offers.get("url"), page_url)
    if not image_url:
        image_url = _jsonld_image(offers)

    currency = str(offers.get("priceCurrency", "EUR")).strip() or "EUR"

    # Determine current and original prices
    current_price: float | None = None
    original_price: float | None = None

    low = _parse_price(str(offers.get("lowPrice", "")))
    high = _parse_price(str(offers.get("highPrice", "")))
    price = _parse_price(str(offers.get("price", "")))

    if low is not None and high is not None:
        current_price = low
        original_price = high
    elif price is not None:
        current_price = price
    elif low is not None:
        current_price = low

    if current_price is None:
        return None

    return Deal(
        title=title,
        url=product_url or page_url,
        store=store_name,
        current_price=current_price,
        original_price=original_price,
        currency=currency,
        image_url=image_url,
    )


# ---------------------------------------------------------------------------
# GenericStore
# ---------------------------------------------------------------------------

class GenericStore(BaseStore):
    """Generic e-commerce scraper that tries multiple extraction strategies."""

    async def build_urls(self) -> list[str]:
        return list(self.config.scrape_urls)

    def parse_deals(self, html: str, url: str) -> list[Deal]:
        soup = BeautifulSoup(html, "lxml")
        store_name = self.config.name

        for strategy_fn, strategy_name in (
            (self._strategy_jsonld, "JSON-LD"),
            (self._strategy_data_attributes, "Data attributes"),
            (self._strategy_microdata, "Microdata"),
            (self._strategy_opengraph, "Open Graph"),
            (self._strategy_css, "CSS patterns"),
        ):
            try:
                deals = strategy_fn(soup, url, store_name)
            except Exception:
                logger.debug(
                    "[%s] Strategy '%s' raised an exception",
                    store_name, strategy_name, exc_info=True,
                )
                deals = []

            if deals:
                # Filter out deals whose URL is the listing/search page itself
                # (no direct product link → shared price history → garbage data)
                deals = [d for d in deals if d.url != url]
                if not deals:
                    continue
                logger.info(
                    "[%s] Strategy '%s' succeeded with %d deal(s) from %s",
                    store_name, strategy_name, len(deals), url,
                )
                return deals

        logger.warning("[%s] No strategy produced results for %s", store_name, url)
        return []

    # ------------------------------------------------------------------
    # Strategy 1: JSON-LD
    # ------------------------------------------------------------------

    def _strategy_jsonld(self, soup: BeautifulSoup, url: str, store_name: str) -> list[Deal]:
        deals: list[Deal] = []
        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            raw = script.string or script.get_text()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                logger.debug("[%s] Could not parse JSON-LD block", store_name)
                continue

            # data may be a dict or a list of dicts
            objects = data if isinstance(data, list) else [data]
            for obj in objects:
                if not isinstance(obj, dict):
                    continue
                try:
                    deals.extend(_extract_from_jsonld_object(obj, url, store_name))
                except Exception:
                    logger.debug("[%s] Error extracting from JSON-LD object", store_name, exc_info=True)

        return deals

    # ------------------------------------------------------------------
    # Strategy 2: Data attributes (analytics/tracking)
    # ------------------------------------------------------------------

    def _strategy_data_attributes(self, soup: BeautifulSoup, url: str, store_name: str) -> list[Deal]:
        """Extract products from analytics data attributes embedded in HTML.

        Covers patterns like:
          - data-impressiondata (Lidl): URL-encoded JSON with name, price, brand
          - data-list-item-* (Game): GA Enhanced Ecommerce attributes
          - data-itemprice + data-itemname (Coolmod): custom tracking
        """
        deals: list[Deal] = []

        # --- Pattern 1: data-impressiondata (Lidl) ---
        elements = soup.select("[data-impressiondata]")
        for el in elements:
            if not isinstance(el, Tag):
                continue
            raw = str(el.get("data-impressiondata", ""))
            if not raw:
                continue
            try:
                data = json.loads(urllib.parse.unquote(raw))
            except (json.JSONDecodeError, ValueError):
                continue
            title = str(data.get("name", "")).strip()
            raw_price = data.get("price")
            if not title or raw_price is None:
                continue
            try:
                price = float(raw_price)
            except (ValueError, TypeError):
                continue
            if price <= 0:
                continue

            link = el.find("a", href=True)
            href = str(link["href"]) if isinstance(link, Tag) else ""
            img = el.find("img")
            img_src = ""
            if isinstance(img, Tag):
                img_src = str(img.get("src") or img.get("data-src") or "")

            deals.append(Deal(
                title=title,
                url=_resolve_url(href or None, url),
                store=store_name,
                current_price=price,
                category=str(data.get("category", "")),
                image_url=_resolve_image(img_src, url),
            ))
        if deals:
            return deals

        # --- Pattern 2: data-list-item-* (Game, GA Enhanced Ecommerce) ---
        elements = soup.select("[data-list-item-price][data-list-item-name]")
        seen_urls: set[str] = set()
        for el in elements:
            if not isinstance(el, Tag):
                continue
            title = str(el.get("data-list-item-name", "")).strip()
            raw_price = str(el.get("data-list-item-price", ""))
            price = _parse_price(raw_price)
            if not title or price is None:
                continue

            href = str(el.get("href", ""))
            product_url = _resolve_url(href or None, url)
            if product_url in seen_urls:
                continue
            seen_urls.add(product_url)

            img = el.find("img")
            img_src = ""
            if isinstance(img, Tag):
                img_src = str(img.get("src") or img.get("data-src") or "")

            deals.append(Deal(
                title=title,
                url=product_url,
                store=store_name,
                current_price=price,
                category=str(el.get("data-list-item-category", "")),
                image_url=_resolve_image(img_src, url),
            ))
        if deals:
            return deals

        # --- Pattern 3: data-itemprice + data-itemname (Coolmod, custom tracking) ---
        elements = soup.select("[data-itemprice]")
        for el in elements:
            if not isinstance(el, Tag):
                continue
            title = str(el.get("data-itemname") or el.get("data-name") or "").strip()
            raw_price = str(el.get("data-itemprice", ""))
            price = _parse_price(raw_price)
            if not title or price is None:
                continue

            href = str(el.get("href", "")) if el.name == "a" else ""
            if not href:
                link = el.find("a", href=True)
                href = str(link["href"]) if isinstance(link, Tag) else ""

            img = el.find("img")
            img_src = ""
            if isinstance(img, Tag):
                img_src = str(img.get("src") or img.get("data-src") or "")

            deals.append(Deal(
                title=title,
                url=_resolve_url(href or None, url),
                store=store_name,
                current_price=price,
                image_url=_resolve_image(img_src, url),
            ))
        if deals:
            return deals

        return deals

    # ------------------------------------------------------------------
    # Strategy 3: Microdata
    # ------------------------------------------------------------------

    def _strategy_microdata(self, soup: BeautifulSoup, url: str, store_name: str) -> list[Deal]:
        deals: list[Deal] = []

        # Find all elements that declare a Product or Offer itemtype scope
        product_scopes = soup.find_all(
            True,
            attrs={"itemtype": re.compile(r"(Product|Offer)", re.IGNORECASE)},
        )

        for scope in product_scopes:
            if not isinstance(scope, Tag):
                continue
            try:
                deal = self._microdata_scope_to_deal(scope, url, store_name)
                if deal:
                    deals.append(deal)
            except Exception:
                logger.debug("[%s] Error parsing microdata scope", store_name, exc_info=True)

        return deals

    def _microdata_text(self, scope: Tag, prop: str) -> str:
        el = scope.find(attrs={"itemprop": prop})
        if not el or not isinstance(el, Tag):
            return ""
        # <meta> uses content attribute
        content = el.get("content", "")
        if content:
            return str(content).strip()
        # <link> uses href
        href = el.get("href", "")
        if href:
            return str(href).strip()
        return el.get_text(strip=True)

    def _microdata_scope_to_deal(self, scope: Tag, page_url: str, store_name: str) -> Deal | None:
        title = self._microdata_text(scope, "name")
        if not title:
            return None

        raw_url = self._microdata_text(scope, "url")
        product_url = _resolve_url(raw_url or None, page_url)

        image_url = self._microdata_text(scope, "image")

        # Price may live in a nested Offer scope
        current_price: float | None = None
        original_price: float | None = None
        currency = "EUR"

        offer_scope = scope.find(attrs={"itemtype": re.compile(r"Offer", re.IGNORECASE)})
        price_scope = offer_scope if isinstance(offer_scope, Tag) else scope

        raw_price = self._microdata_text(price_scope, "price")
        current_price = _parse_price(raw_price)

        raw_orig = (
            self._microdata_text(price_scope, "highPrice")
            or self._microdata_text(price_scope, "originalPrice")
        )
        if raw_orig:
            original_price = _parse_price(raw_orig)

        raw_currency = self._microdata_text(price_scope, "priceCurrency")
        if raw_currency:
            currency = raw_currency

        if current_price is None:
            return None

        return Deal(
            title=title,
            url=product_url,
            store=store_name,
            current_price=current_price,
            original_price=original_price,
            currency=currency,
            image_url=image_url,
        )

    # ------------------------------------------------------------------
    # Strategy 4: Open Graph / meta tags
    # ------------------------------------------------------------------

    def _strategy_opengraph(self, soup: BeautifulSoup, url: str, store_name: str) -> list[Deal]:
        def meta(prop: str) -> str:
            el = soup.find("meta", attrs={"property": prop}) or soup.find(
                "meta", attrs={"name": prop}
            )
            if el and isinstance(el, Tag):
                return str(el.get("content", "")).strip()
            return ""

        title = meta("og:title") or meta("twitter:title")
        if not title:
            return []

        product_url = meta("og:url") or url
        image_url = meta("og:image") or meta("twitter:image")

        raw_price = (
            meta("product:price:amount")
            or meta("og:price:amount")
            or meta("price")
        )
        current_price = _parse_price(raw_price)
        if current_price is None:
            return []

        raw_orig = (
            meta("product:original_price:amount")
            or meta("og:original_price:amount")
        )
        original_price = _parse_price(raw_orig) if raw_orig else None

        currency = (
            meta("product:price:currency")
            or meta("og:price:currency")
            or "EUR"
        )

        return [
            Deal(
                title=title,
                url=product_url,
                store=store_name,
                current_price=current_price,
                original_price=original_price,
                currency=currency,
                image_url=image_url,
            )
        ]

    # ------------------------------------------------------------------
    # Strategy 5: CSS patterns (massively expanded)
    # ------------------------------------------------------------------

    # Card selectors ordered from most specific to most generic.
    # The CSS strategy tries ALL selectors until one produces valid deals.
    _CARD_SELECTORS = [
        # Data-attribute based (very specific, low false-positive)
        "[data-qa='productCard']",                  # BackMarket
        "[data-product]",
        "[data-product-id]",
        "[data-item]",
        "[data-code]",                              # Coolmod
        "[data-productid]",                         # Various
        "[data-pid]",                               # Various ecommerce
        "[data-sku]",                               # Various
        "[data-product-sku]",                       # Various
        # Store-specific patterns
        ".search-item:not(.is-template):not(.hidden)",  # Game
        "li.pdt-item",                              # LDLC
        "a.productBox",                             # Alternate
        ".dne-itemtile",                            # eBay deals
        "li.s-item",                                # eBay search
        "[data-testid='listing-product-card']",      # IKEA offers
        # Class-based (specific to e-commerce)
        "article.product",
        ".product-card",
        ".product-item",
        ".product-grid-item",
        ".product-list-item",
        ".product-miniature",                       # PrestaShop
        ".product-tile",                            # Salesforce Commerce
        ".product-card-wrapper",                    # Various
        ".product-outer",                           # WooCommerce
        ".js-product-miniature",                    # PrestaShop JS
        ".plp-product",                             # Various
        # Broader but still reliable
        "div.product",                              # Lidl, many stores
        "li.product",                               # Various
        # Very broad (only tried as last resort)
        "article",                                  # Phonehouse (bare articles)
    ]

    _TITLE_SELECTORS = [
        "h2", "h3", "h4",
        ".product-title",
        ".product-name",
        "[data-product-name]",
        "[data-test='product-title']",  # BackMarket
        "a[title]",
        # Additional patterns
        ".title a",                 # Game, Phonehouse
        ".title",                   # Various
        ".product__title",          # BEM convention
        ".item-title",              # Various
        ".name a",                  # Various
        ".name",                    # Various
        ".pdt-desc a",              # LDLC
        ".product-link",            # Various
        "a.product-link",           # Various
        "h1",                       # Single product pages
    ]

    _PRICE_SELECTORS = [
        ".price",
        ".product-price",
        ".current-price",
        ".sale-price",
        "[data-price]",
        ".price-current",
        # Additional patterns
        ".buy--price",              # Game
        ".body-2-bold",             # BackMarket (Tailwind)
        ".price--sale",             # Various
        ".price-sales",             # Various
        ".product__price",          # BEM
        ".pdt-price",               # LDLC
        ".prices-group .price",     # Phonehouse
        ".price-box",               # Magento
        ".woocommerce-Price-amount",  # WooCommerce
        "span.amount",              # WooCommerce
        ".price-new",               # OpenCart
        ".special-price",           # Magento
        ".offer-price",             # Various
        "[data-product-price]",     # Various
    ]

    _ORIG_PRICE_SELECTORS = [
        ".original-price",
        ".old-price",
        ".price-old",
        ".was-price",
        ".regular-price",
        "del .price",
        "s .price",
        ".price--crossed",
        "del",
        "s",
        # Additional patterns
        ".price-before",            # Various
        ".price-regular",           # Various
        ".pdt-price-before",        # LDLC
        ".price-original",          # Various
        ".price--rrp",              # Various
        ".list-price",              # Various
        ".price-was",               # Various
        ".crossed-price",           # Various
        ".line-through",            # BackMarket (Tailwind)
    ]

    _DISCOUNT_SELECTORS = [
        ".discount",
        ".badge",
        ".savings",
        ".discount-badge",
        ".discount-label",
        ".price-discount",
        ".promo-label",
    ]

    def _strategy_css(self, soup: BeautifulSoup, url: str, store_name: str) -> list[Deal]:
        """Try each card selector until one produces actual deals."""
        for sel in self._CARD_SELECTORS:
            found = soup.select(sel)
            if not found:
                continue

            cards = [c for c in found if isinstance(c, Tag)]
            if len(cards) < 2:
                continue

            deals: list[Deal] = []
            for card in cards:
                try:
                    deal = self._css_card_to_deal(card, url, store_name)
                    if deal:
                        deals.append(deal)
                except Exception:
                    logger.debug("[%s] Error parsing CSS card (%s)", store_name, sel, exc_info=True)

            if deals:
                logger.debug("[%s] CSS selector '%s' matched %d cards -> %d deals", store_name, sel, len(cards), len(deals))
                return deals

        return []

    def _css_card_to_deal(self, card: Tag, page_url: str, store_name: str) -> Deal | None:
        # ----- Title -----
        title = ""

        # First try data attributes on the card itself
        for attr in ("data-product-name", "data-itemname", "data-list-item-name", "data-name"):
            val = card.get(attr)
            if val:
                title = str(val).strip()
                break

        # Then try child elements with data attributes
        if not title:
            for attr in ("data-product-name", "data-itemname", "data-list-item-name"):
                el = card.find(attrs={attr: True})
                if el and isinstance(el, Tag):
                    title = str(el.get(attr, "")).strip()
                    if title:
                        break

        # Then try CSS selectors
        if not title:
            for sel in self._TITLE_SELECTORS:
                el = card.select_one(sel)
                if el and isinstance(el, Tag):
                    title = str(el.get("title", "")).strip() or el.get_text(strip=True)
                    if title:
                        break

        if not title:
            return None

        # ----- Link -----
        # If the card itself is an <a> tag, use its href directly
        href = ""
        if card.name == "a" and card.get("href"):
            href = str(card["href"])
        if not href:
            link_el = card.find("a", href=True)
            href = str(link_el.get("href", "")) if isinstance(link_el, Tag) else ""
        if not href:
            href = str(card.get("data-url", "") or card.get("data-href", "") or "")
        product_url = _resolve_url(href or None, page_url)

        # ----- Image -----
        img_el = card.find("img")
        image_url = ""
        if isinstance(img_el, Tag):
            img_src = str(
                img_el.get("src")
                or img_el.get("data-src")
                or img_el.get("data-lazy-src")
                or img_el.get("data-original")
                or ""
            ).strip()
            image_url = _resolve_image(img_src, page_url)

        # ----- Current price -----
        current_price: float | None = None

        # Try data attributes first (most reliable)
        for attr in ("data-price", "data-itemprice", "data-list-item-price", "data-product-price"):
            val = card.get(attr)
            if val:
                current_price = _parse_price(str(val))
                if current_price is not None:
                    break
            # Also check child elements
            if current_price is None:
                child = card.find(attrs={attr: True})
                if child and isinstance(child, Tag):
                    current_price = _parse_price(str(child.get(attr, "")))
                    if current_price is not None:
                        break

        # Try CSS selectors
        if current_price is None:
            for sel in self._PRICE_SELECTORS:
                el = card.select_one(sel)
                if el and isinstance(el, Tag):
                    raw = str(el.get("data-price", "") or el.get("content", "")).strip()
                    if not raw:
                        raw = _extract_price_text(el)
                    current_price = _parse_price(raw)
                    if current_price is not None:
                        break

        # Handle split prices: integer + decimal in separate spans (Game pattern)
        if current_price is None:
            int_el = card.select_one(".int, .price-integer, .price-int")
            dec_el = card.select_one(".decimal, .price-decimal, .price-dec")
            if int_el and isinstance(int_el, Tag):
                int_text = int_el.get_text(strip=True)
                dec_text = ""
                if dec_el and isinstance(dec_el, Tag):
                    dec_text = dec_el.get_text(strip=True).lstrip("'.,")
                try:
                    if dec_text:
                        current_price = float(f"{int_text}.{dec_text}")
                    else:
                        current_price = float(int_text)
                except ValueError:
                    pass

        if current_price is None:
            return None

        # ----- Original price -----
        original_price: float | None = None
        for sel in self._ORIG_PRICE_SELECTORS:
            el = card.select_one(sel)
            if el and isinstance(el, Tag):
                raw = _extract_price_text(el)
                p = _parse_price(raw)
                if p is not None and p != current_price and p > current_price:
                    original_price = p
                    break

        # ----- Discount badge -----
        discount_pct = 0.0
        for sel in self._DISCOUNT_SELECTORS:
            el = card.select_one(sel)
            if el and isinstance(el, Tag):
                text = el.get_text(strip=True)
                m = re.search(r"-?\s*(\d+)\s*%", text)
                if m:
                    discount_pct = float(m.group(1))
                    break

        # Scan card text for discount pattern like "-34%"
        if discount_pct == 0.0:
            m = re.search(r"-\s*(\d{1,2})\s*%", card.get_text())
            if m:
                discount_pct = float(m.group(1))

        # Skip deals without a real product URL (just the listing page)
        if product_url == page_url:
            return None

        deal = Deal(
            title=title,
            url=product_url,
            store=store_name,
            current_price=current_price,
            original_price=original_price,
            image_url=image_url,
        )
        if deal.discount_pct == 0.0 and discount_pct > 0:
            deal.discount_pct = discount_pct

        return deal
