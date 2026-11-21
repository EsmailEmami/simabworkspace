"""Builds on the user's own computer: onpc on the server → ssh -R socket → the app → a shell here."""
import json
import shutil
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
        self.assertEqual(self.hub.pending()["runs"][0]["session"], "tab7", "its output shows in that chat")
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

    def test_dev_server_in_the_background(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()
        code, out, err = self.onpc("--bg", "web", sys.executable + " -u -m http.server " + str(port) + " --bind 127.0.0.1")
        self.assertEqual(code, 0, err)
        self.assertIn("started web on " + self.hub.cfg()["name"], out)
        self.assertIn("URL: http://127.0.0.1:%d" % port, out)
        x = self.hub.bg["web"]
        self.assertEqual((x["state"], x["project"]), ("running", "proj"))
        self.assertEqual(self.hub.pending()["bg"][0]["name"], "web")
        code, out, _ = self.onpc("--bg", "web", "sleep 1")
        self.assertEqual(code, 255)
        code, out, _ = self.onpc("--ps")
        self.assertIn("web  running", out)
        import urllib.request
        urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=5).read()
        self.assertTrue(wait_for(lambda: "GET / " in self.hub.bg["web"]["out"], 5))
        code, out, _ = self.onpc("--logs", "web", "5")
        self.assertIn("GET /", out)
        code, out, _ = self.onpc("--stop", "web")
        self.assertIn("stopped web", out)
        self.assertTrue(wait_for(lambda: x["_p"].poll() is not None, 8), "the process is gone")
        code, _, err = self.onpc("--bg", "bad", "exit 3")
        self.assertEqual(code, 255)
        self.assertIn("stopped at once (exit 3)", err)

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


SSHFS = shutil.which("sshfs") or os.path.expanduser("~/.local/lib/simabworkspace/sshfs/usr/bin/sshfs")


@unittest.skipUnless(os.path.isfile(SSHFS) and os.path.exists("/dev/fuse"), "sshfs / FUSE not here")
class LocalOnly(EndToEnd):
    """A Local only project: the server mounts the computer's folder (real sshfs through the link)."""
    test_runs_in_the_synced_copy_with_output_and_exit_code = test_ask_then_always = None      # EndToEnd's own
    test_denied_by_the_user_or_the_list = test_wrong_secret_or_turned_off = None
    test_not_synced_folder_and_no_computer = test_stopping_onpc_stops_the_command = test_dev_server_in_the_background = None

    def setUp(self):
        super().setUp()
        self.pp.stop()
        self.laptop = os.path.join(self.tmp.name, "laptop", "lo")             # the only copy of the files
        os.makedirs(os.path.join(self.laptop, "src"))
        Path(self.laptop, "src", "a.txt").write_text("hello\n")
        Path(self.tmp.name, "secret.txt").write_text("outside\n")
        os.symlink(os.path.join(self.tmp.name, "secret.txt"), os.path.join(self.laptop, "escape"))
        self.mnt = os.path.join(self.home, ".simab-mnt", "lo")
        c = self.hub.cfg()
        proj = {"name": "lo", "remote": "~/.simab-mnt/lo", "local": self.laptop, "localExists": True, "source": "manual",
                "localOnly": c["id"]}
        self.pp = mock.patch.object(self.app, "projects", return_value=[proj])
        self.pp.start()
        bindir = os.path.join(self.home, ".local", "bin")
        os.makedirs(bindir)
        shutil.copy(self.onpc_file, os.path.join(bindir, "onpc"))
        os.chmod(os.path.join(bindir, "onpc"), 0o755)
        lib = os.path.join(self.home, ".local/lib/simabworkspace/sshfs/usr/bin")
        os.makedirs(lib)
        os.symlink(SSHFS, os.path.join(lib, "sshfs"))
        self.env = dict(os.environ, HOME=self.home, PATH="/usr/bin:/bin")      # no system sshfs: our own copy
        self.server_py(sw.PC_ADMIN_PY, ["builds", "~/.simab-mnt/lo", "local"])
        self.dev = c["id"]

    def tearDown(self):
        subprocess.run(["fusermount3", "-u", "-z", self.mnt], capture_output=True)
        super().tearDown()

    def server_py(self, code, args, stdin=b"{}"):
        r = subprocess.run([sys.executable, "-c", code] + args, input=stdin, capture_output=True, env=self.env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout or b"null")

    def mount(self):
        return self.server_py(sw.PC_MOUNT_PY, ["mount"], json.dumps({"dev": self.dev, "mounts": [{"name": "lo", "path": "~/.simab-mnt/lo"}]}).encode())

    def test_mounted_files_commands_hook_and_unmount(self):
        self.assertEqual(self.mount(), {"lo": "ok"})
        self.assertTrue(os.path.ismount(self.mnt))
        self.assertEqual(self.mount(), {"lo": "ok"}, "already mounted: left alone")
        # Claude's own tools on the server = plain files here
        self.assertEqual(Path(self.mnt, "src", "a.txt").read_text(), "hello\n")
        self.assertGreater(self.hub.meter[1], 0, "what the server read is counted (sidebar speed)")
        Path(self.mnt, "src", "b.txt").write_text("from claude\n")
        self.assertEqual(Path(self.laptop, "src", "b.txt").read_text(), "from claude\n", "written on the computer")
        Path(self.mnt, "src", "c.tmp").write_text("new")
        os.replace(os.path.join(self.mnt, "src", "c.tmp"), os.path.join(self.mnt, "src", "a.txt"))   # how editors save
        self.assertEqual(Path(self.laptop, "src", "a.txt").read_text(), "new")
        os.makedirs(os.path.join(self.mnt, "src", "d", "e"))
        self.assertTrue(os.path.isdir(os.path.join(self.laptop, "src", "d", "e")))
        self.assertEqual(os.stat(os.path.join(self.mnt, "src", "a.txt")).st_uid, os.getuid())
        with self.assertRaises(OSError):
            Path(self.mnt, "escape").read_text()                  # a link out of the project folder
        # the chat: hint + the Bash hook; every command runs on the computer, paths translated both ways
        hint = subprocess.run([sys.executable, os.path.join(self.home, ".local/bin/onpc"), "--hint"], cwd=self.mnt,
                              env=self.env, capture_output=True, timeout=10).stdout.decode()
        self.assertEqual(hint, sw.PC_LOCAL_HINT)
        r = subprocess.run([sys.executable, os.path.join(self.home, ".local/bin/onpc"), "--local"], cwd=self.mnt,
                           env=self.env, capture_output=True, timeout=10)
        hookcmd = json.loads(r.stdout)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        out = subprocess.run(hookcmd, shell=True, input=json.dumps({"tool_input": {"command": "ls src", "timeout": 5}}).encode(),
                             env=self.env, capture_output=True, timeout=10).stdout
        new = json.loads(out)["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(new["timeout"], 5)
        self.assertIn("onpc", new["command"])
        res = subprocess.run(new["command"] + "; cat " + os.path.join(self.mnt, "src", "b.txt") + "; pwd", shell=True,
                             cwd=self.mnt, env=self.env, capture_output=True, timeout=30)
        res2 = subprocess.run([sys.executable, os.path.join(self.home, ".local/bin/onpc"),
                               "cat " + os.path.join(self.mnt, "src", "b.txt") + "; pwd; echo $0 >/dev/null"],
                              cwd=os.path.join(self.mnt, "src"), env=self.env, capture_output=True, timeout=30)
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertEqual(res2.stdout.decode(), "from claude\n" + os.path.realpath(os.path.join(self.mnt, "src")) + "\n",
                         "ran on the computer; its folder shown as the server's")
        self.assertIn(b"b.txt", res.stdout)
        self.assertFalse(self.hub.pending()["asks"], "no extra question: Claude's own modes decide")
        # Claude's own files (memory…) are on the server: such a command is not sent to the computer
        for c in ("ls ~/.claude/projects/x/memory/", "cat $HOME/.claude/plans/a.md", "ls " + os.path.join(self.home, ".claude", "x")):
            out = subprocess.run(hookcmd, shell=True, input=json.dumps({"tool_input": {"command": c}}).encode(),
                                 env=self.env, capture_output=True, timeout=10).stdout
            self.assertEqual(out, b"", c)
        # the hook leaves onpc itself alone
        out = subprocess.run(hookcmd, shell=True, input=json.dumps({"tool_input": {"command": new["command"]}}).encode(),
                             env=self.env, capture_output=True, timeout=10).stdout
        self.assertEqual(out, b"")
        # unmount: the folder leaves the server, files stay on the computer
        self.assertEqual(self.server_py(sw.PC_MOUNT_PY, ["umount"], json.dumps({"mounts": [{"name": "lo", "path": "~/.simab-mnt/lo"}]}).encode()), {"lo": "ok"})
        self.assertFalse(os.path.exists(self.mnt))
        self.assertTrue(os.path.isfile(os.path.join(self.laptop, "src", "b.txt")))

    def test_chat_refuses_when_not_mounted(self):
        os.makedirs(self.mnt)
        script = sw.chat_remote_script(self.mnt, "echo started", [])
        r = subprocess.run(["sh", "-c", script], env=dict(self.env, SHELL="/bin/sh"), capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 98)
        self.assertIn(b"lives on your computer", r.stderr)
        self.assertEqual(r.stdout, b"", "Claude did not start in the empty folder")
        self.assertEqual(self.mount(), {"lo": "ok"})
        r = subprocess.run(["sh", "-c", sw.chat_remote_script(self.mnt, "printf '[%s]'", [])], env=dict(self.env, SHELL="/bin/sh"),
                           capture_output=True, timeout=20)
        self.assertIn(b"[--settings]", r.stdout, "mounted: Claude starts with the Bash hook")
        self.assertIn(b"[--append-system-prompt]", r.stdout)

    def test_not_empty_folder_is_not_covered(self):
        os.makedirs(self.mnt)
        Path(self.mnt, "x").write_text("server file")
        self.assertIn("not empty", self.mount()["lo"])


class LocalOnlyApp(unittest.TestCase):
    """Adding / removing a Local only project, and run commands of projects built on the computer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.envp = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        self.envp.start()
        self.store = sw.Store(Path(self.tmp.name) / "gui.json")
        self.app = sw.App(self.store, sw.ServerConf("esi", "h"))
        self.folder = os.path.join(self.tmp.name, "mine")
        os.makedirs(self.folder)
        self.mut = mock.patch.object(sw, "mutagen_sessions", return_value=[])
        self.mut.start()

    def tearDown(self):
        self.mut.stop()
        self.envp.stop()
        self.tmp.cleanup()

    def test_add_and_remove(self):
        with self.assertRaises(sw.ApiError) as e:
            self.app.pc_add_local({"local": self.folder})
        self.assertIn("Connect this computer", e.exception.message)
        self.store.db.put("pc", dict(self.app.pc.cfg(), on=True))
        calls = []
        with mock.patch.object(sw, "remote_python", side_effect=lambda conf, code, args, **kw: calls.append(args) or {}), \
                mock.patch.object(self.app.pc, "unmount") as um:
            r = self.app.pc_add_local({"local": self.folder})
            self.assertEqual(r["project"]["remote"], "~/.simab-mnt/mine")
            self.assertEqual(calls[-1], ["builds", "~/.simab-mnt/mine", "local"])
            p = next(x for x in self.app.projects(max_age=0) if x["name"] == "mine")
            self.assertEqual(p["localOnly"], self.app.pc.cfg()["id"])
            self.assertEqual(p["buildsOn"], "local")
            self.assertEqual(self.app.pc.local_projects()[0]["name"], "mine")
            with self.assertRaises(sw.ApiError) as e:
                self.app.pc_add_local({"local": self.folder})
            self.assertEqual(e.exception.status, 409)
            with self.assertRaises(sw.ApiError):
                self.app.pc_add_local({"local": os.path.join(self.tmp.name, "nope")})
            self.app.pc_forget_local("mine")
            self.assertEqual(um.call_args[0][0][0]["name"], "mine", "its mount leaves the server")
            self.assertEqual(calls[-1], ["builds", "~/.simab-mnt/mine", "server"])
            self.assertNotIn("localOnly", self.store.data["projects"].get("mine") or {})
        self.assertTrue(os.path.isdir(self.folder), "the files stay on the computer")

    def test_run_commands_follow_where_the_project_builds(self):
        cmd = {"id": "c1", "name": "api", "cmd": "go run .", "where": "server", "url": "", "cwd": ""}
        proj = {"name": "p", "remote": "/srv/p", "local": self.folder, "localExists": True, "source": "manual", "endpoint": {}}
        spec = {"kind": "run", "project": "p", "_cmd": cmd}
        built = sw.build_spec(dict(spec), sw.ServerConf("esi", "h"), self.store, [dict(proj, buildsOn="server")])
        self.assertEqual(built["spec"]["where"], "server")
        for on in ("computers", "local"):
            built = sw.build_spec(dict(spec), sw.ServerConf("esi", "h"), self.store, [dict(proj, buildsOn=on)])
            self.assertEqual(built["spec"]["where"], "local", on)


class LinkQuality(unittest.TestCase):
    def test_rtt_bars_and_traffic(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        port = srv.getsockname()[1]
        self.assertIsInstance(sw.pc_rtt("127.0.0.1", port), float)
        srv.close()
        self.assertIsNone(sw.pc_rtt("127.0.0.1", port), "nobody answers")
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": t}):
            app = sw.App(sw.Store(Path(t) / "gui.json"), sw.ServerConf("esi", "127.0.0.1", 22))
            hub = app.pc
            hub.state, hub.wanted = "connected", True
            for rtt, bars in ((20, 5), (90, 4), (200, 3), (400, 2), (900, 1), (None, 0)):
                hub.net["rtt"] = rtt
                self.assertEqual(hub.net_info()["bars"], bars, rtt)
            with mock.patch.object(sw, "pc_rtt", return_value=33.0):
                th = threading.Thread(target=hub._net_loop, args=(0.2,), daemon=True)
                th.start()
                time.sleep(0.3)
                hub.meter[0] += 100000                    # Claude reads files: down from the server's view = to it
                hub.meter[1] += 50000
                self.assertTrue(wait_for(lambda: hub.net["down"] > 0 and hub.net["up"] > 0, 3))
                self.assertEqual(hub.net_info()["rtt"], 33.0)
                self.assertEqual(hub.net_info()["bars"], 5)
                hub.wanted = False
                hub.wake.set()
                th.join(3)
            self.assertEqual(hub.net["down"], 0.0, "reset when the link stops")
            hub.state = "error"
            self.assertEqual(hub.net_info()["bars"], 0)
