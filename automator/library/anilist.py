"""
Número de temporada de los animes cuyo título no lo dice ("Darker than
Black: Ryuusei no Gemini", "Kimetsu no Yaiba: Yuukaku-hen"), a partir de la
cadena de precuelas de AniList y de los datos de la ficha de japanpaw
(fecha de estreno, títulos alternativos) para dar con la entrada correcta.

Se guardan como el resto: <Base>/Temporada N (<subtítulo>).

Cómo se cuenta (igual que en la biblioteca): solo cuentan como temporada
las precuelas de TV/ONA con al menos MAIN_MIN_EPISODES capítulos. Así los
arcos cortos, como el Mugen Ressha-hen de TV (7 caps.) de Kimetsu, no
desplazan la numeración, y una serie corta entre temporadas queda como
"Temporada N.5" (p.ej. Nanatsu no Taizai: Seisen no Shirushi -> 1.5).

Nunca lanza excepciones: si AniList falla o no hay coincidencia clara,
season_info() devuelve None y se nombra la carpeta como siempre.
"""

import json
import logging
import re
import threading
import time
import unicodedata

import requests

from ..core.persistence import JsonStore
from ..core.singleton import Singleton
from .naming import name_key, split_season

log = logging.getLogger("seasons")

ANILIST_URL = "https://graphql.anilist.co"
MAIN_FORMATS = {"TV", "TV_SHORT", "ONA"}
MAIN_MIN_EPISODES = 8
MAX_HOPS = 15
CACHE_TTL = 7 * 24 * 3600
NEG_CACHE_TTL = 6 * 3600
MIN_INTERVAL = 2.1   # AniList limita a ~30 consultas/minuto

_FIELDS = """
  id format episodes synonyms
  title { romaji english }
  startDate { year month day }
  relations { edges { relationType node { id type format episodes } } }
"""
_Q_SEARCH = "query ($s: String) { Page(perPage: 6) { media(search: $s, type: ANIME) {" + _FIELDS + "} } }"
_Q_ID = "query ($id: Int) { Media(id: $id, type: ANIME) {" + _FIELDS + "} }"


class AniListDown(Exception):
    pass


class _Failed:
    """Fallo temporal de AniList (no se cachea)."""


FAILED = _Failed()


class AniListClient(metaclass=Singleton):
    """Consultas a AniList con caché persistente y un poco de calma (límite: ~30-90/min)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._store = JsonStore("anilist_cache.json", "la caché de AniList", indent=None, ensure_ascii=True)
        self._cache: dict = {}
        self._last_call = 0.0

    def load_cache(self):
        """Carga la caché persistente (en el volumen de tmp, sobrevive a reinicios)."""
        try:
            self._cache = self._store.read({}) or {}
        except (OSError, ValueError):
            self._cache = {}

    def query(self, query: str, variables: dict):
        """`data` de la respuesta (None si no hay), de la caché si es reciente; FAILED si AniList no responde."""
        ck = query[:20] + json.dumps(variables, sort_keys=True)
        with self._lock:
            hit = self._cache.get(ck)
            if _fresh(hit):
                return hit[1]
            self._wait_turn()
        data = _post(query, variables)
        if data is FAILED:
            return FAILED  # sin cachear: es un fallo temporal
        with self._lock:
            self._cache[ck] = (time.time(), data)
            self._store.save(self._cache)
        return data

    def _wait_turn(self):
        wait = MIN_INTERVAL - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _ask(self, query: str, variables: dict, what) -> dict:
        """Como query(), pero lanza AniListDown(what) si AniList no responde."""
        data = self.query(query, variables)
        if data is FAILED:
            raise AniListDown(what)
        return data or {}

    def media(self, mid: int):
        return self._ask(_Q_ID, {"id": mid}, mid).get("Media")

    def search(self, title: str) -> list:
        return (self._ask(_Q_SEARCH, {"s": title}, title).get("Page") or {}).get("media") or []


def _fresh(hit) -> bool:
    """¿Sirve aún esa respuesta de la caché? (las de "no existe" caducan antes)."""
    return bool(hit) and time.time() - hit[0] < (CACHE_TTL if hit[1] is not None else NEG_CACHE_TTL)


def _post(query: str, variables: dict):
    """Una consulta a AniList (reintentando una vez si pide calma): `data`, None si no existe o FAILED."""
    def send():
        return requests.post(ANILIST_URL, json={"query": query, "variables": variables}, timeout=10)

    try:
        r = send()
        if r.status_code == 429:
            time.sleep(min(float(r.headers.get("Retry-After", 10)), 30))
            r = send()
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("data")
    except (requests.RequestException, ValueError) as e:
        log.warning("AniList no responde (%s): %s", variables, e)
        return FAILED


# ------------------------------------------------------------------ #
# Interpretación de las entradas de AniList
# ------------------------------------------------------------------ #
def _names(m) -> list[str]:
    t = m.get("title") or {}
    return [x for x in (t.get("romaji"), t.get("english"), *(m.get("synonyms") or [])) if x]


def _date(m) -> str:
    d = m.get("startDate") or {}
    if not d.get("year"):
        return ""
    return f"{d['year']:04d}-{d.get('month') or 0:02d}-{d.get('day') or 0:02d}"


def _is_main(m) -> bool:
    return m.get("format") in MAIN_FORMATS and (m.get("episodes") or MAIN_MIN_EPISODES) >= MAIN_MIN_EPISODES


def _edges(m, rel):
    return [e["node"] for e in ((m.get("relations") or {}).get("edges") or [])
            if e.get("relationType") == rel and (e.get("node") or {}).get("type") == "ANIME"]


def _main_edges(m, rel):
    return [n for n in _edges(m, rel) if n.get("format") in MAIN_FORMATS]


def _alternative_of(m) -> list:
    """
    La serie de la que una OVA o especial es «versión alternativa» de un
    capítulo («Steins;Gate: … Divide By Zero», de la que cuelga Steins;Gate 0).
    Entre dos series de TV no vale: Fullmetal Alchemist y Brotherhood no son
    temporadas una de otra.
    """
    return [] if m.get("format") in MAIN_FORMATS else _main_edges(m, "ALTERNATIVE")


def split_title(title: str):
    """("Darker than Black", "Ryuusei no Gemini") para "Darker than Black: Ryuusei no Gemini".
    Solo con ": " (con espacio): "Re:Zero" no se parte."""
    if ": " not in title:
        return title.strip(), ""
    head, tail = title.split(": ", 1)
    return head.strip(), tail.strip()


def _split_on_base(title: str, bases) -> tuple[str, str] | None:
    """
    Parte el título por el nombre de la primera temporada: con bases
    {"Zombieland Saga"}, "Zombieland Saga Revenge" -> ("Zombieland Saga",
    "Revenge"). Se queda con el principio más largo que coincida.

    Algunas temporadas solo añaden un adorno pegado al nombre
    ("Seitokai Yakuindomo*", "Okusama ga Seitokaichou!+!", "K-On!!"): eso
    no es un subtítulo, así que da (nombre de la primera temporada, "").
    """
    keys: dict[str, str] = {}
    for b in bases:
        if len(name_key(b)) >= 3:
            keys.setdefault(name_key(b), b)
    for i in range(len(title), 0, -1):
        # solo se corta entre palabras ("Working" no es el principio de "Workingman")
        if i < len(title) and title[i - 1].isalnum() and title[i].isalnum():
            continue
        key = name_key(title[:i])
        if key not in keys:
            continue
        tail = title[i:].strip(" :-")
        if not re.search(r"\w", tail):
            return keys[key], ""
        return title[:i].rstrip(":- "), tail
    return None


def _strict(text: str) -> str:
    """Para comparar nombres «tal cual» (sin mayúsculas ni anchos raros, pero con los signos)."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "").casefold()).strip()


class SeasonResolver:
    """Temporada de un título según su cadena de precuelas en AniList."""

    def __init__(self, client: AniListClient | None = None):
        self.client = client or AniListClient()

    def find_media(self, title: str, premiere: str = "", alt_titles=()):
        """
        Entrada de AniList de ese anime: tiene que coincidir el título (o uno de
        los alternativos de japanpaw) o, si no, la fecha de estreno exacta.
        """
        cands = self.client.search(title)
        found = _by_name(cands, (title, *alt_titles), premiere)
        if found is None and premiere:
            found = next((m for m in cands if _date(m) == premiere), None)
        return found

    def _prequel_chain(self, m) -> list:
        """Precuelas de más reciente a más antigua (prefiriendo las de TV)."""
        chain, seen, cur = [], {m["id"]}, m
        for _ in range(MAX_HOPS):
            pre = _edges(cur, "PREQUEL") or _main_edges(cur, "PARENT") or _alternative_of(cur)
            if not pre:
                break
            pre.sort(key=lambda n: (n.get("format") not in MAIN_FORMATS, n["id"]))
            node = next((n for n in pre if n["id"] not in seen), None)
            if node is None:
                break
            seen.add(node["id"])
            cur = self.client.media(node["id"])  # si falla a mitad, AniListDown: mejor nada que un número mal
            if not cur:
                break
            chain.append(cur)
        return chain

    def _has_main_sequel(self, m, hops: int = 4) -> bool:
        """¿Hay una secuela de TV/ONA, aunque sea pasando por especiales u OVAs?"""
        frontier, seen = [m], {m["id"]}
        for _ in range(hops):
            nxt = []
            for cur in frontier:
                for n in _edges(cur, "SEQUEL"):
                    if n["id"] in seen:
                        continue
                    seen.add(n["id"])
                    if n.get("format") in MAIN_FORMATS:
                        return True
                    full = self.client.media(n["id"])
                    if full:
                        nxt.append(full)
            frontier = nxt
            if not frontier:
                break
        return False

    def series_of(self, m) -> str | None:
        """
        Nombre de la serie de una película, OVA o especial según sus precuelas
        (o la serie de la que cuelga), aunque el título no lo diga: «Suzumiya
        Haruhi no Shoushitsu» es de «Suzumiya Haruhi no Yuuutsu». None si es
        una serie o no cuelga de ninguna. Lanza AniListDown.
        """
        if not m or m.get("format") in MAIN_FORMATS:
            return None
        mains = [p for p in self._prequel_chain(m) if _is_main(p)] or self._parent_mains(m)
        if not mains:
            return None
        root = mains[-1]
        title = (root.get("title") or {}).get("romaji") or _names(root)[0]
        return split_season(title, "")[0] or None

    def _parent_mains(self, m) -> list:
        """La serie de la que cuelga `m` y sus temporadas anteriores ([] si no cuelga de ninguna)."""
        # algunas segundas temporadas cuelgan como PARENT ("Horimiya: Piece")
        parents = _main_edges(m, "PARENT")
        parent = self.client.media(parents[0]["id"]) if parents else None
        if not parent:
            return []
        return [parent] + [p for p in self._prequel_chain(parent) if _is_main(p)]

    def series_info(self, title: str, premiere: str = "", alt_titles=()) -> dict | None:
        """{"series": nombre de la serie, "format": "MOVIE"…} de un extra por su título, o None."""
        try:
            m = self.find_media(title, premiere, alt_titles)
            series = self.series_of(m)
        except AniListDown as e:
            log.warning("AniList no responde (%s): no sé de qué serie es %r", e, title)
            return None
        except Exception:
            log.exception("Fallo buscando la serie de %r", title)
            return None
        return {"series": series, "format": m.get("format")} if series else None

    def season_info(self, title: str, premiere: str = "", alt_titles=()):
        """
        {"base": "Darker than Black", "season": "2", "subtitle": "Ryuusei no Gemini"}
        o None si el título no es de este tipo o no hay datos fiables.
        """
        try:
            return self.info_for(self.find_media(title, premiere, alt_titles), title)
        except AniListDown as e:
            log.warning("AniList no responde (%s): %r se nombra como siempre", e, title)
            return None
        except Exception:
            log.exception("Fallo calculando la temporada de %r", title)
            return None

    def info_for(self, m, title: str):
        """
        Como season_info(), pero con la entrada de AniList ya elegida (la
        piden por su id las búsquedas por torrent). Lanza AniListDown.
        """
        if not m or m.get("format") not in MAIN_FORMATS:
            return None  # películas, OVAs, especiales: como siempre
        chain = self._prequel_chain(m)
        mains_before = sum(1 for p in chain if _is_main(p))
        if mains_before == 0:
            found = self._first_season(m, title)
        else:
            found = _later_season(m, title, chain, mains_before)
        if not found:
            return None
        base, season, tail = found
        info = {"base": base, "season": season, "subtitle": tail, "anilist_id": m["id"]}
        log.info("Temporada según AniList: %r -> %s", title, info)
        return info

    def _first_season(self, m, title: str):
        """
        (serie, "1", subtítulo) de una primera temporada: solo si tiene
        secuelas y el título trae subtítulo ("Darker than Black: Kuro no Keiyakusha").
        """
        head, tail = split_title(title)
        if not _is_main(m) or not tail or len(name_key(head)) < 3 or not self._has_main_sequel(m):
            return None
        return head, "1", tail


def _later_season(m, title: str, chain: list, mains_before: int):
    """(serie, temporada, subtítulo) partiendo el título por el nombre de la primera temporada, o None."""
    root = next(p for p in reversed(chain) if _is_main(p))
    bases = []   # en orden: el romaji de la primera temporada, primero
    for n in _names(root):
        bases += [n, split_title(n)[0]]
    split = _split_on_base(title, bases)
    if not split:
        log.info("AniList: %r no empieza por el nombre de su primera temporada (%s)",
                 title, _names(root)[:2])
        return None
    base, tail = split
    season = str(mains_before + 1) if _is_main(m) else f"{mains_before}.5"
    return base, season, tail


def _premiere_ok(m, premiere: str, n_cands: int) -> bool:
    """Una entrada con el título buscado vale si no contradice la fecha de estreno (o es la única)."""
    return not premiere or not _date(m) or _date(m) == premiere or n_cands == 1


def _by_name(cands: list, titles, premiere: str):
    """La primera entrada cuyo nombre coincide con uno de `titles` (y no contradice el estreno)."""
    # primero tal cual y luego sin signos: "Seitokai Yakuindomo" y
    # "Seitokai Yakuindomo*" (la segunda) son lo mismo sin el asterisco
    for key in (_strict, name_key):
        wanted = {key(t) for t in titles if t}
        for m in cands:
            if wanted & {key(n) for n in _names(m)} and _premiere_ok(m, premiere, len(cands)):
                return m
    return None


# ------------------------------------------------------------------ #
# Datos de la ficha de japanpaw
# ------------------------------------------------------------------ #
_PREMIERE_RE = re.compile(r"Fecha de Estreno\s*(\d{4}-\d{2}-\d{2})")
_ALT_RE = re.compile(r"Títulos Alternativos\s*(.+?)\s*Fecha de Estreno", re.S)


def page_facts(text: str):
    """(fecha_estreno, [títulos alternativos]) a partir del texto de la ficha."""
    premiere = ""
    alts = []
    m = _PREMIERE_RE.search(text or "")
    if m:
        premiere = m.group(1)
    m = _ALT_RE.search(text or "")
    if m:
        alts = [a.strip() for a in re.split(r"\s*[,;|]\s*|\n", m.group(1)) if a.strip()][:5]
    return premiere, alts
