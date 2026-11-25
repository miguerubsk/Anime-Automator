"""Encolado: resúmenes para Telegram, enlaces ya sabidos (Drive) y los casos de series y películas."""

from unittest import mock

from tests.helpers import AutomatorTestCase
from tests.test_enqueue import PAGE, link

URL = "https://paste.japan-paw.net/?v=1"


class DescribeResultTest(AutomatorTestCase):
    def describe(self, **result):
        from automator.pipeline.enqueue import describe_download_result
        return describe_download_result(result)

    def test_movie_queued(self):
        txt = self.describe(kind="movie", status="queued", queued=[1], name="Peli", folder="Anime/X", file="P.mkv")
        self.assertEqual(txt, "🎬 Película <b>Peli</b> en cola.\nDestino: Anime/X/P.mkv")

    def test_movie_already_queued(self):
        txt = self.describe(kind="movie", status="queued", queued=[], name="Peli")
        self.assertEqual(txt, "🎬 La película <b>Peli</b> ya estaba en cola.")

    def test_movie_already_had(self):
        txt = self.describe(kind="movie", status="nothing_new", name="Peli", folder="Anime/X", file="P.mkv")
        self.assertEqual(txt, "✅ Ya tienes la película <b>Peli</b>.\nAnime/X/P.mkv")

    def test_movie_error_is_silent(self):
        self.assertEqual(self.describe(kind="movie", status="no_tab_found", name="Peli"), "")

    def test_series_queued(self):
        txt = self.describe(status="queued", name="Serie", queued=[2, 3, 4], available=5, have=1, folder="Anime/S")
        self.assertEqual(txt, "📥 <b>Serie</b>: 3 capítulo(s) encolados (2-4) de 5 disponibles"
                              " — ya tenías 1.\nCarpeta: Anime/S")

    def test_series_queued_with_some_already_in_queue(self):
        txt = self.describe(status="queued", name="Serie", queued=[5], available=5, have=1, folder="Anime/S",
                            already_in_progress=[2, 3])
        self.assertTrue(txt.endswith("\nYa estaban en cola: 2-3"), txt)

    def test_series_complete(self):
        txt = self.describe(status="nothing_new", name="Serie", available=5, have=5, folder="Anime/S")
        self.assertEqual(txt, "✅ <b>Serie</b>: ya lo tienes completo (5 de 5 capítulos disponibles)."
                              "\nCarpeta: Anime/S")

    def test_series_error_is_silent(self):
        self.assertEqual(self.describe(status="page_fetch_failed", name="Serie"), "")


class EnqueueCase(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        from automator.pipeline.pending import PendingStore
        PendingStore()
        emision = mock.patch("automator.library.emision.EmisionLinker.ensure")
        emision.start()
        self.addCleanup(emision.stop)

    def request(self, title="Serie - Japan-Paw! Descarga Calidad", **kwargs):
        from automator.pipeline.enqueue import EnqueueRequest
        return EnqueueRequest(URL, title, **kwargs)

    def enqueuer(self):
        from automator.pipeline.enqueue import Enqueuer
        return Enqueuer(self.cfg)

    def queued(self):
        from automator.pipeline.work_queue import WorkQueue
        return [(i.episode, i.url) for i, _, _ in WorkQueue().view()[0]]


class RunLinksTest(EnqueueCase):
    """Enlaces ya sabidos (de la unidad de Drive), sin leer la ficha."""

    def test_nas_not_mounted(self):
        with mock.patch("automator.pipeline.enqueue.assert_nas_mounted", side_effect=RuntimeError("sin NAS")):
            result = self.enqueuer().run_links(self.request(), {1: "https://d/1.mkv"})
        self.assertEqual(result, {"status": "nas_not_mounted", "error": "sin NAS"})

    def test_no_links(self):
        self.assertEqual(self.enqueuer().run_links(self.request(), {}), {"status": "nothing_new"})

    def test_folder_resolution_failed(self):
        with mock.patch("automator.pipeline.enqueue.Library.resolve", side_effect=RuntimeError("raro")):
            result = self.enqueuer().run_links(self.request(), {1: "https://d/1.mkv"})
        self.assertEqual(result, {"status": "folder_resolution_failed", "error": "raro"})

    def test_nothing_missing_creates_nothing(self):
        from automator.pipeline.registry import InProgress
        folder = self.root / "Anime" / "Serie"
        folder.mkdir()
        (folder / "01.mkv").write_text("x")
        InProgress().add((str(folder), 2))
        result = self.enqueuer().run_links(self.request(), {1: "https://d/1.mkv", 2: "https://d/2.mkv"})
        self.assertEqual(result, {"status": "nothing_new"})
        self.assertEqual(self.queued(), [])

    def test_queues_what_is_missing(self):
        folder = self.root / "Anime" / "Serie"
        folder.mkdir()
        (folder / "01.mkv").write_text("x")
        result = self.enqueuer().run_links(self.request(), {1: "https://d/1.mkv", 2: "https://d/2.mkv"})
        self.assertEqual((result["status"], result["queued"], result["have"]), ("queued", [2], 1))
        self.assertEqual(self.queued(), [(2, "https://d/2.mkv")])


class SeriesTest(EnqueueCase):
    def series(self, req, links):
        return self.enqueuer()._series(req, "", req.watch_title, links)

    def test_folder_resolution_failure_is_reported(self):
        with mock.patch("automator.pipeline.enqueue.Library.resolve", side_effect=RuntimeError("raro")):
            result = self.series(self.request(), {1: "https://a/1.mkv"})
        self.assertEqual(result, {"status": "folder_resolution_failed"})
        self.assertEqual(len(self.sent), 1)
        self.assertIn("raro", self.sent[0])

    def test_nothing_new(self):
        folder = self.root / "Anime" / "Serie"
        folder.mkdir()
        (folder / "01.mkv").write_text("x")
        result = self.series(self.request(), {1: "https://a/1.mkv"})
        self.assertEqual((result["status"], result["available"], result["have"]), ("nothing_new", 1, 1))

    def test_upgrades_are_listed(self):
        folder = self.root / "Anime" / "Serie"
        folder.mkdir()
        (folder / "01.mp4").write_text("x")
        result = self.series(self.request(dry_run=True), {1: "https://a/1.mkv", 2: "https://a/2.mkv"})
        self.assertEqual((result["status"], result["missing"], result["upgrade"]), ("missing", [1, 2], [1]))

    def test_dry_run_all_in_progress(self):
        from automator.pipeline.registry import InProgress
        InProgress().add((str(self.root / "Anime" / "Serie"), 1))
        result = self.series(self.request(dry_run=True), {1: "https://a/1.mkv"})
        self.assertEqual((result["status"], result["missing"], result["already_in_progress"]),
                         ("in_progress", [], [1]))
        self.assertEqual(self.queued(), [])

    def test_tracked_request_is_not_in_the_batch_summary(self):
        import os
        from automator.pipeline.progress import BatchProgress
        os.environ["REQUESTER_CALLBACK_URL"] = "http://requester.invalid/events"
        result = self.series(self.request(job_id=5), {1: "https://a/1.mkv"})
        self.assertEqual((result["queued"], result["tracked"]), ([1], True))
        self.assertEqual(BatchProgress()._batch, {})

    def test_own_request_goes_to_the_batch_summary(self):
        from automator.pipeline.progress import BatchProgress
        result = self.series(self.request(), {1: "https://a/1.mkv", 2: "https://a/2.mkv"})
        self.assertEqual((result["queued"], result["tracked"]), ([1, 2], False))
        self.assertEqual(BatchProgress()._batch[result["folder"]]["pending"], {1, 2})


class MovieTest(EnqueueCase):
    TITLE = "Peli Movie - Japan-Paw! Descarga Calidad"

    def run_movie(self, t1, **kwargs):
        from automator.sources.japanpaw import JapanPawPage
        page = JapanPawPage(PAGE.format(title=self.TITLE, t1=t1, t2=""), URL)
        with mock.patch("automator.pipeline.enqueue.JapanPawPage.fetch", return_value=page):
            return self.enqueuer().run(self.request(self.TITLE, kind="movie", **kwargs))

    def test_without_link(self):
        result = self.run_movie("<p>nada</p>")
        self.assertEqual(result, {"status": "no_tab_found", "kind": "movie"})
        self.assertEqual(len(self.sent), 1)

    def test_queued_once(self):
        t1 = link(1, "https://a/Peli.mkv")
        first = self.run_movie(t1)
        self.assertEqual((first["status"], first["queued"], first["have"], first["upgrade"]),
                         ("queued", [1], 0, []))
        self.assertEqual(self.queued(), [(1, "https://a/Peli.mkv")])
        again = self.run_movie(t1)
        self.assertEqual((again["status"], again["queued"], again["already_in_progress"]), ("queued", [], [1]))

    def test_dry_run(self):
        t1 = link(1, "https://a/Peli.mkv")
        result = self.run_movie(t1, dry_run=True)
        self.assertEqual((result["status"], result["missing"]), ("missing", [1]))
        self.run_movie(t1)
        result = self.run_movie(t1, dry_run=True)
        self.assertEqual((result["status"], result["missing"]), ("in_progress", []))

    def test_already_have_it(self):
        t1 = link(1, "https://a/Peli.mkv")
        first = self.run_movie(t1, dry_run=True)
        dest = self.root / first["folder"] / first["file"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("x")
        result = self.run_movie(t1)
        self.assertEqual((result["status"], result["have"]), ("nothing_new", 1))
        self.assertEqual(self.queued(), [])


class RunErrorsTest(EnqueueCase):
    def run_page(self, title, t1="", **kwargs):
        from automator.pipeline.enqueue import EnqueueRequest
        from automator.sources.japanpaw import JapanPawPage
        page = JapanPawPage(PAGE.format(title=title, t1=t1, t2=""), URL)
        with mock.patch("automator.pipeline.enqueue.JapanPawPage.fetch", return_value=page):
            return self.enqueuer().run(EnqueueRequest(URL, **kwargs))

    def test_nas_not_mounted(self):
        with mock.patch("automator.pipeline.enqueue.assert_nas_mounted", side_effect=RuntimeError("sin NAS")):
            self.assertEqual(self.run_page("X"), {"status": "nas_not_mounted"})
        self.assertEqual(self.sent, ["🚨 sin NAS"])

    def test_page_without_title(self):
        self.assertEqual(self.run_page(""), {"status": "no_title"})
        self.assertIn("no tiene título", self.sent[0])

    def test_series_without_download_tab(self):
        self.assertEqual(self.run_page("Serie - Japan-Paw! Descarga Calidad", kind="series"),
                         {"status": "no_tab_found"})
        self.assertEqual(len(self.sent), 1)

    def test_dry_run_reports_nothing(self):
        self.assertEqual(self.run_page("", dry_run=True), {"status": "no_title"})
        self.assertEqual(self.sent, [])
