"""
Encolado de lo que falta de un anime o película (lo usan el webhook, el bot
de administración, /download y /check).
"""

import logging
from collections.abc import Callable
from pathlib import Path

from ..core.config import load_config
from ..core.nas import assert_nas_mounted
from ..core.util import compress_ranges
from ..library import files
from ..library.folders import Library
from ..library.movies import MovieLibrary, already_have_movie, looks_like_movie_title, movie_other_copies
from ..library.naming import strip_site_suffix
from ..notify.telegram import notify
from ..sources.japanpaw import JapanPawPage, TabRanking
from .jobs import JobTracker
from .model import WorkItem, work_key
from .progress import BatchProgress
from .registry import Completed, InProgress
from .work_queue import WorkQueue

log = logging.getLogger("anime-automator")


class EnqueueRequest:
    """
    Qué se pide y a quién hay que contarle los errores.

    Con `job_id` (petición de requester-bot) los errores no van al Telegram
    propio (los recibe requester-bot en la respuesta) y los capítulos se
    siguen con el JobTracker. Con `dry_run` no se encola ni se crea nada ni
    se avisa de nada. Con `notify_fn` (p.ej. el bot de administración) los
    errores van a esa función (al chat que lo pidió).
    """

    def __init__(self, watch_url: str, watch_title: str = "", link_emision: bool = True, priority: int = 0,
                 job_id=None, kind: str | None = None, title_hint: str | None = None,
                 notify_fn: Callable[[str], None] | None = None, dry_run: bool = False, watch: bool = False):
        self.watch_url = watch_url
        self.watch_title = watch_title
        self.link_emision = link_emision
        self.priority = priority
        self.job_id = job_id
        self.kind = kind
        self.title_hint = title_hint
        self.notify_fn = notify_fn
        self.dry_run = dry_run
        self.watch = watch   # viene del webhook: se avisa también a requester-bot (trabajo observado)

    def report_error(self, msg: str, cfg: dict):
        if self.dry_run:
            return
        if self.notify_fn is not None:
            self.notify_fn(msg)
        elif self.job_id is None:
            notify(msg, cfg)


class Enqueuer:
    """
    Descarga la ficha del anime, calcula los capítulos que faltan (los
    disponibles en la web menos los que ya hay en disco o ya completados) y
    los encola TODOS. Devuelve un dict con "status" y, si procede, detalles.

    `kind` ("movie"/"series") lo puede indicar quien llama; si no, se detecta:
    sin enlaces "Capítulo N" pero con archivos de vídeo, o un único capítulo
    con "Movie"/"Gekijouban" en el título -> película. `title_hint` es el
    título "bonito" (el del post de japanpaw) para nombrar películas.

    En dry_run los estados son "nothing_new" (está todo), "in_progress" (lo
    que falta ya se está descargando) o "missing" (con la lista en "missing").
    """

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config()

    # ---------------- errores ----------------
    def _fail(self, req: EnqueueRequest, status: str, msg: str, traceback: bool = False, **extra) -> dict:
        (log.exception if traceback else log.error)(msg)
        req.report_error(msg, self.cfg)
        return {"status": status, **extra}

    # ---------------- entrada ----------------
    def run(self, req: EnqueueRequest) -> dict:
        try:
            assert_nas_mounted(self.cfg)
        except Exception as e:
            return self._fail(req, "nas_not_mounted", f"🚨 {e}")

        # la ficha se lee ANTES de resolver la carpeta: si no hay nada que
        # descargar (o es una película) no se crea una carpeta vacía
        where = f"de <b>{req.watch_title}</b>" if req.watch_title else req.watch_url
        try:
            page = JapanPawPage.fetch(req.watch_url, self.cfg)
        except Exception as e:
            return self._fail(req, "page_fetch_failed", f"❌ No se pudo descargar la página {where}: {e}", True)
        watch_title = req.watch_title or page.title
        if not watch_title:
            return self._fail(req, "no_title", f"❌ La página {req.watch_url} no tiene título; "
                                               f"no puedo deducir el nombre del anime.")

        links = page.merged_links(TabRanking.from_config(self.cfg))
        movie_title = (req.title_hint or strip_site_suffix(watch_title, self.cfg.get("site_title_suffix", ""))).strip()
        if self._is_movie(req, page, links, movie_title):
            return self._movie(req, page, movie_title)
        if not links:
            return self._fail(req, "no_tab_found",
                              f"❌ No se pudo identificar ninguna pestaña de descarga válida para <b>{watch_title}</b>")
        return self._series(req, page.text, watch_title, links)

    def run_links(self, req: EnqueueRequest, links: dict[int, str]) -> dict:
        """
        Como run(), pero con los enlaces ya sabidos ({capítulo: url}, p.ej. de la
        unidad de Drive) en vez de leerlos de la ficha: sin Chrome ni Cloudflare.
        Sin log de "nada nuevo" (se llama cada pocos minutos).
        """
        try:
            assert_nas_mounted(self.cfg)
        except Exception as e:
            return {"status": "nas_not_mounted", "error": str(e)}
        if not links:
            return {"status": "nothing_new"}
        # primero sin crear nada (ni carpetas ni enlaces de emisión): lo normal
        # en cada vuelta es que no falte nada
        try:
            folder, _name, _new = Library().resolve(self.cfg, req.watch_title, link_emision=False, create=False)
        except Exception as e:
            return {"status": "folder_resolution_failed", "error": str(e)}
        have = files.existing_episode_numbers(Path(folder)) | Completed().episodes_of(str(folder))
        target = {"folder": str(folder)}
        if all(ep in have or work_key(target, ep) in InProgress() for ep in links):
            return {"status": "nothing_new"}
        return self._series(req, "", req.watch_title, links, quiet=True)

    @staticmethod
    def _is_movie(req: EnqueueRequest, page: JapanPawPage, links: dict, movie_title: str) -> bool:
        if req.kind in ("movie", "series"):
            return req.kind == "movie"
        return (not links and bool(page.movie_candidates())) or (
            sorted(links) == [1] and looks_like_movie_title(movie_title))

    # ---------------- series ----------------
    def _series(self, req: EnqueueRequest, page_text: str, watch_title: str, links: dict,
                quiet: bool = False) -> dict:
        try:
            target_folder, anime_name, is_new_folder = Library().resolve(
                self.cfg, watch_title, link_emision=req.link_emision and not req.dry_run,
                create=not req.dry_run, page_text=page_text)
        except Exception as e:
            return self._fail(req, "folder_resolution_failed",
                              f"❌ Error resolviendo la carpeta de <b>{watch_title}</b>: {e}", True)

        target = {"folder": str(target_folder), "name": anime_name, "page_url": req.watch_url}
        folder = Path(target["folder"])
        have = files.existing_episode_numbers(folder) | Completed().episodes_of(target["folder"])
        available = sorted(links)
        missing = [ep for ep in available if ep not in have]
        lower = files.lower_quality_episodes(folder)
        info = {
            "title": watch_title, "name": anime_name, "folder": target["folder"],
            "available": len(available), "have": len(have),
            # de los que faltan, los que ya están en otro formato (se mejoran)
            "upgrade": [ep for ep in missing if ep in lower],
        }

        if not missing:
            (log.debug if quiet else log.info)("Nada nuevo que descargar para %s (ya tengo: %s)",
                                               watch_title, sorted(have))
            return {"status": "nothing_new", **info}

        if req.dry_run:
            busy = [ep for ep in missing if work_key(target, ep) in InProgress()]
            pending = [ep for ep in missing if ep not in busy]
            return {"status": "missing" if pending else "in_progress", **info,
                    "missing": pending, "already_in_progress": busy, "new_folder": is_new_folder}

        items = [WorkItem(target, watch_title, ep, links[ep]) for ep in missing]
        job = JobTracker().new_job(req.job_id, anime_name, watch_title)
        if job is None and req.watch:
            job = JobTracker().new_watch_job(req.watch_url, anime_name, watch_title)
        observed = bool(job and job.watch_url)
        queued, skipped = self._queue(items, job, req.priority)
        if queued and (not job or observed):
            BatchProgress().queued(target["folder"], anime_name, queued)
            log.info("Encolados para %s: %s", watch_title, queued)
        if skipped:
            log.info("Ya en cola/procesándose para %s: %s", watch_title, skipped)
        return {"status": "queued", **info, "queued": queued, "already_in_progress": skipped,
                "tracked": bool(job and queued and not observed)}

    @staticmethod
    def _queue(items: list[WorkItem], job, priority: int):
        """Encola los que no estén ya en curso. (encolados, ya en curso)."""
        queued, skipped = [], []
        for item in items:
            if not InProgress().try_add(item.key):
                skipped.append(item.episode)
                continue
            if job:
                JobTracker().attach(job, item.key)
            WorkQueue().put(item, priority=priority)
            queued.append(item.episode)
        if job and queued:
            JobTracker().seal(job)
        return queued, skipped

    # ---------------- películas ----------------
    def _movie(self, req: EnqueueRequest, page: JapanPawPage, title: str) -> dict:
        url = page.best_movie_link(TabRanking.from_config(self.cfg))
        if not url:
            return self._fail(req, "no_tab_found", f"❌ No hay ningún enlace de descarga para la película <b>{title}</b>",
                              kind="movie")
        folder, filename, from_series = MovieLibrary(self.cfg).destination(title)
        dest = folder / filename
        info = {"title": title, "name": title, "folder": str(folder), "file": filename,
                "kind": "movie", "series_folder": from_series, "available": 1}
        log.info("Película %r -> %s (enlace: %s)", title, dest, url)

        if already_have_movie(folder, filename):
            return {"status": "nothing_new", **info, "have": 1}
        info["upgrade"] = [1] if movie_other_copies(folder, filename) else []

        target = {"folder": str(folder), "name": Path(filename).stem if from_series else title,
                  "movie": True, "filename": filename, "key": f"movie:{dest}", "page_url": req.watch_url}
        item = WorkItem(target, title, 1, url)

        if req.dry_run:
            busy = item.key in InProgress()
            return {"status": "in_progress" if busy else "missing", **info, "have": 0,
                    "missing": [] if busy else [1]}

        job = JobTracker().new_job(req.job_id, title, title, kind="movie")
        queued, skipped = self._queue([item], job, req.priority)
        if not queued:
            return {"status": "queued", **info, "have": 0, "queued": [], "already_in_progress": [1]}
        return {"status": "queued", **info, "have": 0, "queued": [1], "already_in_progress": [],
                "tracked": bool(job)}


# ------------------------------------------------------------------ #
# Atajos para quien llama
# ------------------------------------------------------------------ #
def enqueue_from_webhook(cfg: dict, watch_title: str, watch_url: str) -> dict:
    return Enqueuer(cfg).run(EnqueueRequest(watch_url, watch_title, watch=True))


def download_whole_anime(cfg: dict, url: str, job_id=None, kind=None, title=None, notify_fn=None) -> dict:
    """Encola todos los capítulos que falten del anime (o la película) de esa URL."""
    return Enqueuer(cfg).run(EnqueueRequest(url, link_emision=False, priority=1, job_id=job_id, kind=kind,
                                            title_hint=title, notify_fn=notify_fn))


def check_library(cfg: dict, url: str, kind=None, title=None) -> dict:
    """Qué hay ya en la biblioteca, sin descargar ni crear nada."""
    return Enqueuer(cfg).run(EnqueueRequest(url, link_emision=False, kind=kind, title_hint=title, dry_run=True))


def describe_download_result(result: dict) -> str:
    """Mensaje (HTML de Telegram) para el resultado de un encolado. "" para los errores
    (ya se han notificado al producirse)."""
    st = result.get("status")
    name = result.get("name", "")
    if result.get("kind") == "movie":
        where = f"{result.get('folder', '')}/{result.get('file', '')}"
        if st == "queued" and result.get("queued"):
            return f"🎬 Película <b>{name}</b> en cola.\nDestino: {where}"
        if st == "queued":
            return f"🎬 La película <b>{name}</b> ya estaba en cola."
        if st == "nothing_new":
            return f"✅ Ya tienes la película <b>{name}</b>.\n{where}"
        return ""
    if st == "queued":
        txt = (f"📥 <b>{name}</b>: {len(result['queued'])} capítulo(s) encolados "
               f"({compress_ranges(result['queued'])}) de {result['available']} disponibles"
               f" — ya tenías {result['have']}.\nCarpeta: {result['folder']}")
        if result.get("already_in_progress"):
            txt += f"\nYa estaban en cola: {compress_ranges(result['already_in_progress'])}"
        return txt
    if st == "nothing_new":
        return (f"✅ <b>{name}</b>: ya lo tienes completo ({result['have']} de "
                f"{result['available']} capítulos disponibles).\nCarpeta: {result['folder']}")
    return ""
