"""Arranque: restos temporales y cola guardada."""

import json
import time
from unittest import mock

from tests.helpers import AutomatorTestCase


class CleanupStaleTmpTest(AutomatorTestCase):
    def cleanup(self):
        from automator.pipeline.recovery import cleanup_stale_tmp
        cleanup_stale_tmp()

    def test_removes_only_leftover_downloads_and_remuxes(self):
        tmp = self.root / "tmp"
        for name in ("raw_Uno_1.mkv", "proc_Uno_abc_1.mkv", "pending_work.json", "otro.mkv"):
            (tmp / name).write_bytes(b"x" * 10)
        with self.assertLogs("anime-automator", "INFO") as logs:
            self.cleanup()
        self.assertEqual(sorted(p.name for p in tmp.iterdir()), ["otro.mkv", "pending_work.json"])
        self.assertIn("Limpiados restos temporales", logs.output[0])

    def test_nothing_to_clean_logs_nothing(self):
        with mock.patch("automator.pipeline.recovery.log") as log:
            self.cleanup()
        log.info.assert_not_called()

    def test_one_that_cannot_be_removed_does_not_stop_the_rest(self):
        tmp = self.root / "tmp"
        (tmp / "raw_raro.mkv").mkdir()          # no se puede borrar con unlink
        (tmp / "raw_Uno_1.mkv").write_bytes(b"x")
        self.cleanup()
        self.assertEqual(sorted(p.name for p in tmp.iterdir()), ["raw_raro.mkv"])

    def test_unreadable_tmp_is_logged(self):
        with mock.patch("automator.pipeline.recovery.tmp_dir", side_effect=OSError("sin volumen")), \
                self.assertLogs("anime-automator", "ERROR") as logs:
            self.cleanup()
        self.assertIn("No se pudo limpiar", logs.output[0])


class RestorePendingWorkTest(AutomatorTestCase):
    def restore(self):
        from automator.pipeline.recovery import restore_pending_work
        restore_pending_work()

    def test_nothing_saved_notifies_nothing(self):
        from automator.pipeline.work_queue import WorkQueue
        self.restore()
        (self.root / "tmp" / "pending_work.json").write_text(json.dumps({"items": {}, "order": []}))
        self.restore()
        self.assertEqual(self.sent, [])
        self.assertEqual(WorkQueue().view()[0], [])

    def test_corrupt_state_is_logged(self):
        (self.root / "tmp" / "pending_work.json").write_text("{roto")
        with self.assertLogs("anime-automator", "ERROR") as logs:
            self.restore()
        self.assertIn("No se pudo recuperar la cola", logs.output[0])

    def test_expired_retry_goes_back_to_the_front(self):
        from automator.pipeline.model import key_id
        from automator.pipeline.retries import RetryScheduler
        from automator.pipeline.work_queue import WorkQueue

        def rec(ep, retry_at=None):
            return {"anime_cfg": {"folder": "/a", "name": "A"}, "watch_title": "A", "episode": ep,
                    "url": "u", "attempt": 3, "priority": 1, "retry_at": retry_at}
        items = {key_id(("/a", 1)): rec(1), key_id(("/a", 2)): rec(2, retry_at=time.time() - 10)}
        (self.root / "tmp" / "pending_work.json").write_text(json.dumps(
            {"items": items, "order": [key_id(("/a", 1))], "all_paused": True}))
        self.restore()
        queued, _, all_paused, _ = WorkQueue().view()
        self.assertEqual([(i.episode, p) for i, p, _ in queued], [(2, 1), (1, 1)])
        self.assertTrue(all_paused)
        self.assertEqual(RetryScheduler().entries(), [])
        self.assertEqual(self.sent, ["♻️ Reinicio: se recuperan 2 capítulos pendientes"])
        # y vuelve a quedar guardado (por si se corta la luz otra vez)
        saved = json.loads((self.root / "tmp" / "pending_work.json").read_text())
        self.assertEqual((sorted(saved["items"]), saved["order"]),
                         (sorted(items), [key_id(("/a", 2)), key_id(("/a", 1))]))
