"""
Gestión de la cola desde fuera (bot de administración).

- queue_overview(): foto de todo lo pendiente, agrupado por anime.
- Órdenes (patrón Command): cada acción sobre la cola es un objeto con
  execute() -> texto para el usuario. Quien las lanza (el bot) no necesita
  saber cómo se hace cada una, y una acción nueva es una clase nueva.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .jobs import JobTracker
from .model import WorkItem
from .pending import PendingStore
from .progress import BatchProgress
from .registry import InProgress
from .retries import RetryScheduler
from .torrents import TorrentTracker
from .work_queue import WorkQueue

log = logging.getLogger("anime-automator")


# ------------------------------------------------------------------ #
# Foto de la cola
# ------------------------------------------------------------------ #
@dataclass
class EpisodeView:
    key: tuple
    ep: int
    paused: bool
    state: str               # "queued" | "retry"
    retry_at: float | None = None


@dataclass
class GroupView:
    group: str
    name: str
    paused: bool
    episodes: list[EpisodeView] = field(default_factory=list)

    def of_state(self, state: str) -> list[EpisodeView]:
        return [x for x in self.episodes if x.state == state]

    def find(self, ep: int) -> EpisodeView | None:
        return next((x for x in self.episodes if x.ep == ep), None)


@dataclass
class TorrentView:
    name: str
    ep: int
    nyaa_title: str
    processed: bool


@dataclass
class QueueOverview:
    current: WorkItem | None
    all_paused: bool
    groups: list[GroupView]
    torrents: list[TorrentView]

    @property
    def episode_count(self) -> int:
        return sum(len(g.episodes) for g in self.groups)


def queue_overview() -> QueueOverview:
    """Todo lo pendiente, por anime en el orden de la cola; los reintentos, al final de su anime."""
    items, paused_groups, all_paused, current = WorkQueue().view()
    groups: list[GroupView] = []
    by_group: dict[str, GroupView] = {}

    def group_of(item: WorkItem) -> GroupView:
        if item.group not in by_group:
            by_group[item.group] = GroupView(item.group, item.name, item.group in paused_groups)
            groups.append(by_group[item.group])
        return by_group[item.group]

    for item, _prio, paused in items:
        group_of(item).episodes.append(EpisodeView(item.key, item.episode, paused, "queued"))
    for e in sorted(RetryScheduler().entries(), key=lambda e: e.retry_at):
        group_of(e.item).episodes.append(EpisodeView(e.item.key, e.item.episode, False, "retry", e.retry_at))
    torrents = [TorrentView(t.item.name, t.item.episode, t.nyaa_title, t.processed)
                for _, t in TorrentTracker().snapshot()]
    return QueueOverview(current, all_paused, groups, torrents)


def cancel_episodes(keys) -> int:
    """Quita de la cola (y de los reintentos programados) esos capítulos.
    Si pertenecían a una petición de requester-bot, se le avisa en un solo
    mensaje por petición (y si con eso se cierra, en su resumen)."""
    keys = set(keys)
    cancelled = WorkQueue().remove(keys) + RetryScheduler().remove(keys)
    for item in cancelled:
        InProgress().discard(item.key)
        PendingStore().remove(item)
        BatchProgress().cancelled(item.folder, item.episode)
    JobTracker().cancel([item.key for item in cancelled])
    PendingStore().persist()
    if cancelled:
        log.info("Cancelados desde el bot de administración: %s", [(i.name, i.episode) for i in cancelled])
    return len(cancelled)


def retry_now(key) -> bool:
    """Un capítulo que espera reintento pasa ya a la cola, el primero."""
    item = RetryScheduler().take(key)
    if item is None:
        return False
    WorkQueue().put(item, priority=0, at="front")
    return True


# ------------------------------------------------------------------ #
# Órdenes
# ------------------------------------------------------------------ #
class QueueCommand(ABC):
    @abstractmethod
    def execute(self) -> str:
        """Hace la acción y devuelve un texto corto para el usuario."""


class PauseQueue(QueueCommand):
    def __init__(self, paused: bool):
        self.paused = paused

    def execute(self):
        WorkQueue().set_all_paused(self.paused)
        return "Cola pausada" if self.paused else "Cola reanudada"


class MoveGroup(QueueCommand):
    def __init__(self, group: GroupView, how: str):
        self.group, self.how = group, how

    def execute(self):
        return "Movido" if WorkQueue().move_group(self.group.group, self.how) else "Solo tiene reintentos pendientes"


class PauseGroup(QueueCommand):
    def __init__(self, group: GroupView, paused: bool):
        self.group, self.paused = group, paused

    def execute(self):
        WorkQueue().set_group_paused(self.group.group, self.paused)
        return "Anime en pausa" if self.paused else "Anime reanudado"


class DeleteGroup(QueueCommand):
    def __init__(self, group: GroupView):
        self.group = group

    def execute(self):
        return f"Eliminados {cancel_episodes([x.key for x in self.group.episodes])} capítulo(s)"


class MoveEpisode(QueueCommand):
    def __init__(self, episode: EpisodeView, how: str):
        self.episode, self.how = episode, how

    def execute(self):
        return "Movido" if WorkQueue().move(self.episode.key, self.how) else "No se puede mover más"


class PauseEpisode(QueueCommand):
    def __init__(self, episode: EpisodeView, paused: bool):
        self.episode, self.paused = episode, paused

    def execute(self):
        WorkQueue().set_paused(self.episode.key, self.paused)
        return "Capítulo en pausa" if self.paused else "Capítulo reanudado"


class DeleteEpisode(QueueCommand):
    def __init__(self, episode: EpisodeView):
        self.episode = episode

    def execute(self):
        cancel_episodes([self.episode.key])
        return "Capítulo eliminado"


class RetryEpisodeNow(QueueCommand):
    def __init__(self, episode: EpisodeView):
        self.episode = episode

    def execute(self):
        return "Pasa a ser el siguiente" if retry_now(self.episode.key) else "Ya no estaba esperando reintento"
