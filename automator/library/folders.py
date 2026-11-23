"""Carpeta de destino de cada anime en la biblioteca, y su organización por temporadas."""

import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from ..core.config import load_config
from ..core.singleton import Singleton
from ..notify.telegram import notify
from . import files
from .anilist import SeasonResolver, page_facts
from .emision import EmisionLinker
from .naming import clean_subtitle, folder_safe, name_key, split_season

log = logging.getLogger("anime-automator")


@dataclass
class SeasonLayout:
    base: str          # nombre de la serie (carpeta raíz)
    season: str        # "1", "2", "1.5"...
    subtitle: str      # subtítulo de la temporada, o ""
    full: str          # el título entero como carpeta (como se guardaban antes)


def find_override(cfg: dict, watch_title: str) -> Path | None:
    """Carpeta fijada a mano en config.yaml (animes:) para ese watch_title."""
    for a in cfg.get("animes") or []:
        if a["watch_title"].strip() == watch_title.strip():
            return Path(a["folder"])
    return None


def season_layout(cfg: dict, watch_title: str, page_text: str = "") -> SeasonLayout:
    """
    Nombre de la serie, temporada y subtítulo de un título de japanpaw.
    - Si el título dice la temporada ("2nd Season", "III"...), esa.
    - Si no, AniList (con la fecha de estreno y los títulos alternativos de la
      ficha para no confundirse): "Darker than Black: Ryuusei no Gemini" ->
      ("Darker than Black", "2", "Ryuusei no Gemini").
    - Si tampoco, temporada 1 con el título entero.
    """
    title, season, explicit = split_season(watch_title, cfg.get("site_title_suffix", ""))
    full = folder_safe(title)
    if not explicit:
        premiere, alts = page_facts(page_text)
        info = SeasonResolver().season_info(title, premiere, alts)
        if info:
            sub = folder_safe(clean_subtitle(info["subtitle"])) if info["subtitle"] else ""
            return SeasonLayout(folder_safe(info["base"]), info["season"], sub, full)
    return SeasonLayout(full, str(season), "", full)


class Library(metaclass=Singleton):
    """Consultas y cambios sobre las carpetas de la biblioteca (anime_root)."""

    def __init__(self):
        self._organize_lock = threading.Lock()
        self._organize_warned: set = set()

    # ---------------- búsqueda ----------------
    @staticmethod
    def find_folder(cfg: dict, name: str) -> Path | None:
        """
        Carpeta de la biblioteca que corresponde a `name`: la que se llama
        exactamente así o, si no, una cuyo nombre sea equivalente según
        name_key() (p.ej. "Dandadan" -> "Dan Da Dan"). None si no hay ninguna.
        """
        root = Path(cfg["anime_root"])
        if (root / name).is_dir():
            return root / name
        key = name_key(name)
        if not key:
            return None
        movies_dirname = cfg.get("movies_dirname", "PELICULAS")
        try:
            entries = sorted(root.iterdir())
        except OSError:
            return None
        for d in entries:
            if d.name != movies_dirname and name_key(d.name) == key and d.is_dir():
                return d
        return None

    @staticmethod
    def merge_with_earlier_year(anime_root: Path, name: str, season: int) -> str:
        """
        Algunos títulos llevan el año de emisión de cada temporada:
        "Berserk (S1) (2016)" y "Berserk (S2) (2017)". La temporada 2+ se agrupa
        en la carpeta del mismo nombre con el año anterior más cercano
        ("Berserk (2016)/Temporada 2") en vez de crear "Berserk (2017)".
        """
        m = re.fullmatch(r"(.*\S)\s+\((\d{4})\)", name)
        if not m or season <= 1 or (anime_root / name).exists():
            return name
        base, year = m.group(1), int(m.group(2))
        pattern = re.compile(re.escape(base) + r"\s+\((\d{4})\)", re.IGNORECASE)
        earlier = []
        try:
            for d in anime_root.iterdir():
                mm = pattern.fullmatch(d.name)
                if mm and int(mm.group(1)) < year and d.is_dir():
                    earlier.append((int(mm.group(1)), d.name))
        except OSError:
            return name
        return max(earlier)[1] if earlier else name

    @staticmethod
    def season_base(cfg: dict, folder: Path) -> Path | None:
        """Carpeta de la serie si `folder` es la raíz de una serie o una de sus "Temporada N"."""
        root = Path(cfg["anime_root"])
        if folder.parent == root:
            return folder
        if folder.parent.parent == root and files.SEASON_DIR_RE.match(folder.name):
            return folder.parent
        return None

    # ---------------- resolución de la carpeta de un anime ----------------
    def resolve(self, cfg: dict, watch_title: str, link_emision: bool = True, create: bool = True,
                page_text: str = "", layout: SeasonLayout | None = None):
        """
        Devuelve (target_folder: Path, anime_name: str, is_new_folder: bool).
        Prioridad: override manual en config.yaml > carpeta ya existente con un
        nombre equivalente (ver name_key) > temporada según AniList > parseo
        automático del título.

        Las temporadas sin número en el título se guardan como el resto:
        <Base>/Temporada N (<subtítulo>). Si ya estaban en una carpeta propia con
        el título entero ("Darker than Black - Ryuusei no Gemini"), se sigue
        usando esa.

        Con create=False solo calcula la ruta: no crea carpetas ni enlaces ni
        reorganiza nada (sirve para consultar qué hay ya en la biblioteca).
        Con `layout`, la serie y la temporada ya vienen decididas (búsquedas
        por torrent, que parten de una entrada concreta de AniList).
        """
        link = link_emision and create

        override = find_override(cfg, watch_title)
        if override is not None:
            base = files.base_of(override)
            if link:
                EmisionLinker(cfg).ensure(base.name, base)
            return override, base.name, False

        anime_root = Path(cfg["anime_root"])
        layout = layout or season_layout(cfg, watch_title, page_text)
        name, season, subtitle = layout.base, layout.season, layout.subtitle

        # (no vale la carpeta de la propia serie: "Seitokai Yakuindomo*" se llamaría igual)
        legacy = self.find_folder(cfg, layout.full) if subtitle and name_key(layout.full) != name_key(name) else None
        if legacy is not None:
            # temporada guardada a la antigua, en su propia carpeta
            log.info("Uso la carpeta existente %r para %r (temporada %s)", legacy.name, layout.full, season)
            if link:
                EmisionLinker(cfg).ensure(legacy.name, legacy)
            return legacy, legacy.name, False

        if season.isdigit():
            name = self.merge_with_earlier_year(anime_root, name, int(season))
        existing = self.find_folder(cfg, name)
        if existing is not None and existing.name != name:
            log.info("Uso la carpeta existente %r para %r", existing.name, name)
            name = existing.name
        base_folder = anime_root / name
        is_new = existing is None
        season_dir_name = f"Temporada {season}" + (f" ({subtitle})" if subtitle else "")

        def season_dirs_present():
            return base_folder.exists() and any(
                p.is_dir() and p.name.startswith("Temporada") for p in base_folder.iterdir())

        def season_target():
            return files.find_season_dir(base_folder, season) or base_folder / season_dir_name

        # temporada 1 sin subtítulo y sin carpetas de temporada: en la raíz, como siempre
        plain_first = season == "1" and not subtitle

        if not create:
            if season == "1" and not is_new and not season_dirs_present():
                return base_folder, name, is_new  # la primera temporada suelta en la raíz
            if plain_first and not season_dirs_present():
                return base_folder, name, is_new
            return season_target(), name, is_new

        if is_new:
            base_folder.mkdir(parents=True, exist_ok=True)
            notify(f"📁 Carpeta nueva creada automáticamente: <b>{name}</b> "
                   f"(a partir del título: \"{watch_title}\"). Revisa que el nombre sea "
                   f"correcto — si no lo es, renómbrala a mano y añade un override en "
                   f"config.yaml para que no se vuelva a crear mal.", cfg)

        if link_emision:
            EmisionLinker(cfg).ensure(name, base_folder)

        if season == "1" and not season_dirs_present() and (not is_new or plain_first):
            return base_folder, name, is_new
        target = season_target()
        target.mkdir(parents=True, exist_ok=True)
        # ha llegado otra temporada: la 1 (suelta en la raíz) a su carpeta
        self.organize_seasons(cfg, base_folder)
        return target, name, is_new

    def placement_folder(self, cfg: dict, target: dict) -> tuple[Path, Path | None, bool]:
        """
        (carpeta donde dejar el archivo, carpeta de la serie o None, ¿es un capítulo numerado?)
        Si se pidió como "la única temporada" pero mientras tanto ha llegado
        otra, el capítulo va a Temporada 1.
        """
        dest_folder = Path(target["folder"])
        base = self.season_base(cfg, dest_folder)
        numbered = not target.get("movie") and not target.get("filename")
        if numbered and base is not None and dest_folder == base and files.has_season_dirs(base):
            dest_folder = files.find_season_dir(base, 1) or base / "Temporada 1"
        return dest_folder, base, numbered

    # ---------------- organización ----------------
    def organize_seasons(self, cfg: dict, base_folder: Path) -> int:
        """
        Como en el resto de la biblioteca: con una sola temporada, los capítulos
        van en la raíz de la serie; en cuanto hay carpetas "Temporada N", los
        sueltos de la raíz son la temporada 1 y se mueven a "Temporada 1".
        Devuelve cuántos se han movido. Nunca pisa nada: si ya existe ese
        archivo en Temporada 1, lo deja donde está y avisa.
        """
        with self._organize_lock:
            if not files.has_season_dirs(base_folder):
                return 0
            loose = files.loose_episode_files(base_folder)
            if not loose:
                return 0
            t1 = files.find_season_dir(base_folder, 1)
            if t1 is not None and files.episode_files(t1):
                # ya hay una Temporada 1 con capítulos: los sueltos no son la 1
                # (p.ej. capítulos de otra temporada puestos a mano). No se tocan.
                if base_folder.name not in self._organize_warned:
                    self._organize_warned.add(base_folder.name)
                    names = ", ".join(f.name for f in loose[:6]) + ("…" if len(loose) > 6 else "")
                    notify(f"⚠️ <b>{base_folder.name}</b> tiene capítulos sueltos en la raíz ({names}) "
                           f"y además una '{t1.name}' con capítulos. No sé de qué temporada son: "
                           f"no los muevo, revísalos a mano.", cfg)
                return 0
            t1 = t1 or base_folder / "Temporada 1"
            t1.mkdir(parents=True, exist_ok=True)
            moved, clashes = 0, []
            for f in loose:
                target = t1 / f.name
                if target.exists():
                    clashes.append(f.name)
                    continue
                os.replace(f, target)  # misma NAS: renombrar, no copiar
                moved += 1
        if moved:
            log.info("Reorganizado %s: %s capítulo(s) de la raíz a %s", base_folder.name, moved, t1.name)
            notify(f"🗂️ Reorganizado <b>{base_folder.name}</b>: {moved} capítulo(s) sueltos "
                   f"de la raíz movidos a '{t1.name}', porque ya hay más temporadas.", cfg)
        if clashes:
            notify(f"⚠️ <b>{base_folder.name}</b>: {', '.join(clashes)} están a la vez en la raíz y "
                   f"en '{t1.name}'. No he movido esos: revísalos a mano.", cfg)
        return moved


def resolve_anime_folder(cfg=None, watch_title="", **kwargs):
    return Library().resolve(cfg or load_config(), watch_title, **kwargs)
