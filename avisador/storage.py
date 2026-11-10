"""Historial de precios y registro de ASIN ya notificados (JSON local)."""
from __future__ import annotations

import json
import logging
import os
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

RECORD_EVERY_SECONDS = 6 * 3600   # guarda un punto aunque el precio no cambie, como mucho cada 6 h
MAX_POINTS_PER_ASIN = 200


@dataclass
class PriceStats:
    last: float | None
    median: float | None
    samples: int


class PriceHistory:
    def __init__(self, path: str | Path, history_days: int = 30) -> None:
        self.path = Path(path)
        self.history_days = history_days
        raw = self._load()
        self.items: dict[str, dict] = raw.get("items", {})
        self.notified: dict[str, dict] = raw.get("notified", {})

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            backup = self.path.with_suffix(f".corrupt-{int(time.time())}.json")
            log.error("Historial ilegible (%s). Copia en %s; se empieza de cero.", e, backup)
            try:
                self.path.replace(backup)
            except OSError:
                pass
            return {}
        if "items" not in data:  # formato de la versión anterior: se descarta
            log.info("Historial con formato antiguo; se empieza uno nuevo.")
            return {}
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 2, "items": self.items, "notified": self.notified},
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ precios

    def stats(self, asin: str) -> PriceStats:
        """Estadísticas con los precios registrados ANTES de la lectura actual."""
        points = self.items.get(asin, {}).get("p", [])
        cutoff = time.time() - self.history_days * 86400
        recent = [price for ts, price in points if ts >= cutoff]
        return PriceStats(
            last=points[-1][1] if points else None,
            median=statistics.median(recent) if recent else None,
            samples=len(recent),
        )

    def record(self, asin: str, title: str, price: float) -> None:
        now = int(time.time())
        item = self.items.setdefault(asin, {"t": title, "p": []})
        item["t"] = title[:200]
        item["seen"] = now
        points = item["p"]
        if not points or points[-1][1] != price or now - points[-1][0] >= RECORD_EVERY_SECONDS:
            points.append([now, price])
            del points[:-MAX_POINTS_PER_ASIN]

    # ------------------------------------------------------------ caché de vendedor

    def get_seller(self, asin: str, max_age_hours: float) -> dict | None:
        s = self.items.get(asin, {}).get("seller")
        if s and time.time() - s["ts"] < max_age_hours * 3600:
            return s
        return None

    def set_seller(self, asin: str, ok: bool, label: str) -> None:
        item = self.items.setdefault(asin, {"t": "", "p": [], "seen": int(time.time())})
        item["seller"] = {"ts": int(time.time()), "ok": ok, "label": label}

    # ------------------------------------------------------------ deduplicación

    def should_notify(self, asin: str, price: float, dedup_hours: float, extra_drop_pct: float) -> bool:
        """No repite un ASIN durante `dedup_hours`, salvo que su precio baje otro `extra_drop_pct` %."""
        last = self.notified.get(asin)
        if not last or time.time() - last["ts"] >= dedup_hours * 3600:
            return True
        return price <= last["price"] * (1 - extra_drop_pct / 100)

    def mark_notified(self, asin: str, price: float) -> None:
        self.notified[asin] = {"ts": int(time.time()), "price": price}

    def prune(self, dedup_hours: float) -> None:
        now = time.time()
        self.notified = {a: n for a, n in self.notified.items() if now - n["ts"] < dedup_hours * 3600}
        cutoff = now - self.history_days * 86400
        self.items = {a: i for a, i in self.items.items() if i.get("seen", now) >= cutoff}
