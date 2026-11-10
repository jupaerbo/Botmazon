"""Avisador de chollos de Amazon por nichos con menú interactivo en Telegram.

Uso:
    uvicorn main:app --host 0.0.0.0 --port 8000   # servidor web (Render) + bot + escaneo
    python main.py                 # bot de Telegram (menú) + escaneo periódico en segundo plano
    python main.py --once          # un solo escaneo, sin bot interactivo
    python main.py --dry-run       # no envía alertas push (el menú sí funciona)
    python main.py --test-telegram # envía un mensaje de prueba
    python main.py --get-chat-id   # muestra los chats que han escrito a tu bot
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from avisador.active import ActiveDeals, deal_to_dict
from avisador.config import AMAZON_ES_MERCHANT, Niche, Settings, Source, load_config
from avisador.detector import Deal, classify_seller, evaluate, matches_niche
from avisador.models import Offer
from avisador.notifier import TelegramNotifier, fmt_price, format_deal
from avisador.scraper import AmazonScraper, BlockedError, ScrapeError
from avisador.storage import PriceHistory
from avisador.views import render_menu, to_api_markup

log = logging.getLogger("avisador")


_logging_ready = False


def setup_logging(level: str) -> None:
    global _logging_ready
    if _logging_ready:
        return
    _logging_ready = True
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    if os.getenv("LOG_TO_FILE", "true").lower() != "false":  # en Render se desactiva (disco efímero)
        file = RotatingFileHandler("avisador.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file.setFormatter(fmt)
        root.addHandler(file)
    for noisy in ("urllib3", "httpx", "httpcore", "telegram", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _env(*names: str) -> str:
    """Primer valor no vacío entre varias variables de entorno (nombre nuevo, nombre antiguo)."""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def telegram_config() -> tuple[str, list[str]]:
    """Lee las credenciales de las variables de entorno del sistema (Render) y, si no están,
    del .env local (load_dotenv nunca sobrescribe variables ya definidas)."""
    token = _env("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN")
    chats = [c.strip() for c in _env("CHAT_ID", "TELEGRAM_CHAT_ID").split(",") if c.strip()]
    return token, chats


# ================================================================ escaneo

def fetch_source(scraper: AmazonScraper, source: Source) -> list[Offer]:
    if source.type == "deals":
        offers = scraper.fetch_deals(source.url)
    elif source.type == "search":
        offers = scraper.fetch_search(source.url, source.pages)
    else:
        offers = [scraper.fetch_product(source.url)]
    for o in offers:
        o.source = source.name
        o.target_price = source.target_price
        if source.amazon_only and o.sold_by is None:
            # La URL ya filtra "vendido por Amazon": no hace falta abrir la ficha para verificarlo.
            o.sold_by = o.ships_from = "Amazon"
            o.merchant_id = AMAZON_ES_MERCHANT
    return offers


class _SourcePacer:
    """Pausa aleatoria (jitter) entre una fuente y la siguiente, también entre nichos."""

    def __init__(self, delay: tuple[float, float]) -> None:
        self.delay = delay
        self.started = False

    def wait(self, next_name: str) -> None:
        if self.started:
            pause = random.uniform(*self.delay)
            log.info("⏳ Esperando %.1fs antes de escanear la siguiente fuente (%s)...", pause, next_name)
            time.sleep(pause)
        self.started = True


def scan_niche(niche: Niche, settings: Settings, scraper: AmazonScraper,
               history: PriceHistory, seen: set[str], pacer: _SourcePacer | None = None) -> list[Deal]:
    # 1) Recoger todas las fuentes, fusionando ASIN repetidos (se queda el que trae precio tachado).
    merged: dict[str, Offer] = {}
    for source in niche.sources:
        if scraper.consecutive_failures >= settings.circuit_breaker_failures:
            log.warning("[%s] %s: omitida (cortacircuitos activo: Amazon está limitando)", niche.id, source.name)
            continue
        if pacer:
            pacer.wait(f"{niche.id} · {source.name}")
        try:
            offers = fetch_source(scraper, source)
        except BlockedError as e:
            log.warning("[%s] %s: bloqueado, se reintentará el próximo ciclo (%s)", niche.id, source.name, e)
            continue
        except ScrapeError as e:
            log.error("[%s] %s: %s", niche.id, source.name, e)
            continue
        except Exception:
            log.exception("[%s] %s: error inesperado", niche.id, source.name)
            continue

        kept = 0
        for offer in offers:
            if offer.asin in seen or not matches_niche(offer, niche):
                continue
            kept += 1
            prev = merged.get(offer.asin)
            if prev is None or (offer.list_price and not prev.list_price) or \
                    (offer.target_price is not None and prev.target_price is None):
                merged[offer.asin] = offer
        log.info("[%s] %s: %d productos, %d pasan el filtro del nicho", niche.id, source.name,
                 len(offers), kept)

    # 2) Evaluar cada producto una sola vez y guardar su precio en el historial.
    deals: list[Deal] = []
    for asin, offer in merged.items():
        seen.add(asin)  # un ASIN solo cuenta para el primer nicho que lo encuentra
        offer.niche = niche.id
        deal = evaluate(offer, niche, settings, history.stats(asin))
        history.record(asin, offer.title, offer.price)
        if deal:
            deals.append(deal)
    history.save()
    return deals


class _CheckBudget:
    def __init__(self, n: int) -> None:
        self.left = n


def verify_seller(deal: Deal, settings: Settings, scraper: AmazonScraper,
                  history: PriceHistory, budget: _CheckBudget) -> bool | None:
    """Comprueba en la ficha quién vende y envía. True = aceptar, False = descartar,
    None = no se ha podido comprobar ahora (se reintenta en el próximo ciclo)."""
    o = deal.offer
    cached = history.get_seller(o.asin, settings.seller_cache_hours)
    if cached:
        deal.logistics = cached["label"]
        if not cached["ok"]:
            log.debug("[%s] Descartado (caché, vendedor externo: %s) | %s",
                      deal.niche.id, cached["label"], o.title[:60])
        return cached["ok"]

    if o.sold_by is None and o.ships_from is None:  # viene de búsqueda/ofertas: hay que abrir la ficha
        if budget.left <= 0 or scraper.consecutive_failures >= settings.circuit_breaker_failures:
            return None
        budget.left -= 1
        try:
            page = scraper.fetch_product(o.url)
        except (BlockedError, ScrapeError) as e:
            log.warning("[%s] No se pudo verificar el vendedor de %s: %s", deal.niche.id, o.asin, e)
            return None
        o.sold_by, o.ships_from, o.merchant_id = page.sold_by, page.ships_from, page.merchant_id

        # La ficha tiene el precio más fiable: si ha subido y ya no llega al mínimo, se descarta.
        if not page.available:
            log.info("[%s] Descartado (sin stock en la ficha) | %s", deal.niche.id, o.title[:60])
            return False
        if page.price and deal.old_price and page.price > o.price * 1.02:
            new_discount = (deal.old_price - page.price) / deal.old_price * 100
            if new_discount < deal.niche.min_drop_percent and \
                    not (o.target_price is not None and page.price <= o.target_price):
                log.info("[%s] Descartado (precio ya no vigente: %s) | %s", deal.niche.id,
                         fmt_price(page.price, o.currency), o.title[:60])
                return False
            o.price, deal.discount_percent = page.price, new_discount
            deal.is_price_error = new_discount >= deal.niche.price_error_percent

    ok, label = classify_seller(o)
    history.set_seller(o.asin, ok, label)
    deal.logistics = label
    if not ok:
        log.info("[%s] ❌ Descartado por vendedor externo (%s) | %s", deal.niche.id, label, o.title[:60])
    return ok


def filter_sellers(deals: list[Deal], settings: Settings, scraper: AmazonScraper,
                   history: PriceHistory) -> list[Deal]:
    if not settings.seller_filter:
        return deals
    deals = sorted(deals, key=lambda d: (d.is_price_error, d.discount_percent), reverse=True)
    budget = _CheckBudget(settings.max_seller_checks_per_cycle)
    accepted: list[Deal] = []
    rejected = deferred = 0
    for deal in deals:
        verdict = verify_seller(deal, settings, scraper, history, budget)
        if verdict is None:
            deferred += 1
        elif verdict:
            accepted.append(deal)
        else:
            rejected += 1
    log.info("Filtro de vendedor: %d aceptados, %d descartados, %d pendientes de verificar",
             len(accepted), rejected, deferred)
    history.save()
    return accepted


def push_alerts(deals: list[Deal], settings: Settings, history: PriceHistory,
                notifier: TelegramNotifier | None, affiliate_tag: str | None) -> None:
    """Alertas directas al chat. Por defecto solo los errores de precio (push_mode="errors")."""
    if settings.push_mode == "none":
        return
    candidates = deals if settings.push_mode == "all" else [d for d in deals if d.is_price_error]
    fresh = [d for d in candidates if history.should_notify(
        d.offer.asin, d.offer.price, settings.dedup_hours, settings.renotify_extra_drop_percent)]
    fresh.sort(key=lambda d: (d.is_price_error, d.discount_percent), reverse=True)
    to_send = fresh[: settings.max_alerts_per_cycle]
    if candidates:
        log.info("Alertas push (%s): %d candidatas, %d ya notificadas, %d a enviar",
                 settings.push_mode, len(candidates), len(candidates) - len(fresh), len(to_send))

    for deal in to_send:
        o = deal.offer
        summary = (f"[{deal.niche.id}] {'🚨 ERROR ' if deal.is_price_error else ''}"
                   f"-{deal.discount_percent:.0f}% {fmt_price(o.price, o.currency)} | {o.title[:70]}")
        markup = to_api_markup([[("📂 Ver sección", f"s:{deal.niche.id}:0"), ("🏠 Menú", "m")]])
        if notifier is None:
            log.info("ALERTA (sin envío): %s", summary)
            history.mark_notified(o.asin, o.price)
        elif notifier.send(format_deal(deal, affiliate_tag), reply_markup=markup):
            log.info("Alerta enviada: %s", summary)
            history.mark_notified(o.asin, o.price)
        else:
            log.error("No se pudo enviar: %s", summary)
    history.save()


def run_cycle(config_path: str, scraper: AmazonScraper, history: PriceHistory, store: ActiveDeals,
              notifier: TelegramNotifier | None, affiliate_tag: str | None) -> Settings:
    settings, niches = load_config(config_path)  # se recarga en cada ciclo
    scraper.timeout = settings.request_timeout_seconds
    scraper.max_retries = settings.max_retries
    scraper.backoff_base = settings.backoff_base_seconds
    scraper.backoff_max = settings.backoff_max_seconds
    scraper.delay = settings.delay_between_requests_seconds
    history.history_days = settings.history_days
    scraper.consecutive_failures = 0  # cada ciclo empieza con el cortacircuitos rearmado

    log.info("Escaneando %d nicho(s), %d fuente(s)", len(niches), sum(len(n.sources) for n in niches))
    seen: set[str] = set()
    deals: list[Deal] = []
    pacer = _SourcePacer(settings.delay_between_sources_seconds)
    for niche in niches:
        random.shuffle(niche.sources)  # orden menos predecible
        deals += scan_niche(niche, settings, scraper, history, seen, pacer)
    log.info("Chollos detectados: %d", len(deals))

    deals = filter_sellers(deals, settings, scraper, history)
    store.update(
        niches=[{"id": n.id, "label": n.label, "menu_label": n.menu_label} for n in niches],
        deals=[deal_to_dict(d, affiliate_tag) for d in deals],
        seen=seen,
        ttl_hours=settings.active_deal_ttl_hours,
    )
    push_alerts(deals, settings, history, notifier, affiliate_tag)
    history.prune(settings.dedup_hours)
    history.save()
    return settings


class Scanner:
    """Bucle de escaneo en un hilo aparte; el bot de Telegram puede pedir un escaneo inmediato."""

    def __init__(self, config_path: str, history: PriceHistory, store: ActiveDeals,
                 notifier: TelegramNotifier | None, affiliate_tag: str | None, once: bool = False) -> None:
        self.config_path = config_path
        self.history = history
        self.store = store
        self.notifier = notifier
        self.affiliate_tag = affiliate_tag
        self.once = once
        self.scraper = AmazonScraper()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.scanning = False
        self._requesters: set[int] = set()
        self._lock = threading.Lock()
        self.top_percent = Settings().top_percent

    def request_scan(self, chat_id: int) -> bool:
        """Pide un escaneo ya. Devuelve False si ya había uno en curso (igualmente se avisa al acabar)."""
        with self._lock:
            self._requesters.add(chat_id)
            if self.scanning:
                return False
        self.wake.set()
        return True

    def run(self) -> None:
        while not self.stop.is_set():
            self.scanning = True
            self.store.set_scanning(True)
            settings = Settings()
            try:
                settings = run_cycle(self.config_path, self.scraper, self.history, self.store,
                                     self.notifier, self.affiliate_tag)
            except ValueError as e:
                log.error("Configuración inválida: %s", e)
            except Exception:
                log.exception("Error inesperado en el ciclo de escaneo")
            finally:
                with self._lock:
                    self.scanning = False
                    requesters, self._requesters = self._requesters, set()
                self.store.set_scanning(False)
                self.top_percent = settings.top_percent
            self._notify_requesters(requesters)
            if self.once:
                break
            interval = self._scheduled_interval(settings)
            if self.scraper.consecutive_failures >= settings.circuit_breaker_failures:
                safety = settings.cooldown_minutes * 60 * random.uniform(0.9, 1.2)
                wait = safety + interval
                log.warning("🛑 Cortacircuitos activado (%s): Amazon está limitando las peticiones. "
                            "Pausa de seguridad de %.0f min; tras ella comenzará el intervalo de descanso "
                            "programado de %.0f min (próximo escaneo en ~%.0f min). "
                            "El menú sigue mostrando las últimas ofertas.",
                            self.scraper.last_block_reason or "bloqueo", safety / 60, interval / 60, wait / 60)
            else:
                wait = interval
                log.info("Próximo escaneo en %.1f minutos", wait / 60)
            self.wake.wait(wait)
            self.wake.clear()

    @staticmethod
    def _scheduled_interval(settings: Settings) -> float:
        if settings.scan_interval_minutes:
            return settings.scan_interval_minutes * 60 * random.uniform(0.9, 1.1)
        return random.uniform(settings.min_interval_minutes, settings.max_interval_minutes) * 60

    def _notify_requesters(self, chat_ids: set[int]) -> None:
        if not chat_ids or self.notifier is None:
            return
        text, rows = render_menu(self.store.snapshot(), self.top_percent, note="✅ <b>Escaneo terminado.</b>")
        self.notifier.send(text, reply_markup=to_api_markup(rows), chat_ids=list(chat_ids), preview=False)


# ================================================================ comandos de Telegram

def cmd_test_telegram(token: str, chats: list[str]) -> int:
    if not token or not chats:
        print("❌ Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en el .env")
        return 1
    notifier = TelegramNotifier(token, chats)
    me = notifier.get_me()
    if not me.get("ok"):
        print(f"❌ Token inválido: {me.get('description')}")
        return 1
    print(f"✅ Bot conectado: @{me['result']['username']}")
    ok = notifier.send("🔥 <b>Avisador de chollos</b>\n\nMensaje de prueba: ¡la configuración funciona! ✅")
    print("✅ Mensaje enviado" if ok else "❌ No se pudo enviar (revisa TELEGRAM_CHAT_ID y que hayas escrito /start al bot)")
    return 0 if ok else 1


def cmd_get_chat_id(token: str) -> int:
    if not token:
        print("❌ Primero pon TELEGRAM_BOT_TOKEN en el .env")
        return 1
    r = TelegramNotifier(token, []).get_updates()
    if not r.get("ok"):
        print(f"❌ Error: {r.get('description')}  (si el bot está en marcha, páralo antes)")
        return 1
    chats = {}
    for upd in r.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
    if not chats:
        print("No hay mensajes. Envía /start a tu bot y vuelve a ejecutar este comando.")
        return 1
    print("Chats encontrados (copia el ID en TELEGRAM_CHAT_ID):")
    for cid, name in chats.items():
        print(f"  {cid}\t{name}")
    return 0


# ================================================================ arranque común

def build_runtime(dry_run: bool = False, once: bool = False) -> dict:
    token, chats = telegram_config()
    notifier = TelegramNotifier(token, chats) if token and chats and not dry_run else None
    history = PriceHistory(os.getenv("HISTORY_PATH", "data/history.json"))
    store = ActiveDeals(os.getenv("ACTIVE_DEALS_PATH", "data/active_deals.json"))
    config_path = os.getenv("CONFIG_PATH", "config.json")
    scanner = Scanner(config_path, history, store, notifier,
                      os.getenv("AMAZON_AFFILIATE_TAG", "").strip() or None, once=once)
    try:
        settings, _ = load_config(config_path)
    except ValueError as e:
        log.error("Configuración inválida: %s (se usan valores por defecto)", e)
        settings = Settings()
    return {"token": token, "chats": chats, "notifier": notifier, "history": history,
            "store": store, "scanner": scanner, "settings": settings}


def build_menu_bot(rt: dict):
    from avisador.telegram_bot import MenuBot
    s = rt["settings"]
    return MenuBot(rt["token"], rt["chats"], rt["store"], rt["scanner"], page_size=s.page_size,
                   top_percent=s.top_percent, welcome_on_start=s.welcome_on_start)


# ================================================================ servidor web (Render)

STATE: dict = {}


async def _self_ping(url: str) -> None:
    """En el plan Free de Render el servicio se apaga tras 15 min sin tráfico HTTP entrante.
    Este bucle se hace una petición a sí mismo cada ~10 min para mantenerlo despierto."""
    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            await asyncio.sleep(random.uniform(540, 660))
            try:
                await client.get(f"{url.rstrip('/')}/health")
            except Exception as e:  # noqa: BLE001
                log.debug("Self-ping fallido: %s", e)


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_dotenv()  # no sobrescribe las variables de entorno del sistema
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    rt = build_runtime(dry_run=os.getenv("DRY_RUN", "").lower() == "true")
    STATE.update(rt, started=datetime.now(timezone.utc), tg_app=None)

    thread = threading.Thread(target=rt["scanner"].run, name="scanner", daemon=True)
    thread.start()
    STATE["thread"] = thread

    if rt["token"] and rt["chats"]:
        bot = build_menu_bot(rt)
        tg_app = bot.build(with_post_init=False)
        try:
            await tg_app.initialize()
            await tg_app.start()
            await tg_app.updater.start_polling(drop_pending_updates=True)
            await bot.post_init(tg_app)
            STATE["tg_app"] = tg_app
            log.info("Bot de Telegram activo (/ofertas).")
        except Exception:
            log.exception("No se pudo iniciar el bot de Telegram (el escaneo sigue funcionando)")
    else:
        log.warning("TELEGRAM_TOKEN / CHAT_ID no configurados: solo escaneo, sin menú ni alertas.")

    ping_task = None
    external_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if external_url and os.getenv("SELF_PING", "true").lower() != "false":
        ping_task = asyncio.create_task(_self_ping(external_url))
        log.info("Self-ping activo contra %s/health", external_url)

    log.info("Amazon Bargain Bot en línea. Alertas push: %s", rt["settings"].push_mode)
    try:
        yield
    finally:
        if ping_task:
            ping_task.cancel()
        rt["scanner"].stop.set()
        rt["scanner"].wake.set()
        tg_app = STATE.get("tg_app")
        if tg_app is not None:
            try:
                await tg_app.updater.stop()
                await tg_app.stop()
                await tg_app.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("Error al detener el bot de Telegram")
        log.info("Servicio detenido.")


app = FastAPI(title="Amazon Bargain Bot", lifespan=lifespan)


def _iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


@app.get("/")
async def root() -> dict:
    snap = STATE["store"].snapshot() if "store" in STATE else {}
    return {
        "status": "online",
        "service": "Amazon Bargain Bot",
        "telegram": STATE.get("tg_app") is not None,
        "scanning": bool(snap.get("scanning")),
        "last_scan": _iso(snap.get("updated")),
        "active_deals": {nid: len(lst) for nid, lst in snap.get("deals", {}).items()},
    }


@app.get("/health")
async def health():
    thread = STATE.get("thread")
    alive = bool(thread and thread.is_alive())
    body = {"status": "healthy" if alive else "unhealthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "scanner_alive": alive}
    # Si el hilo de escaneo muere, Render detecta el 503 y reinicia el servicio.
    return JSONResponse(body, status_code=200 if alive else 503)


# ================================================================ línea de comandos (uso local)

def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Avisador de chollos de Amazon por nichos")
    parser.add_argument("--once", action="store_true", help="un solo escaneo, sin bot interactivo")
    parser.add_argument("--dry-run", action="store_true", help="no envía alertas push")
    parser.add_argument("--test-telegram", action="store_true", help="envía un mensaje de prueba")
    parser.add_argument("--get-chat-id", action="store_true", help="muestra los chat IDs de tu bot")
    args = parser.parse_args()

    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    token, chats = telegram_config()
    if args.get_chat_id:
        return cmd_get_chat_id(token)
    if args.test_telegram:
        return cmd_test_telegram(token, chats)

    rt = build_runtime(dry_run=args.dry_run, once=args.once)
    scanner = rt["scanner"]
    if args.once or not (token and chats):
        if not (token and chats):
            log.warning("Telegram no configurado: solo escaneo, sin menú ni alertas.")
        log.info("Avisador iniciado (solo escaneo). Ctrl+C para detener.")
        try:
            scanner.run()
        except KeyboardInterrupt:
            log.info("Detenido por el usuario.")
        finally:
            rt["history"].save()
        return 0

    # Bot interactivo en el hilo principal + escaneo en segundo plano.
    thread = threading.Thread(target=scanner.run, name="scanner", daemon=True)
    thread.start()
    log.info("Avisador iniciado: menú de Telegram activo (/ofertas) y escaneo en segundo plano. "
             "Alertas push: %s. Ctrl+C para detener.", "desactivadas (--dry-run)" if args.dry_run
             else rt["settings"].push_mode)
    try:
        build_menu_bot(rt).build().run_polling(drop_pending_updates=True)
    finally:
        # El hilo de escaneo guarda el historial tras cada paso; no se guarda aquí para no
        # pisar una escritura en curso.
        scanner.stop.set()
        scanner.wake.set()
        log.info("Detenido.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
