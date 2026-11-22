"""
Persistencia de la cola de pendientes (encolados + reintentos diferidos).

Si se corta la luz y el SAI apaga el servidor, al arrancar se recupera todo
lo que estaba pendiente (con su orden y sus pausas) en vez de perderlo.
"""

import threading

from ..core.persistence import JsonStore
from ..core.singleton import Singleton
from .model import WorkItem, key_id
from .work_queue import QueueListener, WorkQueue


class PendingStore(QueueListener, metaclass=Singleton):
    """
    Todo capítulo pendiente, esté en la cola o esperando un reintento.
    Escucha a la WorkQueue (patrón observador) para guardar el orden y las
    pausas en cuanto cambian.
    """

    store = JsonStore("pending_work.json", "la cola de pendientes")

    def __init__(self):
        self._lock = threading.Lock()
        self._records: dict[str, dict] = {}
        WorkQueue().add_listener(self)

    # ---------------- QueueListener ----------------
    def on_put(self, item: WorkItem, priority: int):
        # el guardado llega en on_changed(), ya con el elemento en la cola
        self.upsert(item, priority)

    def on_changed(self):
        self.persist()

    # ---------------- API ----------------
    def upsert(self, item: WorkItem, priority: int | None = None, retry_at: float | None = None):
        k = key_id(item.key)
        with self._lock:
            prev = self._records.get(k, {})
            self._records[k] = item.to_record(prev.get("priority", 0) if priority is None else priority, retry_at)
            if retry_at is not None:
                self._save_locked()

    def restore_record(self, record: dict):
        """Mete tal cual un registro recuperado de disco (sin guardar)."""
        with self._lock:
            self._records[key_id(WorkItem.from_record(record).key)] = record

    def remove(self, item: WorkItem):
        with self._lock:
            if self._records.pop(key_id(item.key), None) is not None:
                self._save_locked()

    def priority_of(self, item: WorkItem) -> int:
        with self._lock:
            return self._records.get(key_id(item.key), {}).get("priority", 0)

    def persist(self):
        with self._lock:
            self._save_locked()

    def _save_locked(self):
        order, paused_groups, all_paused = WorkQueue().snapshot()
        self.store.save({
            "order": [k for k, _ in order],
            "paused": [k for k, p in order if p],
            "paused_groups": sorted(paused_groups),
            "all_paused": all_paused,
            "items": self._records,
        })

    @classmethod
    def read_saved(cls) -> dict:
        """El estado guardado, normalizado (acepta el formato antiguo: solo el dict de capítulos)."""
        data = cls.store.read({}) or {}
        if data and "items" not in data:
            data = {"items": data, "order": list(data.keys())}
        return data
