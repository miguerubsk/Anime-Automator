"""
Pedir un anime entero por torrent (nyaa.si), para lo que no está en japan-paw.

Lo usa requester-bot: parte de una entrada concreta de AniList (por su id),
calcula la carpeta y la temporada como el resto de la biblioteca, busca cada
capítulo (grupo preferido > subtítulos múltiples > WEB-DL > 1080p; nunca
releases sin subtítulos en español) y los añade a Transmission. El
TorrentTracker se encarga del resto, igual que con el fallback.

- plan(): qué hay, sin crear ni descargar nada.
- start(): lo mismo y, si falta algo, lo pone a descargar.
"""

import logging
from pathlib import Path

from ..core.nas import assert_nas_mounted
from ..library import files
from ..library.anilist import MAIN_FORMATS, AniListClient, AniListDown, SeasonResolver, _is_main, _names
from ..library.folders import Library, SeasonLayout
from ..library.naming import clean_subtitle, folder_safe, name_key, split_season
from ..sources.nyaa import NyaaSearch
from .jobs import JobTracker
from .model import WorkItem, work_key
from .registry import Completed, InProgress
from .torrents import TorrentTracker, TrackedTorrent, Transmission

log = logging.getLogger("anime-automator")

# lo que se puede pedir así (las películas no van por número de capítulo)
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
        if not mains:
            return None
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
            if m.get("format") not in SERIES_FORMATS:
                return {"status": "unsupported", "format": m.get("format")}
            layout = self._layout(m)
        except AniListDown as e:
            return {"status": "anilist_down", "error": str(e)}
        self.media = m
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
        missing = [ep for ep in releases if ep not in have]
        busy = [ep for ep in missing if work_key(target, ep) in InProgress()]
        groups: dict[str, int] = {}
        for rel in releases.values():
            groups[rel.group or "?"] = groups.get(rel.group or "?", 0) + 1
        info = {
            "title": title, "name": name, "folder": str(folder), "season": layout.season,
            "subtitle": layout.subtitle, "new_folder": is_new, "episodes": total,
            "found": sorted(releases), "available": len(releases),
            # como /download: «have» es cuántos hay; la lista va aparte
            "have": len([ep for ep in have if not total or ep <= total]),
            "have_episodes": sorted(ep for ep in have if not total or ep <= total),
            "missing": [ep for ep in missing if ep not in busy], "already_in_progress": busy,
            "preferred": sum(1 for r in releases.values() if r.preferred), "groups": groups,
        }
        self._releases, self._target = releases, target
        self._layout_used = layout
        if not releases:
            return {"status": "nothing_found", **info}
        if not missing:
            return {"status": "nothing_new", **info}
        if not info["missing"]:
            return {"status": "in_progress", **info}
        return {"status": "missing", **info}

    # ---------------- descarga ----------------
    def start(self, job_id=None) -> dict:
        result = self.plan()
        if result["status"] != "missing":
            return result
        title = result["title"]
        try:
            folder, name, _ = Library().resolve(self.cfg, title, link_emision=False, create=True,
                                                layout=self._layout_used)
        except Exception as e:
            log.exception("No se pudo crear la carpeta de %r", title)
            return {**result, "status": "folder_resolution_failed", "error": str(e)}
        target = {"folder": str(folder), "name": name}
        job = JobTracker().new_job(job_id, name, title)
        queued, failed = [], []
        for ep in result["missing"]:
            rel = self._releases[ep]
            item = WorkItem(target, title, ep, "")
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
        if job and queued:
            JobTracker().seal(job)
        if not queued:
            return {**result, "status": "transmission_failed" if failed else "in_progress",
                    "folder": str(folder), "not_added": failed}
        return {**result, "status": "queued", "folder": str(folder), "queued": queued, "not_added": failed,
                "tracked": bool(job)}
