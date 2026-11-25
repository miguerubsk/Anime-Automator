"""Vigilancia del Drive: carpeta del anime, vuelta de un anime y enlaces por capítulo."""

import time
from unittest import mock

from tests.helpers import AutomatorTestCase
from tests.test_watches import CFG, HOST, f

TITLE = "Serie Larga - Japan-Paw! Descarga Calidad"


class WatchCase(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        from automator.pipeline.watches import WatchRegistry
        self.w = WatchRegistry().replace([{"watch_url": "https://paste.japan-paw.net/?v=9", "title": TITLE}])[0]
        self.cfg_drive = {**self.cfg, **CFG}
        cfg = mock.patch("automator.pipeline.watches.load_config", return_value=self.cfg_drive)
        cfg.start()
        self.addCleanup(cfg.stop)

    def find(self, dirs, host=HOST, page=None, **watch):
        from automator.pipeline import watches
        from automator.pipeline.watches import DriveWatcher
        for k, v in watch.items():
            setattr(self.w, k, v)
        fetch = {"side_effect": page} if isinstance(page, Exception) else {"return_value": page}
        with mock.patch.object(watches.JapanPawPage, "fetch", **fetch) as fetch_mock, \
             mock.patch.object(watches.DriveMirror, "list_dirs", return_value=dirs) as list_dirs:
            folder = DriveWatcher().find_folder(watches.DriveMirror(self.cfg_drive), "japanpaw-emision:", self.w, host)
        return folder, fetch_mock, list_dirs


class FindFolderTest(WatchCase):
    def test_kuro_says_which_folder(self):
        folder, fetch, list_dirs = self.find(["Otra"], drive_folder="La Suya")
        self.assertEqual(folder, "La Suya")
        list_dirs.assert_not_called()
        fetch.assert_not_called()

    def test_by_name_and_dirs_are_cached(self):
        from automator.pipeline.watches import DriveWatcher
        self.assertEqual(self.find(["Otra", "Serie Larga"])[0], "Serie Larga")
        _, _, list_dirs = self.find(["Otra", "Serie Larga"])
        list_dirs.assert_not_called()   # la lista de carpetas dura una hora
        DriveWatcher()._dirs_at = time.time() - DriveWatcher.dirs_ttl_seconds - 1
        _, _, list_dirs = self.find(["Otra", "Serie Larga"])
        list_dirs.assert_called_once()

    def test_remembered_folder_only_if_still_there(self):
        self.assertEqual(self.find(["Corta"], host="", found_folder="Corta")[0], "Corta")
        from automator.pipeline.watches import DriveWatcher
        DriveWatcher()._dirs = []
        self.assertIsNone(self.find(["Otra"], host="", found_folder="Corta")[0])

    def test_without_host_the_paste_is_not_read(self):
        folder, fetch, _ = self.find(["Otra"], host="")
        self.assertIsNone(folder)
        fetch.assert_not_called()

    def test_unreadable_paste(self):
        with self.assertLogs("anime-automator", "WARNING"):
            folder, fetch, _ = self.find(["Otra"], page=RuntimeError("Cloudflare"))
        self.assertIsNone(folder)
        self.assertGreater(self.w.folder_checked_at, 0)


class FolderFromLinksTest(AutomatorTestCase):
    def test_first_folder_of_that_host(self):
        from automator.pipeline.watches import DriveWatcher
        links = ["", "https://otro/0:down/A/x.mkv", f"https://{HOST.upper()}/otra/ruta.mkv",
                 f"https://{HOST}/0:down/", f"https://{HOST}/0:down/No%20Listada/x.mkv",
                 f"https://{HOST}/0:down/Chii%20Fuyo/x.mkv"]
        self.assertEqual(DriveWatcher.folder_from_links(HOST, links, ["Chii Fuyo"]), "Chii Fuyo")
        self.assertEqual(DriveWatcher.folder_from_links(HOST, links, []), "No Listada")
        self.assertIsNone(DriveWatcher.folder_from_links(HOST, links[:4], []))


class PollTest(WatchCase):
    def poll(self, files=(), folder="Serie Larga", list_files=None, run_links=None, page=None, cfg=None):
        from automator.pipeline import watches
        from automator.pipeline.watches import DriveWatcher
        listing = {"side_effect": list_files} if list_files else {"return_value": list(files)}
        with mock.patch.object(DriveWatcher, "find_folder", return_value=folder), \
             mock.patch.object(watches.DriveMirror, "list_recent_files", **listing), \
             mock.patch("automator.pipeline.enqueue.Enqueuer.run_links",
                        return_value=run_links or {"status": "nothing_new"}) as rl, \
             mock.patch("automator.pipeline.enqueue.enqueue_from_webhook",
                        return_value=page or {"status": "nothing_new"}) as wh:
            result = DriveWatcher().poll(cfg or self.cfg_drive, self.w)
        return result, rl, wh

    def test_host_without_drive(self):
        cfg = {**self.cfg_drive, "drive_watch": {"enabled": True, "host": "otro.host"}}
        result, _, _ = self.poll(cfg=cfg)
        self.assertEqual(result["status"], "no_drive")
        self.assertIn("otro.host", result["error"])
        self.assertEqual(self.w.last_status, "no_drive")

    def test_no_folder_is_warned_once(self):
        with self.assertLogs("anime-automator", "WARNING"):
            self.assertEqual(self.poll(folder=None)[0], {"status": "no_folder"})
        with mock.patch("automator.pipeline.watches.log") as log:
            self.assertEqual(self.poll(folder=None)[0], {"status": "no_folder"})
        log.warning.assert_not_called()

    def test_drive_error(self):
        from automator.sources.drive import DriveError
        result, rl, _ = self.poll(list_files=DriveError("rclone"))
        self.assertEqual(result, {"status": "drive_error", "error": "rclone"})
        rl.assert_not_called()

    def test_drive_and_page_results_are_merged(self):
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        files = [f("[ToonsHub][Web 1080p]/X.S01E07.mkv", recent), f("[Puya][Web 1080p]/X - 08.mkv", recent)]
        result, rl, wh = self.poll(files, run_links={"status": "queued", "queued": [7]},
                                   page={"status": "queued", "queued": [8, 7]})
        self.assertEqual((result["status"], result["queued"], result["page"], result["drive_episodes"]),
                         ("queued", [7, 8], "queued", [7]))
        wh.assert_called_once_with(self.cfg_drive, TITLE, self.w.watch_url)

    def test_old_unnumbered_file_does_not_read_the_page(self):
        files = [f("[Puya][Web 1080p]/X - 08.mkv", "2020-01-01T00:00:00Z"), {"Path": "T/raro.mkv", "Name": "raro.mkv"}]
        result, _, wh = self.poll(files)
        wh.assert_not_called()
        self.assertEqual(result["status"], "nothing_new")

    def test_drive_result_without_status(self):
        result, _, _ = self.poll([f("T/X.S01E01.mkv")], run_links={"queued": []})
        self.assertEqual((result["status"], result["queued"]), ("error", []))


class LinksFromFilesTest(AutomatorTestCase):
    def links(self, files, season=None):
        from automator.pipeline.watches import DriveWatcher
        from automator.sources.japanpaw import TabRanking
        return DriveWatcher.links_from_files(HOST, "X", files, TabRanking("Erai-raws"), season)

    def test_files_at_the_root_and_without_path(self):
        links, unparsed = self.links([{"Name": "X.S01E02.mkv"}, f("X.S01E03.mkv"), f("[Erai-raws][Web 1080p]/X.S01E03.mkv")])
        self.assertEqual(sorted(links), [2, 3])
        self.assertTrue(links[2].endswith("/X/X.S01E02.mkv"), links[2])
        self.assertIn("Erai-raws", links[3])   # la pestaña del grupo preferido gana a la raíz
        self.assertEqual(unparsed, [])

    def test_name_wins_over_path_for_the_episode(self):
        links, _ = self.links([{"Path": "T/S01E09/x.mkv", "Name": "X.S01E04.mkv"}])
        self.assertEqual(list(links), [4])

    def test_nothing_parsable(self):
        self.assertEqual(self.links([f("T/X - 01.mkv"), f("T/notas.txt")]), ({}, [f("T/X - 01.mkv")]))
        self.assertEqual(self.links([]), ({}, []))

    def test_season_with_nothing(self):
        self.assertEqual(self.links([f("T/X.S01E01.mkv")], season=2), ({}, []))


class WatcherStepTest(WatchCase):
    def step(self, cfg):
        from automator.pipeline.watches import DriveWatcher
        with mock.patch("automator.pipeline.watches.load_config", return_value=cfg), \
             mock.patch.object(DriveWatcher, "poll", side_effect=RuntimeError("raro")) as poll, \
             mock.patch("automator.pipeline.watches.time.sleep") as sleep:
            DriveWatcher().step()
        return poll, sleep

    def test_disabled_does_nothing_and_waits_five_minutes(self):
        poll, sleep = self.step({**self.cfg_drive, "drive_watch": {"enabled": False}})
        poll.assert_not_called()
        sleep.assert_called_once_with(300)

    def test_a_failing_anime_does_not_stop_the_loop(self):
        with self.assertLogs("anime-automator", "ERROR"):
            poll, sleep = self.step(self.cfg_drive)
        poll.assert_called_once_with(self.cfg_drive, self.w)
        sleep.assert_called_once_with(600)
