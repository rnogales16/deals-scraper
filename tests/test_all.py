"""Test suite completo para deals-scraper.

Cubre: models, filters, database, generic store (price parsing + strategies),
telegram_bot formatting, config, y la integración entre componentes.

Ejecutar: python -m pytest tests/test_all.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Imports del proyecto
# ---------------------------------------------------------------------------
from deals_scraper.models import Deal, StoreConfig
from deals_scraper.filters import (
    apply_filters,
    calculate_discount,
    check_watchlist,
    classify_deal,
    detect_absurdly_cheap,
    detect_cross_store_bargains,
    infer_category_from_title,
    infer_category_from_url,
    normalize_category,
    verify_real_deals,
)
from deals_scraper.database import Database
from deals_scraper.stores.generic import GenericStore, _parse_price, _extract_price_text
from deals_scraper.stores.cex import CeXStore
from deals_scraper.stores.woocommerce import WooCommerceStore
from deals_scraper.stores.base import _looks_like_product_url
from deals_scraper.telegram_bot import TelegramBot, _escape_html, _safe_title
from deals_scraper.config import (
    get_anti_ban,
    get_filters,
    get_speed,
    get_store_configs,
    load_config,
)


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def tmp_db(tmp_path):
    """Base de datos temporal en disco para tests."""
    db_path = tmp_path / "test_deals.db"
    db = Database(db_path=db_path)
    yield db
    db.close()


@pytest.fixture
def sample_deal():
    """Deal de ejemplo para reusar en múltiples tests."""
    return Deal(
        title="Samsung Galaxy S25 Ultra 256GB",
        url="https://example.com/galaxy-s25",
        store="pccomponentes",
        current_price=899.99,
        original_price=1299.99,
        image_url="https://example.com/img.jpg",
        category="phones",
    )


@pytest.fixture
def sample_store_config():
    """StoreConfig de ejemplo."""
    return StoreConfig(
        name="teststore",
        enabled=True,
        interval_minutes=30,
        scrape_urls=["https://example.com/offers"],
        client_type="browser",
        force_stealth=False,
    )


def _make_deal(title="Test Product", url="https://x.com/p", store="store1",
               current_price=100.0, original_price=None, discount_pct=0.0,
               category="", alert_tier="NORMAL", **kwargs):
    """Helper para crear deals rápidamente."""
    return Deal(
        title=title, url=url, store=store,
        current_price=current_price, original_price=original_price,
        discount_pct=discount_pct, category=category,
        alert_tier=alert_tier, **kwargs,
    )


# ===========================================================================
# 1. MODELS
# ===========================================================================

class TestDeal:
    """Tests para el modelo Deal."""

    def test_auto_discount_calculation(self):
        """Si hay original_price y discount_pct es 0, lo calcula."""
        d = Deal(
            title="Test", url="https://x.com", store="s",
            current_price=70.0, original_price=100.0,
        )
        assert d.discount_pct == 30.0

    def test_no_auto_discount_if_already_set(self):
        """Si discount_pct ya está seteado, no lo recalcula."""
        d = Deal(
            title="Test", url="https://x.com", store="s",
            current_price=70.0, original_price=100.0, discount_pct=25.0,
        )
        assert d.discount_pct == 25.0

    def test_no_discount_without_original(self):
        """Sin original_price, discount_pct se queda en 0."""
        d = Deal(
            title="Test", url="https://x.com", store="s",
            current_price=70.0,
        )
        assert d.discount_pct == 0.0

    def test_default_alert_tier(self):
        """Alert tier por defecto es NORMAL."""
        d = _make_deal()
        assert d.alert_tier == "NORMAL"

    def test_default_currency(self):
        d = _make_deal()
        assert d.currency == "EUR"


class TestStoreConfig:
    """Tests para StoreConfig."""

    def test_from_dict_minimal(self):
        sc = StoreConfig.from_dict({"name": "test", "scrape_urls": ["https://x.com"]})
        assert sc.name == "test"
        assert sc.enabled is True
        assert sc.interval_minutes == 60
        assert sc.client_type == "http"
        assert sc.force_stealth is False

    def test_from_dict_full(self):
        sc = StoreConfig.from_dict({
            "name": "worten",
            "enabled": True,
            "interval_minutes": 15,
            "scrape_urls": ["https://worten.es/offers"],
            "client_type": "browser",
            "force_stealth": True,
        })
        assert sc.name == "worten"
        assert sc.force_stealth is True
        assert sc.client_type == "browser"
        assert sc.interval_minutes == 15

    def test_from_dict_defaults(self):
        """Campos opcionales toman valores por defecto."""
        sc = StoreConfig.from_dict({"name": "x", "scrape_urls": []})
        assert sc.enabled is True
        assert sc.force_stealth is False


# ===========================================================================
# 2. FILTERS
# ===========================================================================

class TestCalculateDiscount:
    def test_basic_discount(self):
        assert calculate_discount(70, 100) == 30.0

    def test_no_original(self):
        assert calculate_discount(70, None) == 0.0

    def test_zero_original(self):
        assert calculate_discount(70, 0) == 0.0

    def test_current_equals_original(self):
        assert calculate_discount(100, 100) == 0.0

    def test_current_above_original(self):
        """Si el precio actual es mayor, descuento = 0."""
        assert calculate_discount(120, 100) == 0.0

    def test_high_discount(self):
        assert calculate_discount(10, 100) == 90.0


class TestClassifyDeal:
    def test_normal(self):
        assert classify_deal(15.0) == "NORMAL"

    def test_chollo(self):
        assert classify_deal(35.0) == "CHOLLO"

    def test_error_de_precio(self):
        assert classify_deal(55.0) == "ERROR_DE_PRECIO"

    def test_boundary_30(self):
        assert classify_deal(30.0) == "CHOLLO"

    def test_boundary_50(self):
        assert classify_deal(50.0) == "ERROR_DE_PRECIO"

    def test_custom_threshold(self):
        assert classify_deal(45.0, price_error_threshold=40.0) == "ERROR_DE_PRECIO"


class TestApplyFilters:
    def test_min_discount_filter(self):
        deals = [
            _make_deal(title="A", current_price=80, original_price=100),  # 20%
            _make_deal(title="B", current_price=95, original_price=100),  # 5%
        ]
        result = apply_filters(deals, {"min_discount": 15})
        assert len(result) == 1
        assert result[0].title == "A"

    def test_price_range_filter(self):
        deals = [
            _make_deal(title="Cheap", current_price=5, discount_pct=50),
            _make_deal(title="Mid", current_price=50, discount_pct=50),
            _make_deal(title="Expensive", current_price=500, discount_pct=50),
        ]
        result = apply_filters(deals, {"min_discount": 0, "price_min": 10, "price_max": 200})
        assert len(result) == 1
        assert result[0].title == "Mid"

    def test_keywords_filter(self):
        deals = [
            _make_deal(title="iPhone 15 Pro", discount_pct=20),
            _make_deal(title="Samsung Galaxy S25", discount_pct=20),
        ]
        result = apply_filters(deals, {"min_discount": 0, "keywords": ["iphone"]})
        assert len(result) == 1
        assert "iPhone" in result[0].title

    def test_categories_filter(self):
        deals = [
            _make_deal(title="A", category="phones", discount_pct=20),
            _make_deal(title="B", category="laptops", discount_pct=20),
        ]
        result = apply_filters(deals, {"min_discount": 0, "categories": ["phones"]})
        assert len(result) == 1
        assert result[0].category == "phones"

    def test_no_filters_passes_all(self):
        deals = [_make_deal(title=f"Deal {i}", discount_pct=20) for i in range(5)]
        result = apply_filters(deals, {"min_discount": 0})
        assert len(result) == 5

    def test_recalculates_discount_if_zero(self):
        d = _make_deal(current_price=50, original_price=100, discount_pct=0.0)
        apply_filters([d], {"min_discount": 0})
        assert d.discount_pct == 50.0


class TestNormalizeCategory:
    def test_known_category(self):
        assert normalize_category("portátiles") == "laptops"
        assert normalize_category("Smartphones") == "phones"
        assert normalize_category("TV") == "tvs"

    def test_unknown_category(self):
        assert normalize_category("juguetes") == "juguetes"


class TestInferCategoryFromUrl:
    def test_direct_segment(self):
        assert infer_category_from_url("https://www.coolmod.com/tarjetas-graficas/") == "gpus"
        assert infer_category_from_url("https://pccomponentes.com/portatiles") == "laptops"
        assert infer_category_from_url("https://samsung.com/es/tvs/all-tvs/") == "tvs"

    def test_sub_segment_fallback(self):
        assert infer_category_from_url("https://coolmod.com/componentes-pc-memorias-ram/") == "ram"

    def test_various_categories(self):
        assert infer_category_from_url("https://x.com/smartphones/deals") == "phones"
        assert infer_category_from_url("https://x.com/tablets/") == "tablets"
        assert infer_category_from_url("https://x.com/auriculares/") == "headphones"
        assert infer_category_from_url("https://x.com/consola/") == "consoles"

    def test_no_match(self):
        assert infer_category_from_url("https://example.com/ofertas") == ""

    def test_empty_url(self):
        assert infer_category_from_url("") == ""


class TestInferCategoryFromTitle:
    def test_laptop(self):
        assert infer_category_from_title("MacBook Air M2 13 pulgadas") == "laptops"
        assert infer_category_from_title("Portátil HP Pavilion 15") == "laptops"

    def test_phone(self):
        assert infer_category_from_title("iPhone 15 Pro Max 256GB") == "phones"
        assert infer_category_from_title("Samsung Galaxy S25 Ultra") == "phones"

    def test_gpu(self):
        assert infer_category_from_title("NVIDIA GeForce RTX 4090") == "gpus"
        assert infer_category_from_title("Radeon RX 7900 XTX") == "gpus"

    def test_tv(self):
        assert infer_category_from_title("Samsung OLED 55 pulgadas") == "tvs"
        assert infer_category_from_title("LG Smart TV 4K") == "tvs"

    def test_console(self):
        assert infer_category_from_title("PlayStation 5 Slim Digital") == "consoles"
        assert infer_category_from_title("Nintendo Switch Lite") == "consoles"
        assert infer_category_from_title("Xbox Series X 1TB") == "consoles"

    def test_no_match(self):
        assert infer_category_from_title("Pack de 3 camisetas básicas") == ""


class TestCheckWatchlist:
    def test_disabled_returns_empty(self):
        deals = [_make_deal(title="iPhone 15")]
        result = check_watchlist(deals, {"enabled": False})
        assert result == []

    def test_substring_match(self):
        """Match con store_discount >= 30% y >= min_discount."""
        deals = [_make_deal(title="Apple iPhone 15 128GB Azul Libre",
                            current_price=300, original_price=600)]
        cfg = {
            "enabled": True,
            "products": [{"name": "iPhone 15", "max_price": 600}],
        }
        result = check_watchlist(deals, cfg, min_discount=45.0)
        assert len(result) == 1
        assert result[0].alert_tier == "CHOLLO"

    def test_below_min_discount_excluded(self):
        """Descuento < 45% vs max_price no matchea."""
        deals = [_make_deal(title="Apple iPhone 15 128GB Azul Libre", current_price=550)]
        cfg = {
            "enabled": True,
            "products": [{"name": "iPhone 15", "max_price": 600}],
        }
        result = check_watchlist(deals, cfg, min_discount=45.0)
        assert result == []

    def test_price_above_max_excluded(self):
        deals = [_make_deal(title="iPhone 15 Pro Max", current_price=1200)]
        cfg = {
            "enabled": True,
            "products": [{"name": "iPhone 15", "max_price": 600}],
        }
        result = check_watchlist(deals, cfg, min_discount=45.0)
        assert result == []

    def test_error_de_precio_tier(self):
        """Si el precio es < 25% del max_price, se marca como ERROR_DE_PRECIO."""
        deals = [_make_deal(title="AirPods Pro", current_price=40)]
        cfg = {
            "enabled": True,
            "products": [{"name": "AirPods Pro", "max_price": 180}],
        }
        result = check_watchlist(deals, cfg, min_discount=45.0)
        assert len(result) == 1
        assert result[0].alert_tier == "ERROR_DE_PRECIO"

    def test_no_duplicates(self):
        """El mismo URL no se matchea dos veces."""
        deals = [
            _make_deal(title="iPhone 15 Pro", url="https://x.com/p1",
                       current_price=200, original_price=600),
            _make_deal(title="iPhone 15 Pro", url="https://x.com/p1",
                       current_price=200, original_price=600),
        ]
        cfg = {
            "enabled": True,
            "products": [{"name": "iPhone 15", "max_price": 600}],
        }
        result = check_watchlist(deals, cfg, min_discount=45.0)
        assert len(result) == 1

    def test_empty_products(self):
        deals = [_make_deal(title="iPhone 15", current_price=500)]
        result = check_watchlist(deals, {"enabled": True, "products": []})
        assert result == []

    def test_compatibility_mention_rejected(self):
        """'PS5' en 'Auriculares Gaming 7.1 PC/PS5' no matchea (compatibilidad)."""
        deals = [_make_deal(title="Auriculares Gaming RGB 7.1 PC/PS5", current_price=30)]
        cfg = {
            "enabled": True,
            "products": [{"name": "PS5", "max_price": 350}],
        }
        result = check_watchlist(deals, cfg)
        assert result == []

    def test_position_suffix_rejected(self):
        """Watchlist term deep in title (compatibility suffix) rejected."""
        deals = [_make_deal(
            title="Logitech G29 Driving Force para PS5/PS4/PS3/PC Compatible",
            current_price=259,
        )]
        cfg = {
            "enabled": True,
            "products": [{"name": "PS5", "max_price": 350}],
        }
        result = check_watchlist(deals, cfg)
        assert result == []

    def test_primary_product_matched(self):
        """'PS5' al inicio del título sí matchea (producto principal)."""
        deals = [_make_deal(title="PS5 Slim 1TB Digital Edition",
                            current_price=150, original_price=400)]
        cfg = {
            "enabled": True,
            "products": [{"name": "PS5", "max_price": 350}],
        }
        result = check_watchlist(deals, cfg, min_discount=45.0)
        assert len(result) == 1

    def test_accessory_prefix_rejected(self):
        """Títulos que empiezan con prefijo de accesorio no matchean."""
        deals = [
            _make_deal(title="Funda iPad Air 11 (2024) Silicona Negro", current_price=33),
            _make_deal(title="Adaptador Audio PS5 BIGBEN", current_price=18,
                       url="https://x.com/adapt"),
            _make_deal(title="Cargador iPhone 15 USB-C 20W", current_price=15,
                       url="https://x.com/charger"),
        ]
        cfg = {
            "enabled": True,
            "products": [
                {"name": "iPad Air", "max_price": 500},
                {"name": "PS5", "max_price": 350},
                {"name": "iPhone 15", "max_price": 600},
            ],
        }
        result = check_watchlist(deals, cfg)
        assert result == []


# ===========================================================================
# 3. DATABASE
# ===========================================================================

class TestDatabase:
    def test_upsert_new_deal(self, tmp_db):
        d = _make_deal(url="https://x.com/p1")
        deal_id, is_new = tmp_db.upsert_deal(d)
        assert is_new is True
        assert deal_id > 0

    def test_upsert_existing_deal(self, tmp_db):
        d = _make_deal(url="https://x.com/p1", current_price=100)
        tmp_db.upsert_deal(d)
        d2 = _make_deal(url="https://x.com/p1", current_price=90)
        deal_id, is_new = tmp_db.upsert_deal(d2)
        assert is_new is False

    def test_price_history_recorded(self, tmp_db):
        d = _make_deal(url="https://x.com/p1", current_price=100)
        deal_id, _ = tmp_db.upsert_deal(d)
        d2 = _make_deal(url="https://x.com/p1", current_price=90)
        tmp_db.upsert_deal(d2)

        stats = tmp_db.get_price_stats(deal_id)
        assert stats is not None
        assert stats["observations"] == 2
        assert stats["min"] == 90
        assert stats["max"] == 100

    def test_get_price_stats_by_url(self, tmp_db):
        d = _make_deal(url="https://x.com/p1", current_price=100)
        tmp_db.upsert_deal(d)
        stats = tmp_db.get_price_stats_by_url("https://x.com/p1")
        assert stats is not None
        assert stats["observations"] == 1

    def test_get_price_stats_nonexistent(self, tmp_db):
        stats = tmp_db.get_price_stats_by_url("https://nonexistent.com")
        assert stats is None

    def test_mark_sent(self, tmp_db):
        d = _make_deal(url="https://x.com/p1")
        deal_id, _ = tmp_db.upsert_deal(d)
        tmp_db.mark_sent([deal_id])

        unsent = tmp_db.get_unsent_deals()
        assert len(unsent) == 0

    def test_get_unsent_deals(self, tmp_db):
        d1 = _make_deal(url="https://x.com/p1", discount_pct=30)
        d2 = _make_deal(url="https://x.com/p2", discount_pct=20)
        tmp_db.upsert_deal(d1)
        tmp_db.upsert_deal(d2)

        unsent = tmp_db.get_unsent_deals()
        assert len(unsent) == 2
        # Ordenados por discount_pct DESC
        assert unsent[0].discount_pct >= unsent[1].discount_pct

    def test_get_unsent_deals_freshness(self, tmp_db):
        """Deals antiguos no aparecen en unsent."""
        d = _make_deal(url="https://x.com/old", discount_pct=50)
        deal_id, _ = tmp_db.upsert_deal(d)
        # Forzar updated_at a hace 30 días
        old_date = (datetime.utcnow() - timedelta(days=30)).isoformat()
        tmp_db.conn.execute("UPDATE deals SET updated_at = ? WHERE id = ?", (old_date, deal_id))
        tmp_db.conn.commit()

        unsent = tmp_db.get_unsent_deals(max_age_days=7)
        assert len(unsent) == 0

    def test_update_verified_deal(self, tmp_db):
        d = _make_deal(url="https://x.com/p1", discount_pct=10, current_price=100)
        deal_id, _ = tmp_db.upsert_deal(d)

        # Marcar como enviado
        tmp_db.mark_sent([deal_id])

        # update_verified_deal NO debe resetear sent_to_telegram
        d.discount_pct = 35.0
        d.original_price = 150.0
        tmp_db.update_verified_deal(d)

        # Verificar que sigue marcado como enviado
        row = tmp_db.conn.execute("SELECT sent_to_telegram FROM deals WHERE id = ?", (deal_id,)).fetchone()
        assert row["sent_to_telegram"] == 1

    def test_update_verified_deal_unsent(self, tmp_db):
        """update_verified_deal SÍ actualiza deals no enviados."""
        d = _make_deal(url="https://x.com/p1", discount_pct=10, current_price=100)
        deal_id, _ = tmp_db.upsert_deal(d)

        d.discount_pct = 35.0
        d.original_price = 150.0
        tmp_db.update_verified_deal(d)

        row = tmp_db.conn.execute(
            "SELECT discount_pct, original_price FROM deals WHERE id = ?", (deal_id,)
        ).fetchone()
        assert row["discount_pct"] == 35.0
        assert row["original_price"] == 150.0

    def test_get_top_deals(self, tmp_db):
        for i in range(15):
            tmp_db.upsert_deal(_make_deal(
                url=f"https://x.com/p{i}",
                discount_pct=float(i * 5),
            ))
        top = tmp_db.get_top_deals(limit=10)
        assert len(top) == 10
        assert top[0].discount_pct >= top[-1].discount_pct

    def test_get_stats(self, tmp_db):
        tmp_db.upsert_deal(_make_deal(url="https://x.com/p1", store="store_a"))
        tmp_db.upsert_deal(_make_deal(url="https://x.com/p2", store="store_b"))

        stats = tmp_db.get_stats()
        assert stats["total_deals"] == 2
        assert stats["stores_tracked"] == 2
        assert stats["sent_to_telegram"] == 0
        assert stats["price_observations"] == 2

    def test_cleanup_old_data(self, tmp_db):
        d = _make_deal(url="https://x.com/old")
        deal_id, _ = tmp_db.upsert_deal(d)

        # Forzar fecha antigua
        old_date = (datetime.utcnow() - timedelta(days=100)).isoformat()
        tmp_db.conn.execute("UPDATE deals SET updated_at = ? WHERE id = ?", (old_date, deal_id))
        tmp_db.conn.commit()

        # También insertar uno reciente
        tmp_db.upsert_deal(_make_deal(url="https://x.com/new"))

        tmp_db.cleanup_old_data(days=90)

        cur = tmp_db.conn.execute("SELECT COUNT(*) as c FROM deals")
        assert cur.fetchone()["c"] == 1  # Solo queda el reciente

    def test_store_price_percentiles(self, tmp_db):
        for i in range(50):
            tmp_db.upsert_deal(_make_deal(
                url=f"https://x.com/p{i}",
                store="amazon",
                current_price=float(10 + i * 10),
                category="phones",
            ))
        result = tmp_db.get_store_price_percentiles("amazon", "phones")
        assert result is not None
        assert result["count"] == 50
        assert result["min"] == 10.0
        assert result["p5"] > 0

    def test_store_price_percentiles_empty(self, tmp_db):
        result = tmp_db.get_store_price_percentiles("nonexistent")
        assert result is None


class TestDatabaseIndices:
    """Verifica que los índices se crean correctamente."""

    def test_indices_exist(self, tmp_db):
        cur = tmp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
        indices = {row["name"] for row in cur.fetchall()}
        assert "idx_deals_store_cat" in indices
        assert "idx_deals_updated" in indices
        assert "idx_deals_sent" in indices
        assert "idx_ph_deal" in indices


class TestExtractVariantTags:
    def test_pro_variant(self):
        tags = Database._extract_variant_tags("iPhone 15 Pro 256GB")
        assert "pro" in tags
        assert "256gb" in tags

    def test_ultra_variant(self):
        tags = Database._extract_variant_tags("Samsung Galaxy S25 Ultra 512GB")
        assert "ultra" in tags
        assert "512gb" in tags

    def test_no_variants(self):
        tags = Database._extract_variant_tags("Samsung Galaxy S25 128GB")
        assert "pro" not in tags
        assert "ultra" not in tags
        assert "128gb" in tags

    def test_mini_and_se(self):
        tags = Database._extract_variant_tags("iPhone SE Mini 64GB")
        assert "se" in tags
        assert "mini" in tags
        assert "64gb" in tags

    def test_console_variants(self):
        tags = Database._extract_variant_tags("PS5 Digital Edition Slim")
        assert "digital" in tags
        assert "slim" in tags


class TestCrossStoreDeals:
    """Tests para find_cross_store_deals."""

    def test_exact_title_match(self, tmp_db):
        """Mismo título exacto en dos tiendas con >45% diferencia."""
        d1 = _make_deal(
            title="iPhone 15 Pro 256GB",
            url="https://amazon.com/iphone15",
            store="amazon",
            current_price=999.0,
        )
        d2 = _make_deal(
            title="iPhone 15 Pro 256GB",
            url="https://pccomp.com/iphone15",
            store="pccomponentes",
            current_price=500.0,
        )
        tmp_db.upsert_deal(d1)
        tmp_db.upsert_deal(d2)

        pairs = tmp_db.find_cross_store_deals(hours=1, min_discount_pct=45.0)
        assert len(pairs) == 1
        cheap, expensive = pairs[0]
        assert cheap.current_price < expensive.current_price

    def test_same_store_not_paired(self, tmp_db):
        """Dos deals de la misma tienda no se emparejan."""
        d1 = _make_deal(title="MacBook Pro", url="https://amazon.com/mb1",
                        store="amazon", current_price=1000)
        d2 = _make_deal(title="MacBook Pro", url="https://amazon.com/mb2",
                        store="amazon", current_price=800)
        tmp_db.upsert_deal(d1)
        tmp_db.upsert_deal(d2)

        pairs = tmp_db.find_cross_store_deals(hours=1)
        assert len(pairs) == 0

    def test_variant_mismatch_excluded(self, tmp_db):
        """Pro vs no-Pro no se emparejan (variantes distintas)."""
        d1 = _make_deal(
            title="iPhone 15 Pro 256GB",
            url="https://store1.com/pro",
            store="store1",
            current_price=999.0,
            category="phones",
        )
        d2 = _make_deal(
            title="iPhone 15 256GB",
            url="https://store2.com/normal",
            store="store2",
            current_price=650.0,
            category="phones",
        )
        tmp_db.upsert_deal(d1)
        tmp_db.upsert_deal(d2)

        pairs = tmp_db.find_cross_store_deals(hours=1)
        # Deben excluirse porque las variantes son distintas (pro vs no-pro)
        assert len(pairs) == 0

    def test_small_diff_excluded(self, tmp_db):
        """Diferencia <45% no genera par."""
        d1 = _make_deal(title="Test Product XYZ 128GB",
                        url="https://s1.com/p",
                        store="store1", current_price=100)
        d2 = _make_deal(title="Test Product XYZ 128GB",
                        url="https://s2.com/p",
                        store="store2", current_price=140)
        tmp_db.upsert_deal(d1)
        tmp_db.upsert_deal(d2)

        pairs = tmp_db.find_cross_store_deals(hours=1, min_discount_pct=45.0)
        assert len(pairs) == 0

    def test_refurbished_vs_new_excluded(self, tmp_db):
        """No comparar reacondicionados (backmarket) con tiendas de nuevo."""
        d1 = _make_deal(title="Galaxy S24 Ultra 256GB",
                        url="https://backmarket.es/galaxy",
                        store="backmarket", current_price=500)
        d2 = _make_deal(title="Galaxy S24 Ultra 256GB",
                        url="https://samsung.com/galaxy",
                        store="samsung", current_price=1200)
        tmp_db.upsert_deal(d1)
        tmp_db.upsert_deal(d2)

        pairs = tmp_db.find_cross_store_deals(hours=1, min_discount_pct=30.0)
        assert len(pairs) == 0

    def test_short_title_excluded(self, tmp_db):
        """Títulos cortos (< 10 chars) se ignoran como slugs de búsqueda."""
        d1 = _make_deal(title="samsung", url="https://miravia.es/s",
                        store="miravia", current_price=10)
        d2 = _make_deal(title="samsung", url="https://ppo.com/s",
                        store="powerplanetonline", current_price=500)
        tmp_db.upsert_deal(d1)
        tmp_db.upsert_deal(d2)

        pairs = tmp_db.find_cross_store_deals(hours=1, min_discount_pct=30.0)
        assert len(pairs) == 0


# ===========================================================================
# 4. PRICE PARSING (generic.py)
# ===========================================================================

class TestParsePrice:
    """Tests para _parse_price — cubre todos los formatos conocidos."""

    # Formato europeo con coma decimal
    def test_european_comma_decimal(self):
        assert _parse_price("299,99€") == 299.99

    def test_european_with_thousands(self):
        assert _parse_price("1.299,99 €") == 1299.99

    # Formato inglés con punto decimal
    def test_english_decimal(self):
        assert _parse_price("$299.99") == 299.99

    def test_english_with_thousands(self):
        assert _parse_price("1,299.99") == 1299.99

    # Formato europeo solo punto (separador de miles)
    def test_european_dot_thousands_3digits(self):
        """4.999€ → 4999.0 (3 dígitos después del punto = miles)."""
        assert _parse_price("4.999€") == 4999.0

    def test_european_dot_thousands_larger(self):
        assert _parse_price("12.999€") == 12999.0

    def test_dot_as_decimal_1digit(self):
        """3.5 → 3.5 (1 dígito después del punto = decimal)."""
        assert _parse_price("3.5") == 3.5

    def test_dot_as_decimal_2digits(self):
        """29.99 → 29.99 (2 dígitos = decimal)."""
        assert _parse_price("29.99") == 29.99

    # Otros formatos
    def test_currency_symbol_after(self):
        assert _parse_price("299,99 EUR") == 299.99

    def test_currency_symbol_before(self):
        assert _parse_price("€ 299,99") == 299.99

    def test_plain_integer(self):
        assert _parse_price("300") == 300.0

    def test_none_input(self):
        assert _parse_price(None) is None

    def test_empty_string(self):
        assert _parse_price("") is None

    def test_garbage_text(self):
        assert _parse_price("no price here") is None

    def test_zero_price(self):
        """Precio 0 se rechaza."""
        assert _parse_price("0€") is None

    def test_huge_price(self):
        """Precios >500000 se rechazan."""
        assert _parse_price("999999€") is None

    def test_non_breaking_spaces(self):
        assert _parse_price("1\xa0299,99\xa0€") == 1299.99

    def test_comma_thousands_only(self):
        """1,299 (sin decimales, exactamente 3 dígitos después de coma) = miles."""
        assert _parse_price("1,299") == 1299.0

    def test_comma_decimal_short(self):
        """9,99 (2 dígitos después de coma) = decimal."""
        assert _parse_price("9,99") == 9.99


class TestExtractPriceText:
    """Tests para _extract_price_text (manejo de <sup> para decimales)."""

    def test_sup_decimal(self):
        from bs4 import BeautifulSoup
        html = '<span class="price">649€<sup>95</sup></span>'
        soup = BeautifulSoup(html, "lxml")
        el = soup.select_one(".price")
        result = _extract_price_text(el)
        assert result == "649.95"

    def test_no_sup(self):
        from bs4 import BeautifulSoup
        html = '<span class="price">649,95€</span>'
        soup = BeautifulSoup(html, "lxml")
        el = soup.select_one(".price")
        result = _extract_price_text(el)
        assert result == "649,95€"


# ===========================================================================
# 5. GENERIC STORE STRATEGIES
# ===========================================================================

class TestGenericStoreJsonLD:
    """Tests para la estrategia JSON-LD."""

    def _make_store(self):
        sc = StoreConfig(
            name="teststore", enabled=True, interval_minutes=60,
            scrape_urls=["https://example.com"], client_type="http",
        )
        return GenericStore(config=sc)

    def test_single_product(self):
        html = """
        <html><body>
        <script type="application/ld+json">
        {
            "@type": "Product",
            "name": "Samsung Galaxy S25",
            "url": "https://example.com/galaxy-s25",
            "offers": {
                "@type": "Offer",
                "price": "899.99",
                "priceCurrency": "EUR"
            }
        }
        </script>
        </body></html>
        """
        store = self._make_store()
        deals = store.parse_deals(html, "https://example.com/listing")
        assert len(deals) == 1
        assert deals[0].title == "Samsung Galaxy S25"
        assert deals[0].current_price == 899.99

    def test_item_list(self):
        html = """
        <html><body>
        <script type="application/ld+json">
        {
            "@type": "ItemList",
            "itemListElement": [
                {"@type": "ListItem", "item": {"@type": "Product", "name": "Prod A",
                    "url": "https://example.com/a",
                    "offers": {"price": "100"}}},
                {"@type": "ListItem", "item": {"@type": "Product", "name": "Prod B",
                    "url": "https://example.com/b",
                    "offers": {"price": "200"}}}
            ]
        }
        </script>
        </body></html>
        """
        store = self._make_store()
        deals = store.parse_deals(html, "https://example.com/listing")
        assert len(deals) == 2

    def test_filters_self_url(self):
        """Deals cuya URL es la misma que la página de listing se descartan."""
        html = """
        <html><body>
        <script type="application/ld+json">
        {"@type": "Product", "name": "Test", "offers": {"price": "99.99"}}
        </script>
        </body></html>
        """
        store = self._make_store()
        # El product no tiene URL, así que defaults to page_url
        deals = store.parse_deals(html, "https://example.com")
        # Should be filtered because deal URL == listing URL
        assert len(deals) == 0

    def test_graph_with_products(self):
        html = """
        <html><body>
        <script type="application/ld+json">
        {
            "@graph": [
                {"@type": "Product", "name": "In Graph",
                 "url": "https://example.com/prod",
                 "offers": {"price": "50"}}
            ]
        }
        </script>
        </body></html>
        """
        store = self._make_store()
        deals = store.parse_deals(html, "https://example.com/listing")
        assert len(deals) == 1
        assert deals[0].title == "In Graph"


class TestGenericStoreMicrodata:
    def _make_store(self):
        sc = StoreConfig(
            name="teststore", enabled=True, interval_minutes=60,
            scrape_urls=["https://example.com"], client_type="http",
        )
        return GenericStore(config=sc)

    def test_basic_microdata(self):
        html = """
        <html><body>
        <div itemscope itemtype="https://schema.org/Product">
            <span itemprop="name">Test Product</span>
            <a itemprop="url" href="https://example.com/product">link</a>
            <div itemscope itemtype="https://schema.org/Offer">
                <meta itemprop="price" content="199.99">
                <meta itemprop="priceCurrency" content="EUR">
            </div>
        </div>
        </body></html>
        """
        store = self._make_store()
        deals = store.parse_deals(html, "https://example.com/listing")
        assert len(deals) == 1
        assert deals[0].current_price == 199.99


class TestGenericStoreCSS:
    def _make_store(self):
        sc = StoreConfig(
            name="teststore", enabled=True, interval_minutes=60,
            scrape_urls=["https://example.com"], client_type="http",
        )
        return GenericStore(config=sc)

    def test_product_cards(self):
        cards = ""
        for i in range(5):
            cards += f"""
            <div class="product-card">
                <h3><a href="https://example.com/p{i}">Product {i}</a></h3>
                <span class="price">{100 + i * 10},99€</span>
            </div>
            """
        html = f"<html><body>{cards}</body></html>"
        store = self._make_store()
        deals = store.parse_deals(html, "https://example.com/listing")
        assert len(deals) == 5

    def test_data_attribute_cards(self):
        cards = ""
        for i in range(3):
            cards += f"""
            <div data-product-id="{i}">
                <h3><a href="https://example.com/dp{i}">DataProd {i}</a></h3>
                <span class="price" data-price="{50 + i * 25}">{50 + i * 25},00€</span>
            </div>
            """
        html = f"<html><body>{cards}</body></html>"
        store = self._make_store()
        deals = store.parse_deals(html, "https://example.com/listing")
        assert len(deals) == 3


class TestGenericStoreOpenGraph:
    def _make_store(self):
        sc = StoreConfig(
            name="teststore", enabled=True, interval_minutes=60,
            scrape_urls=["https://example.com"], client_type="http",
        )
        return GenericStore(config=sc)

    def test_og_product(self):
        html = """
        <html><head>
        <meta property="og:title" content="OG Product">
        <meta property="og:url" content="https://example.com/og-product">
        <meta property="product:price:amount" content="49.99">
        <meta property="product:price:currency" content="EUR">
        </head><body></body></html>
        """
        store = self._make_store()
        deals = store.parse_deals(html, "https://example.com/listing")
        assert len(deals) == 1
        assert deals[0].title == "OG Product"
        assert deals[0].current_price == 49.99


class TestGenericStoreDataAttributes:
    def _make_store(self):
        sc = StoreConfig(
            name="teststore", enabled=True, interval_minutes=60,
            scrape_urls=["https://example.com"], client_type="http",
        )
        return GenericStore(config=sc)

    def test_data_itemprice(self):
        items = ""
        for i in range(3):
            items += f"""
            <div data-itemprice="{20 + i * 10}" data-itemname="ItemPrice Prod {i}">
                <a href="https://example.com/ip{i}">link</a>
            </div>
            """
        html = f"<html><body>{items}</body></html>"
        store = self._make_store()
        deals = store.parse_deals(html, "https://example.com/listing")
        assert len(deals) == 3
        assert deals[0].title == "ItemPrice Prod 0"


# ===========================================================================
# 6. TELEGRAM BOT (formateo)
# ===========================================================================

class TestEscapeHtml:
    def test_basic_escaping(self):
        assert _escape_html("Tom & Jerry") == "Tom &amp; Jerry"
        assert _escape_html("<script>") == "&lt;script&gt;"

    def test_no_escaping_needed(self):
        assert _escape_html("Hello World") == "Hello World"


class TestSafeTitle:
    def test_short_title(self):
        assert _safe_title("Short title") == "Short title"

    def test_long_title_truncated(self):
        title = "A" * 250
        result = _safe_title(title)
        assert len(result) == 203  # 200 + "..."
        assert result.endswith("...")

    def test_exact_200_chars(self):
        title = "B" * 200
        result = _safe_title(title)
        assert result == title  # No truncation, exactly 200

    def test_201_chars_truncated(self):
        title = "C" * 201
        result = _safe_title(title)
        assert len(result) == 203
        assert result == "C" * 200 + "..."

    def test_escaping_applied(self):
        title = "Deal <50% off> & more"
        result = _safe_title(title)
        assert "&lt;" in result
        assert "&amp;" in result


class TestTelegramFormatting:
    """Tests para los métodos de formato de TelegramBot."""

    def _make_bot(self):
        db = MagicMock()
        return TelegramBot(bot_token="fake:token", chat_id="12345", db=db)

    def test_format_normal(self):
        d = _make_deal(
            title="Samsung TV 55",
            current_price=499.99,
            original_price=699.99,
            discount_pct=28.6,
            store="amazon",
        )
        result = TelegramBot._format_normal(d)
        assert "Samsung TV 55" in result
        assert "499.99€" in result
        assert "699.99€" in result
        assert "amazon" in result.lower() or "Amazon" in result

    def test_format_chollo(self):
        d = _make_deal(
            title="AirPods Pro",
            current_price=179.99,
            original_price=279.99,
            discount_pct=35.7,
            store="pccomponentes",
            alert_tier="CHOLLO",
        )
        result = TelegramBot._format_chollo(d)
        assert "CHOLLO" in result
        assert "179.99€" in result

    def test_format_price_error(self):
        d = _make_deal(
            title="MacBook Air M2",
            current_price=399.99,
            original_price=1099.99,
            discount_pct=63.6,
            store="mediamarkt",
            alert_tier="ERROR_DE_PRECIO",
        )
        result = TelegramBot._format_price_error(d)
        assert "ERROR DE PRECIO" in result
        assert "399.99€" in result
        assert "1099.99€" in result
        assert "COMPRAR AHORA" in result

    def test_format_cross_store(self):
        cheap = _make_deal(
            title="PS5 Slim",
            current_price=399.99,
            store="pccomponentes",
            url="https://pccomp.com/ps5",
        )
        expensive = _make_deal(
            title="PS5 Slim",
            current_price=499.99,
            store="amazon",
            url="https://amazon.es/ps5",
        )
        result = TelegramBot._format_cross_store(cheap, expensive, diff_pct=20)
        assert "MISMO PRODUCTO" in result
        assert "399.99€" in result
        assert "499.99€" in result

    def test_format_deal_routes_correctly(self):
        """_format_deal enruta al método correcto según alert_tier."""
        d1 = _make_deal(current_price=100, store="s", alert_tier="ERROR_DE_PRECIO",
                        original_price=300)
        d2 = _make_deal(current_price=100, store="s", alert_tier="CHOLLO",
                        original_price=150, discount_pct=33)
        d3 = _make_deal(current_price=100, store="s", alert_tier="NORMAL",
                        discount_pct=15)

        r1 = TelegramBot._format_deal(d1)
        r2 = TelegramBot._format_deal(d2)
        r3 = TelegramBot._format_deal(d3)

        assert "ERROR DE PRECIO" in r1
        assert "CHOLLO" in r2
        assert "CHOLLO" not in r3 and "ERROR" not in r3


# ===========================================================================
# 7. CONFIG
# ===========================================================================

class TestConfig:
    def test_load_config(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
telegram:
  bot_token: "123:ABC"
  chat_id: "456"
stores:
  - name: teststore
    scrape_urls: ["https://example.com"]
filters:
  min_discount: 20
""")
        cfg = load_config(config_file)
        assert cfg["telegram"]["bot_token"] == "123:ABC"
        assert cfg["stores"][0]["name"] == "teststore"

    def test_missing_config(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.yaml")

    def test_invalid_token(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
telegram:
  bot_token: "TU_BOT_TOKEN_AQUI"
  chat_id: "456"
stores:
  - name: test
    scrape_urls: ["https://x.com"]
""")
        with pytest.raises(ValueError, match="bot_token"):
            load_config(config_file)

    def test_get_store_configs(self):
        cfg = {
            "stores": [
                {"name": "a", "enabled": True, "scrape_urls": ["https://a.com"]},
                {"name": "b", "enabled": False, "scrape_urls": ["https://b.com"]},
                {"name": "c", "scrape_urls": ["https://c.com"]},
            ]
        }
        configs = get_store_configs(cfg)
        names = [sc.name for sc in configs]
        assert "a" in names
        assert "c" in names
        assert "b" not in names

    def test_get_filters_defaults(self):
        f = get_filters({})
        assert f["min_discount"] == 15
        assert f["price_min"] == 0.0
        assert f["keywords"] == []

    def test_get_filters_override(self):
        f = get_filters({"filters": {"min_discount": 30, "keywords": ["iphone"]}})
        assert f["min_discount"] == 30
        assert f["keywords"] == ["iphone"]

    def test_get_anti_ban_defaults(self):
        ab = get_anti_ban({})
        assert ab["delay_min"] == 0.5
        assert ab["proxy_url"] is None

    def test_get_speed_defaults(self):
        s = get_speed({})
        assert s["mode"] == "fast"
        assert s["max_concurrent_stores"] == 4


# ===========================================================================
# 8. VERIFY REAL DEALS (integración filters + database)
# ===========================================================================

class TestVerifyRealDeals:
    def test_new_product_skipped(self, tmp_db):
        """Productos nuevos sin historial no se verifican."""
        d = _make_deal(url="https://x.com/new", current_price=100, discount_pct=20)
        tmp_db.upsert_deal(d)
        verified = verify_real_deals([d], db=tmp_db, min_observations=2)
        assert len(verified) == 0

    def test_real_discount_passes(self, tmp_db):
        """Producto con historial y bajada real pasa la verificación."""
        # Registrar historial de precios altos primero
        d_initial = _make_deal(url="https://x.com/p1", current_price=200)
        tmp_db.upsert_deal(d_initial)
        deal_id = tmp_db.conn.execute("SELECT id FROM deals WHERE url=?", (d_initial.url,)).fetchone()["id"]
        for price in [200, 190, 195]:
            tmp_db.conn.execute(
                "INSERT INTO price_history (deal_id, price, detected_at) VALUES (?, ?, ?)",
                (deal_id, price, datetime.utcnow().isoformat()),
            )
        tmp_db.conn.commit()

        # Ahora el precio baja a 100 — upsert para que quede en historial
        d_cheap = _make_deal(url="https://x.com/p1", current_price=100)
        tmp_db.upsert_deal(d_cheap)

        d_check = _make_deal(url="https://x.com/p1", current_price=100, discount_pct=20)
        verified = verify_real_deals([d_check], db=tmp_db, min_observations=2)
        assert len(verified) == 1
        assert verified[0].alert_tier in ("CHOLLO", "ERROR_DE_PRECIO")

    def test_fake_discount_rejected(self, tmp_db):
        """Producto con historial pero sin bajada real se rechaza."""
        d = _make_deal(url="https://x.com/p2", current_price=100)
        tmp_db.upsert_deal(d)
        deal_id = tmp_db.conn.execute("SELECT id FROM deals WHERE url=?", (d.url,)).fetchone()["id"]
        for price in [100, 102, 99, 101]:
            tmp_db.conn.execute(
                "INSERT INTO price_history (deal_id, price, detected_at) VALUES (?, ?, ?)",
                (deal_id, price, datetime.utcnow().isoformat()),
            )
        tmp_db.conn.commit()

        d_check = _make_deal(url="https://x.com/p2", current_price=100, discount_pct=50)
        verified = verify_real_deals([d_check], db=tmp_db, min_observations=2)
        assert len(verified) == 0

    def test_price_error_bypass(self, tmp_db):
        """Posible error de precio (>50% descuento) bypasses historial check."""
        d = _make_deal(
            url="https://x.com/bypass",
            current_price=100,
            original_price=300,
            discount_pct=0.0,
            store="pccomponentes",
        )
        # No insertar en DB para que sea "nuevo"
        verified = verify_real_deals(
            [d], db=tmp_db, min_observations=2, price_error_threshold=50.0,
        )
        assert len(verified) == 1
        assert verified[0].alert_tier == "ERROR_DE_PRECIO"

    def test_refurbished_store_no_bypass(self, tmp_db):
        """Tiendas de reacondicionados no hacen bypass de error de precio."""
        d = _make_deal(
            url="https://backmarket.com/iphone",
            current_price=400,
            original_price=1200,
            discount_pct=0.0,
            store="backmarket",
        )
        verified = verify_real_deals([d], db=tmp_db, min_observations=2)
        assert len(verified) == 0


# ===========================================================================
# 9. DETECT ABSURDLY CHEAP
# ===========================================================================

class TestDetectAbsurdlyCheap:
    def test_absurdly_cheap_detected(self, tmp_db):
        """Producto nuevo muy por debajo del P5 de la tienda."""
        # Poblar la tienda con 50 productos normales (100-600€)
        for i in range(50):
            tmp_db.upsert_deal(_make_deal(
                url=f"https://x.com/normal{i}",
                store="amazon",
                current_price=float(100 + i * 10),
                category="phones",
            ))

        # Producto sospechosamente barato
        cheap = _make_deal(
            url="https://x.com/suspicious",
            store="amazon",
            current_price=1.0,  # 1€ para un phone
            category="phones",
        )

        detected = detect_absurdly_cheap([cheap], db=tmp_db, min_observations=2)
        assert len(detected) == 1
        assert detected[0].alert_tier == "ERROR_DE_PRECIO"

    def test_normal_price_not_flagged(self, tmp_db):
        """Producto con precio normal no se detecta."""
        for i in range(50):
            tmp_db.upsert_deal(_make_deal(
                url=f"https://x.com/normal{i}",
                store="amazon",
                current_price=float(100 + i * 10),
            ))

        normal = _make_deal(
            url="https://x.com/normal_new",
            store="amazon",
            current_price=150.0,
        )
        detected = detect_absurdly_cheap([normal], db=tmp_db, min_observations=2)
        assert len(detected) == 0

    def test_with_original_price_excluded(self, tmp_db):
        """Productos con original_price no son candidatos."""
        d = _make_deal(
            url="https://x.com/has_original",
            store="amazon",
            current_price=5.0,
            original_price=100.0,
        )
        detected = detect_absurdly_cheap([d], db=tmp_db, min_observations=2)
        assert len(detected) == 0


# ===========================================================================
# 10. DETECT CROSS-STORE BARGAINS (filtro wrapper)
# ===========================================================================

class TestDetectCrossStoreBargains:
    def test_classifies_pairs(self, tmp_db):
        """Wrapper clasifica pares según diferencia (>45%)."""
        d1 = _make_deal(title="Same Product X 256GB",
                        url="https://a.com/p",
                        store="store1", current_price=100)
        d2 = _make_deal(title="Same Product X 256GB",
                        url="https://b.com/p",
                        store="store2", current_price=200)
        tmp_db.upsert_deal(d1)
        tmp_db.upsert_deal(d2)

        pairs = detect_cross_store_bargains(db=tmp_db, hours=1, min_discount_pct=45.0)
        assert len(pairs) == 1
        cheap, expensive = pairs[0]
        assert cheap.alert_tier in ("CHOLLO", "ERROR_DE_PRECIO")
        assert cheap.current_price < expensive.current_price


# ===========================================================================
# 11. BASE STORE — category inference integration
# ===========================================================================

class TestBaseStoreCategoryInference:
    """Tests para la inferencia de categorías en scrape()."""

    @pytest.mark.asyncio
    async def test_url_category_applied(self):
        """URL category se aplica a deals sin categoría."""
        sc = StoreConfig(
            name="test", enabled=True, interval_minutes=60,
            scrape_urls=["https://example.com/smartphones/offers"],
            client_type="http",
        )
        store = GenericStore(config=sc, http_client=AsyncMock())

        # Mock fetch to return HTML with products
        html = """
        <html><body>
        <script type="application/ld+json">
        {"@type": "Product", "name": "Phone X",
         "url": "https://example.com/phone-x",
         "offers": {"price": "499.99"}}
        </script>
        </body></html>
        """
        store.http_client.fetch = AsyncMock(return_value=html)

        deals = await store.scrape()
        assert len(deals) == 1
        assert deals[0].category == "phones"

    @pytest.mark.asyncio
    async def test_title_category_fallback(self):
        """Title-based category se aplica cuando URL no tiene categoría."""
        sc = StoreConfig(
            name="test", enabled=True, interval_minutes=60,
            scrape_urls=["https://example.com/offers"],
            client_type="http",
        )
        store = GenericStore(config=sc, http_client=AsyncMock())

        html = """
        <html><body>
        <script type="application/ld+json">
        {"@type": "Product", "name": "MacBook Air M2 256GB",
         "url": "https://example.com/macbook",
         "offers": {"price": "999.99"}}
        </script>
        </body></html>
        """
        store.http_client.fetch = AsyncMock(return_value=html)

        deals = await store.scrape()
        assert len(deals) == 1
        assert deals[0].category == "laptops"


# ===========================================================================
# 12. TELEGRAM BOT — async send methods
# ===========================================================================

class TestTelegramBotSend:
    @pytest.mark.asyncio
    async def test_send_deals(self):
        db = MagicMock()
        bot = TelegramBot(bot_token="fake:token", chat_id="123", db=db)
        bot._bot = AsyncMock()

        deals = [
            _make_deal(title="Deal 1", url="https://x.com/1", current_price=50, store="s", id=1,
                       image_url="https://img.com/1.jpg"),
            _make_deal(title="Deal 2", url="https://x.com/2", current_price=60, store="s", id=2),
        ]
        sent_ids = await bot.send_deals(deals, max_per_cycle=10)
        assert sent_ids == [1, 2]

    @pytest.mark.asyncio
    async def test_send_deal_immediate(self):
        db = MagicMock()
        bot = TelegramBot(bot_token="fake:token", chat_id="123", db=db)
        bot._bot = AsyncMock()

        d = _make_deal(title="Urgent Deal", store="s",
                       alert_tier="ERROR_DE_PRECIO",
                       current_price=50, original_price=200)
        await bot.send_deal_immediate(d)
        bot._bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_cross_store_deal(self):
        db = MagicMock()
        bot = TelegramBot(bot_token="fake:token", chat_id="123", db=db)
        bot._bot = AsyncMock()

        cheap = _make_deal(title="PS5", store="pccomp", current_price=400,
                           url="https://pccomp.com/ps5")
        expensive = _make_deal(title="PS5", store="amazon", current_price=500,
                               url="https://amazon.es/ps5")
        await bot.send_cross_store_deal(cheap, expensive)
        bot._bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_cycle_summary_silent_on_success(self):
        """Ciclo sin problemas no envía resumen."""
        db = MagicMock()
        bot = TelegramBot(bot_token="fake:token", chat_id="123", db=db)
        bot._bot = AsyncMock()

        await bot.send_cycle_summary(
            stores_scraped=10, stores_failed=0,
            total_deals=100, deals_sent=5, duration_secs=30.0,
        )
        bot._bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_cycle_summary_on_failure(self):
        """Ciclo con 0 deals sí envía resumen."""
        db = MagicMock()
        bot = TelegramBot(bot_token="fake:token", chat_id="123", db=db)
        bot._bot = AsyncMock()

        await bot.send_cycle_summary(
            stores_scraped=10, stores_failed=5,
            total_deals=0, deals_sent=0, duration_secs=30.0,
        )
        bot._bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_validate(self):
        db = MagicMock()
        bot = TelegramBot(bot_token="fake:token", chat_id="123", db=db)
        bot._bot = AsyncMock()
        bot._bot.get_me = AsyncMock(return_value=MagicMock(username="test_bot", first_name="Test"))
        await bot.validate()
        bot._bot.get_me.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_photo_fallback_to_text(self):
        """Si send_photo falla, hace fallback a send_message."""
        db = MagicMock()
        bot = TelegramBot(bot_token="fake:token", chat_id="123", db=db)
        bot._bot = AsyncMock()
        bot._bot.send_photo = AsyncMock(side_effect=Exception("Photo failed"))
        bot._bot.send_message = AsyncMock()

        d = _make_deal(title="With Image", store="s", current_price=100,
                       image_url="https://img.com/pic.jpg")
        await bot._send_deal(d)
        bot._bot.send_message.assert_called_once()


# ===========================================================================
# 13. INTEGRATION: full pipeline (mocked)
# ===========================================================================

class TestPipelineIntegration:
    """Tests de integración que verifican el flujo completo del pipeline."""

    def test_full_flow_filters_to_verify(self, tmp_db):
        """Flujo: deals → apply_filters → verify_real_deals."""
        # Poblar historial
        for url_id in range(5):
            url = f"https://x.com/product{url_id}"
            for _ in range(5):
                d = _make_deal(url=url, current_price=100 + url_id * 20,
                               store="amazon", category="phones")
                tmp_db.upsert_deal(d)

        # Simular bajada de precio real en product0
        d_cheap = _make_deal(
            title="Samsung Galaxy S24 128GB",
            url="https://x.com/product0",
            current_price=50.0,  # Era ~100, ahora 50
            original_price=100.0,
            discount_pct=50.0,
            store="amazon",
            category="phones",
        )
        tmp_db.upsert_deal(d_cheap)

        # Filtros básicos
        deals = [d_cheap]
        filtered = apply_filters(deals, {"min_discount": 10})
        assert len(filtered) == 1

        # Verificación
        verified = verify_real_deals(filtered, db=tmp_db, min_observations=2)
        assert len(verified) == 1
        assert verified[0].discount_pct > 0

    def test_watchlist_plus_filters(self, tmp_db):
        """Watchlist se procesa antes de apply_filters."""
        deals = [
            _make_deal(title="Apple iPhone 15 Pro 256GB", current_price=250,
                       original_price=600, url="https://x.com/iphone15pro"),
            _make_deal(title="Random Product", current_price=50, discount_pct=5,
                       url="https://x.com/random"),
        ]

        watchlist_cfg = {
            "enabled": True,
            "products": [{"name": "iPhone 15", "max_price": 600}],
        }

        # Watchlist capta el iPhone (store_disc=58%, >= min_discount 45%)
        wl = check_watchlist(deals, watchlist_cfg, min_discount=45.0)
        assert len(wl) == 1
        assert "iPhone" in wl[0].title

        # Filtros regulares no captan el random (5% < 45% min)
        random_only = [d for d in deals if "Random" in d.title]
        filtered = apply_filters(random_only, {"min_discount": 45})
        assert len(filtered) == 0

    def test_upsert_then_get_unsent(self, tmp_db):
        """Pipeline: upsert → get_unsent → mark_sent."""
        for i in range(5):
            tmp_db.upsert_deal(_make_deal(
                url=f"https://x.com/p{i}",
                discount_pct=float(20 + i * 5),
            ))

        unsent = tmp_db.get_unsent_deals()
        assert len(unsent) == 5

        ids = [d.id for d in unsent if d.id is not None]
        tmp_db.mark_sent(ids[:3])

        unsent_after = tmp_db.get_unsent_deals()
        assert len(unsent_after) == 2

    def test_price_change_resets_sent(self, tmp_db):
        """Si el precio cambia, sent_to_telegram se resetea a 0."""
        d = _make_deal(url="https://x.com/p1", current_price=100, discount_pct=20)
        deal_id, _ = tmp_db.upsert_deal(d)
        tmp_db.mark_sent([deal_id])

        # Cambio de precio
        d2 = _make_deal(url="https://x.com/p1", current_price=80, discount_pct=20)
        tmp_db.upsert_deal(d2)

        row = tmp_db.conn.execute(
            "SELECT sent_to_telegram FROM deals WHERE id = ?", (deal_id,)
        ).fetchone()
        assert row["sent_to_telegram"] == 0  # Reset por cambio de precio

    def test_same_price_keeps_sent(self, tmp_db):
        """Si el precio no cambia, sent_to_telegram se mantiene."""
        d = _make_deal(url="https://x.com/p1", current_price=100, discount_pct=20)
        deal_id, _ = tmp_db.upsert_deal(d)
        tmp_db.mark_sent([deal_id])

        # Mismo precio
        d2 = _make_deal(url="https://x.com/p1", current_price=100, discount_pct=20)
        tmp_db.upsert_deal(d2)

        row = tmp_db.conn.execute(
            "SELECT sent_to_telegram FROM deals WHERE id = ?", (deal_id,)
        ).fetchone()
        assert row["sent_to_telegram"] == 1  # No reseteado


# ===========================================================================
# 14. EDGE CASES
# ===========================================================================

class TestEdgeCases:
    def test_deal_with_very_long_title(self):
        """Títulos muy largos se manejan correctamente."""
        title = "X" * 1000
        d = _make_deal(title=title)
        formatted = _safe_title(d.title)
        assert len(formatted) == 203

    def test_empty_html_no_crash(self):
        """HTML vacío no causa crash."""
        sc = StoreConfig(
            name="test", enabled=True, interval_minutes=60,
            scrape_urls=["https://example.com"], client_type="http",
        )
        store = GenericStore(config=sc)
        deals = store.parse_deals("", "https://example.com")
        assert deals == []

    def test_malformed_json_ld(self):
        """JSON-LD malformado no causa crash."""
        html = """
        <html><body>
        <script type="application/ld+json">
        {not valid json!!!}
        </script>
        </body></html>
        """
        sc = StoreConfig(
            name="test", enabled=True, interval_minutes=60,
            scrape_urls=["https://example.com"], client_type="http",
        )
        store = GenericStore(config=sc)
        deals = store.parse_deals(html, "https://example.com")
        assert deals == []

    def test_special_chars_in_title(self):
        """Caracteres especiales en título se escapan correctamente."""
        d = _make_deal(title='Samsung "Galaxy" S25 <Ultra> & Pro')
        result = _safe_title(d.title)
        assert "&amp;" in result
        assert "&lt;" in result
        assert "&gt;" in result
        assert '"' in result  # Comillas no se escapan en Telegram HTML

    def test_zero_price_deal_rejected(self):
        """_parse_price rechaza precio 0."""
        assert _parse_price("0.00€") is None
        assert _parse_price("0") is None

    def test_database_close_and_reopen(self, tmp_path):
        """La DB se puede cerrar y reabrir sin pérdida de datos."""
        db_path = tmp_path / "reopen.db"
        db = Database(db_path=db_path)
        db.upsert_deal(_make_deal(url="https://x.com/persist", discount_pct=25))
        db.close()

        db2 = Database(db_path=db_path)
        unsent = db2.get_unsent_deals()
        assert len(unsent) == 1
        assert unsent[0].discount_pct == 25.0
        db2.close()

    def test_concurrent_upserts_same_url(self, tmp_db):
        """Múltiples upserts del mismo URL no crean duplicados."""
        for i in range(10):
            tmp_db.upsert_deal(_make_deal(
                url="https://x.com/same",
                current_price=100 - i,
            ))
        count = tmp_db.conn.execute("SELECT COUNT(*) as c FROM deals").fetchone()["c"]
        assert count == 1

        # Pero el historial tiene todas las observaciones
        stats = tmp_db.get_price_stats_by_url("https://x.com/same")
        assert stats["observations"] == 10


# ===========================================================================
# 15. WOOCOMMERCE STORE (API scraper)
# ===========================================================================

class TestWooCommerceStore:
    """Tests para el scraper WooCommerce via API."""

    def _make_store(self):
        sc = StoreConfig(
            name="lifeinformatica", enabled=True, interval_minutes=30,
            scrape_urls=["https://www.lifeinformatica.com/"],
            client_type="http",
        )
        return WooCommerceStore(config=sc, http_client=AsyncMock())

    def test_build_api_url(self):
        store = self._make_store()
        url = store._build_api_url("https://www.lifeinformatica.com/")
        assert url == "https://www.lifeinformatica.com/wp-json/wc/store/products"

    def test_build_api_url_with_path(self):
        store = self._make_store()
        url = store._build_api_url("https://www.lifeinformatica.com/tienda/ofertas/")
        assert url == "https://www.lifeinformatica.com/wp-json/wc/store/products"

    def test_product_to_deal_basic(self):
        store = self._make_store()
        product = {
            "name": "Logitech MK220 Wireless",
            "permalink": "https://lifeinformatica.com/tienda/logitech-mk220/",
            "prices": {
                "price": "2083",
                "regular_price": "2999",
                "sale_price": "2083",
                "currency_minor_unit": 2,
            },
            "is_in_stock": True,
            "images": [{"src": "https://img.com/mk220.jpg"}],
            "categories": [{"name": "Periféricos"}, {"name": "Teclados"}],
        }
        deal = store._product_to_deal(product)
        assert deal is not None
        assert deal.title == "Logitech MK220 Wireless"
        assert deal.current_price == 20.83
        assert deal.original_price == 29.99
        assert deal.url == "https://lifeinformatica.com/tienda/logitech-mk220/"
        assert deal.store == "lifeinformatica"
        assert deal.image_url == "https://img.com/mk220.jpg"
        assert deal.category == "Teclados"  # Última categoría (más específica)

    def test_product_to_deal_no_sale(self):
        """Producto sin descuento: regular_price == price."""
        store = self._make_store()
        product = {
            "name": "RTX 5060",
            "permalink": "https://lifeinformatica.com/tienda/rtx-5060/",
            "prices": {
                "price": "39900",
                "regular_price": "39900",
                "sale_price": "39900",
                "currency_minor_unit": 2,
            },
            "is_in_stock": True,
            "images": [],
            "categories": [],
        }
        deal = store._product_to_deal(product)
        assert deal is not None
        assert deal.current_price == 399.0
        assert deal.original_price is None  # No hay descuento

    def test_product_to_deal_out_of_stock(self):
        """Productos sin stock se descartan."""
        store = self._make_store()
        product = {
            "name": "Agotado",
            "permalink": "https://lifeinformatica.com/tienda/agotado/",
            "prices": {"price": "10000", "regular_price": "10000",
                       "sale_price": "10000", "currency_minor_unit": 2},
            "is_in_stock": False,
        }
        deal = store._product_to_deal(product)
        assert deal is None

    def test_product_to_deal_missing_permalink(self):
        """Sin permalink → se descarta."""
        store = self._make_store()
        product = {
            "name": "Sin URL",
            "permalink": "",
            "prices": {"price": "5000", "regular_price": "5000",
                       "sale_price": "5000", "currency_minor_unit": 2},
        }
        deal = store._product_to_deal(product)
        assert deal is None

    def test_product_to_deal_zero_price(self):
        """Precio 0 → se descarta."""
        store = self._make_store()
        product = {
            "name": "Gratis",
            "permalink": "https://x.com/gratis/",
            "prices": {"price": "0", "regular_price": "0",
                       "sale_price": "0", "currency_minor_unit": 2},
            "is_in_stock": True,
        }
        deal = store._product_to_deal(product)
        assert deal is None

    def test_html_entities_decoded(self):
        """HTML entities en nombres se decodifican."""
        store = self._make_store()
        product = {
            "name": "Monitor 27&#8243; IPS &amp; FHD",
            "permalink": "https://x.com/monitor/",
            "prices": {"price": "12300", "regular_price": "12300",
                       "sale_price": "12300", "currency_minor_unit": 2},
            "is_in_stock": True, "images": [], "categories": [],
        }
        deal = store._product_to_deal(product)
        assert deal is not None
        assert "&#" not in deal.title
        assert "&amp;" not in deal.title
        assert "″" in deal.title  # &#8243; = double prime
        assert "&" in deal.title

    def test_parse_products_batch(self):
        """_parse_products maneja una lista de productos."""
        store = self._make_store()
        products = [
            {"name": f"Product {i}",
             "permalink": f"https://x.com/p{i}/",
             "prices": {"price": str(10000 + i * 1000), "regular_price": str(15000 + i * 1000),
                        "sale_price": str(10000 + i * 1000), "currency_minor_unit": 2},
             "is_in_stock": True, "images": [], "categories": []}
            for i in range(5)
        ]
        deals = store._parse_products(products)
        assert len(deals) == 5

    def test_parse_deals_json(self):
        """parse_deals() acepta JSON string como fallback."""
        store = self._make_store()
        json_str = json.dumps([
            {"name": "Test", "permalink": "https://x.com/test/",
             "prices": {"price": "5000", "regular_price": "7000",
                        "sale_price": "5000", "currency_minor_unit": 2},
             "is_in_stock": True, "images": [], "categories": []},
        ])
        deals = store.parse_deals(json_str, "https://x.com/api")
        assert len(deals) == 1
        assert deals[0].current_price == 50.0

    @pytest.mark.asyncio
    async def test_scrape_pagination(self):
        """scrape() pagina correctamente la API."""
        store = self._make_store()

        # Página 1: 100 productos (max), página 2: 30 productos (fin)
        page1 = [
            {"name": f"P1-{i}", "permalink": f"https://x.com/p1-{i}/",
             "prices": {"price": "10000", "regular_price": "10000",
                        "sale_price": "10000", "currency_minor_unit": 2},
             "is_in_stock": True, "images": [], "categories": []}
            for i in range(100)
        ]
        page2 = [
            {"name": f"P2-{i}", "permalink": f"https://x.com/p2-{i}/",
             "prices": {"price": "20000", "regular_price": "20000",
                        "sale_price": "20000", "currency_minor_unit": 2},
             "is_in_stock": True, "images": [], "categories": []}
            for i in range(30)
        ]

        call_count = 0
        async def mock_fetch(url):
            nonlocal call_count
            call_count += 1
            # Match exactly page=1 (not page=10)
            if "&page=1" in url and "&page=10" not in url:
                return json.dumps(page1)
            return json.dumps(page2)

        store.http_client.fetch = mock_fetch
        deals = await store.scrape()
        assert len(deals) == 130
        assert call_count == 2


# ===========================================================================
# 16. CEX STORE (browser scraper)
# ===========================================================================

class TestCeXStore:
    """Tests para el scraper CEX via browser (HTML parsing)."""

    def _make_store(self):
        sc = StoreConfig(
            name="cex", enabled=True, interval_minutes=5,
            scrape_urls=["https://es.webuy.com/search?superCatId=4"],
            client_type="browser",
        )
        return CeXStore(config=sc, browser_client=AsyncMock())

    def _make_card_html(self, box_id="TEST123", title="iPhone 13 64GB",
                        price="299.00€", category="Moviles - Android",
                        img_src="https://es.static.webuy.com/product_images/Moviles/TEST123_l.jpg",
                        super_cat_name="MÓVILES"):
        """Helper para generar HTML de una tarjeta CEX (estructura real)."""
        return f"""
        <div class="wrapper-box">
            <div class="thumbnail">
                <div class="card-img">
                    <a href="/product-detail?id={box_id}&categoryName=CAT&superCatName={super_cat_name}">
                        <img src="https://es.static.webuy.com/images/category/es_badge.png" alt="badge">
                        <img src="{img_src}" alt="{title}">
                    </a>
                </div>
            </div>
            <div class="content">
                <div class="card-subtitle">{category}</div>
                <div class="card-title">
                    <a class="line-clamp" href="/product-detail?id={box_id}&categoryName=CAT&superCatName={super_cat_name}">
                        {title}
                    </a>
                </div>
                <div class="product-prices">
                    <div class="price-wrapper">
                        <p class="product-main-price">{price}</p>
                    </div>
                </div>
            </div>
        </div>
        """

    def test_parse_deals_basic(self):
        """Parsea una tarjeta de producto correctamente."""
        store = self._make_store()
        html = f"<html><body>{self._make_card_html()}</body></html>"
        deals = store.parse_deals(html, "https://es.webuy.com/search?superCatId=4")
        assert len(deals) == 1
        assert deals[0].title == "iPhone 13 64GB"
        assert deals[0].current_price == 299.0
        assert deals[0].original_price is None
        assert deals[0].store == "cex"
        assert "TEST123" in deals[0].url
        assert deals[0].category == "Moviles - Android"

    def test_parse_deals_multiple(self):
        """Parsea múltiples tarjetas."""
        store = self._make_store()
        cards = ""
        for i in range(5):
            cards += self._make_card_html(
                box_id=f"BOX{i}", title=f"Product {i}", price=f"{100 + i * 50}.00€",
            )
        html = f"<html><body>{cards}</body></html>"
        deals = store.parse_deals(html, "https://es.webuy.com/search")
        assert len(deals) == 5

    def test_parse_deals_empty(self):
        """HTML sin tarjetas devuelve lista vacía."""
        store = self._make_store()
        html = "<html><body><div>No products</div></body></html>"
        deals = store.parse_deals(html, "https://es.webuy.com/search")
        assert deals == []

    def test_card_no_title(self):
        """Tarjeta sin título se descarta."""
        store = self._make_store()
        html = """<html><body>
        <div class="wrapper-box">
            <div class="thumbnail"></div>
            <div class="content">
                <div class="card-title"><a href="/product-detail?id=X&categoryName=C"></a></div>
                <div class="product-prices"><p class="product-main-price">50.00€</p></div>
            </div>
        </div>
        </body></html>"""
        deals = store.parse_deals(html, "https://es.webuy.com/search")
        assert deals == []

    def test_card_no_price(self):
        """Tarjeta sin precio se descarta."""
        store = self._make_store()
        html = """<html><body>
        <div class="wrapper-box">
            <div class="thumbnail"></div>
            <div class="content">
                <div class="card-title">
                    <a href="/product-detail?id=X&categoryName=C">Some Product</a>
                </div>
            </div>
        </div>
        </body></html>"""
        deals = store.parse_deals(html, "https://es.webuy.com/search")
        assert deals == []

    def test_card_no_id_in_href(self):
        """Tarjeta sin id en href se descarta."""
        store = self._make_store()
        html = """<html><body>
        <div class="wrapper-box">
            <div class="thumbnail"></div>
            <div class="content">
                <div class="card-title">
                    <a href="/some-other-link">Product</a>
                </div>
                <div class="product-prices"><p class="product-main-price">50.00€</p></div>
            </div>
        </div>
        </body></html>"""
        deals = store.parse_deals(html, "https://es.webuy.com/search")
        assert deals == []

    def test_card_zero_price(self):
        """Precio 0 se descarta."""
        store = self._make_store()
        html = f"<html><body>{self._make_card_html(price='0.00€')}</body></html>"
        deals = store.parse_deals(html, "https://es.webuy.com/search")
        assert deals == []

    def test_extract_id(self):
        """Extrae ID del href correctamente."""
        store = self._make_store()
        assert store._extract_id("/product-detail?id=SIPH1364GB&cat=X") == "SIPH1364GB"
        assert store._extract_id("/product-detail?foo=bar&id=ABC123") == "ABC123"
        assert store._extract_id("/other-page") == ""

    def test_parse_price(self):
        """Parseo de precios CEX."""
        store = self._make_store()
        assert store._parse_price("165.00€") == 165.0
        assert store._parse_price("1,299.00€") == 1299.0
        assert store._parse_price("8.00€") == 8.0
        assert store._parse_price("0.00€") is None
        assert store._parse_price("") is None
        assert store._parse_price(None) is None

    def test_image_product_preferred(self):
        """Imagen con product_images se prefiere sobre badge."""
        store = self._make_store()
        html = f"<html><body>{self._make_card_html()}</body></html>"
        deals = store.parse_deals(html, "https://es.webuy.com/search")
        assert len(deals) == 1
        assert "product_images" in deals[0].image_url

    def test_category_from_subtitle(self):
        """Categoría se extrae de card-subtitle."""
        store = self._make_store()
        html = f"<html><body>{self._make_card_html(category='Apple iPhone')}</body></html>"
        deals = store.parse_deals(html, "https://es.webuy.com/search")
        assert len(deals) == 1
        assert deals[0].category == "Apple iPhone"

    def test_category_fallback_from_href(self):
        """Si no hay card-subtitle, usa superCatName del href."""
        store = self._make_store()
        html = """<html><body>
        <div class="wrapper-box">
            <div class="thumbnail"></div>
            <div class="content">
                <div class="card-title">
                    <a href="/product-detail?id=X1&superCatName=INFORM%C3%81TICA">MacBook Pro</a>
                </div>
                <div class="product-prices"><p class="product-main-price">899.00€</p></div>
            </div>
        </div>
        </body></html>"""
        deals = store.parse_deals(html, "https://es.webuy.com/search")
        assert len(deals) == 1
        assert deals[0].category == "INFORMÁTICA"


# ===========================================================================
# 17. LISTING URL SAFEGUARD
# ===========================================================================

class TestLooksLikeProductUrl:
    """Tests para la heurística de filtrado de URLs no-producto."""

    def test_product_urls_pass(self):
        assert _looks_like_product_url("https://lifeinformatica.com/tienda/logitech-mk220/") is True
        assert _looks_like_product_url("https://amazon.es/dp/B0ABCDEF123") is True
        assert _looks_like_product_url("https://pccomponentes.com/producto-largo-nombre") is True

    def test_homepage_rejected(self):
        assert _looks_like_product_url("https://example.com/") is False
        assert _looks_like_product_url("https://example.com") is False

    def test_listing_segments_rejected(self):
        assert _looks_like_product_url("https://lifeinformatica.com/destacados/") is False
        assert _looks_like_product_url("https://lifeinformatica.com/mas-vendidos/") is False
        assert _looks_like_product_url("https://example.com/ofertas/") is False
        assert _looks_like_product_url("https://example.com/outlet/") is False
        assert _looks_like_product_url("https://example.com/rebajas/") is False

    def test_multi_segment_listing_passes(self):
        """URLs con múltiples segmentos pasan (ej: /category/ofertas/)."""
        assert _looks_like_product_url("https://x.com/category/ofertas/") is True
        assert _looks_like_product_url("https://x.com/tienda/producto-slug/") is True

    def test_unknown_single_segment_passes(self):
        """Segmentos únicos desconocidos pasan (podrían ser productos)."""
        assert _looks_like_product_url("https://x.com/rtx-bundle-resident-evil/") is True


# ===========================================================================
# 18. NEW DATABASE METHODS (watchlist, search, recent, etc.)
# ===========================================================================

class TestDatabaseNewMethods:
    """Tests para los nuevos métodos de database.py."""

    def test_search_deals(self, tmp_db):
        tmp_db.upsert_deal(_make_deal(title="iPhone 15 Pro 256GB", url="https://x.com/iphone15",
                                       discount_pct=30))
        tmp_db.upsert_deal(_make_deal(title="Samsung Galaxy S25", url="https://x.com/galaxy",
                                       discount_pct=20))
        results = tmp_db.search_deals("iPhone")
        assert len(results) == 1
        assert "iPhone" in results[0].title

    def test_search_deals_empty(self, tmp_db):
        results = tmp_db.search_deals("nonexistent")
        assert results == []

    def test_get_recent_deals(self, tmp_db):
        tmp_db.upsert_deal(_make_deal(url="https://x.com/recent1", discount_pct=25))
        tmp_db.upsert_deal(_make_deal(url="https://x.com/recent2", discount_pct=15))
        results = tmp_db.get_recent_deals(hours=24, limit=10)
        assert len(results) == 2

    def test_get_top_deals_since(self, tmp_db):
        tmp_db.upsert_deal(_make_deal(url="https://x.com/top1", discount_pct=50))
        tmp_db.upsert_deal(_make_deal(url="https://x.com/top2", discount_pct=30))
        tmp_db.upsert_deal(_make_deal(url="https://x.com/top3", discount_pct=10))
        results = tmp_db.get_top_deals_since(hours=24, limit=2)
        assert len(results) == 2
        assert results[0].discount_pct >= results[1].discount_pct

    def test_get_price_history(self, tmp_db):
        deal_id, _ = tmp_db.upsert_deal(_make_deal(url="https://x.com/ph1", current_price=100))
        # Upsert again with different price
        tmp_db.upsert_deal(_make_deal(url="https://x.com/ph1", current_price=90))
        history = tmp_db.get_price_history(deal_id)
        assert len(history) >= 2
        assert all("price" in h and "detected_at" in h for h in history)

    def test_get_deal_by_id(self, tmp_db):
        deal_id, _ = tmp_db.upsert_deal(_make_deal(url="https://x.com/byid", title="Find Me"))
        deal = tmp_db.get_deal_by_id(deal_id)
        assert deal is not None
        assert deal.title == "Find Me"

    def test_get_deal_by_id_not_found(self, tmp_db):
        assert tmp_db.get_deal_by_id(99999) is None

    def test_get_store_stats(self, tmp_db):
        for i in range(3):
            tmp_db.upsert_deal(_make_deal(url=f"https://x.com/s1-{i}", store="amazon", discount_pct=20))
        for i in range(2):
            tmp_db.upsert_deal(_make_deal(url=f"https://x.com/s2-{i}", store="mediamarkt", discount_pct=30))
        stats = tmp_db.get_store_stats()
        assert len(stats) == 2
        names = [s["store"] for s in stats]
        assert "amazon" in names
        assert "mediamarkt" in names


class TestDatabaseWatchlist:
    """Tests para la watchlist dinámica en SQLite."""

    def test_add_watchlist_item(self, tmp_db):
        item_id = tmp_db.add_watchlist_item("RTX 5070", 500)
        assert item_id is not None
        items = tmp_db.get_watchlist_items()
        assert len(items) == 1
        assert items[0]["name"] == "RTX 5070"
        assert items[0]["max_price"] == 500

    def test_add_duplicate_replaces(self, tmp_db):
        tmp_db.add_watchlist_item("RTX 5070", 500)
        tmp_db.add_watchlist_item("RTX 5070", 600)
        items = tmp_db.get_watchlist_items()
        assert len(items) == 1
        assert items[0]["max_price"] == 600

    def test_remove_watchlist_item(self, tmp_db):
        tmp_db.add_watchlist_item("RTX 5070", 500)
        assert tmp_db.remove_watchlist_item("RTX 5070") is True
        assert tmp_db.get_watchlist_items() == []

    def test_remove_nonexistent(self, tmp_db):
        assert tmp_db.remove_watchlist_item("nonexistent") is False

    def test_watchlist_empty(self, tmp_db):
        assert tmp_db.get_watchlist_items() == []

    def test_wal_mode(self, tmp_db):
        cur = tmp_db.conn.execute("PRAGMA journal_mode")
        mode = cur.fetchone()[0]
        assert mode == "wal"


# ===========================================================================
# 19. DETECT PRICE DROPS
# ===========================================================================

class TestDetectPriceDrops:
    """Tests para detect_price_drops en filters.py."""

    def test_price_drop_detected(self, tmp_db):
        from deals_scraper.filters import detect_price_drops
        # Create deal with history of high prices
        d = _make_deal(url="https://x.com/drop1", current_price=200)
        deal_id, _ = tmp_db.upsert_deal(d)
        for price in [200, 195, 210, 200]:
            tmp_db.conn.execute(
                "INSERT INTO price_history (deal_id, price, detected_at) VALUES (?, ?, ?)",
                (deal_id, price, datetime.utcnow().isoformat()),
            )
        # Now price drops to 150
        tmp_db.upsert_deal(_make_deal(url="https://x.com/drop1", current_price=150))
        tmp_db.conn.commit()

        deal = _make_deal(url="https://x.com/drop1", current_price=150, id=deal_id)
        drops = detect_price_drops([deal], db=tmp_db, drop_threshold=20.0, min_observations=3)
        assert len(drops) == 1
        assert drops[0].alert_tier == "BAJADA_PRECIO"

    def test_no_drop_when_price_stable(self, tmp_db):
        from deals_scraper.filters import detect_price_drops
        d = _make_deal(url="https://x.com/stable", current_price=100)
        deal_id, _ = tmp_db.upsert_deal(d)
        for price in [100, 102, 99, 101]:
            tmp_db.conn.execute(
                "INSERT INTO price_history (deal_id, price, detected_at) VALUES (?, ?, ?)",
                (deal_id, price, datetime.utcnow().isoformat()),
            )
        tmp_db.conn.commit()

        deal = _make_deal(url="https://x.com/stable", current_price=100, id=deal_id)
        drops = detect_price_drops([deal], db=tmp_db, drop_threshold=20.0, min_observations=3)
        assert len(drops) == 0

    def test_insufficient_observations(self, tmp_db):
        from deals_scraper.filters import detect_price_drops
        d = _make_deal(url="https://x.com/few", current_price=50)
        deal_id, _ = tmp_db.upsert_deal(d)
        # Only 1 observation from upsert
        deal = _make_deal(url="https://x.com/few", current_price=50, id=deal_id)
        drops = detect_price_drops([deal], db=tmp_db, drop_threshold=20.0, min_observations=3)
        assert len(drops) == 0


# ===========================================================================
# 20. CHARTS
# ===========================================================================

class TestCharts:
    """Tests para generate_price_chart."""

    def test_generate_chart(self):
        from deals_scraper.charts import generate_price_chart
        history = [
            {"price": 100.0, "detected_at": "2026-01-01T10:00:00"},
            {"price": 95.0, "detected_at": "2026-01-02T10:00:00"},
            {"price": 90.0, "detected_at": "2026-01-03T10:00:00"},
            {"price": 85.0, "detected_at": "2026-01-04T10:00:00"},
        ]
        result = generate_price_chart("Test Product", history)
        assert result is not None
        assert isinstance(result, bytes)
        assert len(result) > 100
        # PNG magic bytes
        assert result[:4] == b"\x89PNG"

    def test_insufficient_data(self):
        from deals_scraper.charts import generate_price_chart
        assert generate_price_chart("Test", []) is None
        assert generate_price_chart("Test", [{"price": 100, "detected_at": "2026-01-01T00:00:00"}]) is None


# ===========================================================================
# 21. MODELS — proxy_url in StoreConfig
# ===========================================================================

class TestStoreConfigProxy:
    """Tests para proxy_url en StoreConfig."""

    def test_proxy_url_default_none(self):
        sc = StoreConfig.from_dict({"name": "test", "scrape_urls": []})
        assert sc.proxy_url is None

    def test_proxy_url_from_dict(self):
        sc = StoreConfig.from_dict({
            "name": "test", "scrape_urls": [],
            "proxy_url": "http://proxy:8080",
        })
        assert sc.proxy_url == "http://proxy:8080"


# ===========================================================================
# 22. TELEGRAM FORMATTING — BAJADA_PRECIO
# ===========================================================================

class TestTelegramPriceDropFormat:
    """Tests para el formato de bajada de precio."""

    def test_format_price_drop(self):
        d = _make_deal(
            title="Samsung Galaxy S25",
            current_price=799.99,
            original_price=999.99,
            discount_pct=20.0,
            store="amazon",
            alert_tier="BAJADA_PRECIO",
        )
        result = TelegramBot._format_price_drop(d)
        assert "BAJADA DE PRECIO" in result
        assert "799.99" in result

    def test_format_deal_routes_price_drop(self):
        d = _make_deal(
            current_price=100, store="s",
            alert_tier="BAJADA_PRECIO",
            original_price=150, discount_pct=33,
        )
        result = TelegramBot._format_deal(d)
        assert "BAJADA DE PRECIO" in result


# ===========================================================================
# 23. WATCHLIST MERGE (DB + YAML)
# ===========================================================================

class TestWatchlistMerge:
    """Tests para el merge de watchlist YAML + DB."""

    def test_merge_db_items(self, tmp_db):
        tmp_db.add_watchlist_item("RTX 5070", 500)
        deals = [
            _make_deal(title="RTX 5070 Gaming GPU", current_price=200,
                       original_price=500, url="https://x.com/rtx5070"),
        ]
        watchlist_cfg = {"enabled": True, "products": []}
        matched = check_watchlist(deals, watchlist_cfg, min_discount=30.0, db=tmp_db)
        assert len(matched) == 1

    def test_no_duplicate_yaml_db(self, tmp_db):
        """Si el mismo producto está en YAML y DB, no duplicar."""
        tmp_db.add_watchlist_item("RTX 5070", 500)
        deals = [
            _make_deal(title="RTX 5070 Gaming GPU", current_price=200,
                       original_price=500, url="https://x.com/rtx5070"),
        ]
        watchlist_cfg = {
            "enabled": True,
            "products": [{"name": "RTX 5070", "max_price": 600}],
        }
        matched = check_watchlist(deals, watchlist_cfg, min_discount=30.0, db=tmp_db)
        # Should match once, not twice
        assert len(matched) == 1
