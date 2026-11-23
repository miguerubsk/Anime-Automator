"""Arranque ordenado de todos los servicios (una sola vez por proceso)."""

import logging
import threading

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("anime-automator")

_started = False
_lock = threading.Lock()


def start():
    """
    Gunicorn importa la app una sola vez (-w 1). En ese momento la cola está
    vacía: es el momento seguro para borrar restos de ejecuciones
    interrumpidas y recuperar lo que quedó pendiente. El orden importa:
    las peticiones de requester-bot se cargan antes que la cola, porque el
    worker necesita saber de qué petición es cada capítulo.
    """
    global _started
    with _lock:
        if _started:
            return
        _started = True

    from .admin_bot.bot import AdminBot
    from .library.anilist import AniListClient
    from .pipeline.jobs import CallbackSender, JobTracker
    from .pipeline.pending import PendingStore
    from .pipeline.recovery import cleanup_stale_tmp, restore_pending_work
    from .pipeline.retries import RetryScheduler
    from .pipeline.torrents import TorrentTracker
    from .pipeline.watches import DriveWatcher
    from .pipeline.worker import Placer, Worker

    PendingStore()               # se engancha a la cola para guardarla
    cleanup_stale_tmp()
    AniListClient().load_cache()
    JobTracker().load()
    TorrentTracker().load()
    restore_pending_work()

    for service in (RetryScheduler(), CallbackSender(), Worker(), Placer(), TorrentTracker(), AdminBot(),
                    DriveWatcher()):
        service.start()
