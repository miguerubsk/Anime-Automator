"""Tipos de error del dominio y su clasificación (¿merece la pena reintentar?)."""

import html

import requests

# Códigos HTTP que indican un problema del lado del servidor / temporal
# (vale la pena reintentar), a diferencia de un 404 (el archivo no existe,
# reintentar no sirve de nada). El 403 se incluye porque en la práctica ha
# resultado ser un bloqueo anti-hotlink/anti-bot intermitente (no un
# archivo inexistente) en algunos hosts de descarga.
TEMPORARY_HTTP_STATUS = {403, 429, 500, 502, 503, 504}


class NasNotMountedError(RuntimeError):
    """No está el archivo centinela: la NAS no está montada."""


class DestinationWriteError(Exception):
    """El mkv ya está procesado y bien, pero no se pudo escribir en la NAS
    (SMB caído, nombre bloqueado por un borrado pendiente, etc.). No es culpa
    de la fuente: se reintenta más tarde, nunca se va a nyaa.si por esto."""


class SourceContentError(Exception):
    """El archivo de japan-paw se descargó bien pero falló al procesarlo
    (pistas, mkvmerge...). Esa web siempre trae audio/subs correctos, así que
    esto es un fallo nuestro, no de la fuente: no se sustituye por una
    release de nyaa.si (que normalmente solo trae subs en inglés)."""


def http_status(exc: Exception) -> int | None:
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = getattr(exc, "response", None)
        return resp.status_code if resp is not None else None
    return None


def is_temporary_download_error(exc: Exception) -> bool:
    """
    True si el error parece temporal (problema de red/certificado, o un
    código HTTP de servidor/límite de tasa) y por tanto vale la pena
    reintentar más tarde en vez de caer directamente al fallback de nyaa.si.
    """
    if isinstance(exc, (requests.exceptions.SSLError, requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout, DestinationWriteError)):
        return True
    return http_status(exc) in TEMPORARY_HTTP_STATUS


def is_unreachable_server(exc: Exception) -> bool:
    """Temporal y de conexión (el servidor no contesta), no un código HTTP ni la NAS."""
    return is_temporary_download_error(exc) and not isinstance(
        exc, (DestinationWriteError, requests.exceptions.HTTPError))


def describe_error(exc: Exception, for_html: bool = True) -> str:
    """
    El error en una línea para los avisos (Telegram, requester-bot); escapado
    para HTML salvo `for_html=False` (requester-bot lo escapa él). Los de requests traen la URL entera partida y el
    repr de urllib3, que en Telegram se ven como un enlace roto.
    """
    from .util import url_host
    req = getattr(exc, "request", None)
    host = url_host(getattr(req, "url", "") or "") or "el servidor"
    if isinstance(exc, requests.exceptions.SSLError):
        text = (f"certificado no válido en {host} (¿IP bloqueada por LaLiga?)"
                if "CERTIFICATE_VERIFY_FAILED" in str(exc) else f"error SSL con {host}")
    elif isinstance(exc, requests.exceptions.Timeout):
        text = f"{host} no responde (tiempo de espera agotado)"
    elif isinstance(exc, requests.exceptions.ConnectionError):
        text = f"no se pudo conectar con {host}"
    elif http_status(exc) is not None:
        reason = getattr(exc.response, "reason", "") or ""
        text = f"{host} respondió HTTP {http_status(exc)} {reason}".strip()
    else:
        text = str(exc) or type(exc).__name__
    return html.escape(text, quote=False) if for_html else text
