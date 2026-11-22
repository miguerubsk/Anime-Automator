"""Metaclase Singleton: una sola instancia por clase en todo el proceso."""

import threading
from abc import ABCMeta


class Singleton(ABCMeta):
    """
    Los servicios con estado compartido (la cola, los reintentos, los
    torrents seguidos, las peticiones de requester-bot...) tienen que ser
    únicos en el proceso: dos instancias de la cola serían dos colas. Con esta
    metaclase, `WorkQueue()` devuelve siempre la misma instancia, venga de
    donde venga la llamada, sin variables globales sueltas por los módulos.

    Deriva de ABCMeta para que un singleton pueda heredar además de una clase
    abstracta (p.ej. los servicios que son un BackgroundLoop).
    Es reentrante: el constructor de un singleton puede pedir otro.
    `Singleton.reset()` existe para las pruebas.
    """

    _instances: dict = {}
    _lock = threading.RLock()

    def __call__(cls, *args, **kwargs):
        inst = Singleton._instances.get(cls)
        if inst is None:
            with Singleton._lock:
                inst = Singleton._instances.get(cls)
                if inst is None:
                    inst = super().__call__(*args, **kwargs)
                    Singleton._instances[cls] = inst
        return inst

    @classmethod
    def reset(mcs, cls=None):
        """Olvida la instancia de `cls` (o todas). Solo para pruebas."""
        with mcs._lock:
            if cls is None:
                mcs._instances.clear()
            else:
                mcs._instances.pop(cls, None)
