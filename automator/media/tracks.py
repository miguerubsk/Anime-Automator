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

from dataclasses import dataclass, field

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

    def guess_japanese_audio(self, audio_tracks):
        return None

    def guess_spanish_sub(self, sub_tracks):
        """(pista elegida, ¿se renuncia a exigir subtítulos?)"""
        return None, False

    def select(self, info: dict, cfg: dict) -> TrackSelection:
        classifier = SpanishClassifier(cfg)
        tracks = info["tracks"]
        audio = [t for t in tracks if t["type"] == "audio"]
        subs = [t for t in tracks if t["type"] == "subtitles"]

        jpn_audio = next((t for t in audio if lang_of(t) in JAPANESE), None)
        if jpn_audio is None:
            jpn_audio = self.guess_japanese_audio(audio)
        spa_audio = classifier.best([t for t in audio if lang_of(t) in SPANISH])
        keep_audio_ids = [t["id"] for t in (jpn_audio, spa_audio) if t]

        chosen_sub = classifier.best([t for t in subs if lang_of(t) in SPANISH])
        waived = False
        if chosen_sub is None:
            chosen_sub, waived = self.guess_spanish_sub(subs)
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
    Para archivos que vienen directamente de japan-paw.net (no para el
    fallback de nyaa.si): esa web es de la comunidad hispanohablante, así que:
    - si no hay NINGUNA pista de subtítulos, se asume que el vídeo ya trae
      los subtítulos en español incrustados (hardsubs), y no se exige nada.
    - si hay exactamente UNA pista de subtítulos sin idioma etiquetado, se
      asume que es la española.
    - si hay exactamente UNA pista de audio sin idioma etiquetado, se asume
      que es la original japonesa (los doblajes casi siempre se etiquetan).
    """

    def guess_japanese_audio(self, audio_tracks):
        if len(audio_tracks) == 1 and lang_of(audio_tracks[0]) in UNTAGGED_LANGS:
            return audio_tracks[0]
        return None

    def guess_spanish_sub(self, sub_tracks):
        if not sub_tracks:
            return None, True
        if len(sub_tracks) == 1 and lang_of(sub_tracks[0]) in UNTAGGED_LANGS:
            return sub_tracks[0], False
        return None, False
