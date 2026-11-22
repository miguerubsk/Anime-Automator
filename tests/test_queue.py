import json
import time

from tests.helpers import AutomatorTestCase


class WorkQueueTest(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        from automator.pipeline.pending import PendingStore
        from automator.pipeline.work_queue import WorkQueue
        PendingStore()  # se engancha a la cola
        self.q = WorkQueue()

    def order(self):
        return [(i.name, i.episode) for i, _, _ in self.q.view()[0]]

    def test_priority_then_manual_reorder(self):
        for ep in (1, 2, 3):
            self.q.put(self.item("Uno", ep), priority=1)
        for ep in (1, 2):
            self.q.put(self.item("Dos", ep), priority=1)
        self.q.put(self.item("Dos", 9), priority=0)  # urgente: delante
        self.assertEqual(self.order()[0], ("Dos", 9))
        self.q.move_group(self.item("Dos").group, "front")
        self.assertEqual(self.order(), [("Dos", 9), ("Dos", 1), ("Dos", 2), ("Uno", 1), ("Uno", 2), ("Uno", 3)])
        self.q.move(self.item("Uno", 3).key, "up")
        self.q.move(self.item("Uno", 1).key, "front")
        self.assertEqual(self.order(), [("Uno", 1), ("Dos", 9), ("Dos", 1), ("Dos", 2), ("Uno", 3), ("Uno", 2)])

    def test_get_skips_paused(self):
        self.q.put(self.item("Uno", 1))
        self.q.put(self.item("Dos", 1))
        self.q.put(self.item("Dos", 2))
        self.q.set_group_paused(self.item("Uno").group, True)
        self.q.set_paused(self.item("Dos", 1).key, True)
        got = self.q.get()
        self.assertEqual((got.name, got.episode), ("Dos", 2))

    def test_saved_state_has_order_and_pauses(self):
        from automator.pipeline.model import key_id
        self.q.put(self.item("Uno", 1))
        self.q.put(self.item("Uno", 2))
        self.q.set_paused(self.item("Uno", 2).key, True)
        data = json.loads((self.root / "tmp" / "pending_work.json").read_text())
        self.assertEqual(data["order"], [key_id(self.item("Uno", 1).key), key_id(self.item("Uno", 2).key)])
        self.assertEqual(data["paused"], [key_id(self.item("Uno", 2).key)])
        self.assertEqual(set(data["items"]), set(data["order"]))


class RecoveryTest(AutomatorTestCase):
    """El estado que deja una versión se recupera al arrancar (incluido el formato de antes de refactorizar)."""

    def _write(self, data):
        (self.root / "tmp" / "pending_work.json").write_text(json.dumps(data))

    def _record(self, folder, ep, retry_at=None, priority=0):
        return {"anime_cfg": {"folder": str(self.root / "Anime" / folder), "name": folder},
                "watch_title": folder, "episode": ep, "url": "u", "attempt": 2, "priority": priority,
                "retry_at": retry_at}

    def _restore(self):
        from automator.pipeline.recovery import restore_pending_work
        from automator.pipeline.registry import InProgress
        from automator.pipeline.retries import RetryScheduler
        from automator.pipeline.work_queue import WorkQueue
        restore_pending_work()
        return WorkQueue(), RetryScheduler(), InProgress()

    def test_restores_order_pauses_retries_and_current(self):
        from automator.pipeline.model import key_id
        recs = {key_id((str(self.root / "Anime" / f), ep)): self._record(f, ep)
                for f, ep in (("Uno", 1), ("Uno", 2), ("Dos", 1))}
        current = self._record("Dos", 5)         # se estaba procesando: no está en el orden
        retry = self._record("Uno", 7, retry_at=time.time() + 3600)
        recs[key_id((str(self.root / "Anime" / "Dos"), 5))] = current
        recs[key_id((str(self.root / "Anime" / "Uno"), 7))] = retry
        order = [key_id((str(self.root / "Anime" / f), ep)) for f, ep in (("Dos", 1), ("Uno", 2), ("Uno", 1))]
        self._write({"order": order, "paused": [order[1]], "paused_groups": [str(self.root / "Anime" / "Uno")],
                     "all_paused": False, "items": recs})

        q, retries, in_progress = self._restore()
        items, paused_groups, _, _ = q.view()
        self.assertEqual([(i.name, i.episode, p) for i, _, p in items],
                         [("Dos", 5, False), ("Dos", 1, False), ("Uno", 2, True), ("Uno", 1, False)])
        self.assertEqual(paused_groups, {str(self.root / "Anime" / "Uno")})
        self.assertEqual([(e.item.name, e.item.episode, e.item.attempt) for e in retries.entries()], [("Uno", 7, 2)])
        self.assertEqual(len(in_progress.snapshot()), 5)
        self.assertTrue(any("se recuperan 4" in s for s in self.sent))

    def test_restores_legacy_format(self):
        from automator.pipeline.model import key_id
        legacy = {key_id((str(self.root / "Anime" / "Uno"), 3)): self._record("Uno", 3)}
        self._write(legacy)  # solo el dict de capítulos, sin "items"
        q, _, _ = self._restore()
        self.assertEqual([(i.name, i.episode) for i, _, _ in q.view()[0]], [("Uno", 3)])


class ManagementTest(AutomatorTestCase):
    def test_cancel_removes_from_queue_retries_and_requester_job(self):
        from automator.pipeline.jobs import JobTracker
        from automator.pipeline.management import cancel_episodes
        from automator.pipeline.pending import PendingStore
        from automator.pipeline.registry import InProgress
        from automator.pipeline.retries import RetryScheduler
        from automator.pipeline.work_queue import WorkQueue
        import os
        os.environ["REQUESTER_CALLBACK_URL"] = "http://requester.invalid/events"
        PendingStore()
        tracker = JobTracker()
        job = tracker.new_job(7, "Uno", "Uno")
        for ep in (1, 2):
            InProgress().add(self.item("Uno", ep).key)
            tracker.attach(job, self.item("Uno", ep).key)
        WorkQueue().put(self.item("Uno", 1))
        RetryScheduler().schedule(self.item("Uno", 2), 1, 2)
        tracker.seal(job)

        self.assertEqual(cancel_episodes([self.item("Uno", 1).key, self.item("Uno", 2).key]), 2)
        self.assertEqual(WorkQueue().view()[0], [])
        self.assertEqual(RetryScheduler().entries(), [])
        self.assertEqual(InProgress().snapshot(), set())
        events = [(p["event"], p.get("episode")) for p in tracker.outbox._items]
        self.assertEqual(events[-1], ("job_complete", None))
        self.assertIn(("episode_failed", 1), events)
