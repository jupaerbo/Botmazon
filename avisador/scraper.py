"""Cliente HTTP para Amazon basado en curl_cffi.

- Impersona la huella TLS/HTTP2 (JA3/JA4) de un navegador real, así Amazon no ve "Python".
- curl_cffi envía las cabeceras propias de ese navegador (User-Agent, sec-ch-ua, Accept,
  Accept-Encoding...) coherentes con la huella; aquí solo añadimos idioma y navegación.
- Una única sesión con cookies que se mantiene entre peticiones (como un usuario real),
  con Referer encadenado; solo se renueva tras un CAPTCHA o un bloqueo persistente.
- Reintentos con backoff exponencial + jitter ante 503/429/CAPTCHA.
"""
from __future__ import annotations

import logging
import os
import random
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from curl_cffi import requests as cffi
from curl_cffi.requests import BrowserType

from . import parsers
from .models import Offer

log = logging.getLogger(__name__)

# Perfiles de navegador de escritorio recientes (se usan los que soporte la versión instalada).
_PREFERRED_PROFILES = ["chrome152", "chrome152_macos", "chrome151", "chrome151_macos",
                       "chrome150", "safari270", "firefox155"]
_AVAILABLE = {b.value for b in BrowserType}
PROFILES = [p for p in _PREFERRED_PROFILES if p in _AVAILABLE] or ["chrome"]

ACCEPT_LANGUAGE = {
    "es": "es-ES,es;q=0.9,en;q=0.7",
    "fr": "fr-FR,fr;q=0.9,en;q=0.7",
    "de": "de-DE,de;q=0.9,en;q=0.7",
    "it": "it-IT,it;q=0.9,en;q=0.7",
    "co.uk": "en-GB,en;q=0.9",
    "com": "en-US,en;q=0.9",
}
RETRY_STATUS = {403, 429, 500, 502, 503, 504, 530}
CAPTCHA_MARKERS = (
    "validatecaptcha", "captchacharacters", "api-services-support@amazon.com",
    "introduce los caracteres que ves", "enter the characters you see",
)


class BlockedError(Exception):
    """Amazon sigue bloqueando tras agotar los reintentos."""


class ScrapeError(Exception):
    """Error no recuperable (p. ej. 404)."""


def with_param(url: str, key: str, value: str) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q[key] = value
    return urlunparse(p._replace(query=urlencode(q)))


def with_page(url: str, page: int) -> str:
    return url if page <= 1 else with_param(url, "page", str(page))


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Backoff exponencial (x2) con jitter: ~15 s, ~30 s, ~60 s... (con base=15), limitado a `cap`."""
    raw = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(raw * 0.6, raw * 1.4)


class AmazonScraper:
    def __init__(self, timeout: float = 20, max_retries: int = 4,
                 backoff_base: float = 5, backoff_max: float = 120,
                 delay: tuple[float, float] = (5, 12)) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.delay = delay
        self._last_request = 0.0
        self.consecutive_failures = 0   # fuentes seguidas bloqueadas (para el cortacircuitos)
        # Proxy opcional (p. ej. residencial) para cuando la IP del servidor está bloqueada.
        self.proxy = os.getenv("PROXY_URL", "").strip() or None
        self.last_block_reason = ""
        self._new_session()

    # ------------------------------------------------------------ sesión

    def _new_session(self) -> None:
        old = getattr(self, "session", None)
        if old is not None:
            old.close()
        self.profile = random.choice(PROFILES)
        self.session = cffi.Session(impersonate=self.profile)
        self._last_url: str | None = None
        log.debug("Nueva sesión HTTP con perfil %s", self.profile)

    def _headers(self, url: str) -> dict[str, str]:
        host = urlparse(url).netloc.lower()
        tld = host.split("amazon.", 1)[-1] if "amazon." in host else "com"
        headers = {"Accept-Language": ACCEPT_LANGUAGE.get(tld, "en-US,en;q=0.9")}
        if self._last_url:  # navegación dentro de Amazon
            headers["Referer"] = self._last_url
            headers["Sec-Fetch-Site"] = "same-origin"
        elif random.random() < 0.5:  # primera visita: a veces llega desde un buscador
            headers["Referer"] = random.choice(["https://www.google.com/", "https://www.bing.com/"])
            headers["Sec-Fetch-Site"] = "cross-site"
        return headers

    def _pace(self) -> None:
        """Pausa aleatoria entre peticiones consecutivas."""
        if self._last_request:
            remaining = random.uniform(*self.delay) - (time.monotonic() - self._last_request)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request = time.monotonic()

    @staticmethod
    def _is_captcha(resp) -> bool:
        if "captcha" in str(resp.url).lower():
            return True
        head = resp.text[:20000].lower()
        return any(m in head for m in CAPTCHA_MARKERS)

    @staticmethod
    def _dump_invalid(url: str, html: str) -> None:
        """Guarda las últimas páginas inesperadas en data/debug/ para poder diagnosticarlas."""
        try:
            folder = Path("data/debug")
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"invalid_{int(time.time())}.html").write_text(f"<!-- {url} -->\n{html}", encoding="utf-8")
            for old in sorted(folder.glob("invalid_*.html"))[:-5]:
                old.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------ petición con reintentos

    def get_html(self, url: str, is_valid: Callable[[str], bool]) -> str:
        reason = "desconocido"
        for attempt in range(1, self.max_retries + 1):
            self._pace()
            retry_after = 0.0
            try:
                extra = {"proxy": self.proxy} if self.proxy else {}
                resp = self.session.get(url, headers=self._headers(url), timeout=self.timeout,
                                        allow_redirects=True, **extra)
            except cffi.RequestsError as e:
                reason = f"error de red ({e.__class__.__name__})"
            else:
                if resp.status_code == 404:
                    raise ScrapeError(f"No encontrado (404): {url}")
                if resp.status_code in RETRY_STATUS:
                    reason = f"HTTP {resp.status_code}"
                    try:
                        retry_after = float(resp.headers.get("Retry-After", 0))
                    except ValueError:
                        pass
                elif self._is_captcha(resp):
                    reason = "CAPTCHA"
                    self._new_session()  # cookies y huella nuevas
                elif resp.status_code != 200:
                    reason = f"HTTP {resp.status_code}"
                elif not is_valid(resp.text):
                    reason = "página sin el contenido esperado"
                    self._dump_invalid(url, resp.text)
                else:
                    self._last_url = url
                    self.consecutive_failures = 0
                    return resp.text

            if attempt < self.max_retries:
                wait = max(backoff_delay(attempt, self.backoff_base, self.backoff_max), retry_after)
                log.warning("%s en %s. Reintento %d/%d en %.0f s",
                            reason, urlparse(url).path[:40], attempt, self.max_retries - 1, wait)
                time.sleep(wait)

        self._new_session()  # bloqueo persistente: la siguiente fuente empieza con sesión nueva
        self.consecutive_failures += 1
        self.last_block_reason = reason
        raise BlockedError(f"{url} -> {reason} tras {self.max_retries} intentos")

    # ------------------------------------------------------------ tipos de fuente

    def fetch_product(self, url: str) -> Offer:
        url = parsers.canonical_url(url)
        html = self.get_html(url, lambda h: 'id="productTitle"' in h or 'id="title"' in h)
        offer = parsers.parse_product_page(html, url)
        if offer is None:
            raise ScrapeError(f"No se pudo leer la ficha {url}")
        return offer

    def fetch_search(self, url: str, pages: int = 1) -> list[Offer]:
        offers: list[Offer] = []
        for page in range(1, max(1, pages) + 1):
            html = self.get_html(with_page(url, page), parsers.is_search_page)
            found = parsers.parse_search_page(html, url)
            offers.extend(found)
            if not found:
                break
        return offers

    def fetch_deals(self, url: str) -> list[Offer]:
        html = self.get_html(url, parsers.is_deals_page)
        return parsers.parse_deals_page(html, url)
