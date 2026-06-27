"""Dataclasses para representar ofertas y configuración de tiendas."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Deal:
    """Representa una oferta encontrada en una tienda."""

    title: str
    url: str
    store: str
    current_price: float
    original_price: float | None = None
    discount_pct: float = 0.0
    category: str = ""
    currency: str = "EUR"
    image_url: str = ""
    # Identificador único de producto con prefijo de tipo: "ean:8412345678901",
    # "gtin:...", "asin:B0XXXX". Opcional; eje futuro del matching anti-fake.
    product_id: str | None = None
    detected_at: datetime = field(default_factory=datetime.utcnow)
    sent_to_telegram: bool = False
    id: int | None = None
    alert_tier: str = "NORMAL"  # "NORMAL" | "CHOLLO" | "ERROR_DE_PRECIO" (not persisted)
    market_price: float | None = None       # Precio de mercado validado (not persisted)
    market_price_source: str = ""           # "cache(idealo)" | "cross_store" | "idealo"

    def __post_init__(self) -> None:
        if self.original_price and self.original_price > 0 and self.discount_pct == 0.0:
            self.discount_pct = round(
                (1 - self.current_price / self.original_price) * 100, 1
            )


@dataclass
class StoreConfig:
    """Configuración de una tienda para scraping."""

    name: str
    enabled: bool
    interval_minutes: int
    scrape_urls: list[str]
    client_type: str  # "http" o "browser"
    force_stealth: bool = False  # Forzar modo stealth aunque speed_mode=fast
    proxy_url: str | None = None  # Proxy override per-store
    wait_for_selector: str | None = None  # CSS selector to wait for before capturing HTML
    use_system_chrome: bool = False  # Use system Chrome (channel='chrome') to bypass TLS fingerprinting
    headful: bool = False  # Run a real (non-headless) browser, off-screen — needed for Akamai (e.g. El Corte Inglés)

    @classmethod
    def from_dict(cls, data: dict) -> StoreConfig:
        return cls(
            name=data["name"],
            enabled=data.get("enabled", True),
            interval_minutes=data.get("interval_minutes", 60),
            scrape_urls=data.get("scrape_urls", []),
            client_type=data.get("client_type", "http"),
            force_stealth=data.get("force_stealth", False),
            proxy_url=data.get("proxy_url"),
            wait_for_selector=data.get("wait_for_selector"),
            use_system_chrome=data.get("use_system_chrome", False),
            headful=data.get("headful", False),
        )
