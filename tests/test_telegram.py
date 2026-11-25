"""Telegram: la Bot API se falsea en la frontera (requests.post)."""

from unittest import mock

import requests

from tests.helpers import AutomatorTestCase


def reply(data):
    resp = mock.Mock()
    resp.json.return_value = data
    return resp


class TelegramApiTest(AutomatorTestCase):
    def api(self, token=" T0K "):
        from automator.notify.telegram import TelegramApi
        return TelegramApi(token)

    def test_enabled_only_with_token(self):
        self.assertTrue(self.api().enabled)
        self.assertFalse(self.api(None).enabled)
        self.assertFalse(self.api("  ").enabled)

    def test_call_returns_result(self):
        with mock.patch("automator.notify.telegram.requests.post",
                        return_value=reply({"ok": True, "result": [1, 2]})) as post:
            self.assertEqual(self.api().call("getUpdates", http_timeout=40, timeout=30), [1, 2])
        post.assert_called_once_with("https://api.telegram.org/botT0K/getUpdates", json={"timeout": 30}, timeout=40)

    def test_call_failures_give_none_without_token_in_log(self):
        cases = [requests.exceptions.ConnectionError("https://api.telegram.org/botT0K/x"),
                 reply({"ok": False, "description": "Bad Request: chat not found"}),
                 reply({"ok": False, "description": "Bad Request: message is not modified"})]
        logged = []
        for answer in cases:
            post = mock.Mock(side_effect=answer) if isinstance(answer, Exception) else mock.Mock(return_value=answer)
            with mock.patch("automator.notify.telegram.requests.post", post), \
                 mock.patch("automator.notify.telegram.log") as log:
                self.assertIsNone(self.api().call("sendMessage", text="x"))
            logged.append([c.args for c in log.warning.call_args_list])
        self.assertEqual(logged[0], [("Telegram %s falló: %s", "sendMessage", "ConnectionError")])
        self.assertEqual(logged[1], [("Telegram %s respondió: %s", "sendMessage", "Bad Request: chat not found")])
        self.assertEqual(logged[2], [])

    def posted(self, method, *args, **kwargs):
        with mock.patch("automator.notify.telegram.requests.post",
                        return_value=reply({"ok": True, "result": {"message_id": 9}})) as post:
            result = getattr(self.api(), method)(*args, **kwargs)
        self.assertEqual(result, {"message_id": 9})
        return post.call_args.args[0].rsplit("/", 1)[1], post.call_args.kwargs["json"]

    def test_send_message(self):
        self.assertEqual(self.posted("send_message", 5, "<b>hola</b>"),
                         ("sendMessage", {"chat_id": 5, "text": "<b>hola</b>", "parse_mode": "HTML"}))
        kb = [[{"text": "a", "callback_data": "q"}]]
        self.assertEqual(self.posted("send_message", 5, "x", kb, preview=False)[1],
                         {"chat_id": 5, "text": "x", "parse_mode": "HTML", "disable_web_page_preview": True,
                          "reply_markup": {"inline_keyboard": kb}})
        self.assertNotIn("disable_web_page_preview", self.posted("send_message", 5, "x", preview=True)[1])

    def test_long_messages_are_cut(self):
        _, params = self.posted("send_message", 5, "a" * 5000)
        self.assertEqual(params["text"], "a" * 4090 + "\n[…]")
        _, params = self.posted("send_message", 5, "a" * 4096)
        self.assertEqual(params["text"], "a" * 4096)

    def test_edit_message_always_has_keyboard(self):
        self.assertEqual(self.posted("edit_message", 5, 7, "x"),
                         ("editMessageText", {"chat_id": 5, "message_id": 7, "text": "x", "parse_mode": "HTML",
                                              "reply_markup": {"inline_keyboard": []}}))

    def test_answer_callback_is_short(self):
        self.assertEqual(self.posted("answer_callback", "cb", "n" * 300),
                         ("answerCallbackQuery", {"callback_query_id": "cb", "text": "n" * 200}))


class NotifierTest(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        notifier_patch = self._patches.pop(1)   # aquí se prueba el Notifier de verdad
        notifier_patch.stop()
        self.cfg_tg = {**self.cfg, "telegram": {"bot_token": "T", "chat_id": "42"}}

    def notify(self, cfg, *answers):
        from automator.notify.telegram import notify
        with mock.patch("automator.notify.telegram.requests.post", side_effect=list(answers)) as post, \
             mock.patch("automator.notify.telegram.time.sleep") as sleep:
            notify(f"Listo en {self.root}/Anime/Serie", cfg)
        return post, sleep

    def test_sends_with_user_paths(self):
        post, sleep = self.notify(self.cfg_tg, reply({"ok": True, "result": {}}))
        self.assertEqual(post.call_args.kwargs["json"],
                         {"chat_id": "42", "text": "Listo en Anime/Serie", "parse_mode": "HTML"})
        sleep.assert_not_called()

    def test_retries_then_gives_up(self):
        bad = reply({"ok": False, "description": "Too Many Requests"})
        with self.assertLogs("anime-automator", "ERROR") as logs:
            post, sleep = self.notify(self.cfg_tg, bad, bad, bad)
        self.assertEqual(post.call_count, 3)
        self.assertEqual([c.args for c in sleep.call_args_list], [(2,), (4,)])
        self.assertIn("Mensaje perdido", logs.output[0])

    def test_second_attempt_works(self):
        post, sleep = self.notify(self.cfg_tg, reply({"ok": False}), reply({"ok": True, "result": {}}))
        self.assertEqual(post.call_count, 2)
        self.assertEqual([c.args for c in sleep.call_args_list], [(2,)])

    def test_not_configured(self):
        post, _ = self.notify(self.cfg)
        post.assert_not_called()
        post, _ = self.notify({**self.cfg, "telegram": {"bot_token": "T"}})
        post.assert_not_called()
