"""Otro enlace de la ficha cuando el que se usaba falla (la página se falsea en el navegador)."""

from unittest import mock

from tests.helpers import AutomatorTestCase

PAGE = """
<html><head><title>Serie - Japan-Paw! Descarga Calidad</title></head><body>
<ul>
  <li tab-target="#t1"><span>[OtroGrupo][Web 720p]</span></li>
  <li tab-target="#t2"><span>[Erai-raws][Web 1080p]</span></li>
</ul>
<div id="t1">
  <a href="https://cdn.example/720/01.mkv">Capítulo 1</a>
  <a href="https://cdn.example/720/02.mkv">Capítulo 2</a>
</div>
<div id="t2">
  <a href="https://cdn.example/1080/01.mkv">Capítulo 1</a>
  <a href="https://cdn.example/1080/Peli Multi-Subs.mkv">La película</a>
</div>
</body></html>
"""


class AlternativeLinkTest(AutomatorTestCase):
    def alternative(self, target, episode, tried, html=PAGE):
        from automator.sources.japanpaw import alternative_link
        with mock.patch("automator.sources.japanpaw.fetch_rendered_html", return_value=html) as fetch:
            return alternative_link(self.cfg, target, episode, tried), fetch

    def test_best_link_not_tried_yet(self):
        target = {"page_url": "https://paste.japan-paw.net/?v=1"}
        link, fetch = self.alternative(target, 1, {"https://cdn.example/1080/01.mkv"})
        self.assertEqual(link, "https://cdn.example/720/01.mkv")
        fetch.assert_called_once_with("https://paste.japan-paw.net/?v=1", "ws://nadie:3000")
        self.assertEqual(self.alternative(target, 1, set())[0], "https://cdn.example/1080/01.mkv")

    def test_none_left(self):
        target = {"page_url": "https://paste.japan-paw.net/?v=1"}
        self.assertIsNone(self.alternative(target, 2, {"https://cdn.example/720/02.mkv"})[0])
        self.assertIsNone(self.alternative(target, 7, set())[0])

    def test_movie_links(self):
        target = {"page_url": "https://paste.japan-paw.net/?v=1", "movie": True}
        link, _ = self.alternative(target, 1, set())
        self.assertEqual(link, "https://cdn.example/1080/Peli Multi-Subs.mkv")

    def test_without_page_it_does_not_look(self):
        link, fetch = self.alternative({}, 1, set())
        self.assertIsNone(link)
        fetch.assert_not_called()

    def test_page_that_cannot_be_read(self):
        from automator.sources.japanpaw import alternative_link
        with mock.patch("automator.sources.japanpaw.fetch_rendered_html", side_effect=RuntimeError("Cloudflare")):
            self.assertIsNone(alternative_link(self.cfg, {"page_url": "https://paste.japan-paw.net/?v=1"}, 1, set()))
