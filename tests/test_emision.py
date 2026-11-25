import subprocess
from pathlib import Path
from unittest import mock

from tests.helpers import AutomatorTestCase


def ran(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class EmisionLinkerTest(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        from automator.library.emision import EmisionLinker
        EmisionLinker._known.clear()
        EmisionLinker._warned.clear()
        self.lcfg = {**self.cfg, "emision_root": str(self.root / "Emision"),
                    "nas_ssh": {"host": "nas", "user": "u", "emision_path": "/pool/Emision"}}
        (self.root / "Emision").mkdir(exist_ok=True)

    def ensure(self, run_result, name="Serie"):
        from automator.library.emision import EmisionLinker
        with mock.patch("automator.library.emision.NasShell.run", return_value=run_result) as run:
            EmisionLinker(self.lcfg).ensure(name, Path(self.root / "Anime" / name))
        return run

    def test_existing_on_nas_but_invisible_by_smb_is_left_alone(self):
        run = self.ensure(ran("EXISTS\n"))
        cmd = run.call_args.args[0]
        self.assertIn("ln -sT --", cmd)            # nunca dentro de una carpeta existente
        self.assertIn("[ -e Serie ] || [ -L Serie ]", cmd)
        self.assertEqual(self.sent, [])            # ni aviso de error ni de creado
        run2 = self.ensure(ran("EXISTS\n"))        # ya se sabe que existe: ni SSH
        run2.assert_not_called()

    def test_created_is_notified_once(self):
        self.ensure(ran("CREATED\n"))
        self.assertTrue(any("Enlace de emisión creado" in m for m in self.sent))

    def test_failure_is_notified_once(self):
        self.ensure(ran("", 1, "ln: permiso denegado"))
        self.ensure(ran("", 1, "ln: permiso denegado"))
        self.assertEqual(sum("No se pudo crear el enlace" in m for m in self.sent), 1)
