"""Reintentos diferidos: capítulos que vuelven a la cola pasado un tiempo."""

import logging
import threading
import time
from dataclasses import dataclass

from ..core.background import BackgroundLoop
from ..core.singleton import Singleton
from ..core.util import url_host
from .model import WorkItem
from .pending import PendingStore
from .work_queue import WorkQueue

log = logging.getLogger("anime-automator")


@dataclass
class RetryEntry:
    item: WorkItem
    retry_at: float


class RetryScheduler(BackgroundLoop, metaclass=Singleton):
    """
    Episodios que fallaron por algo pasajero (conexión, certificado, cupo del
    servidor, la NAS sin montar...) o cuyo enlace hay que volver a buscar más
    tarde. Cada minuto, o al momento si alguien lo despierta, los que ya han
    cumplido su espera vuelven a la cola.
    """

    thread_name = "retries"

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._entries: list[RetryEntry] = []
        self._wakeup = threading.Event()

    def schedule(self, item: WorkItem, delay_hours: float, attempt: int):
        item = item.evolve(attempt=attempt)
        PendingStore().upsert(item, retry_at=time.time() + delay_hours * 3600)
        self.add(item, time.time() + delay_hours * 3600)

    def add(self, item: WorkItem, retry_at: float):
        """Sin pasar por el almacén de pendientes (p.ej. al recuperar el estado)."""
        with self._lock:
            self._entries.append(RetryEntry(item, retry_at))

    def wake(self, url: str, folder: str = "", reason: str = ""):
        """
        Un capítulo acaba de salir adelante: los que esperaban reintento del
        mismo servidor de descarga (o del mismo anime) se prueban YA, quede lo
        que les quede de espera, por si suena la flauta.
        """
        host = url_host(url)
        now = time.time()
        n = 0
        with self._lock:
            for e in self._entries:
                same_host = host and url_host(e.item.url) == host
                same_anime = folder and e.item.target.get("folder") == folder
                if (same_host or same_anime) and e.retry_at > now:
                    e.retry_at = now
                    n += 1
        if n:
            log.info("%s: se adelantan %s reintento(s) pendientes", reason or "Descarga correcta", n)
            self._wakeup.set()

    def entries(self) -> list[RetryEntry]:
        with self._lock:
            return list(self._entries)

    def remove(self, keys) -> list[WorkItem]:
        keys = set(keys)
        with self._lock:
            removed = [e.item for e in self._entries if e.item.key in keys]
            self._entries = [e for e in self._entries if e.item.key not in keys]
        return removed

    def take(self, key) -> WorkItem | None:
        with self._lock:
            found = next((e for e in self._entries if e.item.key == key), None)
            if found is not None:
                self._entries.remove(found)
        return found.item if found else None

    def step(self):
        self._wakeup.wait(60)  # cada minuto, o al momento si alguien lo despierta
        self._wakeup.clear()
        now = time.time()
        with self._lock:
            ready = [e for e in self._entries if e.retry_at <= now]
            self._entries = [e for e in self._entries if e.retry_at > now]
        for e in ready:
            log.info("Reintentando %s capítulo %s (intento %s)", e.item.title, e.item.episode, e.item.attempt)
            WorkQueue().put(e.item, priority=PendingStore().priority_of(e.item))
