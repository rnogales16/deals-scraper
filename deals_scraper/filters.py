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
    """Aplica filtros básicos: descuento, precio, keywords, categorías, marcas.

    NOTA: Este filtro usa el descuento que dice la tienda. Para verificar
    que el descuento es real, usar verify_real_deals() después.
    """
    min_discount = filters_cfg.get("min_discount", 0)
    price_min = filters_cfg.get("price_min", 0.0)
    price_max = filters_cfg.get("price_max", float("inf"))
    keywords = [kw.lower() for kw in filters_cfg.get("keywords", [])]
    categories = [cat.lower() for cat in filters_cfg.get("categories", [])]
    exclude_keywords = [kw.lower() for kw in filters_cfg.get("exclude_keywords", [])]

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

        title_lower = deal.title.lower()

        # Filtro de keywords excluidas (basura, cosméticos, ropa, etc.)
        if exclude_keywords and any(kw in title_lower for kw in exclude_keywords):
            continue

        # Filtro de marca: en TODAS las tiendas salvo las de reacondicionado,
        # solo pasar productos de marcas reconocidas (y no clones que mencionan
        # una marca de forma oportunista — ver _has_known_brand). Evita
        # "Smartwatch estilo Apple" o "Smartwatch Hombre Militar" chino a "60% off".
        if deal.store not in _REFURBISHED_STORES and not _has_known_brand(title_lower):
            continue

        # Filtro de keywords (si hay keywords configuradas)
        if keywords:
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
def classify_deal(real_discount: float, price_error_threshold: float = 75.0) -> str:
    """Clasifica un deal según el descuento real.

    Returns:
        "ERROR_DE_PRECIO" si ≥ price_error_threshold,
        "CHOLLO" si ≥ 70%, "NORMAL" en otro caso.
    """
    if real_discount >= price_error_threshold:
        return "ERROR_DE_PRECIO"
    if real_discount >= 70.0:
        return "CHOLLO"
    return "NORMAL"


# ------------------------------------------------------------------
# Marcas reconocidas con buena salida — productos deseables
# ------------------------------------------------------------------
_KNOWN_BRANDS = {
    # Apple / Mac
    "apple", "iphone", "ipad", "macbook", "airpods", "apple watch", "imac",
    "mac mini", "mac studio", "mac pro", "homepod",
    # Samsung
    "samsung", "galaxy",
    # Sony
    "sony", "playstation", "ps5", "dualsense", "bravia", "wh-1000",
    # Nintendo
    "nintendo", "switch",
    # Microsoft / Xbox
    "microsoft", "xbox", "surface",
    # Google
    "google", "pixel", "chromecast", "nest",
    # Smartphones
    "xiaomi", "redmi", "poco", "oneplus", "oppo", "realme", "huawei",
    "motorola", "nothing phone",
    # PC / Componentes
    "nvidia", "rtx", "gtx", "geforce", "amd", "ryzen", "radeon",
    "intel core", "core i5", "core i7", "core i9",
    "asus", "msi", "gigabyte", "corsair", "kingston", "crucial", "seagate",
    "western digital", "sandisk", "sabrent", "samsung evo", "samsung pro",
    # Portátiles
    "lenovo", "thinkpad", "ideapad", "dell", "latitude", "xps", "inspiron",
    "hp elitebook", "hp probook", "hp pavilion", "hp envy", "hp spectre",
    "hp omen", "hp victus",
    "acer", "aspire", "predator", "razer", "framework",
    # Monitores / TV
    "lg oled", "lg nanocell", "lg ultragear", "lg gram",
    "benq", "viewsonic", "philips", "hisense", "tcl",
    # Audio
    "bose", "sennheiser", "jabra", "jbl", "marshall", "bang & olufsen",
    "shure", "audio-technica", "beyerdynamic", "sonos",
    # Gaming
    "logitech", "steelseries", "hyperx", "roccat", "razer",
    # Periféricos
    "cherry", "keychron", "ducky",
    # Electrodomésticos premium
    "dyson", "roomba", "irobot", "roborock", "dreame", "ecovacs",
    "thermomix", "kitchenaid", "nespresso", "delonghi", "sage", "breville",
    "ninja", "vitamix",
    # Wearables / Fitness
    "garmin", "fitbit", "polar", "suunto", "whoop", "oura",
    # Fotografía / Drones
    "canon", "nikon", "sony alpha", "fujifilm", "gopro", "dji", "insta360",
    # Networking
    "ubiquiti", "unifi", "synology", "qnap", "tp-link", "netgear", "asus router",
    # E-readers
    "kindle", "kobo",
    # Movilidad
    "segway", "ninebot", "xiaomi scooter",
    # Smartphones (ampliación)
    "honor",
    # Networking / carga / accesorios (ampliación)
    "cudy", "anker", "ugreen", "baseus", "belkin",
    # Pequeño electrodoméstico / cocina (ampliación)
    "cecotec", "tefal", "cosori", "instant pot", "braun", "rowenta",
    "taurus", "orbegozo",
    # Gran electrodoméstico (ampliación)
    "haier", "beko", "balay", "bosch", "siemens", "whirlpool", "hoover", "teka",
}


# Alternación de marcas, reutilizable en varias regex
_BRANDS_ALT = '|'.join(re.escape(b.strip()) for b in _KNOWN_BRANDS)

_KNOWN_BRANDS_RE = re.compile(r'\b(?:' + _BRANDS_ALT + r')\b', re.IGNORECASE)


# Señales de CLON / COMPATIBILIDAD inequívocas: si aparecen, el producto menciona
# la marca de forma oportunista y NO es de esa marca. SOLO señales de clon — nada
# de términos de categoría o género ("hombre", "mujer", "militar"), que aparecen
# en productos legítimos (ej: "Garmin para hombre") y darían falsos negativos.
_FAKE_BRAND_SIGNALS = {
    "similar a", "like", "compatible con",
    "réplica", "imitación", "inspirado en", "alternativa a",
    "sin marca", "marca blanca", "genérico", "no oficial",
}

_FAKE_BRAND_SIGNALS_RE = re.compile(
    r'\b(?:' + '|'.join(re.escape(s) for s in _FAKE_BRAND_SIGNALS) + r')\b',
    re.IGNORECASE,
)

# "tipo"/"estilo" NO son señal sueltos ("USB tipo C", "estilo deportivo" son
# legítimos): solo cuentan como clon cuando van SEGUIDOS de una marca conocida
# ("estilo apple", "tipo airpods").
_FAKE_BRAND_PREFIX_RE = re.compile(
    r'\b(?:tipo|estilo)\s+(?:' + _BRANDS_ALT + r')\b',
    re.IGNORECASE,
)


# Marcas con HOMÓNIMO: solo cuentan como marca en su contexto, para no dar
# falsos positivos (conga de percusión en Thomann, "Candy Crush", "cata de
# vinos"). NO se meten en _KNOWN_BRANDS (que es word-boundary suelto).
_CONGA_RE = re.compile(r'\bconga\b', re.IGNORECASE)
_CONGA_CONTEXT_RE = re.compile(r'\b(?:cecotec|robot|aspirador\w*)\b', re.IGNORECASE)
_CANDY_RE = re.compile(r'\bcandy\b', re.IGNORECASE)
_CANDY_CONTEXT_RE = re.compile(
    r'\b(?:lavadora|secadora|lavasecadora|lavavajillas|frigor[ií]fico|nevera|'
    r'congelador|horno|microondas|encimera|vitrocer[áa]mica|inducci[óo]n|combi)\b',
    re.IGNORECASE,
)
_CATA_RE = re.compile(r'\bcata\b', re.IGNORECASE)
_CATA_CONTEXT_RE = re.compile(r'\b(?:campana|extractora)\b', re.IGNORECASE)


def _has_restricted_brand(title_lower: str) -> bool:
    """Marcas con homónimo, válidas SOLO en su contexto (anti-falso-positivo)."""
    if _CONGA_RE.search(title_lower) and _CONGA_CONTEXT_RE.search(title_lower):
        return True
    if _CANDY_RE.search(title_lower) and _CANDY_CONTEXT_RE.search(title_lower):
        return True
    if _CATA_RE.search(title_lower) and _CATA_CONTEXT_RE.search(title_lower):
        return True
    return False


def _has_known_brand(title_lower: str) -> bool:
    """Comprueba si el título contiene una marca reconocida (word boundary).

    Devuelve False si el título contiene una señal de clon/compatibilidad,
    AUNQUE mencione una marca conocida: "Smartwatch estilo Apple" menciona Apple
    pero no es Apple. Las señales son: las inequívocas de _FAKE_BRAND_SIGNALS, y
    "tipo/estilo + <marca conocida>". Incluye marcas con homónimo (conga, candy,
    cata) solo en su contexto vía _has_restricted_brand.
    """
    if _FAKE_BRAND_SIGNALS_RE.search(title_lower):
        return False
    if _FAKE_BRAND_PREFIX_RE.search(title_lower):
        return False
    # "conga" es homónimo de percusión: si aparece SIN su contexto
    # (cecotec/robot/aspirador) NO es la marca, aunque el título lleve otra
    # palabra-marca (p.ej. "Conga LP Aspire" contiene "aspire" de Acer pero es
    # una conga de percusión). El override evita ese falso positivo.
    if _CONGA_RE.search(title_lower) and not _CONGA_CONTEXT_RE.search(title_lower):
        return False
    if _KNOWN_BRANDS_RE.search(title_lower):
        return True
    return _has_restricted_brand(title_lower)


# ------------------------------------------------------------------
# Liquidez de reventa: umbral de descuento mínimo variable según la marca
# ------------------------------------------------------------------
# Marcas de ALTA liquidez (reventa rápida y segura) — umbral más bajo (60%).
_LIQUIDITY_ALTA_BRANDS = (
    "apple", "iphone", "ipad", "macbook", "airpods", "apple watch",
    "samsung", "galaxy", "sony", "playstation", "ps5", "nintendo", "switch",
    "xbox", "dyson", "roborock", "irobot", "nvidia", "rtx", "geforce",
    "lg oled", "garmin", "gopro", "dji", "bose", "sonos",
)
_LIQUIDITY_ALTA_RE = re.compile(
    r'\b(?:' + '|'.join(re.escape(b) for b in _LIQUIDITY_ALTA_BRANDS) + r')\b',
    re.IGNORECASE,
)

# Umbral de descuento mínimo para alertar, según la liquidez de reventa.
_LIQUIDITY_MIN_DISCOUNT = {"alta": 60.0, "media": 70.0, "baja": 80.0}


def liquidity_tier(deal: Deal) -> str:
    """Nivel de liquidez de reventa del producto: 'alta' | 'media' | 'baja'.

    - 'alta': marca de reventa rápida y segura (Apple, PS5, RTX, Dyson, Garmin…).
    - 'media': resto de marcas reconocidas.
    - 'baja': marca no reconocida (o clon). Exige confirmación externa del valor.
    """
    t = deal.title.lower()
    if not _has_known_brand(t):
        return "baja"
    if _LIQUIDITY_ALTA_RE.search(t):
        return "alta"
    return "media"


def liquidity_min_discount(deal: Deal) -> float:
    """Umbral de descuento mínimo (%) para alertar, según la liquidez de reventa."""
    return _LIQUIDITY_MIN_DISCOUNT[liquidity_tier(deal)]


def _is_clone(title_lower: str) -> bool:
    """True si el título es un CLON que menciona una marca de forma oportunista
    ("estilo Apple", "tipo AirPods", señales de _FAKE_BRAND_SIGNALS, conga sin
    contexto). Son los casos que _has_known_brand rechaza por clon (no por marca
    desconocida)."""
    if _FAKE_BRAND_SIGNALS_RE.search(title_lower):
        return True
    if _FAKE_BRAND_PREFIX_RE.search(title_lower):
        return True
    if _CONGA_RE.search(title_lower) and not _CONGA_CONTEXT_RE.search(title_lower):
        return True
    return False


# Tiendas de reacondicionados: su "original_price" es el precio de nuevo
# (hace años), no el valor real actual. No confiar en su descuento para bypass.
_REFURBISHED_STORES = {"backmarket", "apple", "cex", "infocomputer"}

# Tiendas donde los vendedores inflan original_price de forma sistemática.
# No confiar en su descuento para bypass de primera detección.
_INFLATED_PRICE_STORES = {"amazon", "aliexpress", "ebay", "miravia", "lifeinformatica"}

# Keywords en título que indican producto reacondicionado (cualquier tienda)
_REFURBISHED_KEYWORDS = (
    "reacondicionado", "renewed", "refurbished", "remanufactured",
    "seminuevo", "segunda mano", "used", "como nuevo",
)


# Keywords globales de accesorios — filtrar en todo el pipeline
_GLOBAL_ACCESSORY_KEYWORDS = (
    "funda ", "carcasa ", "protector de pantalla", "cristal templado",
    "cable usb", "cable lightning", "cable tipo c",
    "correa para ", "correa de repuesto",
    "almohadilla", "ear tip", "recambio", "repuesto",
    "pegatina", "skin ", "film protector",
    "adaptador ", "cargador ", "fuente de alimentacion",
    "power adapter", "charger ", "charging ",
    # Ampliación (filtro de accesorios reforzado). Las que tenían homónimo o
    # colisión de substring se restringen para no filtrar productos legítimos:
    #   "cooler"   -> "cooler para"/"cooler de"   (evita "Cooler Master" AIO)
    #   "disipador"-> "disipador para"            (evita "Disipador Noctua")
    #   "set de" / "membrana" / "junta"           -> FUERA (colisión con "reset de",
    #     "teclado de membrana", "adjunta/conjunta"; demasiados falsos positivos)
    "ventilador para", "cooler para", "cooler de", "disipador para",
    "base de carga", "grip", "empuñadura", "mochila para", "maletín para",
    "bolsa para", "soporte de pared", "kit de", "filtro para",
)

# Keywords que indican que el producto es un componente/accesorio de otro
# producto más caro (ej: water block para RTX 5090 ≠ RTX 5090 GPU).
# Estos productos NO deben recibir bypass de ERROR_DE_PRECIO porque su
# precio real es mucho menor que el producto principal que mencionan.
_COMPONENT_KEYWORDS = (
    "refrigeracion por agua", "refrigeración por agua",
    "water block", "waterblock", "water cooling",
    "bloque de agua", "bloque refrigeracion",
    "backplate", "back plate",
    "soporte para ", "soporte de ",
    "bracket", "mounting kit", "kit de montaje",
    "riser", "vertical gpu",
    "disipador para ", "heatsink",
)

# Tarjetas regalo / códigos digitales — su precio ES el valor facial,
# no tiene sentido compararlos con el producto que mencionan.
_GIFT_CARD_KEYWORDS = (
    "tarjeta de regalo", "tarjeta regalo", "gift card",
    "codigo digital", "código digital", "descarga digital",
    "digital code", "digital download",
    "tarjeta prepago", "tarjeta monedero",
    "saldo ", "credito ", "crédito ",
    "suscripcion ", "suscripción ",
    "pase de ", "pass ",
)


# Patrón "ACCESORIO + para/compatible con + MARCA": un accesorio que nombra el
# producto al que acompaña ("Soporte para PS5", "Cargador compatible con MacBook").
_ACCESSORY_FOR_BRAND_RE = re.compile(
    r'\b(?:para|compatible con)\s+(?:' + _BRANDS_ALT + r')\b',
    re.IGNORECASE,
)


def _is_accessory_title(title: str) -> bool:
    """Detecta si un título es claramente un accesorio (funda, cable, soporte…).

    Por keyword (_GLOBAL_ACCESSORY_KEYWORDS) o por el patrón "empieza por un
    sustantivo de accesorio (_ACCESSORY_PREFIXES) + nombra para/compatible con
    una marca conocida".
    """
    t = title.lower()
    if any(kw in t for kw in _GLOBAL_ACCESSORY_KEYWORDS):
        return True
    if (any(t.startswith(ap) for ap in _ACCESSORY_PREFIXES)
            and _ACCESSORY_FOR_BRAND_RE.search(t)):
        return True
    return False


# Productos premium que un accesorio suele nombrar. Si el título los menciona
# pero el precio es de accesorio, es sospechoso (ver _is_premium_named_at_accessory_price).
_PREMIUM_PRODUCT_RES = (
    re.compile(r'\biphone\b.{0,18}\bpro\b', re.IGNORECASE),
    re.compile(r'\bmacbook\b', re.IGNORECASE),
    re.compile(r'\b(?:imac|mac\s*studio|mac\s*pro)\b', re.IGNORECASE),
    re.compile(r'\brtx\s*[45]0[0-9]0\b', re.IGNORECASE),
    re.compile(r'\b(?:ps5|playstation\s*5)\b', re.IGNORECASE),
    re.compile(r'\bipad\s*pro\b', re.IGNORECASE),
)


def _mentions_premium_product(title: str) -> bool:
    return any(r.search(title) for r in _PREMIUM_PRODUCT_RES)


def _is_premium_named_at_accessory_price(deal: Deal) -> bool:
    """Coherencia categoría/precio: el título nombra un producto premium pero el
    precio es de accesorio (< 15% del market_price real). Probablemente es el
    accesorio, no el producto. Si hay product_id (EAN/ASIN) que confirme el
    producto real, NO se sospecha (se confía en el identificador)."""
    if deal.product_id:
        return False
    if not deal.market_price or deal.market_price <= 0:
        return False
    if not _mentions_premium_product(deal.title):
        return False
    return deal.current_price < deal.market_price * 0.15


def _is_accessory_deal(deal: Deal) -> bool:
    """Filtro de accesorio REFORZADO: por título (keywords + patrón para/compatible
    con marca) o por incoherencia precio/categoría vs un producto premium."""
    return _is_accessory_title(deal.title) or _is_premium_named_at_accessory_price(deal)


# Plataformas de consola — para detectar JUEGOS que mencionan la consola.
_CONSOLE_PLATFORM_RE = re.compile(
    r'\b(nintendo switch 2|nintendo switch oled|nintendo switch|'
    r'playstation 5|playstation 4|ps5|ps4|'
    r'xbox series x|xbox series s|xbox one)\b',
    re.IGNORECASE,
)
# Palabras que, justo ANTES del nombre de la plataforma, indican que el
# producto SÍ es la consola (no un juego). El resto de prefijos (un nombre
# de juego) implican que es un videojuego para esa plataforma.
_CONSOLE_QUALIFIERS = {
    "consola", "videoconsola", "nueva", "nuevo", "pack",
    # Marcas: una consola puede ir precedida por su marca ("Sony PlayStation 5
    # Pro"), pero un juego nunca ("God of War PlayStation 5", no "...Sony...").
    "sony", "microsoft", "nintendo", "sega", "valve",
}


def _is_console_game(title: str) -> bool:
    """Detecta si el título es un JUEGO para consola, no la consola en sí.

    Los juegos (p.ej. de El Corte Inglés) se titulan "<Nombre Juego> <Plataforma>",
    como "High on Life Nintendo Switch 2" o "Star Fox Nintendo Switch 2". El nombre
    de la plataforma va precedido por el título del juego, no por un cualificador
    ("Consola Nintendo Switch 2"). La consola real empieza por la plataforma o por
    un cualificador, así que solo marcamos cuando hay un prefijo que no lo es.
    """
    m = _CONSOLE_PLATFORM_RE.search(title)
    if not m:
        return False
    prefix = title[:m.start()].strip()
    if not prefix:
        return False  # empieza por la plataforma → es la consola/bundle
    last_word = prefix.lower().split()[-1]
    return last_word not in _CONSOLE_QUALIFIERS


def _is_misleading_product(title: str) -> bool:
    """Detecta productos cuyo título menciona un producto caro pero que NO lo son.

    Ejemplos:
    - "Alphacool Core RTX 5090, Refrigeración por agua" → water block, NO una GPU
    - "Tarjeta de Regalo PlayStation 50€" → gift card, NO una consola
    - "High on Life Nintendo Switch 2" → juego, NO la consola

    Estos productos generan falsos positivos de ERROR_DE_PRECIO porque su precio
    real (~200€ water block, ~50€ gift card/juego) se compara contra el producto
    principal (~1800€ GPU, ~400€ consola).
    """
    t = title.lower()
    return (any(kw in t for kw in _COMPONENT_KEYWORDS)
            or any(kw in t for kw in _GIFT_CARD_KEYWORDS)
            or _is_console_game(title))


def _is_refurbished(deal: Deal) -> bool:
    """Detecta si un deal es reacondicionado por tienda o título."""
    if deal.store in _REFURBISHED_STORES:
        return True
    title_lower = deal.title.lower()
    return any(kw in title_lower for kw in _REFURBISHED_KEYWORDS)


def _has_external_high_value_confirmation(
    deal: Deal, db: Database | None,
    min_observations: int, confirm_discount: float,
) -> bool:
    """¿El descuento alto está respaldado por una fuente EXTERNA?

    Para clasificar como ERROR_DE_PRECIO o chollo extremo, el valor alto NO puede
    venir solo del original_price que reporta la tienda (puede ser un PVP
    inventado). Hace falta AL MENOS UNA confirmación externa, cualquiera de:
      1. market_price (Idealo/cross-store) que confirme >= confirm_discount%.
      2. Historial propio con >= min_observations cuya mediana confirme el descuento.
      3. Mismo product_id (EAN/ASIN) visto en otra tienda a precio alto.
    """
    cur = deal.current_price
    if cur <= 0:
        return False

    # 1. market_price (Idealo o cross-store)
    if deal.market_price and deal.market_price > 0:
        if (1 - cur / deal.market_price) * 100 >= confirm_discount:
            return True

    if db is None:
        return False

    # 2. Historial propio (por product_id si lo hay; si no, por URL)
    stats = None
    if deal.product_id:
        stats = db.get_price_stats_by_product_id(deal.product_id)
    if stats is None and deal.url:
        stats = db.get_price_stats_by_url(deal.url)
    if stats and stats["observations"] >= min_observations:
        median = stats["median"]
        if median > 0 and (1 - cur / median) * 100 >= confirm_discount:
            return True

    # 3. Mismo product_id en otra tienda a precio alto
    if deal.product_id:
        other = db.get_max_price_by_product_id_other_store(deal.product_id, deal.store)
        if other and other > 0 and (1 - cur / other) * 100 >= confirm_discount:
            return True

    return False


# Descuento a partir del cual se permite enviar un error de precio SIN confirmación
# externa (excepción para tiendas sin EAN / Idealo caído).
_EXTREME_DISCOUNT_NO_CONFIRM = 90.0


def _value_status(
    deal: Deal, db: Database | None,
    min_observations: int, confirm_discount: float, discount_pct: float,
) -> str:
    """Decide si el valor alto basta para alertar, y cómo.

    Returns:
        'confirmado'    — respaldado por fuente externa (market/historial/cross-store).
        'no_confirmado' — SIN confirmación, pero descuento extremo (>= 90%) y el deal
                          NO es accesorio ni clon → se permite igualmente (muchos
                          errores reales vienen de tiendas sin EAN). Marcar como no
                          verificado (ERROR_NO_CONFIRMADO).
        'rechazado'     — sin confirmación y sin excepción → NO se envía.

    La excepción del 90% levanta SOLO el requisito de confirmación de valor: los
    filtros de accesorio (_is_accessory_deal) y de clon (_is_clone) siguen vigentes.
    """
    if _has_external_high_value_confirmation(deal, db, min_observations, confirm_discount):
        return "confirmado"
    if (discount_pct >= _EXTREME_DISCOUNT_NO_CONFIRM
            and not _is_accessory_deal(deal)
            and not _is_clone(deal.title.lower())):
        return "no_confirmado"
    return "rechazado"


# ------------------------------------------------------------------
# Verificación anti-fake: solo alertar bajadas de precio reales
# ------------------------------------------------------------------
def verify_real_deals(
    deals: list[Deal],
    db: Database,
    min_observations: int = 2,
    real_discount_min: float = 70.0,
    price_error_threshold: float = 50.0,
    min_savings: float = 80.0,
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
        # Filtro global anti-accesorio REFORZADO: fundas, cables, coolers,
        # "X para <marca>", o accesorio con precio incoherente vs producto premium.
        if _is_accessory_deal(deal):
            continue
        # Historial por product_id (EAN/ASIN) si lo tiene — evita el historial
        # envenenado cuando una URL cambia de producto; si no, por URL (como antes).
        if deal.product_id:
            stats = db.get_price_stats_by_product_id(deal.product_id)
        else:
            stats = db.get_price_stats_by_url(deal.url)

        # Producto nuevo — no tenemos historial
        if stats is None or stats["observations"] < min_observations:
            # Bypass: posible error de precio — alertar inmediatamente
            # Requisitos estrictos para evitar falsos positivos:
            # 1. NO tiendas de reacondicionados
            # 2. NO tiendas con precios inflados conocidos (Amazon marketplace)
            # 3. Descuento ≥ threshold Y ratio < 10
            # 4. Ahorro absoluto ≥ min_savings (de config.filters.min_savings, evitar basura barata)
            # 5. Precio mínimo ≥ 30€ (evitar accesorios)
            # 6. market_price OBLIGATORIO de fuente externa que confirme el
            #    descuento (market_discount >= price_error_threshold). Sin él NO
            #    se envía, aunque la tienda reporte un descuento enorme.
            # Calidad del producto: queremos cazar glitches extremos (aspirador
            # a 0.66€, TV a 0.01€) de productos REALES, no chatarra con precio
            # "anterior" inflado. La señal de "producto de verdad" es: marca
            # reconocida O tienda de retail fiable (no marketplace ni
            # reacondicionado — esos ya se excluyen abajo).
            trusted_retail = (deal.store not in _INFLATED_PRICE_STORES
                              and deal.store not in _REFURBISHED_STORES)
            quality_product = _has_known_brand(deal.title.lower()) or trusted_retail

            # Suelo de precio y tope de ratio: estrictos para productos dudosos
            # (anti-basura), pero relajados para productos de calidad — un glitch
            # real tiene precio ínfimo y ratio altísimo, que es justo lo deseable.
            min_price_floor = 0.0 if quality_product else 30.0
            max_ratio = float("inf") if quality_product else 10.0

            if (not _is_refurbished(deal)
                    and not _is_misleading_product(deal.title)
                    and deal.store not in _INFLATED_PRICE_STORES
                    and deal.original_price and deal.original_price > deal.current_price
                    and deal.current_price >= min_price_floor):
                calculated_discount = (1 - deal.current_price / deal.original_price) * 100
                price_ratio = deal.original_price / deal.current_price
                absolute_savings = deal.original_price - deal.current_price
                # Umbral mínimo según liquidez de reventa (alta 60 / media 70 / baja 80).
                liq_min = liquidity_min_discount(deal)
                if (calculated_discount >= liq_min
                        and price_ratio < max_ratio
                        and absolute_savings >= min_savings):
                    # Confirmación de valor: por fuente externa, o por la excepción
                    # de descuento extremo (>=90%) sin confirmar (errores reales de
                    # tiendas sin EAN). Accesorios y clones ya quedan fuera vía
                    # _value_status, que conserva esos filtros.
                    status = _value_status(
                        deal, db, min_observations, liq_min, calculated_discount)
                    if status != "rechazado":
                        deal.discount_pct = round(calculated_discount, 1)
                        if status == "confirmado":
                            deal.alert_tier = classify_deal(deal.discount_pct, price_error_threshold)
                            logger.warning(
                                "POSIBLE ERROR DE PRECIO (1ª detección, confirmado por "
                                "fuente externa): %s — %.2f€ (descuento: %.0f%%, ahorro: %.0f€)",
                                deal.title[:50], deal.current_price, deal.discount_pct, absolute_savings,
                            )
                        else:  # no_confirmado (excepción >=90%)
                            deal.alert_tier = "ERROR_NO_CONFIRMADO"
                            logger.warning(
                                "POSIBLE ERROR DE PRECIO SIN CONFIRMAR (>=90%%, sin fuente "
                                "externa): %s — %.2f€ (descuento: %.0f%%, ahorro: %.0f€)",
                                deal.title[:50], deal.current_price, deal.discount_pct, absolute_savings,
                            )
                        verified.append(deal)
                        continue
                    logger.debug(
                        "NO ENVIADO (1ª detección, sin confirmación ni excepción >=90%%): "
                        "%s — tienda dice %.0f%%, registrando para futuro",
                        deal.title[:50], calculated_discount,
                    )
                    # Rechazado → cae al registro-para-futuro de abajo.

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

        # Umbral de descuento mínimo según la liquidez de reventa de la marca
        # (alta 60% / media 70% / baja 80%) en vez de un umbral fijo.
        effective_min = liquidity_min_discount(deal)

        # Ahorro absoluto mínimo (de config filters.min_savings): no alertar por poco
        savings = median_price - deal.current_price

        if real_discount < effective_min or savings < min_savings:
            fake_count += 1
            logger.debug(
                "DESCARTADO: %s — %.2f€ (mediana: %.2f€, descuento real: %.1f%%, ahorro: %.0f€, umbral liquidez: %.0f%%)",
                deal.title[:50], deal.current_price, median_price, real_discount, savings, effective_min,
            )
            continue

        # Liquidez BAJA (marca no reconocida): exige confirmación externa del valor
        # alto, no basta con el historial propio.
        if liquidity_tier(deal) == "baja" and not _has_external_high_value_confirmation(
                deal, db, min_observations, effective_min):
            fake_count += 1
            logger.debug(
                "DESCARTADO (liquidez baja sin confirmación externa): %s — %.2f€",
                deal.title[:50], deal.current_price,
            )
            continue

        # El precio actual es significativamente menor que la mediana — oferta real
        logger.info(
            "REAL: %s — %.2f€ (mediana: %.2f€, descuento real: -%.1f%%, ahorro: %.0f€)",
            deal.title[:50], deal.current_price, median_price, real_discount, savings,
        )

        # Reemplazar el descuento de la tienda por el descuento real
        deal.discount_pct = real_discount
        deal.original_price = median_price
        tier = classify_deal(real_discount, price_error_threshold)

        # Bloquear ERROR_DE_PRECIO para productos engañosos (water blocks,
        # gift cards, etc.) — su historial puede estar contaminado.
        if tier == "ERROR_DE_PRECIO" and _is_misleading_product(deal.title):
            logger.info(
                "BLOQUEADO (producto engañoso): %s — tier %s rebajado a CHOLLO",
                deal.title[:50], tier,
            )
            tier = "CHOLLO"

        deal.alert_tier = tier
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

_MIN_STORE_PRODUCTS = 50      # Mínimo de productos para comparar a nivel tienda
_MIN_CATEGORY_PRODUCTS = 15   # Mínimo de productos para comparar a nivel categoría


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

        # Filtro anti-accesorio: adaptadores, cargadores, fundas, cables...
        if _is_accessory_title(deal.title):
            continue

        # Solo productos nuevos o con poco historial
        stats = db.get_price_stats_by_url(deal.url)
        if stats is not None and stats["observations"] >= min_observations:
            continue

        flagged = False
        ref_label = ""
        ref_p5 = 0.0
        threshold = 0.0

        # Tier 0: marca reconocida a precio absurdo (< 50€ para producto premium)
        # Un MacBook a 10€, unas AirPods a 3€, una RTX a 5€ — siempre alertar
        if _has_known_brand(deal.title.lower()) and deal.current_price < 50:
            flagged = True
            ref_label = f"{deal.store}/marca_premium"
            ref_p5 = 200.0  # Estimación conservadora
            threshold = 50.0

        # Tier 1: store + category — precio < 10% del P5
        if not flagged and deal.category:
            ref = db.get_store_price_percentiles(deal.store, deal.category)
            if ref and ref["count"] >= _MIN_CATEGORY_PRODUCTS:
                threshold = ref["p5"] * 0.10
                if threshold > 0 and deal.current_price < threshold:
                    flagged = True
                    ref_label = f"{deal.store}/{deal.category}"
                    ref_p5 = ref["p5"]

        # Tier 2 fallback: store completa
        if not flagged:
            ref = db.get_store_price_percentiles(deal.store)
            if ref is None or ref["count"] < _MIN_STORE_PRODUCTS:
                continue

            multiplier = 0.03 if deal.store in _CHEAP_STORES else 0.08
            threshold = ref["p5"] * multiplier

            if threshold <= 0 or deal.current_price >= threshold:
                continue

            flagged = True
            ref_label = deal.store
            ref_p5 = ref["p5"]

        if flagged:
            synthetic_discount = round((1 - deal.current_price / ref_p5) * 100, 1)
            savings = ref_p5 - deal.current_price

            # Aplicar mismos umbrales que el resto del pipeline:
            # 50% para ≥100€, 60% para <100€, mínimo 50€ de ahorro
            effective_threshold = 50.0
            if ref_p5 < 100:
                effective_threshold = 60.0

            if synthetic_discount < effective_threshold or savings < 50:
                logger.debug(
                    "ABSURDO DESCARTADO: %s — %.2f€ (descuento: %.0f%%, "
                    "ahorro: %.0f€, umbral: %.0f%%)",
                    deal.title[:50], deal.current_price,
                    synthetic_discount, savings, effective_threshold,
                )
                continue

            deal.discount_pct = synthetic_discount
            deal.original_price = ref_p5
            deal.alert_tier = "ERROR_DE_PRECIO"

            logger.warning(
                "ABSURDAMENTE BARATO: %s — %.2f€ (P5 de %s: %.2f€, "
                "umbral: %.2f€, descuento sintético: %.0f%%, ahorro: %.0f€)",
                deal.title[:50], deal.current_price, ref_label,
                ref_p5, threshold, synthetic_discount, savings,
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
    db: Database | None = None,
    min_observations: int = 2,
    price_error_threshold: float = 50.0,
) -> list[Deal]:
    """Busca deals que coincidan con la watchlist del usuario.

    Matching estricto: substring exacto (case-insensitive) con validación
    de contexto para evitar falsos positivos.

    Si se pasa db, los productos de la watchlist dinámica (SQLite) se
    unen con los del YAML.

    Returns:
        Lista de deals que matchean la watchlist y cumplen el precio.
    """
    if not watchlist_cfg.get("enabled", False):
        return []

    products = list(watchlist_cfg.get("products", []))

    # Merge con watchlist dinámica de la BD
    if db is not None:
        db_items = db.get_watchlist_items()
        yaml_names = {p["name"].lower() for p in products}
        for item in db_items:
            if item["name"].lower() not in yaml_names:
                products.append(item)

    if not products:
        return []

    matched: list[Deal] = []
    seen_urls: set[str] = set()

    for deal in deals:
        if deal.url in seen_urls:
            continue
        # Filtro anti-accesorio/engañoso: la watchlist matchea por substring,
        # así que un water block ("RTX 5090 Refrigeración por agua") o un juego
        # ("High on Life Nintendo Switch 2") matchearía el nombre del producto.
        # Aplicar los mismos filtros que el resto del pipeline.
        if _is_accessory_deal(deal) or _is_misleading_product(deal.title):
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
            if any(kw in title_lower for kw in exclude_kws):
                continue

            # Descuento de la TIENDA (real, vs su precio tachado)
            store_discount = 0.0
            if deal.original_price and deal.original_price > deal.current_price:
                store_discount = round(
                    (1 - deal.current_price / deal.original_price) * 100, 1
                )

            # Descuento de WATCHLIST: debe medirse contra el PVP/valor de mercado
            # REAL, no contra max_price (que es solo el límite de alerta, no el
            # MSRP). Referencia por orden de preferencia: msrp del producto ->
            # market_price del deal. Si no hay ninguna, watchlist_discount = None
            # y NO se calcula un descuento sintético: el deal solo podrá pasar por
            # descuento real de tienda (vía 1) o error de precio absoluto (vía 2),
            # nunca por un watchlist_discount inventado.
            msrp = product.get("msrp")
            reference_price = float(msrp) if msrp else deal.market_price
            watchlist_discount = None
            if reference_price and reference_price > deal.current_price:
                watchlist_discount = round(
                    (1 - deal.current_price / reference_price) * 100, 1
                )
            refurbished = _is_refurbished(deal)

            # Umbral de descuento mínimo según la liquidez de reventa de la marca
            # (alta 60% / media 70% / baja 80%) en vez de un umbral fijo.
            effective_threshold = liquidity_min_discount(deal)
            min_savings_wl = 80.0

            # --- Vía 1: La tienda muestra descuento significativo ---
            if store_discount >= effective_threshold and not refurbished:
                store_savings = (deal.original_price - deal.current_price
                                 if deal.original_price else 0)
                if store_savings < min_savings_wl:
                    logger.debug(
                        "WATCHLIST SKIP %s (vía 1): %s — ahorro %.0f€ < %.0f€",
                        product["name"], deal.title[:50],
                        store_savings, min_savings_wl,
                    )
                    continue
                # ERROR_DE_PRECIO si el precio es < 25% del límite de alerta, o si
                # el descuento es desorbitado (>85%). 70-85% = CHOLLO (buen
                # descuento, no necesariamente un error).
                if deal.current_price < max_price * 0.25:
                    deal.alert_tier = "ERROR_DE_PRECIO"
                elif store_discount > 85:
                    deal.alert_tier = "ERROR_DE_PRECIO"
                else:
                    deal.alert_tier = "CHOLLO"
                deal.discount_pct = store_discount
                # Mantener original_price de la tienda

            # --- Vía 2: Precio absurdamente bajo (error de precio absoluto) ---
            # Cualifica por estar muy por debajo del límite de alerta, NO por
            # watchlist_discount. El descuento mostrado es el REAL (vs referencia)
            # si la tenemos; si no, el de la tienda. Nunca el sintético vs max_price.
            elif deal.current_price < max_price * 0.25 and not refurbished:
                wl_savings = max_price - deal.current_price
                if wl_savings < min_savings_wl:
                    continue
                deal.alert_tier = "ERROR_DE_PRECIO"
                deal.discount_pct = (watchlist_discount
                                     if watchlist_discount is not None
                                     else store_discount)
                deal.original_price = deal.original_price or reference_price

            # --- Vía 3: Tienda de reacondicionados ---
            # Solo cualifica por descuento real vs referencia (msrp/market_price).
            # Sin referencia no hay watchlist_discount → no cualifica.
            elif refurbished:
                if watchlist_discount is None or watchlist_discount < effective_threshold:
                    continue
                wl_savings = reference_price - deal.current_price
                if wl_savings < min_savings_wl:
                    continue
                deal.alert_tier = "CHOLLO"
                deal.discount_pct = watchlist_discount
                deal.original_price = None  # No mostrar MSRP viejo

            # --- No es un chollo real: descuento bajo o modelo barato ---
            else:
                logger.debug(
                    "WATCHLIST SKIP %s: %s — %.2f€ (store_disc=%.0f%%, "
                    "wl_disc=%s, umbral=%.0f%%, no vía aplica)",
                    product["name"], deal.title[:50],
                    deal.current_price, store_discount,
                    f"{watchlist_discount:.0f}%" if watchlist_discount is not None else "n/d",
                    effective_threshold,
                )
                continue

            # Confirmación de valor cuando el valor alto no puede venir solo del
            # original_price: liquidez BAJA, o errores/chollos extremos (>= 85%).
            # Permite la excepción del 90% sin confirmar (conservando accesorio/clon).
            if (liquidity_tier(deal) == "baja" or deal.discount_pct >= 85
                    or deal.alert_tier == "ERROR_DE_PRECIO"):
                status = _value_status(
                    deal, db, min_observations, effective_threshold, deal.discount_pct)
                if status == "rechazado":
                    logger.debug(
                        "WATCHLIST SKIP %s: %s — %.0f%% sin confirmación externa ni "
                        "excepción >=90%% (no se envía)",
                        product["name"], deal.title[:50], deal.discount_pct,
                    )
                    continue
                if status == "no_confirmado":
                    deal.alert_tier = "ERROR_NO_CONFIRMADO"

            logger.info(
                "WATCHLIST %s: %s — %.2f€ (max: %.2f€, tier: %s, "
                "store_disc=%.0f%%, wl_disc=%s)",
                product["name"], deal.title[:50],
                deal.current_price, max_price, deal.alert_tier,
                store_discount,
                f"{watchlist_discount:.0f}%" if watchlist_discount is not None else "n/d",
            )
            matched.append(deal)
            seen_urls.add(deal.url)
            break

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
    - Rechaza si hay un "para " en cualquier lugar antes del match
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

    # Rechazar si "para " / "for " aparece en cualquier parte antes del match
    # Ejemplo: "Almohadillas para Airpods Pro y Airpods Pro 2"
    # El "para " indica que es un accesorio para el producto, no el producto.
    if " para " in prefix or prefix.startswith("para "):
        return False
    if " for " in prefix or prefix.startswith("for "):
        return False
    if " compatible " in prefix or prefix.startswith("compatible "):
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
    raw_pairs = db.find_cross_store_deals(
        hours=hours, fuzzy_threshold=fuzzy_threshold,
        min_discount_pct=min_discount_pct,
    )

    # Filtro anti-accesorio reforzado sobre el deal barato (es el que se envía).
    pairs = [
        (cheap, expensive) for cheap, expensive in raw_pairs
        if not (_is_accessory_deal(cheap) or _is_misleading_product(cheap.title))
    ]

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


# ------------------------------------------------------------------
# Detectar bajadas de precio significativas vs mediana histórica
# ------------------------------------------------------------------
def detect_price_drops(
    deals: list[Deal],
    db: Database,
    drop_threshold: float = 70.0,
    min_observations: int = 3,
    min_savings: float = 80.0,
) -> list[Deal]:
    """Detecta deals con bajada brutal respecto a su mediana histórica.

    Solo alerta si el descuento es realmente rentable para reventa:
    - Bajada ≥ drop_threshold% vs mediana
    - Ahorro absoluto ≥ min_savings€
    - Productos baratos (<100€) necesitan ≥60% de bajada

    Args:
        deals: Deals a analizar.
        db: Base de datos con historial.
        drop_threshold: % mínimo de bajada vs mediana para alertar.
        min_observations: Mínimo de observaciones de precio necesarias.
        min_savings: Ahorro mínimo en euros para alertar.

    Returns:
        Lista de deals con bajada significativa, con alert_tier="BAJADA_PRECIO".
    """
    drops: list[Deal] = []

    for deal in deals:
        # Filtro anti-accesorio reforzado (antes solo lo tenían verify/watchlist).
        if _is_accessory_deal(deal) or _is_misleading_product(deal.title):
            continue
        # Historial por product_id (EAN/ASIN) si lo tiene; si no, por deal_id (URL).
        if deal.product_id:
            stats = db.get_price_stats_by_product_id(deal.product_id)
        elif deal.id is not None:
            stats = db.get_price_stats(deal.id)
        else:
            continue
        if stats is None:
            continue
        if stats["observations"] < min_observations:
            continue

        median = stats["median"]
        current = deal.current_price
        if median <= 0 or current >= median:
            continue

        drop_pct = round((1 - current / median) * 100, 1)
        savings = median - current

        # Productos baratos necesitan bajada más agresiva
        effective_threshold = drop_threshold  # 70% para ≥100€
        if median < 100:
            effective_threshold = max(drop_threshold, 80.0)  # 80% para <100€

        if drop_pct < effective_threshold:
            continue
        if savings < min_savings:
            continue

        deal.alert_tier = "BAJADA_PRECIO"
        deal.original_price = median
        deal.discount_pct = drop_pct
        drops.append(deal)
        logger.info(
            "BAJADA PRECIO: %s — %.2f€ (mediana: %.2f€, bajada: -%.1f%%, ahorro: %.0f€)",
            deal.title[:50], current, median, drop_pct, savings,
        )

    if drops:
        logger.info("Bajadas de precio: %d productos con bajada significativa", len(drops))
    return drops
