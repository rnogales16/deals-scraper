"""Lógica de filtros: descuento mínimo, precio, keywords, categorías, verificación anti-fake."""

from __future__ import annotations

import logging
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .database import Database

from .models import Deal

logger = logging.getLogger(__name__)


def calculate_discount(current: float, original: float | None) -> float:
    """Calcula el porcentaje de descuento.

    Returns:
        Porcentaje de descuento (0-100), o 0 si no se puede calcular.
    """
    if not original or original <= 0 or current <= 0:
        return 0.0
    if current >= original:
        return 0.0
    return round((1 - current / original) * 100, 1)


def apply_filters(deals: list[Deal], filters_cfg: dict[str, Any]) -> list[Deal]:
    """Aplica filtros básicos: descuento, precio, keywords, categorías.

    NOTA: Este filtro usa el descuento que dice la tienda. Para verificar
    que el descuento es real, usar verify_real_deals() después.
    """
    min_discount = filters_cfg.get("min_discount", 0)
    price_min = filters_cfg.get("price_min", 0.0)
    price_max = filters_cfg.get("price_max", float("inf"))
    keywords = [kw.lower() for kw in filters_cfg.get("keywords", [])]
    categories = [cat.lower() for cat in filters_cfg.get("categories", [])]

    filtered: list[Deal] = []

    for deal in deals:
        # Recalcular descuento si es necesario
        if deal.discount_pct == 0.0 and deal.original_price:
            deal.discount_pct = calculate_discount(deal.current_price, deal.original_price)

        # Filtro de descuento mínimo
        if deal.discount_pct < min_discount:
            continue

        # Filtro de rango de precio
        if deal.current_price < price_min or deal.current_price > price_max:
            continue

        # Filtro de keywords (si hay keywords configuradas)
        if keywords:
            title_lower = deal.title.lower()
            if not any(kw in title_lower for kw in keywords):
                continue

        # Filtro de categorías (si hay categorías configuradas)
        if categories:
            if deal.category.lower() not in categories:
                continue

        filtered.append(deal)

    logger.info(
        "Filtros básicos: %d/%d ofertas pasan (min_discount=%s%%, precio=%.0f-%.0f€)",
        len(filtered), len(deals), min_discount, price_min, price_max,
    )
    return filtered


# ------------------------------------------------------------------
# Clasificación por tiers de alerta
# ------------------------------------------------------------------
def classify_deal(real_discount: float, price_error_threshold: float = 50.0) -> str:
    """Clasifica un deal según el descuento real.

    Returns:
        "ERROR_DE_PRECIO" si ≥ price_error_threshold,
        "CHOLLO" si ≥ 30%, "NORMAL" en otro caso.
    """
    if real_discount >= price_error_threshold:
        return "ERROR_DE_PRECIO"
    if real_discount >= 30.0:
        return "CHOLLO"
    return "NORMAL"


# Tiendas de reacondicionados: su "original_price" es el precio de nuevo
# (hace años), no el valor real actual. No confiar en su descuento para bypass.
_REFURBISHED_STORES = {"backmarket", "apple", "cex"}

# Keywords en título que indican producto reacondicionado (cualquier tienda)
_REFURBISHED_KEYWORDS = (
    "reacondicionado", "renewed", "refurbished", "remanufactured",
    "seminuevo", "segunda mano", "used", "como nuevo",
)


def _is_refurbished(deal: Deal) -> bool:
    """Detecta si un deal es reacondicionado por tienda o título."""
    if deal.store in _REFURBISHED_STORES:
        return True
    title_lower = deal.title.lower()
    return any(kw in title_lower for kw in _REFURBISHED_KEYWORDS)


# ------------------------------------------------------------------
# Verificación anti-fake: solo alertar bajadas de precio reales
# ------------------------------------------------------------------
def verify_real_deals(
    deals: list[Deal],
    db: Database,
    min_observations: int = 2,
    real_discount_min: float = 10.0,
    price_error_threshold: float = 50.0,
) -> list[Deal]:
    """Filtra ofertas falsas comparando con nuestro historial de precios.

    Lógica:
    - Productos nuevos (primera vez vistos): NO se envían, SALVO que la tienda
      reporte >price_error_threshold% descuento Y tenga original_price (posible
      error de precio — alertar en la primera detección).
    - Productos con historial pero sin bajada real: NO se envían.
    - Productos con bajada real verificada: SÍ se envían.

    Args:
        deals: Ofertas a verificar.
        db: Base de datos con historial de precios.
        min_observations: Mínimo de observaciones para confiar en el historial.
        real_discount_min: % mínimo de descuento vs mediana histórica.
        price_error_threshold: % descuento para considerar error de precio.

    Returns:
        Solo las ofertas con bajadas de precio verificadas (con alert_tier asignado).
    """
    verified: list[Deal] = []
    new_count = 0
    fake_count = 0

    for deal in deals:
        stats = db.get_price_stats_by_url(deal.url)

        # Producto nuevo — no tenemos historial
        if stats is None or stats["observations"] < min_observations:
            # Bypass: posible error de precio — alertar inmediatamente
            # NO aplica a tiendas de reacondicionados (su original_price es
            # el precio de nuevo hace años, no el valor real actual)
            if (not _is_refurbished(deal)
                    and deal.original_price and deal.original_price > deal.current_price):
                calculated_discount = (1 - deal.current_price / deal.original_price) * 100
                price_ratio = deal.original_price / deal.current_price
                if calculated_discount >= price_error_threshold and price_ratio < 10:
                    # Usar el descuento calculado, no el del badge
                    deal.discount_pct = round(calculated_discount, 1)
                    tier = classify_deal(deal.discount_pct, price_error_threshold)
                    deal.alert_tier = tier
                    logger.warning(
                        "POSIBLE ERROR DE PRECIO (1ª detección): %s — %.2f€ "
                        "(original: %.2f€, descuento real: %.0f%%)",
                        deal.title[:50], deal.current_price,
                        deal.original_price, deal.discount_pct,
                    )
                    verified.append(deal)
                    continue

            new_count += 1
            logger.debug(
                "NUEVO (sin historial): %s — %.2f€ (registrando para futuro)",
                deal.title[:50], deal.current_price,
            )
            continue

        # Tenemos historial — comprobar si la bajada es real
        real_discount = stats["real_discount_pct"]
        median_price = stats["median"]
        min_price = stats["min"]

        # Sanity check: si la mediana es >10x el precio actual, el historial
        # está envenenado (precios de otros productos mezclados por URL compartida)
        if deal.current_price > 0 and median_price / deal.current_price > 10:
            fake_count += 1
            logger.warning(
                "HISTORIAL ENVENENADO: %s — %.2f€ (mediana: %.2f€, ratio: %.1fx) — ignorando",
                deal.title[:50], deal.current_price, median_price,
                median_price / deal.current_price,
            )
            continue

        if real_discount < real_discount_min:
            fake_count += 1
            logger.debug(
                "FAKE: %s — %.2f€ (mediana: %.2f€, descuento real: %.1f%%, mín. histórico: %.2f€)",
                deal.title[:50], deal.current_price, median_price, real_discount, min_price,
            )
            continue

        # El precio actual es significativamente menor que la mediana — oferta real
        logger.info(
            "REAL: %s — %.2f€ (mediana: %.2f€, descuento real: -%.1f%%, mín. histórico: %.2f€)",
            deal.title[:50], deal.current_price, median_price, real_discount, min_price,
        )

        # Reemplazar el descuento de la tienda por el descuento real
        deal.discount_pct = real_discount
        deal.original_price = median_price
        deal.alert_tier = classify_deal(real_discount, price_error_threshold)
        verified.append(deal)

    logger.info(
        "Verificación anti-fake: %d reales, %d nuevos (registrados), %d falsos — de %d total",
        len(verified), new_count, fake_count, len(deals),
    )
    return verified


# ------------------------------------------------------------------
# Detección de precios absurdamente bajos (sin descuento marcado)
# ------------------------------------------------------------------

# Tiendas que venden productos muy baratos de forma habitual
_CHEAP_STORES = {"aliexpress", "ebay", "lidl", "ikea", "miravia"}

_MIN_STORE_PRODUCTS = 30      # Mínimo de productos para comparar a nivel tienda
_MIN_CATEGORY_PRODUCTS = 10   # Mínimo de productos para comparar a nivel categoría


def detect_absurdly_cheap(
    deals: list[Deal],
    db: Database,
    min_observations: int = 2,
) -> list[Deal]:
    """Detecta productos nuevos con precio sospechosamente bajo y sin descuento.

    Para productos con original_price=None y discount_pct=0 que no llegarían
    al pipeline normal (apply_filters los descarta por no tener descuento).

    Estrategia de dos tiers:
    1. store + category (si hay ≥10 productos): flag si price < P5 * 0.2
    2. store completa (si hay ≥30 productos): flag si price < P5 * 0.3
       (0.1 para tiendas baratas como aliexpress/ebay)

    Returns:
        Deals detectados como absurdamente baratos, con alert_tier = ERROR_DE_PRECIO.
    """
    detected: list[Deal] = []

    for deal in deals:
        # Solo candidatos: sin original_price y sin descuento
        if deal.original_price is not None:
            continue
        if deal.discount_pct > 0:
            continue
        if deal.current_price <= 0:
            continue

        # Solo productos nuevos o con poco historial
        stats = db.get_price_stats_by_url(deal.url)
        if stats is not None and stats["observations"] >= min_observations:
            continue

        flagged = False
        ref_label = ""
        ref_p5 = 0.0

        # Tier 1: store + category
        if deal.category:
            ref = db.get_store_price_percentiles(deal.store, deal.category)
            if ref and ref["count"] >= _MIN_CATEGORY_PRODUCTS:
                threshold = ref["p5"] * 0.2
                if threshold > 0 and deal.current_price < threshold:
                    flagged = True
                    ref_label = f"{deal.store}/{deal.category}"
                    ref_p5 = ref["p5"]

        # Tier 2 fallback: store completa
        if not flagged:
            ref = db.get_store_price_percentiles(deal.store)
            if ref is None or ref["count"] < _MIN_STORE_PRODUCTS:
                continue

            multiplier = 0.1 if deal.store in _CHEAP_STORES else 0.3
            threshold = ref["p5"] * multiplier

            if threshold <= 0 or deal.current_price >= threshold:
                continue

            flagged = True
            ref_label = deal.store
            ref_p5 = ref["p5"]

        if flagged:
            synthetic_discount = round((1 - deal.current_price / ref_p5) * 100, 1)
            deal.discount_pct = synthetic_discount
            deal.original_price = ref_p5
            deal.alert_tier = "ERROR_DE_PRECIO"

            logger.warning(
                "ABSURDAMENTE BARATO: %s — %.2f€ (P5 de %s: %.2f€, "
                "umbral: %.2f€, descuento sintético: %.0f%%)",
                deal.title[:50], deal.current_price, ref_label,
                ref_p5, threshold, synthetic_discount,
            )
            detected.append(deal)

    if detected:
        logger.info(
            "Detección precios absurdos: %d productos flaggeados",
            len(detected),
        )
    return detected


# ------------------------------------------------------------------
# Mapeo de categorías
# ------------------------------------------------------------------
_CATEGORY_MAP: dict[str, str] = {
    "portátiles": "laptops",
    "ordenadores portátiles": "laptops",
    "laptops": "laptops",
    "monitores": "monitors",
    "pantallas": "monitors",
    "almacenamiento": "storage",
    "discos duros": "storage",
    "ssd": "storage",
    "tarjetas gráficas": "gpus",
    "gpu": "gpus",
    "procesadores": "cpus",
    "cpu": "cpus",
    "smartphones": "phones",
    "móviles": "phones",
    "teléfonos": "phones",
    "televisores": "tvs",
    "tv": "tvs",
}


def normalize_category(category: str) -> str:
    """Normaliza una categoría a la taxonomía común."""
    return _CATEGORY_MAP.get(category.lower(), category.lower())


# ------------------------------------------------------------------
# Capa 1: Inferir categoría de la URL de scraping
# ------------------------------------------------------------------
_URL_CATEGORY_MAP: dict[str, str] = {
    "portatil": "laptops",
    "portatiles": "laptops",
    "laptop": "laptops",
    "notebooks": "laptops",
    "chromebook": "laptops",
    "smartphone": "phones",
    "smartphones": "phones",
    "movil": "phones",
    "moviles": "phones",
    "telefonos": "phones",
    "iphone": "phones",
    "tarjetas-graficas": "gpus",
    "tarjeta-grafica": "gpus",
    "pieza-grafica": "gpus",
    "gpu": "gpus",
    "procesador": "cpus",
    "procesadores": "cpus",
    "cpu": "cpus",
    "discos-duros": "storage",
    "discos-ssd": "storage",
    "ssd": "storage",
    "almacenamiento": "storage",
    "monitor": "monitors",
    "monitores": "monitors",
    "monitor-pc": "monitors",
    "pantalla": "monitors",
    "televisor": "tvs",
    "televisores": "tvs",
    "tv": "tvs",
    "tvs": "tvs",
    "tablet": "tablets",
    "tablets": "tablets",
    "ipad": "tablets",
    "auricular": "headphones",
    "auriculares": "headphones",
    "headphone": "headphones",
    "airpods": "headphones",
    "teclado": "keyboards",
    "teclados": "keyboards",
    "raton": "mice",
    "ratones": "mice",
    "impresora": "printers",
    "impresoras": "printers",
    "cafetera": "coffee_machines",
    "cafeteras": "coffee_machines",
    "aspirador": "vacuums",
    "consola": "consoles",
    "playstation": "consoles",
    "xbox": "consoles",
    "nintendo": "consoles",
    "memorias-ram": "ram",
    "ram": "ram",
    "placas-base": "motherboards",
    "fuentes-alimentacion": "psus",
    "altavoces": "speakers",
    "smartwatch": "smartwatches",
    "patinetes-electricos": "e_scooters",
}


def infer_category_from_url(url: str) -> str:
    """Intenta inferir la categoría de un deal a partir de la URL de scraping.

    Busca segmentos del path que coincidan con el mapa de categorías de URLs.
    Primero intenta con segmentos completos (entre /), luego con sub-segmentos
    (split en - y _).
    """
    from urllib.parse import urlparse

    path = urlparse(url).path.lower().strip("/")
    if not path:
        return ""

    # Capa 1: segmentos completos del path (preserva guiones)
    segments = path.split("/")
    for segment in segments:
        if segment in _URL_CATEGORY_MAP:
            return _URL_CATEGORY_MAP[segment]

    # Capa 2: sub-segmentos (split en - y _)
    for segment in segments:
        parts = re.split(r"[\-_]+", segment)
        for part in parts:
            if part in _URL_CATEGORY_MAP:
                return _URL_CATEGORY_MAP[part]
    return ""


# ------------------------------------------------------------------
# Capa 2: Inferir categoría del título del producto
# ------------------------------------------------------------------
_TITLE_CATEGORY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\b(portátil|laptop|notebook|macbook|chromebook)\b', re.IGNORECASE), 'laptops'),
    (re.compile(r'\b(smartphone|móvil|iphone|galaxy\s*s\d|pixel\s*\d|redmi|poco\s*\w)', re.IGNORECASE), 'phones'),
    (re.compile(r'\b(rtx|gtx|radeon\s*rx|tarjeta.?gráfica)\b', re.IGNORECASE), 'gpus'),
    (re.compile(r'\b(ryzen|intel\s*core|procesador|cpu)\b', re.IGNORECASE), 'cpus'),
    (re.compile(r'\b(ssd|nvme|disco.?duro|hdd)\b', re.IGNORECASE), 'storage'),
    (re.compile(r'\b(monitor|pantalla)\b', re.IGNORECASE), 'monitors'),
    (re.compile(r'\b(televisor|tv\s|smart\s*tv|oled|qled)\b', re.IGNORECASE), 'tvs'),
    (re.compile(r'\b(tablet|ipad)\b', re.IGNORECASE), 'tablets'),
    (re.compile(r'\b(auricular|headphone|airpods|buds|earbuds)\b', re.IGNORECASE), 'headphones'),
    (re.compile(r'\b(teclado|keyboard)\b', re.IGNORECASE), 'keyboards'),
    (re.compile(r'\b(ratón|mouse)\b', re.IGNORECASE), 'mice'),
    (re.compile(r'\b(impresora|printer)\b', re.IGNORECASE), 'printers'),
    (re.compile(r'\b(cafetera|nespresso|dolce.?gusto)\b', re.IGNORECASE), 'coffee_machines'),
    (re.compile(r'\b(aspirador|roomba|robot.?aspirador)\b', re.IGNORECASE), 'vacuums'),
    (re.compile(r'\b(consola|playstation|ps5|xbox|nintendo|switch)\b', re.IGNORECASE), 'consoles'),
    (re.compile(r'\b(smartwatch|reloj\s*inteligente|apple\s*watch|galaxy\s*watch)\b', re.IGNORECASE), 'smartwatches'),
    (re.compile(r'\b(altavoz|speaker|soundbar|barra.?de.?sonido)\b', re.IGNORECASE), 'speakers'),
]


def infer_category_from_title(title: str) -> str:
    """Intenta inferir la categoría de un deal a partir de su título.

    Usa regex patterns sobre palabras clave del título. Devuelve la primera
    coincidencia o cadena vacía si no matchea ninguna.
    """
    for pattern, category in _TITLE_CATEGORY_PATTERNS:
        if pattern.search(title):
            return category
    return ""


# ------------------------------------------------------------------
# Watchlist: alertar cuando un producto vigilado aparece a buen precio
# ------------------------------------------------------------------
def check_watchlist(
    deals: list[Deal],
    watchlist_cfg: dict,
    min_discount: float = 45.0,
) -> list[Deal]:
    """Busca deals que coincidan con la watchlist del usuario.

    Matching estricto: substring exacto (case-insensitive) con validación
    de contexto para evitar falsos positivos (ej: "PS5" en "Auriculares PC/PS5"
    no matchea porque PS5 aparece como compatibilidad, no como producto).

    Si matchea Y el precio actual ≤ max_price Y el descuento ≥ min_discount
    → marca como CHOLLO (o ERROR_DE_PRECIO si < 50% de max_price).

    Returns:
        Lista de deals que matchean la watchlist y cumplen el precio.
    """
    if not watchlist_cfg.get("enabled", False):
        return []

    products = watchlist_cfg.get("products", [])
    if not products:
        return []

    matched: list[Deal] = []
    seen_urls: set[str] = set()  # Evitar duplicados

    for deal in deals:
        if deal.url in seen_urls:
            continue
        title_lower = deal.title.lower()
        for product in products:
            name_lower = product["name"].lower()
            max_price = float(product["max_price"])
            min_price = float(product.get("min_price", 0))
            exclude_kws = [kw.lower() for kw in product.get("exclude_keywords", [])]

            if not _is_watchlist_match(name_lower, title_lower):
                continue
            if deal.current_price > max_price:
                continue
            if deal.current_price < min_price:
                continue

            # Rechazar si el título contiene alguna keyword excluida
            if any(kw in title_lower for kw in exclude_kws):
                continue

            discount = round((1 - deal.current_price / max_price) * 100, 1)

            # Respetar umbral mínimo de descuento
            if discount < min_discount:
                continue

            # Match: precio dentro del umbral
            # Tiendas de reacondicionados tienen precios bajos por defecto,
            # no confundir con errores de precio
            if (deal.current_price < max_price * 0.5
                    and not _is_refurbished(deal)):
                deal.alert_tier = "ERROR_DE_PRECIO"
            else:
                deal.alert_tier = "CHOLLO"
            deal.discount_pct = discount
            # Tiendas de reacondicionados: su original_price es el precio
            # de retail cuando nuevo (hace años), no el valor real actual.
            # No mostrarlo para no engañar.
            if _is_refurbished(deal):
                deal.original_price = None
            else:
                deal.original_price = deal.original_price or max_price

            logger.info(
                "WATCHLIST %s: %s — %.2f€ (max: %.2f€, tier: %s)",
                product["name"], deal.title[:50],
                deal.current_price, max_price, deal.alert_tier,
            )
            matched.append(deal)
            seen_urls.add(deal.url)
            break  # Un deal solo matchea una vez

    if matched:
        logger.info("Watchlist: %d productos encontrados a buen precio", len(matched))
    return matched


_COMPAT_PREFIXES = ("para ", "compatible ", "compatible con ", "works with ", "for ")

# Prefijos de accesorios que indican que NO es el producto principal
_ACCESSORY_PREFIXES = (
    "funda ", "carcasa ", "protector ", "adaptador ", "cargador ", "cable ",
    "soporte ", "correa ", "cristal ", "film ", "pegatina ", "skin ",
    "mando ", "controller ", "joystick ", "gamepad ", "dock ", "base ",
    "stand ", "hub ", "auricular ", "headset ",
    "teclado ", "keyboard ", "ratón ", "mouse ",
)


def _is_watchlist_match(name_lower: str, title_lower: str) -> bool:
    """Verifica si el nombre de la watchlist matchea el título del producto.

    Usa substring exacto con validación de contexto:
    - Rechaza matches en listas de compatibilidad (precedido por "/")
    - Rechaza matches precedidos por "para ", "compatible con ", etc.
    - Rechaza títulos que empiezan con prefijos de accesorios (funda, cargador...)
    - Rechaza matches en la parte final del título (>50% del largo)
    """
    pos = title_lower.find(name_lower)
    if pos == -1:
        return False

    # Rechazar si precedido por "/" (listas de compatibilidad: "PC/PS5")
    if pos > 0 and title_lower[pos - 1] == "/":
        return False

    # Rechazar si precedido por indicadores de compatibilidad
    prefix = title_lower[:pos]
    if any(prefix.endswith(cp) for cp in _COMPAT_PREFIXES):
        return False

    # Rechazar si el título empieza con un prefijo de accesorio
    if any(title_lower.startswith(ap) for ap in _ACCESSORY_PREFIXES):
        return False

    # Rechazar si el match está en la parte final del título (compatibilidad/specs)
    # Los nombres de producto aparecen en la primera mitad del título
    if len(title_lower) > 20 and pos > len(title_lower) * 0.5:
        return False

    return True


# ------------------------------------------------------------------
# Cross-store: detectar el mismo producto más barato en otra tienda
# ------------------------------------------------------------------
def detect_cross_store_bargains(
    db: Database,
    hours: int = 24,
    fuzzy_threshold: int = 85,
    min_discount_pct: float = 45.0,
) -> list[tuple[Deal, Deal]]:
    """Busca productos que están más baratos en otra tienda.

    Wrapper de db.find_cross_store_deals() que clasifica los pares
    según la diferencia de precio.

    Args:
        db: Base de datos.
        hours: Ventana temporal.
        fuzzy_threshold: Umbral de similitud (0-100).
        min_discount_pct: % mínimo de diferencia para alertar.

    Returns:
        Lista de (deal_barato, deal_caro) con alert_tier asignado al barato.
    """
    pairs = db.find_cross_store_deals(
        hours=hours, fuzzy_threshold=fuzzy_threshold,
        min_discount_pct=min_discount_pct,
    )

    for cheap, expensive in pairs:
        diff_pct = (1 - cheap.current_price / expensive.current_price) * 100
        if diff_pct >= 60:
            cheap.alert_tier = "ERROR_DE_PRECIO"
        else:
            cheap.alert_tier = "CHOLLO"
        cheap.discount_pct = round(diff_pct, 1)

    if pairs:
        logger.info("Cross-store: %d chollos encontrados comparando tiendas", len(pairs))
    return pairs
