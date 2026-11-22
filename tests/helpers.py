"""
Base común de las pruebas: una config.yaml temporal (biblioteca, tmp y emisión
en un directorio temporal), Telegram/requester-bot sin configurar y todos los
singletons limpios en cada prueba. Nada toca la red ni la NAS real.

Ejecutar (dentro de la imagen, que tiene las dependencias):
    docker run --rm -v "$PWD:/app" -w /app <imagen> python -m unittest discover -s tests -v
"""

import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

CONFIG_TEMPLATE = """
tmp_dir: "{root}/tmp"
anime_root: "{root}/Anime"
emision_root: "{root}/Emision"
movies_dirname: "PELICULAS"
nas_sentinel_file: ""
site_title_suffix: "- Japan-Paw! Descarga Calidad"
preferred_group: "Erai-raws"
sockpuppet_browser_url: "ws://nadie:3000"
spain_keywords: ["españa", "espana", "castellano", "es-es", "spain", "spa"]
latino_keywords: ["latino", "latin", "es-419", "hispanoamérica", "hispanoamerica"]
telegram:
  bot_token: ""
  chat_id: ""
display_paths:
  - ["{root}/Anime/", "Anime/"]
download_retry:
  initial_delay_minutes: 5
  backoff_multiplier: 2
  max_delay_hours: 6
  jitter: 0
nyaa_fallback:
  enabled: true
  base_url: "https://nyaa.invalid"
  notify_on_use: true
transmission:
  host: "127.0.0.1"
  port: 9
animes:
  - watch_title: "Override Title - Japan-Paw! Descarga Calidad"
    folder: "{root}/Anime/Fijada/Temporada 2"
"""


class AutomatorTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="automator-test-"))
        for d in ("tmp", "Anime", "Emision"):
            (self.root / d).mkdir()
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(textwrap.dedent(CONFIG_TEMPLATE.format(root=self.root)))
        self._env = mock.patch.dict(os.environ, {
            "CONFIG_PATH": str(self.config_path),
            "REQUESTER_CALLBACK_URL": "", "REQUESTER_CALLBACK_SECRET": "",
            "ADMIN_BOT_TOKEN": "", "ADMIN_BOT_CHAT_IDS": "",
        })
        self._env.start()
        from automator.core.singleton import Singleton
        Singleton.reset()
        # sin red: AniList "no sabe nada" y Telegram no envía nada
        self.sent = []
        self._patches = [
            mock.patch("automator.library.anilist.AniListClient.query", return_value=None),
            mock.patch("automator.notify.telegram.Notifier.send",
                       side_effect=lambda text, cfg=None: self.sent.append(text)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._env.stop()
        from automator.core.singleton import Singleton
        Singleton.reset()
        shutil.rmtree(self.root, ignore_errors=True)

    @property
    def cfg(self):
        from automator.core.config import load_config
        return load_config()

    def item(self, folder_name="Uno", ep=1, url="https://cdn.example/x.mkv", **target):
        from automator.pipeline.model import WorkItem
        return WorkItem({"folder": str(self.root / "Anime" / folder_name), "name": folder_name, **target},
                        f"{folder_name} - Japan-Paw! Descarga Calidad", ep, url)
