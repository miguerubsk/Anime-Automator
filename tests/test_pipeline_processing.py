"""De un archivo descargado (HTTP o torrent) a su sitio en la biblioteca."""

from types import SimpleNamespace
from unittest import mock

from tests.helpers import AutomatorTestCase


class FakeResponse:
    def __init__(self, chunks, length=None, error=None):
        self.chunks, self.error = chunks, error
        self.headers = {"Content-Length": str(length)} if length is not None else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.error:
            raise self.error

    def iter_content(self, chunk_size):
        yield from self.chunks


class HttpSourceTest(AutomatorTestCase):
    def fetch(self, response, every=30):
        from automator.pipeline.processing import BROWSER_HEADERS, HttpSource
        source = HttpSource()
        source.log_every_seconds = every
        item = self.item("Uno", 3, url="https://cdn.example/Uno%2003.mkv")
        with mock.patch("automator.pipeline.processing.get_with_bypass", return_value=response) as get:
            raw = source.fetch(self.cfg, item)
        get.assert_called_once_with("https://cdn.example/Uno%2003.mkv", self.cfg, stream=True, timeout=60,
                                    headers=BROWSER_HEADERS)
        return raw

    def test_downloads_to_tmp(self):
        raw = self.fetch(FakeResponse([b"ab", b"cd"], length=4))
        self.assertEqual(raw, self.root / "tmp" / "raw_Uno_3.mkv")
        self.assertEqual(raw.read_bytes(), b"abcd")

    def test_logs_progress_with_and_without_size(self):
        with self.assertLogs("anime-automator", "INFO") as logs:
            self.fetch(FakeResponse([b"ab", b"cd"], length=4), every=0)
        self.assertTrue(any("(50%)" in line for line in logs.output), logs.output)
        with self.assertLogs("anime-automator", "INFO") as logs:
            self.fetch(FakeResponse([b"ab"]), every=0)
        progress = [line for line in logs.output if "MB/s" in line]
        self.assertEqual(len(progress), 1)
        self.assertNotIn("%)", progress[0])

    def test_http_error_propagates(self):
        import requests
        with self.assertRaises(requests.HTTPError):
            self.fetch(FakeResponse([b"x"], error=requests.HTTPError("404")))


class FakeTorrent:
    def __init__(self, download_dir, files, old_api=False):
        self.download_dir = download_dir
        if old_api:
            self.files = lambda: [SimpleNamespace(**f) for f in files]
        else:
            self.get_files = lambda: files


class TorrentFileSourceTest(AutomatorTestCase):
    def fetch(self, torrent):
        from automator.pipeline.processing import TorrentFileSource
        return TorrentFileSource(torrent).fetch(self.cfg, self.item("Uno", 2))

    def test_copies_the_biggest_mkv_and_keeps_the_original(self):
        dl = self.root / "torrents"
        (dl / "Pack").mkdir(parents=True)
        (dl / "Pack" / "ep.mkv").write_bytes(b"video")
        files = [{"name": "Pack/sample.mkv", "size": 10}, {"name": "Pack/ep.mkv", "size": 900},
                 {"name": "Pack/big.nfo", "size": 5000}]
        for old_api in (False, True):
            raw = self.fetch(FakeTorrent(str(dl), files, old_api=old_api))
            self.assertEqual(raw, self.root / "tmp" / "raw_torrent_Uno_2.mkv")
            self.assertEqual(raw.read_bytes(), b"video")
            self.assertTrue((dl / "Pack" / "ep.mkv").exists())   # sigue sembrando

    def test_without_mkv(self):
        with self.assertRaisesRegex(RuntimeError, "ningún archivo .mkv"):
            self.fetch(FakeTorrent(str(self.root), [{"name": "a.mp4", "size": 1}]))

    def test_file_not_found(self):
        with self.assertRaisesRegex(RuntimeError, "No encuentro el archivo descargado"):
            self.fetch(FakeTorrent(str(self.root), [{"name": "a.MKV", "size": 1}]))


class PlaceTest(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        lower = mock.patch("automator.pipeline.processing.remove_lower_quality_copies")
        self.lower = lower.start()
        self.addCleanup(lower.stop)

    def processed(self):
        p = self.root / "tmp" / "proc_x.mkv"
        p.write_bytes(b"remux")
        return p

    def place(self, item, processed):
        from automator.pipeline.processing import EpisodeProcessor
        return EpisodeProcessor(remuxer=mock.Mock()).place(self.cfg, item, processed)

    def test_episode_goes_to_season_1_once_there_are_seasons(self):
        base = self.root / "Anime" / "Uno"
        (base / "Temporada 2").mkdir(parents=True)
        processed = self.processed()
        with mock.patch("automator.library.folders.Library.organize_seasons") as organize:
            dest = self.place(self.item("Uno", 1), processed)
        self.assertEqual(dest, base / "Temporada 1" / "01.mkv")
        self.assertEqual(dest.read_bytes(), b"remux")
        organize.assert_called_once_with(self.cfg, base)   # se reorganiza la serie
        self.assertFalse(processed.exists())
        self.assertEqual(self.lower.call_args.args[1], dest)

    def test_movie_keeps_its_file_name(self):
        processed = self.processed()
        target = {"movie": True, "filename": "Película 01.mkv", "key": "movie:x"}
        with mock.patch("automator.library.folders.Library.organize_seasons") as organize:
            dest = self.place(self.item("Uno", 1, **target), processed)
        self.assertEqual(dest, self.root / "Anime" / "Uno" / "Película 01.mkv")
        organize.assert_not_called()

    def test_housekeeping_failures_do_not_undo_the_placement(self):
        self.lower.side_effect = OSError("ffprobe")
        with mock.patch("automator.library.folders.Library.organize_seasons", side_effect=OSError("smb")), \
                self.assertLogs("anime-automator", "ERROR") as logs:
            dest = self.place(self.item("Uno", 4), self.processed())
        self.assertEqual(dest.read_bytes(), b"remux")
        self.assertEqual(len(logs.output), 2)

    def test_nas_not_mounted_writes_nothing_and_cleans_tmp(self):
        processed = self.processed()
        with mock.patch("automator.pipeline.processing.assert_nas_mounted", side_effect=RuntimeError("sin NAS")), \
                self.assertRaises(RuntimeError):
            self.place(self.item("Uno", 1), processed)
        self.assertFalse(processed.exists())
        self.assertFalse((self.root / "Anime" / "Uno").exists())
