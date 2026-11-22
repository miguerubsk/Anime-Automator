"""mkvmerge: información de pistas y remux."""

import json
import logging
import re
import subprocess
from pathlib import Path

from .tracks import TrackSelection

log = logging.getLogger("anime-automator")


def mkv_track_info(path: Path) -> dict:
    out = subprocess.run(["mkvmerge", "-J", str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def video_height(path: Path) -> int | None:
    """Altura en píxeles del primer vídeo (mkvmerge lee también mp4/avi), o None."""
    try:
        info = mkv_track_info(path)
    except Exception:
        return None
    for t in info.get("tracks") or []:
        if t.get("type") == "video":
            dims = (t.get("properties") or {}).get("pixel_dimensions", "")
            m = re.fullmatch(r"(\d+)x(\d+)", dims)
            if m:
                return int(m.group(2))
    return None


class Remuxer:
    """Genera el .mkv final con solo las pistas elegidas, sin nombres y con los flags por defecto."""

    def validate(self, selection: TrackSelection):
        if not selection.keep_audio_ids:
            raise RuntimeError("No se encontró ninguna pista de audio válida (ni japonés ni español)")
        # Si se ve con audio japonés (que es el caso por defecto), hacen falta
        # subtítulos en español — sin ellos el archivo no sirve, aunque tenga
        # subtítulos en otros idiomas o "MultiSub" en el nombre. Salvo que se
        # haya determinado que no hace falta (p.ej. japan-paw sin pistas de
        # subs = hardsubs ya incrustados).
        if selection.jpn_audio_id is not None and not selection.keep_sub_ids \
                and not selection.sub_requirement_waived:
            raise RuntimeError(
                "No se encontraron subtítulos en español (España/Latino) en un "
                "archivo con audio japonés — sin subtítulos en español no sirve"
            )

    def command(self, src: Path, dst: Path, selection: TrackSelection) -> list[str]:
        cmd = ["mkvmerge", "-o", str(dst),
               "--audio-tracks", ",".join(str(i) for i in selection.keep_audio_ids)]
        if selection.keep_sub_ids:
            cmd += ["--subtitle-tracks", ",".join(str(i) for i in selection.keep_sub_ids)]
        else:
            cmd += ["--no-subtitles"]
        # limpiar nombres de todas las pistas que se conservan (vídeo, audio, subs)
        for tid in selection.clear_name_ids:
            cmd += ["--track-name", f"{tid}:"]
        # limpiar también el título general del contenedor mkv
        cmd += ["--title", ""]
        # flags por defecto
        for tid in selection.keep_audio_ids:
            cmd += ["--default-track-flag", f"{tid}:{'1' if tid == selection.jpn_audio_id else '0'}"]
        for tid in selection.keep_sub_ids:
            cmd += ["--default-track-flag", f"{tid}:{'1' if tid == selection.default_sub_id else '0'}"]
        cmd.append(str(src))
        return cmd

    def remux(self, src: Path, dst: Path, selection: TrackSelection):
        self.validate(selection)
        cmd = self.command(src, dst, selection)
        log.info("Ejecutando: %s", " ".join(cmd))
        subprocess.run(cmd, check=True)
