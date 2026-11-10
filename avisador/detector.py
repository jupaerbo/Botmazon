"""Filtros por nicho y detección de chollos / errores de precio."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .config import Niche, Settings
from .models import Offer
from .storage import PriceStats


@dataclass
class Deal:
    offer: Offer
    niche: Niche
    old_price: float | None
    old_label: str
    discount_percent: float
    is_price_error: bool
    reasons: list[str] = field(default_factory=list)
    logistics: str = ""   # p. ej. "Vendido y enviado por Amazon"


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " " + re.sub(r"[^a-z0-9]+", " ", text).strip() + " "


def matches_niche(offer: Offer, niche: Niche) -> bool:
    """Filtro por palabras clave y rango de precio."""
    if offer.price is None:
        return False
    if offer.price < niche.min_price or (niche.max_price and offer.price > niche.max_price):
        return False
    title = _norm(offer.title)
    if niche.include_keywords and not any(_norm(k) in title for k in niche.include_keywords):
        return False
    if any(_norm(k) in title for k in niche.exclude_keywords):
        return False
    return True


def _pct(old: float, new: float) -> float:
    return (old - new) / old * 100


def evaluate(offer: Offer, niche: Niche, settings: Settings, stats: PriceStats) -> Deal | None:
    """Devuelve un Deal si el descuento supera el mínimo del nicho (o el precio objetivo).

    Referencias que se comparan con el precio actual:
      - Precio recomendado/tachado que muestra Amazon.
      - Mediana de los precios que hemos registrado (si hay muestras suficientes).
      - Último precio registrado (bajada de golpe).
    """
    if not offer.available or offer.price is None:
        return None
    price = offer.price

    refs: list[tuple[float, str]] = []
    if offer.list_price and offer.list_price > price:
        refs.append((offer.list_price, "Precio recomendado/anterior"))
    if stats.median and stats.samples >= settings.min_history_samples and stats.median > price:
        refs.append((stats.median, f"Precio medio ({stats.samples} lecturas)"))
    if stats.last and stats.last > price:
        refs.append((stats.last, "Precio en la revisión anterior"))

    best = max(refs, key=lambda r: _pct(r[0], price), default=None)
    discount = _pct(best[0], price) if best else 0.0

    reasons: list[str] = []
    if discount >= niche.min_drop_percent:
        reasons.append(f"Descuento ≥ {niche.min_drop_percent:.0f}% (mínimo del nicho)")
    if offer.target_price is not None and price <= offer.target_price:
        reasons.append(f"Por debajo de tu precio objetivo ({offer.target_price:.2f})")
    if not reasons:
        return None

    return Deal(
        offer=offer,
        niche=niche,
        old_price=best[0] if best else None,
        old_label=best[1] if best else "",
        discount_percent=discount,
        is_price_error=discount >= niche.price_error_percent,
        reasons=reasons,
    )


# ---------------------------------------------------------------- vendedor / logística

# IDs de vendedor de Amazon (merchant ID). amazon.es verificado: A1AT7YVPFBWXBL.
AMAZON_MERCHANT_IDS = {"A1AT7YVPFBWXBL", "ATVPDKIKX0DER"}


def _is_amazon(name: str | None) -> bool:
    return bool(name) and _norm(name).strip().startswith("amazon")


def classify_seller(offer: Offer) -> tuple[bool, str]:
    """Acepta solo si Amazon gestiona el envío:
       a) Vendido y enviado por Amazon.
       b) Vendido por un tercero y enviado por Amazon (FBA).
    Rechaza "vendido y enviado por un tercero" y los casos sin información."""
    sold, ships = offer.sold_by, offer.ships_from
    amazon_sold = _is_amazon(sold) or (offer.merchant_id in AMAZON_MERCHANT_IDS)
    amazon_ships = _is_amazon(ships)
    if not sold and not ships:
        return False, "sin información de vendedor (sin buy box)"
    if amazon_sold and (amazon_ships or not ships):
        return True, "Vendido y enviado por Amazon"
    if amazon_ships:
        return True, f"Enviado por Amazon · Vendido por {sold} (FBA)"
    if sold == ships:
        return False, f"Vendido y enviado por {sold}"
    return False, f"Vendido por {sold or '?'} y enviado por {ships or '?'}"
