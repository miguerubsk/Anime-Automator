"""
Animes en emisión vigilados directamente en la unidad de Drive de Japan-Paw.

Kuro (requester-bot) es quien sabe qué animes se siguen y cuándo se emiten:
los manda con PUT /watches, cada uno con la misma ventana de comprobación
que pone a change-detection (Schedule.to_dict(): día, minuto de inicio y
horas). Dentro de esa ventana se mira cada pocos minutos la carpeta del
anime en la unidad de emisión:

- archivos con temporada y capítulo en el nombre (S01E07): el capítulo sale
  de ahí (cada temporada empieza en el 1) y se encola directamente, sin abrir
  la ficha del paste, con el enlace del índice equivalente al archivo;
- archivos sin "SxxEyy" (numeración absoluta, "Berserk S2 - 16" = cap. 4):
  no se fía la numeración; se lee la ficha del paste, como con el webhook.

Cuando el capítulo de la semana ya está guardado (llegue por donde llegue:
Drive, ficha, webhook o torrent), se deja de mirar ese anime hasta su
siguiente ventana (sin ventana: 6 días).

change-detection sigue funcionando igual (webhook): los dos caminos llegan
al mismo Enqueuer, que no encola dos veces lo mismo.
"""

import logging
import re
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..core.config import load_config
from ..core.persistence import JsonStore
from ..core.singleton import Singleton
from ..core.background import BackgroundLoop
from ..library.files import existing_episode_numbers
from ..library.naming import name_key, strip_site_suffix
from ..sources.drive import INDEX_PREFIX, DriveError, DriveMirror
from ..sources.japanpaw import VIDEO_EXT_RE, JapanPawPage, TabRanking, parse_tab_label

log = logging.getLogger("anime-automator")

SEASON_EPISODE_RE = re.compile(r"(?<![a-z0-9])S(\d{1,2})\s?E(\d{1,4})(?!\d)", re.IGNORECASE)
NO_WINDOW_PAUSE = timedelta(days=6)
WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def parse_episode(filename: str) -> tuple[int, int] | None:
    """(temporada, capítulo) si el nombre lo dice como SxxEyy; si no, None."""
    m = SEASON_EPISODE_RE.search(filename or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


@dataclass
class Watch:
    watch_url: str
    title: str
    # ventana de Kuro: {"weekday": 0-6 (0 = lunes), "start_min", "hours"}; vacía = siempre
    schedule: dict = field(default_factory=dict)
    timezone: str = ""                                 # la de la ventana (si no, la del contenedor)
    drive_folder: str = ""                             # si no, se deduce del título
    found_folder: str = ""                             # deducida de los enlaces del paste (se recuerda)
    folder_checked_at: float = 0.0                     # cuándo se leyó el paste buscándola
    season: int | None = None                          # si no, la más alta que haya en la carpeta
    # estado
    last_poll: float = 0.0
    last_status: str = ""
    page_kicked_at: float = 0.0
    done_until: float = 0.0        # capítulo de la semana ya guardado: no se mira hasta entonces

    @classmethod
    def from_dict(cls, d: dict) -> "Watch":
        known = {f.name for f in fields(cls)}
        w = cls(**{k: v for k, v in d.items() if k in known})
        w.schedule = dict(w.schedule or {})
        if w.timezone:
            ZoneInfo(w.timezone)  # que falle aquí (400) y no en el bucle
        return w

    @property
    def limited(self) -> bool:
        s = self.schedule
        return s.get("weekday") is not None and 0 < int(s.get("hours") or 0) < 7 * 24

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.timezone)) if self.timezone else datetime.now()

    def active_at(self, when: datetime) -> bool:
        """¿Está `when` dentro de la ventana? (sin ventana: siempre)"""
        if not self.limited:
            return True
        week = 7 * 24 * 60
        start = int(self.schedule["weekday"]) % 7 * 24 * 60 + int(self.schedule.get("start_min") or 0)
        minute = when.weekday() * 24 * 60 + when.hour * 60 + when.minute
        return (minute - start) % week < int(self.schedule["hours"]) * 60

    def window_end(self, when: datetime) -> datetime | None:
        """Cuándo acaba la ventana en la que está `when` (None si está fuera o no hay ventana)."""
        if not self.limited or not self.active_at(when):
            return None
        start = int(self.schedule["weekday"]) % 7 * 24 * 60 + int(self.schedule.get("start_min") or 0)
        minute = when.weekday() * 24 * 60 + when.hour * 60 + when.minute
        elapsed = (minute - start) % (7 * 24 * 60)
        return when.replace(second=0, microsecond=0) + timedelta(minutes=int(self.schedule["hours"]) * 60 - elapsed)

    def describe(self) -> str:
        if not self.limited:
            return "siempre"
        h, m = divmod(int(self.schedule.get("start_min") or 0), 60)
        return f"{WEEKDAYS[int(self.schedule['weekday']) % 7]} {h:02d}:{m:02d}, {self.schedule['hours']} h"


class WatchRegistry(metaclass=Singleton):
    """La lista que manda Kuro (persistente: sobrevive a reinicios)."""

    store = JsonStore("watches.json", "los animes vigilados en el Drive")

    def __init__(self):
        self._lock = threading.Lock()
        raw = self.store.read({}) or {}
        self._watches = {w["watch_url"]: Watch.from_dict(w) for w in raw.get("watches", []) if w.get("watch_url")}

    def replace(self, items: list[dict]) -> list[Watch]:
        """Sustituye la lista entera (conservando el estado de los que siguen)."""
        new = {}
        for raw in items:
            url, title = (raw.get("watch_url") or "").strip(), (raw.get("title") or "").strip()
            if not url or not title:
                raise ValueError(f"cada anime necesita watch_url y title: {raw}")
            w = Watch.from_dict({**raw, "watch_url": url, "title": title})
            old = self._watches.get(url)
            if old:
                w.last_poll, w.last_status, w.page_kicked_at = old.last_poll, old.last_status, old.page_kicked_at
                w.done_until = old.done_until
                w.found_folder, w.folder_checked_at = old.found_folder, old.folder_checked_at
            new[url] = w
        with self._lock:
            self._watches = new
            self._save_locked()
        return list(new.values())

    def all(self) -> list[Watch]:
        with self._lock:
            return list(self._watches.values())

    def get(self, url: str) -> Watch | None:
        with self._lock:
            return self._watches.get(url)

    def save(self):
        with self._lock:
            self._save_locked()

    def episode_done(self, page_url: str | None, folder: str, episode: int):
        """
        Se ha guardado un capítulo: si es de un anime vigilado y es el último que
        hay (no uno atrasado), ya está el de esta semana y se deja de mirar
        hasta que acabe la ventana actual (sin ventana, NO_WINDOW_PAUSE).
        """
        w = self.get(page_url or "")
        if w is None:
            return
        have = existing_episode_numbers(Path(folder))
        if have and episode < max(have):
            return
        now = w.now()
        if w.limited:
            end = w.window_end(now)
            if end is None:  # llegó fuera de la ventana: no hay nada que pausar
                return
        else:
            end = now + NO_WINDOW_PAUSE
        with self._lock:
            w.done_until = end.timestamp()
            self._save_locked()
        log.info("Drive: %s ya tiene el capítulo %s de esta semana; no se vuelve a mirar hasta %s",
                 w.title, episode, end.strftime("%d/%m %H:%M"))

    def _save_locked(self):
        self.store.save({"watches": [asdict(w) for w in self._watches.values()]})


class DriveWatcher(BackgroundLoop, metaclass=Singleton):
    thread_name = "drive-watch"
    dirs_ttl_seconds = 3600
    folder_retry_seconds = 6 * 3600   # sin carpeta: cada cuánto se vuelve a leer el paste

    def __init__(self):
        super().__init__()
        self._dirs: list[str] = []
        self._dirs_at = 0.0
        self._poll_lock = threading.Lock()

    # ---------------- configuración ----------------
    @staticmethod
    def settings(cfg: dict) -> dict:
        w = cfg.get("drive_watch") or {}
        return {"enabled": bool(w.get("enabled")), "host": w.get("host") or "",
                "interval": float(w.get("interval_minutes") or 10) * 60,
                "max_age_days": int(w.get("max_age_days") or 21),
                "page_retry": float(w.get("page_retry_minutes") or 30) * 60,
                "page_window": float(w.get("page_window_hours") or 6) * 3600}

    # ---------------- bucle ----------------
    def step(self):
        cfg = load_config()
        s = self.settings(cfg)
        if s["enabled"]:
            for w in WatchRegistry().all():
                if w.active_at(w.now()) and time.time() >= w.done_until:
                    try:
                        self.poll(cfg, w)
                    except Exception:
                        log.exception("Fallo vigilando %s en el Drive", w.title)
        time.sleep(s["interval"] if s["enabled"] else 300)

    # ---------------- un anime ----------------
    def find_folder(self, mirror: DriveMirror, base: str, w: Watch, host: str = "") -> str | None:
        """
        La carpeta del anime en el Drive: la que diga Kuro; si no, la que se
        llame como el título; si no, la que usan los enlaces del paste (hay
        carpetas con otro nombre, p. ej. «Chii Fuyo» para un título larguísimo).
        Esa se recuerda, y si no aparece no se vuelve a leer el paste hasta
        pasadas unas horas.
        """
        if w.drive_folder:
            return w.drive_folder
        if not self._dirs or time.time() - self._dirs_at > self.dirs_ttl_seconds:
            self._dirs, self._dirs_at = mirror.list_dirs(base), time.time()
        wanted = name_key(strip_site_suffix(w.title, load_config().get("site_title_suffix", "")))
        by_name = next((d for d in self._dirs if name_key(d) == wanted), None)
        if by_name:
            return by_name
        if w.found_folder and w.found_folder in self._dirs:
            return w.found_folder
        if not host or time.time() - w.folder_checked_at < self.folder_retry_seconds:
            return None
        w.folder_checked_at = time.time()
        try:
            page = JapanPawPage.fetch(w.watch_url)
        except Exception as e:
            log.warning("No pude leer el paste de %s para buscar su carpeta: %s", w.title, e)
            return None
        links = [u for t in page.tabs() for u in t["links"].values()]
        w.found_folder = self.folder_from_links(host, links, self._dirs) or ""
        if w.found_folder:
            log.info("Carpeta de %s en el Drive (por los enlaces del paste): %s", w.title, w.found_folder)
        return w.found_folder or None

    @staticmethod
    def folder_from_links(host: str, links: list[str], dirs: list[str]) -> str | None:
        """La primera carpeta (de la raíz de la unidad) a la que apuntan los enlaces de ese host."""
        for url in links:
            p = urllib.parse.urlparse(url or "")
            path = urllib.parse.unquote(p.path)
            if (p.hostname or "").lower() != host.lower() or not path.startswith(INDEX_PREFIX):
                continue
            top = path[len(INDEX_PREFIX):].strip("/").split("/")[0]
            if top and (top in dirs or not dirs):
                return top
        return None

    def poll(self, cfg: dict, w: Watch) -> dict:
        with self._poll_lock:
            result = self._poll(cfg, w)
        w.last_poll, w.last_status = time.time(), result["status"]
        WatchRegistry().save()
        return result

    def _poll(self, cfg: dict, w: Watch) -> dict:
        from .enqueue import EnqueueRequest, Enqueuer, enqueue_from_webhook  # evita un ciclo de importación
        s = self.settings(cfg)
        mirror = DriveMirror(cfg)
        base = mirror.base_for_host(s["host"])
        if not base:
            return {"status": "no_drive", "error": f"drive_watch.host {s['host']!r} no está en drive_mirror.hosts"}
        try:
            folder = self.find_folder(mirror, base, w, s["host"])
            if not folder:
                if w.last_status != "no_folder":
                    log.warning("No encuentro la carpeta de %s en el Drive de emisión", w.title)
                return {"status": "no_folder"}
            files = mirror.list_recent_files(base, folder, s["max_age_days"])
        except DriveError as e:
            log.warning("No se pudo mirar %s en el Drive: %s", w.title, e)
            return {"status": "drive_error", "error": str(e)}

        links, unparsed = self.links_from_files(s["host"], folder, files, TabRanking.from_config(cfg), w.season)
        result = {"status": "nothing_new", "folder": folder, "drive_episodes": sorted(links)}

        if links:
            res = Enqueuer(cfg).run_links(EnqueueRequest(w.watch_url, w.title, watch=True), links)
            if res.get("queued"):
                log.info("Drive: %s tiene capítulos nuevos: %s", w.title, res["queued"])
            result.update(status=res.get("status", "error"), queued=res.get("queued", []))

        # sin "SxxEyy" no se fía la numeración: se lee la ficha del paste, pero
        # solo mientras el archivo es reciente y sin pasarse (cada page_retry)
        newest = max((self._mtime(f) for f in unparsed), default=0)
        now = time.time()
        if newest and now - newest < s["page_window"] and now - w.page_kicked_at > s["page_retry"]:
            w.page_kicked_at = now
            log.info("Drive: archivo nuevo sin SxxEyy en %s; leo la ficha", w.title)
            res = enqueue_from_webhook(cfg, w.title, w.watch_url)
            result["page"] = res.get("status")
            if res.get("queued"):
                result.update(status="queued", queued=sorted(set(result.get("queued", [])) | set(res["queued"])))
        if result["status"] != "queued":
            self._pause_if_already_have(cfg, w, files)
        return result

    def _pause_if_already_have(self, cfg: dict, w: Watch, files: list[dict]):
        """
        Nada nuevo: si lo último subido al Drive es de esta ventana y el último
        capítulo de la biblioteca se guardó DESPUÉS (se bajó por el webhook, la
        ficha o antes de reiniciar), el de esta semana ya está: a pausar. No
        depende de la numeración del Drive (vale también sin SxxEyy).
        """
        from ..library.files import episode_files
        from ..library.folders import Library
        end = w.window_end(w.now()) if w.limited else None
        if end is None:
            return
        start = end - timedelta(hours=int(w.schedule["hours"]))
        uploaded = max((self._mtime(f) for f in files if VIDEO_EXT_RE.search(f.get("Path") or "")), default=0)
        if uploaded < start.timestamp():
            return
        try:
            folder, _name, _new = Library().resolve(cfg, w.title, link_emision=False, create=False)
            eps = episode_files(Path(folder))
            if not eps:
                return
            latest = max(eps)
            saved = max(f.stat().st_mtime for f in eps[latest])
        except Exception:
            return
        if saved >= uploaded:
            WatchRegistry().episode_done(w.watch_url, str(folder), latest)

    @staticmethod
    def _mtime(f: dict) -> float:
        from datetime import datetime
        try:
            return datetime.fromisoformat(f["ModTime"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            return 0.0

    @staticmethod
    def links_from_files(host: str, folder: str, files: list[dict], ranking: TabRanking, season: int | None = None):
        """
        ({capítulo: enlace del índice}, [archivos sin SxxEyy]). Cada subcarpeta
        es una pestaña de la ficha ("[ToonsHub][Web 1080p]"); por capítulo se
        queda la mejor, igual que JapanPawPage.merged_links. Si hay archivos de
        varias temporadas, solo cuenta `season` o, si no se sabe, la más alta.
        """
        tabs: dict[str, dict] = {}
        unparsed = []
        seasons = {p[0] for f in files if VIDEO_EXT_RE.search(f.get("Path") or "")
                   for p in [parse_episode(f.get("Name") or "")] if p}
        wanted = season if season is not None else max(seasons, default=None)
        for f in files:
            path = f.get("Path") or f.get("Name") or ""
            if not VIDEO_EXT_RE.search(path):
                continue
            parsed = parse_episode(f.get("Name") or path)
            if parsed is None:
                unparsed.append(f)
                continue
            if parsed[0] != wanted:
                continue
            tab = path.split("/")[0] if "/" in path else ""
            if tab not in tabs:
                group, source, resolution = parse_tab_label(tab)
                tabs[tab] = {"group": group, "source": source, "resolution": resolution, "links": {}}
            tabs[tab]["links"][parsed[1]] = DriveMirror.url_for(host, f"{folder}/{path}")
        merged = {}
        for t in sorted(tabs.values(), key=ranking.key, reverse=True):
            merged.update(t["links"])
        return merged, unparsed

    # ---------------- para la API ----------------
    def overview(self, cfg: dict) -> list[dict]:
        now = time.time()
        return [{**asdict(w), "window": w.describe(), "active_now": w.active_at(w.now()) and now >= w.done_until,
                 "paused_until": datetime.fromtimestamp(w.done_until).isoformat(timespec="minutes")
                 if w.done_until > now else None}
                for w in WatchRegistry().all()]
