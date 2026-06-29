"""Motor de valor de reventa — Fase 1: Apple / MacBooks.

Para tiendas que NO muestran precio "antes" (p.ej. Worten), estima el valor de
mercado de un MacBook a partir de su MODELO (specs) usando el precio NUEVO del
mismo modelo en mis tiendas que SÍ dan precio (cruce reusando deals.db), y lo
compara con el precio de la tienda para detectar márgenes de reventa.

Filosofía (igual que anti-fake): **FAIL CLOSED**. Si no se extraen specs con
confianza (línea + tamaño + chip + RAM + SSD), NO se emite señal. Mejor perder
una oferta que inventar un margen (= comprar stock malo).

Fase 1 deliberadamente acotada: solo MacBooks. No generalizar hasta validar.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Extracción de specs de MacBook (el matcher estricto)
# ---------------------------------------------------------------------------
# RAM válida (GB) vs SSD: en MacBooks la RAM es ≤ 128GB y el SSD ≥ 256GB, sin
# solape → un "<n>GB" se desambigua por el valor. TB siempre es almacenamiento.
_RAM_GB_VALUES = {8, 16, 18, 24, 32, 36, 48, 64, 96, 128}
_SSD_GB_VALUES = {256, 512}

_LINE_RE = re.compile(r"macbook\s*(air|pro)", re.I)
_CHIP_RE = re.compile(r"\bm([1-5])\s*(pro|max|ultra)?\b", re.I)
# tamaño: 13/14/15/16, con o sin marca de pulgadas, idealmente cerca de "macbook"
_SIZE_RE = re.compile(r"\b(13|14|15|16)(?:[.,]\d)?\s*(?:\"|''|”|pulg|inch|\b)", re.I)
_GB_RE = re.compile(r"(\d{1,4})\s*gb", re.I)
_TB_RE = re.compile(r"(\d)\s*tb", re.I)

# tamaños plausibles por línea (Air: 13/15, Pro: 14/16) → valida el tamaño
_VALID_SIZE = {"air": {13, 15}, "pro": {14, 16}}


@dataclass(frozen=True)
class MacSpecs:
    line: str       # "air" | "pro"
    size: int       # 13 | 14 | 15 | 16
    chip: str       # "m2", "m3pro", "m3max", ...
    ram_gb: int
    ssd_gb: int     # en GB (1TB = 1024)

    @property
    def key(self) -> str:
        return f"macbook-{self.line}-{self.size}-{self.chip}-{self.ram_gb}gb-{self.ssd_gb}"


def extract_mac_specs(title: str) -> MacSpecs | None:
    """Extrae specs de un título de MacBook. FAIL CLOSED → None si falta algo."""
    t = title.lower()
    if "macbook" not in t:
        return None

    m_line = _LINE_RE.search(t)
    if not m_line:
        return None
    line = m_line.group(1).lower()

    m_chip = _CHIP_RE.search(t)
    if not m_chip:
        return None
    variant = (m_chip.group(2) or "").lower()
    chip = f"m{m_chip.group(1)}{variant}"

    # tamaño: primer 13/14/15/16 que sea válido para la línea
    size = None
    for m in _SIZE_RE.finditer(t):
        s = int(m.group(1))
        if s in _VALID_SIZE[line]:
            size = s
            break
    if size is None:
        return None

    # RAM y SSD a partir de todos los GB/TB del título
    gbs = [int(x) for x in _GB_RE.findall(t)]
    tbs = [int(x) for x in _TB_RE.findall(t)]
    ram = next((g for g in gbs if g in _RAM_GB_VALUES), None)
    ssd = next((g for g in gbs if g in _SSD_GB_VALUES), None)
    if ssd is None and tbs:
        ssd = tbs[0] * 1024
    if ram is None or ssd is None:
        return None

    return MacSpecs(line=line, size=size, chip=chip, ram_gb=ram, ssd_gb=ssd)


# ---------------------------------------------------------------------------
# Estimación de valor (precio NUEVO cruzado desde deals.db)
# ---------------------------------------------------------------------------
# Tiendas cuyo precio NO es "nuevo de referencia" (refurb/segunda mano) → excluir
_NON_NEW_STORES = {"backmarket", "apple", "cex", "worten", "worten_flip"}
_REFURB_WORDS = ("reacond", "renew", "refurb", "seminuevo", "usado", "segunda mano")


@dataclass
class ValueEstimate:
    price: float            # mediana del precio nuevo cruzado
    n: int                  # nº de observaciones
    sources: list[str]      # tiendas de las que sale el cruce
    samples: list[tuple[str, float]] = field(default_factory=list)  # (store, price)


def estimate_new_price(specs: MacSpecs, db, *, recency_days: int = 21,
                       min_observations: int = 2) -> ValueEstimate | None:
    """Mediana del precio NUEVO del MISMO modelo (misma key) en mis tiendas.

    FAIL CLOSED: None si no hay suficientes observaciones recientes con la MISMA
    key canónica (mismo chip/RAM/SSD/tamaño) — no aproxima a un modelo parecido.
    """
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=recency_days)).isoformat()
    cur = db.conn.cursor()
    cur.execute(
        "SELECT store, current_price, title FROM deals "
        "WHERE LOWER(title) LIKE '%macbook%' AND updated_at > ? ",
        (cutoff,),
    )
    by_store: dict[str, list[float]] = {}
    samples: list[tuple[str, float]] = []
    for row in cur.fetchall():
        store = row["store"]
        title = row["title"]
        price = row["current_price"]
        if store in _NON_NEW_STORES:
            continue
        if any(w in title.lower() for w in _REFURB_WORDS):
            continue
        cand = extract_mac_specs(title)
        if cand is None or cand.key != specs.key:
            continue
        by_store.setdefault(store, []).append(price)
        samples.append((store, price))

    prices = [p for ps in by_store.values() for p in ps]
    if len(prices) < min_observations:
        return None
    return ValueEstimate(
        price=round(statistics.median(prices), 2),
        n=len(prices),
        sources=sorted(by_store.keys()),
        samples=samples[:8],
    )


# ---------------------------------------------------------------------------
# Evaluación de flip (la señal comercial)
# ---------------------------------------------------------------------------
@dataclass
class FlipCandidate:
    title: str
    url: str
    store: str
    store_price: float
    specs_key: str
    new_price: float            # mediana nuevo cruzado
    new_sources: list[str]
    new_n: int
    resale_ratio: float
    resale_value: float         # new_price * resale_ratio
    margin_eur: float
    margin_pct: float
    samples: list[tuple[str, float]]


def evaluate_flip(deal, db, *, resale_ratio: float = 0.85,
                  min_margin_eur: float = 150.0, min_margin_pct: float = 0.20,
                  recency_days: int = 21, min_observations: int = 2) -> FlipCandidate | None:
    """Evalúa un deal de tienda-sin-referencia como oportunidad de reventa.

    Devuelve un FlipCandidate solo si: specs extraíbles (FAIL CLOSED) + baseline
    de nuevo suficiente + margen comercial (€ y % mínimos). Si no, None.
    """
    specs = extract_mac_specs(deal.title)
    if specs is None:
        return None
    est = estimate_new_price(specs, db, recency_days=recency_days,
                             min_observations=min_observations)
    if est is None:
        return None
    resale_value = round(est.price * resale_ratio, 2)
    margin_eur = round(resale_value - deal.current_price, 2)
    if margin_eur <= 0:
        return None
    margin_pct = margin_eur / resale_value
    if margin_eur < min_margin_eur or margin_pct < min_margin_pct:
        return None
    return FlipCandidate(
        title=deal.title, url=deal.url, store=deal.store,
        store_price=deal.current_price, specs_key=specs.key,
        new_price=est.price, new_sources=est.sources, new_n=est.n,
        resale_ratio=resale_ratio, resale_value=resale_value,
        margin_eur=margin_eur, margin_pct=round(margin_pct, 3),
        samples=est.samples,
    )
