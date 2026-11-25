import json
import os
from unittest import mock

from tests.helpers import AutomatorTestCase

HOST = "emision.craftervault.com"
CFG = {"drive_mirror": {"enabled": True, "prefer": True, "hosts": {HOST: "japanpaw-emision:"}},
       "drive_watch": {"enabled": True, "host": HOST}}


def f(path, mtime="2026-11-23T10:00:00Z"):
    return {"Path": path, "Name": path.split("/")[-1], "ModTime": mtime}


class ParsingTest(AutomatorTestCase):
    def test_parse_episode(self):
        from automator.pipeline.watches import parse_episode
        self.assertEqual(parse_episode("Full.Clearing.S01E07.1080p.CR.WEB-DL.mkv"), (1, 7))
        self.assertEqual(parse_episode("Serie S2E10 [1080p].mkv"), (2, 10))
        self.assertIsNone(parse_episode("[PuyaSubs!] Berserk S2 - 16 [1080p].mkv"))  # numeración absoluta
        self.assertIsNone(parse_episode("[Erai-raws] Serie - 07 [1080p].mkv"))

    def test_links_best_tab_per_episode(self):
        from automator.pipeline.watches import DriveWatcher
        from automator.sources.japanpaw import TabRanking
        files = [f("[ToonsHub][Web 720p]/X.S01E06.mkv"), f("[ToonsHub][Web 720p]/X.S01E07.mkv"),
                 f("[ToonsHub][Web 1080p]/X.S01E07.mkv"), f("[Grupo][Web 1080p]/X - 08.mkv"),
                 f("[ToonsHub][Web 1080p]/notas.txt")]
        links, unparsed = DriveWatcher.links_from_files(HOST, "Shinja Zero", files, TabRanking("Erai-raws"))
        self.assertEqual(sorted(links), [6, 7])
        self.assertIn("%5BWeb%201080p%5D", links[7].replace("[", "%5B").replace("]", "%5D"))
        self.assertTrue(links[7].startswith(f"https://{HOST}/0:down/Shinja%20Zero/"))
        self.assertEqual([u["Name"] for u in unparsed], ["X - 08.mkv"])

    def test_links_round_trip_to_drive(self):
        from automator.pipeline.watches import DriveWatcher
        from automator.sources.drive import DriveMirror
        from automator.sources.japanpaw import TabRanking
        links, _ = DriveWatcher.links_from_files(HOST, "Ao no Hako Season 2",
                                                 [f("[Erai-raws][Web 1080p]/Ao no Hako S02E03 (x).mkv")],
                                                 TabRanking("Erai-raws"))
        self.assertEqual(DriveMirror(CFG).remote_path(links[3]),
                         "japanpaw-emision:Ao no Hako Season 2/[Erai-raws][Web 1080p]/Ao no Hako S02E03 (x).mkv")

    def test_only_latest_season(self):
        from automator.pipeline.watches import DriveWatcher
        from automator.sources.japanpaw import TabRanking
        files = [f("T/X.S01E12.mkv"), f("T/X.S02E01.mkv")]
        links, _ = DriveWatcher.links_from_files(HOST, "X", files, TabRanking("g"))
        self.assertEqual(list(links), [1])
        links, _ = DriveWatcher.links_from_files(HOST, "X", files, TabRanking("g"), season=1)
        self.assertEqual(list(links), [12])

    def test_kuro_window(self):
        from datetime import datetime
        from automator.pipeline.watches import Watch
        # Kuro: domingos desde las 17:00 durante 48 h (cruza la medianoche y el lunes)
        w = Watch.from_dict({"watch_url": "u", "title": "t",
                             "schedule": {"weekday": 6, "start_min": 17 * 60, "hours": 48, "source": "anilist"}})
        at = lambda d, h: datetime(2026, 11, 16 + d, h, 0)  # 16/11/2026 = lunes
        self.assertFalse(w.active_at(at(6, 16)))   # domingo 16:00
        self.assertTrue(w.active_at(at(6, 17)))    # domingo 17:00
        self.assertTrue(w.active_at(at(7, 3)))     # lunes 03:00
        self.assertTrue(w.active_at(at(8, 16)))    # martes 16:00
        self.assertFalse(w.active_at(at(8, 17)))   # martes 17:00: ya pasaron 48 h
        self.assertEqual(w.describe(), "domingo 17:00, 48 h")
        self.assertTrue(Watch("u", "t").active_at(at(3, 12)))  # sin ventana: siempre


class PollTest(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        from automator.pipeline.watches import WatchRegistry
        self.w = WatchRegistry().replace([{
            "watch_url": "https://paste.japan-paw.net/?v=8153", "schedule": {"weekday": 6, "start_min": 0, "hours": 48},
            "title": "Shinja Zero no Megami-sama to Hajimeru Isekai Kouryaku - Japan-Paw! Descarga Calidad"}])[0]

    def poll(self, files, run_links=None, page=None):
        from automator.pipeline import watches
        from automator.pipeline.watches import DriveWatcher
        cfg = {**self.cfg, **CFG, "site_title_suffix": "- Japan-Paw! Descarga Calidad"}
        with mock.patch.object(watches.DriveMirror, "list_dirs",
                               return_value=["Otra Serie", "Shinja Zero no Megami-sama to Hajimeru Isekai Kouryaku"]), \
             mock.patch.object(watches.DriveMirror, "list_recent_files", return_value=files) as lr, \
             mock.patch("automator.pipeline.enqueue.Enqueuer.run_links",
                        return_value=run_links or {"status": "nothing_new"}) as rl, \
             mock.patch("automator.pipeline.enqueue.enqueue_from_webhook",
                        return_value=page or {"status": "nothing_new"}) as wh, \
             mock.patch("automator.pipeline.watches.load_config", return_value=cfg):
            result = DriveWatcher().poll(cfg, self.w)
        return result, lr, rl, wh

    def test_new_episode_is_queued_from_drive(self):
        result, lr, rl, wh = self.poll([f("[ToonsHub][Web 1080p]/X.S01E07.mkv")],
                                       run_links={"status": "queued", "queued": [7]})
        self.assertEqual(lr.call_args.args[1], "Shinja Zero no Megami-sama to Hajimeru Isekai Kouryaku")
        req, links = rl.call_args.args
        self.assertEqual((req.watch_url, req.watch, list(links)), (self.w.watch_url, True, [7]))
        wh.assert_not_called()
        self.assertEqual((result["status"], result["queued"]), ("queued", [7]))

    def test_unnumbered_file_reads_the_page_once(self):
        import time
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        files = [f("[PuyaSubs!][Web 1080p]/Serie - 16.mkv", recent)]
        _, _, rl, wh = self.poll(files, page={"status": "queued", "queued": [4]})
        rl.assert_not_called()
        wh.assert_called_once()
        _, _, _, wh2 = self.poll(files)  # al rato: no vuelve a leer la ficha todavía
        wh2.assert_not_called()

    def test_folder_from_paste_links_when_the_name_differs(self):
        from automator.pipeline import watches
        from automator.pipeline.watches import WatchRegistry
        w = WatchRegistry().replace([{"watch_url": "https://paste.japan-paw.net/?v=8128",
                                      "title": "Tsuihou Sareta Cheat Fuyo Majutsushi wa Kimama na Second Life "
                                               "- Japan-Paw! Descarga Calidad"}])[0]
        self.w = w
        page = mock.Mock()
        page.tabs.return_value = [{"links": {
            1: "https://otro.host/0:down/Nada/x.mkv",
            2: f"https://{HOST}/0:down/Chii%20Fuyo/%5BErai-raws%5D%5BWeb%201080p%5D/Chii%20Fuyo%20-%2002.mkv"}}]
        with mock.patch.object(watches.JapanPawPage, "fetch", return_value=page) as fetch, \
             mock.patch.object(watches.DriveMirror, "list_dirs", return_value=["Chii Fuyo", "Otra Serie"]):
            from automator.pipeline.watches import DriveWatcher
            mirror = watches.DriveMirror({**self.cfg, **CFG})
            dw = DriveWatcher()
            dw._dirs = []
            self.assertEqual(dw.find_folder(mirror, "japanpaw-emision:", w, HOST), "Chii Fuyo")
            self.assertEqual(dw.find_folder(mirror, "japanpaw-emision:", w, HOST), "Chii Fuyo")
            fetch.assert_called_once()                     # se recuerda: no vuelve a leer el paste
        # Kuro manda la lista otra vez: no se pierde
        w2 = WatchRegistry().replace([{"watch_url": w.watch_url, "title": w.title}])[0]
        self.assertEqual(w2.found_folder, "Chii Fuyo")

    def test_no_folder_does_not_reread_the_paste_every_poll(self):
        from automator.pipeline import watches
        from automator.pipeline.watches import DriveWatcher
        page = mock.Mock()
        page.tabs.return_value = []
        with mock.patch.object(watches.JapanPawPage, "fetch", return_value=page) as fetch, \
             mock.patch.object(watches.DriveMirror, "list_dirs", return_value=["Otra Serie"]):
            mirror = watches.DriveMirror({**self.cfg, **CFG})
            dw = DriveWatcher()
            dw._dirs = []
            self.assertIsNone(dw.find_folder(mirror, "japanpaw-emision:", self.w, HOST))
            self.assertIsNone(dw.find_folder(mirror, "japanpaw-emision:", self.w, HOST))
            fetch.assert_called_once()

    def test_state_is_saved(self):
        from automator.core.singleton import Singleton
        from automator.pipeline.watches import WatchRegistry
        self.poll([])
        Singleton.reset(WatchRegistry)
        w = WatchRegistry().get(self.w.watch_url)
        self.assertEqual((w.schedule["weekday"], w.last_status), (6, "nothing_new"))
        self.assertGreater(w.last_poll, 0)


class WatchesApiTest(AutomatorTestCase):
    def client(self):
        from automator.web import create_app
        return create_app().test_client()

    def test_put_requires_secret(self):
        os.environ["REQUESTER_CALLBACK_SECRET"] = "s3cr3t"
        body = {"watches": [{"watch_url": "https://paste.japan-paw.net/?v=1", "title": "X", "timezone": "Europe/Madrid",
                             "schedule": {"weekday": 2, "start_min": 600, "hours": 24}}]}
        c = self.client()
        self.assertEqual(c.put("/watches", json=body).status_code, 401)
        r = c.put("/watches", json=body, headers={"X-Callback-Secret": "s3cr3t"})
        self.assertEqual((r.status_code, r.get_json()["count"]), (200, 1))
        listed = c.get("/watches").get_json()["watches"]
        self.assertEqual((listed[0]["title"], listed[0]["window"]), ("X", "miércoles 10:00, 24 h"))
        bad = c.put("/watches", json={"watches": [{"title": "sin url"}]}, headers={"X-Callback-Secret": "s3cr3t"})
        self.assertEqual(bad.status_code, 400)
        bad_tz = c.put("/watches", json={"watches": [{**body["watches"][0], "timezone": "Marte/Olympus"}]},
                       headers={"X-Callback-Secret": "s3cr3t"})
        self.assertEqual(bad_tz.status_code, 400)
        self.assertEqual(json.loads((self.root / "tmp" / "watches.json").read_text())["watches"][0]["title"], "X")


class WeeklyPauseTest(AutomatorTestCase):
    """Con el capítulo de la semana ya guardado, no se vuelve a mirar hasta la siguiente ventana."""

    def setUp(self):
        super().setUp()
        from automator.pipeline.watches import WatchRegistry
        self.folder = self.root / "Anime" / "Serie"
        self.folder.mkdir(parents=True, exist_ok=True)
        for ep in (1, 2, 3):
            (self.folder / f"{ep:02d}.mkv").write_bytes(b"x")
        self.w = WatchRegistry().replace([{"watch_url": "https://paste.japan-paw.net/?v=5", "title": "Serie",
                                           "schedule": {"weekday": 6, "start_min": 17 * 60, "hours": 48}}])[0]

    def done(self, ep, when):
        from datetime import datetime
        from automator.pipeline.watches import Watch, WatchRegistry
        with mock.patch.object(Watch, "now", return_value=when):
            WatchRegistry().episode_done(self.w.watch_url, str(self.folder), ep)
        return datetime.fromtimestamp(self.w.done_until) if self.w.done_until else None

    def test_pauses_until_window_end(self):
        from datetime import datetime
        # domingo 22/11/2026 a las 20:30, dentro de la ventana (dom 17:00 + 48 h)
        self.assertEqual(self.done(3, datetime(2026, 11, 22, 20, 30)), datetime(2026, 11, 24, 17, 0))

    def test_old_episode_or_outside_window_does_not_pause(self):
        from datetime import datetime
        self.assertIsNone(self.done(2, datetime(2026, 11, 22, 20, 30)))   # atrasado: falta el de la semana
        self.assertIsNone(self.done(3, datetime(2026, 11, 26, 12, 0)))    # jueves: fuera de la ventana

    def test_unknown_anime_is_ignored(self):
        from automator.pipeline.watches import WatchRegistry
        WatchRegistry().episode_done("https://otra", str(self.folder), 3)  # no lanza

    def test_paused_watch_is_skipped_by_the_loop(self):
        import time
        from automator.pipeline.watches import DriveWatcher, WatchRegistry
        self.w.done_until = time.time() + 3600
        WatchRegistry().save()
        cfg = {**self.cfg, **CFG}
        with mock.patch("automator.pipeline.watches.load_config", return_value=cfg), \
             mock.patch.object(DriveWatcher, "poll") as poll, \
             mock.patch("automator.pipeline.watches.time.sleep"):
            DriveWatcher().step()
        poll.assert_not_called()

    def test_saved_episode_from_worker_pauses(self):
        from automator.pipeline.watches import WatchRegistry
        from automator.pipeline.worker import Finisher
        item = self.item("Serie", 3, page_url=self.w.watch_url)
        with mock.patch.object(WatchRegistry, "episode_done") as done:
            Finisher().finish(self.cfg, item, dest=self.folder / "03.mkv")
        done.assert_called_once_with(self.w.watch_url, item.folder, 3)

    def test_poll_pauses_if_week_episode_already_saved(self):
        from datetime import datetime
        from automator.pipeline import watches
        from automator.pipeline.watches import DriveWatcher, Watch
        cfg = {**self.cfg, **CFG}
        sunday_night = datetime(2026, 11, 22, 21, 0)
        import os
        os.utime(self.folder / "03.mkv", (datetime(2026, 11, 22, 20, 0).timestamp(),) * 2)  # guardado después
        files = [f("[Grupo][Web 1080p]/Serie - 03.mkv", "2026-11-22T19:30:00+01:00")]  # subido en la ventana, sin SxxEyy
        with mock.patch.object(Watch, "now", return_value=sunday_night), \
             mock.patch.object(watches.DriveMirror, "list_dirs", return_value=["Serie"]), \
             mock.patch.object(watches.DriveMirror, "list_recent_files", return_value=files), \
             mock.patch("automator.pipeline.enqueue.enqueue_from_webhook", return_value={"status": "nothing_new"}), \
             mock.patch("automator.pipeline.watches.load_config", return_value=cfg):
            DriveWatcher().poll(cfg, self.w)
        self.assertEqual(datetime.fromtimestamp(self.w.done_until), datetime(2026, 11, 24, 17, 0))

    def test_poll_does_not_pause_if_saved_before_upload(self):
        import os
        from datetime import datetime
        from automator.pipeline import watches
        from automator.pipeline.watches import DriveWatcher, Watch
        cfg = {**self.cfg, **CFG}
        os.utime(self.folder / "03.mkv", (datetime(2026, 11, 20, 12, 0).timestamp(),) * 2)  # el de la semana pasada
        files = [f("[Grupo][Web 1080p]/Serie - 04.mkv", "2026-11-22T19:30:00+01:00")]
        with mock.patch.object(Watch, "now", return_value=datetime(2026, 11, 22, 21, 0)), \
             mock.patch.object(watches.DriveMirror, "list_dirs", return_value=["Serie"]), \
             mock.patch.object(watches.DriveMirror, "list_recent_files", return_value=files), \
             mock.patch("automator.pipeline.enqueue.enqueue_from_webhook", return_value={"status": "nothing_new"}), \
             mock.patch("automator.pipeline.watches.load_config", return_value=cfg):
            DriveWatcher().poll(cfg, self.w)
        self.assertEqual(self.w.done_until, 0.0)
