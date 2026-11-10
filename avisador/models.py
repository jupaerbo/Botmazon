"""Modelos de datos compartidos."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Offer:
    """Un producto visto en Amazon (en una búsqueda, en ofertas o en su ficha)."""
    asin: str
    title: str
    url: str
    price: float | None
    list_price: float | None = None   # precio recomendado / tachado / anterior que muestra Amazon
    currency: str = "€"
    available: bool = True
    source: str = ""                  # nombre de la fuente que lo encontró
    niche: str = ""                   # id del nicho (tech, mtg...)
    target_price: float | None = None # solo para fuentes de tipo "product"
    sold_by: str | None = None        # "Vendido por" (solo se conoce desde la ficha)
    ships_from: str | None = None     # "Enviado por"
    merchant_id: str | None = None    # ID del vendedor de la buy box
