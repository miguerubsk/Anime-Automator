"""La cola de trabajo, gestionable (ver, eliminar, pausar, reordenar)."""

import threading
from dataclasses import dataclass

from ..core.singleton import Singleton
from .model import WorkItem, key_id


class QueueListener:
    """Quien quiera enterarse de los cambios de la cola (p.ej. para guardarla en disco)."""

    def on_put(self, item: WorkItem, priority: int):
        """Antes de insertar un elemento."""

    def on_changed(self):
        """Después de un cambio que hay que guardar."""


@dataclass
class _Entry:
    item: WorkItem
    priority: int
    paused: bool = False


class WorkQueue(metaclass=Singleton):
    """
    El orden de ejecución es el orden de la lista. put() inserta según la
    prioridad (0 = urgente, p.ej. un capítulo semanal del webhook, va delante
    de 1 = descarga masiva) y FIFO dentro de cada prioridad; después el admin
    puede reordenar a mano. get() entrega el primer elemento que no esté
    pausado (ni él, ni su anime, ni la cola entera).

    Un "grupo" es un anime (su carpeta) o una película (su clave propia).
    La cola no sabe nada de disco: avisa a sus QueueListener y ellos guardan.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._entries: list[_Entry] = []
        self._paused_groups: set = set()
        self._all_paused = False
        self.current: WorkItem | None = None     # el que se está procesando ahora
        self._snapshot = ((), frozenset(), False)
        self._listeners: list[QueueListener] = []

    def add_listener(self, listener: QueueListener):
        if listener not in self._listeners:
            self._listeners.append(listener)

    # ---------------- internos ----------------
    def _changed_locked(self):
        """Con _cond cogido: copia inmutable para guardar en disco sin bloquear, y despierta a get()."""
        self._snapshot = (
            tuple((key_id(e.item.key), e.paused) for e in self._entries),
            frozenset(self._paused_groups),
            self._all_paused,
        )
        self._cond.notify_all()

    def _notify_changed(self):
        for listener in self._listeners:
            listener.on_changed()

    def _find(self, key) -> int | None:
        return next((i for i, e in enumerate(self._entries) if e.item.key == key), None)

    def _runnable(self, e: _Entry) -> bool:
        return not e.paused and e.item.group not in self._paused_groups

    def snapshot(self):
        """(((clave, pausado), ...), grupos pausados, cola pausada)."""
        return self._snapshot

    # ---------------- productor / consumidor ----------------
    def put(self, item: WorkItem, priority: int = 0, paused: bool = False, at: str | None = None):
        """at: None (según prioridad), "front" o "end"."""
        for listener in self._listeners:
            listener.on_put(item, priority)
        with self._cond:
            i = self._find(item.key)
            if i is not None:
                self._entries.pop(i)
            if at == "front":
                pos = 0
            elif at == "end":
                pos = len(self._entries)
            else:
                pos = next((j + 1 for j in range(len(self._entries) - 1, -1, -1)
                            if self._entries[j].priority <= priority), 0)
            self._entries.insert(pos, _Entry(item, priority, paused))
            self._changed_locked()
        self._notify_changed()

    def get(self) -> WorkItem:
        with self._cond:
            while True:
                if not self._all_paused:
                    for i, e in enumerate(self._entries):
                        if self._runnable(e):
                            self._entries.pop(i)
                            self.current = e.item
                            self._changed_locked()
                            return e.item
                self._cond.wait()

    def task_done(self):
        with self._cond:
            self.current = None

    # ---------------- gestión ----------------
    def view(self):
        """([(item, priority, paused)], grupos pausados, cola pausada, actual)"""
        with self._cond:
            return ([(e.item, e.priority, e.paused) for e in self._entries],
                    set(self._paused_groups), self._all_paused, self.current)

    def remove(self, keys) -> list[WorkItem]:
        keys = set(keys)
        with self._cond:
            removed = [e.item for e in self._entries if e.item.key in keys]
            self._entries = [e for e in self._entries if e.item.key not in keys]
            self._changed_locked()
        return removed

    def set_paused(self, key, paused: bool) -> bool:
        with self._cond:
            i = self._find(key)
            if i is None:
                return False
            self._entries[i].paused = paused
            self._changed_locked()
        self._notify_changed()
        return True

    def set_group_paused(self, group: str, paused: bool):
        with self._cond:
            (self._paused_groups.add if paused else self._paused_groups.discard)(group)
            self._changed_locked()
        self._notify_changed()

    def set_all_paused(self, paused: bool):
        with self._cond:
            self._all_paused = paused
            self._changed_locked()
        self._notify_changed()

    def move(self, key, how: str) -> bool:
        """Capítulo: "front" = el siguiente de toda la cola; "up"/"down" =
        intercambiarlo con el capítulo anterior/siguiente del mismo anime."""
        with self._cond:
            i = self._find(key)
            if i is None:
                return False
            if how == "front":
                self._entries.insert(0, self._entries.pop(i))
            else:
                group = self._entries[i].item.group
                same = [j for j, e in enumerate(self._entries) if e.item.group == group]
                k = same.index(i) + (-1 if how == "up" else 1)
                if not 0 <= k < len(same):
                    return False
                j = same[k]
                self._entries[i], self._entries[j] = self._entries[j], self._entries[i]
            self._changed_locked()
        self._notify_changed()
        return True

    def move_group(self, group: str, how: str) -> bool:
        """Anime completo: "front", "up", "down" o "end" entre los animes de
        la cola. Sus capítulos quedan juntos, en su orden actual."""
        with self._cond:
            order, buckets = [], {}
            for e in self._entries:
                if e.item.group not in buckets:
                    buckets[e.item.group] = []
                    order.append(e.item.group)
                buckets[e.item.group].append(e)
            if group not in buckets:
                return False
            i = order.index(group)
            order.pop(i)
            new_i = {"front": 0, "end": len(order), "up": max(i - 1, 0), "down": min(i + 1, len(order))}[how]
            order.insert(new_i, group)
            self._entries = [e for g in order for e in buckets[g]]
            self._changed_locked()
        self._notify_changed()
        return True

    def restore_flags(self, paused_groups, all_paused):
        with self._cond:
            self._paused_groups = set(paused_groups)
            self._all_paused = bool(all_paused)
            self._changed_locked()
