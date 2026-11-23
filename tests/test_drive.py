from unittest import mock

import requests

from tests.helpers import AutomatorTestCase

ANIME_URL = ("https://anime.craftervault.com/0:down/B/Berserk/%5BAnimeRG%5D%20Berserk%20(2016)%20"
             "%5BWeb%20HEVC%201080p%5D/Berserk%20(2016)%20-%2001.mkv")
EMISION_URL = "https://emision.craftervault.com/0:down/Shinja Zero/[ToonsHub][Web 1080p]/S01E07.mkv"
DRIVE_CFG = {"drive_mirror": {"enabled": True, "hosts": {
    "anime.craftervault.com": "japanpaw:Anime", "emision.craftervault.com": "japanpaw-emision:"}}}


def quota_error():
    resp = requests.Response()
    resp.status_code = 403
    return requests.exceptions.HTTPError("403 Client Error: Forbidden", response=resp)


class DriveMirrorTest(AutomatorTestCase):
    def test_maps_index_links_to_drive_paths(self):
        from automator.sources.drive import DriveMirror
        m = DriveMirror(DRIVE_CFG)
        self.assertEqual(m.remote_path(ANIME_URL),
                         "japanpaw:Anime/B/Berserk/[AnimeRG] Berserk (2016) [Web HEVC 1080p]/Berserk (2016) - 01.mkv")
        self.assertEqual(m.remote_path(EMISION_URL),
                         "japanpaw-emision:Shinja Zero/[ToonsHub][Web 1080p]/S01E07.mkv")

    def test_ignores_unknown_or_unsafe_links(self):
        from automator.sources.drive import DriveMirror
        m = DriveMirror(DRIVE_CFG)
        self.assertIsNone(m.remote_path("https://otro.example.com/0:down/a.mkv"))
        self.assertIsNone(m.remote_path("https://anime.craftervault.com/otra/ruta.mkv"))
        self.assertIsNone(m.remote_path("https://anime.craftervault.com/0:down/../x.mkv"))
        self.assertIsNone(DriveMirror({}).remote_path(ANIME_URL))


PREFER_CFG = {"drive_mirror": {**DRIVE_CFG["drive_mirror"], "prefer": True}}


class SourceOrderTest(AutomatorTestCase):
    def ctx(self, item, exc, cfg=DRIVE_CFG):
        from automator.pipeline.failures import FailureContext
        return FailureContext({**self.cfg, **cfg}, item, exc)

    def cfg_with(self, cfg):
        return {**self.cfg, **cfg}

    def test_default_source(self):
        from automator.pipeline.processing import DriveSource, HttpSource, source_for
        item = self.item("Berserk", 1).evolve(url=ANIME_URL)
        self.assertIsInstance(source_for(self.cfg_with(DRIVE_CFG), item), HttpSource)
        self.assertIsInstance(source_for(self.cfg_with(PREFER_CFG), item), DriveSource)
        # un enlace que no está en el Drive va siempre por el paste
        self.assertIsInstance(source_for(self.cfg_with(PREFER_CFG), self.item("Otro", 1)), HttpSource)
        # estado guardado por la versión anterior
        legacy = item.evolve(via_drive=True)
        self.assertIsInstance(source_for(self.cfg_with(DRIVE_CFG), legacy), DriveSource)

    def test_failed_link_goes_to_drive(self):
        from automator.pipeline.failures import HELD, handle_failure
        from automator.pipeline.processing import DriveSource, source_for
        from automator.pipeline.work_queue import WorkQueue
        item = self.item("Berserk", 1).evolve(url=ANIME_URL)
        with mock.patch.object(WorkQueue, "put") as put:
            self.assertEqual(handle_failure(self.ctx(item, quota_error())), HELD)
        requeued = put.call_args.args[0]
        self.assertEqual(requeued.target["source"], "drive")
        self.assertIsInstance(source_for(self.cfg_with(DRIVE_CFG), requeued), DriveSource)

    def test_preferred_drive_falls_back_to_link(self):
        from automator.pipeline.failures import HELD, handle_failure
        from automator.pipeline.processing import HttpSource, source_for
        from automator.pipeline.work_queue import WorkQueue
        from automator.sources.drive import DriveError
        item = self.item("Berserk", 1).evolve(url=ANIME_URL)
        with mock.patch.object(WorkQueue, "put") as put:
            exc = DriveError("downloadQuotaExceeded")
            self.assertEqual(handle_failure(self.ctx(item, exc, PREFER_CFG)), HELD)
        requeued = put.call_args.args[0]
        self.assertEqual((requeued.target["source"], requeued.target["tried_sources"]), ("paste", ["drive"]))
        self.assertIsInstance(source_for(self.cfg_with(PREFER_CFG), requeued), HttpSource)

    def test_both_failed_retries_later_from_preferred(self):
        from automator.pipeline.failures import HELD, handle_failure
        from automator.pipeline.processing import DriveSource, source_for
        from automator.pipeline.retries import RetryScheduler
        item = self.item("Berserk", 1).evolve(url=ANIME_URL, source="paste", tried_sources=["drive"])
        with mock.patch.object(RetryScheduler, "schedule") as schedule:
            self.assertEqual(handle_failure(self.ctx(item, quota_error(), PREFER_CFG)), HELD)
        later, _hours, attempt = schedule.call_args.args
        self.assertEqual((later.target["source"], later.target["tried_sources"], attempt), (None, [], 2))
        self.assertIsInstance(source_for(self.cfg_with(PREFER_CFG), later), DriveSource)

    def test_drive_last_failure_retries_later(self):
        from automator.pipeline.failures import HELD, handle_failure
        from automator.pipeline.processing import HttpSource, source_for
        from automator.pipeline.retries import RetryScheduler
        from automator.sources.drive import DriveError
        item = self.item("Berserk", 1).evolve(url=ANIME_URL, source="drive", tried_sources=["paste"])
        with mock.patch.object(RetryScheduler, "schedule") as schedule:
            self.assertEqual(handle_failure(self.ctx(item, DriveError("rclone falló (1)"))), HELD)
        later, _hours, attempt = schedule.call_args.args
        self.assertIsInstance(source_for(self.cfg_with(DRIVE_CFG), later), HttpSource)
        self.assertEqual(attempt, 2)

    def test_content_error_does_not_switch_source(self):
        from automator.core.errors import SourceContentError
        from automator.pipeline.failures import DriveMirrorHandler
        item = self.item("Berserk", 1).evolve(url=ANIME_URL, source="drive")
        ctx = self.ctx(item, SourceContentError("mkvmerge falló"))
        self.assertIsNone(DriveMirrorHandler().handle(ctx))  # sigue la cadena normal
        self.assertIsNone(ctx.item.target["source"])
