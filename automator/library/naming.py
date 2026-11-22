"""Nombres de carpeta y número de temporada a partir de los títulos."""

import re
import unicodedata

ROMAN_TO_INT = {
    "II": 2, "III": 3, "IV": 4, "V": 5,
    "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10,
}
_ROMAN_ALT = "|".join(sorted(ROMAN_TO_INT, key=len, reverse=True))
ROMAN_SEASON_RE = re.compile(r"(?:^|\s)(" + _ROMAN_ALT + r")(?=:|\s|$)")
ORDINAL_SEASON_RE = re.compile(r"(\d+)(?:st|nd|rd|th)\s+Season", re.IGNORECASE)
PLAIN_SEASON_RE = re.compile(r"\bSeason\s+(\d+)\b", re.IGNORECASE)
PART_SEASON_RE = re.compile(r"\bPart\s+(\d+)\b", re.IGNORECASE)
COUR_SEASON_RE = re.compile(r"\bCour\s+(\d+)\b", re.IGNORECASE)
# "S2", "S3"... pegado sin espacio — habitual en títulos de releases
# (p.ej. "... S2 - 01 [...]"). Se excluye si en realidad es un "SxxExx"
# (ese caso ya lo cubre otra lógica en parse_nyaa_release) para no comerse
# el número de episodio como si fuera de temporada.
BARE_S_SEASON_RE = re.compile(r"\bS(\d{1,2})\b(?!\s*E\d)", re.IGNORECASE)
# Último recurso: un número suelto al final del título ("One Punch Man 3",
# "Attack on Titan 2", "Dr. Stone 3"). Restringido a 2-9 (una sola cifra) a
# propósito: los números de temporada reales casi nunca pasan de un dígito,
# y así evitamos falsos positivos con títulos que YA llevan un número como
# parte de su nombre real ("Mob Psycho 100", "Zom 100", "86") — esos son de
# 2-3 cifras y quedan fuera. Requiere que haya texto antes (con espacio de
# por medio), para no disparar con un título que sea solo un número.
BARE_NUMBER_SEASON_RE = re.compile(r"(?<=\S)\s+([2-9])\s*$")
INVALID_FS_CHARS_RE = re.compile(r'[<>"/\\|?*]')

# Temporada entre paréntesis/corchetes: "Berserk (S1) (2016)",
# "Nombre [Season 2]", "Nombre (2nd Season)". Se quita con los paréntesis,
# así no queda un "()" huérfano en el nombre de la carpeta.
BRACKET_SEASON_RE = re.compile(
    r"[\(\[]\s*(?:S|Season\s*|Temporada\s*)(\d{1,2})\s*[\)\]]"
    r"|[\(\[]\s*(\d{1,2})(?:st|nd|rd|th)\s+Season\s*[\)\]]",
    re.IGNORECASE,
)

# Se intentan en este orden (el primero que encaje gana). Los más propensos
# a falsos positivos van al final.
SEASON_PATTERNS = [
    (BRACKET_SEASON_RE, lambda m: int(m.group(1) or m.group(2))),
    (ORDINAL_SEASON_RE, lambda m: int(m.group(1))),
    (PLAIN_SEASON_RE, lambda m: int(m.group(1))),
    (PART_SEASON_RE, lambda m: int(m.group(1))),
    (COUR_SEASON_RE, lambda m: int(m.group(1))),
    (ROMAN_SEASON_RE, lambda m: ROMAN_TO_INT[m.group(1)]),
    (BARE_S_SEASON_RE, lambda m: int(m.group(1))),
    (BARE_NUMBER_SEASON_RE, lambda m: int(m.group(1))),
]

MAX_FOLDER_NAME_BYTES = 150
_GROUP_PREFIX_RE = re.compile(r"^\s*\[[^\]]+\]\s*")
_SEASON_FOLDER_RE = re.compile(r"Temporada\s+(\d+)")


def match_season(text: str):
    """(match, temporada) del primer patrón de temporada que encaje en `text`, o (None, None)."""
    for pattern, extract in SEASON_PATTERNS:
        m = pattern.search(text)
        if m:
            return m, extract(m)
    return None, None


def strip_group_prefix(title: str) -> str:
    """Quita el "[Grupo] " del principio de un título de release."""
    return _GROUP_PREFIX_RE.sub("", title)


def trim_name(text: str) -> str:
    """
    Quita espacios y puntos de los extremos (Windows no admite un punto o un
    espacio final) y los separadores " - " que se hayan quedado colgando al
    quitar la temporada o el sufijo de la web. Los guiones que forman parte
    del nombre se respetan ("9-nine-", "-Rewrite-").
    """
    prev = None
    while prev != text:
        prev = text
        text = text.strip()
        text = re.sub(r"(?:\s+-)+$", "", text)
        text = re.sub(r"^(?:-\s+)+", "", text)
        text = text.rstrip(". ")
    return text


def name_key(text: str) -> str:
    """
    Clave para comparar nombres de anime sin que importen mayúsculas,
    espacios, signos ni las distintas formas de escribir las vocales largas:
    "Dan Da Dan" = "Dandadan", "Maoyū Maō Yūsha" = "Maoyuu Maou Yuusha",
    "Darker than Black - Kuro no Keiyakusha" = "Darker Than Black: Kuro no
    Keiyakusha", "Bōgyoryoku" = "Bougyoryoku".
    """
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).casefold()
    t = t.replace("&", "and")
    t = re.sub(r"[\W_]+", "", t)
    t = re.sub(r"o[ou]", "o", t)   # ou / oo -> o
    t = re.sub(r"u+", "u", t)      # uu -> u
    return t


def norm_words(text: str) -> list[str]:
    """Palabras en minúsculas y sin tildes: "Overlord Movie 1" -> ["overlord", "movie", "1"]."""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.findall(r"[a-z0-9]+", t)


def shorten_folder_name(name: str, max_bytes: int = MAX_FOLDER_NAME_BYTES) -> str:
    """
    Deja un nombre válido como carpeta: sin punto/espacio final (Windows y
    SMB los rechazan) y con longitud limitada (límite Linux: 255 bytes).
    Si es demasiado largo se corta en el primer separador " - " (el subtítulo
    larguísimo de muchos light novels) y, si aún sobra, en un límite de palabra.
    """
    name = trim_name(name)
    if len(name.encode("utf-8")) > max_bytes:
        head = name.split(" - ", 1)[0]
        if len(trim_name(head)) >= 5:
            name = trim_name(head)
    if len(name.encode("utf-8")) > max_bytes:
        cut = name.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        name = trim_name(cut)
    return name or "Sin titulo"


def strip_site_suffix(raw_title: str, site_suffix: str) -> str:
    title = raw_title.strip()
    if site_suffix and title.endswith(site_suffix):
        title = trim_name(title[: -len(site_suffix)])
    return title


def split_season(raw_title: str, site_suffix: str):
    """(título_sin_temporada, temporada, ¿lo decía el título?)"""
    title = strip_site_suffix(raw_title, site_suffix)
    m, season = match_season(title)
    if m is None:
        return title, 1, False
    title = title[: m.start()] + title[m.end():]
    # por si algún patrón deja paréntesis/corchetes vacíos
    title = re.sub(r"\(\s*\)|\[\s*\]", " ", title)
    return trim_name(re.sub(r"\s{2,}", " ", title)), season, True


# Palabras inglesas con "ou"/"uu"/"oo" que no son vocales largas japonesas
_ENGLISH_OU = set("""
you your yours yourself yourselves you're you've you'll young youth house houses soul souls hour hours
our ours out outside outlaw outlaws outbreak outer about round sound ground grounds group groups touch
through though thought thoughts country double trouble would could should tour tours four pour mount
mountain mountains count counter counts cloud clouds loud proud south mouth route routes routine cousin
cousins enough rough tough journey journeys shout scout scouts mouse blouse couple course source
resource soup lounge bounty bound boundary fountain thousand doubt account amount announce bounce
pounce flour honour honor colour labour favour rumour humour neighbour vacuum continuum shoujo-ai
""".split())
_ENGLISH_END_RE = re.compile(r"(?:ous|ough|ould|ours?|ound|ount|outh|ouse|out|oup|ouch)$")


def prefer_macrons(title: str) -> str:
    """
    "Maoyuu Maou Yuusha" -> "Maoyū Maō Yūsha": vocales largas del romaji con
    macrón (ou -> ō, uu -> ū), que es la forma corta. No toca las palabras
    inglesas ("You", "Soul", "House"...). name_key() considera iguales las
    dos formas, así que una carpeta ya existente se encuentra igual.
    """
    def conv(word: str) -> str:
        low = word.casefold()
        letters = re.sub(r"[^a-z]", "", low)
        if not letters or letters in _ENGLISH_OU or _ENGLISH_END_RE.search(letters):
            return word
        if not re.search(r"ou|uu", low):
            return word
        out = re.sub(r"ou", "ō", word)
        out = re.sub(r"uu", "ū", out)
        out = re.sub(r"Ou|OU", "Ō", out)
        out = re.sub(r"Uu|UU", "Ū", out)
        return out

    return re.sub(r"[A-Za-z']+", lambda m: conv(m.group(0)), title)


def folder_safe(title: str) -> str:
    """Nombre de carpeta/archivo válido en Windows y Linux, con macrones si se puede."""
    title = title.replace(":", " -")
    # "Ranma 1/2" -> "Ranma 1-2" (la barra separaría carpetas)
    title = title.replace("/", "-")
    title = INVALID_FS_CHARS_RE.sub("", title)
    title = trim_name(re.sub(r"\s{2,}", " ", title))
    return shorten_folder_name(prefer_macrons(title))


def derive_anime_name_and_season(raw_title: str, site_suffix: str):
    """
    "Tensei Shitara Slime Datta Ken 4th Season - Japan-Paw! Descarga Calidad"
    -> ("Tensei Shitara Slime Datta Ken", 4). Sin indicador, temporada 1.
    """
    title, season, _ = split_season(raw_title, site_suffix)
    return folder_safe(title), season


def clean_subtitle(sub: str) -> str:
    """"~Pure Engagement~" -> "Pure Engagement" (adornos del título, no del nombre)."""
    return re.sub(r"^[\s~～☆★♪]+|[\s~～☆★♪]+$", "", sub or "")


def season_from_folder(folder: str) -> int:
    m = _SEASON_FOLDER_RE.search(folder)
    return int(m.group(1)) if m else 1
