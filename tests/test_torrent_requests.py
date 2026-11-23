"""Animes enteros por torrent: catálogo de nyaa.si, carpeta/temporada desde AniList y alta en Transmission."""

import os
import xml.etree.ElementTree as ET
from unittest import mock

from tests.helpers import AutomatorTestCase


def rss(*titles):
    xml = "<rss><channel>" + "".join(
        f'<item><title>{t}</title><enclosure url="https://t.invalid/{i}.torrent"/></item>'
        for i, t in enumerate(titles)) + "</channel></rss>"
    return ET.fromstring(xml).findall(".//item")


def media(mid, romaji, episodes=12, fmt="TV", english=None, synonyms=(), prequel=None):
    edges = [{"relationType": "PREQUEL", "node": {"id": prequel, "type": "ANIME", "format": "TV", "episodes": 12}}] \
        if prequel else []
    return {"id": mid, "format": fmt, "episodes": episodes, "synonyms": list(synonyms),
            "title": {"romaji": romaji, "english": english}, "startDate": {"year": 2020},
            "relations": {"edges": edges}}


class CatalogTest(AutomatorTestCase):
    def search(self):
        from automator.sources.nyaa import NyaaSearch
        return NyaaSearch(self.cfg)

    def test_names_of_a_release(self):
        from automator.sources.nyaa import core_key, release_names
        self.assertIn("kanatakara", release_names(
            "[ToonsHub] From Far Away S01E08 1080p CR WEB-DL AAC2.0 H.264 (Kanata kara, Multi-Subs)"))
        self.assertIn("fromfaraway", release_names(
            "[ToonsHub] From Far Away S01E08 1080p CR WEB-DL AAC2.0 H.264 (Kanata kara, Multi-Subs)"))
        self.assertEqual(release_names("[Erai-raws] Kono Oto Tomare! 2nd Season - 13 END [1080p].mkv"),
                         {core_key("Kono Oto Tomare!")})

    def test_last_episode_marked_end(self):
        from automator.sources.nyaa import parse_nyaa_release
        self.assertEqual(parse_nyaa_release("[Erai-raws] Kono Oto Tomare! 2nd Season - 13 END [1080p].mkv"), (13, 2))
        self.assertEqual(parse_nyaa_release("[Erai-raws] Show - 05v2 [1080p][MultiSub]"), (5, 1))

    def test_spanish_subs_rule(self):
        s = self.search()
        self.assertTrue(s.subs_ok("[Erai-raws] Show - 01 [1080p CR WEB-DL AVC AAC][MultiSub][AB12]"))
        self.assertTrue(s.subs_ok("[Erai-raws] Show - 01 [1080p].mkv"))   # Erai-raws, aunque no lo diga
        self.assertTrue(s.subs_ok("[ToonsHub] Show S01E01 1080p CR WEB-DL (Multi-Subs)"))
        self.assertFalse(s.subs_ok("[ToonsHub] Show S01E01 1080p HIDIVE WEB-DL (Show, English-Sub)"))
        self.assertFalse(s.subs_ok("[SubsPlease] Show - 01 (1080p) [AB12].mkv"))
        self.assertFalse(s.subs_ok("[Judas] Show (Season 2) [1080p][HEVC x265 10bit][Eng-Subs]"))

    def test_best_release_per_episode(self):
        items = rss(
            "[ToonsHub] Hotel Inhumans S02E01 1080p CR WEB-DL AAC2.0 H.264 (Multi-Subs)",
            "[Erai-raws] Hotel Inhumans Season 2 - 01 [1080p CR WEBRip HEVC AAC][MultiSub][AA]",
            "[Erai-raws] Hotel Inhumans Season 2 - 01 [1080p CR WEB-DL AVC AAC][MultiSub][BB]",
            "[ToonsHub] Hotel Inhumans S02E02 1080p CR WEB-DL AAC2.0 H.264 (Multi-Subs)",
            "[Erai-raws] Hotel Inhumans - 03 [1080p CR WEB-DL AVC AAC][MultiSub][CC]",       # temporada 1
            "[Erai-raws] Hotel Inhumans Season 2 - 04 [1080p CR WEB-DL AVC AAC][English Dub]",
            "[SubsPlease] Hotel Inhumans S2 - 05 (1080p) [DD].mkv",                          # sin español
            "[Erai-raws] Hotel Inhumans Season 2 - 14 [1080p CR WEB-DL AVC AAC][MultiSub][EE]",  # > 12
            "[Erai-raws] Otro Anime Season 2 - 06 [1080p CR WEB-DL AVC AAC][MultiSub][FF]",
        )
        s = self.search()
        with mock.patch.object(type(s), "_items", return_value=items):
            found = s.catalog(["Hotel Inhumans Season 2"], {2}, 12)
        self.assertEqual(sorted(found), [1, 2])
        self.assertIn("[BB]", found[1].title)          # Erai-raws WEB-DL
        self.assertTrue(found[1].preferred)
        self.assertEqual(found[2].group, "ToonsHub")    # sin Erai-raws, otro grupo con subtítulos

    def test_stops_searching_when_complete(self):
        items = rss(*[f"[Erai-raws] Corto - {i:02d} [1080p][MultiSub]" for i in (1, 2)])
        s = self.search()
        with mock.patch.object(type(s), "_items", return_value=items) as get:
            found = s.catalog(["Corto", "Short"], {1}, 2)
        self.assertEqual(sorted(found), [1, 2])
        self.assertEqual(get.call_count, 1)


class AniListMocked(AutomatorTestCase):
    """AniList sale de self.db (por id) y nyaa.si de plan(..., releases)."""

    def setUp(self):
        super().setUp()
        self.db = {}
        self._media = mock.patch("automator.library.anilist.AniListClient.media",
                                 side_effect=lambda mid: self.db.get(mid))
        self._media.start()

    def tearDown(self):
        self._media.stop()
        super().tearDown()

    def plan(self, mid, releases, season=None):
        from automator.pipeline.torrent_requests import TorrentRequest
        from automator.sources.nyaa import Release
        found = {ep: Release(ep, f"[Erai-raws] X - {ep:02d}", f"https://t.invalid/{ep}", "Erai-raws",
                             (False, False, False, False)) for ep in releases}
        req = TorrentRequest(self.cfg, mid, season)
        self.catalog = mock.patch("automator.sources.nyaa.NyaaSearch.catalog", return_value=found)
        self.catalog_mock = self.catalog.start()
        self.addCleanup(self.catalog.stop)
        return req


class TorrentRequestTest(AniListMocked):
    def test_second_season_goes_to_its_folder(self):
        self.db[1] = media(1, "Kono Oto Tomare!", 13)
        self.db[2] = media(2, "Kono Oto Tomare! 2nd Season", 13, prequel=1)
        (self.root / "Anime" / "Kono Oto Tomare!").mkdir()
        (self.root / "Anime" / "Kono Oto Tomare!" / "01.mkv").write_text("x")
        result = self.plan(2, [1, 2, 3]).plan()
        self.assertEqual(result["status"], "missing")
        self.assertEqual((result["name"], result["season"]), ("Kono Oto Tomare!", "2"))
        self.assertTrue(result["folder"].endswith("Kono Oto Tomare!/Temporada 2"), result["folder"])
        self.assertEqual(result["missing"], [1, 2, 3])   # el 01 de la T1 no cuenta
        names, seasons, total = self.catalog_mock.call_args[0]
        self.assertEqual((seasons, total), ({2}, 13))

    def test_admin_corrects_the_season(self):
        self.db[5] = media(5, "Dr. Stone: New World", 11)
        result = self.plan(5, [1], season=3).plan()
        self.assertEqual(result["season"], "3")
        self.assertIn(3, self.catalog_mock.call_args[0][1])
        self.assertIn(1, self.catalog_mock.call_args[0][1])   # el título no dice temporada: "1" también vale

    def test_states(self):
        self.db[7] = media(7, "Nada", 12)
        self.assertEqual(self.plan(7, []).plan()["status"], "nothing_found")
        self.db[8] = media(8, "Peli", 1, fmt="MOVIE")
        self.assertEqual(self.plan(8, [1]).plan()["status"], "unsupported")
        self.assertEqual(self.plan(99, [1]).plan()["status"], "not_found")
        folder = self.root / "Anime" / "Completo"
        folder.mkdir()
        (folder / "01.mkv").write_text("x")
        self.db[9] = media(9, "Completo", 1)
        self.assertEqual(self.plan(9, [1]).plan()["status"], "nothing_new")

    def test_start_adds_torrents_and_tracks_the_job(self):
        os.environ["REQUESTER_CALLBACK_URL"] = "http://requester.invalid/events"
        from automator.pipeline.jobs import JobTracker
        from automator.pipeline.registry import InProgress
        from automator.pipeline.torrents import TorrentTracker
        self.db[3] = media(3, "Nuevo Anime", 12)
        req = self.plan(3, [1, 2])
        torrent = mock.Mock(hashString="h1")
        with mock.patch("automator.pipeline.torrents.Transmission.add",
                        side_effect=[(None, torrent), ConnectionError("caído")]):
            result = req.start(job_id=77)
        self.assertEqual((result["status"], result["queued"], result["not_added"]), ("queued", [1], [2]))
        self.assertTrue(result["tracked"])
        self.assertTrue((self.root / "Anime" / "Nuevo Anime").is_dir())
        folder = str(self.root / "Anime" / "Nuevo Anime")
        self.assertIn((folder, 1), InProgress())
        self.assertNotIn((folder, 2), InProgress())
        self.assertEqual([h for h, _ in TorrentTracker().snapshot()], ["h1"])
        self.assertTrue(JobTracker().event((folder, 1), "episode_done"))
        self.assertEqual(JobTracker().outbox._items[-1]["event"], "job_complete")


class TorrentEndpointTest(AutomatorTestCase):
    def test_needs_secret_and_id(self):
        os.environ["REQUESTER_CALLBACK_SECRET"] = "s3"
        from automator.web import create_app
        client = create_app().test_client()
        self.assertEqual(client.post("/torrent/check", json={"anilist_id": 1}).status_code, 401)
        r = client.post("/torrent/check", json={}, headers={"X-Callback-Secret": "s3"})
        self.assertEqual(r.status_code, 400)
        with mock.patch("automator.pipeline.torrent_requests.TorrentRequest.plan",
                        return_value={"status": "nothing_found", "folder": ""}):
            r = client.post("/torrent/check", json={"anilist_id": 1}, headers={"X-Callback-Secret": "s3"})
        self.assertEqual(r.get_json()["status"], "nothing_found")


def chained(mid, romaji, fmt="TV", episodes=13, prequel=None, synonyms=()):
    """Entrada de AniList con su precuela (del formato que sea: la T2 puede colgar de una OVA)."""
    m = media(mid, romaji, episodes, fmt, synonyms=synonyms)
    if prequel:
        pid, pfmt = prequel
        m["relations"]["edges"] = [{"relationType": "PREQUEL", "node": {"id": pid, "type": "ANIME", "format": pfmt}}]
    return m


class SeasonDetectionTest(AniListMocked):
    """Temporadas que solo se distinguen por un signo pegado al nombre."""

    def setUp(self):
        super().setUp()
        self.db[8675] = chained(8675, "Seitokai Yakuindomo")
        self.db[10119] = chained(10119, "Seitokai Yakuindomo OVA", "OVA", 8, prequel=(8675, "TV"))
        self.db[20448] = chained(20448, "Seitokai Yakuindomo＊", prequel=(10119, "OVA"),
                                 synonyms=["Seitokai Yakuindomo*", "Seitokai Yakuindomo Season 2"])

    def test_split_on_base(self):
        from automator.library.anilist import _split_on_base
        self.assertEqual(_split_on_base("Seitokai Yakuindomo＊", ["Seitokai Yakuindomo"]), ("Seitokai Yakuindomo", ""))
        self.assertEqual(_split_on_base("Okusama ga Seitokaichou!+!", ["Okusama ga Seitokaichou!"]),
                         ("Okusama ga Seitokaichou!", ""))
        self.assertEqual(_split_on_base("Zombieland Saga Revenge", ["Zombieland Saga"]), ("Zombieland Saga", "Revenge"))
        self.assertEqual(_split_on_base("Darker than Black: Ryuusei no Gemini", ["Darker than Black"]),
                         ("Darker than Black", "Ryuusei no Gemini"))
        self.assertIsNone(_split_on_base("Workingman", ["Working"]))

    def test_second_season_with_an_asterisk(self):
        from automator.library.anilist import SeasonResolver
        info = SeasonResolver().info_for(self.db[20448], "Seitokai Yakuindomo＊")
        self.assertEqual((info["base"], info["season"], info["subtitle"]), ("Seitokai Yakuindomo", "2", ""))

    def test_exact_title_first(self):
        from automator.library.anilist import SeasonResolver
        resolver = SeasonResolver()
        with mock.patch.object(resolver.client, "search", return_value=[self.db[20448], self.db[8675]]):
            self.assertEqual(resolver.find_media("Seitokai Yakuindomo")["id"], 8675)
            self.assertEqual(resolver.find_media("Seitokai Yakuindomo*")["id"], 20448)
            info = resolver.season_info("Seitokai Yakuindomo*")
        self.assertEqual((info["base"], info["season"]), ("Seitokai Yakuindomo", "2"))

    def test_torrent_goes_to_season_2_not_the_series_folder(self):
        base = self.root / "Anime" / "Seitokai Yakuindomo"
        for season in (1, 2):
            (base / f"Temporada {season}").mkdir(parents=True)
        (base / "Temporada 1" / "01.mkv").write_text("x")
        result = self.plan(20448, [1, 2]).plan()
        self.assertEqual((result["name"], result["season"], result["subtitle"]), ("Seitokai Yakuindomo", "2", ""))
        self.assertTrue(result["folder"].endswith("Seitokai Yakuindomo/Temporada 2"), result["folder"])
        self.assertEqual(result["missing"], [1, 2])
