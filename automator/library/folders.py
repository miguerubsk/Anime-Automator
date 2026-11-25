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
from .anilist import SeasonResolver, page_facts, split_title
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
      La serie es la del título sin la temporada: "Gate: Jieitai… Part 2" va
      con "Gate: Jieitai…", que según AniList es la serie "Gate".
    - Si no, AniList (con la fecha de estreno y los títulos alternativos de la
      ficha para no confundirse): "Darker than Black: Ryuusei no Gemini" ->
      ("Darker than Black", "2", "Ryuusei no Gemini").
    - Si tampoco, temporada 1 con el título entero.
    """
    title, season, explicit = split_season(watch_title, cfg.get("site_title_suffix", ""))
    full = folder_safe(title)
    premiere, alts = page_facts(page_text)
    if explicit:
        # sin subtítulo, AniList no partiría el nombre: no hace falta preguntar
        info = SeasonResolver().season_info(title, premiere, alts) if split_title(title)[1] else None
        return SeasonLayout(folder_safe(info["base"]) if info else full, str(season), "", full)
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
        earlier = _earlier_years(anime_root, m.group(1), int(m.group(2)))
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
            return self._existing(cfg, override, files.base_of(override), link)

        layout = layout or season_layout(cfg, watch_title, page_text)
        legacy = self._legacy_folder(cfg, layout)
        if legacy is not None:
            # temporada guardada a la antigua, en su propia carpeta
            log.info("Uso la carpeta existente %r para %r (temporada %s)", legacy.name, layout.full, layout.season)
            return self._existing(cfg, legacy, legacy, link)

        name, is_new = self._series_name(cfg, layout)
        spot = SeasonSpot(Path(cfg["anime_root"]) / name, layout.season, layout.subtitle)
        if not create:
            return (spot.base_folder if spot.in_root(is_new) else spot.target()), name, is_new
        return self._create(cfg, watch_title, spot, is_new, link_emision), name, is_new

    @staticmethod
    def _existing(cfg: dict, folder: Path, base: Path, link: bool):
        """Resultado de resolve() para una carpeta que ya está decidida (override o temporada a la antigua)."""
        if link:
            EmisionLinker(cfg).ensure(base.name, base)
        return folder, base.name, False

    def _legacy_folder(self, cfg: dict, layout: SeasonLayout) -> Path | None:
        """Carpeta propia con el título entero de una temporada con subtítulo, si ya existe."""
        # (no vale la carpeta de la propia serie: "Seitokai Yakuindomo*" se llamaría igual)
        if not layout.subtitle or name_key(layout.full) == name_key(layout.base):
            return None
        return self.find_folder(cfg, layout.full)

    def _series_name(self, cfg: dict, layout: SeasonLayout) -> tuple[str, bool]:
        """(nombre de la carpeta de la serie, ¿hay que crearla?)."""
        name = layout.base
        if layout.season.isdigit():
            name = self.merge_with_earlier_year(Path(cfg["anime_root"]), name, int(layout.season))
        existing = self.find_folder(cfg, name)
        if existing is not None and existing.name != name:
            log.info("Uso la carpeta existente %r para %r", existing.name, name)
            name = existing.name
        return name, existing is None

    def _create(self, cfg: dict, watch_title: str, spot: "SeasonSpot", is_new: bool, link_emision: bool) -> Path:
        """Crea lo que haga falta (serie, enlace de emisión, carpeta de temporada) y devuelve el destino."""
        base_folder, name = spot.base_folder, spot.base_folder.name
        if is_new:
            base_folder.mkdir(parents=True, exist_ok=True)
            notify(f"📁 Carpeta nueva creada automáticamente: <b>{name}</b> "
                   f"(a partir del título: \"{watch_title}\"). Revisa que el nombre sea "
                   f"correcto — si no lo es, renómbrala a mano y añade un override en "
                   f"config.yaml para que no se vuelva a crear mal.", cfg)

        if link_emision:
            EmisionLinker(cfg).ensure(name, base_folder)

        if spot.in_root(is_new):
            return base_folder
        target = spot.target()
        target.mkdir(parents=True, exist_ok=True)
        # ha llegado otra temporada: la 1 (suelta en la raíz) a su carpeta
        self.organize_seasons(cfg, base_folder)
        return target

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
                self._warn_unknown_season(cfg, base_folder, t1, loose)
                return 0
            t1 = t1 or base_folder / "Temporada 1"
            t1.mkdir(parents=True, exist_ok=True)
            moved, clashes = move_loose_episodes(loose, t1)
        if moved:
            log.info("Reorganizado %s: %s capítulo(s) de la raíz a %s", base_folder.name, moved, t1.name)
            notify(f"🗂️ Reorganizado <b>{base_folder.name}</b>: {moved} capítulo(s) sueltos "
                   f"de la raíz movidos a '{t1.name}', porque ya hay más temporadas.", cfg)
        if clashes:
            notify(f"⚠️ <b>{base_folder.name}</b>: {', '.join(clashes)} están a la vez en la raíz y "
                   f"en '{t1.name}'. No he movido esos: revísalos a mano.", cfg)
        return moved

    def _warn_unknown_season(self, cfg: dict, base_folder: Path, t1: Path, loose: list[Path]):
        """Avisa (una vez por serie) de capítulos sueltos que no se sabe de qué temporada son."""
        if base_folder.name in self._organize_warned:
            return
        self._organize_warned.add(base_folder.name)
        names = ", ".join(f.name for f in loose[:6]) + ("…" if len(loose) > 6 else "")
        notify(f"⚠️ <b>{base_folder.name}</b> tiene capítulos sueltos en la raíz ({names}) "
               f"y además una '{t1.name}' con capítulos. No sé de qué temporada son: "
               f"no los muevo, revísalos a mano.", cfg)


@dataclass
class SeasonSpot:
    """Dónde va una temporada dentro de la carpeta de su serie."""
    base_folder: Path
    season: str
    subtitle: str

    def in_root(self, is_new: bool) -> bool:
        """
        La temporada 1 va suelta en la raíz mientras no haya carpetas de
        temporada (si la serie es nueva, solo cuando no trae subtítulo).
        """
        return self.season == "1" and (not is_new or not self.subtitle) and not self._season_dirs_present()

    def target(self) -> Path:
        """Su carpeta "Temporada N" (la que ya haya, lleve el subtítulo que lleve)."""
        name = f"Temporada {self.season}" + (f" ({self.subtitle})" if self.subtitle else "")
        return files.find_season_dir(self.base_folder, self.season) or self.base_folder / name

    def _season_dirs_present(self) -> bool:
        return self.base_folder.exists() and any(
            p.is_dir() and p.name.startswith("Temporada") for p in self.base_folder.iterdir())


def _earlier_years(anime_root: Path, base: str, year: int) -> list[tuple[int, str]]:
    """[(año, carpeta)] de las carpetas "<base> (año)" de años anteriores a `year`."""
    pattern = re.compile(re.escape(base) + r"\s+\((\d{4})\)", re.IGNORECASE)
    earlier = []
    try:
        for d in anime_root.iterdir():
            mm = pattern.fullmatch(d.name)
            if mm and int(mm.group(1)) < year and d.is_dir():
                earlier.append((int(mm.group(1)), d.name))
    except OSError:
        return []
    return earlier


def move_loose_episodes(loose: list[Path], t1: Path) -> tuple[int, list[str]]:
    """
    Mueve los capítulos sueltos a `t1` sin pisar nada: (cuántos se han
    movido, [los que ya estaban en t1 y se han dejado en la raíz]).
    """
    moved, clashes = 0, []
    for f in loose:
        target = t1 / f.name
        if target.exists():
            clashes.append(f.name)
            continue
        os.replace(f, target)  # misma NAS: renombrar, no copiar
        moved += 1
    return moved, clashes

def resolve_anime_folder(cfg=None, watch_title="", **kwargs):
    return Library().resolve(cfg or load_config(), watch_title, **kwargs)
