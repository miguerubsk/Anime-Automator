"""
Los hilos de trabajo: el worker descarga y remuxa, el colocador copia a la NAS.

El colocador copia a la NAS el capítulo ya remuxado mientras el worker
descarga el siguiente. Cola de 1: como mucho uno esperando y otro copiándose
(si el colocador va atrasado, el worker espera; no se acumulan remuxes en tmp).
"""

import logging
import queue
import time
from pathlib import Path

from ..core.background import BackgroundLoop
from ..core.config import load_config
from ..core.errors import DestinationWriteError
from ..core.nas import assert_nas_mounted
from ..core.singleton import Singleton
from ..notify.telegram import notify
from ..sources.japanpaw import alternative_link
from .failures import RELEASED, FailureContext, handle_failure
from .model import WorkItem
from .pending import PendingStore
from .processing import DRIVE, EpisodeProcessor, chosen_source, source_for
from .progress import BatchProgress
from .registry import Completed, InProgress
from .reporting import report
from .replacements import ReplacementDigest
from .retries import RetryScheduler
from .watches import WatchRegistry
from .work_queue import WorkQueue

log = logging.getLogger("anime-automator")

NAS_RETRY_HOURS = 5 / 60
NAS_ALERT_EVERY_SECONDS = 3600


class Finisher(metaclass=Singleton):
    """Cierra un capítulo: éxito (dest) o fallo (exc), y libera o conserva su estado."""

    def finish(self, cfg: dict, item: WorkItem, dest: Path | None = None, exc: Exception | None = None):
        disposition = RELEASED
        try:
            if exc is None:
                self._success(item, dest)
            else:
                disposition = handle_failure(FailureContext(cfg, item, exc))
        finally:
            if not disposition.keep_in_progress:
                InProgress().discard(item.key)
                ReplacementDigest().flush_if_idle(cfg, item.group)
            if not disposition.keep_pending:
                # terminado (bien, mal o pasado a torrent, que tiene su propio
                # estado persistente): ya no está pendiente
                PendingStore().remove(item)

    @staticmethod
    def _success(item: WorkItem, dest: Path):
        RetryScheduler().wake(item.url, item.target.get("folder", ""), f"{item.title} {item.label} descargado")
        Completed().add(item.key)
        if not item.is_movie:
            WatchRegistry().episode_done(item.target.get("page_url"), item.folder, item.episode)
        BatchProgress().done(item.folder, item.episode)
        report(item.key, "episode_done",
               lambda: f"✅ <b>{item.title}</b> - {item.label} procesado y guardado en:\n{dest}"
                       f"{BatchProgress().block()}",
               source="japanpaw")

    def finish_safely(self, cfg, item, **kwargs):
        try:
            self.finish(cfg, item, **kwargs)
        except Exception:
            log.exception("Fallo cerrando %s cap. %s", item.title, item.episode)


class Placer(BackgroundLoop, metaclass=Singleton):
    thread_name = "placer"

    def __init__(self):
        super().__init__()
        self._queue: queue.Queue = queue.Queue(maxsize=1)

    def put(self, item: WorkItem, processed: Path):
        self._queue.put((item, processed))

    def step(self):
        item, processed = self._queue.get()
        cfg = load_config()
        try:
            try:
                dest = EpisodeProcessor().place(cfg, item, processed)
            except DestinationWriteError as e:
                Finisher().finish_safely(cfg, item, exc=e)
            except Exception as e:
                # la NAS no está, etc.: es nuestro, no del enlace -> temporal
                Finisher().finish_safely(cfg, item, exc=DestinationWriteError(f"No se pudo colocar en la NAS: {e}"))
            else:
                Finisher().finish_safely(cfg, item, dest=dest)
        finally:
            self._queue.task_done()


class Worker(BackgroundLoop, metaclass=Singleton):
    thread_name = "worker"

    def __init__(self):
        super().__init__()
        self._last_nas_alert = 0.0

    def step(self):
        item = WorkQueue().get()
        try:
            self._handle(load_config(), item)
        finally:
            WorkQueue().task_done()

    def _nas_ready(self, cfg: dict, item: WorkItem) -> bool:
        """
        Tras un corte de luz la NAS puede tardar más en volver que este
        servidor: en vez de descartar el capítulo, se reintenta en 5 min
        (sigue en curso y en la cola persistente). Aviso como mucho 1/hora.
        """
        try:
            assert_nas_mounted(cfg)
            return True
        except Exception as e:
            log.error("🚨 %s — %s cap. %s se reintentará en 5 min", e, item.title, item.episode)
            if time.time() - self._last_nas_alert > NAS_ALERT_EVERY_SECONDS:
                self._last_nas_alert = time.time()
                notify(f"🚨 {e}\nLos capítulos pendientes se reintentarán cada 5 min.", cfg)
            RetryScheduler().schedule(item, NAS_RETRY_HOURS, item.attempt)
            return False

    @staticmethod
    def _refresh_link(cfg: dict, item: WorkItem) -> WorkItem:
        """Si toca volver a mirar la ficha (enlace caído), con el enlace que haya ahora."""
        if not item.target.get("relink_pending"):
            return item
        fresh = alternative_link(cfg, item.target, item.episode, set())
        log.info("Releída la ficha de %s %s: %s", item.title, item.label,
                 "enlace nuevo" if fresh and fresh != item.url else "mismo enlace, se prueba igual")
        return item.evolve(url=fresh or None, relink_pending=False)

    def _handle(self, cfg: dict, item: WorkItem):
        if not self._nas_ready(cfg, item):
            return
        item = self._refresh_link(cfg, item)
        try:
            log.info("Procesando %s capítulo %s (intento %s%s)", item.title, item.episode, item.attempt,
                     ", desde el Drive" if chosen_source(cfg, item) == DRIVE else "")
            processed = EpisodeProcessor().prepare(cfg, item, source_for(cfg, item))
        except Exception as e:
            Finisher().finish_safely(cfg, item, exc=e)
        else:
            Placer().put(item, processed)
