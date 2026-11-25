"""Copias de menor calidad que sobran cuando llega el .mkv bueno."""

import logging
from pathlib import Path

from ..media.mkv import video_height
from ..notify.paths import display_path
from ..notify.telegram import notify
from . import files
from .movies import movie_other_copies

log = logging.getLogger("anime-automator")


def lower_copies_for(target: dict, episode_num: int, dest_file: Path) -> list[Path]:
    """Copias en otros formatos del mismo capítulo/película que dest_file."""
    folder = dest_file.parent
    if target.get("movie"):
        return movie_other_copies(folder, dest_file.name)
    return [f for f in files.episode_files(folder).get(episode_num, []) if not files.is_mkv(f)]


def remove_lower_quality_copies(cfg: dict, dest_file: Path, others):
    """
    Tras colocar dest_file (.mkv), borra las otras copias del mismo capítulo o
    película si tienen MENOS O IGUAL resolución. Si una copia vieja es de más
    resolución, o no se puede leer, se deja y se avisa.
    """
    others = [f for f in others if f != dest_file and f.exists()]
    if not others:
        return
    new_h = video_height(dest_file)
    removed, kept = [], []
    for f in others:
        old_h = video_height(f)
        why = _reason_to_keep(old_h, new_h)
        if why is None:
            deleted, text = _delete_copy(f, old_h, dest_file, new_h)
            (removed if deleted else kept).append(text)
        else:
            kept.append(f"{f.name} ({why})")
            log.warning("No borro %s: %s", f, why)
    _report(cfg, dest_file, new_h, removed, kept)


def _reason_to_keep(old_h: int | None, new_h: int | None) -> str | None:
    """Por qué no se borra una copia vieja (None: es de menor o igual resolución, se borra)."""
    if not (new_h and old_h):
        return "no se pudo leer la resolución"
    if old_h > new_h:
        return f"{old_h}p > {new_h}p"
    return None


def _delete_copy(f: Path, old_h: int, dest_file: Path, new_h: int) -> tuple[bool, str]:
    """(¿se ha borrado?, cómo contarlo en el aviso)."""
    try:
        f.unlink()
    except OSError as e:
        return False, f"{f.name} (no se pudo borrar: {e})"
    log.info("Borrada copia de menor calidad %s (%sp) por %s (%sp)", f, old_h, dest_file.name, new_h)
    return True, f"{f.name} ({old_h}p)"


def _report(cfg: dict, dest_file: Path, new_h: int | None, removed: list[str], kept: list[str]):
    where = display_path(str(dest_file.parent), cfg)
    if removed:
        notify(f"🧹 {where}: sustituido por <b>{dest_file.name}</b> ({new_h}p), "
               f"borrado: {', '.join(removed)}", cfg)
    if kept:
        notify(f"⚠️ {where}: hay <b>{dest_file.name}</b> nuevo pero dejo "
               f"{', '.join(kept)}. Revísalo a mano.", cfg)
