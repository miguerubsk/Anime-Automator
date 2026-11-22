"""El elemento de trabajo: un capítulo (o película) de un anime por descargar."""

import json
from dataclasses import dataclass, replace


def work_key(target: dict, episode: int) -> tuple:
    """Clave de en-curso/completado. Las películas usan una propia para no
    chocar con el capítulo 1 de la serie que vive en la misma carpeta."""
    return (target.get("key") or target["folder"], episode)


def key_id(key) -> str:
    """La clave como texto (para los JSON de estado)."""
    return json.dumps(list(key), ensure_ascii=False)


@dataclass(frozen=True)
class WorkItem:
    """
    target: dónde y cómo guardarlo (lo que antes era `anime_cfg`):
        folder, name, page_url y, según el caso, movie, filename, key,
        tried_urls, relinks, relink_pending.
    title: el watch_title (título de la ficha de japan-paw).
    """

    target: dict
    title: str
    episode: int
    url: str
    attempt: int = 1

    # ---------------- identidad ----------------
    @property
    def key(self) -> tuple:
        return work_key(self.target, self.episode)

    @property
    def group(self) -> str:
        """El anime (su carpeta) o la película (su clave propia)."""
        return self.target.get("key") or self.target["folder"]

    @property
    def folder(self) -> str:
        return self.target["folder"]

    # ---------------- presentación ----------------
    @property
    def is_movie(self) -> bool:
        return bool(self.target.get("movie"))

    @property
    def name(self) -> str:
        return self.target.get("name") or self.title

    @property
    def label(self) -> str:
        return "(película)" if self.is_movie else f"Capítulo {self.episode:02d}"

    # ---------------- variantes ----------------
    def evolve(self, url: str | None = None, attempt: int | None = None, **target_changes) -> "WorkItem":
        """Copia con otro enlace/intento y/o datos de destino cambiados."""
        changes = {}
        if url is not None:
            changes["url"] = url
        if attempt is not None:
            changes["attempt"] = attempt
        if target_changes:
            changes["target"] = {**self.target, **target_changes}
        return replace(self, **changes)

    # ---------------- persistencia ----------------
    def to_record(self, priority: int = 0, retry_at: float | None = None) -> dict:
        return {"anime_cfg": self.target, "watch_title": self.title, "episode": self.episode,
                "url": self.url, "attempt": self.attempt, "priority": priority, "retry_at": retry_at}

    @classmethod
    def from_record(cls, rec: dict) -> "WorkItem":
        return cls(rec["anime_cfg"], rec["watch_title"], rec["episode"], rec["url"], rec.get("attempt", 1))
