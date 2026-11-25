"""Las pantallas del bot: texto + teclado de botones."""

import hashlib
import html
import time
from datetime import datetime

from ..core.util import compress_ranges
from ..pipeline.management import GroupView, QueueOverview, queue_overview

EP_BUTTONS_PER_ROW = 5
MAX_EP_BUTTONS = 60   # por encima, el teclado se haría enorme; el resto se ve en el texto

e = html.escape


def btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def gid(group: str) -> str:
    """Id corto y estable de un anime para los botones (callback_data ≤ 64 bytes)."""
    return hashlib.sha1(group.encode()).hexdigest()[:10]


def find_group(overview: QueueOverview, h: str) -> GroupView | None:
    return next((g for g in overview.groups if gid(g.group) == h), None)


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M" if ts - time.time() < 20 * 3600 else "%d/%m %H:%M")


def ep_label(ep) -> str:
    return f"cap. {ep:02d}" if isinstance(ep, int) else f"cap. {ep}"


def short(name: str, n: int = 40) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


View = tuple[str, list]  # (texto HTML, teclado)


def paused_mark(g: GroupView) -> str:
    return "⏸ " if g.paused else ""


def queue_view() -> View:
    ov = queue_overview()
    lines = ["📋 <b>Cola de descargas</b>"]
    if ov.all_paused:
        lines.append("⏸ <b>Cola pausada</b>: no empezará ningún capítulo nuevo.")
    if ov.current:
        lines.append(f"▶️ Ahora: <b>{e(ov.current.name)}</b> {ep_label(ov.current.episode)}")
    if not ov.groups:
        lines.append("\nNo hay nada pendiente.")
    else:
        lines.append(f"\n{ov.episode_count} capítulo(s) pendientes en {len(ov.groups)} anime(s):")
        lines += [f"{i}. {paused_mark(g)}<b>{e(g.name)}</b> — {group_summary(g)}" for i, g in enumerate(ov.groups, 1)]
    if ov.torrents:
        lines.append("\n🧲 Torrents de nyaa.si:")
        lines += [f"• <b>{e(t.name)}</b> {ep_label(t.ep)} ({'sembrando' if t.processed else 'descargando'})"
                  for t in ov.torrents]
    return "\n".join(lines), queue_keyboard(ov)


def group_summary(g: GroupView) -> str:
    """"1-3, 5; ⏳ reintento: 7; 1 en pausa": lo pendiente de un anime en una línea."""
    queued = [x.ep for x in g.of_state("queued")]
    retries = [x.ep for x in g.of_state("retry")]
    n_paused = sum(1 for x in g.episodes if x.paused)
    parts = [compress_ranges(queued)] if queued else []
    if retries:
        parts.append(f"⏳ reintento: {compress_ranges(retries)}")
    if n_paused:
        parts.append(f"{n_paused} en pausa")
    return "; ".join(parts)


def queue_keyboard(ov: QueueOverview) -> list:
    kb = [[btn(f"{i}. {paused_mark(g)}{short(g.name)}", f"g:{gid(g.group)}")]
          for i, g in enumerate(ov.groups, 1)]
    kb.append([btn("▶️ Reanudar cola" if ov.all_paused else "⏸ Pausar cola",
                   "qa:resume" if ov.all_paused else "qa:pause"),
               btn("🔄 Actualizar", "q")])
    return kb


def group_view(h: str, confirm_delete: bool = False) -> View | None:
    ov = queue_overview()
    g = find_group(ov, h)
    if g is None:
        return None
    pos = ov.groups.index(g) + 1
    queued, retries = g.of_state("queued"), g.of_state("retry")
    lines = [f"🎞 <b>{e(g.name)}</b>",
             f"Posición {pos} de {len(ov.groups)} en la cola" + (" — ⏸ <b>en pausa</b>" if g.paused else "")]
    if queued:
        lines.append("\nEn cola, en este orden: " + ", ".join(f"⏸{x.ep}" if x.paused else str(x.ep) for x in queued))
    if retries:
        lines.append("Esperando reintento: " + ", ".join(f"{x.ep} ({hhmm(x.retry_at)})" for x in retries))
    lines.append("\nToca un capítulo para gestionarlo por separado.")

    if confirm_delete:
        lines.append(f"\n⚠️ ¿Eliminar de la cola los {len(g.episodes)} capítulo(s) de este anime?")
        return "\n".join(lines), [[btn("🗑 Sí, eliminar", f"ga:{h}:delok"), btn("Cancelar", f"g:{h}")]]
    return "\n".join(lines), group_keyboard(h, g)


def group_keyboard(h: str, g: GroupView) -> list:
    """Mover, pausar o eliminar el anime, y un botón por capítulo (hasta MAX_EP_BUTTONS)."""
    kb = [
        [btn("⏫ Primero", f"ga:{h}:front"), btn("⬆️ Subir", f"ga:{h}:up"),
         btn("⬇️ Bajar", f"ga:{h}:down"), btn("⏬ Último", f"ga:{h}:end")],
        [btn("▶️ Reanudar" if g.paused else "⏸ Pausar", f"ga:{h}:{'resume' if g.paused else 'pause'}"),
         btn("🗑 Eliminar todo", f"ga:{h}:del")],
    ]
    episodes = g.episodes[:MAX_EP_BUTTONS]
    for i in range(0, len(episodes), EP_BUTTONS_PER_ROW):
        kb.append([btn(f"{'⏳' if x.state == 'retry' else ('⏸' if x.paused else '')}{x.ep}", f"e:{h}:{x.ep}")
                   for x in episodes[i:i + EP_BUTTONS_PER_ROW]])
    kb.append([btn("« Volver a la cola", "q")])
    return kb


def episode_view(h: str, ep: int) -> View | None:
    g = find_group(queue_overview(), h)
    x = g.find(ep) if g else None
    if x is None:
        return None
    lines = [f"🎞 <b>{e(g.name)}</b> — {ep_label(ep)}"]
    if x.state == "retry":
        lines.append(f"⏳ Esperando reintento a las {hhmm(x.retry_at)}.")
        kb = [[btn("▶️ Reintentar ya", f"ea:{h}:{ep}:now"), btn("🗑 Eliminar", f"ea:{h}:{ep}:del")]]
    else:
        queued = g.of_state("queued")
        lines.append(f"Puesto {queued.index(x) + 1} de {len(queued)} dentro del anime"
                     + (" — ⏸ <b>en pausa</b>" if x.paused else ""))
        if g.paused:
            lines.append("(el anime entero está en pausa)")
        kb = [
            [btn("⏫ El siguiente", f"ea:{h}:{ep}:front"), btn("⬆️ Antes", f"ea:{h}:{ep}:up"),
             btn("⬇️ Después", f"ea:{h}:{ep}:down")],
            [btn("▶️ Reanudar" if x.paused else "⏸ Pausar", f"ea:{h}:{ep}:{'resume' if x.paused else 'pause'}"),
             btn("🗑 Eliminar", f"ea:{h}:{ep}:del")],
        ]
    kb.append([btn("« Volver al anime", f"g:{h}")])
    return "\n".join(lines), kb
