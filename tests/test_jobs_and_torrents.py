import json
import os
from unittest import mock

from tests.helpers import AutomatorTestCase


class JobTrackerTest(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        os.environ["REQUESTER_CALLBACK_URL"] = "http://requester.invalid/events"

    def test_lifecycle_survives_restart(self):
        from automator.core.singleton import Singleton
        from automator.pipeline.jobs import JobTracker
        t = JobTracker()
        job = t.new_job(42, "Prueba", "Prueba")
        k1, k2 = self.item("P", 1).key, self.item("P", 2).key
        t.attach(job, k1)
        t.attach(job, k2)
        t.seal(job)
        self.assertTrue(t.event(k1, "episode_done", source="japanpaw"))

        Singleton.reset(JobTracker)  # "reinicio"
        t2 = JobTracker()
        t2.load()
        self.assertEqual(len(t2.outbox), 1)  # el aviso que no llegó a entregarse
        self.assertTrue(t2.event(k2, "episode_done", source="japanpaw"))
        events = [(p["event"], p.get("episode"), p.get("done")) for p in t2.outbox._items]
        self.assertEqual(events, [("episode_done", 1, 1), ("episode_done", 2, 2), ("job_complete", None, [1, 2])])

    def test_no_tracking_without_callback_url(self):
        from automator.pipeline.jobs import JobTracker
        os.environ["REQUESTER_CALLBACK_URL"] = ""
        self.assertIsNone(JobTracker().new_job(1, "x", "x"))

    def test_reads_state_written_before_refactor(self):
        """El formato real que había en /data/tmp antes de la refactorización."""
        folder = str(self.root / "Anime" / "100-man")
        (self.root / "tmp" / "requester_jobs.json").write_text(json.dumps({
            "jobs": {"23": {"job_id": "23", "name": "100-man", "title": "100-man - Japan-Paw!", "kind": "series",
                            "pending": [1, 2], "done": [3, 4], "failed": [], "sealed": True, "finished": False}},
            "keys": [[[folder, 1], "23"], [[folder, 2], "23"]]}))
        from automator.pipeline.jobs import JobTracker
        t = JobTracker()
        t.load()
        t.event((folder, 1), "episode_done")
        t.event((folder, 2), "episode_failed", error=f"{self.root}/Anime/100-man/02.mkv no se pudo")
        last = t.outbox._items[-1]
        self.assertEqual((last["event"], last["job_id"], last["done"], last["failed"]),
                         ("job_complete", "23", [1, 3, 4], [2]))
        # la ruta del error va como la ve el usuario
        self.assertIn("Anime/100-man/02.mkv", t.outbox._items[-2]["error"])
        self.assertNotIn(str(self.root), t.outbox._items[-2]["error"])


class FakeTorrent:
    def __init__(self, percent_done=1.0, status="seeding"):
        self.percent_done, self.status = percent_done, status


class TorrentTrackerTest(AutomatorTestCase):
    def write_state(self, processed=False, added_at=None):
        import time
        folder = str(self.root / "Anime" / "Uno")
        (self.root / "tmp" / "tracked_torrents.json").write_text(json.dumps({
            "abc": {"anime_cfg": {"folder": folder, "name": "Uno"}, "watch_title": "Uno", "episode": 1,
                    "added_at": added_at or time.time(), "processed": processed, "nyaa_title": "[X] Uno - 01"}}))
        return folder

    def tracker(self, client):
        from automator.pipeline.torrents import TorrentTracker
        t = TorrentTracker()
        t.load()
        t._client = client
        return t

    def check(self, t, client):
        t._check(self.cfg, client, "abc", t.snapshot()[0][1])

    def test_loads_even_if_transmission_is_down(self):
        """Tras un apagón la NAS puede volver más tarde: no se pierde ningún torrent."""
        from automator.pipeline.registry import InProgress
        from automator.pipeline.torrents import TorrentTracker
        folder = self.write_state()
        t = TorrentTracker()
        with mock.patch("automator.pipeline.torrents.Transmission.client", side_effect=ConnectionError("caída")):
            t.load()
            self.assertIsNone(t.client(self.cfg))
        self.assertEqual(len(t.snapshot()), 1)
        self.assertIn((folder, 1), InProgress())
        client = mock.Mock()
        with mock.patch("automator.pipeline.torrents.Transmission.client", return_value=client):
            self.assertIs(t.client(self.cfg), client)  # se conecta en cuanto vuelve

    def test_restores_and_processes_completed(self):
        from automator.pipeline.registry import Completed, InProgress
        folder = self.write_state()
        client = mock.Mock()
        client.get_torrent.return_value = FakeTorrent()
        t = self.tracker(client)
        self.assertIn((folder, 1), InProgress())
        with mock.patch("automator.pipeline.torrents.EpisodeProcessor.process",
                        return_value=self.root / "Anime" / "Uno" / "01.mkv"):
            self.check(t, client)
        self.assertNotIn((folder, 1), InProgress())
        self.assertIn((folder, 1), Completed())
        self.assertTrue(json.loads((self.root / "tmp" / "tracked_torrents.json").read_text())["abc"]["processed"])

    def test_removed_when_seeding_stopped(self):
        self.write_state(processed=True)
        client = mock.Mock()
        client.get_torrent.return_value = FakeTorrent(status="stopped")
        t = self.tracker(client)
        self.check(t, client)
        client.remove_torrent.assert_called_once_with(ids=["abc"], delete_data=True)
        self.assertEqual(t.snapshot(), [])

    def test_forgets_torrent_deleted_by_hand(self):
        from automator.pipeline.registry import InProgress
        folder = self.write_state()
        client = mock.Mock()
        client.get_torrent.side_effect = KeyError("no está")
        t = self.tracker(client)
        self.check(t, client)
        self.assertEqual(t.snapshot(), [])
        self.assertNotIn((folder, 1), InProgress())

    def test_timeout_gives_up(self):
        self.write_state(added_at=1)
        client = mock.Mock()
        client.get_torrent.return_value = FakeTorrent(percent_done=0.3)
        t = self.tracker(client)
        self.check(t, client)
        client.remove_torrent.assert_called_once()
        self.assertTrue(any("demasiado tiempo" in s for s in self.sent))
