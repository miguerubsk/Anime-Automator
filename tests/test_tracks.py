import subprocess
import unittest
from unittest import mock

from automator.media.mkv import Remuxer
from automator.media.tracks import JapanPawTrackPolicy, StrictTrackPolicy

CFG = {"spain_keywords": ["españa", "castellano", "spa"], "latino_keywords": ["latino", "es-419"]}


def track(tid, kind, lang="", name=""):
    return {"id": tid, "type": kind, "properties": {"language": lang, "track_name": name}}


class TrackPolicyTest(unittest.TestCase):
    def test_prefers_spain_over_latino_and_drops_english(self):
        info = {"tracks": [track(0, "video"), track(1, "audio", "jpn"), track(2, "audio", "spa", "Latino"),
                           track(3, "audio", "spa", "Castellano"), track(4, "subtitles", "eng"),
                           track(5, "subtitles", "spa", "Latino"), track(6, "subtitles", "spa", "España")]}
        sel = StrictTrackPolicy().select(info, CFG)
        self.assertEqual(sel.jpn_audio_id, 1)
        self.assertEqual(sel.keep_audio_ids, [1, 3])
        self.assertEqual(sel.keep_sub_ids, [6])
        self.assertEqual(sel.clear_name_ids, [0, 1, 3, 6])

    def test_lat_code_is_latino(self):
        info = {"tracks": [track(1, "audio", "jpn"), track(2, "subtitles", "lat")]}
        self.assertEqual(StrictTrackPolicy().select(info, CFG).keep_sub_ids, [2])

    def test_japanpaw_assumptions(self):
        untagged = {"tracks": [track(0, "video"), track(1, "audio", "und"), track(2, "subtitles", "")]}
        strict = StrictTrackPolicy().select(untagged, CFG)
        lenient = JapanPawTrackPolicy().select(untagged, CFG)
        self.assertEqual((strict.jpn_audio_id, strict.keep_sub_ids), (None, []))
        self.assertEqual((lenient.jpn_audio_id, lenient.keep_sub_ids), (1, [2]))

        hardsubs = {"tracks": [track(0, "video"), track(1, "audio", "jpn")]}
        self.assertTrue(JapanPawTrackPolicy().select(hardsubs, CFG).sub_requirement_waived)
        self.assertFalse(StrictTrackPolicy().select(hardsubs, CFG).sub_requirement_waived)

    def test_remuxer_requires_spanish_subs_with_japanese_audio(self):
        info = {"tracks": [track(0, "video"), track(1, "audio", "jpn"), track(2, "subtitles", "eng")]}
        with self.assertRaisesRegex(RuntimeError, "subtítulos en español"):
            Remuxer().validate(StrictTrackPolicy().select(info, CFG))

    def test_remux_command(self):
        info = {"tracks": [track(0, "video"), track(1, "audio", "jpn"), track(2, "subtitles", "spa")]}
        cmd = Remuxer().command("in.mkv", "out.mkv", JapanPawTrackPolicy().select(info, CFG))
        self.assertEqual(cmd, ["mkvmerge", "-o", "out.mkv", "--audio-tracks", "1", "--subtitle-tracks", "2",
                               "--track-name", "0:", "--track-name", "1:", "--track-name", "2:", "--title", "",
                               "--default-track-flag", "1:1", "--default-track-flag", "2:1", "in.mkv"])


    def test_remux_accepts_mkvmerge_warnings_but_not_errors(self):
        """mkvmerge sale con 1 si solo hubo avisos (el archivo vale) y con 2 si falló."""
        info = {"tracks": [track(0, "video"), track(1, "audio", "jpn"), track(2, "subtitles", "spa")]}
        sel = JapanPawTrackPolicy().select(info, CFG)
        def run(code, out):
            def fake(cmd, capture_output=False, text=False, **kw):
                # sin capturar la salida (como texto) no habría nada que dejar en el log
                got = out if capture_output and text else None
                return subprocess.CompletedProcess(cmd, code, stdout=got, stderr="" if got else None)
            return mock.patch("automator.media.mkv.subprocess.run", side_effect=fake)

        with run(1, "Warning: 267 bytes of invalid data"), self.assertLogs("anime-automator", "WARNING") as logs:
            Remuxer().remux("in.mkv", "out.mkv", sel)  # no lanza
        self.assertIn("267 bytes of invalid data", "\n".join(logs.output))
        with run(0, "todo bien"), self.assertNoLogs("anime-automator", "WARNING"):
            Remuxer().remux("in.mkv", "out.mkv", sel)
        with run(2, "Error: no space left"), self.assertLogs("anime-automator", "ERROR") as logs, \
                self.assertRaisesRegex(subprocess.CalledProcessError, "exit status 2"):
            Remuxer().remux("in.mkv", "out.mkv", sel)
        self.assertIn("no space left", "\n".join(logs.output))


def text_sub(tid, lang=""):
    t = track(tid, "subtitles", lang)
    t["properties"]["codec_id"] = "S_TEXT/ASS"
    return t


ES = "¿Qué? Todos la miran. Ven conmigo, no es lo que parece. ¡Eso es una mala idea! " * 5
EN = "What? Everyone is looking at her. Come with me, it's not what you think. " * 5


class JapanPawNeverRejectsTest(unittest.TestCase):
    """Lo de japan-paw siempre trae español: nunca se descarta por las pistas."""

    def select(self, tracks, texts):
        from unittest import mock
        info = {"file_name": "/tmp/x.mkv", "tracks": tracks}
        with mock.patch("automator.media.tracks.subtitle_text", lambda path, t: texts.get(t["id"], "")):
            sel = JapanPawTrackPolicy().select(info, CFG)
        Remuxer().validate(sel)  # no lanza
        return sel

    def test_two_untagged_spanish_subs_berserk(self):
        sel = self.select([track(0, "video"), track(1, "audio", "und"), text_sub(2), text_sub(3)], {2: ES, 3: ES})
        self.assertEqual((sel.jpn_audio_id, sel.keep_sub_ids), (1, [2]))

    def test_picks_spanish_over_untagged_english(self):
        sel = self.select([track(1, "audio", "jpn"), text_sub(2), text_sub(3)], {2: EN, 3: ES})
        self.assertEqual(sel.keep_sub_ids, [3])

    def test_mislabeled_spanish_sub(self):
        sel = self.select([track(1, "audio", "jpn"), text_sub(2, "eng"), text_sub(3, "eng")], {2: EN, 3: ES})
        self.assertEqual(sel.keep_sub_ids, [3])

    def test_unreadable_subs_do_not_reject(self):
        sel = self.select([track(1, "audio", "jpn"), text_sub(2, "eng")], {2: EN})
        self.assertEqual(sel.keep_sub_ids, [])
        self.assertTrue(sel.sub_requirement_waived)

    def test_any_audio_is_kept(self):
        sel = self.select([track(1, "audio", "eng"), track(2, "audio", "fre"), text_sub(3, "spa")], {})
        self.assertEqual((sel.keep_audio_ids, sel.keep_sub_ids), ([1], [3]))

    def test_nyaa_is_still_strict(self):
        info = {"tracks": [track(1, "audio", "jpn"), text_sub(2), text_sub(3)]}
        with self.assertRaises(RuntimeError):
            Remuxer().validate(StrictTrackPolicy().select(info, CFG))


class LanguageGuessTest(unittest.TestCase):
    def test_looks_spanish(self):
        from automator.media.tracks import looks_spanish
        self.assertTrue(looks_spanish(ES))
        self.assertFalse(looks_spanish(EN))
        self.assertFalse(looks_spanish("¿?"))
