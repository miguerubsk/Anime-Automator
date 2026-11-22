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
            total_all = sum(len(e["pending"]) + len(e["done"]) + len(e["failed"]) for e in self._batch.values())
            pending_lines, finished_lines, finished_keys = [], [], []
            for key, e in sorted(self._batch.items(), key=lambda kv: kv[1]["name"].lower()):
                total_n = len(e["pending"]) + len(e["done"]) + len(e["failed"])
                if e["pending"]:
                    extra = f", {len(e['failed'])} fallidos" if e["failed"] else ""
                    pending_lines.append(f"• {e['name']}: {len(e['done'])}/{total_n} hechos{extra}, "
                                         f"{len(e['pending'])} pendientes")
                else:
                    extra = f" ({len(e['failed'])} fallidos)" if e["failed"] else ""
                    finished_lines.append(f"• {e['name']}: completado {len(e['done'])}/{total_n}{extra}")
                    finished_keys.append(key)
            for key in finished_keys:
                self._batch.pop(key, None)
            if total_all <= 1:
                return ""

        if not pending_lines and not finished_lines:
            return ""
        lines = []
        if finished_lines:
            lines += ["✔️ Terminado:", *finished_lines]
        if pending_lines:
            if finished_lines:
                lines.append("")
            lines += ["⏳ Pendiente:", *pending_lines]
        return "\n\n📊 Progreso de la cola:\n" + "\n".join(lines)
