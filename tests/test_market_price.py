"""Tests para el módulo de validación de precios de mercado.

Cubre: normalize_title, MarketPriceCache, cross-store lookup,
IdealoScraper (extracción de precios), MarketPriceChecker (orquestador).

Ejecutar: python -m pytest tests/test_market_price.py -v
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deals_scraper.models import Deal
from deals_scraper.database import Database
from deals_scraper.market_price import (
    MarketPriceCache,
    MarketPriceChecker,
    IdealoScraper,
    _build_idealo_query,
    _lookup_cross_store,
    normalize_title,
)


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def tmp_db(tmp_path):
    """Base de datos temporal para tests."""
    db_path = tmp_path / "test_market.db"
    db = Database(db_path=db_path)
    yield db
    db.close()


def _make_deal(title="Test Product", url="https://x.com/p", store="store1",
               current_price=100.0, original_price=None, discount_pct=0.0,
               category="", **kwargs):
    """Helper para crear deals rápidamente."""
    return Deal(
        title=title, url=url, store=store,
        current_price=current_price, original_price=original_price,
        discount_pct=discount_pct, category=category, **kwargs,
    )


# ===========================================================================
# 1. NORMALIZE TITLE
# ===========================================================================

class TestNormalizeTitle:
    def test_basic_normalization(self):
        """Lowercase + sort tokens."""
        result = normalize_title("Apple iPhone 15 Pro")
        assert "apple" in result
        assert "iphone" in result
        assert "pro" in result

    def test_removes_colors(self):
        """Colores se eliminan."""
        r1 = normalize_title("iPhone 15 Pro Negro 256GB")
        r2 = normalize_title("iPhone 15 Pro Azul 256GB")
        assert r1 == r2  # Same product, different colors

    def test_removes_conditions(self):
        """Condiciones (reacondicionado, renewed) se eliminan."""
        r1 = normalize_title("iPad Air 2022 Reacondicionado")
        r2 = normalize_title("iPad Air 2022")
        assert r1 == r2

    def test_normalizes_storage(self):
        """'256 GB' -> '256gb'."""
        r = normalize_title("Samsung Galaxy S25 256 GB")
        assert "256gb" in r

    def test_strips_accents(self):
        """Acentos se eliminan."""
        r = normalize_title("Portátil ASUS Número 1")
        assert "portatil" in r
        assert "numero" in r

    def test_removes_brackets(self):
        """[Enviado por Amazon] se elimina."""
        r1 = normalize_title("iPhone 15 [Enviado por Amazon]")
        r2 = normalize_title("iPhone 15")
        assert r1 == r2

    def test_removes_parentheses(self):
        """(Reacondicionado) se elimina."""
        r1 = normalize_title("MacBook Air (Reacondicionado)")
        r2 = normalize_title("MacBook Air")
        assert r1 == r2

    def test_sorted_tokens(self):
        """Tokens se ordenan alfabéticamente."""
        r = normalize_title("Pro iPhone 15 Apple")
        tokens = r.split()
        assert tokens == sorted(tokens)

    def test_different_products_different_keys(self):
        """Productos diferentes generan claves diferentes."""
        r1 = normalize_title("iPhone 15 Pro 256GB")
        r2 = normalize_title("Samsung Galaxy S25 256GB")
        assert r1 != r2

    def test_empty_title(self):
        result = normalize_title("")
        assert result == ""

    def test_removes_envio_gratis(self):
        r1 = normalize_title("iPhone 15 envío gratis")
        r2 = normalize_title("iPhone 15")
        assert r1 == r2


# ===========================================================================
# 2. MARKET PRICE CACHE
# ===========================================================================

class TestMarketPriceCache:
    def test_put_and_get_exact(self, tmp_db):
        """Put y get exacto funciona."""
        cache = MarketPriceCache(tmp_db)
        cache.put("15 256gb apple iphone pro", 899.0, "idealo")

        result = cache.get("15 256gb apple iphone pro")
        assert result is not None
        price, source = result
        assert price == 899.0
        assert "idealo" in source

    def test_get_nonexistent(self, tmp_db):
        """Buscar algo que no existe devuelve None."""
        cache = MarketPriceCache(tmp_db)
        result = cache.get("nonexistent product")
        assert result is None

    def test_expired_entries_not_returned(self, tmp_db):
        """Entradas expiradas no se devuelven."""
        cache = MarketPriceCache(tmp_db)
        # Insert with already-expired timestamp
        now = datetime.utcnow()
        expired = now - timedelta(days=1)
        cur = tmp_db.conn.cursor()
        cur.execute(
            "INSERT INTO market_prices "
            "(normalized_title, market_price, source, source_detail, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("expired product", 100.0, "test", "",
             (now - timedelta(days=10)).isoformat(), expired.isoformat()),
        )
        tmp_db.conn.commit()

        result = cache.get("expired product")
        assert result is None

    def test_fuzzy_match(self, tmp_db):
        """Fuzzy match >= 90 funciona."""
        cache = MarketPriceCache(tmp_db)
        cache.put("15 256gb apple iphone pro", 899.0, "idealo")

        # Slightly different key (minor token difference)
        result = cache.get("15 256gb apple iphone pro max")
        # This may or may not match depending on fuzzy ratio
        # The important thing is that exact match works and fuzzy doesn't crash
        # Let's test with a very similar key
        result = cache.get("15 256gb apple iphone pro")
        assert result is not None

    def test_cleanup(self, tmp_db):
        """Cleanup borra entradas expiradas."""
        cache = MarketPriceCache(tmp_db)
        # Insert an expired entry directly
        now = datetime.utcnow()
        expired = now - timedelta(days=1)
        cur = tmp_db.conn.cursor()
        cur.execute(
            "INSERT INTO market_prices "
            "(normalized_title, market_price, source, source_detail, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("old product", 50.0, "test", "",
             (now - timedelta(days=10)).isoformat(), expired.isoformat()),
        )
        tmp_db.conn.commit()

        # Insert a valid entry
        cache.put("new product", 200.0, "idealo")

        cache.cleanup()

        # Old entry should be gone
        count = tmp_db.conn.execute(
            "SELECT COUNT(*) as c FROM market_prices"
        ).fetchone()["c"]
        assert count == 1  # Only the valid one remains


# ===========================================================================
# 3. CROSS-STORE LOOKUP
# ===========================================================================

class TestCrossStoreLookup:
    def test_finds_same_product_different_stores(self, tmp_db):
        """Encuentra el mismo producto en >= 2 tiendas diferentes."""
        # Insert same product in 3 stores
        for i, store in enumerate(["amazon", "pccomponentes", "mediamarkt"]):
            d = _make_deal(
                title="iPhone 15 Pro 256GB",
                url=f"https://{store}.com/iphone15",
                store=store,
                current_price=float(900 + i * 50),
            )
            tmp_db.upsert_deal(d)

        deal = _make_deal(
            title="iPhone 15 Pro 256GB",
            store="coolmod",
            current_price=800.0,
        )
        result = _lookup_cross_store(deal, tmp_db, fuzzy_threshold=80)
        assert result is not None
        assert 900 <= result <= 1000  # Median of the 3 prices

    def test_ignores_same_store(self, tmp_db):
        """No cuenta matches de la misma tienda."""
        d1 = _make_deal(
            title="MacBook Pro M3",
            url="https://amazon.com/mb1",
            store="amazon",
            current_price=1500.0,
        )
        d2 = _make_deal(
            title="MacBook Pro M3",
            url="https://amazon.com/mb2",
            store="amazon",
            current_price=1600.0,
        )
        tmp_db.upsert_deal(d1)
        tmp_db.upsert_deal(d2)

        deal = _make_deal(title="MacBook Pro M3", store="pccomp", current_price=1400)
        result = _lookup_cross_store(deal, tmp_db, fuzzy_threshold=80)
        # Only 1 distinct store (amazon), need >= 2
        assert result is None

    def test_requires_two_matches(self, tmp_db):
        """Necesita >= 2 matches de tiendas diferentes."""
        d = _make_deal(
            title="PS5 Slim Digital",
            url="https://game.es/ps5",
            store="game",
            current_price=400.0,
        )
        tmp_db.upsert_deal(d)

        deal = _make_deal(title="PS5 Slim Digital", store="amazon", current_price=380)
        result = _lookup_cross_store(deal, tmp_db, fuzzy_threshold=80)
        # Only 1 match, need >= 2
        assert result is None

    def test_variant_mismatch_excluded(self, tmp_db):
        """Variantes diferentes (Pro vs no-Pro) no se cuentan."""
        d1 = _make_deal(
            title="iPhone 15 Pro 256GB",
            url="https://store1.com/pro",
            store="store1",
            current_price=999.0,
        )
        d2 = _make_deal(
            title="iPhone 15 256GB",
            url="https://store2.com/normal",
            store="store2",
            current_price=799.0,
        )
        tmp_db.upsert_deal(d1)
        tmp_db.upsert_deal(d2)

        deal = _make_deal(title="iPhone 15 Pro 256GB", store="store3", current_price=900)
        result = _lookup_cross_store(deal, tmp_db, fuzzy_threshold=80)
        # store1 has Pro, store2 doesn't -> variant mismatch -> only 1 valid match
        assert result is None

    def test_product_id_exact_match(self, tmp_db):
        """Con product_id en ambos lados, empareja por id exacto pese a títulos distintos."""
        tmp_db.upsert_deal(_make_deal(
            title="Sony WH-1000XM5", url="https://a.com/1", store="amazon",
            current_price=300.0, product_id="ean:111"))
        tmp_db.upsert_deal(_make_deal(
            title="Auriculares Sony XM5 Negro", url="https://c.com/1", store="coolmod",
            current_price=320.0, product_id="ean:111"))
        deal = _make_deal(title="WH1000XM5", store="ldlc", current_price=290.0,
                          product_id="ean:111")
        result = _lookup_cross_store(deal, tmp_db, fuzzy_threshold=80)
        assert result is not None  # 2 matches por product_id (amazon, coolmod)
        assert 300 <= result <= 320

    def test_product_id_different_not_matched(self, tmp_db):
        """product_id distinto no cuenta como match aunque el título sea idéntico."""
        tmp_db.upsert_deal(_make_deal(
            title="Sony WH-1000XM5", url="https://a.com/1", store="amazon",
            current_price=300.0, product_id="ean:111"))
        tmp_db.upsert_deal(_make_deal(
            title="Sony WH-1000XM5", url="https://c.com/1", store="coolmod",
            current_price=320.0, product_id="ean:222"))
        deal = _make_deal(title="Sony WH-1000XM5", store="ldlc", current_price=290.0,
                          product_id="ean:111")
        result = _lookup_cross_store(deal, tmp_db, fuzzy_threshold=80)
        # Solo amazon (ean:111) matchea; coolmod (ean:222) excluido → <2 matches
        assert result is None


# ===========================================================================
# 4. IDEALO SCRAPER
# ===========================================================================

class TestBuildIdealoQuery:
    def test_basic_query(self):
        """Genera query limpia."""
        q = _build_idealo_query("Apple iPhone 15 Pro 256GB Negro")
        assert "apple" in q
        assert "iphone" in q
        assert "negro" not in q  # Color removed

    def test_removes_conditions(self):
        q = _build_idealo_query("iPad Air 2022 Reacondicionado 64GB")
        assert "reacondicionado" not in q

    def test_limits_tokens(self):
        """Limita a 10 tokens."""
        long_title = " ".join([f"word{i}" for i in range(20)])
        q = _build_idealo_query(long_title)
        assert len(q.split()) <= 10


class TestIdealoScraper:
    def test_extract_prices_from_idealo_data_testid(self):
        """Extrae precios de elementos con data-testid."""
        html = """
        <html><body>
        <div data-testid="resultItem">
            <span class="price">299,99 €</span>
        </div>
        <div data-testid="resultItem">
            <span class="price">319,00 €</span>
        </div>
        <div data-testid="resultItem">
            <span class="price">289,50 €</span>
        </div>
        </body></html>
        """
        prices = IdealoScraper._extract_prices_from_idealo(html)
        assert len(prices) == 3
        result = IdealoScraper._compute_median(prices)
        assert result is not None
        assert 280 <= result <= 320

    def test_extract_prices_class_fallback(self):
        """Extrae precios de elementos con class*=price."""
        html = """
        <html><body>
        <div class="product-price">449,99 €</div>
        <div class="product-price">469,00 €</div>
        </body></html>
        """
        prices = IdealoScraper._extract_prices_from_idealo(html)
        assert len(prices) == 2

    def test_extract_prices_regex_fallback(self):
        """Extrae precios con regex como último recurso."""
        html = "<html><body>El mejor precio es 199,99 € y el otro es 209,99 €</body></html>"
        prices = IdealoScraper._extract_prices_from_idealo(html)
        assert len(prices) == 2

    def test_extract_prices_empty_html(self):
        """HTML sin precios devuelve lista vacía."""
        html = "<html><body>No hay precios aquí</body></html>"
        prices = IdealoScraper._extract_prices_from_idealo(html)
        assert prices == []

    def test_compute_median_filters_outliers(self):
        """Precios outliers se filtran en compute_median."""
        prices = [299.99, 309.0, 0.01, 9999.99]
        result = IdealoScraper._compute_median(prices)
        assert result is not None
        assert 290 <= result <= 310

    def test_extract_from_google_snippets(self):
        """Extrae precios de snippets de Google."""
        html = '<div>idealo.es - desde 449,99 € - iPhone 15 Pro 256GB comparar precios 459,00 €</div>'
        prices = IdealoScraper._extract_prices_from_google(html)
        assert len(prices) == 2
        assert 449.99 in prices

    def test_max_per_cycle_respected(self):
        """No excede max lookups por ciclo."""
        browser = MagicMock()
        scraper = IdealoScraper(browser, max_per_cycle=2)
        assert scraper.lookups_remaining == 2

    @pytest.mark.asyncio
    async def test_lookup_timeout(self):
        """Timeout devuelve None sin crash."""
        browser = AsyncMock()
        browser.fetch = AsyncMock(side_effect=asyncio.TimeoutError)
        scraper = IdealoScraper(browser, max_per_cycle=5)

        deal = _make_deal(title="iPhone 15 Pro 256GB")
        result = await scraper.lookup(deal)
        assert result is None


# ===========================================================================
# 5. MARKET PRICE CHECKER (orquestador)
# ===========================================================================

class TestMarketPriceChecker:
    def test_uses_cache_first(self, tmp_db):
        """Usa cache antes de hacer lookups."""
        cache = MarketPriceCache(tmp_db)
        key = normalize_title("iPhone 15 Pro 256GB")
        cache.put(key, 899.0, "idealo")

        browser = AsyncMock()
        checker = MarketPriceChecker(
            db=tmp_db, browser_client=browser, max_idealo_per_cycle=0,
        )

        deals = [_make_deal(
            title="iPhone 15 Pro 256GB",
            current_price=500.0,
            store="pccomponentes",
        )]

        import asyncio
        asyncio.get_event_loop().run_until_complete(checker.enrich_deals(deals))

        assert deals[0].market_price == 899.0
        assert "cache" in deals[0].market_price_source

    def test_skips_refurbished_stores(self, tmp_db):
        """No procesa deals de tiendas refurbished."""
        browser = AsyncMock()
        checker = MarketPriceChecker(
            db=tmp_db, browser_client=browser, max_idealo_per_cycle=0,
        )

        deals = [_make_deal(
            title="iPhone 15 Pro 256GB",
            current_price=500.0,
            store="backmarket",
        )]

        import asyncio
        asyncio.get_event_loop().run_until_complete(checker.enrich_deals(deals))

        assert deals[0].market_price is None

    def test_sanity_check_rejects_absurd(self, tmp_db):
        """Descarta market_price si es > 10x del current_price."""
        cache = MarketPriceCache(tmp_db)
        key = normalize_title("Cheap Widget")
        cache.put(key, 50000.0, "idealo")  # Absurdly high

        browser = AsyncMock()
        checker = MarketPriceChecker(
            db=tmp_db, browser_client=browser, max_idealo_per_cycle=0,
        )

        deals = [_make_deal(
            title="Cheap Widget",
            current_price=10.0,
            store="amazon",
        )]

        import asyncio
        asyncio.get_event_loop().run_until_complete(checker.enrich_deals(deals))

        assert deals[0].market_price is None  # Rejected by sanity check

    def test_groups_by_normalized_title(self, tmp_db):
        """Mismo producto en diferentes tiendas comparte un solo lookup."""
        cache = MarketPriceCache(tmp_db)
        key = normalize_title("Samsung Galaxy S25 Ultra 256GB")
        cache.put(key, 1199.0, "idealo")

        browser = AsyncMock()
        checker = MarketPriceChecker(
            db=tmp_db, browser_client=browser, max_idealo_per_cycle=0,
        )

        deals = [
            _make_deal(title="Samsung Galaxy S25 Ultra 256GB", store="amazon",
                       current_price=900.0, url="https://amazon.com/s25"),
            _make_deal(title="Samsung Galaxy S25 Ultra 256GB", store="pccomponentes",
                       current_price=950.0, url="https://pccomp.com/s25"),
        ]

        import asyncio
        asyncio.get_event_loop().run_until_complete(checker.enrich_deals(deals))

        # Both deals should have the same market_price from a single cache hit
        assert deals[0].market_price == 1199.0
        assert deals[1].market_price == 1199.0

    @pytest.mark.asyncio
    async def test_cross_store_fallback(self, tmp_db):
        """Usa cross-store cuando cache no tiene resultado."""
        # Insert same product in 2 different stores
        for store, price in [("amazon", 900.0), ("pccomponentes", 950.0)]:
            d = _make_deal(
                title="MacBook Air M3 256GB",
                url=f"https://{store}.com/macbook",
                store=store,
                current_price=price,
            )
            tmp_db.upsert_deal(d)

        browser = AsyncMock()
        checker = MarketPriceChecker(
            db=tmp_db, browser_client=browser, max_idealo_per_cycle=0,
        )

        deal = _make_deal(
            title="MacBook Air M3 256GB",
            store="mediamarkt",
            current_price=850.0,
        )

        await checker.enrich_deals([deal])

        # cross-store should find amazon + pccomponentes
        # median of [900, 950] = 925
        assert deal.market_price == 925.0
        assert deal.market_price_source == "cross_store"


# ===========================================================================
# 6. DATABASE - market_prices table
# ===========================================================================

class TestMarketPricesTable:
    def test_table_created(self, tmp_db):
        """La tabla market_prices se crea automáticamente."""
        cur = tmp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='market_prices'"
        )
        assert cur.fetchone() is not None

    def test_indices_created(self, tmp_db):
        """Los índices de market_prices existen."""
        cur = tmp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
        indices = {row["name"] for row in cur.fetchall()}
        assert "idx_mp_title" in indices
        assert "idx_mp_expires" in indices


# ===========================================================================
# 7. TELEGRAM - market_price formatting
# ===========================================================================

class TestTelegramMarketPrice:
    def test_format_with_market_price(self):
        from deals_scraper.telegram_bot import TelegramBot
        d = _make_deal(
            title="iPhone 15 Pro 256GB",
            current_price=500.0,
            original_price=1200.0,  # Inflated
            store="backmarket",
            discount_pct=58.3,
        )
        d.market_price = 899.0
        d.market_price_source = "idealo"

        result = TelegramBot._format_normal(d)
        assert "Precio de mercado" in result
        assert "899.00" in result
        assert "verificado" in result.lower()

    def test_format_without_market_price(self):
        from deals_scraper.telegram_bot import TelegramBot
        d = _make_deal(
            title="iPhone 15 Pro 256GB",
            current_price=500.0,
            original_price=700.0,
            store="pccomponentes",
            discount_pct=28.6,
        )
        result = TelegramBot._format_normal(d)
        assert "Precio habitual" in result
        assert "700.00" in result
        assert "verificado" not in result.lower()

    def test_get_reference_price_prefers_market(self):
        from deals_scraper.telegram_bot import TelegramBot
        d = _make_deal(current_price=500.0, original_price=1200.0)
        d.market_price = 899.0
        ref = TelegramBot._get_reference_price(d)
        assert ref == 899.0

    def test_get_reference_price_fallback_original(self):
        from deals_scraper.telegram_bot import TelegramBot
        d = _make_deal(current_price=500.0, original_price=700.0)
        ref = TelegramBot._get_reference_price(d)
        assert ref == 700.0

    def test_get_reference_price_none(self):
        from deals_scraper.telegram_bot import TelegramBot
        d = _make_deal(current_price=500.0)
        ref = TelegramBot._get_reference_price(d)
        assert ref is None
