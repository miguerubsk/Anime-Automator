from unittest import mock

import requests

from tests.helpers import AutomatorTestCase

BLOCKED = "https://emision.example.com/0:down/Serie X/[Grupo][Web 1080p]/Serie.S01E07.mkv"


def cert_error(url=BLOCKED):
    e = requests.exceptions.SSLError(
        "HTTPSConnectionPool(host='emision.example.com', port=443): Max retries exceeded "
        "(Caused by SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] "
        "certificate verify failed: self-signed certificate')))")
    e.request = requests.Request("GET", url).prepare()
    return e


class DescribeErrorTest(AutomatorTestCase):
    def test_short_and_html_safe(self):
        from automator.core.errors import describe_error
        text = describe_error(cert_error())
        self.assertEqual(text, "certificado no válido en emision.example.com (¿IP bloqueada por LaLiga?)")
        self.assertEqual(describe_error(ValueError("a <b> & c")), "a &lt;b&gt; &amp; c")
        self.assertEqual(describe_error(ValueError("a <b>"), for_html=False), "a <b>")

    def test_link_is_quoted(self):
        from automator.pipeline.failures import link_to
        html = link_to(BLOCKED)
        self.assertIn('href="https://emision.example.com/0:down/Serie%20X/%5BGrupo%5D%5BWeb%201080p%5D/', html)
        self.assertEqual(link_to(None), "")


class BypassTest(AutomatorTestCase):
    def setUp(self):
        super().setUp()
        from automator.core.netblock import DnsBypass
        from automator.core.singleton import Singleton
        Singleton.reset(DnsBypass)
        self.addCleanup(self._restore)

    def _restore(self):
        import urllib3.util.connection as c
        from automator.core.netblock import DnsBypass
        from automator.core.singleton import Singleton
        inst = Singleton._instances.get(DnsBypass)
        if inst:
            c.create_connection = inst._original
        Singleton.reset(DnsBypass)

    def test_switches_to_free_ip(self):
        from automator.core import netblock
        calls = []

        def fake_get(url, **kw):
            pinned = netblock.DnsBypass().pinned("emision.example.com")
            calls.append((pinned, kw.get("proxies")))
            if pinned != "2.2.2.2":
                raise cert_error()
            return "ok"

        with mock.patch.object(netblock.DnsBypass, "resolve", return_value=["1.1.1.9", "2.2.2.2"]), \
             mock.patch.object(netblock.DnsBypass, "_works", lambda self, h, ip: ip == "2.2.2.2"), \
             mock.patch.object(netblock.requests, "get", side_effect=fake_get):
            self.assertEqual(netblock.get_with_bypass(BLOCKED, {}), "ok")
        self.assertEqual(calls[-1], ("2.2.2.2", None))

    def test_falls_back_to_proxy(self):
        from automator.core import netblock
        cfg = {"blocked_ip_bypass": {"proxy": "socks5h://tor:9050"}}

        def fake_get(url, **kw):
            if not kw.get("proxies"):
                raise cert_error()
            return "por tor"

        with mock.patch.object(netblock.DnsBypass, "resolve", return_value=[]), \
             mock.patch.object(netblock.requests, "get", side_effect=fake_get) as g:
            self.assertEqual(netblock.get_with_bypass(BLOCKED, cfg), "por tor")
            # el siguiente va directo por el proxy
            self.assertEqual(netblock.get_with_bypass(BLOCKED, cfg), "por tor")
        self.assertEqual(g.call_count, 3)

    def test_other_ssl_errors_are_not_bypassed(self):
        from automator.core import netblock
        with mock.patch.object(netblock.requests, "get", side_effect=requests.exceptions.SSLError("handshake")), \
             mock.patch.object(netblock.DnsBypass, "resolve") as resolve:
            with self.assertRaises(requests.exceptions.SSLError):
                netblock.get_with_bypass(BLOCKED, {"blocked_ip_bypass": {"proxy": "socks5h://tor:9050"}})
        resolve.assert_not_called()
