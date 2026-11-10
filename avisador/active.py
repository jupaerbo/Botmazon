"""Caché de "ofertas activas del último escaneo", por nicho.

La escribe el hilo de escaneo y la lee el bot de Telegram para navegar por secciones
al instante, sin volver a consultar Amazon. Se persiste en JSON para que, al reiniciar,
el menú siga mostrando el último escaneo.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from pathlib import Path

from .detector import Deal
from .notifier import with_affiliate

log = logging.getLogger(__name__)


def deal_to_dict(deal: Deal, affiliate_tag: str | None = None) -> dict:
    o = deal.offer
    return {
        "asin": o.asin,
        "title": o.title,
        "url": with_affiliate(o.url, affiliate_tag),
        "price": o.price,
        "old_price": deal.old_price,
        "old_label": deal.old_label,
        "discount": round(deal.discount_percent, 1),
        "is_error": deal.is_price_error,
        "logistics": deal.logistics,
        "currency": o.currency,
        "niche": deal.niche.id,
        "source": o.source,
        "seen_ts": int(time.time()),
    }


class ActiveDeals:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = {"updated": None, "scanning": False, "niches": [], "deals": {}}
        if self.path.exists():
            try:
                self._data.update(json.loads(self.path.read_text(encoding="utf-8")))
                self._data["scanning"] = False
            except (json.JSONDecodeError, OSError) as e:
                log.warning("No se pudo leer %s (%s); se empieza vacío", self.path, e)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def set_scanning(self, value: bool) -> None:
        with self._lock:
            self._data["scanning"] = value

    def update(self, niches: list[dict], deals: list[dict], seen: set[str], ttl_hours: float) -> None:
        """Sustituye las ofertas activas por las del escaneo actual.

        Se conservan (hasta `ttl_hours`) las ofertas anteriores cuyo ASIN no se ha podido
        revisar en este ciclo (p. ej. porque su fuente estaba bloqueada)."""
        now = time.time()
        with self._lock:
            new: dict[str, list[dict]] = {n["id"]: [] for n in niches}
            fresh_asins = {d["asin"] for d in deals}
            for d in deals:
                new.setdefault(d["niche"], []).append(d)
            for nid, old_list in self._data.get("deals", {}).items():
                if nid not in new:
                    continue
                for d in old_list:
                    if d["asin"] not in seen and d["asin"] not in fresh_asins and \
                            now - d.get("seen_ts", 0) < ttl_hours * 3600:
                        new[nid].append(d)
            for lst in new.values():
                lst.sort(key=lambda d: (d["is_error"], d["discount"]), reverse=True)
            self._data.update(updated=int(now), niches=niches, deals=new)
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)
