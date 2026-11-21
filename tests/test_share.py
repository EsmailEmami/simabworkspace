"""Share on your domain: https://DOMAIN/<project>/<name> → a port of the user's computer, through the web version."""
import gzip
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gui"))
sys.path.insert(0, str(ROOT / "tests"))
import server as sw  # noqa: E402
from test_onpc import Forwarder, wait_for  # noqa: E402
from test_gui_server import find_node  # noqa: E402

DOM = "projects.example.com"
MAIN = "workspace.example.com"


class Helpers(unittest.TestCase):
    def test_cookies_of_one_project_only(self):
        self.assertEqual(sw.share_cookies_in("esi.front.sid=1; mmd.front.sid=2; __simab_share=x; esi.front.a=b=c", "esi.front."),
                         "sid=1; a=b=c")
        self.assertEqual(sw.share_cookies_in("", "esi.front."), "")

    def test_set_cookie_gets_the_prefix_and_path(self):
        self.assertEqual(sw.share_cookie_out("sid=1; Path=/api; Domain=localhost; HttpOnly", "esi.front."),
                         "esi.front.sid=1; HttpOnly; Path=/")
        self.assertIsNone(sw.share_cookie_out("junk", "p."))

    def test_location(self):
        self.assertEqual(sw.share_location("http://localhost:3000/login?a=1", ""), "/login?a=1")
        self.assertEqual(sw.share_location("/login", "/esi/front"), "/esi/front/login")
        self.assertEqual(sw.share_location("https://accounts.google.com/x", "/esi/front"), "https://accounts.google.com/x")
        self.assertEqual(sw.share_location("//cdn.x/y", "/esi/front"), "//cdn.x/y")

    def test_inject_first_in_head(self):
        out = sw.share_inject(b"<!doctype html><html><HEAD lang=x><script src=/a.js></script></head>", "esi/front")
        self.assertTrue(out.startswith(b'<!doctype html><html><HEAD lang=x><script src="/__simab/shim.js" data-t="esi/front" data-p="{}"></script><script src=/a.js>'))
        self.assertIn(b'data-p="{&quot;6000&quot;:&quot;esi/api&quot;}"', sw.share_inject(b"<head>", "esi/front", {"6000": "esi/api"}))
        self.assertTrue(sw.share_inject(b"<!DOCTYPE html><p>x", "a/b").startswith(b"<!DOCTYPE html><script"))
        self.assertTrue(sw.share_inject(b"hello", "a/b").startswith(b"<script"))

    def test_domain_and_slug(self):
        self.assertEqual(sw.share_domain_clean("https://Projects.Example.com:443/x"), "projects.example.com")
        self.assertEqual(sw.share_slug("My Shop_App!"), "my-shop-app")
        self.assertTrue(sw.SHARE_SLUG_RE.fullmatch("esi"))
        self.assertFalse(sw.SHARE_SLUG_RE.fullmatch("-x"))


class DevServer(BaseHTTPRequestHandler):
    """What runs on the user's computer (localhost:3001 …)."""
    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, *a):
        pass

    def _out(self, code, body, ctype="text/plain", extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        DevServer.seen.append((self.command, self.path, dict(self.headers)))
        if self.path == "/":
            return self._out(200, b"<!doctype html><html><head><title>x</title></head><body>hi</body></html>",
                             "text/html", [("Content-Security-Policy", "script-src 'none'")])
        if self.path == "/assets/a.js":
            return self._out(200, b"console.log(1)", "text/javascript")
        if self.path == "/set":
            return self._out(200, b"ok", extra=[("Set-Cookie", "sid=1; Path=/api; Domain=localhost; HttpOnly")])
        if self.path == "/go":
            return self._out(302, b"", extra=[("Location", f"http://localhost:{self.server.server_address[1]}/login")])
        if self.path == "/gz":
            return self._out(200, gzip.compress(b"<html><head></head>gz</html>"), "text/html", [("Content-Encoding", "gzip")])
        if self.path == "/chunk":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for part in (b"ab", b"cd"):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
            self.wfile.write(b"0\r\n\r\n")
            return
        return self._out(200, json.dumps({"path": self.path, "headers": dict(self.headers)}).encode(), "application/json")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        self._out(200, (self.command + ":").encode() + body)

    do_PUT = do_POST


def ws_echo_server():
    """A port that answers a WebSocket upgrade and then sends back what it gets, in capitals."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            head = b""
            while not head.endswith(b"\r\n\r\n"):
                head += c.recv(1)
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            d = c.recv(100)
            c.sendall(d.upper())
            c.close()
    threading.Thread(target=serve, daemon=True).start()
    return srv


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.envp = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": t, "HOME": os.path.join(t, "home")})
        self.envp.start()
        os.makedirs(os.environ["HOME"])
        self.keygen = mock.patch.object(sw, "ensure_ssh_key", lambda p: None)
        self.keygen.start()
        # the user's computer: its app (onpc link) and a dev server
        self.store = sw.Store(Path(t) / "gui.json")
        self.pc = sw.App(self.store, sw.ServerConf("esi", "h"))
        self.pchttpd = sw.make_server(self.pc, 0)
        threading.Thread(target=self.pchttpd.serve_forever, daemon=True).start()
        c = self.pc.pc.cfg()
        self.dev = ThreadingHTTPServer(("127.0.0.1", 0), DevServer)
        self.ws = ws_echo_server()
        ok = lambda p: f"share port {p} on the internet"
        self.store.db.put("pc", dict(c, on=True, always={"esi": [ok(self.dev.server_address[1]), ok(self.ws.getsockname()[1]), ok(1)]}))
        self.secret = c["secret"]
        threading.Thread(target=self.dev.serve_forever, daemon=True).start()
        self.devport = self.dev.server_address[1]
        # the server's socket of the computer → its app (what ssh -R gives)
        self.fwd = Forwarder(os.path.join(t, "pc.sock"), self.pc.port)
        self.fwd.start()
        # the web version
        self.gate = sw.WebGate(Path(t) / "web", ["127.0.0.1:22"], verify=lambda c, pw: None, start_loops=False)
        self.gate.secure = False
        self.tok = self.gate.login("1.2.3.4", {"host": "127.0.0.1", "port": "22", "user": "esi", "password": "x"})
        self.wsid = self.gate.ws_of(self.tok)
        hub = self.hub = self.gate.share
        self.link = {"proc": None, "sock": self.fwd.path, "secret": self.secret, "port": self.pc.port, "used": 0, "active": 0}
        hub.link = lambda ws, app, dev: self.link
        hub.device = lambda app, project: "dev1"
        self.web = sw.make_web_server(self.gate, "127.0.0.1", 0)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        self.port = self.web.server_address[1]
        hub.api(self.wsid, "POST", "/domain", {"domain": DOM, "on": True}, MAIN)
        for name, port, pub in (("front", self.devport, True), ("api", self.devport, False), ("ws", self.ws.getsockname()[1], True),
                                ("off", 1, True)):
            hub.api(self.wsid, "POST", "/port", {"project": "esi", "name": name, "port": port, "public": pub, "on": True}, MAIN)
        DevServer.seen = []

    def tearDown(self):
        for s in (self.web, self.dev, self.pchttpd):
            s.shutdown()
            s.server_close()
        self.ws.close()
        self.fwd.close()
        self.gate.shutdown()
        self.keygen.stop()
        self.envp.stop()
        self.tmp.cleanup()

    def req(self, method, path, headers=None, body=None, host=DOM):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        c.request(method, path, body=body, headers=dict({"Host": host}, **(headers or {})))
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    SW = {"X-Simab-SW": "1"}
    NAV = {"X-Simab-SW": "1", "X-Simab-Nav": "1", "Sec-Fetch-Mode": "navigate"}

    def test_entry_installs_the_worker_and_remembers_the_project(self):
        r, d = self.req("GET", "/esi/front/page?x=1", {"Sec-Fetch-Mode": "navigate"})
        self.assertEqual(r.status, 200)
        self.assertIn(b"sessionStorage.setItem('__simabT', t)", d)
        self.assertIn(b'"esi/front"', d)
        self.assertIn(b'"/page?x=1"', d)
        self.assertIn("__simab_last=esi/front", r.getheader("Set-Cookie"))
        self.assertEqual(DevServer.seen, [], "the entry page does not reach the computer")
        r, d = self.req("GET", "/__simab/sw.js")
        self.assertEqual((r.status, r.getheader("Service-Worker-Allowed")), (200, "/"))
        self.assertIn(b"X-Simab-SW", d)

    def test_page_through_the_worker_gets_the_shim(self):
        r, d = self.req("GET", "/__simab/p/esi/front/", dict(self.NAV, **{"Accept-Encoding": "gzip"}))
        self.assertEqual(r.status, 200)
        self.assertIn(b'<head><script src="/__simab/shim.js" data-t="esi/front" data-p="', d)
        self.assertIn(b"&quot;:&quot;esi/front&quot;", d)
        self.assertNotIn(b"esi/api", d, "private ports are not listed for a browser that is not signed in")
        self.assertIsNone(r.getheader("Content-Security-Policy"))
        self.assertEqual(int(r.getheader("Content-Length")), len(d))
        _, path, hd = DevServer.seen[-1]
        self.assertEqual((path, hd["Host"], hd["Accept-Encoding"]), ("/", f"localhost:{self.devport}", "identity"))
        self.assertNotIn("X-Simab-SW", hd)
        r, d = self.req("GET", "/__simab/p/esi/front/gz", dict(self.NAV))
        self.assertIn(b"shim.js", d)
        self.assertIsNone(r.getheader("Content-Encoding"))

    def test_assets_cookies_and_bodies(self):
        r, d = self.req("GET", "/__simab/p/esi/front/assets/a.js", self.SW)
        self.assertEqual((r.status, d), (200, b"console.log(1)"))
        r, _ = self.req("GET", "/__simab/p/esi/front/set", self.SW)
        self.assertEqual(r.getheader("Set-Cookie"), "esi.sid=1; HttpOnly; Path=/")
        r, d = self.req("GET", "/__simab/p/esi/front/echo?q=1", dict(self.SW, Cookie="esi.sid=1; mmd.sid=2; __simab_share=zz",
                                                                    Origin=f"http://{DOM}", Referer=f"http://{DOM}/products"))
        hd = json.loads(d)["headers"]
        self.assertEqual(json.loads(d)["path"], "/echo?q=1")
        self.assertEqual(hd["Cookie"], "sid=1")
        self.assertEqual(hd["Origin"], f"http://localhost:{self.devport}")
        self.assertEqual(hd["Referer"], f"http://localhost:{self.devport}/products")
        self.assertEqual(hd["X-Forwarded-Host"], DOM)
        r, d = self.req("POST", "/__simab/p/esi/front/echo", self.SW, body=b"hello")
        self.assertEqual(d, b"POST:hello")
        r, d = self.req("PUT", "/esi/front/echo", body=b"x")          # an API client, straight to the entry path
        self.assertEqual(d, b"PUT:x")
        r, d = self.req("GET", "/__simab/p/esi/front/chunk", self.SW)
        self.assertEqual(d, b"abcd")

    def test_https_port_and_calls_from_the_panel(self):
        certs = Path(self.tmp.name, "certs")
        certs.mkdir()
        cert, key = sw.self_signed_cert(certs, "localhost")
        import ssl
        tls = ThreadingHTTPServer(("127.0.0.1", 0), DevServer)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        tls.socket = ctx.wrap_socket(tls.socket, server_side=True)
        threading.Thread(target=tls.serve_forever, daemon=True).start()
        self.addCleanup(tls.server_close)
        self.addCleanup(tls.shutdown)
        p = tls.server_address[1]
        self.store.db.put("pc", dict(self.pc.pc.cfg(), always={"esi": [f"share port {p} on the internet", f"share port {self.devport} on the internet"]}))
        self.hub.api(self.wsid, "POST", "/port", {"project": "esi", "name": "secure", "port": p, "tls": True, "public": True, "on": True}, MAIN)
        r, d = self.req("GET", "/__simab/p/esi/secure/echo", dict(self.SW, **{"X-Simab-From": "esi/front", "Origin": f"http://{DOM}"}))
        self.assertEqual(r.status, 200, d)
        hd = json.loads(d)["headers"]
        self.assertEqual(hd["Origin"], f"http://localhost:{self.devport}", "as if the panel on the computer called it")
        self.assertEqual(hd["Host"], f"localhost:{p}")
        self.hub.api(self.wsid, "POST", "/port", {"project": "esi", "name": "front", "port": p, "tls": False, "public": True, "on": True}, MAIN)
        r, d = self.req("GET", "/__simab/p/esi/front/echo", self.SW)
        self.assertEqual(r.status, 502, "plain HTTP to an HTTPS port")
        self.hub.api(self.wsid, "POST", "/port", {"project": "esi", "name": "secure", "port": self.devport, "tls": True, "public": True, "on": True}, MAIN)
        r, d = self.req("GET", "/__simab/p/esi/secure/echo", self.SW)
        self.assertEqual((r.status, d), (502, b"this port does not speak HTTPS \xe2\x80\x94 turn HTTPS off for it"))

    def test_ports_list(self):
        r, d = self.req("GET", "/__simab/ports?t=esi%2Ffront")
        self.assertEqual(sorted(x["t"] for x in json.loads(d)), ["esi/front", "esi/off", "esi/ws"], "private ones only when signed in")

    def test_redirects(self):
        r, _ = self.req("GET", "/__simab/p/esi/front/go", self.SW)
        self.assertEqual((r.status, r.getheader("X-Simab-Location")), (200, "/login"))
        r, _ = self.req("GET", "/esi/front/go")
        self.assertEqual((r.status, r.getheader("Location")), (302, "/esi/front/login"))

    def test_other_pages_and_fallback(self):
        r, d = self.req("GET", "/products", {"Sec-Fetch-Mode": "navigate"})
        self.assertEqual(r.status, 200)
        self.assertIn(b"sessionStorage.getItem('__simabT')", d)
        r, d = self.req("GET", "/assets/a.js", {"Cookie": "__simab_last=esi/front"})   # no service worker in this browser
        self.assertEqual(d, b"console.log(1)")
        r, _ = self.req("GET", "/assets/a.js")
        self.assertEqual(r.status, 404)
        r, d = self.req("GET", "/__simab/p/esi/nope/", self.SW)
        self.assertEqual(r.status, 404)

    def test_port_not_running(self):
        r, d = self.req("GET", "/__simab/p/esi/off/", self.NAV)
        self.assertEqual(r.status, 502)
        self.assertIn(b"nothing is running on port 1", d)
        self.assertIn(b"<html>", d, "a page for the browser")

    def test_a_new_port_is_asked_on_the_computer_first(self):
        x = socket.socket()
        x.bind(("127.0.0.1", 0))
        free = x.getsockname()[1]
        x.close()
        self.hub.api(self.wsid, "POST", "/port", {"project": "esi", "name": "db", "port": free, "public": True, "on": True}, MAIN)
        hub = self.pc.pc
        out = []
        ts = [threading.Thread(target=lambda: out.append(self.req("GET", "/__simab/p/esi/db/", self.SW)[1])) for _ in range(3)]
        for t in ts:
            t.start()
        self.assertTrue(wait_for(lambda: hub.asks))
        time.sleep(0.3)
        self.assertEqual([a["cmd"] for a in hub.asks.values()], [f"share port {free} on the internet"], "one question for all")
        hub.answer(next(iter(hub.asks)), "deny")
        for t in ts:
            t.join(15)
        self.assertEqual(out, [f"port {free} was not allowed on {hub.cfg()['name']}".encode()] * 3)
        def one():
            try:
                out.append(self.req("GET", "/__simab/p/esi/db/", self.SW)[0].status)
            except Exception as e:  # noqa: BLE001
                out.append(repr(e))
        th = threading.Thread(target=one)
        th.start()
        self.assertTrue(wait_for(lambda: hub.asks))
        hub.answer(next(iter(hub.asks)), "always")
        th.join(15)
        self.assertEqual(out[-1], 502, out)
        self.assertIn(f"share port {free} on the internet", hub.cfg()["always"]["esi"])

    def test_not_a_web_server(self):
        x = socket.socket()
        x.bind(("127.0.0.1", 0))
        x.listen(1)
        threading.Thread(target=lambda: x.accept()[0].close(), daemon=True).start()
        p = x.getsockname()[1]
        self.store.db.put("pc", dict(self.pc.pc.cfg(), always={"esi": [f"share port {p} on the internet"]}))
        self.hub.api(self.wsid, "POST", "/port", {"project": "esi", "name": "raw", "port": p, "public": True, "on": True}, MAIN)
        r, d = self.req("GET", "/__simab/p/esi/raw/", self.SW)
        x.close()
        self.assertEqual((r.status, d), (502, b"this port did not answer like a web server"))

    def test_older_app_on_the_computer(self):
        with mock.patch.object(sw.PcHub, "serve_proxy", lambda hub, h: h._json(404, {"error": "not found"})):
            r, d = self.req("GET", "/__simab/p/esi/front/", self.SW)
        self.assertEqual(r.status, 502)
        self.assertIn(b"update Simab Workspace on your computer", d)

    def test_wrong_secret_is_refused_by_the_computer(self):
        self.link = dict(self.link, secret="x" * 40)
        r, d = self.req("GET", "/__simab/p/esi/front/", self.SW)
        self.assertEqual(r.status, 502)
        self.assertIn(b"does not know this server user", d)

    def test_websocket(self):
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall(f"GET /__simab/p/esi/ws/hmr HTTP/1.1\r\nHost: {DOM}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  "Sec-WebSocket-Key: x\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        s.settimeout(10)
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            head += s.recv(1)
        self.assertIn(b" 101 ", head)
        s.sendall(b"frame")
        out = b""
        while len(out) < 5:
            out += s.recv(10)
        self.assertEqual(out, b"FRAME")
        s.close()

    def test_private_share_needs_a_sign_in_of_the_workspace(self):
        r, _ = self.req("GET", "/esi/api/x", {"Sec-Fetch-Mode": "navigate"})
        self.assertEqual(r.status, 302)
        loc = r.getheader("Location")
        self.assertTrue(loc.startswith(f"http://{MAIN}/__simab/share-auth?d={DOM}&to="), loc)
        r, _ = self.req("GET", "/__simab/p/esi/api/x", self.SW)
        self.assertEqual(r.status, 401)
        r, _ = self.req("GET", "/__simab/p/esi/api/x", self.NAV)
        self.assertTrue(r.getheader("X-Simab-Location").startswith(f"http://{MAIN}/__simab/share-auth"))
        # on the workspace's own domain: not signed in → a sign-in page
        path = loc.split(MAIN, 1)[1]
        r, d = self.req("GET", path, host=MAIN)
        self.assertEqual(r.status, 401)
        r, _ = self.req("GET", path, {"Cookie": f"{sw.WEB_COOKIE}={self.tok}"}, host=MAIN)
        self.assertEqual(r.status, 302)
        back = r.getheader("Location")
        self.assertTrue(back.startswith(f"http://{DOM}/__simab/auth?c="), back)
        r, _ = self.req("GET", back.split(DOM, 1)[1])
        self.assertEqual((r.status, r.getheader("Location")), (302, "/esi/api/x"))
        cookie = r.getheader("Set-Cookie").split(";")[0]
        self.assertTrue(cookie.startswith("__simab_share="))
        r, _ = self.req("GET", back.split(DOM, 1)[1])
        self.assertEqual(r.status, 403, "a code works once")
        r, d = self.req("GET", "/__simab/p/esi/api/x", dict(self.SW, Cookie=cookie))
        self.assertEqual((r.status, json.loads(d)["path"]), (200, "/x"))
        self.assertNotIn("Cookie", json.loads(d)["headers"], "the sign-in cookie never reaches the computer")
        self.assertFalse(self.hub.token_ok(cookie.split("=", 1)[1], self.wsid, "other.example.com"))

    def ask_share(self):
        self.hub.api(self.wsid, "POST", "/port", {"project": "esi", "name": "front", "port": self.devport, "access": "ask", "on": True}, MAIN)

    def visitor_asks(self):
        r, d = self.req("GET", "/esi/front/", {"Sec-Fetch-Mode": "navigate"})
        self.assertEqual(r.status, 200)
        self.assertIn(b"Asking the owner", d)
        self.assertIn(b"I am the owner", d)
        r, d = self.req("POST", "/__simab/ask", {"Content-Type": "application/json"}, body=json.dumps({"t": "esi/front"}))
        aid = json.loads(d)["id"]
        r, d = self.req("POST", "/__simab/ask", {"Content-Type": "application/json"}, body=json.dumps({"t": "esi/front"}))
        self.assertEqual(json.loads(d)["id"], aid, "the same visitor asks once")
        return aid

    def test_ask_me_answered_on_the_computer(self):
        self.ask_share()
        aid = self.visitor_asks()
        self.assertEqual([a["id"] for a in self.hub.api(self.wsid, "GET", "/asks", {}, MAIN)["asks"]], [aid], "on the web / phone")
        hub = self.pc.pc
        self.assertTrue(wait_for(lambda: hub.pending()["guests"]), "and on the computer")
        g = hub.pending()["guests"][0]
        self.assertEqual(g["id"], aid)
        self.assertIn("Someone wants to open esi/front", g["text"])
        r, d = self.req("GET", "/__simab/ask?id=" + aid)
        self.assertEqual(json.loads(d), {"state": "waiting"})
        self.pc.pc_api("POST", ["guest"], {"id": aid, "answer": "hour"})
        self.assertTrue(wait_for(lambda: self.hub.asks[aid]["answer"] == "hour"))
        r, d = self.req("GET", "/__simab/ask?id=" + aid)
        self.assertEqual(json.loads(d), {"state": "allowed"})
        cookie = r.getheader("Set-Cookie")
        self.assertTrue(cookie.startswith("__simab_g.esi.front="), cookie)
        self.assertIn("Max-Age=3600", cookie)
        r, _ = self.req("GET", "/__simab/ask?id=" + aid)
        self.assertIsNone(r.getheader("Set-Cookie"), "the cookie is given once")
        c = cookie.split(";")[0]
        r, d = self.req("GET", "/__simab/p/esi/front/assets/a.js", dict(self.SW, Cookie=c))
        self.assertEqual((r.status, d), (200, b"console.log(1)"))
        r, d = self.req("GET", "/__simab/p/esi/api/x", dict(self.SW, Cookie=c))
        self.assertEqual(r.status, 401, "only that link")
        st = self.hub.api(self.wsid, "GET", "", {}, MAIN)
        self.assertEqual([(g["t"], g["exp"] > time.time()) for g in st["guests"]], [("esi/front", True)])
        self.hub.api(self.wsid, "POST", "/revoke", {"id": st["guests"][0]["id"]}, MAIN)
        r, d = self.req("GET", "/__simab/p/esi/front/assets/a.js", dict(self.NAV, Cookie=c))
        self.assertIn(b"Asking the owner", d, "revoked: asks again")

    def test_ask_me_answered_on_the_phone(self):
        self.ask_share()
        aid = self.visitor_asks()
        hub = self.pc.pc
        self.assertTrue(wait_for(lambda: hub.pending()["guests"]))
        self.hub.api(self.wsid, "POST", "/answer", {"id": aid, "answer": "deny"}, MAIN)
        self.assertTrue(wait_for(lambda: not hub.pending()["guests"]), "the computer's question goes away")
        r, d = self.req("GET", "/__simab/ask?id=" + aid)
        self.assertEqual(json.loads(d), {"state": "denied"})
        self.assertEqual(self.hub.api(self.wsid, "GET", "/asks", {}, MAIN)["asks"], [])
        with self.assertRaises(sw.ApiError):
            self.hub.api(self.wsid, "POST", "/answer", {"id": aid, "answer": "maybe"}, MAIN)
        aid2 = self.visitor_asks()
        self.assertNotEqual(aid2, aid)
        self.hub.api(self.wsid, "POST", "/answer", {"id": aid2, "answer": "always"}, MAIN)
        r, _ = self.req("GET", "/__simab/ask?id=" + aid2)
        self.assertIn("Max-Age=34560000", r.getheader("Set-Cookie"))
        self.assertEqual(self.hub.api(self.wsid, "GET", "", {}, MAIN)["guests"][0]["exp"], 0)

    def test_owner_and_others(self):
        self.ask_share()
        r, _ = self.req("POST", "/__simab/ask", {"Content-Type": "application/json"}, body=json.dumps({"t": "esi/api"}))
        self.assertEqual(r.status, 404, "a private link does not ask")
        r, _ = self.req("GET", "/__simab/ask?id=nope")
        self.assertEqual(r.status, 404)
        self.hub.api(self.wsid, "POST", "/port", {"project": "esi", "name": "x1", "port": self.devport, "access": "ask", "on": True}, MAIN)
        for i in range(5):
            self.hub.asks[f"{i:012x}"] = {"id": f"{i:012x}", "ws": "other", "t": "a/b", "ip": "127.0.0.1", "until": time.time() + 99,
                                          "answer": "", "_ua": str(i)}
        r, d = self.req("POST", "/__simab/ask", {"Content-Type": "application/json"}, body=json.dumps({"t": "esi/x1"}))
        self.assertEqual(r.status, 429)

    def test_workspace_domain_is_unchanged(self):
        r, d = self.req("GET", "/api/web-info", host=MAIN)
        self.assertEqual(r.status, 200)
        r, _ = self.req("PUT", "/api/x", host=MAIN)
        self.assertEqual(r.status, 405)


class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gate = sw.WebGate(Path(self.tmp.name) / "web", ["127.0.0.1:22"], verify=lambda c, pw: None, start_loops=False)
        self.hub = self.gate.share

    def tearDown(self):
        self.gate.shutdown()
        self.tmp.cleanup()

    def test_domains(self):
        st = self.hub.api("a", "POST", "/domain", {"domain": "https://Projects.Example.com/", "on": True}, MAIN)
        self.assertEqual(st["domains"], [DOM])
        self.assertEqual(self.hub.domain(DOM + ":443")["ws"], "a")
        with self.assertRaises(sw.ApiError) as e:
            self.hub.api("b", "POST", "/domain", {"domain": DOM, "on": True}, MAIN)
        self.assertEqual(e.exception.status, 409)
        for bad in (MAIN, "127.0.0.1", "nodot", ""):
            with self.assertRaises(sw.ApiError):
                self.hub.api("a", "POST", "/domain", {"domain": bad, "on": True}, MAIN)
        self.hub.api("a", "POST", "/domain", {"domain": DOM, "on": False}, MAIN)
        self.assertIsNone(self.hub.domain(DOM))

    def test_ports(self):
        self.hub.api("a", "POST", "/domain", {"domain": DOM, "on": True}, MAIN)
        st = self.hub.api("a", "POST", "/port", {"project": "My Shop", "name": "Front", "port": "3001", "on": True}, MAIN)
        self.assertEqual(st["shares"], [{"slug": "my-shop", "name": "front", "project": "My Shop", "port": 3001, "public": False, "tls": False, "access": "private",
                                         "urls": [f"https://{DOM}/my-shop/front"]}])
        st = self.hub.api("a", "POST", "/port", {"project": "My Shop", "slug": "shop", "name": "front", "port": 3001, "public": True,
                                                 "old": {"slug": "my-shop", "name": "front"}, "on": True}, MAIN)
        self.assertEqual([(x["slug"], x["public"]) for x in st["shares"]], [("shop", True)])
        for bad in ({"name": "", "port": 1}, {"name": "x", "port": 0}, {"name": "x", "port": 70000}):
            with self.assertRaises(sw.ApiError):
                self.hub.api("a", "POST", "/port", dict({"project": "p", "on": True}, **bad), MAIN)
        self.assertEqual(self.hub.api("b", "GET", "", {}, MAIN), {"domains": [], "shares": [], "guests": []})
        st = self.hub.api("a", "POST", "/port", {"project": "p", "name": "q", "port": 1, "access": "ask", "on": True}, MAIN)
        self.assertEqual([x["access"] for x in st["shares"] if x["name"] == "q"], ["ask"])
        st = self.hub.api("a", "POST", "/port", {"slug": "shop", "name": "front", "on": False}, MAIN)
        self.assertEqual([x["name"] for x in st["shares"]], ["q"])

    def test_idle_links_close(self):
        killed = []
        with mock.patch.object(sw, "pc_kill", lambda p: killed.append(p)):
            self.hub.links = {("a", "d"): {"proc": "p1", "sock": "/nonexistent", "used": 0, "active": 0},
                              ("a", "e"): {"proc": "p2", "sock": "/nonexistent", "used": 0, "active": 1},
                              ("a", "f"): {"proc": "p3", "sock": "/nonexistent", "used": time.time(), "active": 0}}
            self.hub.reap()
        self.assertEqual(killed, ["p1"])
        self.assertEqual(sorted(self.hub.links), [("a", "e"), ("a", "f")])

    def test_link_argv(self):
        popen = mock.MagicMock()
        popen.return_value.poll.return_value = None
        self.hub.popen = popen
        app = mock.MagicMock()
        app.conf = sw.ServerConf("esi", "srv")
        made = {}

        def fake_popen(argv, **kw):
            made["argv"] = argv
            local = argv[argv.index("-L") + 1].split(":")[0]
            Path(local).touch()
            return popen.return_value
        self.hub.popen = fake_popen
        with mock.patch.object(sw, "remote_python", return_value={"sock": "/home/esi/.cache/simabworkspace/pc/dev1.sock", "secret": "s" * 40, "port": 15550}):
            ln = self.hub.link("ws1", app, "dev1")
            self.assertIs(self.hub.link("ws1", app, "dev1"), ln, "kept while it runs")
        self.assertIn("-N", made["argv"])
        self.assertTrue(made["argv"][made["argv"].index("-L") + 1].endswith(":/home/esi/.cache/simabworkspace/pc/dev1.sock"))
        self.assertEqual((ln["secret"], ln["port"]), ("s" * 40, 15550))
        self.assertEqual(oct(os.stat(Path(self.tmp.name) / "web" / "share").st_mode & 0o777), "0o700")


class Admin(unittest.TestCase):
    def test_link_op(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t, ".cache/simabworkspace/pc")
            d.mkdir(parents=True)
            (d / "abc.json").write_text(json.dumps({"id": "abc", "secret": "S", "port": 15550}))
            r = subprocess.run([sys.executable, "-c", sw.PC_ADMIN_PY, "link", "../abc"], env=dict(os.environ, HOME=t),
                               capture_output=True, text=True, timeout=20)
            self.assertEqual(json.loads(r.stdout), {"sock": str(d / "abc.sock"), "secret": "S", "port": 15550})


class BrowserScripts(unittest.TestCase):
    def test_shim_and_worker(self):
        node = find_node()
        if not node:
            self.skipTest("node not found")
        r = subprocess.run([node, str(ROOT / "tests" / "js" / "share_test.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
