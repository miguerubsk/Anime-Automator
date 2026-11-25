"""
Películas.

Una película de una serie que ya está en la biblioteca va a la carpeta de
esa serie (como "Película 01.mkv" / "Película - Subtítulo.mkv", igual que
las que ya hay en Overlord o Inuyasha); una película original va a
<anime_root>/<movies_dirname> con su título como nombre.
"""

import re
from pathlib import Path

from . import files
from .naming import folder_safe, name_key, norm_words, trim_name

MOVIE_MARKER_RE = re.compile(r"\b(?:the\s+)?(?:movie|gekijou?ban|pel[ií]cula|film)\b", re.IGNORECASE)
MOVIE_NUMBER_RE = re.compile(r"\b(?:movie|gekijou?ban|pel[ií]cula|film)\s*0*(\d{1,2})\b", re.IGNORECASE)
_MOVIE_PREFIX_RE = re.compile(r"^\s*pel[ií]cula\b\s*(?:-\s*)?", re.IGNORECASE)


def looks_like_movie_title(title: str) -> bool:
    return bool(MOVIE_MARKER_RE.search(title))


def movie_file_name(title: str) -> str:
    """Nombre dentro de la carpeta de la serie: "Película 02" o "Película - Reze-hen"."""
    m = MOVIE_NUMBER_RE.search(title)
    if m:
        return f"Película {int(m.group(1)):02d}.mkv"
    cut = [i for i in (title.find(":"), title.find(" - ")) if i >= 0]
    if cut:
        sub = title[min(cut):].lstrip(":- ").strip()
        sub = trim_name(MOVIE_MARKER_RE.sub("", sub))
        if sub:
            return f"Película - {folder_safe(sub)}.mkv"
    return "Película.mkv"


class MovieLibrary:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.root = Path(cfg["anime_root"])
        self.movies_dirname = cfg.get("movies_dirname", "PELICULAS")

    def find_series_folder(self, title: str) -> Path | None:
        """
        Carpeta de la biblioteca cuyo nombre sea el principio del título de la
        película ("Overlord" para "Overlord Movie 1 - Fushisha no Ou"), por
        palabras completas y quedándose con la coincidencia más larga. Se
        respetan también los overrides de config.yaml.
        """
        words = norm_words(title)
        override = self._override_where(lambda watch_title: norm_words(watch_title) == words)
        if override is not None:
            return override
        # claves de cada principio del título que acaba en palabra completa:
        # "Dandadan Movie" -> {"dandadan": 1}; así "Dan Da Dan" también casa
        prefixes = {}
        for i in range(1, len(words)):  # tiene que sobrar título (si es idéntico, es una peli original)
            prefixes.setdefault(name_key(" ".join(words[:i])), i)
        best, best_len = None, 0
        for d in self._series_dirs():
            dk = name_key(d.name)
            # mínimo 4 letras para no casar carpetas tipo "K" o "86" por casualidad
            if len(dk) >= 4 and dk in prefixes and len(dk) > best_len:
                best, best_len = d, len(dk)
        return best

    def folder_named(self, series: str) -> Path | None:
        """Carpeta de la biblioteca de esa serie (por name_key, o un override de config.yaml)."""
        key = name_key(series)
        if len(key) < 4:
            return None
        override = self._override_where(lambda watch_title: name_key(watch_title) == key)
        if override is not None:
            return override
        return next((d for d in self._series_dirs() if name_key(d.name) == key), None)

    def _override_where(self, same) -> Path | None:
        """Carpeta de la serie del primer override de config.yaml cuyo watch_title cumple `same`."""
        for a in self.cfg.get("animes") or []:
            if same(a["watch_title"]):
                return files.base_of(Path(a["folder"]))
        return None

    def _series_dirs(self) -> list[Path]:
        """Carpetas de la biblioteca salvo la de películas ([] si no se puede leer)."""
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return []
        return [d for d in entries if d.name != self.movies_dirname and d.is_dir()]

    def destination(self, title: str, series: str = "") -> tuple[Path, str, bool]:
        """
        (carpeta, nombre_archivo, es_de_una_serie). `series` es la serie según
        AniList, para las películas cuyo título no empieza por ella («Suzumiya
        Haruhi no Shoushitsu» -> «Suzumiya Haruhi no Yuuutsu/Película -
        Suzumiya Haruhi no Shoushitsu.mkv»).
        """
        series_folder = self.find_series_folder(title)
        if series_folder is not None:
            return series_folder, movie_file_name(title), True
        series_folder = self.folder_named(series) if series else None
        if series_folder is not None:
            name = movie_file_name(title)
            if name == "Película.mkv":   # sin número ni subtítulo: con su título entero
                name = f"Película - {folder_safe(title)}.mkv"
            return series_folder, name, True
        return self.root / self.movies_dirname, folder_safe(title) + ".mkv", False


def movie_copies(folder: Path, filename: str) -> list[Path]:
    """
    Archivos de vídeo de la carpeta que son esa película: el nombre exacto o
    uno equivalente (name_key) con o sin el prefijo "Película" (así se
    reconoce "Mugen Ressha-hen.mkv" aunque ahora se nombraría
    "Película - Mugen Ressha-hen.mkv").
    """
    def keys(text):
        return {k for k in (name_key(text), name_key(_MOVIE_PREFIX_RE.sub("", text))) if len(k) >= 4}

    stem = Path(filename).stem
    wanted = keys(stem)
    try:
        candidates = [f for f in folder.iterdir() if files.is_video(f) and f.is_file()]
    except OSError:
        return []
    return [f for f in candidates if f.stem == stem or keys(f.stem) & wanted]


def already_have_movie(folder: Path, filename: str) -> bool:
    """¿Ya está esa película en .mkv?"""
    return any(files.is_mkv(f) for f in movie_copies(folder, filename))


def movie_other_copies(folder: Path, filename: str) -> list[Path]:
    """Copias de la película en otros formatos (.mp4, .avi...)."""
    return [f for f in movie_copies(folder, filename) if not files.is_mkv(f)]
