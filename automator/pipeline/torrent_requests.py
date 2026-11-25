"""
Pedir un anime entero por torrent (nyaa.si), para lo que no está en japan-paw.

Lo usa requester-bot: parte de una entrada concreta de AniList (por su id),
calcula la carpeta y la temporada como el resto de la biblioteca, busca cada
capítulo (grupo preferido > subtítulos múltiples > WEB-DL > 1080p; nunca
releases sin subtítulos en español) y los añade a Transmission. El
TorrentTracker se encarga del resto, igual que con el fallback.

Las películas van igual, con un solo «capítulo» (el 1) y a donde van las de
la web (la carpeta de su serie o la de películas).

- plan(): qué hay, sin crear ni descargar nada.
- start(): lo mismo y, si falta algo, lo pone a descargar.
"""

import logging
from collections import Counter
from pathlib import Path

from ..core.nas import assert_nas_mounted
from ..library import files
from ..library.anilist import MAIN_FORMATS, AniListClient, AniListDown, SeasonResolver, _is_main, _names
from ..library.folders import Library, SeasonLayout
from ..library.movies import MovieLibrary, already_have_movie
from ..library.naming import clean_subtitle, folder_safe, name_key, split_season
from ..sources.nyaa import NyaaSearch
from .jobs import JobTracker
from .model import WorkItem, work_key
from .registry import Completed, InProgress
from .torrents import TorrentTracker, TrackedTorrent, Transmission

log = logging.getLogger("anime-automator")

# lo que se pide por número de capítulo; las películas (MOVIE) van aparte
SERIES_FORMATS = MAIN_FORMATS | {"OVA", "ONA", "SPECIAL"}


def parse_season(value) -> str | None:
    """La temporada que corrige el admin ("2", 2, "1.5"), o None si no vale."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        return None
    if not 0 < number < 100:
        return None
    return str(int(number)) if number.is_integer() else text


class TorrentRequest:
    def __init__(self, cfg: dict, anilist_id: int, season=None):
        self.cfg = cfg
        self.anilist_id = int(anilist_id)
        self.season_override = parse_season(season)
        self.media = None

    # ---------------- AniList ----------------
    def _layout(self, m) -> SeasonLayout:
        """Serie y temporada de esa entrada de AniList (o la que haya dicho el admin)."""
        title = (m.get("title") or {}).get("romaji") or _names(m)[0]
        plain, season, explicit = split_season(title, "")
        layout = SeasonLayout(folder_safe(plain), str(season), "", folder_safe(title))
        # como con japan-paw: si el título dice la temporada ("2nd Season"), manda el título
        if not explicit:
            layout = self._layout_from_chain(m, title, plain) or layout
        if self.season_override:
            layout.season = self.season_override
        return layout

    @staticmethod
    def _layout_from_chain(m, title: str, plain: str) -> SeasonLayout | None:
        resolver = SeasonResolver()
        info = resolver.info_for(m, title)
        if info:
            sub = folder_safe(clean_subtitle(info["subtitle"])) if info["subtitle"] else ""
            return SeasonLayout(folder_safe(info["base"]), info["season"], sub, folder_safe(title))
        if m.get("format") not in MAIN_FORMATS:
            return None
        # el título no empieza por el de la primera temporada: el número sale de la cadena igual
        mains = [p for p in resolver._prequel_chain(m) if _is_main(p)]
        return TorrentRequest._layout_after(mains, m, title, plain) if mains else None

    @staticmethod
    def _layout_after(mains: list, m, title: str, plain: str) -> SeasonLayout:
        """Temporada siguiente a las principales de la cadena (`mains`, de la más reciente a la primera)."""
        root_title = (mains[-1].get("title") or {}).get("romaji") or _names(mains[-1])[0]
        base = split_season(root_title, "")[0]
        season = str(len(mains) + 1) if _is_main(m) else f"{len(mains)}.5"
        # si el título es el de la primera con algún signo de más, no hay subtítulo
        sub = "" if name_key(plain) == name_key(base) else folder_safe(plain)
        return SeasonLayout(folder_safe(base), season, sub, folder_safe(title))

    @staticmethod
    def _release_seasons(names, layout: SeasonLayout) -> set[int]:
        """Temporadas con las que un release de este anime puede venir marcado."""
        seasons = {split_season(n, "")[1] for n in names}
        if layout.season.isdigit():
            seasons.add(int(layout.season))
        return seasons

    # ---------------- consulta ----------------
    def plan(self) -> dict:
        try:
            assert_nas_mounted(self.cfg)
        except Exception as e:
            return {"status": "nas_not_mounted", "error": str(e)}
        try:
            m = AniListClient().media(self.anilist_id)
            if not m:
                return {"status": "not_found"}
            if m.get("format") == "MOVIE":
                self.media = m
                return self._plan_movie(m)
            if m.get("format") not in SERIES_FORMATS:
                return {"status": "unsupported", "format": m.get("format")}
            layout = self._layout(m)
        except AniListDown as e:
            return {"status": "anilist_down", "error": str(e)}
        self.media = m
        return self._plan_series(m, layout)

    def _plan_series(self, m, layout: SeasonLayout) -> dict:
        names = _names(m)
        title = names[0]
        try:
            folder, name, is_new = Library().resolve(self.cfg, title, link_emision=False, create=False,
                                                     layout=layout)
        except Exception as e:
            log.exception("No se pudo decidir la carpeta de %r", title)
            return {"status": "folder_resolution_failed", "error": str(e)}

        total = m.get("episodes")
        releases = NyaaSearch(self.cfg).catalog(names, self._release_seasons(names, layout), total)
        target = {"folder": str(folder), "name": name}
        have = files.existing_episode_numbers(Path(folder)) | Completed().episodes_of(str(folder))
        info = {
            "title": title, "name": name, "folder": str(folder), "season": layout.season,
            "subtitle": layout.subtitle, "new_folder": is_new, "episodes": total,
            **self._episodes_info(releases, have, target, total),
        }
        self._releases, self._target = releases, target
        self._layout_used = layout
        return {"status": self._status(info), **info}

    @staticmethod
    def _episodes_info(releases: dict, have: set, target: dict, total: int | None) -> dict:
        """Qué hay en nyaa.si, qué hay ya, qué falta y qué se está descargando."""
        missing = [ep for ep in releases if ep not in have]
        busy = [ep for ep in missing if work_key(target, ep) in InProgress()]
        # como /download: «have» es cuántos hay; la lista va aparte
        kept = sorted(ep for ep in have if not total or ep <= total)
        return {
            "found": sorted(releases), "available": len(releases), "have": len(kept), "have_episodes": kept,
            "missing": [ep for ep in missing if ep not in busy], "already_in_progress": busy,
            "preferred": sum(1 for r in releases.values() if r.preferred),
            "groups": dict(Counter(rel.group or "?" for rel in releases.values())),
        }

    @staticmethod
    def _status(info: dict) -> str:
        if not info["found"]:
            return "nothing_found"
        if not info["missing"] and not info["already_in_progress"]:
            return "nothing_new"
        if not info["missing"]:
            return "in_progress"
        return "missing"

    def _plan_movie(self, m) -> dict:
        names = _names(m)
        title = names[0]
        series = SeasonResolver().series_of(m)   # la serie aunque el título no la diga
        folder, filename, from_series = MovieLibrary(self.cfg).destination(title, series or "")
        dest = folder / filename
        target = {"folder": str(folder), "name": Path(filename).stem if from_series else title,
                  "movie": True, "filename": filename, "key": f"movie:{dest}"}
        have = already_have_movie(folder, filename)
        rel = None if have else NyaaSearch(self.cfg).movie(names)
        releases = {1: rel} if rel else {}
        info = {
            "title": title, "name": title, "folder": str(folder), "file": filename, "kind": "movie",
            "series_folder": from_series, "season": "", "subtitle": "", "new_folder": False, "episodes": 1,
            **self._episodes_info(releases, {1} if have else set(), target, 1),
        }
        self._releases, self._target, self._layout_used = releases, target, None
        return {"status": "nothing_new" if have else self._status(info), **info}

    # ---------------- descarga ----------------
    def start(self, job_id=None) -> dict:
        result = self.plan()
        if result["status"] != "missing":
            return result
        try:
            folder, name, target = self._create_folder(result)
        except Exception as e:
            log.exception("No se pudo crear la carpeta de %r", result["title"])
            return {**result, "status": "folder_resolution_failed", "error": str(e)}
        job = JobTracker().new_job(job_id, name, result["title"], kind=result.get("kind", "series"))
        queued, failed = self._add_torrents(result, target, name, job)
        if job and queued:
            JobTracker().seal(job)
        if not queued:
            return {**result, "status": "transmission_failed" if failed else "in_progress",
                    "folder": str(folder), "not_added": failed}
        return {**result, "status": "queued", "folder": str(folder), "queued": queued, "not_added": failed,
                "tracked": bool(job)}

    def _create_folder(self, result: dict) -> tuple[Path, str, dict]:
        """(carpeta creada, nombre del anime o la película, target de los capítulos)."""
        if result.get("kind") == "movie":
            folder = Path(self._target["folder"])
            folder.mkdir(parents=True, exist_ok=True)
            return folder, result["name"], self._target
        folder, name, _ = Library().resolve(self.cfg, result["title"], link_emision=False, create=True,
                                            layout=self._layout_used)
        return folder, name, {"folder": str(folder), "name": name}

    def _add_torrents(self, result: dict, target: dict, name: str, job) -> tuple[list[int], list[int]]:
        """Añade a Transmission lo que falta. (añadidos, rechazados por Transmission)."""
        queued, failed = [], []
        for ep in result["missing"]:
            rel = self._releases[ep]
            item = WorkItem(target, result["title"], ep, "")
            if not InProgress().try_add(item.key):
                continue
            try:
                _client, torrent = Transmission.add(self.cfg, rel.link)
            except Exception as e:
                log.warning("Transmission no acepta el torrent de %s cap. %s: %s", name, ep, e)
                InProgress().discard(item.key)
                failed.append(ep)
                continue
            if job:
                JobTracker().attach(job, item.key)
            TorrentTracker().track(torrent.hashString, TrackedTorrent(item, rel.title))
            queued.append(ep)
            log.info("Torrent añadido para %s cap. %s: %s", name, ep, rel.title)
        return queued, failed
