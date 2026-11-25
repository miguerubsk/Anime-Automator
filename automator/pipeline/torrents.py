"""Fallback de nyaa.si: el torrent se baja con Transmission y se procesa al completarse."""

import logging
import threading
import time
from dataclasses import dataclass, field

import transmission_rpc

from ..core.background import BackgroundLoop
from ..core.config import load_config
from ..core.persistence import JsonStore
from ..core.singleton import Singleton
from ..library.naming import season_from_folder, strip_site_suffix
from ..sources.nyaa import NyaaSearch, extract_query_from_failed_url
from .model import WorkItem
from .processing import EpisodeProcessor, TorrentFileSource
from .progress import BatchProgress
from .registry import Completed, InProgress
from .reporting import report
from .watches import WatchRegistry

log = logging.getLogger("anime-automator")


class Transmission:
    """Acceso a Transmission (corre en la NAS, con su propia carpeta y ratio de siembra)."""

    @staticmethod
    def client(cfg: dict) -> transmission_rpc.Client:
        tcfg = cfg["transmission"]
        return transmission_rpc.Client(host=tcfg["host"], port=tcfg["port"],
                                       username=tcfg.get("username") or None,
                                       password=tcfg.get("password") or None)

    @classmethod
    def add(cls, cfg: dict, torrent_url: str):
        """
        Sin forzar download_dir ni seedRatioLimit: Transmission ya tiene su
        carpeta de destino y su ratio de siembra (6) — forzar los nuestros es
        lo que causaba el "Permission denied" al no coincidir con su config.
        """
        client = cls.client(cfg)
        return client, client.add_torrent(torrent_url)

    @staticmethod
    def remove(client, torrent_id, reason: str):
        try:
            client.remove_torrent(ids=[torrent_id], delete_data=True)
            log.info("Torrent %s eliminado (%s)", torrent_id, reason)
        except Exception:
            log.exception("No se pudo eliminar el torrent %s (%s)", torrent_id, reason)


@dataclass
class TrackedTorrent:
    item: WorkItem
    nyaa_title: str
    added_at: float = field(default_factory=time.time)
    processed: bool = False

    def to_dict(self) -> dict:
        return {"anime_cfg": self.item.target, "watch_title": self.item.title, "episode": self.item.episode,
                "added_at": self.added_at, "processed": self.processed, "nyaa_title": self.nyaa_title}

    @classmethod
    def from_dict(cls, d: dict) -> "TrackedTorrent":
        item = WorkItem(d["anime_cfg"], d["watch_title"], d["episode"], "")
        return cls(item, d.get("nyaa_title", ""), d.get("added_at", time.time()), d.get("processed", False))


class TorrentTracker(BackgroundLoop, metaclass=Singleton):
    """
    Torrents de fallback en curso: hash -> TrackedTorrent. Se usa el hash y no
    el id porque Transmission renumera los ids cada vez que se reinicia. Se
    guardan en disco porque la siembra hasta el ratio puede tardar días.

    Cada minuto: los completados se procesan; los que tardan demasiado se
    descartan; los procesados que ya pararon de sembrar (ratio alcanzado) se
    borran de Transmission con sus datos.

    La conexión con Transmission se abre al revisarlos, no al cargarlos: tras
    un apagón la NAS (donde vive Transmission) puede tardar más en volver que
    este servidor, y antes eso hacía que se olvidaran todos los torrents.
    """

    thread_name = "torrents"
    poll_seconds = 60
    store = JsonStore("tracked_torrents.json", "el estado de los torrents seguidos")

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._torrents: dict[str, TrackedTorrent] = {}
        self._client = None

    def client(self, cfg: dict):
        """Cliente de Transmission (se reconecta en la siguiente vuelta si no está)."""
        if self._client is None:
            try:
                self._client = Transmission.client(cfg)
            except Exception as e:
                log.warning("Transmission no responde (%s); reviso los torrents en la próxima vuelta",
                            type(e).__name__)
        return self._client

    # ---------------- estado ----------------
    def _save_locked(self):
        self.store.save({h: t.to_dict() for h, t in self._torrents.items()})

    def track(self, torrent_hash: str, tracked: TrackedTorrent):
        with self._lock:
            self._torrents[torrent_hash] = tracked
            self._save_locked()

    def forget(self, torrent_hash: str):
        with self._lock:
            self._torrents.pop(torrent_hash, None)
            self._save_locked()

    def mark_processed(self, tracked: TrackedTorrent):
        with self._lock:
            tracked.processed = True
            self._save_locked()

    def snapshot(self) -> list[tuple[str, TrackedTorrent]]:
        with self._lock:
            return list(self._torrents.items())

    def load(self):
        """Al arrancar, recupera los torrents que se estaban siguiendo."""
        try:
            data = self.store.read({}) or {}
            if not data:
                return
            with self._lock:
                for h, d in data.items():
                    tracked = TrackedTorrent.from_dict(d)
                    self._torrents[h] = tracked
                    if not tracked.processed:
                        InProgress().add(tracked.item.key)
            log.info("Recuperados %s torrents de nyaa.si en seguimiento", len(data))
        except Exception:
            log.exception("No se pudo cargar el estado de los torrents seguidos")

    # ---------------- bucle ----------------
    def step(self):
        cfg = load_config()
        torrents = self.snapshot()
        client = self.client(cfg) if torrents else None
        if client is not None:
            for torrent_hash, tracked in torrents:
                self._check(cfg, client, torrent_hash, tracked)
        time.sleep(self.poll_seconds)

    def _check(self, cfg: dict, client, torrent_hash: str, tracked: TrackedTorrent):
        t = self._fetch(client, torrent_hash, tracked)
        if t is None:
            return
        if not tracked.processed and not self._advance(cfg, client, torrent_hash, tracked, t):
            return
        if tracked.processed and "stop" in str(getattr(t, "status", "")).lower():
            # ya paró de sembrar solo (por haber alcanzado el ratio límite)
            Transmission.remove(client, torrent_hash, "alcanzó el ratio de siembra")
            self.forget(torrent_hash)

    def _fetch(self, client, torrent_hash: str, tracked: TrackedTorrent):
        """El torrent en Transmission, o None si no se puede consultar ahora o ya no está."""
        try:
            return client.get_torrent(torrent_hash)
        except KeyError:
            # ya no está en Transmission (borrado a mano): dejar de seguirlo
            log.info("El torrent %s ya no existe en Transmission, se deja de seguir", torrent_hash)
            self.forget(torrent_hash)
            if not tracked.processed:
                InProgress().discard(tracked.item.key)
        except Exception:
            log.exception("No se pudo consultar el torrent %s en Transmission", torrent_hash)
        return None

    def _advance(self, cfg: dict, client, torrent_hash: str, tracked: TrackedTorrent, t) -> bool:
        """
        Un torrent aún sin procesar: si se ha completado, se procesa; si tarda
        demasiado, se descarta. False si ya no hay nada más que hacer con él.
        """
        if (getattr(t, "percent_done", 0) or 0) >= 1.0:
            return self._process_completed(cfg, client, torrent_hash, tracked, t)
        if time.time() - tracked.added_at > cfg["transmission"].get("torrent_wait_timeout_hours", 48) * 3600:
            item = tracked.item
            self._give_up(client, torrent_hash, tracked, "timeout sin completarse",
                          f"el torrent \"{tracked.nyaa_title}\" no se completó a tiempo",
                          lambda: f"❌ (nyaa.si) El torrent de <b>{item.title}</b> Capítulo "
                                  f"{item.episode:02d} lleva demasiado tiempo sin completarse "
                                  f"(\"{tracked.nyaa_title}\"). Se elimina y se descarta."
                                  f"{BatchProgress().block()}")
            return False
        return True

    def _process_completed(self, cfg, client, torrent_hash, tracked: TrackedTorrent, t) -> bool:
        item = tracked.item
        try:
            dest = EpisodeProcessor().process(cfg, item, TorrentFileSource(t))
        except Exception as e:
            log.exception("Fallo procesando el resultado del torrent %s", torrent_hash)
            self._give_up(client, torrent_hash, tracked, "falló el procesado", f"(nyaa.si) {e}",
                          lambda err=e: f"❌ (nyaa.si) Error procesando <b>{item.title}</b> "
                                        f"{item.label}: {err}{BatchProgress().block()}")
            return False
        BatchProgress().done(item.folder, item.episode)
        report(item.key, "episode_done",
               lambda: f"✅ (nyaa.si) <b>{item.title}</b> - {item.label} "
                       f"procesado y guardado en:\n{dest}{BatchProgress().block()}",
               source="nyaa")
        self.mark_processed(tracked)
        Completed().add(item.key)
        if not item.is_movie:
            WatchRegistry().episode_done(item.target.get("page_url"), item.folder, item.episode)
        InProgress().discard(item.key)
        return True

    def _give_up(self, client, torrent_hash, tracked: TrackedTorrent, why: str, error: str, message):
        item = tracked.item
        BatchProgress().failed(item.folder, item.episode)
        report(item.key, "episode_failed", message, error=error)
        InProgress().discard(item.key)
        Transmission.remove(client, torrent_hash, why)
        self.forget(torrent_hash)


class NyaaFallback:
    """Busca el capítulo en nyaa.si y lo añade a Transmission."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def try_start(self, item: WorkItem, failed_url: str = "", reason: str = "") -> bool:
        """
        True si se ha encolado el torrent (el episodio queda "en curso" hasta
        que el TorrentTracker lo termine), False si no se encontró nada.
        `reason` es el motivo real por el que falló japan-paw (no siempre es un
        enlace caído), y se incluye en el aviso.
        """
        ep = item.episode
        raw_name = strip_site_suffix(item.title, self.cfg.get("site_title_suffix", ""))
        queries = [q for q in (extract_query_from_failed_url(failed_url, ep) if failed_url else None,
                               f"{raw_name} - {ep:02d}") if q]
        expected_season = season_from_folder(item.folder)
        link, nyaa_title = NyaaSearch(self.cfg).search(queries, ep, expected_season)
        if not link:
            log.warning("nyaa.si no tiene nada para %s temporada %s capítulo %s (queries: %s)",
                        raw_name, expected_season, ep, queries)
            return False

        _client, torrent = Transmission.add(self.cfg, link)
        TorrentTracker().track(torrent.hashString, TrackedTorrent(item, nyaa_title))
        reason_text = f" Motivo: {reason}." if reason else ""
        report(item.key, "episode_fallback",
               f"🔄 La fuente de japan-paw no sirvió para <b>{item.title}</b> Capítulo {ep:02d}.{reason_text} "
               f"Usando fallback de nyaa.si: \"{nyaa_title}\". Se procesará solo cuando termine de descargar.",
               notify_if=self.cfg["nyaa_fallback"].get("notify_on_use", True),
               reason=reason, release=nyaa_title)
        return True
