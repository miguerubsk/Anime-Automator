"""
Anime Automator
================
Recibe avisos de change-detection.io (y peticiones del bot de administración
y de requester-bot), localiza los capítulos en japan-paw.net, los descarga,
deja solo las pistas que interesan (audio japonés/español, subtítulos en
español), los guarda en la biblioteca de la NAS y avisa por Telegram.

Organización del paquete (de más bajo a más alto nivel):

    core/       configuración, persistencia, hilos de fondo, errores, NAS
    notify/     Telegram y traducción de rutas internas a las que ve el usuario
    library/    nombres, temporadas, carpetas de la biblioteca, películas
    sources/    japan-paw.net (fichas), nyaa.si, navegador headless
    media/      selección de pistas y remux con mkvmerge
    pipeline/   cola, reintentos, peticiones de requester-bot, torrents,
                descarga/procesado/colocación y gestión de fallos
    admin_bot/  bot de Telegram de administración
    web.py      API HTTP (Flask)
    bootstrap.py arranque ordenado de todos los servicios
"""
