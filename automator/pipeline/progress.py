"""Progreso del "lote" actual, para incluirlo al final de las notificaciones."""

import threading

from ..core.singleton import Singleton


class BatchProgress(metaclass=Singleton):
    """carpeta -> {"name", "pending": set, "done": set, "failed": set}"""

    def __init__(self):
        self._lock = threading.Lock()
        self._batch: dict[str, dict] = {}

    def queued(self, folder: str, name: str, episodes):
        with self._lock:
            entry = self._batch.setdefault(folder, {"name": name, "pending": set(), "done": set(), "failed": set()})
            entry["name"] = name
            entry["pending"].update(episodes)

    def _move(self, folder: str, ep, to: str | None):
        with self._lock:
            entry = self._batch.get(folder)
            if entry:
                entry["pending"].discard(ep)
                if to:
                    entry[to].add(ep)

    def done(self, folder: str, ep):
        self._move(folder, ep, "done")

    def failed(self, folder: str, ep):
        self._move(folder, ep, "failed")

    def cancelled(self, folder: str, ep):
        self._move(folder, ep, None)

    def block(self) -> str:
        """
        Resumen para añadir al final de los avisos de éxito/error. "" si solo
        hay un capítulo de por medio en total (no merece la pena para eso).
        Los animes ya terminados se retiran siempre (aunque no se llegue a
        mostrar texto), para no colarse en un futuro resumen que no tenga
        nada que ver.
        """
        with self._lock:
            total_all = sum(self._total(e) for e in self._batch.values())
            pending_lines, finished_lines = [], []
            for key, e in sorted(self._batch.items(), key=lambda kv: kv[1]["name"].lower()):
                if e["pending"]:
                    pending_lines.append(self._pending_line(e))
                else:
                    finished_lines.append(self._finished_line(e))
                    self._batch.pop(key)
            if total_all <= 1:
                return ""
        return self._render(finished_lines, pending_lines)

    @staticmethod
    def _total(e: dict) -> int:
        return len(e["pending"]) + len(e["done"]) + len(e["failed"])

    def _pending_line(self, e: dict) -> str:
        extra = f", {len(e['failed'])} fallidos" if e["failed"] else ""
        return f"• {e['name']}: {len(e['done'])}/{self._total(e)} hechos{extra}, {len(e['pending'])} pendientes"

    def _finished_line(self, e: dict) -> str:
        extra = f" ({len(e['failed'])} fallidos)" if e["failed"] else ""
        return f"• {e['name']}: completado {len(e['done'])}/{self._total(e)}{extra}"

    @staticmethod
    def _render(finished_lines: list[str], pending_lines: list[str]) -> str:
        """Hay al menos una línea: si el total pasa de 1, hay algún anime en el lote."""
        lines = []
        if finished_lines:
            lines += ["✔️ Terminado:", *finished_lines]
        if pending_lines:
            if finished_lines:
                lines.append("")
            lines += ["⏳ Pendiente:", *pending_lines]
        return "\n\n📊 Progreso de la cola:\n" + "\n".join(lines)
