"""
Qué hacer cuando un capítulo falla (cadena de manejadores / estrategias).

Cada manejador mira el fallo y, si le toca, lo resuelve y dice qué pasa con
el capítulo (Disposition); si no, pasa al siguiente. El orden importa:

  0. DriveMirror         el mismo archivo desde la unidad de Drive (sin cupo ni LaLiga)
  1. TemporaryError      conexión, 5xx, cupo, la NAS: reintento con espera creciente
                         (y de vez en cuando, otro enlace si el servidor no contesta)
  2. LogPermanent        deja constancia del fallo y sigue
  3. AlternativeLink     otro enlace para ese capítulo en la ficha
  4. NyaaFallback        el capítulo en nyaa.si por torrent
  5. RelinkLater         volver a mirar la ficha mañana (se suelen resubir)
  6. GiveUp              se da por fallido y se avisa

Para añadir un caso nuevo basta una clase más en la lista (abierto/cerrado).
"""

import html
import logging
import random
import urllib.parse
from dataclasses import dataclass

from ..core.errors import (SourceContentError, describe_error, is_temporary_download_error,
                           is_unreachable_server)
from ..core.util import format_duration
from ..sources.drive import DriveError, DriveMirror
from ..sources.japanpaw import alternative_link
from .backoff import compute_retry_delay_seconds
from .model import WorkItem
from .progress import BatchProgress
from .reporting import report
from .retries import RetryScheduler
from .work_queue import WorkQueue

log = logging.getLogger("anime-automator")

# hasta cuántas veces se vuelve a leer la ficha (un día entre cada una) si
# un capítulo da 404 y no hay ni otro enlace ni torrent
RELINK_MAX = 3
RELINK_DELAY_HOURS = 24
# con errores de conexión, cada cuántos intentos se mira si la ficha tiene
# otro enlace (otro servidor) para ese capítulo
CONNECTION_ALT_EVERY = 4


def link_to(url: str | None, text: str = "enlace") -> str:
    """<a> para Telegram con la URL bien escapada (espacios, corchetes...)."""
    if not url or not url.startswith(("http://", "https://")):
        return ""
    safe = urllib.parse.quote(url, safe=":/?&=%#@!$'()*+,;~")
    return f' (<a href="{html.escape(safe)}">{text}</a>)'


@dataclass(frozen=True)
class Disposition:
    keep_in_progress: bool   # sigue "en curso" (reintento, otro enlace, torrent)
    keep_pending: bool       # sigue en la cola persistente


HELD = Disposition(True, True)           # volverá a la cola
HANDED_OFF = Disposition(True, False)    # lo sigue otro (el TorrentTracker)
RELEASED = Disposition(False, False)     # terminado, bien o mal


@dataclass
class FailureContext:
    cfg: dict
    item: WorkItem
    exc: Exception

    @property
    def source_failed(self) -> bool:
        """¿Ha fallado la fuente (el enlace) y no nuestro procesado?"""
        return not isinstance(self.exc, SourceContentError)

    def requeue_with_alternative(self, why: str) -> bool:
        """Si la ficha tiene otro enlace para este capítulo, se encola ya con él."""
        item = self.item
        tried = list(item.target.get("tried_urls") or []) + [item.url]
        alt = alternative_link(self.cfg, item.target, item.episode, set(tried))
        if not alt:
            return False
        log.info("%s %s %s: %s", item.title, item.label, why, alt)
        WorkQueue().put(item.evolve(url=alt, attempt=1, tried_urls=tried), priority=0, at="front")
        return True


class FailureHandler:
    def handle(self, ctx: FailureContext) -> Disposition | None:
        """Disposition si este manejador se ocupa del fallo; None para pasar al siguiente."""
        raise NotImplementedError


class DriveMirrorHandler(FailureHandler):
    """
    Si el enlace del paste falla (cupo, LaLiga, 404...), se prueba ya el mismo
    archivo en la unidad de Drive. Si lo que falla es el propio Drive, se
    reintenta más tarde (otra vez enlace y, si falla, Drive); si el archivo
    bajó pero no se pudo procesar, sigue la cadena normal.
    """

    def handle(self, ctx):
        item = ctx.item
        if item.target.get("via_drive"):
            ctx.item = item = item.evolve(via_drive=False)
            if not isinstance(ctx.exc, DriveError):
                return None
            delay = compute_retry_delay_seconds(item.attempt, ctx.cfg, ctx.exc)
            log.warning("%s %s: falló el enlace y también el Drive (%s); reintento en %s",
                        item.title, item.label, ctx.exc, format_duration(delay))
            RetryScheduler().schedule(item, delay / 3600, item.attempt + 1)
            report(item.key, "episode_retry",
                   f"⏳ <b>{item.title}</b> {item.label}: falló el enlace y también el Drive "
                   f"({describe_error(ctx.exc)}). Se reintentará en ~{format_duration(delay)}.",
                   notify_if=item.attempt <= 3 or item.attempt % 6 == 0,
                   attempt=item.attempt, error=describe_error(ctx.exc, for_html=False),
                   delay=format_duration(delay))
            return HELD
        if not ctx.source_failed or not DriveMirror(ctx.cfg).remote_path(item.url):
            return None
        log.info("%s %s falló desde el enlace (%s): lo bajo del Drive", item.title, item.label, ctx.exc)
        WorkQueue().put(item.evolve(via_drive=True), priority=0, at="front")
        return HELD


class TemporaryErrorHandler(FailureHandler):
    """
    Error temporal (conexión, certificado, 429/5xx, la NAS) — se reintenta más
    tarde, sin límite de intentos, con espera creciente. No se va a nyaa.si
    por esto: el problema no es que el archivo no exista.
    """

    def handle(self, ctx):
        e, item = ctx.exc, ctx.item
        if not is_temporary_download_error(e):
            return None
        log.warning("Error temporal descargando %s capítulo %s (intento %s): %s",
                    item.title, item.episode, item.attempt, e)
        # servidor inalcanzable un buen rato: ¿hay otro enlace en la ficha?
        if is_unreachable_server(e) and item.attempt % CONNECTION_ALT_EVERY == 0:
            if ctx.requeue_with_alternative("el servidor no responde, pruebo otro enlace"):
                return HELD
        delay = compute_retry_delay_seconds(item.attempt, ctx.cfg, e)
        RetryScheduler().schedule(item, delay / 3600, item.attempt + 1)
        # a partir del 4º intento, para no saturar Telegram en cortes largos,
        # solo se avisa de vez en cuando (el log registra cada intento)
        report(item.key, "episode_retry",
               f"⏳ Error temporal con <b>{item.title}</b> {item.label} (intento {item.attempt}): "
               f"{describe_error(e)}{link_to(item.url)}\n"
               f"Se reintentará en ~{format_duration(delay)}.",
               notify_if=item.attempt <= 3 or item.attempt % 6 == 0,
               attempt=item.attempt, error=describe_error(e, for_html=False), delay=format_duration(delay))
        return HELD


class LogPermanentFailure(FailureHandler):
    def handle(self, ctx):
        log.error("Fallo procesando episodio %s de %s", ctx.item.episode, ctx.item.title, exc_info=ctx.exc)
        return None


class AlternativeLinkHandler(FailureHandler):
    """¿La ficha tiene otro enlace para este capítulo (otra pestaña, o lo han vuelto a subir)?"""

    def handle(self, ctx):
        if ctx.source_failed and ctx.requeue_with_alternative(f"falló ({ctx.exc}): pruebo otro enlace de la ficha"):
            return HELD
        return None


class NyaaFallbackHandler(FailureHandler):
    """
    El fallback de nyaa busca por número de capítulo: no sirve para películas
    ni cuando el archivo de japan-paw se descargó bien (el fallo es nuestro,
    no del enlace: una release de nyaa no lo arreglaría).
    """

    def handle(self, ctx):
        if not (ctx.cfg.get("nyaa_fallback", {}).get("enabled") and not ctx.item.is_movie and ctx.source_failed):
            return None
        from .torrents import NyaaFallback  # aquí para no crear un ciclo de importación
        try:
            if NyaaFallback(ctx.cfg).try_start(ctx.item, ctx.item.url, str(ctx.exc)):
                return HANDED_OFF
        except Exception:
            log.exception("El fallback de nyaa.si también falló")
        return None


class RelinkLaterHandler(FailureHandler):
    """Ni otro enlace ni torrent: se vuelve a mirar la ficha mañana, hasta RELINK_MAX veces."""

    def handle(self, ctx):
        item = ctx.item
        relinks = int(item.target.get("relinks") or 0)
        if not (ctx.source_failed and item.target.get("page_url") and relinks < RELINK_MAX):
            return None
        delay = RELINK_DELAY_HOURS * 3600 * random.uniform(0.9, 1.1)
        later = item.evolve(relinks=relinks + 1, tried_urls=[], relink_pending=True)
        RetryScheduler().schedule(later, delay / 3600, 1)
        log.info("%s %s sin enlace válido: vuelvo a mirar la ficha en %s (%s/%s)",
                 item.title, item.label, format_duration(delay), relinks + 1, RELINK_MAX)
        report(item.key, "episode_retry",
               f"🔗 <b>{item.title}</b> {item.label}: el enlace no funciona ({describe_error(ctx.exc)})"
               f"{link_to(item.url)} y no hay "
               f"alternativa. Vuelvo a mirar la ficha en ~{format_duration(delay)} ({relinks + 1}/{RELINK_MAX}).",
               attempt=relinks + 1, error=describe_error(ctx.exc, for_html=False), delay=format_duration(delay))
        return HELD


class GiveUpHandler(FailureHandler):
    def handle(self, ctx):
        item = ctx.item
        BatchProgress().failed(item.folder, item.episode)
        report(item.key, "episode_failed",
               lambda: (f"❌ Error procesando <b>{item.title}</b> {item.label}: {describe_error(ctx.exc)}"
                        f"{link_to(item.url)}{BatchProgress().block()}"),
               error=describe_error(ctx.exc, for_html=False))
        return RELEASED


DEFAULT_FAILURE_CHAIN = (
    DriveMirrorHandler(),
    TemporaryErrorHandler(),
    LogPermanentFailure(),
    AlternativeLinkHandler(),
    NyaaFallbackHandler(),
    RelinkLaterHandler(),
    GiveUpHandler(),
)


def handle_failure(ctx: FailureContext, chain=DEFAULT_FAILURE_CHAIN) -> Disposition:
    for handler in chain:
        disposition = handler.handle(ctx)
        if disposition is not None:
            return disposition
    return RELEASED
