"""Arranque: recuperar el estado en orden y arrancar cada servicio una sola vez por proceso."""

import importlib
from unittest import mock

from tests.helpers import AutomatorTestCase


class BootstrapTest(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        from automator import bootstrap
        self.bootstrap = bootstrap
        bootstrap._started = False
        self.addCleanup(setattr, bootstrap, "_started", False)

    def test_recovers_then_starts_services_once(self):
        steps = []

        def step(name, result=None):
            return mock.Mock(side_effect=lambda *a, **k: steps.append(name) or result)

        def service(name):
            inst = mock.Mock()
            inst.start.side_effect = lambda: steps.append(f"{name}.start")
            inst.load.side_effect = lambda: steps.append(f"{name}.load")
            inst.load_cache.side_effect = lambda: steps.append(f"{name}.load_cache")
            return mock.Mock(side_effect=lambda: steps.append(name) or inst)

        names = {
            "automator.pipeline.pending.PendingStore": step("PendingStore"),
            "automator.pipeline.recovery.cleanup_stale_tmp": step("cleanup_stale_tmp"),
            "automator.pipeline.recovery.restore_pending_work": step("restore_pending_work"),
            **{f"automator.{path}": service(path.rsplit(".", 1)[1]) for path in (
                "library.anilist.AniListClient", "pipeline.jobs.JobTracker", "pipeline.torrents.TorrentTracker",
                "pipeline.retries.RetryScheduler", "pipeline.jobs.CallbackSender", "pipeline.worker.Worker",
                "pipeline.worker.Placer", "admin_bot.bot.AdminBot", "pipeline.watches.DriveWatcher")},
        }
        # todos los módulos importados antes de falsear nada: si no, uno que se importe
        # a mitad (p.ej. failures) se quedaría para siempre con la clase falsa
        for target in names:
            importlib.import_module(target.rsplit(".", 1)[0])
        patches = [mock.patch(target, new) for target, new in names.items()]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        self.bootstrap.start()
        self.bootstrap.start()   # gunicorn puede importarlo más de una vez: no repite nada

        starts = [s for s in steps if s.endswith(".start")]
        self.assertEqual(steps[:steps.index("restore_pending_work") + 1], [
            "PendingStore", "cleanup_stale_tmp", "AniListClient", "AniListClient.load_cache",
            "JobTracker", "JobTracker.load", "TorrentTracker", "TorrentTracker.load", "restore_pending_work"])
        self.assertEqual(starts, [f"{n}.start" for n in ("RetryScheduler", "CallbackSender", "Worker", "Placer",
                                                         "TorrentTracker", "AdminBot", "DriveWatcher")])
