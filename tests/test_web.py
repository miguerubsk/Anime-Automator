"""La API HTTP (Flask test_client): rutas, códigos y JSON. El pipeline se falsea en su frontera."""

import os
from unittest import mock

from tests.helpers import AutomatorTestCase

PASTE = "https://paste.japan-paw.net/?v=1"
BAD_URL = {"status": "bad_url", "error": "se espera una URL de japan-paw.net"}


class WebApiTestCase(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        from automator.web import create_app
        self.client = create_app().test_client()

    def fake(self, name, **kwargs):
        patcher = mock.patch(f"automator.web.{name}", **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def folder(self, name="Serie"):
        return f"{self.root}/Anime/{name}"


class WebhookTest(WebApiTestCase):
    def test_enqueues_japanpaw_watches(self):
        enqueue = self.fake("enqueue_from_webhook", return_value={"status": "queued", "episode": 3})
        r = self.client.post("/webhook", json={"watch_title": "Serie", "watch_url": PASTE})
        self.assertEqual((r.status_code, r.get_json()), (200, {"status": "queued", "episode": 3}))
        self.assertEqual(enqueue.call_args.args[1:], ("Serie", PASTE))

    def test_other_watches_are_ignored(self):
        enqueue = self.fake("enqueue_from_webhook")
        r = self.client.post("/webhook", data="no es json")
        self.assertEqual((r.status_code, r.get_json()), (200, {"status": "ignored"}))
        enqueue.assert_not_called()


class DownloadTest(WebApiTestCase):
    def test_bad_url(self):
        for body in ({"url": "https://otra.web/x"}, {}, None):
            r = self.client.post("/download", json=body)
            self.assertEqual((r.status_code, r.get_json()), (400, BAD_URL))

    def test_manual_download_is_notified(self):
        result = {"status": "queued", "name": "Serie", "queued": [1], "available": 1, "have": 0,
                  "folder": self.folder()}
        dl = self.fake("download_whole_anime", return_value=result)
        notify = self.fake("notify")
        r = self.client.post("/download", json={"url": f"  {PASTE} ", "kind": "película", "title": "  ",
                                                "job_id": ""})
        self.assertEqual((r.status_code, r.get_json()["folder"]), (200, "Anime/Serie"))
        self.assertEqual(dl.call_args.args[1], PASTE)
        self.assertEqual(dl.call_args.kwargs, {"job_id": None, "kind": None, "title": None})
        self.assertIn("1 capítulo(s) encolados", notify.call_args.args[0])

    def test_requester_download_is_not_notified(self):
        dl = self.fake("download_whole_anime", return_value={"status": "error"})
        notify = self.fake("notify")
        r = self.client.post("/download", json={"url": PASTE, "kind": "movie", "title": " Peli ", "job_id": 7})
        self.assertEqual(r.get_json(), {"status": "error"})
        self.assertEqual(dl.call_args.kwargs, {"job_id": "7", "kind": "movie", "title": "Peli"})
        notify.assert_not_called()

    def test_nothing_to_say_is_not_notified(self):
        self.fake("download_whole_anime", return_value={"status": "error"})
        notify = self.fake("notify")
        self.client.post("/download", json={"url": PASTE})
        notify.assert_not_called()


class CheckTest(WebApiTestCase):
    def test_check(self):
        check = self.fake("check_library", return_value={"status": "missing", "folder": self.folder(), "have": [1]})
        r = self.client.post("/check", json={"url": PASTE, "kind": "series", "title": "Serie"})
        self.assertEqual((r.status_code, r.get_json()), (200, {"status": "missing", "folder": "Anime/Serie",
                                                               "have": [1]}))
        self.assertEqual(check.call_args.args[1], PASTE)
        self.assertEqual(check.call_args.kwargs, {"kind": "series", "title": "Serie"})

    def test_without_folder(self):
        self.fake("check_library", return_value={"status": "not_found", "folder": ""})
        self.assertEqual(self.client.post("/check", json={"url": PASTE}).get_json(),
                         {"status": "not_found", "folder": ""})

    def test_bad_url(self):
        r = self.client.post("/check", json={"url": "https://otra.web"})
        self.assertEqual((r.status_code, r.get_json()), (400, BAD_URL))


class SeasonsTest(WebApiTestCase):
    def layout(self, cfg, title):
        from automator.library.folders import SeasonLayout
        return SeasonLayout(f"Base {title}", "2", "Sub", title)

    def test_with_anilist(self):
        self.fake("season_layout", side_effect=self.layout)
        resolver = self.fake("SeasonResolver")
        resolver.return_value.series_info.side_effect = \
            lambda t: {"series": "Gran Serie", "format": "MOVIE"} if t == "Peli" else None
        r = self.client.post("/seasons", json={"titles": ["Serie", "Peli"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"seasons": [
            {"title": "Serie", "base": "Base Serie", "key": "baseserie", "season": "2", "subtitle": "Sub"},
            {"title": "Peli", "base": "Base Peli", "key": "basepeli", "season": "2", "subtitle": "Sub",
             "series": "Gran Serie", "series_key": "granserie", "format": "MOVIE"}]})

    def test_out_of_time_goes_without_anilist_and_is_warmed(self):
        layout = self.fake("season_layout")
        warm = self.fake("SeasonWarmer.warm")
        titles = [f"Oshi no Ko {i}nd Season" for i in range(2, 16)]
        data = self.client.post("/seasons", json={"titles": titles, "budget": -1}).get_json()
        self.assertEqual(len(data["seasons"]), 12)
        self.assertEqual(data["seasons"][0],
                         {"title": titles[0], "base": "Oshi no Ko", "key": "oshinoko", "season": "2", "subtitle": ""})
        layout.assert_not_called()
        self.assertEqual(warm.call_args.args[1], titles[:12])

    def test_no_titles(self):
        self.assertEqual(self.client.post("/seasons", json={}).get_json(), {"seasons": []})


class SecretTestCase(WebApiTestCase):
    def setUp(self):
        super().setUp()
        os.environ["REQUESTER_CALLBACK_SECRET"] = "s3cret"

    def call(self, method, path, body=None, secret="s3cret"):
        r = self.client.open(path, method=method, json=body, headers={"X-Callback-Secret": secret})
        return r.status_code, r.get_json()


class WatchesTest(SecretTestCase):
    def test_replace(self):
        registry = self.fake("WatchRegistry")
        registry.return_value.replace.return_value = ["a", "b"]
        self.assertEqual(self.call("PUT", "/watches", {"watches": [{"watch_url": "x"}]}), (200, {"status": "ok",
                                                                                              "count": 2}))
        registry.return_value.replace.assert_called_once_with([{"watch_url": "x"}])
        self.call("PUT", "/watches", {})
        registry.return_value.replace.assert_called_with([])

    def test_replace_invalid(self):
        registry = self.fake("WatchRegistry")
        registry.return_value.replace.side_effect = ValueError("falta watch_url")
        self.assertEqual(self.call("PUT", "/watches", {"watches": [{}]}),
                         (400, {"status": "bad_request", "error": "falta watch_url"}))

    def test_needs_secret(self):
        registry = self.fake("WatchRegistry")
        for method, path in (("PUT", "/watches"), ("POST", "/watches/poll"), ("POST", "/torrent/check"),
                             ("POST", "/torrent/download")):
            self.assertEqual(self.call(method, path, {}, secret="otro"), (401, {"status": "unauthorized"}))
        registry.assert_not_called()

    def test_list(self):
        watcher = self.fake("DriveWatcher")
        watcher.return_value.overview.return_value = [{"title": "Serie"}]
        self.assertEqual(self.call("GET", "/watches", secret=""), (200, {"watches": [{"title": "Serie"}]}))

    def test_poll(self):
        registry, watcher = self.fake("WatchRegistry"), self.fake("DriveWatcher")
        registry.return_value.get.side_effect = lambda url: "w" if url == "https://x" else None
        watcher.return_value.poll.return_value = {"status": "ok", "new": 1}
        self.assertEqual(self.call("POST", "/watches/poll", {"watch_url": " https://x "}),
                         (200, {"status": "ok", "new": 1}))
        self.assertEqual(watcher.return_value.poll.call_args.args[1], "w")
        self.assertEqual(self.call("POST", "/watches/poll", {}), (404, {"status": "unknown_watch"}))


class TorrentTest(SecretTestCase):
    def test_check(self):
        req = self.fake("TorrentRequest")
        req.return_value.plan.return_value = {"status": "missing", "folder": self.folder(), "missing": [1]}
        self.assertEqual(self.call("POST", "/torrent/check", {"anilist_id": "21", "season": 2}),
                         (200, {"status": "missing", "folder": "Anime/Serie", "missing": [1]}))
        self.assertEqual(req.call_args.args[1:], (21, 2))

    def test_download(self):
        req = self.fake("TorrentRequest")
        req.return_value.start.return_value = {"status": "queued", "queued": [1]}
        self.assertEqual(self.call("POST", "/torrent/download", {"anilist_id": 21, "job_id": 9}),
                         (200, {"status": "queued", "queued": [1]}))
        req.return_value.start.assert_called_once_with(job_id="9")
        self.assertEqual(req.call_args.args[1:], (21, None))
        self.call("POST", "/torrent/download", {"anilist_id": 21, "job_id": ""})
        req.return_value.start.assert_called_with(job_id=None)

    def test_without_anilist_id(self):
        req = self.fake("TorrentRequest")
        for path in ("/torrent/check", "/torrent/download"):
            for body in ({}, {"anilist_id": "abc"}):
                self.assertEqual(self.call("POST", path, body),
                                 (400, {"status": "bad_request", "error": "falta anilist_id"}))
        req.assert_not_called()


class HealthTest(WebApiTestCase):
    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual((r.status_code, r.get_json()), (200, {"status": "ok"}))
        self.assertEqual(self.client.post("/health").status_code, 405)


class ImmediateThread:
    def __init__(self, target, args, daemon):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class SeasonWarmerTest(AutomatorTestCase):
    def test_warms_each_title_once_and_survives_failures(self):
        from automator.web import SeasonWarmer
        warmer = SeasonWarmer()
        warmer._busy.add("Ocupado")          # otro hilo ya lo está consultando
        done = []

        def layout(cfg, title):
            done.append(title)
            if title == "Falla":
                raise RuntimeError("AniList caído")

        with mock.patch("automator.web.season_layout", side_effect=layout), \
             mock.patch("automator.web.threading.Thread", ImmediateThread), \
             self.assertLogs("anime-automator", "ERROR"):
            warmer.warm({}, ["Falla", "Ocupado", "Bien"])
        self.assertEqual(done, ["Falla", "Bien"])
        self.assertEqual(warmer._busy, {"Ocupado"})
