"""
Rutas "de usuario" en los avisos.

Internamente se trabaja con rutas del contenedor/servidor (/nuevo-pool/...),
pero a quien lee los avisos le sirve la ruta que ve por SMB (o el servicio
que sea). config.yaml -> display_paths: lista de [prefijo, sustituto]; se
aplica el primero que coincida. Lo que va entre <code>…</code> NO se toca
(p.ej. el comando ln -s que hay que ejecutar en la NAS con su ruta real).
"""

import re

from ..core.config import load_config

DEFAULT_DISPLAY_PATHS = [["/nuevo-pool/", ""]]
_CODE_RE = re.compile(r"(<code>.*?</code>)", re.S)


class PathDisplay:
    def __init__(self, rules):
        self._rules = []
        for src, dst in rules:
            src = str(src).rstrip("/")
            dst = str(dst or "").rstrip("/")
            # el prefijo seguido de "/" o de un final de ruta (espacio, fin, etc.)
            self._rules.append((re.compile(re.escape(src) + r"(/|(?=[\s<>\"'),;:]|$))"), dst))

    @classmethod
    def from_config(cls, cfg: dict | None = None) -> "PathDisplay":
        try:
            rules = (cfg or load_config()).get("display_paths") or DEFAULT_DISPLAY_PATHS
        except Exception:
            rules = DEFAULT_DISPLAY_PATHS
        return cls(rules)

    def _translate_chunk(self, chunk: str) -> str:
        for rx, dst in self._rules:
            # "/nuevo-pool/Anime/X" -> "Anime/X"; con sustituto, "Share/X"
            chunk = rx.sub(lambda m, dst=dst: (dst + m.group(1)) if dst else "", chunk)
        return chunk

    def translate(self, text, keep_code: bool = True):
        if not text:
            return text
        text = str(text)
        if not keep_code:
            return self._translate_chunk(text)
        return "".join(part if part.startswith("<code>") else self._translate_chunk(part)
                       for part in _CODE_RE.split(text))


def display_path(text, cfg: dict | None = None, keep_code: bool = True):
    """Traduce las rutas internas que aparezcan en `text` a las que ve el usuario."""
    return PathDisplay.from_config(cfg).translate(text, keep_code)
