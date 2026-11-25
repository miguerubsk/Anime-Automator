"""Entrega de avisos a requester-bot y recuperación del estado de las peticiones."""

import json
import os
import time
from unittest import mock

from tests.helpers import AutomatorTestCase


class CallbackSenderTest(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        os.environ["REQUESTER_CALLBACK_URL"] = "http://requester.invalid/events"
        os.environ["REQUESTER_CALLBACK_SECRET"] = "s3"
        self.sleeps = []
        sleep = mock.patch("automator.pipeline.jobs.time.sleep", side_effect=self.sleeps.append)
        sleep.start()
        self.addCleanup(sleep.stop)

    def outbox(self, *payloads):
        from automator.pipeline.jobs import JobTracker
        for p in payloads:
            JobTracker().outbox.append(p)
        return JobTracker().outbox

    def step(self, **post):
        from automator.pipeline.jobs import CallbackSender
        with mock.patch("automator.pipeline.jobs.requests.post", **post) as p:
            CallbackSender().step()
        return p

    def test_delivered_event_leaves_the_outbox(self):
        box = self.outbox({"job_id": "1", "event": "episode_done", "episode": 3, "_queued_at": time.time()})
        post = self.step(return_value=mock.Mock(ok=True))
        post.assert_called_once_with("http://requester.invalid/events",
                                     json={"job_id": "1", "event": "episode_done", "episode": 3}, timeout=15,
                                     headers={"X-Callback-Secret": "s3"})
        self.assertEqual(len(box), 0)
        self.assertEqual(self.sleeps, [])

    def test_failed_delivery_retries_the_same_event_waiting_longer(self):
        from automator.pipeline.jobs import CallbackSender
        box = self.outbox({"job_id": "1", "event": "episode_done", "_queued_at": time.time()},
                          {"job_id": "1", "event": "job_complete", "_queued_at": time.time()})
        self.step(return_value=mock.Mock(ok=False, status_code=502))
        self.step(side_effect=ConnectionError("caído"))
        self.assertEqual(self.sleeps, [5, 10])
        self.assertEqual(len(box), 2)
        post = self.step(return_value=mock.Mock(ok=True))
        self.assertEqual(post.call_args.kwargs["json"]["event"], "episode_done")
        self.assertEqual(len(box), 1)
        self.assertEqual(CallbackSender()._attempt, 0)

    def test_wait_is_capped_at_five_minutes(self):
        from automator.pipeline.jobs import CallbackSender
        self.outbox({"job_id": "1", "event": "episode_done", "_queued_at": time.time()})
        CallbackSender()._attempt = 10
        self.step(side_effect=ConnectionError("caído"))
        self.assertEqual(self.sleeps, [300])

    def test_too_old_failure_is_dropped_and_told_on_own_telegram(self):
        box = self.outbox({"job_id": "1", "event": "episode_failed", "name": "Serie", "episode": 2,
                           "error": "404", "_queued_at": 1})
        self.step(side_effect=ConnectionError("caído"))
        self.assertEqual(len(box), 0)
        self.assertEqual(self.sent, ["⚠️ requester-bot no responde. Evento perdido de <b>Serie</b>: "
                                     "episode_failed 2 404"])

    def test_too_old_progress_or_watched_events_are_dropped_silently(self):
        box = self.outbox({"job_id": "1", "event": "episode_done", "name": "S", "_queued_at": 1},
                          {"job_id": "w", "event": "episode_failed", "name": "S", "watch_url": "u", "_queued_at": 1})
        self.step(side_effect=ConnectionError("caído"))
        self.step(side_effect=ConnectionError("caído"))
        self.assertEqual(len(box), 0)
        self.assertEqual(self.sent, [])


class JobTrackerLoadTest(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        os.environ["REQUESTER_CALLBACK_URL"] = "http://requester.invalid/events"

    def test_nothing_saved(self):
        from automator.pipeline.jobs import JobTracker
        t = JobTracker()
        t.load()
        self.assertEqual((t._by_key, len(t.outbox)), ({}, 0))

    def test_keys_of_unknown_jobs_are_ignored(self):
        from automator.pipeline.jobs import JobTracker
        (self.root / "tmp" / "requester_jobs.json").write_text(json.dumps({
            "jobs": {"7": {"job_id": "7", "name": "S", "title": "S", "pending": [1], "sealed": False}},
            "keys": [[["/a", 1], "7"], [["/a", 2], "8"]]}))
        t = JobTracker()
        t.load()
        self.assertEqual(list(t._by_key), [("/a", 1)])
        self.assertTrue(t._by_key[("/a", 1)].sealed)   # tras reiniciar no se le añade nada más

    def test_corrupt_jobs_do_not_stop_the_outbox(self):
        from automator.pipeline.jobs import JobTracker
        (self.root / "tmp" / "requester_jobs.json").write_text("{roto")
        (self.root / "tmp" / "requester_outbox.json").write_text(json.dumps([{"job_id": "1", "event": "x"}]))
        t = JobTracker()
        with self.assertLogs("anime-automator", "ERROR"):
            t.load()
        self.assertEqual((t._by_key, len(t.outbox)), ({}, 1))

    def test_corrupt_outbox_is_logged(self):
        from automator.pipeline.jobs import JobTracker
        (self.root / "tmp" / "requester_outbox.json").write_text("[roto")
        t = JobTracker()
        with self.assertLogs("anime-automator", "ERROR") as logs:
            t.load()
        self.assertIn("bandeja", logs.output[0])
        self.assertEqual(len(t.outbox), 0)
