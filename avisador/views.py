"""Textos y teclados del menú de Telegram (funciones puras, sin dependencias de red).

Los teclados se devuelven como filas de (texto, callback_data) para poder convertirlos
tanto a objetos de python-telegram-bot como al JSON de la Bot API.

callback_data:
    m            menú principal
    s:<id>:<p>   sección de un nicho, página p
    t:<p>        "Ver todo" (top), página p
    r            actualizar escaneo
    x            no hace nada (indicador de página)
"""
from __future__ import annotations

import html
import time
from datetime import datetime

from .notifier import fmt_price

Rows = list[list[tuple[str, str]]]


def _ago(ts: int | None) -> str:
    if not ts:
        return "nunca"
    mins = int((time.time() - ts) // 60)
    when = datetime.fromtimestamp(ts).strftime("%H:%M")
    if mins < 1:
        return f"{when} (ahora mismo)"
    if mins < 60:
        return f"{when} (hace {mins} min)"
    return f"{datetime.fromtimestamp(ts).strftime('%d/%m %H:%M')} (hace {mins // 60} h)"


def _menu_label(n: dict) -> str:
    return n.get("menu_label") or n.get("label") or n["id"]


def top_deals(snap: dict, top_percent: float) -> list[dict]:
    all_deals = [d for lst in snap.get("deals", {}).values() for d in lst]
    top = [d for d in all_deals if d["discount"] >= top_percent]
    return sorted(top, key=lambda d: (d["is_error"], d["discount"]), reverse=True)


def render_menu(snap: dict, top_percent: float, note: str = "") -> tuple[str, Rows]:
    deals = snap.get("deals", {})
    lines = ["🛒 <b>Avisador de chollos Amazon</b>", ""]
    status = "🔄 <i>Escaneando ahora…</i>" if snap.get("scanning") else ""
    lines.append(f"🕒 Último escaneo: {_ago(snap.get('updated'))} {status}".rstrip())
    lines.append("")
    rows: Rows = []
    for n in snap.get("niches", []):
        count = len(deals.get(n["id"], []))
        errors = sum(d["is_error"] for d in deals.get(n["id"], []))
        extra = f" · 🚨 {errors}" if errors else ""
        lines.append(f"{html.escape(_menu_label(n))}: <b>{count}</b>{extra}")
        rows.append([(f"{_menu_label(n)} ({count})", f"s:{n['id']}:0")])
    top = top_deals(snap, top_percent)
    lines.append(f"🔥 Top &gt; {top_percent:.0f}%: <b>{len(top)}</b>")
    rows.append([(f"🔥 Ver Todo (Chollos Top > {top_percent:.0f}%) ({len(top)})", "t:0")])
    rows.append([("🔄 Actualizar Escaneo", "r")])
    if note:
        lines += ["", note]
    lines += ["", "Elige una sección:"]
    return "\n".join(lines), rows


def _render_item(i: int, d: dict, show_niche: str | None = None) -> str:
    cur = d.get("currency", "€")
    title = html.escape(d["title"][:90] + ("…" if len(d["title"]) > 90 else ""))
    head = "🚨 " if d["is_error"] else ""
    lines = [f"<b>{i}.</b> {head}<b>{title}</b>"]
    if d.get("old_price"):
        lines.append(f"    <s>{fmt_price(d['old_price'], cur)}</s> → <b>{fmt_price(d['price'], cur)}</b>"
                     f"  (<b>-{d['discount']:.0f}%</b>)")
    else:
        lines.append(f"    <b>{fmt_price(d['price'], cur)}</b>")
    meta = []
    if show_niche:
        meta.append(html.escape(show_niche))
    if d.get("logistics"):
        meta.append(f"🚚 {html.escape(d['logistics'])}")
    if meta:
        lines.append("    " + " · ".join(meta))
    lines.append(f'    🛒 <a href="{html.escape(d["url"], quote=True)}">Ver en Amazon</a>')
    return "\n".join(lines)


def _paginate(items: list[dict], page: int, size: int) -> tuple[list[dict], int, int]:
    pages = max(1, -(-len(items) // size))
    page = min(max(0, page), pages - 1)
    return items[page * size:(page + 1) * size], page, pages


def _nav(prefix: str, page: int, pages: int) -> Rows:
    nav = []
    if page > 0:
        nav.append(("◀️ Anterior", f"{prefix}:{page - 1}"))
    nav.append((f"{page + 1}/{pages}", "x"))
    if page < pages - 1:
        nav.append(("Siguiente ▶️", f"{prefix}:{page + 1}"))
    return [nav, [("🏠 Menú Principal", "m")]]


def render_section(snap: dict, niche_id: str, page: int, size: int) -> tuple[str, Rows]:
    niche = next((n for n in snap.get("niches", []) if n["id"] == niche_id), None)
    if niche is None:
        return "Esa sección ya no existe.", [[("🏠 Menú Principal", "m")]]
    items = snap.get("deals", {}).get(niche_id, [])
    return _render_list(_menu_label(niche), items, f"s:{niche_id}", page, size, snap, None)


def render_top(snap: dict, page: int, size: int, top_percent: float) -> tuple[str, Rows]:
    labels = {n["id"]: n.get("label", n["id"]) for n in snap.get("niches", [])}
    items = top_deals(snap, top_percent)
    return _render_list(f"🔥 Chollos Top > {top_percent:.0f}%", items, "t", page, size, snap, labels)


def _render_list(title: str, items: list[dict], prefix: str, page: int, size: int,
                 snap: dict, niche_labels: dict | None) -> tuple[str, Rows]:
    chunk, page, pages = _paginate(items, page, size)
    lines = [f"<b>{html.escape(title)}</b>",
             f"{len(items)} oferta(s) activas · escaneo {_ago(snap.get('updated'))}", ""]
    if not items:
        lines.append("No hay ofertas activas en esta sección ahora mismo. 😴")
    for i, d in enumerate(chunk, start=page * size + 1):
        label = niche_labels.get(d["niche"]) if niche_labels else None
        lines += [_render_item(i, d, label), ""]
    return "\n".join(lines).rstrip(), _nav(prefix, page, pages)


def to_api_markup(rows: Rows) -> dict:
    """Teclado en formato JSON de la Bot API (para envíos con requests)."""
    return {"inline_keyboard": [[{"text": t, "callback_data": c} for t, c in row] for row in rows]}
