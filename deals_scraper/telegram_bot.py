"""Envío de mensajes a Telegram + comandos /start /status /top."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from .database import Database
from .models import Deal

logger = logging.getLogger(__name__)

# Telegram API rate limit: ~30 msgs/segundo a un chat
_TELEGRAM_MSG_DELAY = 0.05  # 50ms entre mensajes (≈20/s, conservador)


class TelegramBot:
    """Gestiona el envío de ofertas y comandos del bot de Telegram."""

    def __init__(self, bot_token: str, chat_id: str, db: Database) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.db = db
        self._bot = Bot(token=bot_token)

    async def validate(self) -> None:
        """Valida token y chat_id al arrancar. Falla rápido si son inválidos."""
        try:
            me = await self._bot.get_me()
            logger.info("Telegram bot conectado: @%s (%s)", me.username, me.first_name)
        except Exception as exc:
            raise RuntimeError(f"Token de Telegram inválido: {exc}") from exc

    # ------------------------------------------------------------------
    # Envío de ofertas
    # ------------------------------------------------------------------
    async def send_deals(self, deals: list[Deal], max_per_cycle: int = 20) -> list[int]:
        """Envía ofertas a Telegram. Devuelve los IDs de las ofertas enviadas."""
        sent_ids: list[int] = []
        to_send = deals[:max_per_cycle]

        for deal in to_send:
            try:
                await self._send_deal(deal)
                if deal.id is not None:
                    sent_ids.append(deal.id)
                await asyncio.sleep(_TELEGRAM_MSG_DELAY)
            except Exception:
                logger.exception("Error enviando oferta a Telegram: %s", deal.title)

        logger.info("Enviadas %d/%d ofertas a Telegram", len(sent_ids), len(deals))
        return sent_ids

    async def send_deal_immediate(self, deal: Deal) -> None:
        """Envía un deal inmediatamente (para errores de precio / chollos urgentes)."""
        try:
            await self._send_deal(deal)
            logger.info("Alerta inmediata enviada: [%s] %s", deal.alert_tier, deal.title[:50])
        except Exception:
            logger.exception("Error enviando alerta inmediata: %s", deal.title)

    async def _send_deal(self, deal: Deal) -> None:
        """Envía una oferta individual con formato HTML."""
        text = self._format_deal(deal)

        # Errores de precio: text-only para máxima velocidad
        if deal.alert_tier == "ERROR_DE_PRECIO":
            await self._bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return

        if deal.image_url:
            try:
                await self._bot.send_photo(
                    chat_id=self.chat_id,
                    photo=deal.image_url,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                )
                return
            except Exception:
                # Si falla la imagen, enviar solo texto
                logger.debug("No se pudo enviar imagen, enviando solo texto")

        await self._bot.send_message(
            chat_id=self.chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )

    @staticmethod
    def _get_reference_price(deal: Deal) -> float | None:
        """Obtiene el mejor precio de referencia disponible para un deal."""
        if deal.market_price and deal.market_price > deal.current_price:
            return deal.market_price
        if deal.original_price and deal.original_price > deal.current_price:
            return deal.original_price
        return None

    @staticmethod
    def _format_deal(deal: Deal) -> str:
        """Formatea una oferta en HTML para Telegram según su alert_tier."""
        tier = getattr(deal, "alert_tier", "NORMAL")

        if tier == "ERROR_DE_PRECIO":
            return TelegramBot._format_price_error(deal)
        if tier == "CHOLLO":
            return TelegramBot._format_chollo(deal)
        return TelegramBot._format_normal(deal)

    @staticmethod
    def _format_price_error(deal: Deal) -> str:
        """Formato urgente para errores de precio."""
        ref_price = TelegramBot._get_reference_price(deal)
        lines = [
            "\U0001f6a8\U0001f6a8\U0001f6a8 <b>ERROR DE PRECIO</b> \U0001f6a8\U0001f6a8\U0001f6a8",
            "",
            f"<b>{_safe_title(deal.title)}</b>",
            "",
        ]
        if ref_price:
            ahorro = ref_price - deal.current_price
            pct = round((1 - deal.current_price / ref_price) * 100)
            label = "Precio de mercado" if deal.market_price else "Antes"
            lines.append(
                f"{label}: <s>{ref_price:.2f}\u20ac</s>\n"
                f"AHORA: <b>{deal.current_price:.2f}\u20ac</b>\n"
                f"Ahorras: <b>{ahorro:.2f}\u20ac (-{pct}%)</b>"
            )
        else:
            lines.append(f"<b>{deal.current_price:.2f}\u20ac</b>")

        if deal.market_price:
            lines.append("\n\U0001f4ca Precio de mercado verificado")

        lines.append(f"\n\U0001f3ea {deal.store.capitalize()}")
        lines.append("")
        lines.append(f'\U0001f6d2 <a href="{deal.url}">COMPRAR AHORA</a>')
        return "\n".join(lines)

    @staticmethod
    def _format_chollo(deal: Deal) -> str:
        """Formato destacado para chollos (30-49% descuento real)."""
        ref_price = TelegramBot._get_reference_price(deal)
        lines = [
            "\U0001f525 <b>CHOLLO</b>",
            "",
            f"<b>{_safe_title(deal.title)}</b>",
            "",
        ]
        if ref_price:
            label = "Precio de mercado" if deal.market_price else "Precio habitual"
            lines.append(
                f"{label}: <s>{ref_price:.2f}\u20ac</s>\n"
                f"Ahora: <b>{deal.current_price:.2f}\u20ac</b>"
            )
        else:
            lines.append(f"<b>{deal.current_price:.2f}\u20ac</b>")

        if deal.discount_pct > 0:
            lines.append(f"\U0001f4c9 Bajada real: <b>-{deal.discount_pct:.0f}%</b>")

        if deal.market_price:
            lines.append("\U0001f4ca Precio de mercado verificado")

        lines.append(f"\U0001f3ea {deal.store.capitalize()}")
        lines.append("")
        lines.append(f'<a href="{deal.url}">Ver oferta</a>')
        return "\n".join(lines)

    @staticmethod
    def _format_normal(deal: Deal) -> str:
        """Formato estándar."""
        ref_price = TelegramBot._get_reference_price(deal)
        lines = []
        lines.append(f"<b>{_safe_title(deal.title)}</b>")
        lines.append("")

        if ref_price:
            label = "Precio de mercado" if deal.market_price else "Precio habitual"
            lines.append(
                f"{label}: <s>{ref_price:.2f}\u20ac</s>\n"
                f"Ahora: <b>{deal.current_price:.2f}\u20ac</b>"
            )
        else:
            lines.append(f"<b>{deal.current_price:.2f}\u20ac</b>")

        if deal.discount_pct > 0:
            lines.append(f"\U0001f4c9 Bajada real: <b>-{deal.discount_pct:.0f}%</b>")

        if deal.market_price:
            lines.append("\U0001f4ca Precio de mercado verificado")

        lines.append(f"\U0001f3ea {deal.store.capitalize()}")
        lines.append("")
        lines.append(f'<a href="{deal.url}">Ver oferta</a>')
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Cross-store: enviar comparación de precios
    # ------------------------------------------------------------------
    async def send_cross_store_deal(self, cheap: Deal, expensive: Deal) -> None:
        """Envía una alerta de comparación cross-store."""
        try:
            diff_pct = round((1 - cheap.current_price / expensive.current_price) * 100)
            text = self._format_cross_store(cheap, expensive, diff_pct)
            await self._bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            logger.info(
                "Cross-store enviado: %s — %s %.2f€ vs %s %.2f€ (-%d%%)",
                cheap.title[:40], cheap.store, cheap.current_price,
                expensive.store, expensive.current_price, diff_pct,
            )
        except Exception:
            logger.exception("Error enviando cross-store: %s", cheap.title)

    @staticmethod
    def _format_cross_store(cheap: Deal, expensive: Deal, diff_pct: int) -> str:
        """Formato para alertas de comparación cross-store."""
        lines = [
            "\U0001f500 <b>MISMO PRODUCTO, MEJOR PRECIO</b>",
            "",
            f"<b>{_safe_title(cheap.title)}</b>",
            "",
            f"En {expensive.store.capitalize()}: {expensive.current_price:.2f}\u20ac",
            f"En {cheap.store.capitalize()}: <b>{cheap.current_price:.2f}\u20ac</b>"
            f" \u2190 {diff_pct}% m\u00e1s barato",
            "",
            f'\U0001f6d2 <a href="{cheap.url}">COMPRAR EN {cheap.store.upper()}</a>',
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Health monitoring: resumen del ciclo
    # ------------------------------------------------------------------
    async def send_cycle_summary(
        self,
        stores_scraped: int,
        stores_failed: int,
        total_deals: int,
        deals_sent: int,
        duration_secs: float,
    ) -> None:
        """Envía un resumen del ciclo de scraping. Alerta si hay problemas."""
        # Solo enviar alerta si hay problemas (0 deals o muchos fallos)
        if total_deals > 0 and stores_failed == 0:
            return  # Ciclo normal, no molestar

        if total_deals == 0:
            icon = "\u26a0\ufe0f"  # ⚠️
            status = "SIN RESULTADOS"
        elif stores_failed > stores_scraped // 2:
            icon = "\u26a0\ufe0f"
            status = "PROBLEMAS"
        else:
            icon = "\u2139\ufe0f"  # ℹ️
            status = "PARCIAL"

        text = (
            f"{icon} <b>Scraper: {status}</b>\n\n"
            f"Tiendas OK: {stores_scraped - stores_failed}/{stores_scraped}\n"
            f"Tiendas con error: {stores_failed}\n"
            f"Deals encontrados: {total_deals}\n"
            f"Alertas enviadas: {deals_sent}\n"
            f"Duración: {duration_secs:.1f}s"
        )

        try:
            await self._bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                disable_notification=True,  # No molestar con sonido
            )
        except Exception:
            logger.exception("Error enviando resumen de ciclo")

    # ------------------------------------------------------------------
    # Comandos del bot (para uso con polling)
    # ------------------------------------------------------------------
    def build_application(self) -> Application:
        """Construye la Application de python-telegram-bot con los handlers."""
        app = Application.builder().token(self.bot_token).build()
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("top", self._cmd_top))
        return app

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler para /start."""
        await update.message.reply_text(
            "👋 <b>Deals Scraper Bot</b>\n\n"
            "Te enviaré las mejores ofertas que encuentre.\n\n"
            "Comandos:\n"
            "/status — Estadísticas\n"
            "/top — Top 10 ofertas",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler para /status — estadísticas de la base de datos."""
        stats = self.db.get_stats()
        await update.message.reply_text(
            f"📊 <b>Estadísticas</b>\n\n"
            f"Ofertas totales: {stats['total_deals']}\n"
            f"Enviadas a Telegram: {stats['sent_to_telegram']}\n"
            f"Tiendas rastreadas: {stats['stores_tracked']}",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_top(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler para /top — últimas 10 mejores ofertas."""
        top = self.db.get_top_deals(limit=10)
        if not top:
            await update.message.reply_text("No hay ofertas registradas aún.")
            return

        lines = ["<b>🏆 Top 10 ofertas</b>\n"]
        for i, deal in enumerate(top, 1):
            lines.append(
                f"{i}. <b>{_escape_html(deal.title[:60])}</b>\n"
                f"   {deal.current_price:.2f}€ (-{deal.discount_pct:.0f}%) "
                f'— <a href="{deal.url}">Ver</a>'
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


_MAX_TITLE_LEN = 200


def _escape_html(text: str) -> str:
    """Escapa caracteres especiales para HTML de Telegram."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _safe_title(title: str) -> str:
    """Trunca y escapa un título para uso seguro en mensajes Telegram."""
    if len(title) > _MAX_TITLE_LEN:
        title = title[:_MAX_TITLE_LEN] + "..."
    return _escape_html(title)
