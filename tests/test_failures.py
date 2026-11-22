from unittest import mock

import requests

from tests.helpers import AutomatorTestCase


def http_error(status):
    resp = requests.Response()
    resp.status_code = status
    return requests.exceptions.HTTPError(response=resp)


class FailureChainTest(AutomatorTestCase):
    """Qué pasa con un capítulo según cómo falla."""

    def setUp(self):
        super().setUp()
        from automator.pipeline.pending import PendingStore
        from automator.pipeline.registry import InProgress
        PendingStore()
        self.alt = mock.patch("automator.pipeline.failures.alternative_link", return_value=None)
        self.nyaa = mock.patch("automator.pipeline.torrents.NyaaFallback.try_start", return_value=False)
        self.alt_mock, self.nyaa_mock = self.alt.start(), self.nyaa.start()
        self.item1 = self.item("Uno", 1, page_url="https://paste.japan-paw.net/?v=1")
        InProgress().add(self.item1.key)

    def tearDown(self):
        self.alt.stop()
        self.nyaa.stop()
        super().tearDown()

    def finish(self, exc, item=None):
        from automator.pipeline.worker import Finisher
        Finisher().finish(self.cfg, item or self.item1, exc=exc)

    def state(self):
        from automator.pipeline.registry import InProgress
        from automator.pipeline.retries import RetryScheduler
        from automator.pipeline.work_queue import WorkQueue
        return (self.item1.key in InProgress(),
                [(e.item.attempt, e.item.target.get("relinks")) for e in RetryScheduler().entries()],
                [(i.url, i.target.get("tried_urls")) for i, _, _ in WorkQueue().view()[0]])

    def test_connection_error_is_retried_later(self):
        self.finish(requests.exceptions.ConnectionError("caído"))
        in_progress, retries, queued = self.state()
        self.assertTrue(in_progress)
        self.assertEqual(retries, [(2, None)])
        self.nyaa_mock.assert_not_called()
        self.assertTrue(any("Error temporal" in s for s in self.sent))

    def test_every_fourth_connection_error_looks_for_another_link(self):
        self.alt_mock.return_value = "https://otro/x.mkv"
        self.finish(requests.exceptions.ConnectionError("caído"), self.item1.evolve(attempt=4))
        _, retries, queued = self.state()
        self.assertEqual(retries, [])
        self.assertEqual(queued, [("https://otro/x.mkv", ["https://cdn.example/x.mkv"])])

    def test_404_tries_alternative_link_first(self):
        self.alt_mock.return_value = "https://otro/x.mkv"
        self.finish(http_error(404))
        self.assertEqual(self.state()[2][0][0], "https://otro/x.mkv")
        self.nyaa_mock.assert_not_called()

    def test_404_without_alternative_goes_to_nyaa(self):
        self.nyaa_mock.return_value = True
        self.finish(http_error(404))
        in_progress, retries, queued = self.state()
        self.assertTrue(in_progress)              # lo sigue el TorrentTracker
        self.assertEqual((retries, queued), ([], []))

    def test_404_without_alternative_nor_torrent_rechecks_page_tomorrow(self):
        self.finish(http_error(404))
        self.assertEqual(self.state()[1], [(1, 1)])

    def test_content_error_is_not_replaced_by_nyaa(self):
        from automator.core.errors import SourceContentError
        self.finish(SourceContentError("sin subtítulos"))
        in_progress, retries, queued = self.state()
        self.assertEqual((in_progress, retries, queued), (False, [], []))
        self.nyaa_mock.assert_not_called()
        self.alt_mock.assert_not_called()
        self.assertTrue(any("❌ Error procesando" in s for s in self.sent))

    def test_success_releases_and_wakes_same_server_retries(self):
        from automator.pipeline.retries import RetryScheduler
        other = self.item("Uno", 2, url="https://cdn.example/y.mkv")
        RetryScheduler().schedule(other, 5, 2)
        from automator.pipeline.worker import Finisher
        Finisher().finish(self.cfg, self.item1, dest=self.root / "Anime" / "Uno" / "01.mkv")
        self.assertFalse(self.state()[0])
        self.assertLessEqual(RetryScheduler().entries()[0].retry_at, __import__("time").time())
        self.assertTrue(any(f"procesado y guardado en:\n{self.root}/Anime/Uno/01.mkv" in s for s in self.sent))

    def test_notifier_shows_user_paths(self):
        from automator.notify.telegram import Notifier
        self._patches[1].stop()  # el Notifier de verdad, pero sin red
        try:
            with mock.patch("automator.notify.telegram.TelegramApi.send_message", return_value={}) as send, \
                    mock.patch.dict(self.cfg["telegram"], {"bot_token": "t", "chat_id": "1"}):
                Notifier().send(f"guardado en {self.root}/Anime/Uno/01.mkv <code>{self.root}/Anime/x</code>")
            self.assertEqual(send.call_args[0][1],
                             f"guardado en Anime/Uno/01.mkv <code>{self.root}/Anime/x</code>")
        finally:
            self._patches[1].start()
