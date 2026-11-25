import os
import re
from unittest import mock

from tests.helpers import AutomatorTestCase


def plain(text):
    return re.sub(r"<[^>]+>", "", text)


class QueuedTestCase(AutomatorTestCase):
    """Chat 100 autorizado y en la cola: «Uno» 1-3 y «Dos» 1."""

    def setUp(self):
        super().setUp()
        os.environ["ADMIN_BOT_CHAT_IDS"] = "100"
        from automator.pipeline.pending import PendingStore
        from automator.pipeline.work_queue import WorkQueue
        PendingStore()
        for ep in (1, 2, 3):
            WorkQueue().put(self.item("Uno", ep), priority=1)
        WorkQueue().put(self.item("Dos", 1), priority=1)


class AdminBotTest(QueuedTestCase):
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


class AdminBotLoopTest(QueuedTestCase):
    """El bucle contra la Bot API falseada en la frontera (requests.post)."""

    def run_steps(self, answers, steps=1):
        """answers: {método: [respuestas sucesivas]}; devuelve [(método, json, timeout)] de lo que se llamó."""
        from automator.admin_bot.bot import AdminBot
        calls = []

        def fake_post(url, json=None, timeout=None):
            method = url.rsplit("/", 1)[1]
            calls.append((method, json, timeout))
            queue = answers.get(method) or [{"ok": True, "result": True}]
            data = queue.pop(0) if len(queue) > 1 else queue[0]
            resp = mock.Mock()
            resp.json.return_value = data
            return resp

        os.environ["ADMIN_BOT_TOKEN"] = "T"
        bot = AdminBot()
        with mock.patch("automator.notify.telegram.requests.post", fake_post), \
             mock.patch("automator.admin_bot.bot.time.sleep") as sleep:
            for _ in range(steps):
                bot.step()
        return calls, sleep

    def test_receives_and_answers(self):
        updates = [{"update_id": 7, "message": {"chat": {"id": 100}, "text": "/cola"}},
                   {"update_id": 8, "edited_message": {}},
                   {"update_id": 9, "callback_query": {"id": "cb", "data": "q",
                                                       "message": {"chat": {"id": 100}, "message_id": 5}}}]
        calls, sleep = self.run_steps({"getUpdates": [{"ok": True, "result": updates},
                                                      {"ok": True, "result": []}]}, steps=2)
        self.assertEqual([m for m, _, _ in calls], ["setMyCommands", "getUpdates", "sendMessage",
                                                    "answerCallbackQuery", "editMessageText", "getUpdates"])
        self.assertEqual([c["command"] for c in calls[0][1]["commands"]],
                         ["cola", "descargar", "pausar", "reanudar", "ayuda"])
        self.assertEqual(calls[1][1:], ({"timeout": 30, "allowed_updates": ["message", "callback_query"]}, 40))
        self.assertIn("4 capítulo(s) pendientes", plain(calls[2][1]["text"]))
        self.assertEqual(calls[5][1]["offset"], 10)
        sleep.assert_not_called()

    def test_commands_retried_until_set(self):
        calls, _ = self.run_steps({"setMyCommands": [{"ok": False}, {"ok": True, "result": True}],
                                   "getUpdates": [{"ok": True, "result": []}]}, steps=3)
        self.assertEqual([m for m, _, _ in calls].count("setMyCommands"), 2)

    def test_bad_update_does_not_stop_the_rest(self):
        updates = [{"update_id": 1, "message": {"text": "sin chat"}},
                   {"update_id": 2, "message": {"chat": {"id": 100}, "text": "/ayuda"}}]
        with self.assertLogs("anime-automator.admin-bot", "ERROR"):
            calls, _ = self.run_steps({"getUpdates": [{"ok": True, "result": updates}]})
        self.assertIn("Anime-Automator", calls[-1][1]["text"])

    def test_without_network_waits(self):
        calls, sleep = self.run_steps({"getUpdates": [{"ok": False, "description": "Unauthorized"}]})
        sleep.assert_called_once_with(5)
        self.assertEqual([m for m, _, _ in calls], ["setMyCommands", "getUpdates"])


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
