import os
import re
from unittest import mock

from tests.helpers import AutomatorTestCase


def plain(text):
    return re.sub(r"<[^>]+>", "", text)


class AdminBotTest(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        os.environ["ADMIN_BOT_CHAT_IDS"] = "100"
        from automator.pipeline.pending import PendingStore
        from automator.pipeline.work_queue import WorkQueue
        PendingStore()
        for ep in (1, 2, 3):
            WorkQueue().put(self.item("Uno", ep), priority=1)
        WorkQueue().put(self.item("Dos", 1), priority=1)

    def bot(self):
        from automator.admin_bot.bot import AdminBot
        bot = AdminBot()
        bot.replies = []
        bot.send = lambda chat, text, kb=None: bot.replies.append((chat, text, kb))
        bot.edit = lambda chat, mid, text, kb=None: bot.replies.append((chat, text, kb))
        bot.api = mock.Mock()
        return bot

    def callback(self, bot, data):
        bot.on_callback({"id": "cb", "data": data, "message": {"chat": {"id": 100}, "message_id": 5}})
        return bot.api.answer_callback.call_args[0][1], bot.replies[-1]

    def test_queue_command_and_buttons(self):
        from automator.admin_bot.views import gid
        from automator.pipeline.work_queue import WorkQueue
        bot = self.bot()
        bot.on_message({"chat": {"id": 100}, "text": "/cola"})
        _, text, kb = bot.replies[-1]
        self.assertIn("4 capítulo(s) pendientes en 2 anime(s)", plain(text))
        self.assertEqual(kb[0][0]["callback_data"], f"g:{gid(self.item('Uno').group)}")

        h = gid(self.item("Dos").group)
        notice, _ = self.callback(bot, f"ga:{h}:front")
        self.assertEqual(notice, "Movido")
        self.assertEqual(WorkQueue().view()[0][0][0].name, "Dos")

        notice, (_, text, _) = self.callback(bot, f"ea:{gid(self.item('Uno').group)}:2:pause")
        self.assertEqual(notice, "Capítulo en pausa")
        self.assertIn("en pausa", plain(text))

        notice, (_, text, _) = self.callback(bot, f"ga:{h}:delok")
        self.assertEqual(notice, "Eliminados 1 capítulo(s)")
        self.assertIn("3 capítulo(s) pendientes en 1 anime(s)", plain(text))

    def test_unauthorized_chat_only_gets_chat_id(self):
        bot = self.bot()
        bot.on_message({"chat": {"id": 999}, "text": "/cola"})
        bot.on_message({"chat": {"id": 999}, "text": "/start"})
        self.assertEqual(len(bot.replies), 1)
        self.assertEqual(plain(bot.replies[0][1]), "Tu Chat ID es: 999")
        bot.on_callback({"id": "cb", "data": "q", "message": {"chat": {"id": 999}, "message_id": 1}})
        bot.api.answer_callback.assert_called_with("cb", "No autorizado")

    def test_pause_and_bad_url(self):
        from automator.pipeline.work_queue import WorkQueue
        bot = self.bot()
        bot.on_message({"chat": {"id": 100}, "text": "/pausar"})
        self.assertTrue(WorkQueue().view()[2])
        bot.on_message({"chat": {"id": 100}, "text": "/descargar https://otra.web/x"})
        self.assertIn("No veo una URL válida", bot.replies[-1][1])
        bot.on_message({"chat": {"id": 100}, "text": "/loquesea"})
        self.assertIn("Anime-Automator", bot.replies[-1][1])


class WebTest(AutomatorTestCase):
    def client(self):
        from automator.web import create_app
        return create_app().test_client()

    def test_health_and_validation(self):
        c = self.client()
        self.assertEqual(c.get("/health").get_json(), {"status": "ok"})
        self.assertEqual(c.post("/download", json={"url": "https://otra.web/x"}).status_code, 400)
        self.assertEqual(c.post("/check", json={}).status_code, 400)
        self.assertEqual(c.post("/webhook", json={"watch_url": "https://otra.web"}).get_json(), {"status": "ignored"})

    def test_check_shows_user_paths(self):
        result = {"status": "missing", "folder": f"{self.root}/Anime/Serie", "missing": [1]}
        with mock.patch("automator.web.check_library", return_value=result):
            data = self.client().post("/check", json={"url": "https://paste.japan-paw.net/?v=1"}).get_json()
        self.assertEqual(data["folder"], "Anime/Serie")

    def test_seasons_endpoint(self):
        data = self.client().post("/seasons", json={"titles": ["Oshi no Ko 2nd Season"]}).get_json()
        self.assertEqual(data["seasons"][0]["season"], "2")
        self.assertEqual(data["seasons"][0]["base"], "Oshi no Ko")
