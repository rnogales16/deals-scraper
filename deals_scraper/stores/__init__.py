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
from .cex import CeXStore
from .decathlon import DecathlonStore
from .generic import GenericStore
from .mediamarkt import MediaMarktStore
from .pccomponentes import PcComponentesStore
from .woocommerce import WooCommerceStore

# Tiendas con scraper específico
STORE_REGISTRY: dict[str, type[BaseStore]] = {
    "amazon": AmazonStore,
    "cex": CeXStore,
    "decathlon": DecathlonStore,
    "mediamarkt": MediaMarktStore,
    "pccomponentes": PcComponentesStore,
    "lifeinformatica": WooCommerceStore,
}

# Scraper por defecto para tiendas sin implementación específica
DEFAULT_STORE_CLASS: type[BaseStore] = GenericStore
