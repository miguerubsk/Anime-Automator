"""Telegram: cliente de la API (común) y el notificador del chat de avisos."""

import logging
import time

import requests

from ..core.config import load_config
from ..core.singleton import Singleton
from .paths import display_path

log = logging.getLogger("anime-automator")

MAX_MESSAGE_LEN = 4096


class TelegramApi:
    """
    Llamadas a la Bot API. Lo usan tanto el notificador como el bot de
    administración (antes cada uno tenía su propio código para lo mismo).
    Nunca lanza: devuelve el `result` de Telegram o None si falla.
    """

    BASE_URL = "https://api.telegram.org"

    def __init__(self, token: str):
        self.token = (token or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def call(self, method: str, http_timeout: float = 20, **params):
        """`params` son los de Telegram (ojo: getUpdates tiene su propio "timeout")."""
        try:
            r = requests.post(f"{self.BASE_URL}/bot{self.token}/{method}", json=params, timeout=http_timeout)
            data = r.json()
        except Exception as ex:
            # sin el token en el log: requests lo incluiría en la URL
            log.warning("Telegram %s falló: %s", method, type(ex).__name__)
            return None
        if not data.get("ok"):
            desc = data.get("description", "")
            if "message is not modified" not in desc:
                log.warning("Telegram %s respondió: %s", method, desc)
            return None
        return data.get("result")

    @staticmethod
    def _message_params(text: str, keyboard=None, preview: bool | None = None) -> dict:
        if len(text) > MAX_MESSAGE_LEN:
            text = text[:MAX_MESSAGE_LEN - 6] + "\n[…]"
        params = {"text": text, "parse_mode": "HTML"}
        if preview is False:
            params["disable_web_page_preview"] = True
        if keyboard is not None:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        return params

    def send_message(self, chat_id, text: str, keyboard=None, preview: bool | None = None):
        return self.call("sendMessage", chat_id=chat_id, **self._message_params(text, keyboard, preview))

    def edit_message(self, chat_id, message_id, text: str, keyboard=None, preview: bool | None = None):
        return self.call("editMessageText", chat_id=chat_id, message_id=message_id,
                         **self._message_params(text, keyboard or [], preview))

    def answer_callback(self, callback_id, text: str = ""):
        return self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200])


class Notifier(metaclass=Singleton):
    """Avisos al chat de notificaciones (telegram.bot_token / chat_id de config.yaml)."""

    max_retries = 3

    def send(self, text: str, cfg: dict | None = None):
        cfg = cfg or load_config()
        text = display_path(text, cfg)
        tcfg = cfg.get("telegram") or {}
        api, chat_id = TelegramApi(tcfg.get("bot_token")), tcfg.get("chat_id")
        if not api.enabled or not chat_id:
            log.warning("Telegram no configurado, omito notificación: %s", text)
            return

        for attempt in range(1, self.max_retries + 1):
            if api.send_message(chat_id, text) is not None:
                return
            log.warning("Fallo enviando notificación de Telegram (intento %s/%s)", attempt, self.max_retries)
            if attempt < self.max_retries:
                time.sleep(2 * attempt)

        # se han agotado los reintentos: lo dejamos bien visible en el log,
        # porque es la única forma de enterarse si un mensaje se pierde de verdad
        log.error("No se pudo enviar la notificación de Telegram tras %s intentos. Mensaje perdido:\n%s",
                  self.max_retries, text)


def notify(text: str, cfg: dict | None = None):
    Notifier().send(text, cfg)
