"""Claude sessions running on the server — this app, VS Code, terminals: list, stop, watch."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gui"))
sys.path.insert(0, str(ROOT / "tests"))
import server as sw  # noqa: E402
import test_gui_server as base  # noqa: E402

CONV_A = "aaaaaaaa-1111-2222-3333-444444444444"
CONV_B = "bbbbbbbb-1111-2222-3333-444444444444"
CONV_C = "cccccccc-1111-2222-3333-444444444444"

# stands in for our relay daemon: its command line looks like it, and it runs "Claude" (sleep)
FAKE_RELAY = "# simabworkspace/chats\nimport subprocess, sys, time\np = subprocess.Popen(['sleep', '60'])\nprint(p.pid, flush=True)\ntime.sleep(60)\n"


def start_time(pid):
    st = Path(f"/proc/{pid}/stat").read_text()
    return st[st.rindex(")") + 2:].split()[19]


def J(o):
    return json.dumps(o, separators=(",", ":"), ensure_ascii=False)


@unittest.skipUnless(os.path.exists("/proc/self/stat"), "needs /proc")
class RunningOnServer(base.TmpHome):
    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp.name) / "home"
        self.sess = self.home / ".claude" / "sessions"
        self.sess.mkdir(parents=True)
        (self.home / "shop" / "api").mkdir(parents=True)
        (self.home / "blog").mkdir(parents=True)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
        super().tearDown()

    def spawn(self, argv, **kw):
        p = subprocess.Popen(argv, start_new_session=True, **kw)
        self.procs.append(p)
        return p

    def write(self, pid, conv, cwd, entrypoint, status="idle", start=None, **extra):
        info = {"pid": pid, "sessionId": conv, "cwd": str(cwd), "entrypoint": entrypoint, "status": status,
                "procStart": start if start is not None else start_time(pid), "startedAt": time.time() * 1000,
                "updatedAt": time.time() * 1000, "name": "x", **extra}
        (self.sess / f"{pid}.json").write_text(json.dumps(info))

    def run_script(self, *args):
        r = subprocess.run([sys.executable, "-c", sw.CLAUDE_RUNNING_PY] + list(args), capture_output=True, text=True,
                           env=dict(os.environ, HOME=str(self.home)), timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def scene(self):
        relay = self.spawn([sys.executable, "-c", FAKE_RELAY, "daemon", "0123456789abcdef"], stdout=subprocess.PIPE, text=True)
        ours = int(relay.stdout.readline())
        term = self.spawn(["sleep", "60"])
        self.write(ours, CONV_A, self.home / "shop" / "api", "sdk-cli", "busy")
        self.write(term.pid, CONV_B, self.home / "blog", "cli", tmux="sw-blog-claude:@1.%1")
        dead = self.spawn(["true"])
        dead.wait()
        self.write(dead.pid, CONV_C, self.home / "shop", "claude-vscode", start="1")          # gone
        reused = self.spawn(["sleep", "60"])
        self.write(reused.pid, CONV_C, self.home / "shop", "claude-vscode", start="12345")    # pid of something else now
        return ours, term.pid

    def test_lists_only_real_sessions_and_who_runs_them(self):
        ours, term = self.scene()
        d = self.run_script("list")
        self.assertEqual(d["home"], str(self.home))
        by = {x["sessionId"]: x for x in d["sessions"]}
        self.assertEqual(set(by), {CONV_A, CONV_B}, "dead and reused pids are not listed")
        self.assertEqual((by[CONV_A]["owner"], by[CONV_A]["relay"], by[CONV_A]["status"]), ("simab", "0123456789abcdef", "busy"))
        self.assertEqual((by[CONV_B]["owner"], by[CONV_B]["tmux"]), ("terminal", "sw-blog-claude:@1.%1"))

    def test_vscode_is_recognised(self):
        p = self.spawn(["sleep", "60"])
        self.write(p.pid, CONV_C, self.home / "shop", "claude-vscode")
        self.assertEqual(self.run_script("list")["sessions"][0]["owner"], "vscode")

    def test_stop_only_that_conversation(self):
        ours, term = self.scene()
        self.assertIn("error", self.run_script("stop", str(term), CONV_A))       # pid and conversation must match
        self.assertIsNone(self.procs[1].poll())
        self.assertEqual(self.run_script("stop", str(term), CONV_B), {"ok": True})
        self.procs[1].wait(timeout=5)
        self.assertEqual([x["sessionId"] for x in self.run_script("list")["sessions"]], [CONV_A])
        self.assertIn("error", self.run_script("stop", str(term), CONV_B))       # not running any more

    def local_remote_python(self, conf, code, args, multiplex=True, timeout=30):
        r = subprocess.run([sys.executable, "-c", code] + list(args), capture_output=True, text=True,
                           env=dict(os.environ, HOME=str(self.home)), timeout=timeout)
        return json.loads(r.stdout or "null")

    def test_app_maps_sessions_to_projects_and_open_tabs(self):
        ours, term = self.scene()
        app = sw.App(self.store, sw.ServerConf("esi", "h", "22", ""))
        projects = [{"name": "shop", "remote": "~/shop", "local": "", "localExists": False, "endpoint": {}},
                    {"name": "shop-api", "remote": str(self.home / "shop" / "api"), "local": "", "localExists": False, "endpoint": {}},
                    {"name": "blog", "remote": "~/blog/", "local": "", "localExists": False, "endpoint": {}}]
        built = {"make_argv": None, "cwd": self.tmp.name, "title": "Claude · shop-api", "resume": CONV_A, "mode": "default",
                 "model": "", "relay": "0123456789abcdef", "spec": {"kind": "chat", "project": "shop-api", "where": "server"}}
        tab = sw.ChatSession("tab1", built)
        app.sessions["tab1"] = tab
        with mock.patch.object(app, "projects", return_value=projects), \
                mock.patch.object(sw, "remote_python", side_effect=self.local_remote_python):
            r = app.claude_running(force=True)
            self.assertEqual({k: [x["sessionId"] for x in v] for k, v in r.items()},
                             {"shop-api": [CONV_A], "blog": [CONV_B]}, "the deepest project folder wins")
            self.assertEqual(r["shop-api"][0]["tab"], "tab1", "open here, found by its relay")
            self.assertEqual(r["blog"][0]["tab"], "")
            self.assertEqual(app.claude_running_one("blog", CONV_B)["pid"], term)
            out = app.claude_stop("blog", term, CONV_B)
            self.assertEqual(out["running"], [])
            with self.assertRaises(sw.ApiError):
                app.claude_stop("blog", term, CONV_B)
            with self.assertRaises(sw.ApiError):
                app.claude_stop("blog", "1", CONV_B)
        tab.closed = True

    def test_watch_shows_what_the_other_claude_writes(self):
        proj = self.home / ".claude" / "projects" / sw.claude_project_dir(str(self.home / "blog"))
        proj.mkdir(parents=True)
        f = proj / f"{CONV_B}.jsonl"
        f.write_text(J({"type": "user", "message": {"role": "user", "content": "first question"}}) + "\n")
        app = sw.App(self.store, sw.ServerConf("esi", "h", "22", ""))
        projects = [{"name": "blog", "remote": str(self.home / "blog"), "local": "", "localExists": False, "endpoint": {}}]
        running = {"pid": 1, "sessionId": CONV_B, "status": "busy", "owner": "vscode"}
        sw.WatchSession.POLL = 0.1
        with mock.patch.object(app, "projects", return_value=projects), \
                mock.patch.object(sw, "remote_python", side_effect=self.local_remote_python), \
                mock.patch.object(app, "claude_running_one", side_effect=lambda p, c: running if running else None):
            info = app.create_session({"kind": "chat", "project": "blog", "resume": CONV_B, "watch": True})
            w = app.get(info["id"])
            self.assertIsInstance(w, sw.WatchSession)
            self.assertEqual((info["title"], info["spec"]["watch"], info["spec"]["resume"]), ("Watch · blog", True, CONV_B))
            self.assertEqual(w.events[0]["type"], "_history")                    # what was there before
            self.assertEqual(w.state()["watch"], {"running": True, "status": "busy", "owner": "vscode"})
            with open(f, "a") as fh:
                fh.write(J({"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": [{"type": "text", "text": "live answer"}]}}) + "\n")
                fh.write('{"type":"assistant","message":{"id":"m2"')               # still being written
            self.assertTrue(base_wait(lambda: any(e.get("type") == "assistant" for e in w.events)))
            got = [e for e in w.events if e.get("type") == "assistant"]
            self.assertEqual(got[0]["message"]["content"][0]["text"], "live answer")
            self.assertEqual(len(got), 1, "half a line is not read")
            running = None                                                       # it stopped
            self.assertTrue(base_wait(lambda: w.watch["running"] is False))
            self.assertTrue(any(e.get("type") == "_watch" and e["running"] is False for e in w.events))
            w.handle({"t": "send", "content": "hi"})                             # nothing is sent anywhere
            app.close_session(info["id"])
            self.assertTrue(w.closed)
            with self.assertRaises(sw.ApiError):
                app.create_session({"kind": "chat", "project": "blog", "resume": "not-a-uuid", "watch": True})


def base_wait(fn, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


if __name__ == "__main__":
    unittest.main()
