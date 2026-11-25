"""Biblioteca: carpeta de cada anime, temporadas, películas y copias de menor calidad."""

import json
import subprocess
from pathlib import Path
from unittest import mock

from tests.helpers import AutomatorTestCase

SUFFIX = " - Japan-Paw! Descarga Calidad"


class LibraryTestCase(AutomatorTestCase):
    """Biblioteca vacía en self.anime; los enlaces de emisión (por SSH a la NAS) se apuntan en self.links."""

    def setUp(self):
        super().setUp()
        self.anime = self.root / "Anime"
        self.links = []
        self._ensure = mock.patch("automator.library.emision.EmisionLinker.ensure",
                                  side_effect=lambda name, folder: self.links.append((name, folder)))
        self._ensure.start()

    def tearDown(self):
        self._ensure.stop()
        super().tearDown()

    def mk(self, *names, files=()):
        """Crea anime/<names...> y dentro los archivos `files`."""
        d = self.anime.joinpath(*names)
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            (d / f).write_text("x")
        return d

    def library(self):
        from automator.library.folders import Library
        return Library()


class ResolveTest(LibraryTestCase):
    def resolve(self, title, **kw):
        return self.library().resolve(self.cfg, title + SUFFIX, **kw)

    def layout(self, base, season, subtitle="", full=None):
        from automator.library.folders import SeasonLayout
        return SeasonLayout(base, season, subtitle, full or base)

    def test_override_without_emision_link(self):
        folder, name, new = self.library().resolve(self.cfg, "Override Title" + SUFFIX, link_emision=False)
        self.assertEqual((folder, name, new), (self.anime / "Fijada" / "Temporada 2", "Fijada", False))
        self.assertEqual(self.links, [])

    def test_override_links_the_series_folder(self):
        self.library().resolve(self.cfg, "Override Title" + SUFFIX)
        self.assertEqual(self.links, [("Fijada", self.anime / "Fijada")])

    def test_new_series_is_created_in_the_root(self):
        folder, name, new = self.resolve("Nueva")
        self.assertEqual((folder, name, new), (self.anime / "Nueva", "Nueva", True))
        self.assertTrue(folder.is_dir())
        self.assertEqual(self.links, [("Nueva", folder)])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Carpeta nueva", self.sent[0])
        self.assertIn("<b>Nueva</b>", self.sent[0])

    def test_without_create_nothing_is_touched(self):
        folder, name, new = self.resolve("Nueva", create=False)
        self.assertEqual((folder, name, new), (self.anime / "Nueva", "Nueva", True))
        self.assertFalse(folder.exists())
        self.assertEqual((self.links, self.sent), ([], []))

    def test_without_emision_link(self):
        self.resolve("Nueva", link_emision=False)
        self.assertEqual(self.links, [])

    def test_existing_folder_with_an_equivalent_name(self):
        self.mk("Dan Da Dan")
        folder, name, new = self.resolve("Dandadan")
        self.assertEqual((folder, name, new), (self.anime / "Dan Da Dan", "Dan Da Dan", False))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.links, [("Dan Da Dan", self.anime / "Dan Da Dan")])

    def test_second_season_moves_the_first_one_to_its_folder(self):
        self.mk("Show", files=("01.mkv", "02.mkv"))
        folder, name, new = self.resolve("Show 2nd Season")
        self.assertEqual((folder, name, new), (self.anime / "Show" / "Temporada 2", "Show", False))
        self.assertTrue(folder.is_dir())
        self.assertEqual(sorted(p.name for p in (self.anime / "Show" / "Temporada 1").iterdir()),
                         ["01.mkv", "02.mkv"])
        self.assertTrue(any("Reorganizado" in s for s in self.sent))

    def test_second_season_without_create(self):
        self.mk("Show", files=("01.mkv",))
        folder, _, new = self.resolve("Show 2nd Season", create=False)
        self.assertEqual((folder, new), (self.anime / "Show" / "Temporada 2", False))
        self.assertFalse(folder.exists())
        self.assertTrue((self.anime / "Show" / "01.mkv").exists())

    def test_existing_season_folder_with_subtitle_is_reused(self):
        self.mk("Show", "Temporada 2 (Otra)")
        folder, _, _ = self.resolve("Show 2nd Season", create=False)
        self.assertEqual(folder, self.anime / "Show" / "Temporada 2 (Otra)")

    def test_first_season_when_there_are_season_folders(self):
        self.mk("Show", "Temporada 2")
        folder, _, _ = self.resolve("Show", create=False)
        self.assertEqual(folder, self.anime / "Show" / "Temporada 1")
        self.assertFalse(folder.exists())
        folder, _, _ = self.resolve("Show")
        self.assertEqual(folder, self.anime / "Show" / "Temporada 1")
        self.assertTrue(folder.is_dir())

    def test_first_season_of_an_existing_series_stays_in_the_root(self):
        self.mk("Show")
        self.assertEqual(self.resolve("Show", create=False)[0], self.anime / "Show")
        self.assertEqual(self.resolve("Show")[0], self.anime / "Show")

    def test_new_series_starting_at_season_2(self):
        folder, name, new = self.resolve("Nueva 2nd Season")
        self.assertEqual((folder, name, new), (self.anime / "Nueva" / "Temporada 2", "Nueva", True))
        self.assertTrue(folder.is_dir())

    def test_season_stored_the_old_way(self):
        legacy = self.mk("Darker than Black - Ryuusei no Gemini")
        layout = self.layout("Darker than Black", "2", "Ryuusei no Gemini", "Darker than Black - Ryuusei no Gemini")
        folder, name, new = self.resolve("x", layout=layout)
        self.assertEqual((folder, name, new), (legacy, legacy.name, False))
        self.assertEqual(self.links, [(legacy.name, legacy)])
        self.assertEqual(self.resolve("x", layout=layout, create=False), (legacy, legacy.name, False))
        self.assertEqual(len(self.links), 1)

    def test_season_with_subtitle(self):
        layout = self.layout("Darker than Black", "2", "Ryuusei no Gemini", "Darker than Black - Ryuusei no Gemini")
        folder, name, new = self.resolve("x", layout=layout)
        self.assertEqual(folder, self.anime / "Darker than Black" / "Temporada 2 (Ryuusei no Gemini)")
        self.assertEqual((name, new), ("Darker than Black", True))

    def test_new_first_season_with_subtitle_gets_its_folder(self):
        layout = self.layout("Darker than Black", "1", "Kuro no Keiyakusha", "Darker than Black - Kuro no Keiyakusha")
        expected = self.anime / "Darker than Black" / "Temporada 1 (Kuro no Keiyakusha)"
        self.assertEqual(self.resolve("x", layout=layout, create=False)[0], expected)
        self.assertEqual(self.resolve("x", layout=layout)[0], expected)

    def test_existing_first_season_with_subtitle_stays_in_the_root(self):
        self.mk("Darker than Black")
        layout = self.layout("Darker than Black", "1", "Kuro no Keiyakusha", "Darker than Black - Kuro no Keiyakusha")
        self.assertEqual(self.resolve("x", layout=layout, create=False)[0], self.anime / "Darker than Black")
        self.assertEqual(self.resolve("x", layout=layout)[0], self.anime / "Darker than Black")

    def test_a_mark_glued_to_the_name_is_not_an_old_folder(self):
        self.mk("K-On")
        folder, name, _ = self.resolve("x", layout=self.layout("K-On", "2", "!!", "K-On!!"))
        self.assertEqual((folder, name), (self.anime / "K-On" / "Temporada 2 (!!)", "K-On"))

    def test_year_of_each_season_goes_to_the_first_one(self):
        self.mk("Berserk (2016)")
        folder, name, _ = self.resolve("x", layout=self.layout("Berserk (2017)", "2"))
        self.assertEqual((folder, name), (self.anime / "Berserk (2016)" / "Temporada 2", "Berserk (2016)"))

    def test_half_season(self):
        self.mk("Show", files=("01.mkv",))
        folder, _, _ = self.resolve("x", layout=self.layout("Show", "1.5"))
        self.assertEqual(folder, self.anime / "Show" / "Temporada 1.5")
        self.assertTrue((self.anime / "Show" / "Temporada 1" / "01.mkv").exists())

    def test_resolve_anime_folder_shortcut(self):
        from automator.library.folders import resolve_anime_folder
        self.assertEqual(resolve_anime_folder(watch_title="Nueva" + SUFFIX, create=False)[1], "Nueva")


class SeasonLayoutTest(LibraryTestCase):
    def test_anilist_season_with_subtitle(self):
        from automator.library.folders import season_layout
        info = {"base": "Darker than Black", "season": "2", "subtitle": "Ryuusei no Gemini", "anilist_id": 1}
        page = "Títulos Alternativos DTB 2, Otro\nFecha de Estreno 2009-10-08"
        with mock.patch("automator.library.anilist.SeasonResolver.season_info", return_value=info) as si:
            layout = season_layout(self.cfg, "Darker than Black: Ryuusei no Gemini" + SUFFIX, page)
        si.assert_called_once_with("Darker than Black: Ryuusei no Gemini", "2009-10-08", ["DTB 2", "Otro"])
        self.assertEqual((layout.base, layout.season, layout.subtitle, layout.full),
                         ("Darker than Black", "2", "Ryūsei no Gemini", "Darker than Black - Ryūsei no Gemini"))

    def test_anilist_season_without_subtitle(self):
        from automator.library.folders import season_layout
        info = {"base": "Seitokai Yakuindomo", "season": "2", "subtitle": "", "anilist_id": 1}
        with mock.patch("automator.library.anilist.SeasonResolver.season_info", return_value=info):
            layout = season_layout(self.cfg, "Seitokai Yakuindomo*" + SUFFIX)
        self.assertEqual((layout.base, layout.season, layout.subtitle), ("Seitokai Yakuindomo", "2", ""))


class FolderLookupTest(LibraryTestCase):
    def test_find_folder(self):
        lib = self.library()
        self.mk("Dan Da Dan")
        self.mk("PELICULAS")
        (self.anime / "Archivo Suelto").write_text("x")
        self.assertEqual(lib.find_folder(self.cfg, "Dan Da Dan"), self.anime / "Dan Da Dan")
        self.assertEqual(lib.find_folder(self.cfg, "Dandadan"), self.anime / "Dan Da Dan")
        self.assertIsNone(lib.find_folder(self.cfg, "Peliculas"))       # la de películas no es una serie
        self.assertIsNone(lib.find_folder(self.cfg, "Archivo suelto"))  # ni un archivo
        self.assertIsNone(lib.find_folder(self.cfg, "!!!"))
        self.assertIsNone(lib.find_folder(self.cfg, "Otra"))

    def test_find_folder_without_library(self):
        self.anime.rmdir()
        self.assertIsNone(self.library().find_folder(self.cfg, "Dandadan"))

    def test_merge_with_earlier_year(self):
        merge = self.library().merge_with_earlier_year
        self.mk("Berserk (2014)")
        self.mk("berserk (2016)")
        self.mk("Berserk (2019)")
        (self.anime / "Berserk (2015)").write_text("x")
        self.assertEqual(merge(self.anime, "Berserk (2017)", 2), "berserk (2016)")
        self.assertEqual(merge(self.anime, "Berserk (2017)", 1), "Berserk (2017)")
        self.assertEqual(merge(self.anime, "Berserk (2013)", 2), "Berserk (2013)")
        self.assertEqual(merge(self.anime, "Berserk (2019)", 3), "Berserk (2019)")   # ya existe
        self.assertEqual(merge(self.anime, "Berserk", 2), "Berserk")
        self.assertEqual(merge(self.root / "no-existe", "Berserk (2017)", 2), "Berserk (2017)")

    def test_season_base(self):
        lib = self.library()
        self.assertEqual(lib.season_base(self.cfg, self.anime / "Show"), self.anime / "Show")
        self.assertEqual(lib.season_base(self.cfg, self.anime / "Show" / "Temporada 2"), self.anime / "Show")
        self.assertIsNone(lib.season_base(self.cfg, self.anime / "Show" / "Extras"))
        self.assertIsNone(lib.season_base(self.cfg, self.root / "Otro" / "Show"))


class PlacementTest(LibraryTestCase):
    def place(self, folder, **target):
        return self.library().placement_folder(self.cfg, {"folder": str(folder), **target})

    def test_only_season_that_now_has_company(self):
        show = self.mk("Show")
        self.mk("Show", "Temporada 2")
        self.assertEqual(self.place(show), (show / "Temporada 1", show, True))
        t1 = self.mk("Show", "Temporada 1 (Inicio)")
        self.assertEqual(self.place(show), (t1, show, True))

    def test_single_season_stays_in_the_root(self):
        show = self.mk("Show")
        self.assertEqual(self.place(show), (show, show, True))

    def test_movies_and_named_files_are_not_moved(self):
        show = self.mk("Show")
        self.mk("Show", "Temporada 2")
        self.assertEqual(self.place(show, movie=True), (show, show, False))
        self.assertEqual(self.place(show, filename="OVA.mkv"), (show, show, False))

    def test_season_folder_and_outside_folder(self):
        t2 = self.mk("Show", "Temporada 2")
        self.assertEqual(self.place(t2), (t2, self.anime / "Show", True))
        other = self.root / "Otro"
        self.assertEqual(self.place(other), (other, None, True))


class OrganizeSeasonsTest(LibraryTestCase):
    def organize(self, folder):
        return self.library().organize_seasons(self.cfg, folder)

    def test_single_season_is_left_alone(self):
        show = self.mk("Show", files=("01.mkv",))
        self.assertEqual(self.organize(show), 0)
        self.assertTrue((show / "01.mkv").exists())

    def test_nothing_loose(self):
        show = self.mk("Show", files=("OVA.mkv",))
        self.mk("Show", "Temporada 2")
        self.assertEqual(self.organize(show), 0)
        self.assertEqual(self.sent, [])

    def test_loose_episodes_go_to_season_1(self):
        show = self.mk("Show", files=("01.mkv", "02.mp4", "OVA.mkv"))
        self.mk("Show", "Temporada 2")
        t1 = self.mk("Show", "Temporada 1 (Inicio)")
        self.assertEqual(self.organize(show), 2)
        self.assertEqual(sorted(p.name for p in t1.iterdir()), ["01.mkv", "02.mp4"])
        self.assertTrue((show / "OVA.mkv").exists())
        self.assertEqual(len(self.sent), 1)
        self.assertIn("2 capítulo(s)", self.sent[0])
        self.assertIn("'Temporada 1 (Inicio)'", self.sent[0])

    def test_season_1_with_episodes_is_not_touched_and_warns_once(self):
        show = self.mk("Show", files=[f"{i:02d}.mkv" for i in range(13, 21)])
        self.mk("Show", "Temporada 1", files=("01.mkv",))
        self.assertEqual(self.organize(show), 0)
        self.assertEqual(self.organize(show), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("13.mkv, 14.mkv, 15.mkv, 16.mkv, 17.mkv, 18.mkv…", self.sent[0])
        self.assertIn("'Temporada 1' con capítulos", self.sent[0])
        self.assertTrue((show / "20.mkv").exists())

    def test_few_loose_episodes_are_listed_without_ellipsis(self):
        show = self.mk("Show", files=("13.mkv",))
        self.mk("Show", "Temporada 1", files=("01.mkv",))
        self.organize(show)
        self.assertIn("(13.mkv)", self.sent[0])

    def test_an_episode_already_in_season_1_is_not_overwritten(self):
        from automator.library.folders import move_loose_episodes
        show = self.mk("Show", files=("01.mkv", "02.mkv"))
        t1 = self.mk("Show", "Temporada 1", files=("01.mkv",))
        (t1 / "01.mkv").write_text("el de antes")
        moved, clashes = move_loose_episodes([show / "01.mkv", show / "02.mkv"], t1)
        self.assertEqual((moved, clashes), (1, ["01.mkv"]))
        self.assertEqual((t1 / "01.mkv").read_text(), "el de antes")
        self.assertTrue((show / "01.mkv").exists())
        self.assertTrue((t1 / "02.mkv").exists())


class FilesTest(LibraryTestCase):
    def test_loose_episode_files(self):
        from automator.library.files import loose_episode_files
        show = self.mk("Show", files=("02.mkv", "01.mp4", "OVA.mkv", "notas.txt"))
        self.mk("Show", "03.mkv")
        self.assertEqual(loose_episode_files(show), [show / "01.mp4", show / "02.mkv"])
        self.assertEqual(loose_episode_files(self.anime / "No existe"), [])

    def test_has_season_dirs(self):
        from automator.library.files import has_season_dirs
        show = self.mk("Show", files=("Temporada 1",))
        self.assertFalse(has_season_dirs(show))
        self.mk("Show", "temporada 2 (Final)")
        self.assertTrue(has_season_dirs(show))
        self.assertFalse(has_season_dirs(self.anime / "No existe"))

    def test_episode_files_of_a_missing_folder(self):
        from automator.library.files import episode_files, find_season_dir
        self.assertEqual(episode_files(self.anime / "No existe"), {})
        self.assertIsNone(find_season_dir(self.anime / "No existe", 1))


class MovieLibraryTest(LibraryTestCase):
    def movies(self):
        from automator.library.movies import MovieLibrary
        return MovieLibrary(self.cfg)

    def test_series_folder_by_the_start_of_the_title(self):
        self.mk("Overlord")
        self.mk("Overlord II")
        self.mk("PELICULAS")
        self.mk("K")
        (self.anime / "Archivo").write_text("x")
        m = self.movies()
        self.assertEqual(m.find_series_folder("Overlord II Movie: Sei Oukoku-hen"), self.anime / "Overlord II")
        self.assertEqual(m.find_series_folder("Overlord Movie 1"), self.anime / "Overlord")
        self.assertIsNone(m.find_series_folder("K Movie: Missing Kings"))     # demasiado corto
        self.assertIsNone(m.find_series_folder("Overlord"))                   # es el título entero
        self.assertIsNone(m.find_series_folder("Peliculas Movie"))
        self.assertIsNone(m.find_series_folder("Archivo Movie"))

    def test_series_folder_from_an_override(self):
        self.assertEqual(self.movies().find_series_folder("Override Title" + SUFFIX), self.anime / "Fijada")

    def test_without_library(self):
        self.anime.rmdir()
        self.assertIsNone(self.movies().find_series_folder("Overlord Movie 1"))
        self.assertIsNone(self.movies().folder_named("Overlord"))

    def test_folder_named(self):
        self.mk("Suzumiya Haruhi no Yūutsu")
        self.mk("PELICULAS")
        m = self.movies()
        self.assertEqual(m.folder_named("Suzumiya Haruhi no Yuuutsu"), self.anime / "Suzumiya Haruhi no Yūutsu")
        self.assertEqual(m.folder_named("Override Title" + SUFFIX), self.anime / "Fijada")
        self.assertIsNone(m.folder_named("Películas"))
        self.assertIsNone(m.folder_named("Otra Serie"))
        self.assertIsNone(m.folder_named("K"))

    def test_destination(self):
        self.mk("Overlord")
        self.mk("Suzumiya Haruhi no Yuuutsu")
        m = self.movies()
        self.assertEqual(m.destination("Overlord Movie 2"), (self.anime / "Overlord", "Película 02.mkv", True))
        self.assertEqual(m.destination("Suzumiya Haruhi no Shoushitsu", "Suzumiya Haruhi no Yuuutsu"),
                         (self.anime / "Suzumiya Haruhi no Yuuutsu",
                          "Película - Suzumiya Haruhi no Shōshitsu.mkv", True))
        self.assertEqual(m.destination("Kimi no Na wa."), (self.anime / "PELICULAS", "Kimi no Na wa.mkv", False))

    def test_movie_file_name(self):
        from automator.library.movies import movie_file_name
        self.assertEqual(movie_file_name("Overlord Movie 1: Fushisha no Ou"), "Película 01.mkv")
        self.assertEqual(movie_file_name("Chainsaw Man: Reze-hen"), "Película - Reze-hen.mkv")
        self.assertEqual(movie_file_name("Kimetsu no Yaiba - Mugen Ressha-hen"), "Película - Mugen Ressha-hen.mkv")
        self.assertEqual(movie_file_name("Overlord: The Movie"), "Película.mkv")
        self.assertEqual(movie_file_name("Overlord"), "Película.mkv")

    def test_movie_copies_of_a_missing_folder(self):
        from automator.library.movies import movie_copies
        self.assertEqual(movie_copies(self.anime / "No existe", "Película 01.mkv"), [])


class FakeMkvmerge:
    """`mkvmerge -J`: la altura de cada archivo sale de `heights` (sin entrada: no se puede leer)."""

    def __init__(self, heights):
        self.heights = heights

    def __call__(self, cmd, **kw):
        h = self.heights.get(Path(cmd[-1]).name)
        if h is None:
            raise subprocess.CalledProcessError(2, cmd)
        info = {"tracks": [{"type": "audio", "properties": {}},
                           {"type": "video", "properties": {"pixel_dimensions": f"{h * 16 // 9}x{h}"}}]}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(info), "")


class LowerQualityTest(LibraryTestCase):
    def remove(self, heights, dest, *others):
        from automator.library.quality import remove_lower_quality_copies
        with mock.patch("automator.media.mkv.subprocess.run", FakeMkvmerge(heights)):
            remove_lower_quality_copies(self.cfg, dest, list(others))

    def setUp(self):
        super().setUp()
        self.show = self.mk("Show", files=("01.mkv", "01.mp4", "01.avi", "01.wmv"))

    def test_lower_or_equal_copies_are_deleted(self):
        s = self.show
        self.remove({"01.mkv": 1080, "01.mp4": 720, "01.avi": 1080}, s / "01.mkv", s / "01.mp4", s / "01.avi")
        self.assertFalse((s / "01.mp4").exists())
        self.assertFalse((s / "01.avi").exists())
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0], "🧹 Anime/Show: sustituido por <b>01.mkv</b> (1080p), "
                                       "borrado: 01.mp4 (720p), 01.avi (1080p)")

    def test_better_or_unreadable_copies_are_kept(self):
        s = self.show
        self.remove({"01.mkv": 720, "01.mp4": 1080}, s / "01.mkv", s / "01.mp4", s / "01.avi")
        self.assertTrue((s / "01.mp4").exists())
        self.assertTrue((s / "01.avi").exists())
        self.assertEqual(self.sent, ["⚠️ Anime/Show: hay <b>01.mkv</b> nuevo pero dejo 01.mp4 (1080p > 720p), "
                                     "01.avi (no se pudo leer la resolución). Revísalo a mano."])

    def test_unreadable_new_file_keeps_everything(self):
        s = self.show
        self.remove({"01.mp4": 480}, s / "01.mkv", s / "01.mp4")
        self.assertTrue((s / "01.mp4").exists())
        self.assertIn("01.mp4 (no se pudo leer la resolución)", self.sent[0])

    def test_copy_that_cannot_be_deleted(self):
        s = self.show
        (s / "01.mpg").mkdir()
        self.remove({"01.mkv": 1080, "01.mpg": 480, "01.mp4": 480}, s / "01.mkv", s / "01.mpg", s / "01.mp4")
        self.assertEqual(len(self.sent), 2)
        self.assertIn("borrado: 01.mp4 (480p)", self.sent[0])
        self.assertIn("dejo 01.mpg (no se pudo borrar:", self.sent[1])

    def test_nothing_else_to_remove(self):
        s = self.show
        self.remove({"01.mkv": 1080}, s / "01.mkv", s / "01.mkv", s / "02.mp4")
        self.assertEqual(self.sent, [])

    def test_lower_copies_for(self):
        from automator.library.quality import lower_copies_for
        s = self.show
        (s / "02.mp4").write_text("x")
        self.assertEqual(sorted(lower_copies_for({}, 1, s / "01.mkv")), [s / "01.avi", s / "01.mp4", s / "01.wmv"])
        (s / "Película 01.mp4").write_text("x")
        (s / "Película 01.mkv").write_text("x")
        self.assertEqual(lower_copies_for({"movie": True}, 0, s / "Película 01.mkv"), [s / "Película 01.mp4"])
