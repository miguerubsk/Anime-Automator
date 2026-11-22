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
        queue, retries, pending, in_progress = WorkQueue(), RetryScheduler(), PendingStore(), InProgress()
        queue.restore_flags(data.get("paused_groups", []), data.get("all_paused", False))
        paused = set(data.get("paused", []))
        order = [k for k in data.get("order", []) if k in records]
        now = time.time()
        n_queue = n_retry = 0

        # 1) lo que se estaba procesando o esperando un reintento (no está en
        #    el orden guardado): lo que se estaba procesando vuelve al frente
        for k, rec in records.items():
            if k in order:
                continue
            item = WorkItem.from_record(rec)
            in_progress.add(item.key)
            if rec.get("retry_at") and rec["retry_at"] > now:
                pending.restore_record(rec)
                retries.add(item, rec["retry_at"])
                n_retry += 1
            else:
                queue.put(item, priority=rec.get("priority", 0), at="front")
                n_queue += 1
        # 2) la cola, en su orden
        for k in order:
            rec = records[k]
            item = WorkItem.from_record(rec)
            in_progress.add(item.key)
            queue.put(item, priority=rec.get("priority", 0), paused=k in paused, at="end")
            n_queue += 1
        pending.persist()
        log.info("Cola recuperada tras reinicio: %s encolados, %s reintentos diferidos", n_queue, n_retry)
        notify(f"♻️ Reinicio: se recuperan {n_queue} capítulos pendientes"
               + (f" y {n_retry} reintentos programados" if n_retry else ""), load_config())
    except Exception:
        log.exception("No se pudo recuperar la cola de pendientes")
