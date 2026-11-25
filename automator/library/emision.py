"""Enlaces de la carpeta de emisión (Anime_Emisión) a las series que se están emitiendo."""

import logging
import os
import shlex
import threading
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

    Lo que se ve por SMB no es fiable para lo recién creado: el cliente SMB
    guarda en caché el listado de la raíz del share y lo que se crea por SSH
    no lo invalida (el enlace puede no "existir" aquí durante horas). Por eso,
    si aquí no aparece, se pregunta a la NAS antes de crear nada, y se crea
    con `ln -T` (si ya hubiera una carpeta o enlace con ese nombre, `ln -s`
    a secas crearía el enlace DENTRO de ella, roto).
    """

    # nombres que ya se sabe que existen en la NAS / de los que ya se avisó
    _known: set = set()
    _warned: set = set()
    _lock = threading.Lock()

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config()
        self.root = Path(self.cfg["emision_root"])
        self.shell = NasShell(self.cfg)
        self.nas_root = self.shell.scfg.get("emision_path") or str(self.root)

    def ensure(self, name: str, anime_folder: Path):
        with self._lock:
            if name in self._known:
                return
        if os.path.lexists(self.root / name):
            self._remember(name)
            return
        rel = os.path.relpath(anime_folder, self.root)
        nas_link = f"{self.nas_root.rstrip('/')}/{name}"
        try:
            created = self._create(name, rel)
        except Exception as e:
            log.exception("No se pudo crear el symlink de emisión para %s", name)
            with self._lock:
                if name in self._warned:
                    return
                self._warned.add(name)
            manual_cmd = f"ln -s {shlex.quote(rel)} {shlex.quote(nas_link)}"
            notify(f"⚠️ No se pudo crear el enlace de emisión de <b>{name}</b>: {e}. "
                   f"Créalo a mano en el NAS: <code>{manual_cmd}</code>", self.cfg)
            return
        self._remember(name)
        if created:
            log.info("Symlink de emisión creado en la NAS: %s -> %s", nas_link, rel)
            notify(f"🔗 Enlace de emisión creado: <b>{name}</b>", self.cfg)
        else:
            log.info("El enlace de emisión de %s ya existía en la NAS (por SMB aún no se ve)", name)

    def _remember(self, name: str):
        with self._lock:
            self._known.add(name)
            self._warned.discard(name)

    def _create(self, name: str, rel: str) -> bool:
        """Crea el enlace en la NAS. False si ya había algo con ese nombre (no se toca)."""
        if not self.shell.configured:
            raise RuntimeError("falta la sección nas_ssh (host/user) en config.yaml")
        q = shlex.quote
        out = self.shell.run(
            f"cd {q(self.nas_root)} && if [ -e {q(name)} ] || [ -L {q(name)} ]; then echo EXISTS; "
            f"else ln -sT -- {q(rel)} {q(name)} && test -e {q(name)} && echo CREATED; fi")
        if out.returncode != 0 or ("CREATED" not in out.stdout and "EXISTS" not in out.stdout):
            raise RuntimeError((out.stderr or out.stdout).strip()
                               or f"ssh terminó con código {out.returncode} (¿el enlace no resuelve? destino: {rel})")
        return "CREATED" in out.stdout
