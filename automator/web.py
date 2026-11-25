"""API HTTP (Flask): webhook de change-detection, /download, /check, /seasons, /watches, /torrent, /health."""

import functools
import hmac
import logging
import threading
import time

from flask import Flask, current_app, jsonify, request

from .core.config import env, load_config
from .core.util import is_japanpaw_url
from .library.anilist import SeasonResolver
from .library.folders import season_layout
from .library.naming import folder_safe, name_key, split_season
from .notify.paths import display_path
from .notify.telegram import notify
from .pipeline.enqueue import check_library, describe_download_result, download_whole_anime, enqueue_from_webhook
from .pipeline.torrent_requests import TorrentRequest
from .pipeline.watches import DriveWatcher, WatchRegistry

log = logging.getLogger("anime-automator")

BAD_URL = ({"status": "bad_url", "error": "se espera una URL de japan-paw.net"}, 400)


def _payload() -> dict:
    return request.get_json(force=True, silent=True) or {}


def _japanpaw_request(payload: dict):
    """(url, kind, title) de una petición /download o /check, o None si la URL no vale."""
    url = (payload.get("url") or "").strip()
    if not is_japanpaw_url(url):
        return None
    kind = payload.get("kind") if payload.get("kind") in ("movie", "series") else None
    title = (payload.get("title") or "").strip() or None
    return url, kind, title


def _authorized() -> bool:
    """Si hay REQUESTER_CALLBACK_SECRET, quien cambia cosas tiene que mandarlo (el mismo que usa Kuro)."""
    secret = env("REQUESTER_CALLBACK_SECRET")
    return not secret or hmac.compare_digest(request.headers.get("X-Callback-Secret", ""), secret)


def _shown(result: dict, cfg: dict) -> dict:
    """El resultado con la carpeta tal como la ve el usuario."""
    shown = dict(result)
    if shown.get("folder"):
        shown["folder"] = display_path(shown["folder"], cfg, keep_code=False)
    return shown


class SeasonWarmer:
    """Lo que no dio tiempo a consultar en /seasons se sigue consultando para dejarlo en caché."""

    def __init__(self):
        self._lock = threading.Lock()
        self._busy: set = set()

    def warm(self, cfg: dict, titles):
        threading.Thread(target=self._run, args=(cfg, titles), daemon=True).start()

    def _run(self, cfg, titles):
        for t in titles:
            with self._lock:
                if t in self._busy:
                    continue
                self._busy.add(t)
            try:
                season_layout(cfg, t)
            except Exception:
                log.exception("Fallo precalculando la temporada de %r", t)
            finally:
                with self._lock:
                    self._busy.discard(t)


def _requires_secret(view):
    """La ruta responde 401 si falta el secreto de REQUESTER_CALLBACK_SECRET (ver _authorized)."""
    @functools.wraps(view)
    def guarded():
        if not _authorized():
            return jsonify({"status": "unauthorized"}), 401
        return view()
    return guarded


def _job_id(payload: dict) -> str | None:
    job_id = payload.get("job_id")
    return str(job_id) if job_id not in (None, "") else None


def webhook():
    cfg = load_config()
    payload = _payload()
    log.info("Payload recibido: %s", payload)
    watch_title = payload.get("watch_title", "")
    watch_url = payload.get("watch_url", "")
    log.info("Webhook recibido (title): %s", watch_title)
    log.info("Webhook recibido (url): %s", watch_url)
    if "japan-paw.net" not in watch_url:
        log.info("Ignorando webhook de %s (no es un anime de japan-paw.net)", watch_url)
        return jsonify({"status": "ignored"}), 200
    return jsonify(enqueue_from_webhook(cfg, watch_title, watch_url)), 200


def download_all():
    """Descarga completa de un anime (no en emisión). La usan el bot de administración y requester-bot."""
    cfg = load_config()
    payload = _payload()
    parsed = _japanpaw_request(payload)
    if parsed is None:
        return jsonify(BAD_URL[0]), BAD_URL[1]
    url, kind, title = parsed
    job_id = _job_id(payload)
    log.info("Descarga completa solicitada por HTTP: %s (job %s)", url, job_id)
    result = download_whole_anime(cfg, url, job_id=job_id, kind=kind, title=title)
    if job_id is None:  # las peticiones de requester-bot las notifica el propio bot
        msg = describe_download_result(result)
        if msg:
            notify(msg, cfg)
    return jsonify(_shown(result, cfg)), 200


def check():
    """Qué hay ya en la biblioteca, sin descargar ni crear nada (lo usa requester-bot)."""
    cfg = load_config()
    parsed = _japanpaw_request(_payload())
    if parsed is None:
        return jsonify(BAD_URL[0]), BAD_URL[1]
    url, kind, title = parsed
    result = check_library(cfg, url, kind=kind, title=title)
    log.info("Consulta de biblioteca para %s: %s", url,
             {k: result.get(k) for k in ("status", "name", "have", "available")})
    return jsonify(_shown(result, cfg)), 200


def seasons_endpoint():
    """
    Temporada de cada título de japanpaw (para que requester-bot marque los
    resultados como T1, T2... y ofrezca "todas"). Cuerpo: {"titles": [...]}.
    Respuesta: {"seasons": [{"title", "base", "key", "season", "subtitle"}]},
    en el mismo orden; las películas, OVAs y especiales de una serie llevan
    además "series", "series_key" y "format" (lo dice AniList). Se corta a los `budget` segundos (AniList es lento la
    primera vez; luego tira de caché) y lo que falte va sin AniList.
    """
    cfg = load_config()
    payload = _payload()
    titles = [str(t) for t in (payload.get("titles") or [])][:12]
    deadline = time.time() + min(float(payload.get("budget") or 15), 40)
    entries = [_season_entry(cfg, t, deadline) for t in titles]
    late = [t for t, (_, in_time) in zip(titles, entries) if not in_time]
    if late:
        current_app.extensions["season_warmer"].warm(cfg, late)
    return jsonify({"seasons": [entry for entry, _ in entries]}), 200


def _season_layout_by_deadline(cfg: dict, title: str, deadline: float):
    """(base, temporada, subtítulo, a_tiempo): con AniList si aún hay tiempo; si no, solo por el título."""
    if time.time() < deadline:
        layout = season_layout(cfg, title)
        return layout.base, layout.season, layout.subtitle, True
    name, num, _ = split_season(title, "")
    return folder_safe(name), str(num), "", False


def _season_entry(cfg: dict, title: str, deadline: float) -> tuple[dict, bool]:
    """La entrada de /seasons de un título y si dio tiempo a consultarlo en AniList."""
    base, season, subtitle, in_time = _season_layout_by_deadline(cfg, title, deadline)
    entry = {"title": title, "base": base, "key": name_key(base), "season": season, "subtitle": subtitle}
    # películas, OVAs y especiales: de qué serie son, aunque el título no lo diga
    extra = SeasonResolver().series_info(title) if in_time and time.time() < deadline else None
    if extra:
        entry.update(series=extra["series"], series_key=name_key(extra["series"]), format=extra["format"])
    return entry, in_time


# ---------------- animes vigilados en el Drive (los manda Kuro) ----------------
@_requires_secret
def watches_replace():
    """
    Lista completa de animes en seguimiento (sustituye a la anterior):
    {"watches": [{"watch_url", "title",
                  "schedule"?: {"weekday": 0-6 (0=lunes), "start_min", "hours"},
                  "timezone"?, "drive_folder"?, "season"?}]}
    """
    try:
        watches = WatchRegistry().replace(list(_payload().get("watches") or []))
    except (ValueError, TypeError, KeyError) as e:
        return jsonify({"status": "bad_request", "error": str(e)}), 400
    log.info("Kuro ha actualizado los animes vigilados en el Drive: %s", len(watches))
    return jsonify({"status": "ok", "count": len(watches)}), 200


def watches_list():
    return jsonify({"watches": DriveWatcher().overview(load_config())}), 200


@_requires_secret
def watches_poll():
    """Mirar ya un anime en el Drive (aunque hoy no se emita): {"watch_url"}."""
    w = WatchRegistry().get((_payload().get("watch_url") or "").strip())
    if w is None:
        return jsonify({"status": "unknown_watch"}), 404
    return jsonify(DriveWatcher().poll(load_config(), w)), 200


# ---------------- animes enteros por torrent (los pide Kuro) ----------------
def _torrent_request():
    payload = _payload()
    try:
        anilist_id = int(payload.get("anilist_id"))
    except (TypeError, ValueError):
        return None, payload
    return TorrentRequest(load_config(), anilist_id, payload.get("season")), payload


MISSING_ANILIST_ID = ({"status": "bad_request", "error": "falta anilist_id"}, 400)


@_requires_secret
def torrent_check():
    """
    Qué capítulos hay por torrent de una entrada de AniList, sin descargar:
    {"anilist_id", "season"?}. Estados: missing, nothing_new, in_progress,
    nothing_found, unsupported, not_found, anilist_down...
    """
    req, _ = _torrent_request()
    if req is None:
        return jsonify(MISSING_ANILIST_ID[0]), MISSING_ANILIST_ID[1]
    result = req.plan()
    log.info("Consulta por torrent de AniList %s: %s", req.anilist_id,
             {k: result.get(k) for k in ("status", "name", "season", "found", "missing")})
    return jsonify(_shown(result, load_config())), 200


@_requires_secret
def torrent_download():
    """Lo mismo, y pone a descargar lo que falte: {"anilist_id", "season"?, "job_id"?}."""
    req, payload = _torrent_request()
    if req is None:
        return jsonify(MISSING_ANILIST_ID[0]), MISSING_ANILIST_ID[1]
    result = req.start(job_id=_job_id(payload))
    log.info("Descarga por torrent de AniList %s (job %s): %s", req.anilist_id, payload.get("job_id"),
             {k: result.get(k) for k in ("status", "name", "season", "queued", "not_added")})
    return jsonify(_shown(result, load_config())), 200


def health():
    return jsonify({"status": "ok"})


ROUTES = (
    ("/webhook", "POST", webhook),
    ("/download", "POST", download_all),
    ("/check", "POST", check),
    ("/seasons", "POST", seasons_endpoint),
    ("/watches", "PUT", watches_replace),
    ("/watches", "GET", watches_list),
    ("/watches/poll", "POST", watches_poll),
    ("/torrent/check", "POST", torrent_check),
    ("/torrent/download", "POST", torrent_download),
    ("/health", "GET", health),
)


def create_app() -> Flask:
    app = Flask("anime-automator")
    app.extensions["season_warmer"] = SeasonWarmer()
    for rule, method, view in ROUTES:
        app.add_url_rule(rule, view_func=view, methods=[method])
    return app
