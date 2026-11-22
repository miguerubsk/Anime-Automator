"""Enlaces de la carpeta de emisión (Anime_Emisión) a las series que se están emitiendo."""

import logging
import os
import shlex
from pathlib import Path

from ..core.config import load_config
from ..core.nas import NasShell
from ..notify.telegram import notify

log = logging.getLogger("anime-automator")


class EmisionLinker:
    """
    Garantiza que exista <emision_root>/<name> -> carpeta del anime. Si ya
    existe algo con ese nombre (aunque esté roto) no se toca.

    emision_root es el share Anime_Emisión de la NAS. Por SMB la NAS no deja
    crear symlinks ("Operation not supported"), así que el enlace se crea en
    la propia NAS por SSH (config: nas_ssh). Allí los shares viven en las
    mismas rutas que aquí (/nuevo-pool/<share>), y el destino es relativo
    (../Anime/<name>), así que funciona igual visto desde la NAS o por SMB.
    """

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config()
        self.root = Path(self.cfg["emision_root"])
        self.shell = NasShell(self.cfg)
        self.nas_root = self.shell.scfg.get("emision_path") or str(self.root)

    def ensure(self, name: str, anime_folder: Path):
        if os.path.lexists(self.root / name):
            return
        rel = os.path.relpath(anime_folder, self.root)
        nas_link = f"{self.nas_root.rstrip('/')}/{name}"
        try:
            self._create(name, rel)
            log.info("Symlink de emisión creado en la NAS: %s -> %s", nas_link, rel)
            notify(f"🔗 Enlace de emisión creado: <b>{name}</b>", self.cfg)
        except Exception as e:
            log.exception("No se pudo crear el symlink de emisión para %s", name)
            manual_cmd = f"ln -s {shlex.quote(rel)} {shlex.quote(nas_link)}"
            notify(f"⚠️ No se pudo crear el enlace de emisión de <b>{name}</b>: {e}. "
                   f"Créalo a mano en el NAS: <code>{manual_cmd}</code>", self.cfg)

    def _create(self, name: str, rel: str):
        if not self.shell.configured:
            raise RuntimeError("falta la sección nas_ssh (host/user) en config.yaml")
        q = shlex.quote
        out = self.shell.run(f"cd {q(self.nas_root)} && ln -s -- {q(rel)} {q(name)} && test -e {q(name)}")
        if out.returncode != 0:
            raise RuntimeError((out.stderr or out.stdout).strip()
                               or f"ssh terminó con código {out.returncode} (¿el enlace no resuelve? destino: {rel})")
