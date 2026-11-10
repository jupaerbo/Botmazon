"""Análisis del HTML de Amazon: fichas de producto, búsquedas y página de ofertas."""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .models import Offer

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

PRICE_SELECTORS = [
    "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
    "#corePrice_feature_div .a-price .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
    "#apex_desktop .priceToPay .a-offscreen",
    "#apex_desktop .a-price .a-offscreen",
    "#tp_price_block_total_price_ww .a-offscreen",
    "#priceblock_dealprice",
    "#priceblock_ourprice",
    "#priceblock_saleprice",
    "#price_inside_buybox",
    "#newBuyBoxPrice",
    "span.a-price.aok-align-center .a-offscreen",
]
PRICE_CONTAINERS = [
    "#corePriceDisplay_desktop_feature_div .priceToPay",
    "#corePrice_feature_div .a-price",
    "#apex_desktop .a-price",
]
LIST_PRICE_SELECTORS = [
    "#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-text-price[data-a-strike='true'] .a-offscreen",
    "#corePrice_desktop .a-text-price[data-a-strike='true'] .a-offscreen",
    "#corePrice_feature_div .a-text-price .a-offscreen",
    "#apex_desktop .basisPrice .a-offscreen",
    "#priceblock_listprice",
    "#listPrice",
    ".a-price.a-text-price[data-a-strike='true'] .a-offscreen",
]
UNAVAILABLE_WORDS = (
    "no disponible", "agotado", "currently unavailable", "out of stock",
    "non disponible", "nicht verfügbar", "non disponibile",
)
SPONSORED_PREFIXES = ("Anuncio patrocinado:", "Sponsored Ad:", "Sponsored:")


# ---------------------------------------------------------------- utilidades

def extract_asin(url: str) -> str | None:
    m = re.search(r"/(?:dp|gp/product|gp/aw/d|exec/obidos/asin)/([A-Z0-9]{10})", url, re.I)
    return m.group(1).upper() if m else None


def product_url(base_url: str, asin: str) -> str:
    """URL limpia https://www.amazon.xx/dp/ASIN"""
    return f"https://{urlparse(base_url).netloc}/dp/{asin}"


def canonical_url(url: str) -> str:
    asin = extract_asin(url)
    return product_url(url, asin) if asin else url


def currency_for(url: str) -> str:
    host = urlparse(url).netloc
    if host.endswith(".co.uk"):
        return "£"
    if host.endswith(".com"):
        return "$"
    return "€"


def parse_price(text: str | None) -> float | None:
    """Convierte '1.299,99 €', '$1,299.99', '29,99€', '52.59'... en float."""
    if not text:
        return None
    s = re.sub(r"[^\d.,]", "", str(text))
    if not s or not any(c.isdigit() for c in s):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):          # 1.299,99
            s = s.replace(".", "").replace(",", ".")
        else:                                    # 1,299.99
            s = s.replace(",", "")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        head, _, tail = s.rpartition(sep)
        if len(tail) == 3 and head:              # 1.299 -> miles
            s = s.replace(sep, "")
        else:                                    # 29,99 -> decimal
            s = head.replace(sep, "") + "." + tail
    try:
        value = float(s)
    except ValueError:
        return None
    return value if value > 0 else None


def _first_price(node, selectors: list[str]) -> float | None:
    for sel in selectors:
        for el in node.select(sel):
            value = parse_price(el.get_text(" ", strip=True))
            if value:
                return value
    return None


def _clean_list_price(price: float | None, list_price: float | None) -> float | None:
    if price is None or list_price is None or list_price <= price:
        return None
    return list_price


# ---------------------------------------------------------------- vendedor / logística

def _text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def parse_seller(soup: BeautifulSoup) -> tuple[str | None, str | None, str | None]:
    """Devuelve (vendido_por, enviado_por, merchant_id) de la buy box.

    Amazon muestra dos bloques ("Vendido por" / "Envío desde") cuando son distintos,
    y uno solo ("Remitente / Vendedor") cuando el mismo vende y envía.
    """
    def feature(name: str) -> str | None:
        box = soup.select_one(f'[offer-display-feature-name="{name}"] .offer-display-feature-text')
        return _text(box) or None

    sold_by = feature("desktop-merchant-info")
    ships_from = feature("desktop-fulfiller-info")

    if not sold_by:  # maquetación antigua: "Vendido por X y enviado por Amazon."
        text = _text(soup.select_one("#merchant-info") or soup.select_one("#merchantInfoFeature_feature_div"))
        m = re.search(r"vendido por (.+?) y (?:enviado|gestionado) por (.+?)\.?$", text, re.I)
        if m:
            sold_by, ships_from = m.group(1).strip(), m.group(2).strip()
        else:
            m = re.search(r"(?:vendido|remitente\s*/\s*vendedor)\s*(?:por)?\s*(.+)$", text, re.I)
            sold_by = m.group(1).strip() if m else None

    if sold_by and not ships_from:
        ships_from = sold_by  # bloque combinado: el mismo vende y envía

    mid_el = soup.select_one("input#merchantID") or soup.select_one('input[name="merchantID"]')
    merchant_id = (mid_el.get("value") or None) if mid_el else None
    return sold_by, ships_from, merchant_id


# ---------------------------------------------------------------- ficha de producto

def parse_product_page(html: str, url: str) -> Offer | None:
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one("#productTitle") or soup.select_one("#title")
    if not title_el:
        return None
    title = " ".join(title_el.get_text().split())

    price = _first_price(soup, PRICE_SELECTORS)
    if price is None:  # precio partido en whole + fraction
        for sel in PRICE_CONTAINERS:
            el = soup.select_one(sel)
            whole = el.select_one(".a-price-whole") if el else None
            if whole:
                frac = el.select_one(".a-price-fraction")
                price = parse_price(re.sub(r"\D", "", whole.get_text()) + "," +
                                    (re.sub(r"\D", "", frac.get_text()) if frac else "00"))
                if price:
                    break

    avail_el = soup.select_one("#availability") or soup.select_one("#outOfStock")
    avail_text = (avail_el.get_text(" ", strip=True) if avail_el else "").lower()
    available = price is not None and not any(w in avail_text for w in UNAVAILABLE_WORDS)
    sold_by, ships_from, merchant_id = parse_seller(soup)

    return Offer(
        sold_by=sold_by,
        ships_from=ships_from,
        merchant_id=merchant_id,
        asin=extract_asin(url) or url,
        title=title,
        url=canonical_url(url),
        price=price,
        list_price=_clean_list_price(price, _first_price(soup, LIST_PRICE_SELECTORS)),
        currency=currency_for(url),
        available=available,
    )


# ---------------------------------------------------------------- resultados de búsqueda

def parse_search_page(html: str, base_url: str) -> list[Offer]:
    soup = BeautifulSoup(html, "html.parser")
    offers: dict[str, Offer] = {}
    for item in soup.select('div[data-component-type="s-search-result"]'):
        asin = (item.get("data-asin") or "").upper()
        if not ASIN_RE.match(asin) or asin in offers:
            continue
        titles = []
        for h2 in item.select("h2"):
            t = h2.get("aria-label") or h2.get_text(" ", strip=True)
            for prefix in SPONSORED_PREFIXES:
                if t.startswith(prefix):
                    t = t[len(prefix):]
            titles.append(" ".join(t.split()))
        title = max(titles, key=len, default="")
        price = _first_price(item, [".a-price:not(.a-text-price) .a-offscreen"])
        if not title or price is None:
            continue
        list_price = _first_price(item, [
            ".a-price.a-text-price[data-a-strike='true'] .a-offscreen",
            ".a-price.a-text-price .a-offscreen",
        ])
        offers[asin] = Offer(
            asin=asin,
            title=title,
            url=product_url(base_url, asin),
            price=price,
            list_price=_clean_list_price(price, list_price),
            currency=currency_for(base_url),
        )
    return list(offers.values())


def is_search_page(html: str) -> bool:
    return "s-main-slot" in html or "s-result-list" in html


# ---------------------------------------------------------------- página de ofertas (/deals)

def _dig(obj, *keys):
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def parse_deals_page(html: str, base_url: str) -> list[Offer]:
    """La página /deals se pinta con JavaScript, pero el HTML inicial incluye un JSON
    'productSearchResponse' con los productos, su precio de oferta y el recomendado."""
    decoder = json.JSONDecoder()
    offers: dict[str, Offer] = {}
    pos = 0
    while (i := html.find('"productSearchResponse"', pos)) != -1:
        pos = i + 1
        start = html.find("{", i)
        try:
            data, _ = decoder.raw_decode(html, start)
        except ValueError:
            continue
        for p in data.get("products", []) or []:
            asin = (p.get("asin") or "").upper()
            if not ASIN_RE.match(asin) or asin in offers:
                continue
            price = parse_price(_dig(p, "price", "priceToPay", "price"))
            if price is None:
                continue
            state = (_dig(p, "dealDetails", "state") or "AVAILABLE").upper()
            list_price = parse_price(_dig(p, "price", "basisPrice", "price"))
            offers[asin] = Offer(
                asin=asin,
                title=" ".join((p.get("title") or asin).split()),
                url=product_url(base_url, asin),
                price=price,
                list_price=_clean_list_price(price, list_price),
                currency=currency_for(base_url),
                available=state in ("AVAILABLE", "ACTIVE"),
            )
    return list(offers.values())


def is_deals_page(html: str) -> bool:
    return '"productSearchResponse"' in html


def absolute(base_url: str, link: str) -> str:
    return urljoin(base_url, link)
