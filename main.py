"""Punto de entrada CLI — deals-scraper.

Uso:
    python main.py --once       # Ejecución única
    python main.py --daemon     # Loop con APScheduler
    python main.py --config path/to/config.yaml
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import signal
import sys
import time

import click

from deals_scraper.config import get_anti_ban, get_filters, get_store_configs, load_config
from deals_scraper.database import Database
from deals_scraper.filters import (
    apply_filters,
    calculate_discount,
    check_watchlist,
    detect_absurdly_cheap,
    detect_cross_store_bargains,
    detect_price_drops,
    verify_real_deals,
)
from deals_scraper.market_price import MarketPriceChecker
from deals_scraper.http_client import HttpClient
from deals_scraper.browser_client import BrowserClient
from deals_scraper.scheduler import Scheduler
from deals_scraper.stores import STORE_REGISTRY, DEFAULT_STORE_CLASS
from deals_scraper.telegram_bot import TelegramBot

logger = logging.getLogger("deals_scraper")

# PID del proceso principal — para matar Chrome hijos al salir
_MY_PID = os.getpid()


def _cleanup_chrome_processes() -> None:
    """Mata procesos Chrome/Chromium que sean hijos de este proceso."""
    import subprocess
    try:
        # Buscar procesos Chrome cuyo parent sea nuestro PID o hayan quedado huérfanos
        result = subprocess.run(
            ["pgrep", "-f", "chrome.*--headless|chrome-headless|chromium"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            for pid_str in pids:
                try:
                    pid = int(pid_str)
                    if pid != _MY_PID:
                        os.kill(pid, signal.SIGTERM)
                except (ValueError, ProcessLookupError):
                    pass
            logger.info("Limpieza: %d procesos Chrome terminados", len(pids))
    except Exception:
        pass


atexit.register(_cleanup_chrome_processes)


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def run_cycle(
    cfg: dict,
    db: Database,
    http_client: HttpClient,
    browser_client: BrowserClient,
    telegram_bot: TelegramBot,
    store_name: str | None = None,
) -> None:
    """Ejecuta un ciclo de scraping para todas las tiendas activas (o una específica).

    Flujo optimizado:
    1. Scrapear tiendas EN PARALELO (Semaphore limita concurrencia)
    2. DB upserts secuenciales (SQLite single-writer)
    3. Filtros básicos + verificación anti-fake
    4. Errores de precio → alerta inmediata a Telegram
    5. Resto de ofertas verificadas → batch a Telegram
    """
    store_configs = get_store_configs(cfg)
    filters_cfg = get_filters(cfg)
    speed_cfg = cfg.get("speed", {})
    speed_mode = speed_cfg.get("mode", "stealth") == "fast"
    anti_ban = get_anti_ban(cfg)
    max_per_cycle = cfg.get("telegram", {}).get("max_offers_per_cycle", 20)
    real_discount_min = filters_cfg.get("real_discount_min", 10.0)
    min_observations = filters_cfg.get("min_observations", 2)
    price_error_threshold = speed_cfg.get("price_error_threshold", 50.0)
    min_savings = filters_cfg.get("min_savings", 80.0)
    max_concurrent_stores = speed_cfg.get("max_concurrent_stores", 4)
    max_concurrent_urls = speed_cfg.get("max_concurrent_urls_per_store", 3)

    if store_name:
        store_configs = [s for s in store_configs if s.name == store_name]

    # --- Limpieza periódica de datos antiguos ---
    if not store_name:  # Solo en ciclos completos, no por tienda individual
        db.cleanup_old_data(days=90)

    # --- Scraping paralelo de tiendas ---
    cycle_start = time.monotonic()
    store_semaphore = asyncio.Semaphore(max_concurrent_stores)
    stores_failed = 0

    async def _scrape_store(sc):
        nonlocal stores_failed
        async with store_semaphore:
            # Use system Chrome for stores that need it (e.g. Akamai TLS fingerprinting)
            effective_browser = browser_client
            own_browser = None
            if sc.use_system_chrome:
                own_browser = BrowserClient(
                    delay_min=anti_ban["delay_min"],
                    delay_max=anti_ban["delay_max"],
                    max_requests_per_minute=anti_ban["max_requests_per_minute"],
                    proxy_url=sc.proxy_url or anti_ban["proxy_url"],
                    speed_mode=speed_mode,
                    channel="chrome",
                )
                effective_browser = own_browser

            store_cls = STORE_REGISTRY.get(sc.name, DEFAULT_STORE_CLASS)
            store = store_cls(
                config=sc,
                http_client=http_client,
                browser_client=effective_browser,
                max_concurrent_urls=max_concurrent_urls,
            )
            logger.info("=== Scrapeando %s ===", sc.name)
            try:
                deals = await store.scrape()
                logger.info("[%s] %d ofertas crudas encontradas", sc.name, len(deals))
                if not deals:
                    stores_failed += 1
                return deals
            except Exception:
                logger.exception("Error scrapeando %s", sc.name)
                stores_failed += 1
                return []
            finally:
                if own_browser:
                    await own_browser.close()

    results = await asyncio.gather(*[_scrape_store(sc) for sc in store_configs])

    # Liberar contextos del browser solo en ciclos completos (no por tienda individual)
    # En ciclos individuales del scheduler, no limpiar — otra tienda puede estar usándolos
    if not store_name:
        await browser_client.cleanup_contexts()

    # --- DB upserts secuenciales (SQLite single-writer) ---
    all_raw_deals: list = []
    for deals in results:
        new_count = 0
        for deal in deals:
            deal_id, is_new = db.upsert_deal(deal)
            deal.id = deal_id
            if is_new:
                new_count += 1
        if new_count and deals:
            logger.info("[%s] %d productos nuevos registrados", deals[0].store, new_count)
        all_raw_deals.extend(deals)

    # --- Contador global de alertas (respetar max_per_cycle para TODAS) ---
    deals_sent = 0

    # --- Bajadas de precio significativas ---
    price_drop_deals = detect_price_drops(all_raw_deals, db=db)
    for deal in price_drop_deals:
        if deals_sent >= max_per_cycle:
            logger.info("Límite de alertas alcanzado (%d), parando envíos", max_per_cycle)
            break
        if deal.id is not None and db.is_sent(deal.id):
            continue
        await telegram_bot.send_deal_immediate(deal)
        if deal.id is not None:
            db.mark_sent([deal.id])
        deals_sent += 1

    # --- Watchlist: alertar productos vigilados a buen precio ---
    watchlist_cfg = cfg.get("watchlist", {})
    watchlist_deals: list = []
    if watchlist_cfg.get("enabled", False):
        min_discount = filters_cfg.get("min_discount", 45.0)
        watchlist_deals = check_watchlist(
            all_raw_deals, watchlist_cfg, min_discount=min_discount, db=db,
            min_observations=min_observations, price_error_threshold=price_error_threshold,
        )
        for deal in watchlist_deals:
            if deals_sent >= max_per_cycle:
                logger.info("Límite de alertas alcanzado (%d), parando envíos", max_per_cycle)
                break
            # No reenviar deals ya enviados (evita duplicados entre ciclos)
            if deal.id is not None and db.is_sent(deal.id):
                continue
            await telegram_bot.send_deal_immediate(deal)
            if deal.id is not None:
                db.mark_sent([deal.id])
            deals_sent += 1

    # --- Detección de precios absurdamente bajos — DESACTIVADO ---
    # Genera demasiados falsos positivos: asigna precio original sintético
    # basado en percentiles de la tienda (P5), lo cual no refleja el valor
    # real del producto. Ejemplo: SSD de 48€ en Coolmod → "original 200€".
    # Las ofertas reales se detectan por verify_real_deals y check_watchlist.

    # --- Filtros básicos ---
    all_deals = apply_filters(all_raw_deals, filters_cfg)

    if not all_deals:
        logger.info("No hay ofertas que pasen los filtros básicos")
        # --- Resumen del ciclo ---
        if not store_name:
            duration = time.monotonic() - cycle_start
            await telegram_bot.send_cycle_summary(
                stores_scraped=len(store_configs), stores_failed=stores_failed,
                total_deals=len(all_raw_deals), deals_sent=deals_sent,
                duration_secs=duration,
            )
        return

    # --- Validación de precios de mercado ---
    market_cfg = cfg.get("market_price", {})
    if market_cfg.get("enabled", True):
        checker = MarketPriceChecker(
            db=db, browser_client=browser_client,
            max_idealo_per_cycle=market_cfg.get("max_idealo_per_cycle", 10),
            cache_ttl_days=market_cfg.get("cache_ttl_days", 7),
            cross_store_fuzzy_threshold=market_cfg.get("cross_store_fuzzy_threshold", 80),
        )
        await checker.enrich_deals(all_deals)

        # Reemplazar original_price inflado con market_price
        for deal in all_deals:
            if deal.market_price and deal.original_price:
                if deal.original_price > deal.market_price * 1.3:
                    logger.info(
                        "MARKET FIX: %s — original %.2f€ -> market %.2f€ (store: %s)",
                        deal.title[:50], deal.original_price,
                        deal.market_price, deal.store,
                    )
                    deal.original_price = deal.market_price
                    deal.discount_pct = calculate_discount(
                        deal.current_price, deal.market_price,
                    )

    # --- Verificación anti-fake (con bypass para errores de precio) ---
    verified = verify_real_deals(
        all_deals,
        db=db,
        min_observations=min_observations,
        real_discount_min=real_discount_min,
        price_error_threshold=price_error_threshold,
        min_savings=min_savings,
    )

    # --- Alertas inmediatas para errores de precio (confirmados y sin confirmar) ---
    _error_tiers = ("ERROR_DE_PRECIO", "ERROR_NO_CONFIRMADO")
    price_errors = [d for d in verified if d.alert_tier in _error_tiers]
    normal_deals = [d for d in verified if d.alert_tier not in _error_tiers]

    for deal in price_errors:
        if deals_sent >= max_per_cycle:
            logger.info("Límite de alertas alcanzado (%d), parando envíos", max_per_cycle)
            break
        if deal.id is not None and db.is_sent(deal.id):
            continue
        await telegram_bot.send_deal_immediate(deal)
        if deal.id is not None:
            db.mark_sent([deal.id])
        deals_sent += 1

    # --- Cross-store: comparar precios entre tiendas ---
    cross_store_cfg = cfg.get("cross_store", {})
    if cross_store_cfg.get("enabled", False):
        pairs = detect_cross_store_bargains(db, hours=24)
        _inflated_stores = {"amazon", "aliexpress", "ebay", "miravia", "lifeinformatica"}
        for cheap, expensive in pairs:
            if deals_sent >= max_per_cycle:
                break
            if expensive.store in _inflated_stores:
                continue
            if cheap.id is not None and db.is_sent(cheap.id):
                continue
            await telegram_bot.send_cross_store_deal(cheap, expensive)
            if cheap.id is not None:
                db.mark_sent([cheap.id])
            deals_sent += 1

    # Persistir descuento real sin resetear sent_to_telegram de deals ya enviados
    for deal in normal_deals:
        db.update_verified_deal(deal)

    # --- Enviar resto de ofertas verificadas en batch ---
    unsent = db.get_unsent_deals(limit=max_per_cycle)
    verified_urls = {d.url for d in normal_deals}
    unsent_verified = [d for d in unsent if d.url in verified_urls]

    if unsent_verified:
        logger.info("Enviando %d ofertas VERIFICADAS a Telegram...", len(unsent_verified))
        sent_ids = await telegram_bot.send_deals(unsent_verified, max_per_cycle=max_per_cycle)
        db.mark_sent(sent_ids)
        deals_sent += len(sent_ids)
    else:
        logger.info("No hay ofertas verificadas para enviar (se necesita más historial)")

    # --- Resumen del ciclo (solo en ciclos completos) ---
    if not store_name:
        duration = time.monotonic() - cycle_start
        await telegram_bot.send_cycle_summary(
            stores_scraped=len(store_configs), stores_failed=stores_failed,
            total_deals=len(all_raw_deals), deals_sent=deals_sent,
            duration_secs=duration,
        )


@click.command()
@click.option("--once", is_flag=True, help="Ejecutar un solo ciclo y salir")
@click.option("--daemon", is_flag=True, help="Ejecutar en modo daemon con scheduler")
@click.option("--config", "config_path", default=None, help="Ruta al config.yaml")
@click.option("--verbose", "-v", is_flag=True, help="Logging detallado (DEBUG)")
@click.option("--dashboard", is_flag=True, help="Arrancar dashboard web")
@click.option("--dashboard-port", default=8080, help="Puerto del dashboard")
def main(once: bool, daemon: bool, config_path: str | None, verbose: bool,
         dashboard: bool, dashboard_port: int) -> None:
    """Deals Scraper — Busca ofertas y envía alertas a Telegram."""
    _setup_logging(verbose)

    if not once and not daemon:
        once = True  # Por defecto, ejecución única

    try:
        cfg = load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Error de configuración: %s", exc)
        sys.exit(1)

    anti_ban = get_anti_ban(cfg)
    speed_cfg = cfg.get("speed", {})
    speed_mode = speed_cfg.get("mode", "stealth") == "fast"

    db = Database()
    http_client = HttpClient(
        delay_min=anti_ban["delay_min"],
        delay_max=anti_ban["delay_max"],
        max_requests_per_minute=anti_ban["max_requests_per_minute"],
        proxy_url=anti_ban["proxy_url"],
    )
    browser_client = BrowserClient(
        delay_min=anti_ban["delay_min"],
        delay_max=anti_ban["delay_max"],
        max_requests_per_minute=anti_ban["max_requests_per_minute"],
        proxy_url=anti_ban["proxy_url"],
        speed_mode=speed_mode,
    )
    _tg = cfg["telegram"]
    telegram_bot = TelegramBot(
        bot_token=_tg["bot_token"],
        chat_id=str(_tg["chat_id"]),
        db=db,
        chat_id_errores=str(_tg["chat_id_errores"]) if _tg.get("chat_id_errores") else None,
        chat_id_chollos=str(_tg["chat_id_chollos"]) if _tg.get("chat_id_chollos") else None,
    )

    if dashboard:
        logger.info("Modo: dashboard web en puerto %d", dashboard_port)
        from deals_scraper.dashboard import create_app
        import uvicorn
        app = create_app(db.db_path)
        uvicorn.run(app, host="0.0.0.0", port=dashboard_port)
        return

    if once:
        logger.info("Modo: ejecución única")
        asyncio.run(_run_once(cfg, db, http_client, browser_client, telegram_bot))
    elif daemon:
        logger.info("Modo: daemon")
        asyncio.run(_run_daemon(cfg, db, http_client, browser_client, telegram_bot))


async def _run_once(cfg, db, http_client, browser_client, telegram_bot) -> None:
    try:
        await telegram_bot.validate()
        await run_cycle(cfg, db, http_client, browser_client, telegram_bot)
    finally:
        await http_client.close()
        await browser_client.close()
        db.close()


async def _run_single_store(
    cfg, db, http_client, browser_client, telegram_bot, target_store: str,
) -> None:
    """Wrapper para el scheduler: ejecuta un ciclo para una sola tienda."""
    await run_cycle(cfg, db, http_client, browser_client, telegram_bot, store_name=target_store)


async def _run_daemon(cfg, db, http_client, browser_client, telegram_bot) -> None:
    await telegram_bot.validate()

    # Iniciar polling de Telegram para recibir comandos
    app = telegram_bot.build_application()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram polling iniciado — comandos activos")

    scheduler = Scheduler()
    store_configs = get_store_configs(cfg)

    for sc in store_configs:
        scheduler.add_store_job(
            store_name=sc.name,
            func=_run_single_store,
            interval_minutes=sc.interval_minutes,
            cfg=cfg,
            db=db,
            http_client=http_client,
            browser_client=browser_client,
            telegram_bot=telegram_bot,
            target_store=sc.name,
        )

    # Ejecutar un ciclo inicial inmediato
    logger.info("Ejecutando ciclo inicial...")
    await run_cycle(cfg, db, http_client, browser_client, telegram_bot)

    # Mantener vivo con scheduler
    # Signal handler para shutdown limpio
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(_shutdown(
            scheduler, app, http_client, browser_client, db,
        )))

    try:
        await scheduler.run_forever()
    finally:
        await _shutdown(scheduler, app, http_client, browser_client, db)


async def _shutdown(scheduler, app, http_client, browser_client, db) -> None:
    """Shutdown limpio: cierra todos los recursos y mata procesos Chrome."""
    logger.info("Shutdown iniciado — cerrando recursos...")
    try:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
    except Exception:
        pass
    try:
        await http_client.close()
    except Exception:
        pass
    try:
        await browser_client.close()
    except Exception:
        pass
    try:
        db.close()
    except Exception:
        pass
    _cleanup_chrome_processes()
    logger.info("Shutdown completo")
    sys.exit(0)


if __name__ == "__main__":
    main()
