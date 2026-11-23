"""
Selección de pistas (patrón estrategia).

Qué se conserva siempre: audio japonés + un audio en español (España, o
latino si no hay de España), y UN subtítulo en español (España > latino).
El inglés y el resto de idiomas quedan fuera siempre.

Lo que cambia según de dónde venga el archivo es qué se puede SUPONER
cuando las pistas no están bien etiquetadas: eso es la política
(TrackPolicy). StrictTrackPolicy no supone nada (releases de nyaa.si);
JapanPawTrackPolicy añade las suposiciones razonables para una web de la
comunidad hispanohablante. Cualquier política es intercambiable por otra
(Liskov): todas devuelven una TrackSelection con el mismo significado.
"""

import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("anime-automator")

UNTAGGED_LANGS = ("", "und", "un", "mis", "zxx")
JAPANESE = ("jpn", "ja")
SPANISH = ("spa", "es", "lat")


def lang_of(track) -> str:
    return (track["properties"].get("language") or "").lower()


@dataclass
class TrackSelection:
    jpn_audio_id: int | None = None
    keep_audio_ids: list = field(default_factory=list)
    keep_sub_ids: list = field(default_factory=list)
    default_sub_id: int | None = None
    clear_name_ids: list = field(default_factory=list)
    sub_requirement_waived: bool = False


class SpanishClassifier:
    """¿Una pista en español es de España o latina?"""

    def __init__(self, cfg: dict):
        self.spain_kw = [k.lower() for k in cfg["spain_keywords"]]
        self.latino_kw = [k.lower() for k in cfg["latino_keywords"]]

    def classify(self, track) -> str:
        props = track["properties"]
        name = (props.get("track_name") or "").lower()
        lang = (props.get("language") or "").lower()
        lang_ietf = (props.get("language_ietf") or "").lower()
        # Algunos fansubs (p.ej. PuyaSubs) usan el código "lat" -técnicamente
        # el de la lengua latina, pero lo reutilizan a su manera- para marcar la
        # pista de español latino. Es una señal directa y fiable.
        if lang == "lat":
            return "latino"
        haystack = f"{name} {lang} {lang_ietf}"
        if any(k in haystack for k in self.latino_kw):
            return "latino"
        if any(k in haystack for k in self.spain_kw):
            return "espana"
        # Ambiguo: código genérico "es"/"spa" sin pista textual -> se asume que
        # es la única variante disponible, se trata como "espana" (prioridad alta)
        return "espana"

    def best(self, tracks):
        """La de España si la hay; si no, la latina; si no hay ninguna, None."""
        classified = [(t, self.classify(t)) for t in tracks]
        espana = [t for t, c in classified if c == "espana"]
        latino = [t for t, c in classified if c == "latino"]
        return espana[0] if espana else (latino[0] if latino else None)


class StrictTrackPolicy:
    """Sin suposiciones: solo cuenta lo que está etiquetado."""

    def guess_japanese_audio(self, audio_tracks, info):
        return None

    def guess_spanish_sub(self, sub_tracks, info):
        """(pista elegida, ¿se renuncia a exigir subtítulos?)"""
        return None, False

    def fallback_audio(self, audio_tracks):
        """Qué audio conservar si ninguno es japonés ni español (None = se rechaza)."""
        return None

    def select(self, info: dict, cfg: dict) -> TrackSelection:
        classifier = SpanishClassifier(cfg)
        tracks = info["tracks"]
        audio = [t for t in tracks if t["type"] == "audio"]
        subs = [t for t in tracks if t["type"] == "subtitles"]

        jpn_audio = next((t for t in audio if lang_of(t) in JAPANESE), None)
        if jpn_audio is None:
            jpn_audio = self.guess_japanese_audio(audio, info)
        spa_audio = classifier.best([t for t in audio if lang_of(t) in SPANISH])
        keep_audio_ids = [t["id"] for t in (jpn_audio, spa_audio) if t]
        if not keep_audio_ids:
            fallback = self.fallback_audio(audio)
            if fallback:
                jpn_audio, keep_audio_ids = fallback, [fallback["id"]]

        chosen_sub = classifier.best([t for t in subs if lang_of(t) in SPANISH])
        waived = False
        if chosen_sub is None:
            chosen_sub, waived = self.guess_spanish_sub(subs, info)
        keep_sub_ids = [chosen_sub["id"]] if chosen_sub else []

        video_ids = [t["id"] for t in tracks if t["type"] == "video"]
        return TrackSelection(
            jpn_audio_id=jpn_audio["id"] if jpn_audio else None,
            keep_audio_ids=keep_audio_ids,
            keep_sub_ids=keep_sub_ids,
            default_sub_id=chosen_sub["id"] if chosen_sub else None,
            clear_name_ids=video_ids + keep_audio_ids + keep_sub_ids,
            sub_requirement_waived=waived,
        )


class JapanPawTrackPolicy(StrictTrackPolicy):
    """
    Para lo que viene de japan-paw.net, por el enlace o por su Drive (no para
    el fallback de nyaa.si). Esa web es de la comunidad hispanohablante y
    TODO lo que sube trae audio o subtítulos en español, aunque muchas veces
    sin etiquetar. Así que nunca se descarta un archivo por sus pistas:
    - audio: si no hay japonés etiquetado, el primero sin etiqueta; si ni
      eso, el primero que haya.
    - subtítulos: sin ninguno etiquetado en español, se lee el texto de los
      que no están etiquetados (o, si todos lo están, de todos) y se queda
      el que esté en español. Si hay uno solo sin etiquetar, ese. Si no hay
      subtítulos, se asume que van incrustados (hardsubs).
    - si aun así no aparece ninguno en español, se sigue sin subtítulos (y
      se deja constancia en el log) en vez de tirar el capítulo.
    """

    def guess_japanese_audio(self, audio_tracks, info):
        return next((t for t in audio_tracks if lang_of(t) in UNTAGGED_LANGS), None)

    def fallback_audio(self, audio_tracks):
        return audio_tracks[0] if audio_tracks else None

    def guess_spanish_sub(self, sub_tracks, info):
        if not sub_tracks:
            return None, True
        untagged = [t for t in sub_tracks if lang_of(t) in UNTAGGED_LANGS]
        if len(untagged) == 1:
            return untagged[0], False
        candidates = untagged or sub_tracks
        path = info.get("file_name")
        for t in candidates:
            text = subtitle_text(path, t) if path else ""
            if text and looks_spanish(text):
                return t, False
        if untagged and not any(is_text_sub(t) for t in untagged):
            return untagged[0], False  # imagen (PGS/VobSub): no se puede leer, el primero
        log.warning("No identifico subtítulos en español en %s (pistas %s): se sigue sin subtítulos",
                    path, [t["id"] for t in sub_tracks])
        return None, True


# ------------------------------------------------------------------ #
# ¿En qué idioma está un subtítulo de texto?
# ------------------------------------------------------------------ #
SPANISH_WORDS = {"que", "de", "el", "la", "los", "las", "es", "no", "y", "en", "un", "una", "por", "para",
                 "qué", "lo", "se", "me", "te", "con", "está", "pero", "eso", "esto", "sí", "mi", "tu", "yo"}
ENGLISH_WORDS = {"the", "you", "is", "and", "to", "of", "it", "that", "what", "this", "are", "i", "my",
                 "your", "we", "be", "have", "don't", "i'm", "it's", "with", "for", "not", "can"}
ASS_TAGS = re.compile(r"\{[^}]*\}|\\[Nnh]")


def is_text_sub(track) -> bool:
    return (track.get("properties", {}).get("codec_id") or "").startswith("S_TEXT")


def subtitle_text(path: str, track, limit: int = 200_000) -> str:
    """El texto de los diálogos de una pista de subtítulos de texto ('' si no se puede)."""
    if not is_text_sub(track):
        return ""
    try:
        with tempfile.TemporaryDirectory(dir=Path(path).parent) as tmp:
            out = Path(tmp) / "sub.txt"
            subprocess.run(["mkvextract", str(path), "tracks", f"{track['id']}:{out}"],
                           capture_output=True, timeout=120, check=True)
            raw = out.read_text("utf-8", "replace")[:limit]
    except Exception as e:
        log.debug("No se pudo extraer la pista %s de %s: %s", track.get("id"), path, e)
        return ""
    lines = []
    for line in raw.splitlines():
        if line.startswith("Dialogue:"):
            line = line.split(",", 9)[-1]
        elif "-->" in line or line.strip().isdigit() or line.startswith(("[", "Format:", "Style:")):
            continue
        lines.append(ASS_TAGS.sub(" ", line))
    return "\n".join(lines)


def looks_spanish(text: str) -> bool:
    words = re.findall(r"[a-záéíóúñü']+", text.lower())
    es = sum(w in SPANISH_WORDS for w in words) + 3 * (text.count("¿") + text.count("¡"))
    en = sum(w in ENGLISH_WORDS for w in words)
    return es >= 20 and es > 2 * en
