"""Carga y validación de config.json (nichos, fuentes y ajustes).

Algunos valores se pueden sobrescribir desde el .env:
    MIN_DROP_<ID_NICHO>      p. ej. MIN_DROP_TECH=30, MIN_DROP_MTG=25
    PRICE_ERROR_<ID_NICHO>   p. ej. PRICE_ERROR_MTG=55
    PRICE_ERROR_PERCENT      umbral global de error de precio
    DEDUP_HOURS              horas sin repetir el mismo ASIN
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)

SOURCE_TYPES = ("deals", "search", "product")
AMAZON_ES_MERCHANT = "A1AT7YVPFBWXBL"  # emi= de Amazon.es (vendido por Amazon), verificado
# IDs incorrectos conocidos que se corrigen automáticamente (A1ATTZ506680M devuelve 0 resultados).
WRONG_MERCHANT_IDS = ("A1ATTZ506680M",)


@dataclass
class Settings:
    amazon_domain: str = "www.amazon.es"
    min_interval_minutes: float = 10
    max_interval_minutes: float = 20
    scan_interval_minutes: float | None = None   # si se define, sustituye a min/max (±10% de jitter)
    delay_between_sources_seconds: tuple[float, float] = (12, 28)
    dedup_hours: float = 36
    renotify_extra_drop_percent: float = 10
    price_error_percent: float = 60
    min_history_samples: int = 3
    history_days: int = 30
    max_alerts_per_cycle: int = 15
    request_timeout_seconds: float = 20
    max_retries: int = 4
    backoff_base_seconds: float = 5
    backoff_max_seconds: float = 120
    delay_between_requests_seconds: tuple[float, float] = (5, 12)
    seller_filter: bool = True
    seller_cache_hours: float = 24
    max_seller_checks_per_cycle: int = 40
    push_mode: str = "errors"          # "errors" (solo errores de precio), "all" o "none"
    top_percent: float = 40
    page_size: int = 5
    active_deal_ttl_hours: float = 2
    welcome_on_start: bool = True
    circuit_breaker_failures: int = 2   # fuentes seguidas bloqueadas que detienen el ciclo
    cooldown_minutes: float = 45        # pausa tras activarse el cortacircuitos


@dataclass
class Source:
    type: str
    url: str
    name: str
    pages: int = 1
    target_price: float | None = None
    amazon_only: bool = False   # la URL ya filtra "vendido por Amazon" (emi=...)


@dataclass
class Niche:
    id: str
    label: str
    min_drop_percent: float
    price_error_percent: float
    menu_label: str = ""
    min_price: float = 0
    max_price: float | None = None
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)


def deals_url(domain: str, departments: list[str]) -> str:
    """URL de la página de ofertas filtrada por departamento(s)."""
    state = json.dumps({"state": {"refinementFilters": {"departments": departments}}, "version": 1},
                       separators=(",", ":"))
    return f"https://{domain}/deals?discounts-widget={quote(json.dumps(state), safe='')}"


def _range(value, default):
    try:
        lo, hi = float(value[0]), float(value[1])
        return (min(lo, hi), max(lo, hi))
    except (TypeError, ValueError, IndexError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("Valor no numérico en %s=%r; se ignora", name, raw)
        return default


def _opt_float(v) -> float | None:
    return float(v) if v not in (None, "") else None


def load_config(path: str | Path) -> tuple[Settings, list[Niche]]:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ValueError(f"No existe {path}") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON inválido en {path}: {e}") from e

    s, d = dict(raw.get("settings", {}) or {}), Settings()
    bs = raw.get("bot_settings", {}) or {}   # bloque de baja carga: tiene prioridad
    if "scan_interval_minutes" in bs:
        s["scan_interval_minutes"] = bs["scan_interval_minutes"]
    if "delay_between_sources_seconds" in bs:
        v = bs["delay_between_sources_seconds"]
        s["delay_between_sources_seconds"] = [v.get("min"), v.get("max")] if isinstance(v, dict) else v
    if "max_retries_per_source" in bs:  # reintentos -> intentos totales = 1 + reintentos
        s["max_retries"] = 1 + max(0, int(bs["max_retries_per_source"]))
    if "backoff_factor_seconds" in bs:
        s["backoff_base_seconds"] = bs["backoff_factor_seconds"]
    settings = Settings(
        amazon_domain=str(s.get("amazon_domain", d.amazon_domain)).replace("https://", "").strip("/"),
        min_interval_minutes=float(s.get("min_interval_minutes", d.min_interval_minutes)),
        max_interval_minutes=float(s.get("max_interval_minutes", d.max_interval_minutes)),
        scan_interval_minutes=_opt_float(s.get("scan_interval_minutes")),
        delay_between_sources_seconds=_range(s.get("delay_between_sources_seconds"),
                                             d.delay_between_sources_seconds),
        dedup_hours=_env_float("DEDUP_HOURS", float(s.get("dedup_hours", d.dedup_hours))),
        renotify_extra_drop_percent=float(s.get("renotify_extra_drop_percent", d.renotify_extra_drop_percent)),
        price_error_percent=_env_float("PRICE_ERROR_PERCENT",
                                       float(s.get("price_error_percent", d.price_error_percent))),
        min_history_samples=int(s.get("min_history_samples", d.min_history_samples)),
        history_days=int(s.get("history_days", d.history_days)),
        max_alerts_per_cycle=int(s.get("max_alerts_per_cycle", d.max_alerts_per_cycle)),
        request_timeout_seconds=float(s.get("request_timeout_seconds", d.request_timeout_seconds)),
        max_retries=max(1, int(s.get("max_retries", d.max_retries))),
        backoff_base_seconds=float(s.get("backoff_base_seconds", d.backoff_base_seconds)),
        backoff_max_seconds=float(s.get("backoff_max_seconds", d.backoff_max_seconds)),
        seller_filter=bool(s.get("seller_filter", d.seller_filter)),
        seller_cache_hours=float(s.get("seller_cache_hours", d.seller_cache_hours)),
        max_seller_checks_per_cycle=int(s.get("max_seller_checks_per_cycle", d.max_seller_checks_per_cycle)),
        push_mode=os.getenv("PUSH_MODE", "").strip().lower() or str(s.get("push_mode", d.push_mode)).lower(),
        top_percent=_env_float("TOP_PERCENT", float(s.get("top_percent", d.top_percent))),
        page_size=min(10, max(1, int(s.get("page_size", d.page_size)))),
        active_deal_ttl_hours=float(s.get("active_deal_ttl_hours", d.active_deal_ttl_hours)),
        welcome_on_start=bool(s.get("welcome_on_start", d.welcome_on_start)),
        circuit_breaker_failures=max(1, int(s.get("circuit_breaker_failures", d.circuit_breaker_failures))),
        cooldown_minutes=float(s.get("cooldown_minutes", d.cooldown_minutes)),
        delay_between_requests_seconds=_range(s.get("delay_between_requests_seconds"),
                                              d.delay_between_requests_seconds),
    )
    if settings.min_interval_minutes > settings.max_interval_minutes:
        settings.min_interval_minutes, settings.max_interval_minutes = (
            settings.max_interval_minutes, settings.min_interval_minutes)

    niches: list[Niche] = []
    for n in raw.get("categories") or raw.get("niches") or []:
        if not n.get("enabled", True):
            continue
        nid = str(n.get("id") or "").strip()
        if not nid:
            log.warning("Nicho sin 'id' ignorado")
            continue
        key = nid.upper()
        niche = Niche(
            id=nid,
            label=n.get("label") or nid.upper(),
            menu_label=n.get("menu_label") or n.get("label") or nid.upper(),
            min_drop_percent=_env_float(f"MIN_DROP_{key}", float(n.get("min_drop_percent", 30))),
            price_error_percent=_env_float(
                f"PRICE_ERROR_{key}",
                float(n.get("price_error_percent", settings.price_error_percent))),
            min_price=float(n.get("min_price", 0) or 0),
            max_price=_opt_float(n.get("max_price")),
            include_keywords=list(n.get("include_keywords", []) or []),
            exclude_keywords=list(n.get("exclude_keywords", []) or []),
        )
        for i, src in enumerate(n.get("sources", []) or []):
            if not src.get("enabled", True):
                continue
            stype = src.get("type")
            if stype not in SOURCE_TYPES:
                log.warning("[%s] fuente #%d ignorada: tipo %r no válido", nid, i + 1, stype)
                continue
            url = (src.get("url") or "").strip()
            if stype == "deals" and not url:
                deps = [str(x) for x in src.get("departments", []) or []]
                url = deals_url(settings.amazon_domain, deps) if deps else f"https://{settings.amazon_domain}/deals"
            if "amazon." not in url:
                log.warning("[%s] fuente #%d ignorada: URL no válida", nid, i + 1)
                continue
            for wrong in WRONG_MERCHANT_IDS:
                if f"emi={wrong}" in url:
                    log.warning("[%s] %s: emi=%s no es Amazon.es (0 resultados); se usa %s",
                                nid, src.get("name"), wrong, AMAZON_ES_MERCHANT)
                    url = url.replace(f"emi={wrong}", f"emi={AMAZON_ES_MERCHANT}")
            if stype == "search" and src.get("amazon_only") and "emi=" not in url:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}emi={src.get('merchant_id', AMAZON_ES_MERCHANT)}"
            amazon_only = f"emi={AMAZON_ES_MERCHANT}" in url
            niche.sources.append(Source(
                type=stype,
                url=url,
                name=src.get("name") or f"{stype} #{i + 1}",
                pages=max(1, int(src.get("pages", 1))),
                target_price=_opt_float(src.get("target_price")),
                amazon_only=amazon_only,
            ))
        niches.append(niche)
    return settings, niches
