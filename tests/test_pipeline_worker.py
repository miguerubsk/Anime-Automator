"""Los hilos de trabajo (una vuelta cada uno) y los reintentos diferidos."""

import time
from unittest import mock

from tests.helpers import AutomatorTestCase


class WorkerCase(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        from automator.pipeline.pending import PendingStore
        PendingStore()

    def in_progress(self, item):
        from automator.pipeline.registry import InProgress
        InProgress().add(item.key)
        return item

    def retries(self):
        from automator.pipeline.retries import RetryScheduler
        return [(e.item.episode, e.item.attempt, round(e.retry_at - time.time())) for e in RetryScheduler().entries()]


class WorkerStepTest(WorkerCase):
    def setUp(self):
        super().setUp()
        self.placed = []
        put = mock.patch("automator.pipeline.worker.Placer.put",
                         side_effect=lambda item, processed: self.placed.append((item, processed)))
        put.start()
        self.addCleanup(put.stop)

    def step(self, item, **prepare):
        from automator.pipeline.work_queue import WorkQueue
        from automator.pipeline.worker import Worker
        WorkQueue().put(item)
        with mock.patch("automator.pipeline.worker.EpisodeProcessor.prepare", **prepare) as p:
            Worker().step()
        self.assertIsNone(WorkQueue().view()[3])   # task_done
        return p

    def test_prepared_episode_goes_to_the_placer(self):
        item = self.in_progress(self.item("Uno", 1))
        prepare = self.step(item, return_value=self.root / "tmp" / "proc.mkv")
        self.assertEqual(prepare.call_args.args[1], item)
        self.assertEqual(self.placed, [(item, self.root / "tmp" / "proc.mkv")])

    def test_failure_goes_through_the_failure_chain(self):
        from automator.core.errors import SourceContentError
        from automator.pipeline.registry import InProgress
        item = self.in_progress(self.item("Uno", 1))
        self.step(item, side_effect=SourceContentError("sin subtítulos"))
        self.assertEqual(self.placed, [])
        self.assertNotIn(item.key, InProgress())
        self.assertEqual(len(self.sent), 1)
        self.assertIn("sin subtítulos", self.sent[0])

    def test_without_nas_retries_in_five_minutes_and_warns_once_an_hour(self):
        with mock.patch("automator.pipeline.worker.assert_nas_mounted", side_effect=RuntimeError("sin NAS")):
            prepare = self.step(self.in_progress(self.item("Uno", 1)))
            self.step(self.in_progress(self.item("Uno", 2)))
        prepare.assert_not_called()
        self.assertEqual(self.retries(), [(1, 1, 300), (2, 1, 300)])
        self.assertEqual(self.sent, ["🚨 sin NAS\nLos capítulos pendientes se reintentarán cada 5 min."])

    def test_relink_pending_reads_the_page_again(self):
        item = self.in_progress(self.item("Uno", 1, relink_pending=True, page_url="https://paste/1"))
        with mock.patch("automator.pipeline.worker.alternative_link", return_value="https://nuevo/1.mkv") as alt:
            prepare = self.step(item, return_value=self.root / "tmp" / "proc.mkv")
        alt.assert_called_once_with(self.cfg, item.target, 1, set())
        used = prepare.call_args.args[1]
        self.assertEqual((used.url, used.target["relink_pending"]), ("https://nuevo/1.mkv", False))

    def test_relink_without_new_link_tries_the_same(self):
        item = self.in_progress(self.item("Uno", 1, relink_pending=True))
        with mock.patch("automator.pipeline.worker.alternative_link", return_value=None):
            prepare = self.step(item, return_value=self.root / "tmp" / "proc.mkv")
        used = prepare.call_args.args[1]
        self.assertEqual((used.url, used.target["relink_pending"]), (item.url, False))


class PlacerStepTest(WorkerCase):
    def step(self, item, **place):
        from automator.pipeline.worker import Placer
        Placer().put(item, self.root / "tmp" / "proc.mkv")
        with mock.patch("automator.pipeline.worker.EpisodeProcessor.place", **place):
            Placer().step()
        self.assertEqual(Placer()._queue.unfinished_tasks, 0)

    def test_placed_episode_is_completed(self):
        from automator.pipeline.registry import Completed, InProgress
        item = self.in_progress(self.item("Uno", 1))
        self.step(item, return_value=self.root / "Anime" / "Uno" / "01.mkv")
        self.assertIn(item.key, Completed())
        self.assertNotIn(item.key, InProgress())
        self.assertEqual(len(self.sent), 1)
        self.assertIn("procesado y guardado", self.sent[0])

    def test_write_error_is_retried_later(self):
        from automator.core.errors import DestinationWriteError
        from automator.pipeline.registry import InProgress
        item = self.in_progress(self.item("Uno", 1))
        self.step(item, side_effect=DestinationWriteError("SMB caído"))
        self.assertIn(item.key, InProgress())
        self.assertEqual([(ep, attempt) for ep, attempt, _ in self.retries()], [(1, 2)])

    def test_any_other_error_is_ours_and_temporary(self):
        from automator.pipeline.registry import InProgress
        item = self.in_progress(self.item("Uno", 1))
        self.step(item, side_effect=RuntimeError("sin NAS"))
        self.assertIn(item.key, InProgress())
        self.assertEqual([(ep, attempt) for ep, attempt, _ in self.retries()], [(1, 2)])
        self.assertIn("No se pudo colocar en la NAS: sin NAS", self.sent[0])


class RetrySchedulerTest(WorkerCase):
    def scheduler(self):
        from automator.pipeline.retries import RetryScheduler
        return RetryScheduler()

    def test_step_requeues_only_what_is_due(self):
        from automator.pipeline.work_queue import WorkQueue
        r = self.scheduler()
        r.schedule(self.item("Uno", 1), 0, 2)
        r.schedule(self.item("Uno", 2), 1, 2)
        r._wakeup.set()   # no esperar el minuto
        r.step()
        self.assertEqual([i.episode for i, _, _ in WorkQueue().view()[0]], [1])
        self.assertEqual([e.item.episode for e in r.entries()], [2])
        self.assertFalse(r._wakeup.is_set())

    def test_step_keeps_the_priority(self):
        from automator.pipeline.pending import PendingStore
        from automator.pipeline.work_queue import WorkQueue
        r = self.scheduler()
        item = self.item("Uno", 1)
        PendingStore().upsert(item, priority=1)
        r.schedule(item, 0, 2)
        r._wakeup.set()
        r.step()
        self.assertEqual([(i.episode, i.attempt, p) for i, p, _ in WorkQueue().view()[0]], [(1, 2, 1)])

    def test_take(self):
        r = self.scheduler()
        item = self.item("Uno", 1)
        r.add(item, time.time() + 60)
        self.assertIsNone(r.take(("/otro", 1)))
        self.assertEqual(r.take(item.key), item)
        self.assertEqual(r.entries(), [])
        self.assertIsNone(r.take(item.key))
