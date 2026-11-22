"""Hilos de fondo con un patrón común (método plantilla)."""

import logging
import threading
import time
from abc import ABC, abstractmethod

log = logging.getLogger("anime-automator")


class BackgroundLoop(ABC):
    """
    Un servicio que repite `step()` para siempre en un hilo demonio.

    Las subclases solo implementan un paso (sacar un capítulo de la cola,
    revisar los torrents, enviar un aviso...). Un error inesperado en un paso
    se registra y el bucle sigue: antes, una excepción no prevista mataba el
    hilo en silencio y ese servicio dejaba de funcionar hasta reiniciar.
    """

    thread_name = "background"
    error_pause_seconds = 1.0

    def __init__(self):
        self._thread: threading.Thread | None = None

    @abstractmethod
    def step(self):
        """Una vuelta del bucle. Puede bloquear (esperar trabajo) o dormir."""

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name=self.thread_name)
        self._thread.start()

    def _run(self):
        while True:
            try:
                self.step()
            except Exception:
                log.exception("Fallo inesperado en el hilo %s; sigue funcionando", self.thread_name)
                time.sleep(self.error_pause_seconds)
