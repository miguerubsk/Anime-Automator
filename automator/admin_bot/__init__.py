"""
Bot de Telegram de administración de Anime-Automator.

Corre como un hilo dentro del propio automatizador (long polling a
getUpdates, prácticamente 0% CPU en reposo) y usa SU PROPIO bot, distinto del
de las notificaciones:

    ADMIN_BOT_TOKEN      token del bot (si está vacío, el bot no arranca)
    ADMIN_BOT_CHAT_IDS   chats autorizados, separados por comas

Qué hace:
- /start (cualquier chat): responde con el Chat ID, para configurarlo.
- Una URL de japan-paw.net, o /descargar URL (/bajar URL): encola todo lo
  que falte de ese anime o película.
- /cola: gestión de la cola. Ver, eliminar, pausar y reordenar animes
  completos y capítulos sueltos; también los reintentos programados.
- /pausar y /reanudar: pausa o reanuda la cola entera.

Los chats no autorizados solo reciben respuesta a /start; todo lo demás se
ignora sin contestar.
"""
