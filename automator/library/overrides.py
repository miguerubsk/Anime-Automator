"""
Casos especiales de config.yaml (animes:): carpeta fijada a mano para un
título, y para las películas, si hace falta, el nombre del archivo.

    animes:
      - watch_title: "Fate/kaleid liner Prisma☆Illya"
        folder: "/nuevo-pool/Anime/Fate/Kaleid/Liner Prisma Illya"
      - watch_title: "Kizumonogatari I: Tekketsu-hen"
        folder: "/nuevo-pool/Anime/Monogatari series/1.1 Kizumonogatari I - Tekketsu-hen"
        file: "Kizumonogatari I Tekketsu-hen.mkv"

El título casa con o sin el sufijo de la web y sin mirar signos ni
mayúsculas: la búsqueda dice «Fate/kaleid liner Prisma☆Illya» y el paste
«Fate kaleid liner Prisma Illya - Japan-Paw! Descarga Calidad».
"""

from .naming import name_key, strip_site_suffix


def _key(cfg: dict, title: str) -> str:
    return name_key(strip_site_suffix(title or "", cfg.get("site_title_suffix", "")))


def override_for(cfg: dict, title: str) -> dict | None:
    """La entrada de animes: para ese título, o None."""
    key = _key(cfg, title)
    if not key:
        return None
    return next((a for a in cfg.get("animes") or [] if _key(cfg, a["watch_title"]) == key), None)
