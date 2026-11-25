"""Búsquedas en nyaa.si: el RSS se falsea en la frontera (requests.get)."""

from unittest import mock

import requests

from tests.helpers import AutomatorTestCase


def item(title, url=None, link=None):
    enclosure = f'<enclosure url="{url}"/>' if url else ""
    link_tag = f"<link>{link}</link>" if link else ""
    return f"<item><title>{title}</title>{enclosure}{link_tag}</item>"


def response(*items):
    resp = requests.Response()
    resp.status_code = 200
    resp._content = ("<rss><channel>" + "".join(items) + "</channel></rss>").encode()
    return resp


def failure():
    resp = requests.Response()
    resp.status_code = 503
    return resp


class NyaaHttp(AutomatorTestCase):
    """Cada consulta (parámetro q) devuelve su respuesta; lo demás, un RSS vacío."""

    def setUp(self):
        super().setUp()
        self.answers, self.queries = {}, []

        def fake_get(url, params=None, timeout=None):
            self.queries.append(params["q"])
            self.assertEqual((url, params["page"], params["c"]), ("https://nyaa.invalid/", "rss", "1_2"))
            return self.answers.get(params["q"], response())

        patcher = mock.patch("automator.sources.nyaa.requests.get", side_effect=fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def nyaa(self):
        from automator.sources.nyaa import NyaaSearch
        return NyaaSearch(self.cfg)


class EpisodeSearchTest(NyaaHttp):
    def test_prefers_group_then_webdl_then_1080p(self):
        self.answers["Show"] = response(
            item("[SubsPlease] Show - 05 (720p) [AA].mkv", "https://t/720"),
            item("[SubsPlease] Show - 05 (1080p) [BB].mkv", "https://t/1080"),
            item("[ToonsHub] Show S01E05 1080p CR WEB-DL (Multi-Subs)", "https://t/webdl"),
            item("[Erai-raws] Show - 05 [720p CR WEBRip][MultiSub]", "https://t/erai"),
        )
        self.assertEqual(self.nyaa().search(["Show"], 5, 1),
                         ("https://t/erai", "[Erai-raws] Show - 05 [720p CR WEBRip][MultiSub]"))

    def test_webdl_beats_1080p_and_ties_keep_nyaa_order(self):
        self.answers["Show"] = response(
            item("[A] Show - 05 (1080p).mkv", "https://t/a"),
            item("[B] Show - 05 (1080p).mkv", "https://t/b"),
        )
        self.assertEqual(self.nyaa().search(["Show"], 5, 1)[0], "https://t/a")
        self.answers["Show"] = response(
            item("[A] Show - 05 (1080p).mkv", "https://t/a"),
            item("[B] Show - 05 (720p) WEB-DL.mkv", "https://t/b"),
        )
        self.assertEqual(self.nyaa().search(["Show"], 5, 1)[0], "https://t/b")

    def test_discards_dubs_other_episodes_other_seasons_and_linkless(self):
        self.answers["Show"] = response(
            item("[Erai-raws] Show - 05 [1080p][English Dub]", "https://t/dub"),
            item("[Erai-raws] Show - 06 [1080p][MultiSub]", "https://t/ep6"),
            item("[Erai-raws] Show 2nd Season - 05 [1080p][MultiSub]", "https://t/s2"),
            item("[Erai-raws] Show - 05 [1080p][MultiSub][sin enlace]"),
            item("[Other] Show - 05 [480p]", link="https://t/link"),
        )
        self.assertEqual(self.nyaa().search(["Show"], 5, 1), ("https://t/link", "[Other] Show - 05 [480p]"))

    def test_next_query_only_when_nothing_found(self):
        self.answers["Segunda"] = response(item("[X] Show - 05 [1080p]", "https://t/2"))
        self.assertEqual(self.nyaa().search([None, "", "Primera", "Segunda", "Tercera"], 5, 1),
                         ("https://t/2", "[X] Show - 05 [1080p]"))
        self.assertEqual(self.queries, ["Primera", "Segunda"])

    def test_nothing_found(self):
        self.answers["Show"] = response(item("[X] Show - 05 [1080p][Dubbed]", "https://t/1"))
        self.assertEqual(self.nyaa().search(["Show", "Otro"], 5, 1), (None, None))

    def test_http_errors_are_raised(self):
        self.answers["Show"] = failure()
        with self.assertRaises(requests.exceptions.HTTPError):
            self.nyaa().search(["Show"], 5, 1)


class MovieSearchTest(NyaaHttp):
    GOOD = "[ToonsHub] Peli 1080p WEB-DL (Multi-Subs)"
    BEST = "[Erai-raws] Peli - Movie [1080p CR WEB-DL][MultiSub]"

    def test_keeps_searching_until_preferred_group(self):
        self.answers["Peli"] = response(item(self.GOOD, "https://t/good"))
        self.answers["Peli Movie"] = response(item(self.BEST, "https://t/best"))
        best = self.nyaa().movie(["Peli", "peli", "Peli: Movie", "Otra"])
        self.assertEqual((best.title, best.link, best.group, best.episode), (self.BEST, "https://t/best", "Erai-raws", 1))
        self.assertEqual(self.queries, ["Peli", "Peli Movie"])   # «peli» repetido; tras Erai-raws, nada más

    def test_failing_query_is_skipped(self):
        self.answers["Peli"] = failure()
        self.answers["Peli Movie"] = response(item(self.GOOD, "https://t/good"), item(self.GOOD))
        self.assertEqual(self.nyaa().movie(["Peli", "Peli Movie"]).link, "https://t/good")

    def test_nothing_suitable(self):
        self.answers["Peli"] = response(item("[Erai-raws] Peli - Movie [1080p][English Dub][MultiSub]", "https://t/d"))
        self.assertIsNone(self.nyaa().movie(["Peli"], max_queries=1))


class CatalogSearchTest(NyaaHttp):
    def test_queries_and_failing_query_is_skipped(self):
        self.answers["Show 1080p"] = failure()
        self.answers["Show"] = response(item("[Erai-raws] Show - 01 [1080p][MultiSub]", "https://t/1"),
                                        item("[Erai-raws] Show - 02 [1080p][MultiSub]"))
        found = self.nyaa().catalog(["Show!", "show", "Sh"], {1})
        self.assertEqual({ep: r.link for ep, r in found.items()}, {1: "https://t/1"})
        self.assertEqual(self.queries, ["Show 1080p", "Show", "Sh 1080p", "Sh"])
