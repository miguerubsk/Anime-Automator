"""mkvmerge/mkvextract: altura del vídeo y texto de los subtítulos (con los programas falseados)."""

import json
import subprocess
from pathlib import Path
from unittest import mock

from tests.helpers import AutomatorTestCase

CFG = {"spain_keywords": ["españa", "castellano"], "latino_keywords": ["latino", "es-419"]}

ASS = """[Script Info]
Title: Capítulo 1

[V4+ Styles]
Format: Name, Fontname
Style: Default,Arial

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\i1}¿Qué pasa,{\\b1} amigo?\\Nnada
Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,Hola\\hmundo
"""

SRT = """1
00:00:01,000 --> 00:00:02,000
Hola, ¿qué tal?

12
00:00:03,000 --> 00:00:04,000
Bien
"""

SPANISH = "\n".join(["¿Qué es lo que pasa? No lo sé, pero es para ti y para mí."] * 4)
ENGLISH = "\n".join(["What is it that you want? I don't know, but it's for you and me."] * 4)


def track(tid, kind, lang="", codec="S_TEXT/ASS"):
    return {"id": tid, "type": kind, "properties": {"language": lang, "codec_id": codec}}


class FakeMkvextract:
    """`mkvextract <archivo> tracks <id>:<salida>`: escribe en la salida el texto de `texts[id]`."""

    def __init__(self, texts):
        self.texts, self.calls = texts, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        tid, out = cmd[3].split(":", 1)
        if int(tid) not in self.texts:
            raise subprocess.CalledProcessError(2, cmd)
        Path(out).write_text(self.texts[int(tid)], "utf-8")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")


class SubtitleTextTest(AutomatorTestCase):
    def text(self, texts, tr, **kw):
        from automator.media.tracks import subtitle_text
        fake = FakeMkvextract(texts)
        video = self.root / "tmp" / "x.mkv"
        with mock.patch("automator.media.tracks.subprocess.run", fake):
            return subtitle_text(str(video), tr, **kw), fake.calls

    def test_ass_dialogue_without_tags(self):
        text, calls = self.text({3: ASS}, track(3, "subtitles"))
        # las cabeceras que no son [sección], Format: ni Style: se quedan (no estorban para el idioma)
        self.assertEqual(text, "Title: Capítulo 1\n\n\n ¿Qué pasa,  amigo? nada\nHola mundo")
        self.assertEqual(calls[0][:3], ["mkvextract", str(self.root / "tmp" / "x.mkv"), "tracks"])
        self.assertEqual(Path(calls[0][3].split(":", 1)[1]).parent.parent, self.root / "tmp")  # junto al vídeo

    def test_srt_without_numbers_or_times(self):
        text, _ = self.text({2: SRT}, track(2, "subtitles", codec="S_TEXT/UTF8"))
        self.assertEqual(text, "Hola, ¿qué tal?\n\nBien")

    def test_limit(self):
        text, _ = self.text({2: "abcdef"}, track(2, "subtitles", codec="S_TEXT/UTF8"), limit=3)
        self.assertEqual(text, "abc")

    def test_image_subtitles_are_not_extracted(self):
        text, calls = self.text({2: SRT}, track(2, "subtitles", codec="S_HDMV/PGS"))
        self.assertEqual((text, calls), ("", []))

    def test_extraction_failure(self):
        self.assertEqual(self.text({}, track(2, "subtitles"))[0], "")


class GuessSpanishSubTest(AutomatorTestCase):
    """JapanPawTrackPolicy lee los subtítulos sin etiquetar con mkvextract para dar con el español."""

    def select(self, tracks, texts):
        from automator.media.tracks import JapanPawTrackPolicy
        info = {"file_name": str(self.root / "tmp" / "x.mkv"), "tracks": tracks}
        with mock.patch("automator.media.tracks.subprocess.run", FakeMkvextract(texts)):
            return JapanPawTrackPolicy().select(info, CFG)

    def test_reads_the_untagged_ones(self):
        sel = self.select([track(1, "audio", "jpn"), track(2, "subtitles"), track(3, "subtitles", "und")],
                          {2: ENGLISH, 3: SPANISH})
        self.assertEqual((sel.keep_sub_ids, sel.default_sub_id, sel.sub_requirement_waived), ([3], 3, False))

    def test_image_subtitles_first_untagged(self):
        sel = self.select([track(1, "audio", "jpn"), track(2, "subtitles", codec="S_HDMV/PGS"),
                           track(3, "subtitles", codec="S_VOBSUB")], {})
        self.assertEqual((sel.keep_sub_ids, sel.sub_requirement_waived), ([2], False))

    def test_without_file_name_nothing_is_read(self):
        from automator.media.tracks import JapanPawTrackPolicy
        info = {"tracks": [track(1, "audio", "jpn"), track(2, "subtitles"), track(3, "subtitles")]}
        with mock.patch("automator.media.tracks.subprocess.run") as run:
            sel = JapanPawTrackPolicy().select(info, CFG)
        run.assert_not_called()
        self.assertEqual((sel.keep_sub_ids, sel.sub_requirement_waived), ([], True))

    def test_generic_spanish_code_counts_as_spain(self):
        from automator.media.tracks import StrictTrackPolicy
        info = {"tracks": [track(0, "video", codec="V"), track(1, "audio", "jpn"), track(2, "audio", "spa"),
                           track(3, "subtitles", "es")]}
        sel = StrictTrackPolicy().select(info, CFG)
        self.assertEqual((sel.keep_audio_ids, sel.keep_sub_ids, sel.clear_name_ids), ([1, 2], [3], [0, 1, 2, 3]))

    def test_strict_rejects_unknown_audio(self):
        from automator.media.tracks import StrictTrackPolicy
        sel = StrictTrackPolicy().select({"tracks": [track(1, "audio", "eng"), track(2, "subtitles", "spa")]}, CFG)
        self.assertEqual((sel.jpn_audio_id, sel.keep_audio_ids, sel.keep_sub_ids), (None, [], [2]))


class FakeMkvmerge:
    def __init__(self, info):
        self.info = info

    def __call__(self, cmd, **kw):
        if isinstance(self.info, Exception):
            raise self.info
        return subprocess.CompletedProcess(cmd, 0, self.info if isinstance(self.info, str) else json.dumps(self.info), "")


class VideoHeightTest(AutomatorTestCase):
    def height(self, info):
        from automator.media.mkv import video_height
        with mock.patch("automator.media.mkv.subprocess.run", FakeMkvmerge(info)):
            return video_height(self.root / "x.mp4")

    def test_first_video_with_dimensions(self):
        info = {"tracks": [{"type": "audio", "properties": {}},
                           {"type": "video"},
                           {"type": "video", "properties": {"pixel_dimensions": "raro"}},
                           {"type": "video", "properties": {"pixel_dimensions": "1920x1080"}},
                           {"type": "video", "properties": {"pixel_dimensions": "1280x720"}}]}
        self.assertEqual(self.height(info), 1080)

    def test_unknown(self):
        self.assertIsNone(self.height({"tracks": [{"type": "audio", "properties": {}}]}))
        self.assertIsNone(self.height({}))
        self.assertIsNone(self.height("no es json"))
        self.assertIsNone(self.height(subprocess.CalledProcessError(2, ["mkvmerge"])))
        self.assertIsNone(self.height(FileNotFoundError("mkvmerge")))

    def test_mkvmerge_is_asked_for_json(self):
        from automator.media.mkv import mkv_track_info
        with mock.patch("automator.media.mkv.subprocess.run", FakeMkvmerge({"tracks": []})):
            self.assertEqual(mkv_track_info(Path("/a/b.mkv")), {"tracks": []})
