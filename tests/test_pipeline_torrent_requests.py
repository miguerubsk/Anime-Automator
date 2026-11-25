"""Animes enteros por torrent: estados de plan(), temporadas por la cadena de AniList y start()."""

from pathlib import Path
from unittest import mock

from tests.test_torrent_requests import AniListMocked, media


def release(ep, title="[Erai-raws] Peli", preferred=False):
    from automator.sources.nyaa import Release
    return Release(ep, title, f"https://t.invalid/{ep}", "Erai-raws", (not preferred, False, False, False))


class PlanStatesTest(AniListMocked):
    def test_nas_not_mounted(self):
        from automator.pipeline.torrent_requests import TorrentRequest
        with mock.patch("automator.pipeline.torrent_requests.assert_nas_mounted", side_effect=RuntimeError("sin NAS")):
            self.assertEqual(TorrentRequest(self.cfg, 1).plan(), {"status": "nas_not_mounted", "error": "sin NAS"})

    def test_anilist_down(self):
        from automator.library.anilist import AniListDown
        from automator.pipeline.torrent_requests import TorrentRequest
        with mock.patch("automator.library.anilist.AniListClient.media", side_effect=AniListDown("429")):
            self.assertEqual(TorrentRequest(self.cfg, 1).plan(), {"status": "anilist_down", "error": "429"})

    def test_folder_resolution_failed(self):
        self.db[1] = media(1, "Uno", 12)
        req = self.plan(1, [1])
        with mock.patch("automator.pipeline.torrent_requests.Library.resolve", side_effect=OSError("smb")), \
                self.assertLogs("anime-automator", "ERROR"):
            self.assertEqual(req.plan(), {"status": "folder_resolution_failed", "error": "smb"})

    def test_everything_missing_already_downloading(self):
        from automator.pipeline.registry import InProgress
        self.db[1] = media(1, "Uno", 12)
        folder = str(self.root / "Anime" / "Uno")
        InProgress().add((folder, 1))
        InProgress().add((folder, 2))
        result = self.plan(1, [1, 2]).plan()
        self.assertEqual((result["status"], result["missing"], result["already_in_progress"]),
                         ("in_progress", [], [1, 2]))

    def test_summary(self):
        self.db[1] = media(1, "Uno", 3)
        folder = self.root / "Anime" / "Uno"
        folder.mkdir()
        for name in ("01.mkv", "13.mkv"):        # el 13 no es de esta entrada (solo tiene 3)
            (folder / name).write_text("x")
        result = self.plan(1, [1, 2]).plan()
        self.assertEqual({k: result[k] for k in ("status", "found", "available", "have", "have_episodes",
                                                 "missing", "preferred", "groups", "episodes", "new_folder")},
                         {"status": "missing", "found": [1, 2], "available": 2, "have": 1, "have_episodes": [1],
                          "missing": [2], "preferred": 2, "groups": {"Erai-raws": 2}, "episodes": 3,
                          "new_folder": False})

    def test_unknown_episode_count_keeps_everything(self):
        self.db[1] = media(1, "Uno", None)
        folder = self.root / "Anime" / "Uno"
        folder.mkdir()
        (folder / "13.mkv").write_text("x")
        result = self.plan(1, [1]).plan()
        self.assertEqual((result["have"], result["have_episodes"]), (1, [13]))


class SeasonFromChainTest(AniListMocked):
    """El título no empieza por el de la primera temporada: el número sale de la cadena de precuelas."""

    def test_sequel_with_another_name(self):
        self.db[1] = media(1, "Shingeki no Kyojin", 25)
        self.db[2] = media(2, "Attack Titan Final", 12, prequel=1)
        result = self.plan(2, [1]).plan()
        self.assertEqual((result["name"], result["season"], result["subtitle"]),
                         ("Shingeki no Kyojin", "2", "Attack Titan Final"))

    def test_short_one_between_seasons_is_point_five(self):
        self.db[1] = media(1, "Shingeki no Kyojin", 25)
        self.db[2] = media(2, "Lost Girls", 4, prequel=1)
        result = self.plan(2, [1]).plan()
        self.assertEqual((result["season"], result["subtitle"]), ("1.5", "Lost Girls"))

    def test_ova_keeps_its_own_name(self):
        self.db[1] = media(1, "Shingeki no Kyojin", 25)
        self.db[2] = media(2, "Ilse no Techou", 1, fmt="OVA", prequel=1)
        result = self.plan(2, [1]).plan()
        self.assertEqual((result["name"], result["season"]), ("Ilse no Techō", "1"))

    def test_chain_without_main_seasons(self):
        self.db[1] = media(1, "Corto", 3)
        self.db[2] = media(2, "Corto Dos Mas", 3, prequel=1)
        result = self.plan(2, [1]).plan()
        self.assertEqual((result["name"], result["season"]), ("Corto Dos Mas", "1"))


class MoviePlanTest(AniListMocked):
    def plan_movie(self, rel):
        from automator.pipeline.torrent_requests import TorrentRequest
        self.db[1] = media(1, "Peli Suelta", 1, fmt="MOVIE")
        with mock.patch("automator.sources.nyaa.NyaaSearch.movie", return_value=rel):
            req = TorrentRequest(self.cfg, 1)
            return req, req.plan()

    def test_not_on_nyaa(self):
        _, result = self.plan_movie(None)
        self.assertEqual((result["status"], result["found"], result["groups"], result["missing"]),
                         ("nothing_found", [], {}, []))

    def test_already_downloading(self):
        from automator.pipeline.registry import InProgress
        _, first = self.plan_movie(release(1, preferred=True))
        self.assertEqual((first["status"], first["preferred"], first["groups"]), ("missing", 1, {"Erai-raws": 1}))
        InProgress().add((f"movie:{Path(first['folder']) / first['file']}", 1))
        _, result = self.plan_movie(release(1))
        self.assertEqual((result["status"], result["missing"], result["already_in_progress"]),
                         ("in_progress", [], [1]))

    def test_start_adds_the_movie(self):
        from automator.pipeline.torrents import TorrentTracker
        req, _ = self.plan_movie(release(1))
        torrent = mock.Mock(hashString="hm")
        with mock.patch("automator.sources.nyaa.NyaaSearch.movie", return_value=release(1)), \
                mock.patch("automator.pipeline.torrents.Transmission.add", return_value=(None, torrent)):
            result = req.start()
        self.assertEqual((result["status"], result["queued"], result["tracked"]), ("queued", [1], False))
        self.assertTrue(Path(result["folder"]).is_dir())
        (h, tracked), = TorrentTracker().snapshot()
        self.assertEqual((h, tracked.item.target["filename"], tracked.item.is_movie), ("hm", result["file"], True))


    def test_start_movie_of_a_series_keeps_its_episode_name(self):
        from automator.pipeline.torrent_requests import TorrentRequest
        from automator.pipeline.torrents import TorrentTracker
        (self.root / "Anime" / "Seitokai Yakuindomo").mkdir()
        self.db[2] = media(2, "Seitokai Yakuindomo Movie 2", 1, fmt="MOVIE")
        with mock.patch("automator.sources.nyaa.NyaaSearch.movie", return_value=release(1)), \
                mock.patch("automator.pipeline.torrents.Transmission.add",
                           return_value=(None, mock.Mock(hashString="hm"))):
            result = TorrentRequest(self.cfg, 2).start()
        self.assertEqual((result["status"], result["name"]), ("queued", "Seitokai Yakuindomo Movie 2"))
        (_, tracked), = TorrentTracker().snapshot()
        self.assertEqual((tracked.item.target["name"], tracked.item.title), ("Película 02", result["title"]))


class StartTest(AniListMocked):
    def test_nothing_to_start(self):
        self.db[1] = media(1, "Uno", 12)
        with mock.patch("automator.pipeline.torrents.Transmission.add") as add:
            result = self.plan(1, []).start()
        self.assertEqual(result["status"], "nothing_found")
        add.assert_not_called()

    def test_folder_cannot_be_created(self):
        from automator.library.folders import Library
        self.db[1] = media(1, "Uno", 12)
        real = Library.resolve

        def resolve(self_, cfg, title, link_emision=True, create=True, **kw):
            if create:
                raise OSError("solo lectura")
            return real(self_, cfg, title, link_emision=link_emision, create=create, **kw)
        with mock.patch.object(Library, "resolve", resolve), self.assertLogs("anime-automator", "ERROR"):
            result = self.plan(1, [1]).start()
        self.assertEqual((result["status"], result["error"], result["missing"]),
                         ("folder_resolution_failed", "solo lectura", [1]))

    def test_transmission_refuses_everything(self):
        from automator.pipeline.registry import InProgress
        from automator.pipeline.torrents import TorrentTracker
        self.db[1] = media(1, "Uno", 12)
        with mock.patch("automator.pipeline.torrents.Transmission.add", side_effect=ConnectionError("caído")):
            result = self.plan(1, [1, 2]).start(job_id=3)
        self.assertEqual((result["status"], result["not_added"]), ("transmission_failed", [1, 2]))
        self.assertEqual((InProgress().snapshot(), TorrentTracker().snapshot()), (set(), []))
