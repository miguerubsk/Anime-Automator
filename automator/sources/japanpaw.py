"""Fichas de japan-paw.net: pestañas de descarga, enlaces por capítulo y de películas."""

import logging
import re
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup

from ..core.config import load_config
from .browser import fetch_rendered_html

log = logging.getLogger("anime-automator")

# Prioridad de calidad por tipo de fuente conocido (menor = mejor). Tipos no
# reconocidos se tratan como de prioridad baja (ni rompen el parseo ni se
# anteponen a los conocidos).
SOURCE_PRIORITY = {"bd": 0, "dl": 1, "web": 2, "tv": 3}
DEFAULT_SOURCE_PRIORITY = 5
KNOWN_SOURCE_TYPES = ("BD", "DL", "Web", "TV")

VIDEO_EXT_RE = re.compile(r"\.(mkv|mp4|avi)$", re.IGNORECASE)
DUB_ONLY_RE = re.compile(r"\bdub\b|\bdub-|doblaje|doblada", re.IGNORECASE)
JPN_FRIENDLY_RE = re.compile(r"multi|sub|vose|jap|dual", re.IGNORECASE)
EPISODE_LINK_RE = re.compile(r"Cap[ií]tulo\s*0*(\d+)", re.IGNORECASE)

# rank de los candidatos a película
RANK_JAPANESE, RANK_UNKNOWN, RANK_DUB = 0, 1, 2


def resolve_link(href: str) -> str:
    """Enlace real detrás del acortador redirect.japan-paw.net y de acortadores intermedios (ouo.io)."""
    if "redirect.japan-paw.net" in href and "#" in href:
        url = urllib.parse.unquote(href.split("#", 1)[1])
    else:
        url = href
    # Algunos acortadores intermedios (p.ej. ouo.io) llevan la URL real en un
    # parámetro "s=":  https://ouo.io/qs/XXXX?s=https%3A%2F%2Fanime.craftervault.com/...mkv
    # Ojo: la URL real puede llevar "&" sin codificar (la carpeta del servidor
    # para títulos que empiezan por número es "1.Numeros & Simbolos"), así que
    # no vale parse_qs: se coge todo lo que va detrás de "s=" hasta el final.
    m = re.search(r"[?&]s=(https?(?::|%3A).*)$", url, re.IGNORECASE)
    if m:
        inner = m.group(1)
        if re.match(r"https?%3A", inner, re.IGNORECASE):
            inner = urllib.parse.unquote(inner)
        url = inner
    return url


def parse_tab_label(label: str):
    """
    (group, source, resolution) de la etiqueta de una pestaña de descarga:
      "[ToonsHub][Web 1080p]"            -> ("ToonsHub", "web", 1080)
      "[Japan Paw!] [BD 1080p]"          -> ("Japan Paw!", "bd", 1080)
      "[DantalianSubs] [Web HEVC 1080p]" -> ("DantalianSubs", "web", 1080)
      "Web 1080p"                        -> ("", "web", 1080)
      "[Grupo Raro] [SD]"                -> ("Grupo Raro", "web", None)
    Nunca falla: preferimos usar una pestaña "de la que no sabemos bien la
    calidad" antes que no tener nada.
    """
    res_match = re.search(r"(\d{3,4})p", label, re.IGNORECASE)
    resolution = int(res_match.group(1)) if res_match else None
    source_match = re.search(r"\b(" + "|".join(KNOWN_SOURCE_TYPES) + r")\b", label, re.IGNORECASE)
    source = source_match.group(1).lower() if source_match else "web"
    group = ""
    group_match = re.match(r"\s*\[([^\]]+)\]", label)
    if group_match:
        candidate = group_match.group(1).strip()
        # si ese primer corchete es el de la resolución ("[Web 1080p]"), no es un grupo
        if not re.search(r"\d{3,4}p", candidate, re.IGNORECASE):
            group = candidate
    return group, source, resolution


class TabRanking:
    """
    Estrategia de orden de las pestañas (y candidatos a película): menor = mejor.
    Resolución (hasta max_resolution) > tipo de fuente > grupo preferido.
    """

    def __init__(self, preferred_group: str, max_resolution=None):
        self.preferred_group = (preferred_group or "").lower()
        self.max_resolution = max_resolution

    @classmethod
    def from_config(cls, cfg: dict | None = None) -> "TabRanking":
        cfg = cfg or load_config()
        return cls(cfg["preferred_group"], cfg.get("max_resolution"))

    def key(self, t: dict):
        res = t["resolution"]
        if not res:
            resolution_rank = float("inf")  # desconocida: prioridad mínima, pero usable
        elif self.max_resolution and res > self.max_resolution:
            # por encima del tope: solo si no hay nada <= tope, y entonces la más baja
            resolution_rank = 100000 + res
        else:
            resolution_rank = -res
        source_rank = SOURCE_PRIORITY.get(t["source"], DEFAULT_SOURCE_PRIORITY)
        group_rank = 0 if t["group"].lower() == self.preferred_group else 1
        return (resolution_rank, source_rank, group_rank)

    def movie_key(self, c: dict):
        # un doblaje solo se usa si no hay otra cosa (sin audio japonés no sirve)
        return (c["rank"] == RANK_DUB, self.key(c), c["rank"])


class JapanPawPage:
    """Una ficha de japan-paw ya descargada (HTML)."""

    def __init__(self, html: str, url: str = ""):
        self.html = html
        self.url = url
        self._soup = BeautifulSoup(html, "html.parser")

    @classmethod
    def fetch(cls, url: str, cfg: dict | None = None) -> "JapanPawPage":
        cfg = cfg or load_config()
        return cls(fetch_rendered_html(url, cfg["sockpuppet_browser_url"]), url)

    @property
    def title(self) -> str:
        return self._soup.title.get_text(strip=True) if self._soup.title else ""

    @property
    def text(self) -> str:
        return self._soup.get_text(" ", strip=True)

    def _iter_tabs(self):
        """(group, source, resolution, contenedor) de cada pestaña de descarga."""
        for li in self._soup.select("li[tab-target]"):
            target = li.get("tab-target", "").lstrip("#")
            label_span = li.select_one("span")
            label = label_span.get_text(strip=True) if label_span else ""
            container = self._soup.find(id=target)
            if container is None:
                continue
            yield (*parse_tab_label(label), container)

    def tabs(self) -> list[dict]:
        """[{"group", "source", "resolution", "links": {episodio: url}}]"""
        out = []
        for group, source, resolution, container in self._iter_tabs():
            links = {}
            for a in container.find_all("a"):
                # el diseño nuevo escribe "Capítulo" con tilde (y el número en un
                # <b> aparte); el antiguo era "Capitulo N". Se aceptan ambos.
                m = EPISODE_LINK_RE.search(a.get_text(strip=True))
                href = a.get("href", "")
                if m and href:
                    links[int(m.group(1))] = resolve_link(href)
            out.append({"group": group, "source": source, "resolution": resolution, "links": links})
        return out

    def merged_links(self, ranking: TabRanking) -> dict[int, str]:
        """
        En vez de escoger UNA pestaña, fusiona los enlaces de todas: para cada
        capítulo, el de la pestaña de mayor prioridad que lo tenga. Así no se
        pierden capítulos que el grupo preferido aún no ha subido pero otro sí.
        """
        merged = {}
        # peor primero, para que las mejores, aplicadas al final, sobreescriban
        for t in sorted(self.tabs(), key=ranking.key, reverse=True):
            merged.update(t["links"])
        return merged

    def movie_candidates(self) -> list[dict]:
        """
        Todos los enlaces a archivos de vídeo, sean cuales sean sus textos
        ("Capitulo 1", el título, "DUB-Lat", "Multi-Audio & Multi-Subs"...).
        rank: 0 = parece llevar audio japonés/subs, 1 = no se sabe, 2 = doblaje.
        """
        cands = []
        for group, source, resolution, container in self._iter_tabs():
            for a in container.find_all("a", href=True):
                url = resolve_link(a["href"])
                path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
                if not url.startswith("http") or not VIDEO_EXT_RE.search(path):
                    continue
                text = a.get_text(" ", strip=True)
                haystack = f"{text} {Path(path).name}"
                if DUB_ONLY_RE.search(text) and not JPN_FRIENDLY_RE.search(text):
                    rank = RANK_DUB
                elif JPN_FRIENDLY_RE.search(haystack):
                    rank = RANK_JAPANESE
                else:
                    rank = RANK_UNKNOWN
                cands.append({"group": group, "source": source, "resolution": resolution,
                              "rank": rank, "url": url, "text": text})
        return cands

    def best_movie_link(self, ranking: TabRanking) -> str | None:
        cands = self.movie_candidates()
        return min(cands, key=ranking.movie_key)["url"] if cands else None

    def ranked_urls(self, ranking: TabRanking, episode: int, movie: bool) -> list[str]:
        """Todos los enlaces de ese capítulo (o de la película), del mejor al peor."""
        if movie:
            return [c["url"] for c in sorted(self.movie_candidates(), key=ranking.movie_key)]
        tabs = sorted(self.tabs(), key=ranking.key)
        return [t["links"][episode] for t in tabs if episode in t["links"]]


def alternative_link(cfg: dict, target: dict, episode: int, tried) -> str | None:
    """
    Otro enlace para ese capítulo (o película) en la ficha, leída de nuevo:
    de otra pestaña/grupo o uno nuevo si lo han vuelto a subir. El mejor que
    no esté en `tried`, o None.
    """
    page_url = target.get("page_url")
    if not page_url:
        return None
    try:
        page = JapanPawPage.fetch(page_url, cfg)
    except Exception as e:
        log.warning("No se pudo releer la ficha %s buscando otro enlace: %s", page_url, e)
        return None
    urls = page.ranked_urls(TabRanking.from_config(cfg), episode, bool(target.get("movie")))
    return next((u for u in urls if u not in tried), None)
