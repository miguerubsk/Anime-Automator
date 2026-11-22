"""Chrome headless (sockpuppetbrowser) para leer páginas protegidas por Cloudflare."""

from playwright.sync_api import sync_playwright

from ..core.config import load_config


def fetch_rendered_html(url: str, sockpuppet_url: str | None = None, timeout_ms: int = 30000) -> str:
    """
    Descarga el HTML de la página usando el mismo Chrome headless que ya usa
    change-detection, para saltarnos la protección anti-bot de Cloudflare
    que bloquea peticiones simples (requests/curl).
    """
    sockpuppet_url = sockpuppet_url or load_config()["sockpuppet_browser_url"]
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(sockpuppet_url)
        try:
            page = browser.new_context().new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            return page.content()
        finally:
            browser.close()
