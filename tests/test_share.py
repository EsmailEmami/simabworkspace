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

DOM = "apps.example.com"
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
        self.assertEqual(sw.share_location("/login", "/esi/esi/front"), "/esi/esi/front/login")
        self.assertEqual(sw.share_location("https://accounts.google.com/x", "/esi/esi/front"), "https://accounts.google.com/x")
        self.assertEqual(sw.share_location("//cdn.x/y", "/esi/esi/front"), "//cdn.x/y")

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
        self.gate = sw.WebGate(Path(t) / "web", ["127.0.0.1:22"], verify=lambda c, pw: None, start_loops=False, share_domain=DOM)
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
        hub.note_main(MAIN)
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
        r, d = self.req("GET", "/esi/esi/front/page?x=1", {"Sec-Fetch-Mode": "navigate"})
        self.assertEqual(r.status, 200)
        self.assertIn(b"sessionStorage.setItem('__simabT', t)", d)
        self.assertIn(b'"esi/esi/front"', d)
        self.assertIn(b'"/page?x=1"', d)
        self.assertIn("__simab_last=esi/esi/front", r.getheader("Set-Cookie"))
        self.assertEqual(DevServer.seen, [], "the entry page does not reach the computer")
        r, d = self.req("GET", "/__simab/sw.js")
        self.assertEqual((r.status, r.getheader("Service-Worker-Allowed")), (200, "/"))
        self.assertIn(b"X-Simab-SW", d)

    def test_page_through_the_worker_gets_the_shim(self):
        r, d = self.req("GET", "/__simab/p/esi/esi/front/", dict(self.NAV, **{"Accept-Encoding": "gzip"}))
        self.assertEqual(r.status, 200)
        self.assertIn(b'<head><script src="/__simab/shim.js" data-t="esi/esi/front" data-p="', d)
        self.assertIn(b"&quot;:&quot;esi/esi/front&quot;", d)
        self.assertNotIn(b"esi/api", d, "private ports are not listed for a browser that is not signed in")
        self.assertIsNone(r.getheader("Content-Security-Policy"))
        self.assertEqual(int(r.getheader("Content-Length")), len(d))
        _, path, hd = DevServer.seen[-1]
        self.assertEqual((path, hd["Host"], hd["Accept-Encoding"]), ("/", f"localhost:{self.devport}", "identity"))
        self.assertNotIn("X-Simab-SW", hd)
        r, d = self.req("GET", "/__simab/p/esi/esi/front/gz", dict(self.NAV))
        self.assertIn(b"shim.js", d)
        self.assertIsNone(r.getheader("Content-Encoding"))

    def test_assets_cookies_and_bodies(self):
        r, d = self.req("GET", "/__simab/p/esi/esi/front/assets/a.js", self.SW)
        self.assertEqual((r.status, d), (200, b"console.log(1)"))
        r, _ = self.req("GET", "/__simab/p/esi/esi/front/set", self.SW)
        self.assertEqual(r.getheader("Set-Cookie"), "esi.esi.sid=1; HttpOnly; Path=/")
        r, d = self.req("GET", "/__simab/p/esi/esi/front/echo?q=1", dict(self.SW, Cookie="esi.esi.sid=1; mmd.x.sid=2; __simab_share.esi=zz",
                                                                    Origin=f"http://{DOM}", Referer=f"http://{DOM}/products"))
        hd = json.loads(d)["headers"]
        self.assertEqual(json.loads(d)["path"], "/echo?q=1")
        self.assertEqual(hd["Cookie"], "sid=1")
        self.assertEqual(hd["Origin"], f"http://localhost:{self.devport}")
        self.assertEqual(hd["Referer"], f"http://localhost:{self.devport}/products")
        self.assertEqual(hd["X-Forwarded-Host"], DOM)
        r, d = self.req("POST", "/__simab/p/esi/esi/front/echo", self.SW, body=b"hello")
        self.assertEqual(d, b"POST:hello")
        r, d = self.req("PUT", "/esi/esi/front/echo", body=b"x")          # an API client, straight to the entry path
        self.assertEqual(d, b"PUT:x")
        r, d = self.req("GET", "/__simab/p/esi/esi/front/chunk", self.SW)
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
        r, d = self.req("GET", "/__simab/p/esi/esi/secure/echo", dict(self.SW, **{"X-Simab-From": "esi/esi/front", "Origin": f"http://{DOM}"}))
        self.assertEqual(r.status, 200, d)
        hd = json.loads(d)["headers"]
        self.assertEqual(hd["Origin"], f"http://localhost:{self.devport}", "as if the panel on the computer called it")
        self.assertEqual(hd["Host"], f"localhost:{p}")
        self.hub.api(self.wsid, "POST", "/port", {"project": "esi", "name": "front", "port": p, "tls": False, "public": True, "on": True}, MAIN)
        r, d = self.req("GET", "/__simab/p/esi/esi/front/echo", self.SW)
        self.assertEqual(r.status, 502, "plain HTTP to an HTTPS port")
        self.hub.api(self.wsid, "POST", "/port", {"project": "esi", "name": "secure", "port": self.devport, "tls": True, "public": True, "on": True}, MAIN)
        r, d = self.req("GET", "/__simab/p/esi/esi/secure/echo", self.SW)
        self.assertEqual((r.status, d), (502, b"this port does not speak HTTPS \xe2\x80\x94 turn HTTPS off for it"))

    def test_ports_list(self):
        r, d = self.req("GET", "/__simab/ports?t=esi%2Fesi%2Ffront")
        self.assertEqual(sorted(x["t"] for x in json.loads(d)), ["esi/esi/front", "esi/esi/off", "esi/esi/ws"], "private ones only when signed in")

    def test_redirects(self):
        r, _ = self.req("GET", "/__simab/p/esi/esi/front/go", self.SW)
        self.assertEqual((r.status, r.getheader("X-Simab-Location")), (200, "/login"))
        r, _ = self.req("GET", "/esi/esi/front/go")
        self.assertEqual((r.status, r.getheader("Location")), (302, "/esi/esi/front/login"))

    def test_other_pages_and_fallback(self):
        r, d = self.req("GET", "/products", {"Sec-Fetch-Mode": "navigate"})
        self.assertEqual(r.status, 200)
        self.assertIn(b"sessionStorage.getItem('__simabT')", d)
        self.assertIn(b"if (!t) t = null;", d)
        r, d = self.req("GET", "/portal", {"Sec-Fetch-Mode": "navigate", "Cookie": "__simab_last=esi/esi/front"})
        self.assertIn(b'if (!t) t = "esi/esi/front";', d, "a new tab (no sessionStorage) goes to the last project opened here")
        r, d = self.req("GET", "/assets/a.js", {"Cookie": "__simab_last=esi/esi/front"})   # no service worker in this browser
        self.assertEqual(d, b"console.log(1)")
        r, _ = self.req("GET", "/assets/a.js")
        self.assertEqual(r.status, 404)
        r, d = self.req("GET", "/__simab/p/esi/esi/nope/", self.SW)
        self.assertEqual(r.status, 404)

    def test_port_not_running(self):
        r, d = self.req("GET", "/__simab/p/esi/esi/off/", self.NAV)
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
        ts = [threading.Thread(target=lambda: out.append(self.req("GET", "/__simab/p/esi/esi/db/", self.SW)[1])) for _ in range(3)]
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
                out.append(self.req("GET", "/__simab/p/esi/esi/db/", self.SW)[0].status)
            except Exception as e:  # noqa: BLE001
                out.append(repr(e))
        th = threading.Thread(target=one)
        th.start()
        self.assertTrue(wait_for(lambda: hub.asks))
        hub.answer(next(iter(hub.asks)), "always")
        th.join(15)
        self.assertEqual(out[-1], 502, out)
        self.assertIn(f"share port {free} on the internet", hub.cfg()["always"]["esi"])
        hub.cfg()
        self.store.db.put("pc", dict(hub.cfg(), always={}))
        for ans in ("once", "hour"):                 # one answer is enough for the whole page (many requests)
            hub.share_ok.clear()
            hub.hour.clear()
            th = threading.Thread(target=one)
            th.start()
            self.assertTrue(wait_for(lambda: hub.asks))
            hub.answer(next(iter(hub.asks)), ans)
            th.join(15)
            for _ in range(3):
                one()
            self.assertEqual(out[-4:], [502] * 4, ans)
            self.assertEqual(hub.asks, {})

    def test_not_a_web_server(self):
        x = socket.socket()
        x.bind(("127.0.0.1", 0))
        x.listen(1)
        threading.Thread(target=lambda: x.accept()[0].close(), daemon=True).start()
        p = x.getsockname()[1]
        self.store.db.put("pc", dict(self.pc.pc.cfg(), always={"esi": [f"share port {p} on the internet"]}))
        self.hub.api(self.wsid, "POST", "/port", {"project": "esi", "name": "raw", "port": p, "public": True, "on": True}, MAIN)
        r, d = self.req("GET", "/__simab/p/esi/esi/raw/", self.SW)
        x.close()
        self.assertEqual((r.status, d), (502, b"this port did not answer like a web server"))

    def test_older_app_on_the_computer(self):
        with mock.patch.object(sw.PcHub, "serve_proxy", lambda hub, h: h._json(404, {"error": "not found"})):
            r, d = self.req("GET", "/__simab/p/esi/esi/front/", self.SW)
        self.assertEqual(r.status, 502)
        self.assertIn(b"update Simab Workspace on your computer", d)

    def test_status_through_the_computer(self):
        st = self.hub.probe(self.wsid, None, [self.hub.find(self.wsid, "esi", n) for n in ("front", "off", "ws")])
        self.assertEqual(st, {"esi/front": "on", "esi/off": "port-off", "esi/ws": "on"})
        self.hub.device = lambda app, project: (_ for _ in ()).throw(sw.ApiError(503, "offline"))
        self.assertEqual(self.hub.probe(self.wsid, None, [self.hub.find(self.wsid, "esi", "front")]), {"esi/front": "pc-off"})

    def test_wrong_secret_is_refused_by_the_computer(self):
        self.link = dict(self.link, secret="x" * 40)
        r, d = self.req("GET", "/__simab/p/esi/esi/front/", self.SW)
        self.assertEqual(r.status, 502)
        self.assertIn(b"does not know this server user", d)

    def test_websocket(self):
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall(f"GET /__simab/p/esi/esi/ws/hmr HTTP/1.1\r\nHost: {DOM}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
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
        r, _ = self.req("GET", "/esi/esi/api/x", {"Sec-Fetch-Mode": "navigate"})
        self.assertEqual(r.status, 302)
        loc = r.getheader("Location")
        self.assertTrue(loc.startswith(f"http://{MAIN}/__simab/share-auth?u=esi&to="), loc)
        r, _ = self.req("GET", "/__simab/p/esi/esi/api/x", self.SW)
        self.assertEqual(r.status, 401)
        r, _ = self.req("GET", "/__simab/p/esi/esi/api/x", self.NAV)
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
        self.assertEqual((r.status, r.getheader("Location")), (302, "/esi/esi/api/x"))
        cookie = r.getheader("Set-Cookie").split(";")[0]
        self.assertTrue(cookie.startswith("__simab_share.esi="))
        r, _ = self.req("GET", back.split(DOM, 1)[1])
        self.assertEqual(r.status, 403, "a code works once")
        r, d = self.req("GET", "/__simab/p/esi/esi/api/x", dict(self.SW, Cookie=cookie))
        self.assertEqual((r.status, json.loads(d)["path"]), (200, "/x"))
        self.assertNotIn("Cookie", json.loads(d)["headers"], "the sign-in cookie never reaches the computer")
        self.assertFalse(self.hub.token_ok(cookie.split("=", 1)[1], self.wsid, "other.example.com"))
        on = self.hub.live(self.wsid)["active"]
        self.assertEqual([(x["t"], x["who"]) for x in on], [("esi/api", "you")], "online now")

    def ask_share(self):
        self.hub.api(self.wsid, "POST", "/port", {"project": "esi", "name": "front", "port": self.devport, "access": "ask", "on": True}, MAIN)

    def visitor_asks(self):
        r, d = self.req("GET", "/esi/esi/front/", {"Sec-Fetch-Mode": "navigate"})
        self.assertEqual(r.status, 200)
        self.assertIn(b"Asking the owner", d)
        self.assertIn(b"I am the owner", d)
        r, d = self.req("POST", "/__simab/ask", {"Content-Type": "application/json"}, body=json.dumps({"t": "esi/esi/front"}))
        aid = json.loads(d)["id"]
        r, d = self.req("POST", "/__simab/ask", {"Content-Type": "application/json"}, body=json.dumps({"t": "esi/esi/front"}))
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
        self.assertTrue(cookie.startswith("__simab_g.esi.esi.front="), cookie)
        self.assertIn("Max-Age=3600", cookie)
        r, _ = self.req("GET", "/__simab/ask?id=" + aid)
        self.assertIsNone(r.getheader("Set-Cookie"), "the cookie is given once")
        c = cookie.split(";")[0]
        r, d = self.req("GET", "/__simab/p/esi/esi/front/assets/a.js", dict(self.SW, Cookie=c))
        self.assertEqual((r.status, d), (200, b"console.log(1)"))
        r, d = self.req("GET", "/__simab/p/esi/esi/api/x", dict(self.SW, Cookie=c))
        self.assertEqual(r.status, 401, "only that link")
        st = self.hub.api(self.wsid, "GET", "", {}, MAIN)
        self.assertEqual([(g["t"], g["exp"] > time.time()) for g in st["guests"]], [("esi/front", True)])
        gid = st["guests"][0]["id"]
        self.assertEqual([x["who"] for x in self.hub.live(self.wsid)["active"]], ["guest"])
        self.pc.pc.share_revoke(gid)                                       # revoked in the app: the computer refuses at once
        r, d = self.req("GET", "/__simab/p/esi/esi/front/assets/a.js", dict(self.SW, Cookie=c))
        self.assertEqual((r.status, d), (502, b"your access to this link was taken back"))
        self.assertIn(gid, self.pc.pc.cfg()["shareRevoked"])
        self.pc.pc.cfg()
        self.store.db.put("pc", dict(self.pc.pc.cfg(), shareRevoked=[]))
        held = socket.socket()
        self.hub.conns = {gid: {held}}
        self.hub.api(self.wsid, "POST", "/revoke", {"id": gid}, MAIN)
        self.assertEqual(self.hub.live(self.wsid)["active"], [], "no longer online")
        self.assertNotIn(gid, self.hub.conns, "its open connections closed")
        held.close()
        r, d = self.req("GET", "/__simab/p/esi/esi/front/assets/a.js", dict(self.NAV, Cookie=c))
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
        r, _ = self.req("POST", "/__simab/ask", {"Content-Type": "application/json"}, body=json.dumps({"t": "esi/esi/api"}))
        self.assertEqual(r.status, 404, "a private link does not ask")
        r, _ = self.req("GET", "/__simab/ask?id=nope")
        self.assertEqual(r.status, 404)
        self.hub.api(self.wsid, "POST", "/port", {"project": "esi", "name": "x1", "port": self.devport, "access": "ask", "on": True}, MAIN)
        for i in range(5):
            self.hub.asks[f"{i:012x}"] = {"id": f"{i:012x}", "ws": "other", "t": "a/b", "ip": "127.0.0.1", "until": time.time() + 99,
                                          "answer": "", "_ua": str(i)}
        r, d = self.req("POST", "/__simab/ask", {"Content-Type": "application/json"}, body=json.dumps({"t": "esi/esi/x1"}))
        self.assertEqual(r.status, 429)

    def test_page_errors_go_to_the_log(self):
        with mock.patch.object(sw, "log_line") as log:
            r, _ = self.req("POST", "/__simab/log", body=json.dumps({"t": "esi/esi/front", "at": "/x", "msg": "boom\nline2"}))
        self.assertEqual(r.status, 204)
        log.assert_called_once_with("share page esi/esi/front /x: boom line2")

    def test_workspace_domain_is_unchanged(self):
        r, d = self.req("GET", "/api/web-info", host=MAIN)
        self.assertEqual(r.status, 200)
        r, _ = self.req("PUT", "/api/x", host=MAIN)
        self.assertEqual(r.status, 405)


class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gate = sw.WebGate(Path(self.tmp.name) / "web", ["127.0.0.1:22"], verify=lambda c, pw: None, start_loops=False, share_domain=DOM)
        self.hub = self.gate.share

    def tearDown(self):
        self.gate.shutdown()
        self.tmp.cleanup()

    def test_apps_address(self):
        self.assertTrue(self.hub.is_host(DOM + ":443") and not self.hub.is_host(MAIN))
        with self.assertRaises(sw.ApiError) as e:
            self.hub.api("a", "POST", "/domain", {"domain": "x.example.com", "on": True}, MAIN)
        self.assertEqual(e.exception.status, 404, "users do not add domains: the admin sets one for everybody")
        self.hub.note_main(MAIN + ":550")
        self.hub.note_main(DOM)
        self.assertEqual(self.hub.main_host, MAIN + ":550", "never the apps address itself")
        self.assertEqual(sw.ShareHub(self.gate).main_host, MAIN + ":550", "kept")
        self.assertEqual(self.hub._split("/esi/shop/front/a/b"), ("esi", "shop", "front", "/a/b"))
        self.assertEqual(self.hub._split("/esi/shop"), ("", "", "", ""))
        self.assertEqual(self.hub._split("/../shop/front"), ("", "", "", ""))

    def test_ports(self):
        st = self.hub.api("a", "POST", "/port", {"project": "My Shop", "name": "Front", "port": "3001", "on": True}, MAIN)
        self.assertEqual(st["shares"], [{"slug": "my-shop", "name": "front", "project": "My Shop", "port": 3001, "public": False, "tls": False, "access": "private",
                                         "urls": []}], "no user for this workspace: no link")
        st = self.hub.api("a", "POST", "/port", {"project": "My Shop", "slug": "shop", "name": "front", "port": 3001, "public": True,
                                                 "old": {"slug": "my-shop", "name": "front"}, "on": True}, MAIN)
        self.assertEqual([(x["slug"], x["public"]) for x in st["shares"]], [("shop", True)])
        for bad in ({"name": "", "port": 1}, {"name": "x", "port": 0}, {"name": "x", "port": 70000}):
            with self.assertRaises(sw.ApiError):
                self.hub.api("a", "POST", "/port", dict({"project": "p", "on": True}, **bad), MAIN)
        self.assertEqual(self.hub.api("b", "GET", "", {}, MAIN), {"base": "", "shares": [], "guests": []})
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


def local_remote_python(home):
    """remote_python that runs the script here, with HOME = the server user's home."""
    def run(conf, code, args, stdin=None, **kw):
        r = subprocess.run([sys.executable, "-c", code] + list(args), input=stdin, capture_output=True, timeout=20,
                           env=dict(os.environ, HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config")))
        return json.loads(r.stdout)
    return run


class SharedSettings(unittest.TestCase):
    """The list lives in share.json on the server: the app and the web version change the same one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "server-home")
        os.makedirs(self.home)
        self.envp = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(self.tmp.name, "laptop")})
        self.envp.start()
        self.rp = mock.patch.object(sw, "remote_python", side_effect=local_remote_python(self.home))
        self.rp.start()
        self.keygen = mock.patch.object(sw, "ensure_ssh_key", lambda p: None)
        self.keygen.start()
        self.app = sw.App(sw.Store(Path(self.tmp.name) / "gui.json"), sw.ServerConf("esi", "h"))
        self.gate = sw.WebGate(Path(self.tmp.name) / "web", ["127.0.0.1:22"], verify=lambda c, pw: None, start_loops=False, share_domain=DOM)
        tok = self.gate.login("1.2.3.4", {"host": "127.0.0.1", "port": "22", "user": "esi", "password": "x"})
        self.ws = self.gate.ws_of(tok)
        self.web = self.gate.app_for(tok)

    def tearDown(self):
        self.gate.shutdown()
        self.keygen.stop()
        self.rp.stop()
        self.envp.stop()
        self.tmp.cleanup()

    def test_app_edits_the_web_follows(self):
        with self.assertRaises(sw.ApiError):
            self.app.share_api("POST", "/domain", {"domain": "x.example.com", "on": True})
        st = self.app.share_api("POST", "/port", {"project": "avahesab", "name": "panel", "port": 6001, "access": "ask", "on": True})
        self.assertEqual((st["shares"][0]["urls"], st["web"]), ([], False), "no link until the web version said its address")
        f = Path(self.home, ".config/simabworkspace/share.json")
        self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(f.read_text())["shares"][0]["access"], "ask")
        self.assertIsNone(self.gate.share.find(self.ws, "avahesab", "panel"), "not yet: the web version reads it every 30 s")
        self.gate.share.pull(self.ws, self.web)
        self.assertEqual(self.gate.share.find(self.ws, "avahesab", "panel")["access"], "ask")
        st = self.app.share_api("GET", "", {})
        self.assertEqual(st["base"], f"https://{DOM}/esi")
        self.assertEqual(st["shares"][0]["urls"], [f"https://{DOM}/esi/avahesab/panel"], "the app shows the link")
        # the web version edits: the file and its tables at once
        st = self.web.share_api("POST", "/port", {"project": "avahesab", "name": "api", "port": 6000, "tls": True, "on": True}, MAIN)
        self.assertEqual([x["name"] for x in st["shares"]], ["api", "panel"])
        self.assertTrue(self.gate.share.find(self.ws, "avahesab", "api")["tls"])
        self.assertEqual(self.gate.share.main_host, MAIN)
        self.app.share_api("POST", "/port", {"slug": "avahesab", "name": "api", "on": False})
        self.gate.share.pull(self.ws, self.web)
        self.assertIsNone(self.gate.share.find(self.ws, "avahesab", "api"))

    def test_revoke_from_the_app_and_live_for_the_app(self):
        self.gate.share.api(self.ws, "POST", "/port", {"project": "p", "name": "f", "port": 3001, "access": "ask", "on": True}, MAIN)
        now = time.time()
        self.gate.share.db.run("INSERT INTO web_share_guests (hash, ws, slug, name, label, ip, created, exp, last) VALUES (?, ?, 'p', 'f', 'Chrome', '1.1.1.1', ?, 0, ?)",
                               ("ab" * 32, self.ws, now, now))
        self.gate.share.pull(self.ws, self.web)
        st = self.app.share_api("GET", "", {})
        self.assertEqual([g["id"] for g in st["live"]["guests"]], ["ab" * 8], "the app sees who was let in")
        st = self.app.share_api("POST", "/revoke", {"id": "ab" * 8})
        self.assertEqual(st["live"]["guests"], [], "gone at once in the app")
        self.assertEqual(len(self.gate.share.state(self.ws)["guests"]), 1, "the web version does it on its next read")
        self.gate.share.pull(self.ws, self.web)
        self.assertEqual(self.gate.share.state(self.ws)["guests"], [])
        self.assertEqual(self.app.share_data()["revoke"], [], "done: the list is emptied")
        self.assertEqual(self.app.share_api("GET", "", {})["live"]["guests"], [], "and the app's view says so")
        st = self.web.share_api("GET", "", {}, MAIN)
        self.assertEqual(set(st["live"]), {"asks", "guests", "active", "at"})

    def test_first_read_keeps_what_the_web_had(self):
        self.gate.share.api(self.ws, "POST", "/port", {"project": "p", "name": "f", "port": 3001, "on": True}, MAIN)
        self.gate.share.pull(self.ws, self.web)
        st = self.app.share_api("GET", "", {})
        self.assertEqual(([x["name"] for x in st["shares"]], st["base"]), (["f"], f"https://{DOM}/esi"))

    def test_bad_entries_are_dropped(self):
        Path(self.home, ".config/simabworkspace").mkdir(parents=True)
        Path(self.home, ".config/simabworkspace/share.json").write_text(json.dumps({"base": "javascript:x", "shares": [
            {"slug": "a", "name": "b", "port": "x"}, {"slug": "a", "name": "c", "port": 5, "access": "weird"}]}))
        self.gate.share.pull(self.ws, self.web)
        self.assertEqual(self.gate.share.find(self.ws, "a", "c")["access"], "private")
        self.assertIsNone(self.gate.share.find(self.ws, "a", "b"))
        self.assertEqual(self.app.share_api("GET", "", {})["base"], f"https://{DOM}/esi")

    def test_status_on_this_computer(self):
        x = socket.socket()
        x.bind(("127.0.0.1", 0))
        x.listen(1)
        self.addCleanup(x.close)
        y = socket.socket()
        y.bind(("127.0.0.1", 0))
        free = y.getsockname()[1]
        y.close()
        for name, port in (("up", x.getsockname()[1]), ("down", free)):
            self.app.share_api("POST", "/port", {"project": "p", "name": name, "port": port, "on": True})
        self.assertEqual(self.app.share_api("GET", "/status", {})["status"], {"p/up": "pc-off", "p/down": "pc-off"})
        self.app.store.db.put("pc", dict(self.app.pc.cfg(), on=True))
        self.assertEqual(self.app.share_api("GET", "/status", {})["status"], {"p/up": "on", "p/down": "port-off"})


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


class ProjectDialog(unittest.TestCase):
    def test_project_share_dialog_lists_its_shares(self):
        js = (ROOT / "gui" / "static" / "app.js").read_text()
        add = js[js.index("function shareAddDialog"):js.index("function shareProjList")]
        self.assertIn("project ? shareProjList(project", add)        # the ⋯ menu dialog shows what is shared already
        lst = js[js.index("function shareProjList"):js.index("function shareRows")]
        self.assertIn("x.project === project", lst)
        self.assertIn("shareDialog(x)", lst)


class PortStop(unittest.TestCase):
    def test_pc_port(self):
        self.assertEqual(sw.pc_port("http://localhost:6000/"), 6000)
        self.assertEqual(sw.pc_port("https://x.dev/"), 443)
        self.assertEqual(sw.pc_port("nonsense"), 0)

    @unittest.skipIf(sw.IS_WIN, "posix")
    def test_stop_port_kills_the_listener(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        p = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(wait_for(lambda: sw.port_open(port), 10))
            if not sw.port_pids(port):
                self.skipTest("no lsof / fuser here")
            hub = object.__new__(sw.PcHub); hub.bg = {}
            self.assertEqual(hub.stop_port(port)["port"], port)
            self.assertIsNotNone(p.wait(5))
            with self.assertRaises(sw.ApiError):
                hub.stop_port(port)              # nothing runs there now
        finally:
            p.kill()

    def test_web_never_kills_server_ports_and_ui_has_stop(self):
        src = (ROOT / "gui" / "server.py").read_text()
        i = src.index('if key == ("POST", "portstop"):')
        self.assertIn("if self.web:", src[i:i + 200])
        self.assertIn('"op") == "portstop"', src)
        js = (ROOT / "gui" / "static" / "app.js").read_text()
        self.assertIn("pc/remote-portstop", js)
        self.assertIn("shareProgram(cur", js)                  # Manage dialog: ▤ Output / ■ Stop
        self.assertIn("sharePortStop(x); } }, '■')", js)      # sidebar ■


if __name__ == "__main__":
    unittest.main()
