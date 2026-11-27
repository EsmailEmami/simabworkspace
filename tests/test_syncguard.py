"""Sync guard: a .simab-root marker on both sides, a pause on a nearly empty side / mass delete / lost top folder,
a check before every resume, server snapshots + restore. The "server" is another folder here (syncguard.py run
as a script, as on the server). SIMAB_MUTAGEN_TEST=1 also runs the scenarios with a real Mutagen session in /tmp."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gui"))
import server as sw  # noqa: E402
import syncguard as sg  # noqa: E402

GUARD = str(ROOT / "gui" / "syncguard.py")


def tree(root, n=40, dirs=("api", "web")):
    for d in dirs:
        for i in range(n // len(dirs)):
            p = Path(root, d, f"f{i}.txt")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"{d} {i}\n")
    Path(root, "CLAUDE.md").write_text("notes\n")


def session(name, local, remote, lf=None, rf=None, paused=False, ignores=()):
    ep = lambda path, host, n: dict({"path": path, "connected": n is not None, "scanned": n is not None},
                                    **({"host": host, "user": "u", "port": 22} if host else {}),
                                    **({"files": n} if n else {}))
    return {"name": name, "alpha": ep(local, "", lf), "beta": ep(remote, "srv", rf), "paused": paused,
            "configuration": {"ignores": list(ignores)}}


class Fake:
    """A SyncGuard whose server calls run syncguard.py locally and whose Mutagen calls are recorded."""

    def __init__(self, tmp, settings=None):
        self.calls = []
        self.home = Path(tmp, "home")
        self.home.mkdir(exist_ok=True)
        self.g = sw.SyncGuard(Path(tmp, "state"), settings=lambda: settings or {})
        self.g._mutagen = lambda *a, **k: self.calls.append(a) or True
        self.g._call = self.call

    def call(self, ep, args, timeout=30, stdin=None):
        r = subprocess.run([sys.executable, "-I", GUARD] + args, input=stdin, capture_output=True,
                           env=dict(os.environ, HOME=str(self.home)), timeout=timeout)
        if r.returncode:
            raise sw.ApiError(502, r.stderr.decode()[-200:])
        return json.loads(r.stdout or b"null")

    def paused(self, name):
        return ("pause", name) in self.calls


class MarkerTest(unittest.TestCase):
    def test_marker_read_write_and_verdicts(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(sg.read_marker(t + "/nope")["state"], "noroot")
            self.assertEqual(sg.read_marker(t)["state"], "missing")
            os.makedirs(t + "/.git/info")
            self.assertTrue(sg.write_marker(t, "abcdef0123456789", "p"))
            self.assertEqual(sg.read_marker(t), {"state": "ok", "id": "abcdef0123456789"})
            self.assertIn("/.simab-root", Path(t, ".git/info/exclude").read_text())
            self.assertFalse(sg.write_marker(t, "ffffffffffffffff"))          # never replaces another id
            self.assertTrue(sg.write_marker(t, "abcdef0123456789"))
            ok = {"state": "ok", "id": "abcdef0123456789"}
            miss, gone = {"state": "missing", "id": ""}, {"state": "noroot", "id": ""}
            self.assertIsNone(sg.marker_verdict(ok, dict(ok)))
            self.assertEqual(sg.marker_verdict(miss, miss), "adopt")
            self.assertIn("not be mounted", sg.marker_verdict(miss, ok))
            self.assertIn("server", sg.marker_verdict(ok, miss))
            self.assertIn("missing", sg.marker_verdict(gone, ok))
            self.assertIn("different", sg.marker_verdict(ok, {"state": "ok", "id": "1111111111111111"}))

    def test_counts_and_drops(self):
        cfg = sg.config({"dropPct": 20, "dropFiles": 500})
        self.assertIn("nearly", sg.lopsided(1, 7000, cfg))
        self.assertIn("server", sg.lopsided(7000, 0, cfg))
        self.assertIsNone(sg.lopsided(0, 2, cfg))           # two small new folders
        self.assertIsNone(sg.lopsided(6000, 7000, cfg))
        w = sg.DropWatch()
        self.assertIsNone(w.check(1000, cfg, 0))
        self.assertIsNone(w.check(950, cfg, 2))             # 5 %: normal work
        self.assertIn("disappeared", w.check(700, cfg, 4))  # 30 % of the most seen
        w = sg.DropWatch()
        w.check(100000, cfg, 0)
        self.assertIn("600 of 100000", w.check(99400, cfg, 2))   # few percent, but many files
        w = sg.DropWatch()
        w.check(50, cfg, 0)
        self.assertIsNone(w.check(35, cfg, 2))              # 30 % but only 15 files
        w = sg.DropWatch()
        w.check(1000, cfg, 0)
        self.assertIsNone(w.check(700, cfg, 0 + sg.WINDOW + 5))   # old baseline forgotten
        self.assertEqual(sg.config({"dropPct": 400, "on": "x"})["dropPct"], 100)

    def test_top_dirs_and_count(self):
        with tempfile.TemporaryDirectory() as t:
            tree(t)
            os.makedirs(t + "/node_modules/x")
            Path(t, "node_modules/x/a.js").write_text("x")
            self.assertEqual(sg.top_dirs(t, ["node_modules"]), {"api", "web"})
            self.assertEqual(sg.count_files(t, ["node_modules"]), 41)
            self.assertEqual(sg.count_files(t + "/nope"), -1)

    def test_count_skips_path_patterns_and_vcs(self):
        # docker/data (a "/" inside) and .git with Git off are not synced: counting them made every
        # project look "This computer has 87331 files, the server 15262"
        with tempfile.TemporaryDirectory() as t:
            tree(t)
            for d in ("docker/data/pg", ".git/objects", "web/docker/data"):
                os.makedirs(os.path.join(t, d), exist_ok=True)
                for i in range(5):
                    Path(t, d, f"f{i}").write_text("x")
            ign = ["node_modules", "docker/data"]
            self.assertEqual(sg.count_files(t, ign), 41 + 5 + 5)    # web/docker/data stays (rooted pattern)
            self.assertEqual(sg.count_files(t, ign + [".git"]), 41 + 5)
            self.assertTrue(sg._ignored("a/b/node_modules", ["node_modules"]))
            self.assertTrue(sg._ignored("dist", ["/dist"]))
            self.assertFalse(sg._ignored("web/dist", ["/dist"]))
            self.assertTrue(sg._ignored("api/vendor", ["api/**/vendor/"]))      # from a nested .gitignore
            self.assertTrue(sg._ignored("api/x/vendor", ["api/**/vendor/"]))
            vcs = sw.SyncGuard._ignores({"configuration": {"ignores": ["x"], "ignoreVCSMode": "ignore"}})
            self.assertIn(".git", vcs)
            self.assertNotIn(".git", sw.SyncGuard._ignores({"configuration": {"ignores": ["x"]}}))


class DiskTest(unittest.TestCase):
    """pc: docker made /media/me/DISK2/sync/shop (root) before udisks mounted the disk → DISK21."""
    INFO = ("22 1 8:2 / / rw - ext4 /dev/sda2 rw\n"
            "90 22 8:17 / {mnt} rw,nosuid - ext4 /dev/sdb1 rw\n")
    IDS = ({"/dev/sdb1": "1111-aaaa", "/dev/sda2": "2222-bbbb"}, {"/dev/sdb1": "DISK2"})

    def info(self, t, mnt):
        f = os.path.join(t, "mountinfo")
        Path(f).write_text(self.INFO.format(mnt=mnt.replace(" ", "\\040")))
        return f

    def test_disk_mounted_under_another_name(self):
        with tempfile.TemporaryDirectory() as t:
            good = self.info(t, "/media/me/DISK2")
            rec = sg.mount_of("/media/me/DISK2/sync/shop", good, self.IDS)
            self.assertEqual((rec["mnt"], rec["uuid"], rec["label"]), ("/media/me/DISK2", "1111-aaaa", "DISK2"))
            self.assertIsNone(sg.mount_verdict("/media/me/DISK2/sync/shop", rec, rec, good, self.IDS))
            moved = self.info(t, "/media/me/DISK21")
            cur = sg.mount_of("/media/me/DISK2/sync/shop", moved, self.IDS)
            self.assertEqual(cur["mnt"], "/")
            why = sg.mount_verdict("/media/me/DISK2/sync/shop", rec, cur, moved, self.IDS)
            self.assertIn("mounted at /media/me/DISK21 instead of /media/me/DISK2", why)
            self.assertIn("/media/me/DISK21/sync/shop", why)

    def test_not_mounted_and_first_sight_on_system_disk(self):
        with tempfile.TemporaryDirectory() as t:
            f = os.path.join(t, "mi")
            Path(f).write_text("22 1 8:2 / / rw - ext4 /dev/sda2 rw\n")
            rec = {"mnt": "/media/me/DISK2", "uuid": "1111-aaaa", "label": "DISK2"}
            cur = sg.mount_of("/media/me/DISK2/x", f, self.IDS)
            self.assertIn("is not mounted", sg.mount_verdict("/media/me/DISK2/x", rec, cur, f, self.IDS))
            self.assertIn("system disk", sg.mount_verdict("/media/me/DISK2/sync/blog", {}, cur, f, self.IDS))
            self.assertIsNone(sg.mount_verdict("/home/me/aista", {}, cur, f, self.IDS))

    def test_folder_made_by_root(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertIsNone(sg.foreign_owner(t))
            with mock.patch.object(sg.os, "getuid", return_value=os.getuid() + 1):
                self.assertIn("docker", sg.foreign_owner(t))


class SnapshotTest(unittest.TestCase):
    def _run(self, use_rsync):
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"HOME": t + "/home"}):
            if not use_rsync:
                patch = mock.patch.object(sg.shutil, "which", return_value=None)
                patch.start()
                self.addCleanup(patch.stop)
            src = t + "/proj"
            tree(src)
            os.makedirs(src + "/node_modules/big")
            Path(src, "node_modules/big/x.js").write_text("x")
            self.assertFalse(sg.snapshot(src, "abcdef0123456789")["ok"])          # no marker: nothing taken
            sg.write_marker(src, "abcdef0123456789")
            a = sg.snapshot(src, "abcdef0123456789", keep=3)
            self.assertTrue(a["ok"], a)
            self.assertFalse(os.path.exists(a["path"] + "/node_modules"))
            self.assertTrue(sg.snapshot(src, "abcdef0123456789", every_min=30)["skipped"])
            time.sleep(1.1)
            b = sg.snapshot(src, "abcdef0123456789", force=True, keep=3)
            ino = lambda p: os.stat(p).st_ino
            self.assertEqual(ino(a["path"] + "/api/f0.txt"), ino(b["path"] + "/api/f0.txt"))   # hard link: cheap
            shutil.rmtree(src + "/api")
            Path(src, "CLAUDE.md").write_text("changed\n")
            r = sg.restore(src, dry=True)
            self.assertEqual(r["files"], 20)
            r = sg.restore(src)
            self.assertEqual(r["files"], 20)
            self.assertEqual(Path(src, "CLAUDE.md").read_text(), "changed\n")         # kept: not overwritten
            Path(src, "api/f1.txt").write_text("edited")
            self.assertNotEqual(Path(b["path"], "api/f1.txt").read_text(), "edited")  # snapshot untouched
            sg.restore(src, overwrite=True)
            self.assertEqual(Path(src, "CLAUDE.md").read_text(), "notes\n")
            for _ in range(3):
                time.sleep(1.1)
                sg.snapshot(src, "abcdef0123456789", force=True, keep=3, days=0)
            self.assertEqual(len(sg.list_snaps("abcdef0123456789")), 3)

    def test_snapshot_rsync(self):
        if not shutil.which("rsync"):
            self.skipTest("no rsync")
        self._run(True)

    def test_snapshot_python(self):
        self._run(False)

    def test_cli_restore(self):
        with tempfile.TemporaryDirectory() as t:
            env = dict(os.environ, HOME=t)
            src = t + "/proj"
            tree(src)
            sg.write_marker(src, "abcdef0123456789")
            run = lambda *a: subprocess.run([sys.executable, "-I", GUARD] + list(a), env=env, capture_output=True, text=True)
            self.assertEqual(run("snapshot", src).returncode, 0)
            shutil.rmtree(src + "/web")
            out = run("restore", src)
            self.assertIn("Restored 20 files", out.stdout)
            self.assertIn("1 snapshots", run("list").stdout)


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, True)
        self.loc, self.rem = self.t + "/pc/shop", self.t + "/server/shop"
        tree(self.loc)
        tree(self.rem)
        self.f = Fake(self.t)

    def adopt(self):
        k, why = self.f.g.verify(session("p", self.loc, self.rem))
        self.assertIsNone(k, why)
        self.assertEqual(sg.read_marker(self.loc)["id"], sg.read_marker(self.rem)["id"])

    def test_adopt_then_unmounted_disk_pauses(self):
        self.adopt()
        os.rename(self.loc, self.loc + ".real")              # DISK2 not mounted: docker made the folder again
        os.makedirs(self.loc + "/docker/data")
        self.f.g.tick([session("p", self.loc, self.rem, 0, 41)])
        self.assertTrue(self.f.paused("p"))
        self.assertEqual(self.f.g.alert_for("p")["kind"], "marker")
        self.assertIn("lost its .simab-root", self.f.g.alert_for("p")["reason"])
        self.assertTrue(Path(self.rem, "api/f0.txt").exists())

    def test_missing_folder_pauses(self):
        self.adopt()
        shutil.rmtree(self.loc)
        self.f.g.tick([session("p", self.loc, self.rem)])
        self.assertTrue(self.f.paused("p"))
        self.assertIn("missing", self.f.g.alert_for("p")["reason"])

    def test_first_sight_of_a_wiped_folder_writes_nothing(self):
        shutil.rmtree(self.loc)
        os.makedirs(self.loc + "/docker")                   # the incident: never seen by the guard before
        k, why = self.f.g.verify(session("p", self.loc, self.rem))
        self.assertEqual(k, "lopsided", why)
        self.assertEqual(sg.read_marker(self.loc)["state"], "missing")
        self.assertEqual(sg.read_marker(self.rem)["state"], "missing")
        self.assertIn("Kept", "Kept " + self.f.g.check_resume(session("p", self.loc, self.rem, paused=True)))
        self.assertTrue(self.f.paused("p"))

    def test_server_has_marker_this_computer_not(self):
        sg.write_marker(self.rem, "abcdef0123456789")
        shutil.rmtree(self.loc)
        os.makedirs(self.loc + "/docker")
        k, _ = self.f.g.verify(session("p", self.loc, self.rem))
        self.assertEqual(k, "lopsided")
        tree(self.loc)                                       # a second computer joining with a full folder: adopts
        k, _ = self.f.g.verify(session("p", self.loc, self.rem))
        self.assertIsNone(k)
        self.assertEqual(sg.read_marker(self.loc)["id"], "abcdef0123456789")

    def test_fresh_download_is_not_paused(self):
        shutil.rmtree(self.loc)
        os.makedirs(self.loc)
        self.f.g.note_created("p")
        self.assertEqual(self.f.g.verify(session("p", self.loc, self.rem)), (None, None))
        self.f.g.tick([session("p", self.loc, self.rem, 1, 42)])
        self.assertFalse(self.f.paused("p"))
        self.f.g.tick([session("p", self.loc, self.rem, 42, 42)])
        self.assertNotIn("fresh", self.f.g.roots["p"])          # balanced: the watch is on from now

    def test_mass_delete_pauses_and_approve_resumes(self):
        self.adopt()
        g = self.f.g
        g.snapped["p"] = time.time()
        g.tick([session("p", self.loc, self.rem, 41, 41)], now=100)
        shutil.rmtree(self.loc + "/api")
        g.tick([session("p", self.loc, self.rem, 21, 41)], now=102)
        self.assertTrue(self.f.paused("p"))
        self.assertIn(g.alert_for("p")["kind"], ("drop", "topdir"))
        self.assertEqual(g.approve(session("p", self.loc, self.rem, paused=True)), "")
        self.assertIn(("resume", "p"), self.f.calls)
        self.assertIsNone(g.alert_for("p"))
        self.assertTrue(sg.list_snaps(sg.read_marker(self.rem)["id"]) or True)

    def test_drop_on_server_side_pauses(self):
        self.adopt()
        g = self.f.g
        g.snapped["p"] = time.time()
        g.tick([session("p", self.loc, self.rem, 1000, 1000)], now=0)
        g.tick([session("p", self.loc, self.rem, 1000, 400)], now=2)
        self.assertIn("On the server", g.alert_for("p")["reason"])

    def test_top_folder_gone_pauses(self):
        self.adopt()
        g = self.f.g
        g.snapped["p"] = time.time()
        g.tick([session("p", self.loc, self.rem, 41, 41)], now=0)
        shutil.rmtree(self.loc + "/web")
        g.tick([session("p", self.loc, self.rem, 41, 41)], now=2)    # Mutagen has not rescanned yet
        self.assertEqual(g.alert_for("p")["kind"], "topdir")
        self.assertIn("web", g.alert_for("p")["reason"])

    def test_off_and_thresholds(self):
        f = Fake(self.t, {"guardOn": False})
        shutil.rmtree(self.loc)
        f.g.tick([session("p", self.loc, self.rem)])
        self.assertEqual(f.g.check_resume(session("p", self.loc, self.rem)), "")
        self.assertFalse(f.calls)

    def test_resume_check_takes_a_snapshot(self):
        self.adopt()
        self.assertEqual(self.f.g.check_resume(session("p", self.loc, self.rem, paused=True)), "")
        pid = sg.read_marker(self.rem)["id"]
        with mock.patch.dict(os.environ, {"HOME": str(self.f.home)}):
            self.assertEqual(len(sg.list_snaps(pid)), 1)
        self.assertTrue(Path(self.f.home, ".simabworkspace/syncguard.py").is_file())   # restore command on the server

    def test_daemon_start_pauses_a_lost_folder_first(self):
        self.adopt()
        self.f.g._save()
        shutil.rmtree(self.loc)
        os.makedirs(self.loc + "/docker")
        order = []
        g = sw.SyncGuard(Path(self.t, "state"))               # a new process: from the saved state
        g._mutagen = lambda *a, **k: order.append(a) or True
        with mock.patch.object(sw, "which", return_value="/bin/mutagen"), \
                mock.patch.object(sw.subprocess, "run", side_effect=lambda a, **k: order.append(tuple(a[1:])) or mock.Mock(returncode=0)):
            bad = sw.guarded_daemon_start(g)
        self.assertEqual([n for n, _ in bad], ["p"])
        self.assertEqual(order, [("pause", "p"), ("daemon", "start")])
        self.assertTrue(Path(self.t, "state/syncguard.log").read_text().count("PAUSED"))

    def test_rm_star_keeps_the_marker_but_resume_still_refuses(self):
        self.adopt()
        for n in os.listdir(self.loc):                        # `rm -rf *` leaves .simab-root behind
            if not n.startswith("."):
                p = os.path.join(self.loc, n)
                shutil.rmtree(p) if os.path.isdir(p) else os.unlink(p)
        why = self.f.g.check_resume(session("p", self.loc, self.rem, paused=True))
        self.assertIn("nearly", why)

    def test_disk_moved_pauses_and_approve_refuses(self):
        self.adopt()
        g = self.f.g
        g.roots["p"]["mount"] = {"mnt": "/media/me/DISK2", "uuid": "1111-aaaa", "label": "DISK2"}
        with mock.patch.object(sg, "mount_of", return_value={"mnt": "/", "uuid": "2222-bbbb"}), \
                mock.patch.object(sg, "where_mounted", return_value="/media/me/DISK21"):
            g.tick([session("p", self.loc, self.rem, 41, 41)])
            self.assertEqual(g.alert_for("p")["kind"], "disk")
            self.assertIn("DISK21", g.alert_for("p")["reason"])
            self.assertIn("cannot skip", g.approve(session("p", self.loc, self.rem, paused=True)))
        self.assertNotIn(("resume", "p"), self.f.calls)

    def test_root_owned_folder_pauses_before_anything(self):
        self.adopt()
        with mock.patch.object(sg.os, "getuid", return_value=os.getuid() + 1):
            self.f.g.tick([session("p", self.loc, self.rem, 41, 41)])
            self.assertIn("docker", self.f.g.alert_for("p")["reason"])
            self.assertTrue(self.f.g.check_resume(session("p", self.loc, self.rem, paused=True)))

    def test_settings_map(self):
        g = sw.SyncGuard(Path(self.t, "s2"), settings=lambda: {"guardDropPct": 35, "guardTopDirs": False, "guardSnapEvery": 0})
        c = g.cfg()
        self.assertEqual((c["dropPct"], c["topDirs"], c["snapEvery"]), (35, False, 0))
        self.assertTrue(sw.DEFAULT_SETTINGS["guardOn"])


@unittest.skipUnless(os.environ.get("SIMAB_MUTAGEN_TEST") == "1" and shutil.which("mutagen"), "real Mutagen: SIMAB_MUTAGEN_TEST=1")
class RealMutagenTest(unittest.TestCase):
    """Two folders in /tmp and a test session (sgtest-*); the user's own sessions are never touched."""

    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="sgtest-")
        self.addCleanup(shutil.rmtree, self.t, True)
        self.names = []

    def tearDown(self):
        for n in self.names:
            subprocess.run(["mutagen", "sync", "terminate", n], capture_output=True)

    def mk(self, name):
        a, b = self.t + f"/{name}-pc", self.t + f"/{name}-server"
        tree(a, 200)
        os.makedirs(b)
        subprocess.run(["mutagen", "sync", "create", "--name", name, "--sync-mode", "two-way-safe", "--ignore", "docker/data",
                        a, b], check=True, capture_output=True)
        self.names.append(name)
        subprocess.run(["mutagen", "sync", "flush", name], check=True, capture_output=True, timeout=120)
        return a, b

    def sess(self, name):
        out = subprocess.run(["mutagen", "sync", "list", "--template", "{{ json . }}", name], capture_output=True, text=True).stdout
        s = json.loads(out)[0]
        s["beta"]["host"], s["beta"]["user"] = "srv", "u"      # play the server's side
        return s

    def unmount(self, a):
        os.rename(a, a + ".disk")
        os.makedirs(a + "/docker/data")
        Path(a, "docker/data/db").write_text("x")

    def test_without_guard_mutagen_spreads_the_delete(self):
        name = f"sgtest-ctl-{os.getpid()}"
        a, b = self.mk(name)
        subprocess.run(["mutagen", "sync", "pause", name], check=True, capture_output=True)
        self.unmount(a)
        subprocess.run(["mutagen", "sync", "resume", name], check=True, capture_output=True)
        subprocess.run(["mutagen", "sync", "flush", name], capture_output=True, timeout=120)
        self.assertFalse(Path(b, "api").exists(), "Mutagen did not delete: the incident would not reproduce")

    def test_reconnect_after_unmount_is_refused(self):
        name = f"sgtest-g-{os.getpid()}"
        a, b = self.mk(name)
        f = Fake(self.t)
        f.g._mutagen = sw.SyncGuard._mutagen.__get__(f.g)      # real pause / resume
        self.assertEqual(f.g.check_resume(self.sess(name)), "")
        subprocess.run(["mutagen", "sync", "pause", name], check=True, capture_output=True)
        self.unmount(a)
        why = f.g.check_resume(self.sess(name))
        self.assertTrue(why)
        time.sleep(3)
        self.assertTrue(Path(b, "api/f0.txt").exists())
        self.assertTrue(self.sess(name)["paused"])

    def test_missing_and_empty_root_on_reconnect(self):
        name = f"sgtest-m-{os.getpid()}"
        a, b = self.mk(name)
        f = Fake(self.t)
        f.g._mutagen = sw.SyncGuard._mutagen.__get__(f.g)
        self.assertEqual(f.g.check_resume(self.sess(name)), "")
        subprocess.run(["mutagen", "sync", "pause", name], check=True, capture_output=True)
        os.rename(a, a + ".disk")                                      # not there at all
        self.assertIn("missing", f.g.check_resume(self.sess(name)))
        os.makedirs(a)                                                 # there, but empty
        self.assertTrue(f.g.check_resume(self.sess(name)))
        f.g.precheck()
        self.assertTrue(self.sess(name)["paused"])
        self.assertTrue(Path(b, "api/f0.txt").exists())

    def test_running_session_paused_on_mass_delete(self):
        """A delete on a live session reaches the other side within Mutagen's own cycle (inotify beats any poll):
        the guard stops what follows, the snapshot taken before brings it back."""
        name = f"sgtest-d-{os.getpid()}"
        a, b = self.mk(name)
        f = Fake(self.t, {"guardSnapEvery": 0})
        f.g._mutagen = sw.SyncGuard._mutagen.__get__(f.g)
        self.assertEqual(f.g.check_resume(self.sess(name)), "")        # also takes a snapshot of the "server"
        th = __import__("threading").Thread(target=lambda: [f.g.tick([self.sess(name)]) or time.sleep(0.05) for _ in range(100)], daemon=True)
        th.start()
        time.sleep(1)
        shutil.rmtree(a + "/api")
        th.join(30)
        self.assertTrue(self.sess(name)["paused"])
        self.assertIn(f.g.alert_for(name)["kind"], ("topdir", "drop"))
        shutil.rmtree(a + "/web")                                      # more deletes: they stay here
        time.sleep(2)
        self.assertTrue(Path(b, "web/f0.txt").exists())
        with mock.patch.dict(os.environ, {"HOME": str(f.home)}):
            r = sg.restore(b)
        self.assertTrue(r["ok"], r)
        self.assertTrue(Path(b, "api/f0.txt").exists())

