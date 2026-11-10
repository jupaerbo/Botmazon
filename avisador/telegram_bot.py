"""Bot interactivo de Telegram (python-telegram-bot): menú por secciones con Inline Keyboards.

Solo atiende a los chats de TELEGRAM_CHAT_ID (el bot es privado).
"""
from __future__ import annotations

import logging
from typing import Protocol

from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions,
                      Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from .active import ActiveDeals
from .views import Rows, render_menu, render_section, render_top

log = logging.getLogger(__name__)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class ScanController(Protocol):
    def request_scan(self, chat_id: int) -> bool: ...


def _markup(rows: Rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=c) for t, c in row] for row in rows])


class MenuBot:
    def __init__(self, token: str, chat_ids: list[str], store: ActiveDeals, scanner: ScanController,
                 page_size: int = 5, top_percent: float = 40, welcome_on_start: bool = True) -> None:
        self.token = token
        self.chat_ids = {int(c) for c in chat_ids if c.lstrip("-").isdigit()}
        self.store = store
        self.scanner = scanner
        self.page_size = page_size
        self.top_percent = top_percent
        self.welcome_on_start = welcome_on_start

    # ------------------------------------------------------------ montaje

    def build(self, with_post_init: bool = True) -> Application:
        """with_post_init=False cuando el ciclo de vida lo gestiona otro (FastAPI): entonces hay
        que llamar a `post_init(app)` manualmente tras `app.start()`."""
        builder = Application.builder().token(self.token)
        if with_post_init:
            builder = builder.post_init(self.post_init)
        app = builder.build()
        allowed = filters.Chat(chat_id=list(self.chat_ids))
        app.add_handler(CommandHandler(["start", "ofertas", "menu"], self._cmd_menu, filters=allowed))
        app.add_handler(CallbackQueryHandler(self._on_button))
        app.add_error_handler(self._on_error)
        return app

    async def post_init(self, app: Application) -> None:
        await app.bot.set_my_commands([
            BotCommand("ofertas", "Menú de ofertas por secciones"),
            BotCommand("start", "Mostrar el menú principal"),
        ])
        if self.welcome_on_start:
            text, rows = render_menu(self.store.snapshot(), self.top_percent,
                                     note="👋 Bot iniciado. Te avisaré al instante si detecto un 🚨 error de precio.")
            for chat_id in self.chat_ids:
                try:
                    await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                               reply_markup=_markup(rows), link_preview_options=NO_PREVIEW)
                except Exception as e:  # noqa: BLE001
                    log.warning("No se pudo enviar el menú inicial a %s: %s", chat_id, e)

    # ------------------------------------------------------------ handlers

    async def _cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text, rows = render_menu(self.store.snapshot(), self.top_percent)
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML,
                                                  reply_markup=_markup(rows), link_preview_options=NO_PREVIEW)

    async def _on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query.message is None or query.message.chat.id not in self.chat_ids:
            await query.answer("No autorizado", show_alert=True)
            return
        data = query.data or ""
        snap = self.store.snapshot()
        toast = None

        if data == "x":
            await query.answer()
            return
        if data == "r":
            started = self.scanner.request_scan(query.message.chat.id)
            toast = ("🔄 Escaneo lanzado. Te aviso al terminar (3-6 min)." if started
                     else "⏳ Ya hay un escaneo en curso. Te aviso al terminar.")
            snap["scanning"] = True
            text, rows = render_menu(snap, self.top_percent)
        elif data.startswith("s:"):
            _, niche_id, page = data.split(":", 2)
            text, rows = render_section(snap, niche_id, int(page), self.page_size)
        elif data.startswith("t:"):
            text, rows = render_top(snap, int(data[2:]), self.page_size, self.top_percent)
        else:  # "m" o desconocido -> menú principal
            text, rows = render_menu(snap, self.top_percent)

        await query.answer(toast, show_alert=False)
        try:
            # Se edita el mismo mensaje: el chat no se llena de mensajes al navegar.
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_markup(rows),
                                          link_preview_options=NO_PREVIEW)
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            if "can't be edited" in str(e).lower() or "not found" in str(e).lower():
                await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_markup(rows),
                                               link_preview_options=NO_PREVIEW)
                return
            raise

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Error en el bot de Telegram: %s", context.error)
