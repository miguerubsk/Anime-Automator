"""
Avisos de copias viejas sustituidas, juntos por anime.

Cada capítulo que llega en .mkv puede sustituir a una copia peor; en vez de
un aviso por capítulo, se apuntan aquí y se cuentan todos en uno cuando ya
no queda nada de ese anime en marcha (ni en cola, ni en reintento, ni en un
torrent). Si el contenedor se reinicia a medias, lo apuntado se pierde (los
borrados quedan en el log).
"""

import logging
import threading

from ..core.singleton import Singleton
from ..library.quality import Replacement, replacement_notices
from ..notify.telegram import notify
from .registry import InProgress

log = logging.getLogger("anime-automator")


class ReplacementDigest(metaclass=Singleton):
    def __init__(self):
        self._lock = threading.Lock()
        self._by_group: dict[str, list[Replacement]] = {}

    def add(self, group: str, done: Replacement | None):
        if done is None:
            return
        with self._lock:
            self._by_group.setdefault(group, []).append(done)

    def flush_if_idle(self, cfg: dict, group: str):
        """Si ya no queda nada de ese anime en marcha, avisa de todo lo sustituido."""
        if any(key[0] == group for key in InProgress().snapshot()):
            return
        with self._lock:
            done = self._by_group.pop(group, [])
        for text in replacement_notices(done):
            notify(text, cfg)
