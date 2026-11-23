"""
Trabajos con seguimiento (peticiones de requester-bot).

Si /download recibe un "job_id" y está configurado REQUESTER_CALLBACK_URL,
los capítulos de esa petición no se notifican por el Telegram propio: cada
evento (capítulo hecho/fallido/reintento/fallback y el final del trabajo)
se envía a requester-bot, que avisa a quien lo pidió y al admin.

Los capítulos que llegan por el webhook de change-detection son trabajos
«observados» (watch_url): se notifican por el Telegram propio como siempre
y, además, sus eventos van a requester-bot con la URL vigilada, para que
avise a los clientes que siguen ese anime cada semana.

Tanto los trabajos como la bandeja de salida se guardan en disco: tras un
corte de luz se sigue sabiendo a qué petición pertenece cada capítulo, y los
avisos que no se llegaron a entregar se envían al volver (el bot puede
tardar en arrancar más que nosotros, así que se reintenta con paciencia).
"""

import logging
import threading
import time
from dataclasses import asdict, dataclass, field, fields

import requests

from ..core.background import BackgroundLoop
from ..core.config import env
from ..core.persistence import JsonStore
from ..core.singleton import Singleton
from ..notify.paths import display_path
from ..notify.telegram import notify

log = logging.getLogger("anime-automator")

OUTBOX_MAX_AGE_SECONDS = 48 * 3600
FINAL_EVENTS = ("episode_done", "episode_failed")


@dataclass
class Job:
    job_id: str
    name: str
    title: str
    kind: str = "series"
    pending: set = field(default_factory=set)
    done: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    sealed: bool = False      # ya no se le van a añadir más capítulos
    finished: bool = False    # ya se ha enviado job_complete
    watch_url: str = ""       # trabajo observado (webhook): no sustituye a los avisos propios

    @property
    def total(self) -> int:
        return len(self.pending) + len(self.done) + len(self.failed)

    def to_dict(self) -> dict:
        return {**asdict(self), "pending": sorted(self.pending)}

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in d.items() if k in known}
        values["pending"] = set(d.get("pending", []))
        return cls(**values)


class Outbox:
    """Cola FIFO persistente de eventos por entregar a requester-bot."""

    def __init__(self):
        self.store = JsonStore("requester_outbox.json", "la bandeja de avisos para requester-bot")
        self._items: list[dict] = []
        self._cond = threading.Condition()

    def append(self, payload: dict):
        with self._cond:
            self._items.append(payload)
            self.store.save(self._items)
            self._cond.notify()

    def wait_head(self) -> dict:
        with self._cond:
            while not self._items:
                self._cond.wait()
            return self._items[0]

    def pop(self, item: dict):
        with self._cond:
            if self._items and self._items[0] is item:
                self._items.pop(0)
                self.store.save(self._items)

    def load(self) -> int:
        items = self.store.read([]) or []
        with self._cond:
            self._items[:0] = items
            self._cond.notify()
        return len(items)

    def __len__(self):
        with self._cond:
            return len(self._items)


class JobTracker(metaclass=Singleton):
    store = JsonStore("requester_jobs.json", "el estado de las peticiones de requester-bot")

    def __init__(self):
        self.callback_url = env("REQUESTER_CALLBACK_URL")
        self.callback_secret = env("REQUESTER_CALLBACK_SECRET")
        self._lock = threading.Lock()
        self._by_key: dict[tuple, Job] = {}
        self.outbox = Outbox()

    @property
    def enabled(self) -> bool:
        return bool(self.callback_url)

    # ---------------- alta de trabajos ----------------
    def new_job(self, job_id, name: str, title: str, kind: str = "series") -> Job | None:
        """Un trabajo con seguimiento, o None si no hay petición o no hay a quién avisar."""
        if job_id is None or not self.enabled:
            return None
        return Job(str(job_id), name, title, kind)

    def new_watch_job(self, watch_url: str, name: str, title: str) -> Job | None:
        """Trabajo observado de un webhook (None si no hay requester-bot al que avisar)."""
        if not self.enabled or not watch_url:
            return None
        return Job(f"watch:{time.time_ns()}", name, title, watch_url=watch_url)

    def attach(self, job: Job, key: tuple):
        """Se registra ANTES de encolar: el worker podría terminarlo enseguida."""
        with self._lock:
            self._by_key[key] = job
            job.pending.add(key[1])

    def seal(self, job: Job):
        with self._lock:
            job.sealed = True
            self._save_locked()
            self._maybe_finish(job)  # por si ya han terminado todos mientras se encolaba

    # ---------------- eventos ----------------
    def event(self, key: tuple, event: str, **data) -> bool:
        """
        Informa a requester-bot del progreso de un episodio. Devuelve True si el
        episodio pertenece a un trabajo con seguimiento (en ese caso no hay que
        notificarlo por el Telegram propio). Los observados devuelven False.
        """
        with self._lock:
            job = self._by_key.get(key)
            if job is None:
                return False
            ep = key[1]
            if event in FINAL_EVENTS:
                self._by_key.pop(key, None)
                job.pending.discard(ep)
                (job.done if event == "episode_done" else job.failed).append(ep)
            self._emit(job, event, episode=ep, done=len(job.done), failed=len(job.failed),
                       total=job.total, **data)
            self._maybe_finish(job)
            self._save_locked()
        return not job.watch_url

    def _emit(self, job: Job, event: str, **data):
        if data.get("error"):
            data["error"] = display_path(data["error"], keep_code=False)
        extra = {"watch_url": job.watch_url} if job.watch_url else {}
        self.outbox.append({"job_id": job.job_id, "event": event, "name": job.name,
                            "kind": job.kind, **extra, **data, "_queued_at": time.time()})

    def _maybe_finish(self, job: Job):
        """Con _lock. Emite job_complete cuando ya no queda nada pendiente."""
        if job.sealed and not job.pending and not job.finished:
            job.finished = True
            self._emit(job, "job_complete", done=sorted(job.done), failed=sorted(job.failed),
                       total=len(job.done) + len(job.failed))

    # ---------------- persistencia ----------------
    def _save_locked(self):
        jobs, keys = {}, []
        for key, job in self._by_key.items():
            jobs[job.job_id] = job.to_dict()
            keys.append([list(key), job.job_id])
        self.store.save({"jobs": jobs, "keys": keys})

    def load(self):
        """Al arrancar: recupera peticiones en curso y avisos sin entregar."""
        try:
            data = self.store.read({}) or {}
            jobs = {}
            for jid, raw in (data.get("jobs") or {}).items():
                job = Job.from_dict(raw)
                # tras reiniciar ya no se van a añadir más capítulos a esa
                # petición: se sella para que el aviso final llegue siempre
                job.sealed = True
                jobs[jid] = job
            with self._lock:
                for key, jid in data.get("keys", []):
                    if jid in jobs:
                        self._by_key[tuple(key)] = jobs[jid]
            if jobs:
                log.info("Recuperadas %s peticiones de requester-bot en curso", len(jobs))
        except Exception:
            log.exception("No se pudo cargar el estado de las peticiones de requester-bot")
        try:
            n = self.outbox.load()
            if n:
                log.info("Recuperados %s avisos para requester-bot sin entregar", n)
        except Exception:
            log.exception("No se pudo cargar la bandeja de avisos para requester-bot")


class CallbackSender(BackgroundLoop, metaclass=Singleton):
    """
    Un solo hilo de envío (los eventos llegan en orden). Si requester-bot no
    responde, se reintenta el MISMO evento (para no desordenar) esperando cada
    vez más, hasta 5 min; pasadas 48 h se descarta, y si era un fallo o el
    final de una petición se avisa por el Telegram propio.
    """

    thread_name = "requester-callbacks"

    def __init__(self):
        super().__init__()
        self._attempt = 0

    def _deliver(self, tracker: JobTracker, payload: dict) -> bool:
        try:
            resp = requests.post(tracker.callback_url, json=payload, timeout=15,
                                 headers={"X-Callback-Secret": tracker.callback_secret})
            if resp.ok:
                return True
            log.warning("requester-bot respondió HTTP %s al evento %s", resp.status_code, payload)
        except Exception as e:
            log.warning("No se pudo enviar el evento a requester-bot (intento %s): %s", self._attempt + 1, e)
        return False

    def step(self):
        tracker = JobTracker()
        item = tracker.outbox.wait_head()
        payload = {k: v for k, v in item.items() if k != "_queued_at"}
        delivered = self._deliver(tracker, payload)
        too_old = time.time() - item.get("_queued_at", time.time()) > OUTBOX_MAX_AGE_SECONDS
        if not (delivered or too_old):
            self._attempt += 1
            time.sleep(min(5 * 2 ** (self._attempt - 1), 300))
            return
        tracker.outbox.pop(item)
        self._attempt = 0
        if not delivered:
            log.error("Evento para requester-bot descartado tras %s h sin poder entregarse: %s",
                      OUTBOX_MAX_AGE_SECONDS // 3600, payload)
            # fallos y finales no se pueden perder en silencio: van al Telegram propio
            # (los observados ya se avisaron por ahí)
            if not payload.get("watch_url") and payload["event"] in ("episode_failed", "job_complete"):
                notify(f"⚠️ requester-bot no responde. Evento perdido de <b>{payload.get('name')}</b>: "
                       f"{payload['event']} {payload.get('episode', '')} {payload.get('error', '')}")
