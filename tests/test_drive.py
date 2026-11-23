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


class DriveFallbackTest(AutomatorTestCase):
    def ctx(self, item, exc):
        from automator.pipeline.failures import FailureContext
        return FailureContext({**self.cfg, **DRIVE_CFG}, item, exc)

    def test_failed_link_goes_to_drive(self):
        from automator.pipeline.failures import HELD, handle_failure
        from automator.pipeline.processing import DriveSource, source_for
        from automator.pipeline.work_queue import WorkQueue
        item = self.item("Berserk", 1).evolve(url=ANIME_URL)
        with mock.patch.object(WorkQueue, "put") as put:
            self.assertEqual(handle_failure(self.ctx(item, quota_error())), HELD)
        requeued = put.call_args.args[0]
        self.assertTrue(requeued.target["via_drive"])
        self.assertIsInstance(source_for(requeued), DriveSource)

    def test_drive_failure_retries_later_from_link(self):
        from automator.pipeline.failures import HELD, handle_failure
        from automator.pipeline.processing import HttpSource, source_for
        from automator.pipeline.retries import RetryScheduler
        from automator.sources.drive import DriveError
        item = self.item("Berserk", 1).evolve(url=ANIME_URL, via_drive=True)
        with mock.patch.object(RetryScheduler, "schedule") as schedule:
            self.assertEqual(handle_failure(self.ctx(item, DriveError("rclone falló (1)"))), HELD)
        later, _hours, attempt = schedule.call_args.args
        self.assertFalse(later.target["via_drive"])
        self.assertIsInstance(source_for(later), HttpSource)
        self.assertEqual(attempt, 2)

    def test_content_error_from_drive_is_not_retried_on_drive(self):
        from automator.core.errors import SourceContentError
        from automator.pipeline.failures import DriveMirrorHandler
        item = self.item("Berserk", 1).evolve(url=ANIME_URL, via_drive=True)
        ctx = self.ctx(item, SourceContentError("sin subtítulos"))
        self.assertIsNone(DriveMirrorHandler().handle(ctx))  # sigue la cadena normal
        self.assertFalse(ctx.item.target["via_drive"])
