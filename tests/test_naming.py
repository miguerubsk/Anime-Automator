import unittest

from automator.library.naming import (derive_anime_name_and_season, folder_safe, name_key, prefer_macrons,
                                      shorten_folder_name, split_season)
from automator.sources.japanpaw import parse_tab_label, resolve_link
from automator.sources.nyaa import extract_query_from_failed_url, parse_nyaa_release

SUFFIX = "- Japan-Paw! Descarga Calidad"


class NamingTest(unittest.TestCase):
    def test_seasons_from_title(self):
        cases = {
            "Tensei Shitara Slime Datta Ken 4th Season - Japan-Paw! Descarga Calidad": ("Tensei Shitara Slime Datta Ken", 4),
            "Mushoku Tensei III: Isekai Ittara Honki Dasu - Japan-Paw! Descarga Calidad": ("Mushoku Tensei - Isekai Ittara Honki Dasu", 3),
            "Yomi no Tsugai - Japan-Paw! Descarga Calidad": ("Yomi no Tsugai", 1),
            "Synduality Noir Part 2 - Japan-Paw! Descarga Calidad": ("Synduality Noir", 2),
            "One Punch Man 3 - Japan-Paw! Descarga Calidad": ("One Punch Man", 3),
            "Mob Psycho 100 - Japan-Paw! Descarga Calidad": ("Mob Psycho 100", 1),
            "Berserk (S2) (2017)": ("Berserk (2017)", 2),
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(derive_anime_name_and_season(title, SUFFIX), expected)

    def test_split_season_reports_if_explicit(self):
        self.assertEqual(split_season("Darker than Black: Ryuusei no Gemini", ""),
                         ("Darker than Black: Ryuusei no Gemini", 1, False))
        self.assertTrue(split_season("Oshi no Ko 2nd Season", "")[2])

    def test_folder_safe(self):
        self.assertEqual(folder_safe("Ranma 1/2"), "Ranma 1-2")
        self.assertEqual(folder_safe("Re:Zero kara"), "Re -Zero kara")
        self.assertEqual(prefer_macrons("Maoyuu Maou Yuusha"), "Maoyū Maō Yūsha")
        self.assertEqual(prefer_macrons("Your House"), "Your House")
        self.assertTrue(len(shorten_folder_name("Nombre " + "larguísimo " * 30).encode()) <= 150)

    def test_name_key_equivalences(self):
        self.assertEqual(name_key("Dan Da Dan"), name_key("Dandadan"))
        self.assertEqual(name_key("Maoyū Maō Yūsha"), name_key("Maoyuu Maou Yuusha"))


class SourcesParsingTest(unittest.TestCase):
    def test_tab_labels(self):
        self.assertEqual(parse_tab_label("[ToonsHub][Web 1080p]"), ("ToonsHub", "web", 1080))
        self.assertEqual(parse_tab_label("[Japan Paw!] [BD 1080p]"), ("Japan Paw!", "bd", 1080))
        self.assertEqual(parse_tab_label("Web 1080p"), ("", "web", 1080))
        self.assertEqual(parse_tab_label("[Grupo Raro] [SD]"), ("Grupo Raro", "web", None))

    def test_resolve_link(self):
        self.assertEqual(resolve_link("https://redirect.japan-paw.net/x#https%3A%2F%2Fa.b%2Fc.mkv"), "https://a.b/c.mkv")
        self.assertEqual(resolve_link("https://ouo.io/qs/X?s=https://cdn/1.Numeros & Simbolos/a.mkv"),
                         "https://cdn/1.Numeros & Simbolos/a.mkv")

    def test_nyaa_release(self):
        self.assertEqual(parse_nyaa_release("[Erai-raws] Show S02E06 [1080p]"), (6, 2))
        self.assertEqual(parse_nyaa_release("[SubsPlease] Mato Seihei no Slave 2 - 01 (1080p)"), (1, 2))
        self.assertEqual(parse_nyaa_release("[X] Show - 10 [1080p 10bit]"), (10, 1))
        self.assertEqual(parse_nyaa_release("Sin episodio"), (None, 1))

    def test_query_from_failed_url(self):
        url = "https://cdn/x/%5BErai-raws%5D%20Show%20-%2007%20%5B1080p%5D%5BMultiSub%5D.mkv"
        self.assertEqual(extract_query_from_failed_url(url, 7), "Show - 07")
