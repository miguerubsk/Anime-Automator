"""
Esquivar el bloqueo de IPs de LaLiga.

Durante los partidos, los operadores españoles bloquean IPs enteras de
Cloudflare: la conexión acaba en un servidor que presenta un certificado
autofirmado y una página de "El acceso a la presente dirección IP ha sido
bloqueado...". La web (p.ej. el CDN de descarga de japan-paw) sigue
funcionando por otras IPs de Cloudflare, que es lo que da otro DNS.

DnsBypass pregunta por DNS cifrado (DoH a 1.1.1.1 / 8.8.8.8, por IP) otras
IPs del mismo nombre, prueba cada una CON verificación de certificado
(SNI y certificado siguen siendo los del nombre real, solo cambia a qué IP
se conecta) y, si alguna responde, fija esa IP para ese nombre un rato.
Afecta a todo el proceso (urllib3), así que también sirve a reintentos y a
otras descargas del mismo servidor.

Si no hay ninguna IP libre y config.yaml tiene `blocked_ip_bypass.proxy`
(p.ej. Tor: "socks5h://host:9050"), ese servidor se descarga por el proxy
durante el mismo rato.
"""

import logging
import threading
import time

import requests
import urllib3.util.connection as urllib3_connection

from .singleton import Singleton
from .util import url_host

log = logging.getLogger("anime-automator")

DOH_SERVERS = (
    ("https://1.1.1.1/dns-query", {"accept": "application/dns-json"}),
    ("https://8.8.8.8/resolve", {}),
)


def is_cert_failure(exc: Exception) -> bool:
    """¿Falló la verificación del certificado? (lo que se ve desde una IP bloqueada)"""
    return isinstance(exc, requests.exceptions.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(exc)


class DnsBypass(metaclass=Singleton):
    """host -> IP alternativa, aplicada al abrir cualquier conexión de urllib3/requests."""

    ttl_seconds = 6 * 3600

    def __init__(self):
        self._lock = threading.Lock()
        self._pins: dict[str, tuple[str, float]] = {}  # host -> (ip, caduca)
        self._proxied: dict[str, float] = {}  # host -> caduca (va por el proxy)
        self._original = urllib3_connection.create_connection
        urllib3_connection.create_connection = self._create_connection

    # -- conexión ------------------------------------------------------ #
    def _create_connection(self, address, *args, **kwargs):
        host, port = address
        ip = self.pinned(host)
        return self._original((ip or host, port), *args, **kwargs)

    def pinned(self, host: str) -> str | None:
        with self._lock:
            pin = self._pins.get((host or "").lower())
            if pin and pin[1] < time.time():
                self._pins.pop(host.lower(), None)
                return None
            return pin[0] if pin else None

    def _set(self, host: str, ip: str | None):
        with self._lock:
            if ip:
                self._pins[host] = (ip, time.time() + self.ttl_seconds)
            else:
                self._pins.pop(host, None)

    # -- proxy (último recurso) ----------------------------------------- #
    def use_proxy(self, host: str):
        with self._lock:
            self._proxied[host.lower()] = time.time() + self.ttl_seconds

    def proxied(self, host: str) -> bool:
        with self._lock:
            until = self._proxied.get((host or "").lower())
            if until and until < time.time():
                self._proxied.pop(host.lower(), None)
                return False
            return bool(until)

    # -- resolución ---------------------------------------------------- #
    @staticmethod
    def resolve(host: str) -> list[str]:
        """IPv4 de `host` según los DoH (sin pasar por el DNS del sistema/operador)."""
        ips: list[str] = []
        for url, headers in DOH_SERVERS:
            try:
                r = requests.get(url, params={"name": host, "type": "A"}, headers=headers, timeout=8)
                for ans in r.json().get("Answer") or []:
                    if ans.get("type") == 1 and ans.get("data") not in ips:
                        ips.append(ans["data"])
            except Exception as e:
                log.debug("DoH %s falló para %s: %s", url, host, e)
        return ips

    def _works(self, host: str, ip: str) -> bool:
        self._set(host, ip)
        try:
            requests.head(f"https://{host}/", timeout=10, allow_redirects=False)
            return True
        except requests.exceptions.RequestException:
            return False
        finally:
            self._set(host, None)

    def bypass(self, url: str) -> str | None:
        """
        Tras un fallo de certificado, busca y fija una IP de `host` que
        funcione. Devuelve la IP fijada (None si no hay ninguna).
        """
        host = url_host(url)
        if not host:
            return None
        # no se mira si la IP "actual" sirve la página de bloqueo: el DNS del
        # sistema alterna IPs bloqueadas y libres, así que eso no es fiable.
        # Basta el fallo de certificado; cada candidata se valida con el real.
        bad = self.pinned(host)
        self._set(host, None)  # si la IP fijada también ha caído, se busca otra
        for ip in self.resolve(host):
            if ip != bad and self._works(host, ip):
                self._set(host, ip)
                log.warning("%s da un certificado falso (¿bloqueo de LaLiga?): uso %s, que funciona, durante %s h",
                            host, ip, self.ttl_seconds // 3600)
                return ip
        log.warning("%s da un certificado falso (¿bloqueo de LaLiga?) y no encuentro otra IP que funcione", host)
        return None


def bypass_proxy(cfg: dict | None = None) -> str | None:
    from .config import load_config
    return ((load_config() if cfg is None else cfg).get("blocked_ip_bypass") or {}).get("proxy") or None


def get_with_bypass(url: str, cfg: dict | None = None, **kwargs) -> requests.Response:
    """
    requests.get que, ante el bloqueo de LaLiga, lo intenta una vez más por
    otra IP o, si no hay ninguna libre, por el proxy configurado.
    """
    host, proxy = url_host(url), bypass_proxy(cfg)
    bypass = DnsBypass()
    if proxy and bypass.proxied(host):
        return requests.get(url, proxies={"http": proxy, "https": proxy}, **kwargs)
    try:
        return requests.get(url, **kwargs)
    except requests.exceptions.SSLError as e:
        if not is_cert_failure(e):
            raise
        if bypass.bypass(url):
            return requests.get(url, **kwargs)
        if not proxy:
            raise
        log.warning("%s: sin IP libre, descargo por el proxy %s durante %s h",
                    host, proxy, bypass.ttl_seconds // 3600)
        bypass.use_proxy(host)
    return requests.get(url, proxies={"http": proxy, "https": proxy}, **kwargs)
