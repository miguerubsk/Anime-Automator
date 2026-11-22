"""Conjuntos de capítulos compartidos entre hilos (en curso / completados)."""

import threading

from ..core.singleton import Singleton


class KeySet:
    """Un set de claves (carpeta, episodio) con su propio cerrojo."""

    def __init__(self):
        self._keys: set = set()
        self._lock = threading.Lock()

    def add(self, key):
        with self._lock:
            self._keys.add(key)

    def try_add(self, key) -> bool:
        """Lo añade si no estaba. True si lo ha añadido."""
        with self._lock:
            if key in self._keys:
                return False
            self._keys.add(key)
            return True

    def discard(self, key):
        with self._lock:
            self._keys.discard(key)

    def __contains__(self, key) -> bool:
        with self._lock:
            return key in self._keys

    def episodes_of(self, folder: str) -> set:
        with self._lock:
            return {ep for (f, ep) in self._keys if f == folder}

    def snapshot(self) -> set:
        with self._lock:
            return set(self._keys)


class InProgress(KeySet, metaclass=Singleton):
    """Capítulos encolados, procesándose, esperando reintento o en un torrent."""


class Completed(KeySet, metaclass=Singleton):
    """
    Capítulos completados con éxito en esta ejecución del contenedor.
    Se consulta ADEMÁS del listado de disco: un montaje SMB puede devolver un
    listado de directorio cacheado unos segundos tras escribir un archivo, y
    así no se reprocesa por error. No persiste entre reinicios (al reiniciar,
    la caché del montaje ya se habrá refrescado).
    """
