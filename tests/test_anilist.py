"""AniList: cliente con caché y calma, y temporadas/series según la cadena de precuelas."""

from unittest import mock

import requests

from automator.library.anilist import AniListClient
from tests.helpers import AutomatorTestCase

REAL_QUERY = AniListClient.query   # helpers.AutomatorTestCase la sustituye por "AniList no sabe nada"


def media(mid, romaji, fmt="TV", episodes=12, english=None, synonyms=(), start=(2020, 4, 1), **relations):
    """Entrada de AniList; relations: prequel=[(id, formato)], sequel=..., parent=..."""
    edges = [{"relationType": rel.upper(), "node": {"id": nid, "type": "ANIME", "format": nfmt}}
             for rel, nodes in relations.items() for nid, nfmt in nodes]
    year, month, day = start if start else (None, None, None)
    return {"id": mid, "format": fmt, "episodes": episodes, "synonyms": list(synonyms),
            "title": {"romaji": romaji, "english": english},
            "startDate": {"year": year, "month": month, "day": day},
            "relations": {"edges": edges}}


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code, self.payload, self.headers = status, payload, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeAniList:
    """graphql.anilist.co: responde por id o por búsqueda con self.db; `replies` fuerza respuestas."""

    def __init__(self):
        self.db, self.searches, self.replies, self.calls = {}, {}, [], []

    def __call__(self, url, json, timeout):
        self.calls.append(json["variables"])
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        if "id" in json["variables"]:
            m = self.db.get(json["variables"]["id"])
            return FakeResponse(404) if m is None else FakeResponse(200, {"data": {"Media": m}})
        found = [self.db[i] for i in self.searches.get(json["variables"]["s"], [])]
        return FakeResponse(200, {"data": {"Page": {"media": found}}})


class AniListTestCase(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        self.api = FakeAniList()
        self.sleeps = []
        self._real = [mock.patch.object(AniListClient, "query", REAL_QUERY),
                      mock.patch("automator.library.anilist.requests.post", side_effect=self.api),
                      mock.patch("automator.library.anilist.time.sleep", side_effect=self.sleeps.append)]
        for p in self._real:
            p.start()

    def tearDown(self):
        for p in reversed(self._real):
            p.stop()
        super().tearDown()

    def add(self, *entries):
        for m in entries:
            self.api.db[m["id"]] = m

    def resolver(self):
        from automator.library.anilist import SeasonResolver
        return SeasonResolver()


class ClientTest(AniListTestCase):
    def test_media_and_search(self):
        self.add(media(1, "Uno"))
        self.api.searches["uno"] = [1]
        client = AniListClient()
        self.assertEqual(client.media(1)["title"]["romaji"], "Uno")
        self.assertIsNone(client.media(2))                     # 404: no existe
        self.assertEqual([m["id"] for m in client.search("uno")], [1])
        self.assertEqual(client.search("nada"), [])

    def test_answers_are_cached_and_persisted(self):
        self.add(media(1, "Uno"))
        client = AniListClient()
        client.media(1)
        client.media(1)
        client.media(2)
        client.media(2)                                        # "no existe" también se recuerda
        self.assertEqual(len(self.api.calls), 2)
        client.load_cache()
        self.assertEqual(len(client._cache), 2)
        from automator.core.singleton import Singleton
        Singleton.reset()
        fresh = AniListClient()
        fresh.load_cache()
        self.assertEqual(fresh.media(1)["id"], 1)
        self.assertEqual(len(self.api.calls), 2)

    def test_old_answers_are_asked_again(self):
        self.add(media(1, "Uno"))
        client = AniListClient()
        with mock.patch("automator.library.anilist.time.time", return_value=1_000_000.0):
            client.media(1)
            client.media(99)
        with mock.patch("automator.library.anilist.time.time", return_value=1_000_000.0 + 7 * 3600):
            client.media(1)                                    # positiva: dura una semana
            client.media(99)                                   # negativa: solo 6 horas
        self.assertEqual([c["id"] for c in self.api.calls], [1, 99, 99])

    def test_corrupt_cache_is_ignored(self):
        (self.root / "tmp" / "anilist_cache.json").write_text("{no es json")
        client = AniListClient()
        client.load_cache()
        self.assertEqual(client._cache, {})

    def test_waits_between_calls(self):
        client = AniListClient()
        client.media(1)
        self.assertEqual(self.sleeps, [])
        client.media(2)
        self.assertEqual(len(self.sleeps), 1)
        self.assertGreater(self.sleeps[0], 2)

    def test_rate_limit_is_retried_once(self):
        self.add(media(1, "Uno"))
        self.api.replies = [FakeResponse(429, headers={"Retry-After": "60"})]
        self.assertEqual(AniListClient().media(1)["id"], 1)
        self.assertEqual(self.sleeps, [30])

    def test_failures_raise_and_are_not_cached(self):
        from automator.library.anilist import AniListDown
        client = AniListClient()
        for reply in (FakeResponse(429), FakeResponse(429)):
            self.api.replies.append(reply)
        with self.assertRaises(AniListDown):
            client.media(1)
        self.api.replies = [requests.ConnectionError("caído")]
        with self.assertRaises(AniListDown):
            client.search("uno")
        self.api.replies = [FakeResponse(500)]
        with self.assertRaises(AniListDown):
            client.media(1)
        self.api.replies = [FakeResponse(200, ValueError("no es json"))]
        with self.assertRaises(AniListDown):
            client.media(1)
        self.add(media(1, "Uno"))
        self.assertEqual(client.media(1)["id"], 1)


class FindMediaTest(AniListTestCase):
    def setUp(self):
        super().setUp()
        self.add(media(1, "Shingeki no Kyojin", english="Attack on Titan", start=(2013, 4, 7)),
                 media(2, "Shingeki no Kyojin", fmt="OVA", start=(2013, 12, 9)),
                 media(3, "Otra cosa", start=(2013, 12, 9)),
                 media(4, "Sin fecha", start=None))
        self.api.searches["Shingeki no Kyojin"] = [3, 2, 1]

    def test_by_title_and_premiere(self):
        r = self.resolver()
        self.assertEqual(r.find_media("Shingeki no Kyojin", "2013-04-07")["id"], 1)
        self.assertEqual(r.find_media("Shingeki no Kyojin", "2013-12-09")["id"], 2)
        self.assertEqual(r.find_media("Shingeki no Kyojin")["id"], 2)

    def test_by_alternative_title(self):
        self.api.searches["SnK"] = [3, 1]
        self.assertEqual(self.resolver().find_media("SnK", alt_titles=["Attack on Titan!"])["id"], 1)

    def test_only_the_premiere_matches(self):
        self.api.searches["Titanes"] = [1, 3]
        self.assertEqual(self.resolver().find_media("Titanes", "2013-12-09")["id"], 3)
        self.assertIsNone(self.resolver().find_media("Titanes", "2001-01-01"))
        self.assertIsNone(self.resolver().find_media("Titanes"))

    def test_single_candidate_or_without_date(self):
        self.api.searches["Sin fecha"] = [4]
        self.assertEqual(self.resolver().find_media("Sin fecha", "2001-01-01")["id"], 4)
        self.api.searches["Shingeki"] = [1]
        self.assertEqual(self.resolver().find_media("Shingeki", "2001-01-01", ["Attack on Titan"])["id"], 1)


class SeasonInfoTest(AniListTestCase):
    def setUp(self):
        super().setUp()
        self.add(media(10, "Darker than Black: Kuro no Keiyakusha", episodes=25, sequel=[(11, "TV")]),
                 media(11, "Darker than Black: Ryuusei no Gemini", prequel=[(12, "OVA"), (10, "TV")]),
                 media(12, "Darker than Black: Kuro no Keiyakusha Gaiden", fmt="OVA", episodes=4,
                       prequel=[(10, "TV")]),
                 media(13, "Darker than Black: Gaiden TV", episodes=4, prequel=[(11, "TV")]))
        for mid in (10, 11, 13):
            self.api.searches[self.api.db[mid]["title"]["romaji"]] = [mid]

    def info(self, title):
        i = self.resolver().season_info(title)
        return i and (i["base"], i["season"], i["subtitle"], i["anilist_id"])

    def test_second_season_by_its_prequels(self):
        self.assertEqual(self.info("Darker than Black: Ryuusei no Gemini"),
                         ("Darker than Black", "2", "Ryuusei no Gemini", 11))

    def test_first_season_with_sequels(self):
        self.assertEqual(self.info("Darker than Black: Kuro no Keiyakusha"),
                         ("Darker than Black", "1", "Kuro no Keiyakusha", 10))

    def test_short_series_between_seasons(self):
        self.assertEqual(self.info("Darker than Black: Gaiden TV"), ("Darker than Black", "2.5", "Gaiden TV", 13))

    def test_sequel_reached_through_an_ova(self):
        self.add(media(20, "Nanatsu: Primera", sequel=[(21, "OVA")]),
                 media(21, "Nanatsu OVA", fmt="OVA", sequel=[(20, "TV"), (22, "SPECIAL")]),
                 media(22, "Nanatsu Special", fmt="SPECIAL", sequel=[(23, "TV")]))
        self.api.searches["Nanatsu: Primera"] = [20]
        self.assertEqual(self.info("Nanatsu: Primera"), ("Nanatsu", "1", "Primera", 20))

    def test_first_season_without_sequel_or_subtitle(self):
        self.add(media(30, "Sola: Una"), media(31, "Sin Subtitulo", sequel=[(32, "TV")]),
                 media(33, "Huérfana: Rota", sequel=[(34, "OVA")]))
        self.api.searches.update({"Sola: Una": [30], "Sin Subtitulo": [31], "Huérfana: Rota": [33]})
        self.assertIsNone(self.info("Sola: Una"))
        self.assertIsNone(self.info("Sin Subtitulo"))
        self.assertIsNone(self.info("Huérfana: Rota"))       # la secuela no existe en AniList

    def test_title_that_does_not_start_with_the_first_season(self):
        self.add(media(40, "Otro Nombre", prequel=[(10, "TV")]))
        self.api.searches["Otro Nombre"] = [40]
        self.assertIsNone(self.info("Otro Nombre"))

    def test_movies_are_not_seasons(self):
        self.add(media(50, "Darker than Black Movie", fmt="MOVIE", prequel=[(10, "TV")]))
        self.api.searches["Darker than Black Movie"] = [50]
        self.assertIsNone(self.info("Darker than Black Movie"))
        self.assertIsNone(self.info("No está en AniList"))

    def test_anilist_down_or_broken(self):
        self.api.replies = [requests.ConnectionError("caído")]
        self.assertIsNone(self.info("Darker than Black: Ryuusei no Gemini"))
        with mock.patch("automator.library.anilist.SeasonResolver.find_media", side_effect=KeyError("id")):
            self.assertIsNone(self.info("Darker than Black: Ryuusei no Gemini"))

    def test_prequel_loop_and_missing_prequel(self):
        self.add(media(60, "Bucle: Dos", prequel=[(61, "TV")]), media(61, "Bucle", prequel=[(60, "TV")]),
                 media(62, "Perdida: Dos", prequel=[(63, "TV")]))
        self.api.searches.update({"Bucle: Dos": [60], "Perdida: Dos": [62]})
        self.assertEqual(self.info("Bucle: Dos"), ("Bucle", "2", "Dos", 60))
        self.assertIsNone(self.info("Perdida: Dos"))

    def test_second_season_hanging_as_parent(self):
        self.add(media(70, "Horimiya"), media(71, "Horimiya: Piece", parent=[(70, "TV")]))
        self.api.searches["Horimiya: Piece"] = [71]
        self.assertEqual(self.info("Horimiya: Piece"), ("Horimiya", "2", "Piece", 71))


class SeriesInfoTest(AniListTestCase):
    def setUp(self):
        super().setUp()
        self.add(media(849, "Suzumiya Haruhi no Yuuutsu 2", episodes=14, prequel=[(848, "TV")]),
                 media(848, "Suzumiya Haruhi no Yuuutsu", episodes=14),
                 media(7311, "Suzumiya Haruhi no Shoushitsu", fmt="MOVIE", episodes=1, prequel=[(849, "TV")]),
                 media(900, "Haruhi-chan", fmt="ONA", episodes=25),
                 media(901, "Haruhi-chan Special", fmt="SPECIAL", episodes=1, parent=[(900, "ONA")]),
                 media(902, "Suelta", fmt="OVA", episodes=1),
                 media(903, "Rota", fmt="OVA", episodes=1, parent=[(999, "TV")]))
        for mid in (7311, 901, 902, 903, 848):
            self.api.searches[self.api.db[mid]["title"]["romaji"]] = [mid]

    def series(self, title):
        return self.resolver().series_info(title)

    def test_movie_of_a_series(self):
        self.assertEqual(self.series("Suzumiya Haruhi no Shoushitsu"),
                         {"series": "Suzumiya Haruhi no Yuuutsu", "format": "MOVIE"})

    def test_special_hanging_from_its_series(self):
        self.assertEqual(self.series("Haruhi-chan Special"), {"series": "Haruhi-chan", "format": "SPECIAL"})

    def test_no_series(self):
        self.assertIsNone(self.series("Suelta"))
        self.assertIsNone(self.series("Rota"))
        self.assertIsNone(self.series("Suzumiya Haruhi no Yuuutsu"))   # es la serie
        self.assertIsNone(self.series("No existe"))

    def test_anilist_down_or_broken(self):
        self.api.replies = [requests.ConnectionError("caído")]
        self.assertIsNone(self.series("Suzumiya Haruhi no Shoushitsu"))
        with mock.patch("automator.library.anilist.SeasonResolver.find_media", side_effect=KeyError("id")):
            self.assertIsNone(self.series("Suzumiya Haruhi no Shoushitsu"))


class PageFactsTest(AutomatorTestCase):
    def test_page_facts(self):
        from automator.library.anilist import page_facts
        text = "Títulos Alternativos Uno, Dos; Tres | Cuatro\nCinco, Seis\nFecha de Estreno 2009-10-08"
        self.assertEqual(page_facts(text), ("2009-10-08", ["Uno", "Dos", "Tres", "Cuatro", "Cinco"]))
        self.assertEqual(page_facts(None), ("", []))
