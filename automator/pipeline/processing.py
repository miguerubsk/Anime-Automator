"""
De un archivo descargado a un capítulo en la biblioteca.

    EpisodeSource.fetch()  -> raw_*.mkv en tmp     (descarga HTTP o copia del torrent)
    EpisodeProcessor.remux -> proc_*.mkv en tmp    (solo las pistas que interesan)
    EpisodeProcessor.place -> NN.mkv en la NAS     (copia atómica, temporadas, calidad)

Los temporales se borran SIEMPRE, haya éxito o fallo, para no dejar
1-1.5 GB abandonados en /data/tmp.
"""

import hashlib
import logging
import os
import shutil
import time
from abc import ABC, abstractmethod
from pathlib import Path

from ..core.config import tmp_dir
from ..core.errors import DestinationWriteError, SourceContentError
from ..core.nas import assert_nas_mounted
from ..core.netblock import get_with_bypass
from ..core.util import format_duration, safe_name
from ..library.folders import Library
from ..library.quality import lower_copies_for, remove_lower_quality_copies
from ..media.mkv import Remuxer, mkv_track_info
from ..sources.drive import DriveError, DriveMirror
from ..media.tracks import JapanPawTrackPolicy, StrictTrackPolicy
from .model import WorkItem

log = logging.getLogger("anime-automator")

# Algunas CDNs (p.ej. craftervault.com) devuelven 403 a peticiones sin pinta
# de navegador — cabeceras mínimas para evitarlo.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Referer": "https://japan-paw.net/",
}


# ------------------------------------------------------------------ #
# Fuentes del archivo bruto (estrategia)
# ------------------------------------------------------------------ #
class EpisodeSource(ABC):
    """De dónde sale el archivo bruto y qué se puede suponer de sus pistas."""

    track_policy = StrictTrackPolicy()
    # un fallo al procesar un archivo que SÍ se descargó, ¿es culpa nuestra
    # (SourceContentError) en vez de la fuente?
    content_errors_are_ours = False

    @abstractmethod
    def fetch(self, cfg: dict, item: WorkItem) -> Path:
        """Deja el archivo bruto en tmp y devuelve su ruta (es una copia propia: se borrará)."""


class HttpSource(EpisodeSource):
    """Descarga directa del enlace de japan-paw.net."""

    track_policy = JapanPawTrackPolicy()
    content_errors_are_ours = True
    log_every_seconds = 30

    def fetch(self, cfg: dict, item: WorkItem) -> Path:
        raw = tmp_dir(cfg) / f"raw_{safe_name(item.target['name'])}_{item.episode}.mkv"
        raw.parent.mkdir(parents=True, exist_ok=True)
        name, ep = item.target["name"], item.episode
        log.info("Descargando %s -> %s", item.url, raw)
        with get_with_bypass(item.url, cfg, stream=True, timeout=60, headers=BROWSER_HEADERS) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            t0 = last_log = time.time()
            with open(raw, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last_log >= self.log_every_seconds:
                        last_log = now
                        speed = done / max(now - t0, 1) / 1e6
                        pct = f" ({done * 100 // total}%)" if total else ""
                        log.info("Descargando %s cap. %s: %.2f GB%s a %.1f MB/s", name, ep, done / 1e9, pct, speed)
            log.info("Descarga completada %s cap. %s: %.2f GB en %s",
                     name, ep, done / 1e9, format_duration(time.time() - t0))
        return raw


class DriveSource(HttpSource):
    """El mismo archivo del enlace, bajado de la unidad de Drive de Japan-Paw (sin cupo)."""

    def fetch(self, cfg: dict, item: WorkItem) -> Path:
        mirror = DriveMirror(cfg)
        remote = mirror.remote_path(item.url)
        if not remote:
            raise DriveError(f"el enlace no corresponde a ninguna unidad de Drive: {item.url}")
        raw = tmp_dir(cfg) / f"raw_{safe_name(item.target['name'])}_{item.episode}.mkv"
        raw.parent.mkdir(parents=True, exist_ok=True)
        mirror.download(remote, raw, f"{item.target['name']} cap. {item.episode}")
        return raw


def source_for(item: WorkItem) -> EpisodeSource:
    """Por dónde se baja este capítulo: el enlace del paste o, si toca, el Drive."""
    return DriveSource() if item.target.get("via_drive") else HttpSource()


def torrent_files(torrent) -> list[dict]:
    """Normaliza la lista de archivos de un torrent entre versiones de transmission-rpc."""
    raw = torrent.get_files() if hasattr(torrent, "get_files") else torrent.files()
    return [{"name": f["name"], "size": f["size"]} if isinstance(f, dict) else {"name": f.name, "size": f.size}
            for f in raw]


class TorrentFileSource(EpisodeSource):
    """
    El .mkv más grande de un torrent ya completado. Se usa la carpeta que el
    propio Transmission reporta (torrent.download_dir): como Transmission
    corre en la NAS y este contenedor la monta en las mismas rutas, vale tal cual.
    """

    def __init__(self, torrent):
        self.torrent = torrent

    def fetch(self, cfg: dict, item: WorkItem) -> Path:
        mkv_files = [f for f in torrent_files(self.torrent) if f["name"].lower().endswith(".mkv")]
        if not mkv_files:
            raise RuntimeError("El torrent no contiene ningún archivo .mkv")
        main = max(mkv_files, key=lambda f: f["size"])
        src_path = Path(self.torrent.download_dir) / main["name"]
        if not src_path.exists():
            raise RuntimeError(f"No encuentro el archivo descargado en {src_path}")
        raw = tmp_dir(cfg) / f"raw_torrent_{safe_name(item.target['name'])}_{item.episode}.mkv"
        raw.parent.mkdir(parents=True, exist_ok=True)
        # se copia (nunca se mueve/borra el original) para no romper la siembra
        shutil.copy2(src_path, raw)
        return raw


# ------------------------------------------------------------------ #
# Remux y colocación
# ------------------------------------------------------------------ #
class EpisodeProcessor:
    def __init__(self, remuxer: Remuxer | None = None):
        self.remuxer = remuxer or Remuxer()

    @staticmethod
    def processed_path(cfg: dict, target: dict, episode: int) -> Path:
        """Nombre único del remux (dos temporadas del mismo anime pueden coincidir
        en nombre y número mientras una se coloca y la otra se descarga)."""
        tag = hashlib.sha1(str(target.get("folder", "")).encode()).hexdigest()[:6]
        return tmp_dir(cfg) / f"proc_{safe_name(target['name'])}_{tag}_{episode}.mkv"

    def remux(self, cfg: dict, item: WorkItem, raw: Path, policy) -> Path:
        """Remuxa `raw` (que se borra siempre) y devuelve el .mkv listo para colocar."""
        processed = self.processed_path(cfg, item.target, item.episode)
        try:
            selection = policy.select(mkv_track_info(raw), cfg)
            self.remuxer.remux(raw, processed, selection)
            return processed
        except BaseException:
            processed.unlink(missing_ok=True)
            raise
        finally:
            raw.unlink(missing_ok=True)

    def prepare(self, cfg: dict, item: WorkItem, source: EpisodeSource) -> Path:
        """Fuente -> remux listo para colocar."""
        raw = source.fetch(cfg, item)
        try:
            return self.remux(cfg, item, raw, source.track_policy)
        except Exception as e:
            if source.content_errors_are_ours:
                raise SourceContentError(str(e)) from e
            raise

    def place(self, cfg: dict, item: WorkItem, processed: Path) -> Path:
        """Copia el remux a su carpeta de la NAS (y lo borra de tmp siempre)."""
        try:
            assert_nas_mounted(cfg)  # última comprobación antes de escribir en destino
            library = Library()
            dest_folder, base, numbered = library.placement_folder(cfg, item.target)
            dest_file = dest_folder / (item.target.get("filename") or f"{item.episode:02d}.mkv")
            self._atomic_copy(processed, dest_file)
            if numbered and base is not None:
                try:
                    library.organize_seasons(cfg, base)
                except Exception:
                    log.exception("Fallo reorganizando las temporadas de %s", base)
            try:
                remove_lower_quality_copies(cfg, dest_file, lower_copies_for(item.target, item.episode, dest_file))
            except Exception:
                log.exception("Fallo revisando copias de menor calidad de %s", dest_file)
            return dest_file
        finally:
            processed.unlink(missing_ok=True)

    @staticmethod
    def _atomic_copy(src: Path, dest_file: Path):
        """A un .part en la propia carpeta y luego renombrar: nunca queda un NN.mkv a medias."""
        part = dest_file.parent / f".{dest_file.name}.part"
        try:
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, part)
            os.replace(part, dest_file)
        except OSError as e:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            raise DestinationWriteError(f"No se pudo escribir {dest_file} en la NAS: {e}") from e

    def process(self, cfg: dict, item: WorkItem, source: EpisodeSource) -> Path:
        """Todo seguido (lo usa el camino de torrents)."""
        return self.place(cfg, item, self.prepare(cfg, item, source))
