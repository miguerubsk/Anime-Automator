"""
Botones del bot (callback_data), con el patrón Command.

Cada tipo de botón es una clase con su prefijo ("g", "ga", "e"...) que
interpreta su callback_data y devuelve (aviso emergente, pantalla siguiente).
Las acciones sobre la cola son órdenes de pipeline.management
(MoveGroup, PauseEpisode...). Para un botón nuevo: una clase más con
@button("prefijo"), sin tocar el despachador.
"""

from abc import ABC, abstractmethod

from ..pipeline import management as m
from . import views

BUTTONS: dict[str, "Button"] = {}


def button(prefix: str):
    def register(cls):
        BUTTONS[prefix] = cls()
        return cls
    return register


class Button(ABC):
    @abstractmethod
    def press(self, args: list[str]) -> tuple[str, views.View | None]:
        """(aviso, pantalla) — pantalla None = volver a la lista de la cola."""


@button("q")
class ShowQueue(Button):
    def press(self, args):
        return "", views.queue_view()


@button("qa")
class QueueAction(Button):
    def press(self, args):
        return m.PauseQueue(args[0] == "pause").execute(), views.queue_view()


@button("g")
class ShowGroup(Button):
    def press(self, args):
        return "", views.group_view(args[0])


@button("ga")
class GroupAction(Button):
    def press(self, args):
        h, action = args[0], args[1]
        if action == "del":  # primero, confirmar
            return "", views.group_view(h, confirm_delete=True)
        g = views.find_group(m.queue_overview(), h)
        if g is None:
            return "Ese anime ya no está en la cola", None
        command = {
            "front": lambda: m.MoveGroup(g, "front"), "up": lambda: m.MoveGroup(g, "up"),
            "down": lambda: m.MoveGroup(g, "down"), "end": lambda: m.MoveGroup(g, "end"),
            "pause": lambda: m.PauseGroup(g, True), "resume": lambda: m.PauseGroup(g, False),
            "delok": lambda: m.DeleteGroup(g),
        }.get(action)
        notice = command().execute() if command else ""
        return notice, (views.group_view(h) if action != "delok" else None)


@button("e")
class ShowEpisode(Button):
    def press(self, args):
        return "", views.episode_view(args[0], int(args[1]))


@button("ea")
class EpisodeAction(Button):
    def press(self, args):
        h, ep, action = args[0], int(args[1]), args[2]
        g = views.find_group(m.queue_overview(), h)
        x = g.find(ep) if g else None
        if x is None:
            notice = "Ese capítulo ya no está en la cola"
        else:
            command = {
                "front": lambda: m.MoveEpisode(x, "front"), "up": lambda: m.MoveEpisode(x, "up"),
                "down": lambda: m.MoveEpisode(x, "down"),
                "pause": lambda: m.PauseEpisode(x, True), "resume": lambda: m.PauseEpisode(x, False),
                "now": lambda: m.RetryEpisodeNow(x), "del": lambda: m.DeleteEpisode(x),
            }.get(action)
            notice = command().execute() if command else ""
        view = views.episode_view(h, ep) if action not in ("del", "now") else views.group_view(h)
        return notice, view


def press(data: str) -> tuple[str, views.View | None]:
    prefix, *args = data.split(":")
    handler = BUTTONS.get(prefix)
    if handler is None:
        return "", None
    return handler.press(args)
