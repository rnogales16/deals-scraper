"""APScheduler wrapper para modo daemon."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


class Scheduler:
    """Wrapper sobre APScheduler para registrar jobs de scraping por tienda."""

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()

    def add_store_job(
        self,
        store_name: str,
        func: Callable[..., Coroutine],
        interval_minutes: int,
        **kwargs: Any,
    ) -> None:
        """Registra un job periódico para una tienda.

        Args:
            store_name: Nombre de la tienda (usado como job id).
            func: Coroutine a ejecutar periódicamente.
            interval_minutes: Intervalo entre ejecuciones.
            **kwargs: Argumentos extra pasados a func.
        """
        self._scheduler.add_job(
            func,
            trigger=IntervalTrigger(minutes=interval_minutes),
            id=f"store_{store_name}",
            name=f"Scrape {store_name}",
            kwargs=kwargs,
            replace_existing=True,
            max_instances=1,
        )
        logger.info(
            "Job registrado: %s cada %d minutos", store_name, interval_minutes,
        )

    def start(self) -> None:
        """Inicia el scheduler."""
        self._scheduler.start()
        logger.info("Scheduler iniciado con %d jobs", len(self._scheduler.get_jobs()))

    def stop(self) -> None:
        """Detiene el scheduler."""
        self._scheduler.shutdown(wait=False)
        logger.info("Scheduler detenido")

    async def run_forever(self) -> None:
        """Mantiene el proceso vivo mientras el scheduler ejecuta jobs."""
        self.start()
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            self.stop()
