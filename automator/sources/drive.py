"""
La unidad compartida de Google Drive de Japan-Paw, por rclone.

Los enlaces del paste son de un índice web del propio Drive
(https://anime.craftervault.com/0:down/B/Berserk/[Grupo]/X - 01.mkv), así
que la ruta del archivo en la unidad sale directamente del enlace. El
índice tiene cupo de descarga ("403. Cuota Excedida") y depende de
Cloudflare (bloqueos de LaLiga); bajarlo por la API de Drive con la cuenta
propia no tiene ninguna de las dos cosas.

config.yaml:
    drive_mirror:
      enabled: true
      prefer: true        # el Drive primero y el enlace del paste de respaldo
      rclone_config: "/config/rclone/rclone.conf"
      hosts:                                  # host del índice -> remote:carpeta
        anime.craftervault.com: "japanpaw:Anime"
        emision.craftervault.com: "japanpaw-emision:"
"""

import json
import logging
import subprocess
import time
import urllib.parse
from collections import deque
from pathlib import Path

from ..core.util import url_host

log = logging.getLogger("anime-automator")

INDEX_PREFIX = "/0:down/"


class DriveError(RuntimeError):
    """rclone no pudo bajar el archivo del Drive."""


class DriveMirror:
    def __init__(self, cfg: dict):
        self.dcfg = cfg.get("drive_mirror") or {}

    @property
    def enabled(self) -> bool:
        return bool(self.dcfg.get("enabled") and self.dcfg.get("hosts"))

    @property
    def prefer(self) -> bool:
        """¿El Drive antes que el enlace del paste?"""
        return self.enabled and bool(self.dcfg.get("prefer"))

    def remote_path(self, url: str | None) -> str | None:
        """'remote:carpeta/ruta/del/archivo.mkv' para un enlace del índice, o None."""
        if not (self.enabled and url):
            return None
        base = (self.dcfg.get("hosts") or {}).get(url_host(url))
        path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        if base is None or not path.startswith(INDEX_PREFIX):
            return None
        rel = path[len(INDEX_PREFIX):].strip("/")
        if not rel or ".." in rel.split("/"):
            return None
        remote, _, folder = base.partition(":")
        folder = folder.strip("/")
        return f"{remote}:{folder}/{rel}" if folder else f"{remote}:{rel}"

    # ---------------- del Drive al enlace (para vigilar la unidad) ---------------- #
    def base_for_host(self, host: str) -> str | None:
        return (self.dcfg.get("hosts") or {}).get(host)

    @staticmethod
    def join(base: str, rel: str) -> str:
        """'japanpaw-emision:' + 'X/Y.mkv' -> 'japanpaw-emision:X/Y.mkv' (con o sin carpeta en la base)."""
        remote, _, folder = base.partition(":")
        folder = folder.strip("/")
        rel = rel.strip("/")
        return f"{remote}:{folder}/{rel}" if folder else f"{remote}:{rel}"

    @staticmethod
    def url_for(host: str, rel: str) -> str:
        """El enlace del índice para una ruta relativa a la raíz de ese host."""
        return f"https://{host}{INDEX_PREFIX}" + urllib.parse.quote(rel.strip("/"), safe="/()!&'+,;=@~[]")

    def _rclone(self, *args, timeout: float = 120) -> str:
        cmd = ["rclone", *args, "--config", self.dcfg.get("rclone_config") or "/config/rclone/rclone.conf"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            raise DriveError(f"rclone {args[0]} falló ({out.returncode}): "
                             f"{(out.stderr.strip().splitlines() or ['sin salida'])[-1]}")
        return out.stdout

    def list_dirs(self, base: str) -> list[str]:
        """Carpetas de primer nivel."""
        return [d.rstrip("/") for d in self._rclone("lsf", "--dirs-only", self.join(base, "")).splitlines() if d]

    def list_recent_files(self, base: str, folder: str, max_age_days: int) -> list[dict]:
        """Archivos de `folder` (recursivo) modificados en los últimos días: [{Path, Name, Size, ModTime}]."""
        out = self._rclone("lsjson", "-R", "--files-only", "--no-mimetype", "--max-age", f"{max_age_days}d",
                           self.join(base, folder))
        return json.loads(out or "[]")

    def download(self, remote_path: str, dest: Path, label: str = ""):
        """Baja `remote_path` a `dest`, con el progreso de rclone en el log cada 30 s."""
        cmd = ["rclone", "copyto", remote_path, str(dest),
               "--config", self.dcfg.get("rclone_config") or "/config/rclone/rclone.conf",
               "--drive-acknowledge-abuse", "--retries", "3", "--low-level-retries", "10",
               "--timeout", "5m", "--stats", "30s", "--stats-one-line", "--stats-log-level", "NOTICE"]
        log.info("Descargando del Drive %s -> %s", remote_path, dest)
        t0 = time.time()
        tail: deque[str] = deque(maxlen=5)
        with subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True) as proc:
            for line in proc.stderr:
                line = line.strip()
                if not line:
                    continue
                tail.append(line)
                if "Transferred:" in line or "NOTICE" in line:
                    log.info("Drive %s: %s", label, line.split(": ", 1)[-1])
            code = proc.wait()
        if code != 0 or not dest.exists():
            dest.unlink(missing_ok=True)
            raise DriveError(f"rclone falló ({code}): {tail[-1] if tail else 'sin salida'}")
        log.info("Descarga del Drive completada %s: %.2f GB en %.0f min",
                 label, dest.stat().st_size / 1e9, (time.time() - t0) / 60)
