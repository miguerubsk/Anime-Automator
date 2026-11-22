"""El bucle del bot: recibe mensajes y botones de Telegram y los despacha."""

import logging
import time

from ..core.background import BackgroundLoop
from ..core.config import env
from ..core.singleton import Singleton
from ..notify.paths import display_path
from ..notify.telegram import TelegramApi
from . import buttons, commands, views

log = logging.getLogger("anime-automator.admin-bot")

BOT_COMMANDS = [
    {"command": "cola", "description": "Ver y gestionar la cola"},
    {"command": "descargar", "description": "Encolar un anime: /descargar URL"},
    {"command": "pausar", "description": "Pausar toda la cola"},
    {"command": "reanudar", "description": "Reanudar la cola"},
    {"command": "ayuda", "description": "Qué puedo hacer"},
]


class AdminBot(BackgroundLoop, metaclass=Singleton):
    thread_name = "admin-bot"

    def __init__(self):
        super().__init__()
        self.api = TelegramApi(env("ADMIN_BOT_TOKEN"))
        self.chat_ids = {c.strip() for c in env("ADMIN_BOT_CHAT_IDS").split(",") if c.strip()}
        # Sin archivo de offset: Telegram descarta por su cuenta las
        # actualizaciones ya confirmadas, y las no confirmadas se reciben al
        # arrancar (que es justo lo que queremos tras un reinicio).
        self._offset = None
        self._commands_set = False

    # ---------------- salida ----------------
    def send(self, chat_id, text: str, keyboard=None):
        return self.api.send_message(chat_id, display_path(text), keyboard, preview=False)

    def edit(self, chat_id, message_id, text: str, keyboard=None):
        return self.api.edit_message(chat_id, message_id, display_path(text), keyboard, preview=False)

    # ---------------- entrada ----------------
    def on_message(self, raw: dict):
        chat_id = str(raw["chat"]["id"])
        text = (raw.get("text") or "").strip()
        if not text:
            return
        msg = commands.Message(chat_id, text, chat_id in self.chat_ids)
        cmd = commands.resolve(msg)
        if cmd is not None:
            cmd.run(msg, lambda t, kb=None: self.send(chat_id, t, kb))

    def on_callback(self, cq: dict):
        chat_id = str(cq["message"]["chat"]["id"])
        if chat_id not in self.chat_ids:
            self.api.answer_callback(cq["id"], "No autorizado")
            return
        data = cq.get("data", "")
        try:
            notice, view = buttons.press(data)
        except Exception:
            log.exception("Fallo procesando el botón %s", data)
            notice, view = "Error, mira el log del automatizador", None
        if view is None:  # el anime/capítulo ya no existe: volver a la lista
            view = views.queue_view()
        self.api.answer_callback(cq["id"], notice)
        self.edit(chat_id, cq["message"]["message_id"], *view)

    # ---------------- bucle ----------------
    def step(self):
        if not self._commands_set:
            self._commands_set = self.api.call("setMyCommands", commands=BOT_COMMANDS) is not None
        params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
        if self._offset is not None:
            params["offset"] = self._offset
        updates = self.api.call("getUpdates", http_timeout=40, **params)
        if updates is None:
            time.sleep(5)  # sin red, token inválido, etc.
            return
        for u in updates:
            self._offset = u["update_id"] + 1
            try:
                if "callback_query" in u:
                    self.on_callback(u["callback_query"])
                elif "message" in u:
                    self.on_message(u["message"])
            except Exception:
                log.exception("Fallo procesando la actualización %s", u.get("update_id"))

    def start(self):
        if not self.api.enabled:
            log.info("ADMIN_BOT_TOKEN vacío: bot de administración desactivado")
            return
        if not self.chat_ids:
            log.warning("ADMIN_BOT_CHAT_IDS vacío: el bot solo responderá a /start con el Chat ID")
        super().start()
        log.info("Bot de administración en marcha (%s chat(s) autorizado(s))", len(self.chat_ids))
