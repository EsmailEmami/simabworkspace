"""Changing what is not synced, safely (2026-11-27, cheshmeh): the guard never calls files that left the
session (a new Not synced list, a recreated session) a mass delete; a resume from the CLI starts the window's
counts again; every pause is logged; a long pause is told; one side may win the first round of a recreated
session; Mutagen's left-out conflicts are counted; `mutagen sync list` never blocks; git name/email check;
onpc leaves remote URLs and ssh commands alone; a project new on a computer takes its run commands once."""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gui"))
sys.path.insert(0, str(ROOT / "tests"))
import server as sw  # noqa: E402
import syncguard as sg  # noqa: E402
from test_syncguard import Fake, session, tree  # noqa: E402


def sess(lf, rf, ignores=(), ident="", paused=False, loc="", rem=""):
    s = session("p", loc, rem, lf, rf, paused=paused, ignores=ignores)
    if ident:
        s["identifier"] = ident
    return s


class GuardRebase(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, True)
        self.loc, self.rem = self.t + "/pc/shop", self.t + "/server/shop"
        tree(self.loc)
        tree(self.rem)
        os.makedirs(self.loc + "/node_modules/x")
        self.f = Fake(self.t)
        self.assertEqual(self.f.g.verify(session("p", self.loc, self.rem)), (None, None))
        self.f.g.snapped["p"] = time.time()

    def s(self, lf, rf, **k):
        return sess(lf, rf, loc=self.loc, rem=self.rem, **k)

    def log(self):
        return Path(self.t, "state/syncguard.log").read_text()

    def test_adding_ignores_is_not_a_drop(self):
        g = self.f.g
        g.tick([self.s(103883, 103883)], now=0)
        g.tick([self.s(28317, 28317, ignores=("node_modules", "graphify-out"))], now=2)   # 75566 left the session
        self.assertFalse(self.f.paused("p"), g.alert_for("p"))
        self.assertIn("file counts start again", self.log())
        g.tick([self.s(28317, 28317, ignores=("node_modules", "graphify-out"))], now=4)  # balanced: watching again
        g.tick([self.s(8000, 28317, ignores=("node_modules", "graphify-out"))], now=6)   # a real mass delete
        self.assertTrue(self.f.paused("p"))
        self.assertEqual(g.alert_for("p")["kind"], "drop")

    def test_a_top_folder_newly_ignored_is_not_deleted(self):
        g = self.f.g
        g.tick([self.s(41, 41)], now=0)
        self.assertIn("node_modules", g.tops["p"])
        g.tick([self.s(41, 41, ignores=("node_modules",))], now=2)
        g.tick([self.s(41, 41, ignores=("node_modules",))], now=4)
        self.assertFalse(self.f.paused("p"), g.alert_for("p"))

    def test_recreated_session_starts_again(self):
        g = self.f.g
        g.tick([self.s(5000, 5000, ident="a1")], now=0)
        g.tick([self.s(1200, 5000, ident="b2")], now=2)            # new session: staging from scratch
        self.assertFalse(self.f.paused("p"))

    def test_cli_resume_resets_the_window_guard(self):
        g = self.f.g
        g.tick([self.s(1000, 1000)], now=0)
        g.tick([self.s(400, 1000)], now=2)
        self.assertEqual(g.alert_for("p")["kind"], "drop")
        g.tick([self.s(400, 1000, paused=True)], now=3)
        time.sleep(0.01)
        cli = Fake(self.t)                                          # `simabworkspace resume p` = another process
        self.assertEqual(cli.g.check_resume(session("p", self.loc, self.rem, paused=True)), "")
        self.f.calls.clear()
        g.tick([self.s(400, 1000)], now=4)                          # the window's old base was 1000
        g.tick([self.s(400, 1000)], now=6)
        self.assertFalse(self.f.paused("p"), g.alert_for("p"))
        self.assertIsNone(g.alert_for("p"))

    def test_raw_mutagen_resume_also_starts_again(self):
        g = self.f.g
        g.tick([self.s(1000, 1000)], now=0)
        g.tick([self.s(1000, 1000, paused=True)], now=2)
        g.tick([self.s(300, 1000)], now=4)
        self.assertFalse(self.f.paused("p"))
        self.assertIn("not by Simab Workspace", self.log())

    def test_every_pause_is_logged_even_the_same_reason(self):
        g = self.f.g
        g.pause("p", "drop", "500 of 1000 files disappeared at once")
        g.pause("p", "drop", "500 of 1000 files disappeared at once")
        log = self.log()
        self.assertEqual(log.count("PAUSED: 500 of 1000"), 1)
        self.assertEqual(log.count("PAUSED again: 500 of 1000"), 1)
        self.assertEqual(Fake(self.t).g.alert_for("p")["reason"], "500 of 1000 files disappeared at once")  # kept on disk

    def test_long_pause_is_told_once(self):
        changed = []
        g = self.f.g
        g._changed = lambda: changed.append(1)
        g.pause("p", "drop", "x gone")
        g.tick([self.s(10, 10, paused=True)], now=1000)
        g.tick([self.s(10, 10, paused=True)], now=1500)
        self.assertIsNone(g.paused_long("p"))
        g.tick([self.s(10, 10, paused=True)], now=1601)
        g.tick([self.s(10, 10, paused=True)], now=1700)
        self.assertEqual(g.paused_long("p"), 1000)
        self.assertEqual(self.log().count("still paused after 10 min: x gone"), 1)
        g.tick([self.s(10, 10)], now=1800)
        self.assertIsNone(g.paused_long("p"))
        off = Fake(self.t, {"guardPausedMin": 0}).g
        off.tick([self.s(10, 10, paused=True)], now=0)
        off.tick([self.s(10, 10, paused=True)], now=10 ** 6)
        self.assertIsNone(off.paused_long("p"))


class SetIgnoresFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.store = sw.Store(Path(self.tmp.name) / "gui.json")
        self.local = os.path.join(self.tmp.name, "shop")
        os.makedirs(self.local)
        self.sess = [{"name": "shop", "identifier": "old", "status": "watching",
                      "alpha": {"path": self.local}, "beta": {"host": "h", "user": "esi", "path": "/srv/shop"},
                      "configuration": {"ignores": ["node_modules"], "synchronizationMode": "two-way-resolved"},
                      "conflicts": [{"root": "a", "alphaChanges": [], "betaChanges": []}], "excludedConflicts": 51}]
        self.app = sw.App(self.store, sw.ServerConf("esi", "h", "225", ""))

    def test_order_snapshot_busy_script_rebase(self):
        order, g = [], self.app.guard
        g.roots["shop"] = {"id": "abcdef0123456789"}
        g.snapshot = lambda s, force=False: order.append(("snap", force, "shop" in self.app.reshaping))

        def script(a, **k):
            order.append(("script", a, g._busy("shop")))
            return 0, "ok"
        new = [dict(self.sess[0], identifier="new", configuration={"ignores": ["node_modules", "graphify-out"]})]
        with mock.patch.object(sw, "mutagen_sessions", side_effect=lambda timeout=8, fresh=False: new if fresh else self.sess), \
                mock.patch.object(self.app, "run_script", side_effect=script):
            self.app.set_ignores("shop", ["node_modules", "graphify-out"], winner="local")
        self.assertEqual(order[0], ("snap", True, True))
        self.assertEqual(order[1], ("script", ["ignore", "shop", "node_modules,graphify-out", "--winner", "local"], True))
        self.assertNotIn("shop", self.app.reshaping)
        self.assertTrue(g.roots["shop"]["fresh"])
        self.assertEqual(g.roots["shop"]["sig"], "new|node_modules\ngraphify-out")
        with self.assertRaises(sw.ApiError):
            self.app.set_ignores("shop", ["x"], winner="both")

    def test_same_list_is_a_no_op_unless_forced(self):
        seen = []
        with mock.patch.object(sw, "mutagen_sessions", return_value=self.sess), \
                mock.patch.object(self.app, "run_script", side_effect=lambda a, **k: (seen.append(a), (0, "ok"))[1]):
            self.assertTrue(self.app.set_ignores("shop", ["node_modules"])["unchanged"])
            self.assertEqual(seen, [])
            self.app.set_ignores("shop", ["node_modules"], force=True)      # back to two-way-safe
            self.assertEqual(seen[-1], ["ignore", "shop", "node_modules", "--force"])

    def test_details_count_the_conflicts_mutagen_left_out(self):
        with mock.patch.object(sw, "mutagen_sessions", return_value=self.sess):
            d = self.app.sync_details("shop")
        self.assertEqual((len(d["conflicts"]), d["moreConflicts"], d["mode"]), (1, 51, "two-way-resolved"))
        self.assertEqual(sw.excluded_conflicts({"excludedConflicts": True}), 0)

    def test_defaults_skip_build_and_tool_output(self):
        for x in ("node_modules", "graphify-out", ".angular", ".next", "dist", "build", ".dev-run", "docker/data",
                  "__pycache__", ".venv", "*.log"):
            self.assertIn(x, sw.default_ignores())
            self.assertIn(x, sw.FALLBACK_IGNORES)
        self.assertNotIn(".env", sw.default_ignores())


class BashWinnerRound(unittest.TestCase):
    def funcs(self, *names):
        script = (ROOT / "simabworkspace.sh").read_text()
        return "\n".join(re.search(r"^%s\(\) \{.*?^\}" % n, script, re.S | re.M).group(0) for n in names)

    def run_ignore(self, args, mode_ok=True):
        js = json.dumps([{"name": "shop", "alpha": {"path": "/l/shop"}, "beta": {"host": "h", "user": "esi", "port": 225, "path": "/r/shop"},
                          "configuration": {"ignores": ["node_modules"]}}])
        prog = f"""
exec 2>&1
SSH_USER=esi; SSH_HOST=h; SSH_PORT=225; DEFAULT_IGNORES=node_modules; LABEL=x
title() {{ :; }}; info() {{ echo "INFO $*"; }}; ok() {{ echo "OK $*"; }}; warn() {{ echo "WARN $*"; }}; err() {{ echo "ERR $*"; }}
daemon_running() {{ return 0; }}; ask() {{ printf '%s' "$2"; }}; confirm() {{ return 1; }}
sessions_json() {{ printf '%s' '{js}'; }}; session_git() {{ echo on; }}
mutagen() {{ echo "MUTAGEN $*" >&2; }}
wait_sync() {{ echo "WAIT $*"; {"true" if mode_ok else "[ \"${2:-}\" = flush ]"}; }}
""" + self.funcs("ignore_set", "sanitize_name", "create_session") + "\nignore_set " + " ".join(shlex.quote(a) for a in args)
        return subprocess.run(["bash", "-c", prog], capture_output=True, text=True)

    def test_server_wins_first_round_then_two_way_safe(self):
        r = self.run_ignore(["shop", "node_modules,graphify-out", "--winner", "server"])
        out = r.stdout
        self.assertEqual(r.returncode, 0, out + r.stderr)
        creates = [l for l in out.splitlines() if l.startswith("MUTAGEN sync create")]
        self.assertEqual(len(creates), 2, out)
        self.assertIn("--sync-mode two-way-resolved", creates[0])
        self.assertTrue(creates[0].endswith("esi@h:225:/r/shop /l/shop"), creates[0])      # the winner is alpha
        self.assertIn("--sync-mode two-way-safe", creates[1])
        self.assertTrue(creates[1].endswith("/l/shop esi@h:225:/r/shop"), creates[1])
        self.assertLess(out.index("WAIT shop\n"), out.index("MUTAGEN sync terminate shop", out.index(creates[0])))

    def test_local_wins_and_force_same_list(self):
        out = self.run_ignore(["shop", "node_modules", "--force"]).stdout
        self.assertIn("--sync-mode two-way-safe", out)
        self.assertNotIn("No change", out)
        out = self.run_ignore(["shop", "node_modules", "--winner", "local"]).stdout
        self.assertIn("--sync-mode two-way-resolved", out.splitlines()[[i for i, l in enumerate(out.splitlines()) if "sync create" in l][0]])
        self.assertIn("/l/shop esi@h:225:/r/shop", out)

    def test_unfinished_first_round_says_how_to_finish(self):
        r = self.run_ignore(["shop", "node_modules,x", "--winner", "local"], mode_ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--force", r.stdout)
        self.assertIn("ERR --winner", self.run_ignore(["shop", "x", "--winner", "both"]).stdout)


class MutagenListNeverBlocks(unittest.TestCase):
    def setUp(self):
        sw._MS.update(run=None, data=None, t=0.0)
        self.addCleanup(sw._MS.update, run=None, data=None, t=0.0)

    def test_slow_list_returns_the_last_good_one(self):
        gate = threading.Event()
        calls = []

        def run(argv, **k):
            calls.append(1)
            if len(calls) > 1:
                gate.wait(5)
            return mock.Mock(stdout=json.dumps([{"name": "n%d" % len(calls)}]))
        with mock.patch.object(sw, "which", return_value="/bin/mutagen"), mock.patch.object(sw.subprocess, "run", side_effect=run), \
                mock.patch.object(sw, "MS_WAIT", 0.2):
            self.assertEqual(sw.mutagen_sessions()[0]["name"], "n1")
            t0 = time.time()
            self.assertEqual(sw.mutagen_sessions()[0]["name"], "n1")      # the daemon is stuck: the last list
            self.assertEqual(sw.mutagen_sessions()[0]["name"], "n1")      # shares the call in flight
            self.assertLess(time.time() - t0, 2)
            self.assertEqual(len(calls), 2)
            gate.set()
            time.sleep(0.2)
            self.assertEqual(sw.mutagen_sessions()[0]["name"], "n3")


class GitIdentity(unittest.TestCase):
    def test_check_and_set(self):
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "XDG_CONFIG_HOME": home + "/.config", "GIT_CONFIG_NOSYSTEM": "1"}
            with mock.patch.dict(os.environ, env):
                self.assertEqual(sw.git_identity(), {"name": "", "email": ""})
                app = mock.Mock()
                for bad in (("", "a@b.c"), ("A", "nope"), ("A\x01B", "a@b.c")):
                    with self.assertRaises(sw.ApiError):
                        sw.App.set_git_identity(app, *bad)
                r = sw.App.set_git_identity(app, "Esi", "esi@example.com")
                self.assertEqual((r["name"], r["email"]), ("Esi", "esi@example.com"))
                self.assertEqual(sw.git_identity()["email"], "esi@example.com")

    def test_installers_warn(self):
        self.assertIn("git config --global user.email", (ROOT / "install.sh").read_text())
        self.assertIn("git config --global user.email", (ROOT / "install.ps1").read_text())


class OnpcRewrite(unittest.TestCase):
    S, L = "/home/esi/shop", "/mnt/d/shop"

    def r(self, cmd):
        return sw.rewrite_server_path(cmd, self.S, self.L)

    def test_local_paths_rewritten(self):
        self.assertEqual(self.r("cd /home/esi/shop && make"), "cd /mnt/d/shop && make")
        self.assertEqual(self.r("docker run -v /home/esi/shop/data:/d x"), "docker run -v /mnt/d/shop/data:/d x")
        self.assertEqual(self.r("ls '/home/esi/shop'"), "ls '/mnt/d/shop'")

    def test_remote_url_and_ssh_kept(self):
        self.assertEqual(self.r("mutagen sync create /home/esi/shop esi@h:225:/home/esi/shop"),
                         "mutagen sync create /mnt/d/shop esi@h:225:/home/esi/shop")
        self.assertEqual(self.r("scp x h:/home/esi/shop/"), "scp x h:/home/esi/shop/")
        self.assertEqual(self.r('ssh -p 225 esi@h "cd /home/esi/shop; git pull"; ls /home/esi/shop'),
                         'ssh -p 225 esi@h "cd /home/esi/shop; git pull"; ls /mnt/d/shop')
        self.assertEqual(self.r("FOO=1 sudo ssh h ls /home/esi/shop"), "FOO=1 sudo ssh h ls /home/esi/shop")

    def test_only_whole_path_parts(self):
        self.assertEqual(self.r("ls /home/esi/shop2 /x/home/esi/shop"), "ls /home/esi/shop2 /x/home/esi/shop")


class AdoptFromOthers(unittest.TestCase):
    def test_new_project_takes_commands_once(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            store = sw.Store(Path(tmp) / "gui.json")
            app = sw.App(store, sw.ServerConf("esi", "h", "225", ""))
            app._share_projects = [{"name": "shop", "remote": "/home/esi/shop", "sharedFrom": "desk", "sharedDev": "d1",
                                    "commands": [{"name": "Run", "cmd": "npm start", "where": "local"}]}]
            proj = lambda: [{"name": "shop", "remote": "/home/esi/shop", "source": "mutagen", "commands": store.commands("shop")
                             if hasattr(store, "commands") else (store.data["projects"].get("shop") or {}).get("commands") or []}]
            with mock.patch.object(app, "projects", side_effect=lambda max_age=3.0: proj()):
                self.assertEqual(app.adopt_from_others(), {"shop": 1})
                store.set_commands("shop", [])                        # removed on purpose: never comes back
                self.assertEqual(app.adopt_from_others(), {})
            self.assertTrue(store.data["projects"]["shop"]["adopted"])


if __name__ == "__main__":
    unittest.main()
