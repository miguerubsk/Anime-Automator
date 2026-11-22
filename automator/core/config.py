"""Configuración (config.yaml) y variables de entorno."""

import logging
import os
import re
import threading
from pathlib import Path

import yaml

from .singleton import Singleton

log = logging.getLogger("anime-automator")

DEFAULT_CONFIG_PATH = "/config/config.yaml"
_ENV_RE = re.compile(r"\$\{(\w+)\}")


class Config(metaclass=Singleton):
    """
    config.yaml con las ${VARIABLES} sustituidas por las del entorno.

    Se puede editar sin reiniciar: se vuelve a leer en cuanto cambia la fecha
    de modificación del archivo (antes se releía en cada llamada, aunque no
    hubiera cambiado nada). Quien la usa no debe modificar el dict devuelto.
    """

    def __init__(self, path: str | None = None):
        self.path = Path(path or os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH))
        self._lock = threading.Lock()
        self._data: dict | None = None
        self._mtime = None

    def load(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            mtime = None
        with self._lock:
            if self._data is None or mtime != self._mtime:
                raw = self.path.read_text(encoding="utf-8")
                raw = _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), raw)
                self._data = yaml.safe_load(raw) or {}
                self._mtime = mtime
            return self._data


def load_config() -> dict:
    return Config().load()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def tmp_dir(cfg: dict | None = None) -> Path:
    return Path((cfg or load_config())["tmp_dir"])
