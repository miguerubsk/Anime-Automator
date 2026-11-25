"""Torrents: alta por el fallback y la vuelta del TorrentTracker."""

import json
import time
from unittest import mock

from tests.helpers import AutomatorTestCase
from tests.test_jobs_and_torrents import FakeTorrent


class NyaaFallbackTest(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        add = mock.patch("automator.pipeline.torrents.Transmission.add",
                         return_value=(None, mock.Mock(hashString="h1")))
        self.add = add.start()
        self.addCleanup(add.stop)

    def try_start(self, found, **kwargs):
        from automator.pipeline.torrents import NyaaFallback
        item = self.item("Uno", 3, url="https://cdn.example/[Erai-raws]%20Uno%20-%2003%20[1080p].mkv")
        with mock.patch("automator.pipeline.torrents.NyaaSearch.search", return_value=found) as search:
            ok = NyaaFallback(self.cfg).try_start(item, **kwargs)
        return ok, search

    def tracked(self):
        from automator.pipeline.torrents import TorrentTracker
        return [(h, t.item.episode, t.nyaa_title) for h, t in TorrentTracker().snapshot()]

    def test_nothing_found(self):
        ok, search = self.try_start((None, None))
        self.assertFalse(ok)
        self.assertEqual(search.call_args.args, (["Uno - 03"], 3, 1))
        self.add.assert_not_called()
        self.assertEqual((self.tracked(), self.sent), ([], []))

    def test_found_is_added_and_tracked(self):
        ok, search = self.try_start(("https://t.invalid/1.torrent", "[Erai-raws] Uno - 03"),
                                    failed_url="https://cdn.example/[Erai-raws]%20Uno%20-%2003%20[1080p].mkv",
                                    reason="HTTP 404")
        self.assertTrue(ok)
        self.assertEqual(search.call_args.args[0], ["Uno - 03", "Uno - 03"])
        self.add.assert_called_once_with(self.cfg, "https://t.invalid/1.torrent")
        self.assertEqual(self.tracked(), [("h1", 3, "[Erai-raws] Uno - 03")])
        self.assertEqual(len(self.sent), 1)
        self.assertIn(" Motivo: HTTP 404.", self.sent[0])
        self.assertIn("[Erai-raws] Uno - 03", self.sent[0])

    def test_without_reason(self):
        ok, _ = self.try_start(("https://t.invalid/1.torrent", "X"))
        self.assertTrue(ok)
        self.assertNotIn("Motivo", self.sent[0])


class TrackerStepTest(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        sleep = mock.patch("automator.pipeline.torrents.time.sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def track(self, processed=False, added_at=None):
        from automator.pipeline.registry import InProgress
        from automator.pipeline.torrents import TorrentTracker, TrackedTorrent
        item = self.item("Uno", 1)
        InProgress().add(item.key)
        TorrentTracker().track("abc", TrackedTorrent(item, "[X] Uno - 01", added_at or time.time(), processed))
        return item

    def step(self, client):
        from automator.pipeline.torrents import TorrentTracker
        with mock.patch("automator.pipeline.torrents.Transmission.client", return_value=client) as connect:
            TorrentTracker().step()
        self.sleep.assert_called_once_with(60)
        return connect

    def test_without_torrents_does_not_connect(self):
        connect = self.step(mock.Mock())
        connect.assert_not_called()

    def test_transmission_down_checks_nothing(self):
        from automator.pipeline.torrents import TorrentTracker
        self.track()
        with mock.patch("automator.pipeline.torrents.Transmission.client", side_effect=ConnectionError("caída")):
            TorrentTracker().step()
        self.assertEqual(len(TorrentTracker().snapshot()), 1)

    def test_still_downloading_is_left_alone(self):
        from automator.pipeline.registry import InProgress
        from automator.pipeline.torrents import TorrentTracker
        item = self.track()
        client = mock.Mock()
        client.get_torrent.return_value = FakeTorrent(percent_done=0.5)
        self.step(client)
        client.get_torrent.assert_called_once_with("abc")
        client.remove_torrent.assert_not_called()
        self.assertEqual(len(TorrentTracker().snapshot()), 1)
        self.assertIn(item.key, InProgress())

    def test_transmission_error_keeps_tracking(self):
        from automator.pipeline.torrents import TorrentTracker
        self.track()
        client = mock.Mock()
        client.get_torrent.side_effect = TimeoutError("lenta")
        with self.assertLogs("anime-automator", "ERROR"):
            self.step(client)
        self.assertEqual(len(TorrentTracker().snapshot()), 1)

    def test_processing_failure_gives_up(self):
        from automator.pipeline.registry import InProgress
        from automator.pipeline.torrents import TorrentTracker
        item = self.track()
        client = mock.Mock()
        client.get_torrent.return_value = FakeTorrent()
        with mock.patch("automator.pipeline.torrents.EpisodeProcessor.process", side_effect=RuntimeError("sin subs")):
            self.step(client)
        client.remove_torrent.assert_called_once_with(ids=["abc"], delete_data=True)
        self.assertEqual(TorrentTracker().snapshot(), [])
        self.assertNotIn(item.key, InProgress())
        self.assertEqual(len(self.sent), 1)
        self.assertIn("sin subs", self.sent[0])

    def test_processed_still_seeding_is_kept(self):
        from automator.pipeline.torrents import TorrentTracker
        self.track(processed=True)
        client = mock.Mock()
        client.get_torrent.return_value = FakeTorrent(status="seeding")
        self.step(client)
        client.remove_torrent.assert_not_called()
        self.assertEqual(len(TorrentTracker().snapshot()), 1)

    def test_deleted_by_hand_after_processing_keeps_completed_state(self):
        from automator.pipeline.registry import InProgress
        from automator.pipeline.torrents import TorrentTracker
        item = self.track(processed=True)
        client = mock.Mock()
        client.get_torrent.side_effect = KeyError("no está")
        self.step(client)
        self.assertEqual(TorrentTracker().snapshot(), [])
        self.assertIn(item.key, InProgress())   # no lo libera: ya no era cosa del torrent


class TrackerLoadTest(AutomatorTestCase):
    def test_corrupt_state_is_logged(self):
        from automator.pipeline.torrents import TorrentTracker
        (self.root / "tmp" / "tracked_torrents.json").write_text("{roto")
        with self.assertLogs("anime-automator", "ERROR"):
            TorrentTracker().load()
        self.assertEqual(TorrentTracker().snapshot(), [])

    def test_processed_ones_are_not_in_progress(self):
        from automator.pipeline.registry import InProgress
        from automator.pipeline.torrents import TorrentTracker
        (self.root / "tmp" / "tracked_torrents.json").write_text(json.dumps({
            "h": {"anime_cfg": {"folder": "/a", "name": "A"}, "watch_title": "A", "episode": 2, "processed": True}}))
        TorrentTracker().load()
        self.assertEqual(len(TorrentTracker().snapshot()), 1)
        self.assertNotIn(("/a", 2), InProgress())
