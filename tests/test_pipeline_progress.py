"""El resumen de progreso que va al final de los avisos."""

from tests.helpers import AutomatorTestCase


class BatchProgressTest(AutomatorTestCase):
    def progress(self):
        from automator.pipeline.progress import BatchProgress
        return BatchProgress()

    def test_single_episode_has_no_summary_but_is_retired(self):
        p = self.progress()
        p.queued("/a", "Uno", [1])
        p.done("/a", 1)
        self.assertEqual(p.block(), "")
        self.assertEqual(p._batch, {})

    def test_nothing_queued(self):
        self.assertEqual(self.progress().block(), "")

    def test_finished_and_pending_sorted_by_name(self):
        p = self.progress()
        p.queued("/b", "beta", [1, 2, 3])
        p.queued("/a", "Alfa", [1, 2])
        p.queued("/c", "Gamma", [1, 2])
        p.done("/a", 1)
        p.failed("/a", 2)
        p.done("/b", 1)
        p.failed("/b", 2)
        p.done("/c", 1)
        p.cancelled("/c", 2)
        self.assertEqual(p.block(), "\n\n📊 Progreso de la cola:\n"
                                    "✔️ Terminado:\n"
                                    "• Alfa: completado 1/2 (1 fallidos)\n"
                                    "• Gamma: completado 1/1\n"
                                    "\n"
                                    "⏳ Pendiente:\n"
                                    "• beta: 1/3 hechos, 1 fallidos, 1 pendientes")
        # los terminados se retiran; lo pendiente sigue
        self.assertEqual(sorted(p._batch), ["/b"])
        self.assertEqual(p.block(), "\n\n📊 Progreso de la cola:\n"
                                    "⏳ Pendiente:\n"
                                    "• beta: 1/3 hechos, 1 fallidos, 1 pendientes")

    def test_only_pending_without_failures(self):
        p = self.progress()
        p.queued("/a", "Uno", [1, 2])
        p.done("/a", 1)
        self.assertEqual(p.block(), "\n\n📊 Progreso de la cola:\n⏳ Pendiente:\n• Uno: 1/2 hechos, 1 pendientes")

    def test_only_finished(self):
        p = self.progress()
        p.queued("/a", "Uno", [1, 2])
        p.done("/a", 1)
        p.done("/a", 2)
        self.assertEqual(p.block(), "\n\n📊 Progreso de la cola:\n✔️ Terminado:\n• Uno: completado 2/2")
        self.assertEqual(p._batch, {})

    def test_unknown_folder_is_ignored(self):
        p = self.progress()
        p.done("/nada", 1)
        self.assertEqual(p._batch, {})
