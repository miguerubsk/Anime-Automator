import unittest

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
