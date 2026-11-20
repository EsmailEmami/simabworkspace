"""Persian voice made on the server (TTS_PY): docker image built on first use, a container on a
unix socket, Piper / Microsoft engines. docker here is a fake on PATH."""
import base64
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gui"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as sw  # noqa: E402
import test_gui_server as base  # noqa: E402

# the fake voice: answers one POST on the socket with "AUDIO:<engine>:<text>", then keeps serving a while
FAKE_SRV = r'''
import json, os, socketserver, sys
from http.server import BaseHTTPRequestHandler
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        data = ("AUDIO:%s:%s:%s" % (b["engine"], b["voice"], b["text"])).encode()
        code = 500 if b["text"] == "boom" else 200
        self.send_response(code); self.send_header("Content-Type", "audio/mpeg" if b["engine"] == "edge" else "audio/wav")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
class S(socketserver.UnixStreamServer):
    def get_request(self):
        r, _ = self.socket.accept(); return r, ("t", 0)
s = S(sys.argv[1], H); s.timeout = 3
for _ in range(5): s.handle_request()
'''

# fake docker: state in $FAKE_DOCKER (image, running), every call logged
FAKE_DOCKER = r'''#!PY
import json, os, subprocess, sys
st = os.environ["FAKE_DOCKER"]
def state():
    try: return json.load(open(st + "/state.json"))
    except OSError: return {}
s = state(); a = sys.argv[1:]
open(st + "/calls", "a").write(json.dumps(a) + "\n")
if a[0] == "version": print("27"); sys.exit()
if a[0] == "inspect":
    if s.get("running"): print("true " + s["running"]); sys.exit()
    sys.exit(1)
if a[:2] == ["image", "inspect"]: sys.exit(0 if s.get("image") else 1)
if a[0] in ("build", "rm"): sys.exit()
if a[0] == "run":
    run = a[a.index("-v") + 1].split(":")[0]
    subprocess.Popen([sys.executable, st + "/srv.py", run + "/tts.sock"], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    s["running"] = a[-1]; json.dump(s, open(st + "/state.json", "w")); print("cid"); sys.exit()
sys.exit(2)
'''


class TtsScript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "h")
        self.st = os.path.join(self.tmp.name, "d")
        bin_ = os.path.join(self.tmp.name, "bin")
        for d in (self.home, self.st, bin_):
            os.makedirs(d)
        Path(self.st, "srv.py").write_text(FAKE_SRV)
        dk = Path(bin_, "docker")
        dk.write_text(FAKE_DOCKER.replace("#!PY", "#!" + sys.executable))
        dk.chmod(dk.stat().st_mode | stat.S_IEXEC)
        self.env = {"HOME": self.home, "FAKE_DOCKER": self.st, "PATH": bin_ + ":" + os.environ.get("PATH", "")}

    def set_state(self, **kw):
        Path(self.st, "state.json").write_text(json.dumps(kw))

    def calls(self):
        p = Path(self.st, "calls")
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def run_tts(self, body):
        req = sw.clean_tts(body)
        r = subprocess.run([sys.executable, "-I", "-c", sw.TTS_PY, sw.TTS_IMAGE], input=json.dumps(req).encode(),
                           capture_output=True, timeout=60, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr.decode()[-500:])
        return json.loads(r.stdout.decode())

    def test_first_use_builds_the_image_once(self):
        self.assertEqual(self.run_tts({"engine": "piper", "text": "سلام"}), {"error": "installing", "installing": True})
        b = Path(self.home, ".cache/simab-tts/build")
        self.assertIn("piper-tts==", (b / "Dockerfile").read_text())
        self.assertIn("PiperVoice.load", (b / "serve.py").read_text())
        time.sleep(0.3)
        self.run_tts({"engine": "piper", "text": "سلام"})       # still building: not a second build
        builds = [c for c in self.calls() if c[0] == "build"]
        self.assertEqual(builds, [["build", "-t", sw.TTS_IMAGE, str(b)]])

    def test_starts_the_container_then_speaks(self):
        self.set_state(image=True)
        r = self.run_tts({"engine": "piper", "text": "  سلام دنیا "})
        self.assertEqual(r["mime"], "audio/wav")
        self.assertEqual(base64.b64decode(r["audio"]).decode(), "AUDIO:piper:fa-IR-DilaraNeural:سلام دنیا")
        run = [c for c in self.calls() if c[0] == "run"][0]
        self.assertIn("unless-stopped", run)
        self.assertEqual(run[-1], sw.TTS_IMAGE)
        self.assertNotIn("-p", run)                               # no port: a unix socket
        # running now: the next call talks to it straight away, Microsoft with the chosen voice
        r = self.run_tts({"engine": "edge", "text": "سلام", "voice": "fa-IR-FaridNeural"})
        self.assertEqual((r["mime"], base64.b64decode(r["audio"]).decode()), ("audio/mpeg", "AUDIO:edge:fa-IR-FaridNeural:سلام"))
        self.assertEqual(len([c for c in self.calls() if c[0] == "run"]), 1)
        self.assertIn("failed", self.run_tts({"engine": "piper", "text": "boom"})["error"])

    def test_an_old_image_container_is_replaced(self):
        self.set_state(image=True, running="simab-tts:0")
        self.assertIn("audio", self.run_tts({"engine": "piper", "text": "سلام"}))
        self.assertIn(["rm", "-f", "simab-tts"], self.calls())


class TtsApp(base.TmpHome):
    def test_checks_and_answers(self):
        for bad in ({"engine": "x", "text": "a"}, {"engine": "piper", "text": "  "}, {"engine": "piper", "text": 5},
                    {"engine": "piper", "text": "a" * (sw.TTS_MAX + 1)}, None):
            with self.assertRaises(sw.ApiError):
                sw.clean_tts(bad)
        self.assertEqual(sw.clean_tts({"engine": "edge", "text": "a", "voice": "evil"})["voice"], "fa-IR-DilaraNeural")
        app = sw.App(self.store, sw.ServerConf("esi", "h"))
        self.addCleanup(app.shutdown_sessions)
        seen = []
        def fake(conf, code, args, multiplex=True, timeout=30, stdin=None):
            seen.append((code, args, json.loads(stdin)))
            return answers.pop(0)
        answers = [{"audio": "QQ==", "mime": "audio/mpeg"}, {"error": "installing", "installing": True}, {"error": "no docker"}]
        with mock.patch.object(sw, "remote_python", fake):
            self.assertEqual(app.tts({"engine": "edge", "text": "سلام"}), {"audio": "QQ==", "mime": "audio/mpeg"})
            self.assertEqual(app.tts({"engine": "piper", "text": "سلام"}), {"installing": True})
            with self.assertRaises(sw.ApiError):
                app.tts({"engine": "piper", "text": "سلام"})
        self.assertEqual((seen[0][0], seen[0][1]), (sw.TTS_PY, [sw.TTS_IMAGE]))
        self.assertEqual(seen[0][2]["text"], "سلام")
        with self.assertRaises(sw.ApiError):
            sw.App(self.store, sw.ServerConf("", "")).tts({"engine": "piper", "text": "a"})


class TtsHttp(base.HttpServer):
    def test_route_and_web(self):
        with mock.patch.object(sw.App, "tts", lambda self, b: {"audio": "QQ==", "mime": "audio/wav", "got": b["text"]}):
            st, d = self.req("POST", "/api/tts", {"engine": "piper", "text": "سلام"})
        self.assertEqual((st, d["got"]), (200, "سلام"))
        self.assertNotIn(("POST", "tts"), sw.WEB_BLOCKED)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TtsScript))
    suite.addTests(loader.loadTestsFromTestCase(TtsApp))
    suite.addTest(TtsHttp("test_route_and_web"))
    return suite
