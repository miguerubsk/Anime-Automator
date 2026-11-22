"""Archivos de vídeo y carpetas de temporada de la biblioteca."""

import re
from pathlib import Path

# formatos de vídeo de la biblioteca; solo los .mkv cuentan como "ya lo tengo"
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".wmv", ".mov", ".flv", ".ogm", ".rmvb", ".mpg", ".mpeg", ".divx"}
SEASON_DIR_RE = re.compile(r"Temporada\s+\d", re.IGNORECASE)


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS


def is_numbered_video(path: Path) -> bool:
    """01.mkv, 12.mp4... (los capítulos; no las películas, OVAs, icon.ico...)."""
    return is_video(path) and re.fullmatch(r"\d+", path.stem) is not None


def is_mkv(path: Path) -> bool:
    return path.suffix.lower() == ".mkv"


def base_of(folder: Path) -> Path:
    """La carpeta de la serie, tanto si `folder` es ella como si es una "Temporada N"."""
    return folder.parent if folder.name.startswith("Temporada") else folder


def episode_files(folder: Path) -> dict[int, list[Path]]:
    """{número: [archivos]} de los NN.<ext> de vídeo de la carpeta."""
    out: dict[int, list[Path]] = {}
    try:
        files = list(folder.iterdir())
    except OSError:
        return out
    for f in files:
        if is_numbered_video(f):
            out.setdefault(int(f.stem), []).append(f)
    return out


def existing_episode_numbers(folder: Path) -> set[int]:
    """Números de episodio que ya están en .mkv en la carpeta de destino."""
    return {ep for ep, fs in episode_files(folder).items() if any(is_mkv(f) for f in fs)}


def lower_quality_episodes(folder: Path) -> set[int]:
    """Capítulos que solo están en otro formato (.mp4, .avi...): se descargan
    en .mkv y luego se borra la copia vieja si es de menor resolución."""
    return {ep for ep, fs in episode_files(folder).items() if not any(is_mkv(f) for f in fs)}


def loose_episode_files(base_folder: Path) -> list[Path]:
    """Capítulos numerados sueltos en la raíz de la serie."""
    try:
        return sorted(f for f in base_folder.iterdir() if f.is_file() and is_numbered_video(f))
    except OSError:
        return []


def has_season_dirs(base_folder: Path) -> bool:
    try:
        return any(p.is_dir() and SEASON_DIR_RE.match(p.name) for p in base_folder.iterdir())
    except OSError:
        return False


def find_season_dir(base_folder: Path, season) -> Path | None:
    """La carpeta "Temporada N" de la serie, lleve o no subtítulo ("Temporada 3 (BASURA)")."""
    rx = re.compile(rf"Temporada\s+0*{re.escape(str(season))}(?:\s*\(.*\))?", re.IGNORECASE)
    try:
        for d in sorted(base_folder.iterdir()):
            if d.is_dir() and rx.fullmatch(d.name):
                return d
    except OSError:
        pass
    return None
