"""A quién se avisa de lo que pasa con un capítulo."""

from collections.abc import Callable

from ..notify.telegram import notify
from .jobs import JobTracker


def report(key: tuple, event: str, message: str | Callable[[], str], notify_if: bool = True, **data) -> bool:
    """
    Si el capítulo es de una petición de requester-bot, el evento va a esa
    petición (y de ahí, a quien lo pidió). Si no, `message` va al Telegram
    propio (solo si `notify_if`). `message` puede ser una función para no
    calcular el texto (p.ej. el resumen de progreso, que tiene efectos) si
    al final no se envía. Devuelve True si el capítulo tenía seguimiento.
    """
    if JobTracker().event(key, event, **data):
        return True
    if notify_if:
        notify(message() if callable(message) else message)
    return False
