"""Copias de menor calidad que sobran cuando llega el .mkv bueno."""

import logging
from dataclasses import dataclass
from pathlib import Path

from ..core.util import compress_ranges

from ..media.mkv import video_height
from ..notify.paths import display_path
from . import files
from .movies import movie_other_copies

log = logging.getLogger("anime-automator")


def lower_copies_for(target: dict, episode_num: int, dest_file: Path) -> list[Path]:
    """Copias en otros formatos del mismo capítulo/película que dest_file."""
    folder = dest_file.parent
    if target.get("movie"):
        return movie_other_copies(folder, dest_file.name)
    return [f for f in files.episode_files(folder).get(episode_num, []) if not files.is_mkv(f)]


@dataclass
class Replacement:
    """Lo que ha pasado con las copias viejas al llegar un .mkv bueno."""
    where: str                       # su carpeta, como se enseña en los avisos
    episode: int | None              # None en películas
    name: str                        # el archivo nuevo
    height: int | None               # su resolución
    removed: list[tuple[str, int]]   # (copia borrada, su resolución)
    kept: list[str]                  # copias que se quedan, con el porqué


def remove_lower_quality_copies(cfg: dict, dest_file: Path, others, episode: int | None = None) -> Replacement | None:
    """
    Tras colocar dest_file (.mkv), borra las otras copias del mismo capítulo o
    película si tienen MENOS O IGUAL resolución. Si una copia vieja es de más
    resolución, o no se puede leer, se deja. Devuelve qué ha hecho (None si
    no había otras copias); avisar es cosa de quien llama (replacement_notices).
    """
    others = [f for f in others if f != dest_file and f.exists()]
    if not others:
        return None
    new_h = video_height(dest_file)
    removed, kept = [], []
    for f in others:
        old_h = video_height(f)
        why = _reason_to_keep(old_h, new_h)
        if why is None:
            why = _delete_copy(f, old_h, dest_file, new_h)
        if why is None:
            removed.append((f.name, old_h))
        else:
            kept.append(f"{f.name} ({why})")
            log.warning("No borro %s: %s", f, why)
    return Replacement(display_path(str(dest_file.parent), cfg), episode, dest_file.name, new_h, removed, kept)


def _reason_to_keep(old_h: int | None, new_h: int | None) -> str | None:
    """Por qué no se borra una copia vieja (None: es de menor o igual resolución, se borra)."""
    if not (new_h and old_h):
        return "no se pudo leer la resolución"
    if old_h > new_h:
        return f"{old_h}p > {new_h}p"
    return None


def _delete_copy(f: Path, old_h: int, dest_file: Path, new_h: int) -> str | None:
    """La borra; None si se ha podido, o por qué no."""
    try:
        f.unlink()
    except OSError as e:
        return f"no se pudo borrar: {e}"
    log.info("Borrada copia de menor calidad %s (%sp) por %s (%sp)", f, old_h, dest_file.name, new_h)
    return None


def replacement_notices(done: list[Replacement]) -> list[str]:
    """Los avisos de unas cuantas sustituciones: uno por carpeta (y otro si algo se ha quedado)."""
    by_where: dict[str, list[Replacement]] = {}
    for r in done:
        by_where.setdefault(r.where, []).append(r)
    notices = []
    for where, group in by_where.items():
        notices += [n for n in (_removed_notice(where, group), _kept_notice(where, group)) if n]
    return notices


def _heights(values) -> str:
    return ", ".join(f"{h}p" for h in sorted({h for h in values if h}))


def _removed_notice(where: str, group: list[Replacement]) -> str | None:
    with_removed = [r for r in group if r.removed]
    if not with_removed:
        return None
    if len(with_removed) == 1:
        r = with_removed[0]
        return (f"🧹 {where}: sustituido por <b>{r.name}</b> ({r.height}p), "
                f"borrado: {', '.join(f'{name} ({h}p)' for name, h in r.removed)}")
    eps = [r.episode for r in with_removed if r.episode is not None]
    which = compress_ranges(eps) if len(eps) == len(with_removed) else ", ".join(r.name for r in with_removed)
    return (f"🧹 {where}: {len(with_removed)} capítulo(s) sustituidos por los .mkv "
            f"({_heights(r.height for r in with_removed)}): {which}. "
            f"Borradas las copias viejas ({_heights(h for r in with_removed for _, h in r.removed)}).")


def _kept_notice(where: str, group: list[Replacement]) -> str | None:
    with_kept = [r for r in group if r.kept]
    if not with_kept:
        return None
    new = f"<b>{with_kept[0].name}</b> nuevo" if len(with_kept) == 1 else "versión nueva"
    return (f"⚠️ {where}: hay {new} pero dejo {', '.join(k for r in with_kept for k in r.kept)}. "
            f"Revísalo a mano.")
