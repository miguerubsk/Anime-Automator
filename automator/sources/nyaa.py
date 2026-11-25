"""Búsqueda de capítulos en nyaa.si (fallback cuando el enlace de japan-paw no sirve)."""

import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import requests

from ..library.movies import MOVIE_MARKER_RE
from ..library.naming import match_season, name_key, split_season, strip_group_prefix

log = logging.getLogger("anime-automator")

SXXEXX_RE = re.compile(r"\bS(\d{1,2})E0*(\d+)\b", re.IGNORECASE)
# "- 01 [", "- 01v2 [", "- 13 END [" (el último capítulo de Erai-raws)
DASH_EPISODE_RE = re.compile(r"-\s*0*(\d+)(?:v\d+)?(?:\s*END)?\s*(?:\||\[|\(|$)")
EP_WORD_RE = re.compile(r"\bEp(?:isode)?\.?\s*0*(\d+)\b", re.IGNORECASE)

# Releases que anuncian abiertamente ser doblaje (no llevan audio japonés,
# que es un requisito imprescindible del pipeline) — se descartan antes de
# gastar ancho de banda descargándolas para que fallen después.
DUB_EXCLUDE_KEYWORDS = ("english dub", "eng dub", "dubbed", "[dub]")

# Subtítulos: el pipeline solo guarda los de español, así que en las
# búsquedas de animes enteros se descarta lo que no los anuncie (salvo el
# grupo preferido, Erai-raws, que los trae aunque no siempre lo ponga).
MULTISUB_RE = re.compile(r"multi[\s-]?subs?|multiple[\s-]subtitles?|\b(?:spa|spanish|español|esp)\b",
                         re.IGNORECASE)
ENGLISH_ONLY_RE = re.compile(r"\beng(?:lish)?[\s-]?subs?\b|\[eng\]", re.IGNORECASE)
# lotes, colecciones y temporadas enteras («S02», «Season 2»): no son una película
BATCH_RE = re.compile(r"\b(?:batch|complete|collection|seasons?|S\d{1,2})\b|\d+\s*[~-]\s*\d+\s*(?:\]|\)|$)",
                      re.IGNORECASE)
RESOLUTION_RE = re.compile(r"\b(?:480|576|720|1080|2160)p\b", re.IGNORECASE)


def extract_query_from_failed_url(failed_url: str, episode: int) -> str | None:
    """
    A partir del enlace de japan-paw que falló (aunque dé 404, el nombre de
    archivo ya viene con el estilo de nombrado real del fansub, que suele
    coincidir con cómo aparece en nyaa.si — más fiable que reconstruirlo
    desde el watch_title).
    """
    try:
        name = urllib.parse.unquote(Path(urllib.parse.urlparse(failed_url).path).name)
    except Exception:
        return None
    name = re.sub(r"\.mkv$", "", name, flags=re.IGNORECASE)
    # quitar corchetes de grupo/resolución/hash: [Erai-raws], [MultiSub], [8553AB2E]...
    name = re.sub(r"\[[^\]]*\]", " ", name)
    # nombres estilo ToonsHub vienen con puntos en vez de espacios
    if " " not in name and name.count(".") > 3:
        name = name.replace(".", " ")
    name = re.sub(r"\s{2,}", " ", name).strip()
    # cortar justo después del número de episodio (el título del capítulo y la
    # calidad/codec no tienen por qué coincidir entre grupos)
    m = re.search(rf"S\d{{1,2}}E0*{episode}\b", name, re.IGNORECASE) or re.search(rf"-\s*0*{episode}\b", name)
    if m:
        name = name[: m.end()]
    name = name.strip(" -.")
    return name or None


def guess_season_from_release_title(title: str, episode: int) -> int:
    """
    Temporada según el texto que precede al "- NN" del título
    ("Mato Seihei no Slave 2 - 01" -> 2). Sin indicador, temporada 1.
    """
    t = strip_group_prefix(title)
    m = re.search(rf"-\s*0*{episode}\b", t)
    if m:
        t = t[: m.start()]
    m, season = match_season(t.strip())
    return season if m is not None else 1


def parse_nyaa_release(title: str):
    """
    (episodio, temporada) de un título de nyaa de forma estricta, por posición
    conocida (si se buscara el número en cualquier parte, el "10" de "10bit"
    se confundiría con el episodio 10). (None, 1) si no se reconoce.
    """
    m = SXXEXX_RE.search(title)  # "S02E06": la señal más fiable
    if m:
        return int(m.group(2)), int(m.group(1))
    m = DASH_EPISODE_RE.search(title)  # "Nombre - 01 [...]" / "- 01 |" / "- 01 ("
    if m:
        episode = int(m.group(1))
        return episode, guess_season_from_release_title(title, episode)
    m = EP_WORD_RE.search(title)  # "Episode 01" / "Ep01"
    if m:
        mm, season = match_season(strip_group_prefix(title))
        return int(m.group(1)), (season if mm is not None else 1)
    return None, 1


def release_names(title: str) -> set[str]:
    """
    Claves (name_key, sin la temporada) de los nombres que da un release:
    lo que va antes del capítulo y, como hace ToonsHub, los títulos
    alternativos entre paréntesis o tras un " | ".
    "[ToonsHub] From Far Away S01E08 1080p (Kanata kara, Multi-Subs)" ->
    {"fromfaraway", "kanatakara", "multisubs"}.
    """
    t = strip_group_prefix(title)
    m = SXXEXX_RE.search(t) or DASH_EPISODE_RE.search(t) or EP_WORD_RE.search(t)
    parts = [t[: m.start()] if m else t]
    for group in re.findall(r"\(([^)]*)\)", t):
        parts += group.split(",")
    if "|" in t:
        parts.append(t.split("|", 1)[1])
    return {k for k in (core_key(p) for p in parts) if len(k) >= 3}


def core_key(name: str) -> str:
    """name_key del nombre sin la temporada ("Kono Oto Tomare! 2nd Season" -> "konootomare")."""
    return name_key(split_season(name.strip(" -"), "")[0])


def query_text(name: str) -> str:
    """El nombre tal como se busca (los signos estorban en la búsqueda)."""
    return re.sub(r"\s{2,}", " ", re.sub(r"[!?:;,.~☆★♪\"'()\[\]]", " ", name)).strip()


def movie_key(name: str) -> str:
    """
    Clave de una película sin «Movie», «Gekijouban», «The Movie» ni el año:
    "Gekijouban Seitokai Yakuindomo 2 - Movie" = "Seitokai Yakuindomo Movie 2".
    """
    t = re.sub(r"\(\s*\d{4}\s*\)", " ", name or "")
    return name_key(MOVIE_MARKER_RE.sub(" ", t))


def movie_release_names(title: str) -> set[str]:
    """Claves (movie_key) de los nombres que da el release de una película."""
    t = re.sub(r"\.(?:mkv|mp4|avi)$", "", strip_group_prefix(title), flags=re.IGNORECASE)
    parts = [p for g in re.findall(r"\(([^)]*)\)", t) for p in g.split(",")]
    if "|" in t:
        parts += t.split("|")[1:]
    main = re.split(r"\[|\(|\|", t.split("|")[0], maxsplit=1)[0]
    m = RESOLUTION_RE.search(main)
    parts.append(main[: m.start()] if m else main)
    return {k for k in (movie_key(p.strip(" -")) for p in parts) if len(k) >= 3}


def is_dub(title: str) -> bool:
    return any(kw in title.lower() for kw in DUB_EXCLUDE_KEYWORDS)


def episode_verdict(title: str, episode: int, season: int) -> str:
    """¿Vale el release para ese capítulo? 'ok', 'dub', 'other' (otro capítulo) o 'season' (otra temporada)."""
    if is_dub(title):
        return "dub"
    found_episode, found_season = parse_nyaa_release(title)
    if found_episode != episode:
        return "other"
    return "ok" if found_season == season else "season"


def item_link(item) -> str | None:
    """El .torrent de un item del RSS (o, si no lo trae, su enlace)."""
    enclosure = item.find("enclosure")
    return enclosure.get("url") if enclosure is not None else item.findtext("link")


def search_queries(names, variants) -> list[str]:
    """Las búsquedas de unos nombres (cada `variants` con "{}" = el nombre), sin repetir nombres."""
    queries, seen = [], set()
    for n in names:
        q = query_text(n)
        if q and q.lower() not in seen:
            seen.add(q.lower())
            queries += [v.format(q) for v in variants]
    return queries


def wanted_keys(names, key) -> set[str]:
    """Las claves (`key`) de unos nombres, sin las demasiado cortas para distinguir nada."""
    return {k for k in map(key, names) if len(k) >= 3}


def best_of(releases) -> "Release | None":
    """El de mejor rank (a igualdad, el primero)."""
    return min(filter(None, releases), key=lambda r: r.rank, default=None)


@dataclass
class Release:
    episode: int
    title: str
    link: str
    group: str
    rank: tuple

    @property
    def preferred(self) -> bool:
        return self.rank[0] is False


class NyaaSearch:
    """
    Busca un capítulo en el RSS de búsqueda de nyaa.si. Descarta candidatos:
    - cuyo episodio real no sea el que buscamos.
    - cuyo título indique una temporada distinta a la esperada.
    - que anuncien ser doblaje (sin audio japonés).
    Preferible no encontrar nada a traer el capítulo equivocado.
    """

    def __init__(self, cfg: dict):
        self.nfg = cfg["nyaa_fallback"]
        self.preferred_group = cfg["preferred_group"].lower()

    def _items(self, query: str):
        resp = requests.get(self.nfg["base_url"] + "/",
                            params={"page": "rss", "q": query, "c": self.nfg.get("category", "1_2")},
                            timeout=30)
        resp.raise_for_status()
        return ET.fromstring(resp.content).findall(".//item")

    def _answers(self, queries):
        """Los items de cada búsqueda; las que fallan se saltan (con aviso en el log)."""
        for query in queries:
            try:
                yield self._items(query)
            except Exception as e:
                log.warning("nyaa.si no responde a %r: %s", query, e)

    def subs_ok(self, title: str) -> bool:
        """¿Puede traer subtítulos en español? (si no, no vale la pena bajarlo)."""
        multi = bool(MULTISUB_RE.search(title))
        if ENGLISH_ONLY_RE.search(title) and not multi:
            return False
        return multi or f"[{self.preferred_group}]" in title.lower()

    def _rank(self, title: str, with_subs: bool = True) -> tuple:
        """Menor = mejor: grupo preferido > subtítulos anunciados (si `with_subs`) > WEB-DL > 1080p."""
        low = title.lower()
        subs = (not MULTISUB_RE.search(title),) if with_subs else ()
        return (self.preferred_group not in low, *subs, not ("web-dl" in low or "webdl" in low), "1080p" not in low)

    def catalog(self, names, seasons, max_episode: int | None = None, max_queries: int = 6) -> dict:
        """
        Los capítulos de un anime entero: {capítulo: Release}, el mejor de cada
        uno. `names` son sus títulos (de AniList) y `seasons` las temporadas
        que puede indicar un release suyo. Solo se aceptan releases cuyo nombre
        coincide con uno de `names`, de esas temporadas, sin doblaje, con
        subtítulos en español anunciados (o del grupo preferido) y, si se sabe
        cuántos capítulos tiene, dentro de ese número.
        """
        wanted = wanted_keys(names, core_key)
        found: dict[int, Release] = {}
        for items in self._answers(search_queries(names, ("{} 1080p", "{}"))[:max_queries]):
            for rel in filter(None, (self._release(item, wanted, seasons, max_episode) for item in items)):
                found[rel.episode] = best_of([found.get(rel.episode), rel])
            if max_episode and all(ep in found for ep in range(1, max_episode + 1)):
                break
        log.info("nyaa.si: %s capítulos para %s (temporadas %s)", len(found), names[:2], sorted(seasons))
        return dict(sorted(found.items()))

    def movie(self, names, max_queries: int = 6) -> "Release | None":
        """
        La mejor versión de una película: que se llame como ella (sin contar
        «Movie»/«Gekijouban»/año), sin número de capítulo, que no sea un lote,
        sin doblaje y con subtítulos en español anunciados (o del grupo preferido).
        """
        wanted = wanted_keys(names, movie_key)
        best = None
        for items in self._answers(search_queries(names, ("{}",))[:max_queries]):
            best = best_of([best, *(self._movie_release(item, wanted) for item in items)])
            if best and best.preferred:
                break
        log.info("nyaa.si: película %s para %s", best.title if best else "no encontrada", names[:2])
        return best

    def _build(self, item, title: str, episode: int) -> "Release | None":
        link = item_link(item)
        if not link:
            return None
        m = re.match(r"\s*\[([^\]]+)\]", title)
        return Release(episode, title, link, m.group(1) if m else "", self._rank(title))

    def _acceptable(self, title: str) -> bool:
        """Sin doblaje y con subtítulos en español (o del grupo preferido)."""
        return not is_dub(title) and self.subs_ok(title)

    def _movie_release(self, item, wanted: set) -> Release | None:
        title = item.findtext("title") or ""
        if not self._acceptable(title) or BATCH_RE.search(title) or parse_nyaa_release(title)[0] is not None:
            return None
        if not (movie_release_names(title) & wanted):
            return None
        return self._build(item, title, 1)

    def _release(self, item, wanted: set, seasons, max_episode) -> Release | None:
        title = item.findtext("title") or ""
        if not self._acceptable(title) or not (release_names(title) & wanted):
            return None
        episode, season = parse_nyaa_release(title)
        if not episode or season not in seasons or (max_episode and episode > max_episode):
            return None
        return self._build(item, title, episode)

    def search(self, queries, episode: int, expected_season: int):
        """(torrent_url, título) de la mejor coincidencia, o (None, None)."""
        for query in filter(None, queries):
            match = self._episode_match(query, episode, expected_season)
            if match:
                return match
        return None, None

    def _episode_match(self, query: str, episode: int, expected_season: int):
        """(torrent_url, título) del mejor release del capítulo en una búsqueda, o None."""
        candidates, verdicts = [], Counter()
        for item in self._items(query):
            title = item.findtext("title") or ""
            verdict = episode_verdict(title, episode, expected_season)
            verdicts[verdict] += 1
            link = item_link(item)
            if verdict == "ok" and link:
                candidates.append((self._rank(title, with_subs=False), link, title))
        if verdicts["season"] or verdicts["dub"]:
            log.info("nyaa.si: descartados %s de temporada distinta a la %s y %s doblajes (query %r)",
                     verdicts["season"], expected_season, verdicts["dub"], query)
        # grupo preferido > WEB-DL > 1080p; el resto en el orden de nyaa
        best = min(candidates, key=lambda c: c[0], default=None)
        return best and best[1:]
