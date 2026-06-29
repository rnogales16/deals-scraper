"""Registro de scrapers de tiendas.

Tiendas con scraper específico: amazon, pccomponentes, mediamarkt.
Todas las demás usan GenericStore (JSON-LD, microdata, Open Graph, CSS patterns).

Para añadir una nueva tienda:
1. Añádela en config.yaml con client_type: browser
2. Se usará GenericStore automáticamente si no hay scraper específico.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BaseStore

from .amazon import AmazonStore
from .cecotec import CecotecStore
from .cex import CeXStore
from .conforama import ConforamaStore
from .decathlon import DecathlonStore
from .elcorteingles import ElCorteInglesStore
from .generic import GenericStore
from .lidl import LidlStore
from .mediamarkt import MediaMarktStore
from .pccomponentes import PcComponentesStore
from .thomann import ThomannStore
from .vsgamers import VSGamersStore
from .woocommerce import WooCommerceStore
from .worten import WortenStore

# Tiendas con scraper específico
STORE_REGISTRY: dict[str, type[BaseStore]] = {
    "amazon": AmazonStore,
    "cecotec": CecotecStore,
    "cex": CeXStore,
    "conforama": ConforamaStore,
    "decathlon": DecathlonStore,
    "elcorteingles": ElCorteInglesStore,
    "lidl": LidlStore,
    "mediamarkt": MediaMarktStore,
    "pccomponentes": PcComponentesStore,
    "thomann": ThomannStore,
    "vsgamers": VSGamersStore,
    "lifeinformatica": WooCommerceStore,
    # Store dedicado al motor de reventa (Fase 1). El "worten" normal sigue en
    # GenericStore para no romper sus alertas de descuento existentes.
    "worten_flip": WortenStore,
}

# Scraper por defecto para tiendas sin implementación específica
DEFAULT_STORE_CLASS: type[BaseStore] = GenericStore
