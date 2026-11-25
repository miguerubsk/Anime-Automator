"""Pantallas del bot de administración y /descargar (la cola y el encolado se falsean en su frontera)."""

import os
import re
import time
from unittest import mock

from tests.helpers import AutomatorTestCase


def plain(text):
    return re.sub(r"<[^>]+>", "", text)


def callbacks(kb):
    return [[b["callback_data"] for b in row] for row in kb]


class ViewsTestCase(AutomatorTestCase):
    def overview(self, groups=(), current=None, all_paused=False, torrents=()):
        from automator.pipeline.management import QueueOverview
        ov = QueueOverview(current, all_paused, list(groups), list(torrents))
        patcher = mock.patch("automator.admin_bot.views.queue_overview", return_value=ov)
        patcher.start()
        self.addCleanup(patcher.stop)
        return ov

    @staticmethod
    def group(name, queued=(), retry=(), paused=False, paused_eps=()):
        from automator.pipeline.management import EpisodeView, GroupView
        g = GroupView(f"grupo-{name}", name, paused)
        g.episodes += [EpisodeView((name, ep), ep, ep in paused_eps, "queued") for ep in queued]
        g.episodes += [EpisodeView((name, ep), ep, False, "retry", at) for ep, at in retry]
        return g


class QueueViewTest(ViewsTestCase):
    def test_empty_queue(self):
        from automator.admin_bot.views import queue_view
        self.overview()
        text, kb = queue_view()
        self.assertEqual(plain(text), "📋 Cola de descargas\n\nNo hay nada pendiente.")
        self.assertEqual(callbacks(kb), [["qa:pause", "q"]])
        self.assertEqual(kb[0][0]["text"], "⏸ Pausar cola")

    def test_everything_at_once(self):
        from automator.admin_bot.views import gid, queue_view
        from automator.pipeline.management import TorrentView
        long_name = "Un nombre larguísimo de anime que no cabe en un botón <b>"
        self.overview(
            groups=[self.group("Uno", queued=[1, 2, 3, 5], paused_eps=[5]),
                    self.group(long_name, retry=[(4, time.time() + 60)], paused=True)],
            current=self.item("Actual & Co", 7), all_paused=True,
            torrents=[TorrentView("Tor", 3, "[X] Tor - 03", False), TorrentView("Ren", "OVA", "[X] Ren", True)])
        text, kb = queue_view()
        self.assertEqual(plain(text).split("\n"), [
            "📋 Cola de descargas",
            "⏸ Cola pausada: no empezará ningún capítulo nuevo.",
            "▶️ Ahora: Actual &amp; Co cap. 07",
            "",
            "5 capítulo(s) pendientes en 2 anime(s):",
            "1. Uno — 1-3, 5; 1 en pausa",
            f"2. ⏸ {long_name.replace('<b>', '&lt;b&gt;')} — ⏳ reintento: 4",
            "",
            "🧲 Torrents de nyaa.si:",
            "• Tor cap. 03 (descargando)",
            "• Ren cap. OVA (sembrando)",
        ])
        self.assertEqual(callbacks(kb), [[f"g:{gid('grupo-Uno')}"], [f"g:{gid('grupo-' + long_name)}"],
                                         ["qa:resume", "q"]])
        self.assertEqual(kb[1][0]["text"], "2. ⏸ Un nombre larguísimo de anime que no ca…")
        self.assertEqual(kb[2][0]["text"], "▶️ Reanudar cola")


class GroupViewTest(ViewsTestCase):
    def test_unknown_group(self):
        from automator.admin_bot.views import group_view
        self.overview(groups=[self.group("Uno", queued=[1])])
        self.assertIsNone(group_view("nada"))

    def test_group_with_queue_and_retries(self):
        from automator.admin_bot.views import gid, group_view
        g = self.group("Dos", queued=list(range(1, 7)) + list(range(10, 70)), paused_eps=[2],
                       retry=[(7, time.time() + 3600)])
        self.overview(groups=[self.group("Uno", queued=[1]), g])
        h = gid(g.group)
        text, kb = group_view(h)
        lines = plain(text).split("\n")
        self.assertEqual(lines[:2], ["🎞 Dos", "Posición 2 de 2 en la cola"])
        self.assertTrue(lines[3].startswith("En cola, en este orden: 1, ⏸2, 3, 4, 5, 6, 10, 11"), lines[3])
        self.assertRegex(lines[4], r"^Esperando reintento: 7 \(\d\d:\d\d\)$")
        self.assertEqual(lines[-1], "Toca un capítulo para gestionarlo por separado.")
        self.assertEqual(callbacks(kb)[:2], [[f"ga:{h}:front", f"ga:{h}:up", f"ga:{h}:down", f"ga:{h}:end"],
                                             [f"ga:{h}:pause", f"ga:{h}:del"]])
        self.assertEqual([b["text"] for b in kb[2]], ["1", "⏸2", "3", "4", "5"])
        episode_rows = kb[2:-1]
        self.assertEqual(len(episode_rows), 12)               # 60 botones como mucho, de 5 en 5
        self.assertEqual(episode_rows[-1][-1]["callback_data"], f"e:{h}:63")   # 1-6 y 10-63
        self.assertEqual(callbacks(kb)[-1], ["q"])

    def test_paused_group_and_retry_button(self):
        from automator.admin_bot.views import gid, group_view
        g = self.group("Uno", retry=[(3, time.time() + 48 * 3600)], paused=True)
        self.overview(groups=[g])
        h = gid(g.group)
        text, kb = group_view(h)
        self.assertIn("Posición 1 de 1 en la cola — ⏸ en pausa", plain(text))
        self.assertRegex(plain(text), r"Esperando reintento: 3 \(\d\d/\d\d \d\d:\d\d\)")
        self.assertNotIn("En cola", plain(text))
        self.assertEqual(kb[1][0]["callback_data"], f"ga:{h}:resume")
        self.assertEqual(kb[2], [{"text": "⏳3", "callback_data": f"e:{h}:3"}])

    def test_confirm_delete(self):
        from automator.admin_bot.views import gid, group_view
        g = self.group("Uno", queued=[1, 2])
        self.overview(groups=[g])
        h = gid(g.group)
        text, kb = group_view(h, confirm_delete=True)
        self.assertTrue(plain(text).endswith("⚠️ ¿Eliminar de la cola los 2 capítulo(s) de este anime?"))
        self.assertEqual(callbacks(kb), [[f"ga:{h}:delok", f"g:{h}"]])


class ImmediateThread:
    """threading.Thread que ejecuta el trabajo al arrancar (para ver qué contesta el bot)."""

    def __init__(self, target, args, daemon):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class DownloadCommandTest(AutomatorTestCase):
    URL = "https://paste.japan-paw.net/?v=1234"

    def download(self, text, **fake):
        from automator.admin_bot import commands
        os.environ["ADMIN_BOT_CHAT_IDS"] = "100"
        replies = []
        msg = commands.Message("100", text, True)
        with mock.patch("automator.admin_bot.commands.threading.Thread", ImmediateThread), \
             mock.patch("automator.admin_bot.commands.download_whole_anime", **fake) as dl:
            commands.resolve(msg).run(msg, lambda t, kb=None: replies.append((t, kb)))
        return replies, dl

    def test_queued(self):
        result = {"status": "queued", "name": "Serie", "queued": [1, 2], "available": 2, "have": 0,
                  "folder": "Anime/Serie"}
        replies, dl = self.download(f"mira {self.URL}.", return_value=result)
        self.assertEqual(dl.call_args.args[1], self.URL)
        self.assertEqual(replies[0][0], "📨 Mirando la página, enseguida te digo qué encolo…")
        self.assertIn("2 capítulo(s) encolados", replies[1][0])
        self.assertEqual(replies[1][1], [[{"text": "📋 Ver la cola", "callback_data": "q"}]])

    def test_errors_reported_while_enqueuing(self):
        def failing(cfg, url, notify_fn):
            notify_fn("❌ uno")
            notify_fn("❌ dos")
            return {"status": "error"}

        replies, _ = self.download(f"/descargar {self.URL}", side_effect=failing)
        self.assertEqual(replies[1], ("❌ uno\n❌ dos", None))

    def test_status_without_message(self):
        replies, _ = self.download(f"/bajar {self.URL}", return_value={"status": "<raro>"})
        self.assertEqual(replies[1], ("❌ No se pudo encolar (&lt;raro&gt;).", None))

    def test_exception(self):
        with self.assertLogs("anime-automator.admin-bot", "ERROR"):
            replies, _ = self.download(self.URL, side_effect=RuntimeError("a<b"))
        self.assertEqual(replies[1], ("❌ Error encolando: <code>a&lt;b</code>", None))
