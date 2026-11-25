"""Chrome headless (sockpuppetbrowser) para leer páginas protegidas por Cloudflare."""

import logging
import time

from playwright.sync_api import sync_playwright

from ..core.config import load_config

log = logging.getLogger("anime-automator")

# la web a veces rechaza una petición suelta (ERR_HTTP_RESPONSE_CODE_FAILURE) y
# a los pocos segundos responde bien: se reintenta antes de dar el error
RETRY_DELAYS = (5, 20)


def _fetch_once(url: str, sockpuppet_url: str, timeout_ms: int) -> str:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(sockpuppet_url)
        try:
            page = browser.new_context().new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            return page.content()
        finally:
            browser.close()


def fetch_rendered_html(url: str, sockpuppet_url: str | None = None, timeout_ms: int = 30000) -> str:
    """
    Descarga el HTML de la página usando el mismo Chrome headless que ya usa
    change-detection, para saltarnos la protección anti-bot de Cloudflare
    que bloquea peticiones simples (requests/curl). Si falla, lo reintenta
    un par de veces (RETRY_DELAYS) antes de lanzar el error.
    """
    sockpuppet_url = sockpuppet_url or load_config()["sockpuppet_browser_url"]
    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        try:
            return _fetch_once(url, sockpuppet_url, timeout_ms)
        except Exception as e:
            if delay is None:
                raise
            log.warning("No se pudo leer %s (intento %s): %s; reintento en %s s",
                        url, attempt, str(e).splitlines()[0], delay)
            time.sleep(delay)
