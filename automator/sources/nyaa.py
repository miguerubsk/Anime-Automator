"""Búsqueda de capítulos en nyaa.si (fallback cuando el enlace de japan-paw no sirve)."""

import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from ..library.naming import match_season, strip_group_prefix

log = logging.getLogger("anime-automator")

SXXEXX_RE = re.compile(r"\bS(\d{1,2})E0*(\d+)\b", re.IGNORECASE)
DASH_EPISODE_RE = re.compile(r"-\s*0*(\d+)\s*(?:\||\[|\(|$)")
EP_WORD_RE = re.compile(r"\bEp(?:isode)?\.?\s*0*(\d+)\b", re.IGNORECASE)

# Releases que anuncian abiertamente ser doblaje (no llevan audio japonés,
# que es un requisito imprescindible del pipeline) — se descartan antes de
# gastar ancho de banda descargándolas para que fallen después.
DUB_EXCLUDE_KEYWORDS = ("english dub", "eng dub", "dubbed", "[dub]")


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

    def search(self, queries, episode: int, expected_season: int):
        """(torrent_url, título) de la mejor coincidencia, o (None, None)."""
        for query in filter(None, queries):
            candidates = []
            wrong_season = dub = 0
            for item in self._items(query):
                title = item.findtext("title") or ""
                low = title.lower()
                if any(kw in low for kw in DUB_EXCLUDE_KEYWORDS):
                    dub += 1
                    continue
                found_episode, found_season = parse_nyaa_release(title)
                if found_episode != episode:
                    continue
                if found_season != expected_season:
                    wrong_season += 1
                    continue
                enclosure = item.find("enclosure")
                link = enclosure.get("url") if enclosure is not None else item.findtext("link")
                if not link:
                    continue
                candidates.append((self.preferred_group in low,
                                   "web-dl" in low or "webdl" in low,
                                   "1080p" in low, title, link))
            if wrong_season or dub:
                log.info("nyaa.si: descartados %s de temporada distinta a la %s y %s doblajes (query %r)",
                         wrong_season, expected_season, dub, query)
            if candidates:
                # grupo preferido > WEB-DL > 1080p; el resto en el orden de nyaa
                candidates.sort(key=lambda c: (not c[0], not c[1], not c[2]))
                return candidates[0][4], candidates[0][3]
        return None, None
