"""
Órdenes de texto del bot (/cola, /pausar, /descargar URL...), con el patrón Command.

Cada orden es una clase registrada por sus nombres. Para una orden nueva
basta una clase más con @command(...), sin tocar el despachador.
"""

import logging
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..core.config import load_config
from ..core.util import is_japanpaw_url
from ..pipeline.enqueue import describe_download_result, download_whole_anime
from ..pipeline.management import PauseQueue
from . import views
from .views import btn, e

log = logging.getLogger("anime-automator.admin-bot")

URL_RE = re.compile(r"https?://[^\s<>\"]+")

HELP = (
    "🤖 <b>Anime-Automator</b>\n\n"
    "• Mándame una URL de japan-paw.net (o <code>/descargar URL</code>) y encolo todo lo que falte.\n"
    "• /cola — ver y gestionar lo pendiente: eliminar, pausar y cambiar el orden de animes o "
    "capítulos sueltos.\n"
    "• /pausar y /reanudar — toda la cola (lo que se esté descargando termina igualmente).\n"
    "• /start — tu Chat ID."
)

QUEUE_BUTTON = [[btn("📋 Ver la cola", "q")]]


@dataclass
class Message:
    chat_id: str
    text: str
    authorized: bool

    @property
    def command(self) -> str:
        """"/cola@MiBot extra" -> "/cola"; "" si no empieza por /."""
        return self.text.split()[0].split("@")[0].lower() if self.text.startswith("/") else ""


COMMANDS: dict[str, "Command"] = {}


def command(*names: str):
    def register(cls):
        inst = cls()
        for n in names:
            COMMANDS[n] = inst
        return cls
    return register


class Command(ABC):
    public = False  # ¿la puede usar un chat no autorizado?

    @abstractmethod
    def run(self, msg: Message, reply):
        """`reply(texto, teclado=None)` contesta en el chat."""


@command("/start")
class StartCommand(Command):
    public = True

    def run(self, msg, reply):
        text = f"Tu Chat ID es: <code>{msg.chat_id}</code>"
        reply(text + "\n\n" + HELP if msg.authorized else text)


@command("/ayuda", "/help")
class HelpCommand(Command):
    def run(self, msg, reply):
        reply(HELP)


@command("/cola")
class QueueCommand(Command):
    def run(self, msg, reply):
        reply(*views.queue_view())


@command("/pausar", "/reanudar")
class PauseCommand(Command):
    def run(self, msg, reply):
        pause = msg.command == "/pausar"
        PauseQueue(pause).execute()
        reply("⏸ Cola pausada: no empezará ningún capítulo nuevo." if pause else "▶️ Cola reanudada.", QUEUE_BUTTON)


@command("/descargar", "/bajar")
class DownloadCommand(Command):
    """También se usa con cualquier mensaje que lleve una URL de japan-paw.net."""

    def run(self, msg, reply):
        m = URL_RE.search(msg.text)
        url = m.group(0).rstrip(".,)") if m else ""
        if not is_japanpaw_url(url):
            reply("No veo una URL válida de japan-paw.net en el mensaje. Ejemplo:\n"
                  "<code>/descargar https://paste.japan-paw.net/?v=1234</code>")
            return
        reply("📨 Mirando la página, enseguida te digo qué encolo…")
        # en su propio hilo: abrir la página puede tardar hasta ~30 s
        threading.Thread(target=self._enqueue, args=(url, reply), daemon=True).start()

    @staticmethod
    def _enqueue(url: str, reply):
        try:
            errors: list[str] = []
            result = download_whole_anime(load_config(), url, notify_fn=errors.append)
            text = describe_download_result(result) or "\n".join(errors) \
                or f"❌ No se pudo encolar ({e(str(result.get('status')))})."
            reply(text, QUEUE_BUTTON if result.get("queued") else None)
        except Exception as ex:
            log.exception("Fallo encolando %s desde el bot de administración", url)
            reply(f"❌ Error encolando: <code>{e(str(ex))}</code>")


def resolve(msg: Message) -> Command | None:
    """La orden que toca para un mensaje, o None si no hay que contestar."""
    cmd = COMMANDS.get(msg.command)
    if cmd is None and "japan-paw.net" in msg.text:
        cmd = COMMANDS["/descargar"]
    if cmd is None and msg.command:
        cmd = COMMANDS["/ayuda"]  # una orden desconocida: la ayuda
    if not msg.authorized and not (cmd is not None and cmd.public):
        log.info("Mensaje ignorado de un chat no autorizado: %s", msg.chat_id)
        return None
    return cmd
