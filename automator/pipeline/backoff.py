"""Cuánto esperar antes de reintentar (patrón estrategia, una por tipo de error)."""

import random

from ..core.errors import http_status


class BackoffPolicy:
    """
    Espera exponencial con tope y dispersión (±jitter): así los capítulos no
    reintentan todos a la vez y, entre todos, se "sondea" el servidor más a
    menudo. Cada subclase solo decide la base y el tope.
    """

    def __init__(self, rcfg: dict):
        self.rcfg = rcfg

    def base_seconds(self) -> float:
        return self.rcfg.get("initial_delay_minutes", 5) * 60

    def max_seconds(self) -> float:
        return self.rcfg.get("max_delay_hours", 6) * 3600

    def delay(self, attempt: int) -> float:
        multiplier = self.rcfg.get("backoff_multiplier", 2)
        delay = min(self.base_seconds() * (multiplier ** (attempt - 1)), self.max_seconds())
        jitter = float(self.rcfg.get("jitter", 0.25))
        return delay * random.uniform(1 - jitter, 1 + jitter)


class QuotaBackoff(BackoffPolicy):
    """Cupo del servidor agotado: se empieza corto y cada capítulo crece por
    su cuenta; en cuanto uno sale adelante se despiertan todos (RetryScheduler.wake)."""

    def base_seconds(self) -> float:
        return self.rcfg.get("quota_initial_delay_minutes", 30) * 60

    def max_seconds(self) -> float:
        return self.rcfg.get("quota_max_delay_hours", 18) * 3600


def backoff_for(cfg: dict, exc: Exception | None = None) -> BackoffPolicy:
    rcfg = cfg.get("download_retry", {})
    if http_status(exc) in set(rcfg.get("quota_status_codes", [403])):
        return QuotaBackoff(rcfg)
    return BackoffPolicy(rcfg)


def compute_retry_delay_seconds(attempt: int, cfg: dict, exc: Exception | None = None) -> float:
    return backoff_for(cfg, exc).delay(attempt)
