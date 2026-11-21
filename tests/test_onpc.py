"""Builds on the user's own computer: onpc on the server → ssh -R socket → the app → a shell here."""
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gui"))
import server as sw  # noqa: E402


def wait_for(fn, t=10):
    end = time.time() + t
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


class Rules(unittest.TestCase):
    def cfg(self, **kw):
        c = dict(sw.PC_DEFAULT)
        c.update(kw)
        return c

    def test_segments_keep_redirects(self):
        self.assertEqual(sw.pc_segments("npm ci && npm run build 2>&1 | tail -5; echo ok"),
                         ["npm ci", "npm run build 2>&1", "tail -5", "echo ok"])

    def test_deny_list_wins_in_any_part(self):
        v, why = sw.pc_decide(self.cfg(mode="all"), "web", "npm run build && git push origin", {}, 0)
        self.assertEqual(v, "deny")
        self.assertIn("git push origin", why)

    def test_modes(self):
        self.assertEqual(sw.pc_decide(self.cfg(mode="all"), "web", "npm test", {}, 0)[0], "allow")
        self.assertEqual(sw.pc_decide(self.cfg(mode="new"), "web", "npm test", {}, 0)[0], "ask")
        self.assertEqual(sw.PC_DEFAULT["mode"], "chat", "like the chat by default")
        self.assertEqual(sw.pc_decide(self.cfg(mode="chat"), "web", "npm test", {}, 0)[0], "ask", "the window decides by the chat's mode")
        self.assertEqual(sw.pc_decide(self.cfg(mode="chat", always={"web": ["npm test"]}), "web", "npm test", {}, 0)[0], "allow")
        c = self.cfg(mode="new", allow=["npm run *", "ng build*"])
        self.assertEqual(sw.pc_decide(c, "web", "npm run lint && ng build --prod", {}, 0)[0], "allow")
        self.assertEqual(sw.pc_decide(c, "web", "npm run lint && rm x", {}, 0)[0], "ask", "every part must be allowed")
        c = self.cfg(mode="new", always={"web": ["npm test"]})
        self.assertEqual(sw.pc_decide(c, "web", "npm test", {}, 0)[0], "allow")
        self.assertEqual(sw.pc_decide(c, "api", "npm test", {}, 0)[0], "ask", "remembered per project")
        self.assertEqual(sw.pc_decide(self.cfg(mode="new"), "web", "x", {"web": 100}, 50)[0], "allow", "allowed for an hour")
        self.assertEqual(sw.pc_decide(self.cfg(mode="new"), "web", "x", {"web": 100}, 150)[0], "ask", "the hour is over")
        c = self.cfg(mode="every", allow=["*"], always={"web": ["x"]})
        self.assertEqual(sw.pc_decide(c, "web", "x", {"web": 9e9}, 0)[0], "ask", "every time means every time")

    def test_allowed_projects(self):
        v, why = sw.pc_decide(self.cfg(mode="all", projects=["web"]), "api", "go test", {}, 0)
        self.assertEqual(v, "deny")
        self.assertIn("api", why)

    def test_link_argv(self):
        a = sw.pc_link_argv(sw.ServerConf("esi", "h", 22), "/home/esi/.cache/simabworkspace/pc/abc.sock", 15550)
        self.assertIn("-R", a)
        self.assertEqual(a[a.index("-R") + 1], "/home/esi/.cache/simabworkspace/pc/abc.sock:127.0.0.1:15550")
        self.assertIn("StreamLocalBindUnlink=yes", a)
        self.assertNotIn("ExitOnForwardFailure=yes", a, "a failing RemoteForward of ~/.ssh/config must not end our link")
        self.assertNotIn("ClearAllForwardings=yes", a)


class ServerScripts(unittest.TestCase):
    """PC_SETUP_PY / PC_ADMIN_PY / onpc --hint run on the server: here with a fake HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        os.makedirs(os.path.join(self.home, "proj", "web"))
        self.env = dict(os.environ, HOME=self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def py(self, code, args, stdin=b"", cwd=None):
        r = subprocess.run([sys.executable, "-c", code] + args, input=stdin, capture_output=True, env=self.env, cwd=cwd, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.decode()

    def test_setup_list_builds_remove(self):
        dev = {"id": "abcdef012345", "name": "laptop", "os": "windows", "port": 1, "secret": "s" * 40, "since": 5}
        stale = os.path.join(self.home, ".cache/simabworkspace/pc", dev["id"] + ".sock")
        os.makedirs(os.path.dirname(stale))
        socket.socket(socket.AF_UNIX).bind(stale)            # left by the last link: sshd would refuse to bind it again
        out = json.loads(self.py(sw.PC_SETUP_PY, [], json.dumps({"onpc": sw.ONPC_PY, "device": dev, "paths": {"p": "~/proj"}}).encode()))
        d = os.path.join(self.home, ".cache/simabworkspace/pc")
        self.assertEqual(out["dir"], d)
        self.assertEqual(out["real"], {"p": os.path.realpath(os.path.join(self.home, "proj"))})
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700, "only this server user")
        self.assertFalse(os.path.exists(stale), "the old socket is removed before ssh -R binds it again")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, dev["id"] + ".json")).st_mode), 0o600, "the secret is in it")
        onpc = os.path.join(self.home, ".local/bin/onpc")
        self.assertEqual(stat.S_IMODE(os.stat(onpc).st_mode), 0o755)
        lst = json.loads(self.py(sw.PC_ADMIN_PY, ["list"], json.dumps({"paths": {"p": "~/proj"}}).encode()))
        self.assertEqual(lst["devices"][0]["name"], "laptop")
        self.assertFalse(lst["devices"][0]["online"])
        self.assertNotIn("secret", lst["devices"][0])
        self.assertEqual(lst["builds"], {"p": "server"})
        # builds on the computers → onpc --hint says so inside the project, not elsewhere
        self.py(sw.PC_ADMIN_PY, ["builds", "~/proj", "computers"])
        lst = json.loads(self.py(sw.PC_ADMIN_PY, ["list"], json.dumps({"paths": {"p": "~/proj"}}).encode()))
        self.assertEqual(lst["builds"], {"p": "computers"})
        hint = subprocess.run([sys.executable, onpc, "--hint"], cwd=os.path.join(self.home, "proj", "web"),
                              env=self.env, capture_output=True, timeout=10).stdout.decode()
        self.assertEqual(hint, sw.PC_HINT)
        self.assertEqual(subprocess.run([sys.executable, onpc, "--hint"], cwd=self.home, env=self.env,
                                        capture_output=True, timeout=10).stdout, b"")
        self.py(sw.PC_ADMIN_PY, ["builds", "~/proj", "server"])
        self.assertEqual(json.loads(self.py(sw.PC_ADMIN_PY, ["list"], b"{}"))["builds"], {})
        self.py(sw.PC_ADMIN_PY, ["remove", dev["id"]])
        self.assertEqual(json.loads(self.py(sw.PC_ADMIN_PY, ["list"], b"{}"))["devices"], [])

    def test_chat_gets_the_hint_only_with_onpc(self):
        fake = os.path.join(self.home, ".local/bin")
        os.makedirs(fake)
        script = sw.chat_remote_script(os.path.join(self.home, "proj"), "printf '[%s]'", ["--x"])
        env = dict(self.env, SHELL="/bin/sh")
        out = subprocess.run(["sh", "-c", script], env=env, capture_output=True, timeout=20).stdout.decode()
        self.assertEqual(out, "[--x]", "no onpc → no extra prompt")
        with open(os.path.join(fake, "onpc"), "w") as f:
            f.write("#!/bin/sh\n[ \"$1\" = --hint ] && printf 'use onpc'\n")
        os.chmod(os.path.join(fake, "onpc"), 0o755)
        out = subprocess.run(["sh", "-c", script], env=env, capture_output=True, timeout=20).stdout.decode()
        self.assertEqual(out, "[--x][--append-system-prompt][use onpc]")
        # the chat's relay key reaches the commands Claude runs (onpc sends it: the question goes to that chat)
        script = sw.chat_remote_script(os.path.join(self.home, "proj"), "printenv SIMAB_CHAT; true", [], "0123456789abcdef")
        out = subprocess.run(["sh", "-c", script], env=env, capture_output=True, timeout=20).stdout.decode()
        self.assertEqual(out, "0123456789abcdef\n")
        script = sw.chat_remote_script(os.path.join(self.home, "proj"), "printenv SIMAB_CHAT; true", [], "x; rm -rf ~")
        self.assertNotIn("rm -rf", script, "only a real relay key")


class Forwarder(threading.Thread):
    """Stands in for `ssh -R`: unix socket on the "server" → the app's TCP port."""

    def __init__(self, path, port):
        super().__init__(daemon=True)
        self.path, self.port = path, port
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(8)

    def run(self):
        while True:
            try:
                a, _ = self.srv.accept()
            except OSError:
                return
            b = socket.create_connection(("127.0.0.1", self.port))
            for x, y in ((a, b), (b, a)):
                threading.Thread(target=self.pipe, args=(x, y), daemon=True).start()

    @staticmethod
    def pipe(x, y):
        try:
            while True:
                d = x.recv(65536)
                if not d:
                    break
                y.sendall(d)
        except OSError:
            pass
        for s in (y,):
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def close(self):
        self.srv.close()
        os.unlink(self.path)


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.envp = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": t, "SHELL": "/bin/sh"})
        self.envp.start()
        self.home = os.path.join(t, "server-home")             # the server user's home
        self.srv_dir = os.path.join(self.home, "proj")
        os.makedirs(os.path.join(self.srv_dir, "web"))
        self.local = os.path.join(t, "laptop", "proj")        # the synced copy on this computer
        os.makedirs(os.path.join(self.local, "web"))
        self.store = sw.Store(Path(t) / "gui.json")
        self.app = sw.App(self.store, sw.ServerConf("esi", "h"))
        self.httpd = sw.make_server(self.app, 0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        proj = {"name": "proj", "remote": "~/proj", "local": self.local, "localExists": True, "source": "manual"}
        self.pp = mock.patch.object(self.app, "projects", return_value=[proj])
        self.pp.start()
        hub = self.hub = self.app.pc
        hub.home = self.home
        c = hub.cfg()
        self.store.db.put("pc", dict(c, on=True, mode="all"))
        # what the setup script puts on the server
        self.pcdir = os.path.join(self.home, ".cache/simabworkspace/pc")
        os.makedirs(self.pcdir)
        self.onpc_file = os.path.join(self.home, "onpc")
        Path(self.onpc_file).write_text(sw.ONPC_PY)
        Path(self.pcdir, c["id"] + ".json").write_text(json.dumps(
            {"id": c["id"], "name": "laptop", "os": "linux", "port": self.app.port, "secret": c["secret"], "since": 1}))
        self.fwd = Forwarder(os.path.join(self.pcdir, c["id"] + ".sock"), self.app.port)
        self.fwd.start()

    def tearDown(self):
        try:
            self.fwd.close()
        except OSError:
            pass
        self.pp.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.envp.stop()
        self.tmp.cleanup()

    def onpc_proc(self, *args, cwd=None, chat=""):
        return subprocess.Popen([sys.executable, self.onpc_file] + list(args), cwd=cwd or os.path.join(self.srv_dir, "web"),
                                env=dict(os.environ, HOME=self.home, SIMAB_CHAT=chat), stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def onpc(self, *args, cwd=None):
        p = self.onpc_proc(*args, cwd=cwd)
        out, err = p.communicate(timeout=30)
        return p.returncode, out.decode(), err.decode()

    def setcfg(self, **kw):
        self.store.db.put("pc", dict(self.hub.cfg(), **kw))

    def test_runs_in_the_synced_copy_with_output_and_exit_code(self):
        code, out, err = self.onpc("pwd; echo out; echo err >&2; exit 3")
        self.assertEqual(code, 3)
        self.assertEqual(out, os.path.realpath(os.path.join(self.local, "web")) + "\nout\n")
        self.assertEqual(err, "err\n")
        r = self.hub.pending()["runs"][0]
        self.assertEqual((r["state"], r["code"], r["project"]), ("done", 3, "proj"))
        self.assertIn("out", self.hub.run_info(r["id"])["out"])
        code, out, _ = self.onpc("printf", "%s|", "a b", "c")
        self.assertEqual((code, out), (0, "a b|c|"), "several words are quoted like the shell had them")

    def test_ask_then_always(self):
        self.setcfg(mode="new")
        import types
        self.app.sessions["tab7"] = types.SimpleNamespace(spec={"kind": "chat", "relay": "00112233445566ff"})
        p = self.onpc_proc("echo built", chat="00112233445566ff")
        self.assertTrue(wait_for(lambda: self.hub.pending()["asks"]))
        a = self.hub.pending()["asks"][0]
        self.assertEqual((a["project"], a["cmd"]), ("proj", "echo built"))
        self.assertEqual((a["chat"], a["session"]), ("00112233445566ff", "tab7"), "asked only in the chat that ran it")
        del self.app.sessions["tab7"]
        self.hub.answer(a["id"], "always")
        out, err = p.communicate(timeout=20)
        self.assertEqual((p.returncode, out), (0, b"built\n"))
        self.assertIn(b"waiting for the user", err)
        code, out, err = self.onpc("echo built")
        self.assertEqual((code, out, err), (0, "built\n", ""), "remembered: not asked again")
        self.assertEqual(self.hub.cfg()["always"], {"proj": ["echo built"]})

    def test_denied_by_the_user_or_the_list(self):
        self.setcfg(mode="every")
        p = self.onpc_proc("echo nope")
        self.assertTrue(wait_for(lambda: self.hub.pending()["asks"]))
        self.hub.answer(self.hub.pending()["asks"][0]["id"], "deny")
        out, err = p.communicate(timeout=20)
        self.assertEqual((p.returncode, out), (126, b""))
        self.assertIn(b"NOT run", err)
        code, out, err = self.onpc("sudo rm x")
        self.assertEqual((code, out), (126, ""))
        self.assertIn("deny list", err)
        self.assertEqual(self.hub.pending()["runs"][0]["state"], "denied")

    def test_wrong_secret_or_turned_off(self):
        self.setcfg(on=False)
        code, _, err = self.onpc("echo x")
        self.assertEqual(code, 126)
        self.assertIn("turned off", err)

    def test_not_synced_folder_and_no_computer(self):
        code, _, err = self.onpc("echo x", cwd=self.home)
        self.assertEqual(code, 255)
        self.assertIn("not synced to laptop", err)
        self.fwd.close()
        code, _, err = self.onpc("echo x")
        self.assertEqual(code, 255)
        self.assertIn("none of your computers is connected", err)
        self.assertIn("NOT run", err)
        _, out, _ = self.onpc("--list")
        self.assertIn("offline", out)

    def test_stopping_onpc_stops_the_command(self):
        mark = os.path.join(self.local, "web", "done")
        p = self.onpc_proc("sleep 5; touch done")
        self.assertTrue(wait_for(lambda: self.hub.pending()["runs"] and self.hub.pending()["runs"][0]["state"] == "running"))
        time.sleep(0.3)
        p.terminate()
        p.wait(10)
        self.assertTrue(wait_for(lambda: self.hub.pending()["runs"][0]["state"] == "stopped", 10))
        time.sleep(5.5)
        self.assertFalse(os.path.exists(mark), "the command was killed with onpc")


class Link(unittest.TestCase):
    def test_setup_then_ssh_r_and_api(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": t}):
            store = sw.Store(Path(t) / "gui.json")
            app = sw.App(store, sw.ServerConf("esi", "h"))
            app.port = 15999
            calls, argvs = [], []

            reach = {"ok": True}

            def remote(conf, code, args, **kw):
                calls.append((code, json.loads(kw["stdin"])))
                if code == sw.PC_ADMIN_PY:          # the check after connecting
                    return {"devices": [{"id": hub.cfg()["id"], "online": reach["ok"]}]}
                return {"home": "/home/esi", "dir": "/home/esi/.cache/simabworkspace/pc", "real": {}}

            class P:
                stderr = None
                def poll(self):
                    return None
                def terminate(self):
                    pass
            hub = sw.PcHub(app, popen=lambda a: argvs.append(a) or P(), remote=remote)
            app.pc = hub
            with mock.patch.object(app, "projects", return_value=[]):
                st = hub.update({"on": True, "name": "work-pc", "mode": "every", "allow": ["npm run *", " "], "output": "panel"})
                self.assertTrue(wait_for(lambda: hub.state == "connected", 6))
                self.assertEqual(calls[-1][0], sw.PC_ADMIN_PY, "connected only after the server reached us")
                hub.stop()
                # ssh is up but the socket did not open: an error, then it tries again
                reach["ok"] = False
                n = len(argvs)
                hub.update({"on": False})
                hub.update({"on": True})
                self.assertTrue(wait_for(lambda: hub.state == "error", 6))
                self.assertIn("cannot reach this computer", hub.error)
                reach["ok"] = True
                self.assertTrue(wait_for(lambda: hub.state == "connected" and len(argvs) > n + 1, 12), "reconnects")
                hub.stop()
            self.assertEqual(st["config"]["allow"], ["npm run *"])
            self.assertNotIn("secret", st["config"])
            self.assertEqual(calls[0][0], sw.PC_SETUP_PY)
            dev = calls[0][1]["device"]
            self.assertEqual((dev["name"], dev["port"], dev["id"]), ("work-pc", 15999, hub.cfg()["id"]))
            self.assertIn("/home/esi/.cache/simabworkspace/pc/" + dev["id"] + ".sock:127.0.0.1:15999", argvs[0])
            self.assertTrue(app.state()["pc"]["on"])
            for bad in ({"mode": "x"}, {"output": "x"}, {"allow": "npm"}, {"name": ""}):
                with self.assertRaises(sw.ApiError):
                    hub.update(bad)
            with self.assertRaises(sw.ApiError):
                hub.answer("nope", "once")
            with self.assertRaises(sw.ApiError):
                hub.answer("nope", "maybe")

    def test_web_version_has_no_computer(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": t}):
            app = sw.App(sw.Store(Path(t) / "gui.json"), sw.ServerConf("esi", "h"), web=True)
            with self.assertRaises(sw.ApiError) as e:
                app.pc_api("GET", [], {})
            self.assertEqual(e.exception.status, 403)
            self.assertFalse(app.state()["pc"]["on"])


if __name__ == "__main__":
    unittest.main()
