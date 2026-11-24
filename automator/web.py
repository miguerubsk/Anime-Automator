"""API HTTP (Flask): webhook de change-detection, /download, /check, /seasons, /watches, /torrent, /health."""

import hmac
import logging
import threading
import time

from flask import Flask, jsonify, request

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


def create_app() -> Flask:
    app = Flask("anime-automator")
    warmer = SeasonWarmer()

    @app.route("/webhook", methods=["POST"])
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

    @app.route("/download", methods=["POST"])
    def download_all():
        """Descarga completa de un anime (no en emisión). La usan el bot de administración y requester-bot."""
        cfg = load_config()
        payload = _payload()
        parsed = _japanpaw_request(payload)
        if parsed is None:
            return jsonify(BAD_URL[0]), BAD_URL[1]
        url, kind, title = parsed
        job_id = payload.get("job_id")
        job_id = str(job_id) if job_id not in (None, "") else None
        log.info("Descarga completa solicitada por HTTP: %s (job %s)", url, job_id)
        result = download_whole_anime(cfg, url, job_id=job_id, kind=kind, title=title)
        if job_id is None:  # las peticiones de requester-bot las notifica el propio bot
            msg = describe_download_result(result)
            if msg:
                notify(msg, cfg)
        return jsonify(_shown(result, cfg)), 200

    @app.route("/check", methods=["POST"])
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

    @app.route("/seasons", methods=["POST"])
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
        out, late = [], []
        for t in titles:
            if time.time() < deadline:
                layout = season_layout(cfg, t)
                base, season, subtitle = layout.base, layout.season, layout.subtitle
            else:
                late.append(t)
                title, num, _ = split_season(t, "")
                base, season, subtitle = folder_safe(title), str(num), ""
            entry = {"title": t, "base": base, "key": name_key(base), "season": season, "subtitle": subtitle}
            # películas, OVAs y especiales: de qué serie son, aunque el título no lo diga
            extra = SeasonResolver().series_info(t) if t not in late and time.time() < deadline else None
            if extra:
                entry.update(series=extra["series"], series_key=name_key(extra["series"]), format=extra["format"])
            out.append(entry)
        if late:
            warmer.warm(cfg, late)
        return jsonify({"seasons": out}), 200

    # ---------------- animes vigilados en el Drive (los manda Kuro) ----------------
    @app.route("/watches", methods=["PUT"])
    def watches_replace():
        """
        Lista completa de animes en seguimiento (sustituye a la anterior):
        {"watches": [{"watch_url", "title",
                      "schedule"?: {"weekday": 0-6 (0=lunes), "start_min", "hours"},
                      "timezone"?, "drive_folder"?, "season"?}]}
        """
        if not _authorized():
            return jsonify({"status": "unauthorized"}), 401
        try:
            watches = WatchRegistry().replace(list(_payload().get("watches") or []))
        except (ValueError, TypeError, KeyError) as e:
            return jsonify({"status": "bad_request", "error": str(e)}), 400
        log.info("Kuro ha actualizado los animes vigilados en el Drive: %s", len(watches))
        return jsonify({"status": "ok", "count": len(watches)}), 200

    @app.route("/watches", methods=["GET"])
    def watches_list():
        return jsonify({"watches": DriveWatcher().overview(load_config())}), 200

    @app.route("/watches/poll", methods=["POST"])
    def watches_poll():
        """Mirar ya un anime en el Drive (aunque hoy no se emita): {"watch_url"}."""
        if not _authorized():
            return jsonify({"status": "unauthorized"}), 401
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

    @app.route("/torrent/check", methods=["POST"])
    def torrent_check():
        """
        Qué capítulos hay por torrent de una entrada de AniList, sin descargar:
        {"anilist_id", "season"?}. Estados: missing, nothing_new, in_progress,
        nothing_found, unsupported, not_found, anilist_down...
        """
        if not _authorized():
            return jsonify({"status": "unauthorized"}), 401
        req, _ = _torrent_request()
        if req is None:
            return jsonify({"status": "bad_request", "error": "falta anilist_id"}), 400
        result = req.plan()
        log.info("Consulta por torrent de AniList %s: %s", req.anilist_id,
                 {k: result.get(k) for k in ("status", "name", "season", "found", "missing")})
        return jsonify(_shown(result, load_config())), 200

    @app.route("/torrent/download", methods=["POST"])
    def torrent_download():
        """Lo mismo, y pone a descargar lo que falte: {"anilist_id", "season"?, "job_id"?}."""
        if not _authorized():
            return jsonify({"status": "unauthorized"}), 401
        req, payload = _torrent_request()
        if req is None:
            return jsonify({"status": "bad_request", "error": "falta anilist_id"}), 400
        job_id = payload.get("job_id")
        result = req.start(job_id=str(job_id) if job_id not in (None, "") else None)
        log.info("Descarga por torrent de AniList %s (job %s): %s", req.anilist_id, job_id,
                 {k: result.get(k) for k in ("status", "name", "season", "queued", "not_added")})
        return jsonify(_shown(result, load_config())), 200

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"})

    return app
