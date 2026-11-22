"""Archivos JSON de estado en el volumen persistente (sobreviven a reinicios y apagones)."""

import json
import logging
import os
from pathlib import Path

from .config import tmp_dir

log = logging.getLogger("anime-automator")


class JsonStore:
    """
    Un archivo JSON en tmp_dir, escrito de forma atómica (primero a .tmp y
    luego os.replace), así un corte de luz a mitad de escritura nunca deja el
    archivo a medias: o queda el anterior o el nuevo.

    Antes cada estado (cola, torrents, peticiones, bandeja de avisos, caché
    de AniList) repetía esta misma lógica a su manera.
    """

    def __init__(self, filename: str, what: str, *, indent: int | None = 1, ensure_ascii: bool = False):
        self.filename = filename
        self.what = what  # para los mensajes de log: "la cola de pendientes", etc.
        self._dump_kwargs = {"indent": indent, "ensure_ascii": ensure_ascii, "default": str}

    @property
    def path(self) -> Path:
        return tmp_dir() / self.filename

    def exists(self) -> bool:
        return self.path.exists()

    def read(self, default=None):
        """Contenido del archivo, o `default` si no existe. Un JSON corrupto lanza ValueError."""
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return default
        return json.loads(text)

    def write(self, data):
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, **self._dump_kwargs))
        os.replace(tmp, path)

    def save(self, data) -> bool:
        """Como write(), pero un fallo se registra en el log en vez de propagarse."""
        try:
            self.write(data)
            return True
        except Exception:
            log.exception("No se pudo guardar %s", self.what)
            return False
