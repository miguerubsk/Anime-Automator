from unittest import mock

from tests.helpers import AutomatorTestCase

PAGE = """<html><head><title>{title}</title></head><body>
<ul><li tab-target="#t1"><span>[Erai-raws][Web 1080p]</span></li>
    <li tab-target="#t2"><span>[Otro][Web 720p]</span></li></ul>
<div id="t1">{t1}</div><div id="t2">{t2}</div>
<p>Fecha de Estreno 2024-01-01</p></body></html>"""


def link(ep, url):
    return f'<a href="{url}">Capítulo <b>{ep:02d}</b></a>'


class EnqueueTest(AutomatorTestCase):
    def page(self, title, t1="", t2=""):
        from automator.sources.japanpaw import JapanPawPage
        return mock.patch("automator.pipeline.enqueue.JapanPawPage.fetch",
                          return_value=JapanPawPage(PAGE.format(title=title, t1=t1, t2=t2), "https://paste.japan-paw.net/?v=1"))

    def run_enqueue(self, **kwargs):
        from automator.pipeline.enqueue import Enqueuer, EnqueueRequest
        return Enqueuer(self.cfg).run(EnqueueRequest("https://paste.japan-paw.net/?v=1", **kwargs))

    def test_queues_missing_episodes_with_best_links(self):
        from automator.pipeline.pending import PendingStore
        from automator.pipeline.work_queue import WorkQueue
        PendingStore()
        (self.root / "Anime" / "Serie").mkdir()
        (self.root / "Anime" / "Serie" / "01.mkv").write_bytes(b"")
        t1 = link(1, "https://a/1080-1.mkv") + link(2, "https://a/1080-2.mkv")
        t2 = link(2, "https://b/720-2.mkv") + link(3, "https://b/720-3.mkv")
        with self.page("Serie - Japan-Paw! Descarga Calidad", t1, t2), \
                mock.patch("automator.library.emision.EmisionLinker.ensure"):
            result = self.run_enqueue(priority=1)
        self.assertEqual((result["status"], result["queued"], result["have"]), ("queued", [2, 3], 1))
        self.assertEqual([(i.episode, i.url) for i, _, _ in WorkQueue().view()[0]],
                         [(2, "https://a/1080-2.mkv"), (3, "https://b/720-3.mkv")])
        # una segunda petición no duplica nada
        with self.page("Serie - Japan-Paw! Descarga Calidad", t1, t2), \
                mock.patch("automator.library.emision.EmisionLinker.ensure"):
            again = self.run_enqueue()
        self.assertEqual((again["queued"], again["already_in_progress"]), ([], [2, 3]))

    def test_webhook_is_observed_for_requester_bot(self):
        import os
        from automator.pipeline.jobs import JobTracker
        from automator.pipeline.pending import PendingStore
        os.environ["REQUESTER_CALLBACK_URL"] = "http://requester.invalid/events"
        PendingStore()
        with self.page("Semanal - Japan-Paw! Descarga Calidad", link(1, "https://a/1.mkv")), \
                mock.patch("automator.library.emision.EmisionLinker.ensure"):
            result = self.run_enqueue(watch=True)
        self.assertEqual((result["queued"], result["tracked"]), ([1], False))
        job = JobTracker()._by_key[next(iter(JobTracker()._by_key))]
        self.assertEqual(job.watch_url, "https://paste.japan-paw.net/?v=1")

    def test_dry_run_creates_nothing(self):
        with self.page("Nueva - Japan-Paw! Descarga Calidad", link(1, "https://a/1.mkv")):
            result = self.run_enqueue(dry_run=True)
        self.assertEqual((result["status"], result["missing"], result["new_folder"]), ("missing", [1], True))
        self.assertFalse((self.root / "Anime" / "Nueva").exists())
        self.assertEqual(self.sent, [])

    def test_movie_detection_and_destination(self):
        from automator.pipeline.pending import PendingStore
        PendingStore()
        (self.root / "Anime" / "Overlord").mkdir()
        t1 = link(1, "https://a/Overlord%20Movie%201%20[Multi-Sub].mkv")
        with self.page("Overlord Movie 1: Fushisha no Ou - Japan-Paw! Descarga Calidad", t1):
            result = self.run_enqueue()
        self.assertEqual((result["kind"], result["file"], result["series_folder"]),
                         ("movie", "Película 01.mkv", True))

    def test_errors_go_to_notify_fn(self):
        errors = []
        with mock.patch("automator.pipeline.enqueue.JapanPawPage.fetch", side_effect=RuntimeError("Cloudflare")):
            result = self.run_enqueue(notify_fn=errors.append)
        self.assertEqual(result["status"], "page_fetch_failed")
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.sent, [])

    def test_override_folder(self):
        from automator.library.folders import Library
        with mock.patch("automator.library.emision.EmisionLinker.ensure") as ensure:
            folder, name, new = Library().resolve(self.cfg, "Override Title - Japan-Paw! Descarga Calidad")
        self.assertEqual((folder.name, name, new), ("Temporada 2", "Fijada", False))
        ensure.assert_called_once()
