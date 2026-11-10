"""Envío de notificaciones a Telegram (Bot API)."""
from __future__ import annotations

import html
import logging
import time
from urllib.parse import urlencode, urlparse, urlunparse

import requests

from .detector import Deal

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


def fmt_price(value: float, currency: str = "€") -> str:
    if currency == "€":
        s = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"{s} €"
    return f"{currency}{value:,.2f}"


def with_affiliate(url: str, tag: str | None) -> str:
    if not tag:
        return url
    return urlunparse(urlparse(url)._replace(query=urlencode({"tag": tag})))


def format_deal(deal: Deal, affiliate_tag: str | None = None) -> str:
    o, cur = deal.offer, deal.offer.currency
    link = html.escape(with_affiliate(o.url, affiliate_tag), quote=True)
    lines = [f"<b>[{html.escape(deal.niche.label)}]</b>"]
    if deal.is_price_error:
        lines.append("🚨 <b>¡POSIBLE ERROR DE PRECIO!</b> 🚨")
    else:
        lines.append("🔥 <b>¡CHOLLO DETECTADO!</b> 🔥")
    lines += ["", f"📦 <b>{html.escape(o.title[:300])}</b>", ""]
    if deal.old_price:
        lines += [
            f"💸 Antes: <s>{fmt_price(deal.old_price, cur)}</s> <i>({html.escape(deal.old_label)})</i>",
            f"✅ Ahora: <b>{fmt_price(o.price, cur)}</b>",
            f"📉 Descuento: <b>-{deal.discount_percent:.0f}%</b> "
            f"(ahorras {fmt_price(deal.old_price - o.price, cur)})",
        ]
    else:
        lines.append(f"✅ Precio: <b>{fmt_price(o.price, cur)}</b>")
    if deal.logistics:
        lines.append(f"🚚 {html.escape(deal.logistics)}")
    if deal.niche.menu_label:
        lines.append(f"📂 Sección: {html.escape(deal.niche.menu_label)}")
    lines += [
        "",
        *(f"• {html.escape(r)}" for r in deal.reasons),
        f"📍 Fuente: {html.escape(o.source)}",
        "",
        f'🛒 <a href="{link}">Ver en Amazon</a>',
    ]
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, token: str, chat_ids: list[str], timeout: float = 15) -> None:
        self.token = token
        self.chat_ids = chat_ids
        self.timeout = timeout

    def _call(self, method: str, payload: dict | None = None) -> dict:
        resp = requests.post(API.format(token=self.token, method=method),
                             json=payload or {}, timeout=self.timeout)
        try:
            return resp.json()
        except ValueError:
            return {"ok": False, "description": f"HTTP {resp.status_code}"}

    def get_me(self) -> dict:
        return self._call("getMe")

    def get_updates(self) -> dict:
        return self._call("getUpdates")

    def send(self, text: str, reply_markup: dict | None = None, chat_ids: list | None = None,
             preview: bool = True) -> bool:
        """Envía a los chats indicados (por defecto, todos). Nunca lanza excepciones;
        True si algún chat lo recibió."""
        any_ok = False
        for chat_id in (chat_ids if chat_ids is not None else self.chat_ids):
            payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                       "link_preview_options": {"is_disabled": not preview}}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            for attempt in range(3):
                try:
                    r = self._call("sendMessage", payload)
                except requests.RequestException as e:
                    log.warning("Error de red con Telegram (%s). Reintento...", e)
                    time.sleep(5 * (attempt + 1))
                    continue
                if r.get("ok"):
                    any_ok = True
                    break
                retry_after = (r.get("parameters") or {}).get("retry_after")
                if retry_after:
                    log.warning("Telegram pide esperar %s s (rate limit).", retry_after)
                    time.sleep(int(retry_after) + 1)
                    continue
                log.error("Telegram rechazó el mensaje para %s: %s", chat_id, r.get("description"))
                break
        time.sleep(1.1)  # límite de Telegram: ~1 mensaje/segundo por chat
        return any_ok
