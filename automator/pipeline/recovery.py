"""Arranque: limpieza de restos y recuperación de lo que quedó pendiente antes de apagarse."""

import logging
import time

from ..core.config import load_config, tmp_dir
from ..notify.telegram import notify
from .model import WorkItem
from .pending import PendingStore
from .registry import InProgress
from .retries import RetryScheduler
from .work_queue import WorkQueue

log = logging.getLogger("anime-automator")


def cleanup_stale_tmp():
    """
    Al arrancar la cola está vacía, así que cualquier raw_*/proc_* del volumen
    temporal es un resto de una ejecución interrumpida (reinicio a mitad de
    una descarga) y ocupa disco para nada.
    """
    try:
        freed = 0
        for pattern in ("raw_*.mkv", "proc_*.mkv"):
            for p in tmp_dir().glob(pattern):
                try:
                    freed += p.stat().st_size
                    p.unlink()
                except OSError:
                    pass
        if freed:
            log.info("Limpiados restos temporales de una ejecución anterior: %.2f GB", freed / 1e9)
    except Exception:
        log.exception("No se pudo limpiar el directorio temporal al arrancar")


def restore_pending_work():
    """Vuelve a encolar lo que quedó pendiente, con el mismo orden y las mismas pausas."""
    try:
        data = PendingStore.read_saved()
        records = data.get("items") or {}
        if not records:
            return
        pending = PendingStore()   # antes de encolar: así guarda lo que se va recuperando
        WorkQueue().restore_flags(data.get("paused_groups", []), data.get("all_paused", False))
        order = [k for k in data.get("order", []) if k in records]
        n_queue, n_retry = _restore_unordered(records, order)
        n_queue += _restore_queue(records, order, set(data.get("paused", [])))
        pending.persist()
        log.info("Cola recuperada tras reinicio: %s encolados, %s reintentos diferidos", n_queue, n_retry)
        notify(f"♻️ Reinicio: se recuperan {n_queue} capítulos pendientes"
               + (f" y {n_retry} reintentos programados" if n_retry else ""), load_config())
    except Exception:
        log.exception("No se pudo recuperar la cola de pendientes")


def _restore_unordered(records: dict, order: list[str]) -> tuple[int, int]:
    """
    Lo que se estaba procesando o esperando un reintento (no está en el orden
    guardado): lo que se estaba procesando vuelve al frente. (encolados, reintentos)
    """
    now = time.time()
    n_queue = n_retry = 0
    for k, rec in records.items():
        if k in order:
            continue
        item = WorkItem.from_record(rec)
        InProgress().add(item.key)
        if rec.get("retry_at") and rec["retry_at"] > now:
            PendingStore().restore_record(rec)
            RetryScheduler().add(item, rec["retry_at"])
            n_retry += 1
        else:
            WorkQueue().put(item, priority=rec.get("priority", 0), at="front")
            n_queue += 1
    return n_queue, n_retry


def _restore_queue(records: dict, order: list[str], paused: set) -> int:
    """La cola, en su orden. Devuelve cuántos se han encolado."""
    for k in order:
        rec = records[k]
        item = WorkItem.from_record(rec)
        InProgress().add(item.key)
        WorkQueue().put(item, priority=rec.get("priority", 0), paused=k in paused, at="end")
    return len(order)
