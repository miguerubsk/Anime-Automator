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
        if new_h and old_h and old_h <= new_h:
            try:
                f.unlink()
                removed.append(f"{f.name} ({old_h}p)")
                log.info("Borrada copia de menor calidad %s (%sp) por %s (%sp)", f, old_h, dest_file.name, new_h)
            except OSError as e:
                kept.append(f"{f.name} (no se pudo borrar: {e})")
        else:
            why = "no se pudo leer la resolución" if not (new_h and old_h) else f"{old_h}p > {new_h}p"
            kept.append(f"{f.name} ({why})")
            log.warning("No borro %s: %s", f, why)
    where = display_path(str(dest_file.parent), cfg)
    if removed:
        notify(f"🧹 {where}: sustituido por <b>{dest_file.name}</b> ({new_h}p), "
               f"borrado: {', '.join(removed)}", cfg)
    if kept:
        notify(f"⚠️ {where}: hay <b>{dest_file.name}</b> nuevo pero dejo "
               f"{', '.join(kept)}. Revísalo a mano.", cfg)
