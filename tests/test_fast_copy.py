"""Add project → First copy: N tar streams at once, then the (paused) Mutagen session resumes."""
import json
import os
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


def local_ssh(self, cmd, **kw):
    """ssh replaced by the local shell: the "server" folder is just another folder here."""
    return subprocess.Popen(["sh", "-c", cmd], **kw)


def make_tree(root):
    files = {"main.go": b"package main\n", "web/app.ts": b"x" * 70000, "web/a b/ü.txt": "سلام".encode(),
             "node_modules/x/i.js": b"no", "dist/out.js": b"no", "src/dist/keep.js": b"keep",
             ".git/HEAD": b"ref: refs/heads/main\n", "empty.txt": b""}
    for rel, data in files.items():
        p = Path(root, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    os.chmod(Path(root, "main.go"), 0o755)
    os.symlink("main.go", Path(root, "link"))
    return files


class CopyList(unittest.TestCase):
    def test_list_skips_not_synced_and_vcs(self):
        with tempfile.TemporaryDirectory() as d:
            make_tree(d)
            got = {r for r, _ in sw.copy_list_local(d, ["node_modules", "/dist"], True)["files"]}
            self.assertEqual(got, {"main.go", "web/app.ts", "web/a b/ü.txt", "src/dist/keep.js", ".git/HEAD", "empty.txt", "link"})
            got = {r for r, _ in sw.copy_list_local(d, [], False)["files"]}
            self.assertNotIn(".git/HEAD", got)
            self.assertIn("node_modules/x/i.js", got)
            self.assertIn("error", sw.copy_list_local(d + "/nope", [], True))

    def test_buckets_spread_by_size_and_count(self):
        files = [["big", 10_000_000]] + [["s%03d" % i, 10] for i in range(300)]
        b = sw.copy_buckets(files, 4)
        self.assertEqual(len(b), 4)
        self.assertEqual(sorted(sum(b, [])), sorted(f[0] for f in files))
        self.assertEqual(b[[i for i, x in enumerate(b) if "big" in x][0]], ["big"], "the big file has a stream for itself")
        self.assertEqual(sw.copy_buckets([["a", 1]], 8), [["a"]])
        self.assertEqual(sw.copy_buckets([], 4), [])


class FastCopyRuns(unittest.TestCase):
    def run_copy(self, mode, src, dst, ignores, git, streams=3):
        done = []
        job = sw.FastCopy("shop", mode, dst if mode == "download" else src, src if mode == "download" else dst,
                          sw.ServerConf("esi", "h", "22", ""), ignores, git, streams, on_done=done.append)
        if mode == "download":   # the list runs on the server: here, the same script locally
            job._list = lambda: sw.copy_list_local(src, ignores, git)
        with mock.patch.object(sw.FastCopy, "_ssh", local_ssh), mock.patch.object(sw, "mutagen_resume") as res:
            job.run()
        res.assert_called_once_with("shop")
        self.assertEqual(done, [job])
        return job

    def check(self, src, dst, job):
        self.assertEqual(Path(dst, "web/app.ts").read_bytes(), b"x" * 70000)
        self.assertEqual(Path(dst, "web/a b/ü.txt").read_text(), "سلام")
        self.assertTrue(os.access(Path(dst, "main.go"), os.X_OK), "executable bit kept")
        self.assertEqual(os.readlink(Path(dst, "link")), "main.go")
        self.assertTrue(Path(dst, "src/dist/keep.js").exists())
        self.assertFalse(Path(dst, "node_modules").exists())
        self.assertFalse(Path(dst, "dist").exists())
        self.assertFalse(Path(dst, ".git").exists(), "git off: .git stays")
        self.assertEqual(job.failed, [])
        self.assertEqual(job.files, job.total_files)
        self.assertEqual(job.total_files, 6)
        self.assertTrue(job.done)

    def test_upload(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as srv:
            make_tree(src)
            dst = os.path.join(srv, "new folder")
            job = self.run_copy("upload", src, dst, ["node_modules", "/dist"], False)
            self.check(src, dst, job)

    def test_download(self):
        with tempfile.TemporaryDirectory() as srv, tempfile.TemporaryDirectory() as home:
            make_tree(srv)
            dst = os.path.join(home, "shop")
            job = self.run_copy("download", srv, dst, ["node_modules", "/dist"], False, streams=4)
            self.check(srv, dst, job)
            self.assertGreater(job.bytes, 70000)

    def test_a_failure_still_resumes_the_sync(self):
        job = sw.FastCopy("shop", "download", "/tmp/x", "/srv/x", sw.ServerConf("esi", "h", "22", ""), [], True, 2)
        job._list = lambda: {"error": "folder not found: /srv/x"}
        with mock.patch.object(sw, "mutagen_resume") as res:
            job.run()
        res.assert_called_once_with("shop")
        self.assertIn("folder not found: /srv/x", job.failed)

    def test_progress(self):
        job = sw.FastCopy("shop", "upload", "/a", "/b", sw.ServerConf("esi", "h", "22", ""), [], True, 99)
        self.assertEqual(job.streams, sw.COPY_MAX_STREAMS)
        job.total, job.total_files, job.files = 1000, 10, 4
        job.add(400)
        job.started = time.time() - 10
        p = job.progress()
        self.assertEqual((p["percent"], p["bytes"], p["totalBytes"], p["files"], p["totalFiles"]), (40, 400, 1000, 4, 10))
        self.assertEqual(p["eta"], 15)
        job.add(5000)
        self.assertEqual(job.progress()["percent"], 99, "never 100 until the sync has checked it")


class AppSide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = sw.App(sw.Store(Path(self.tmp.name, "gui.json")), sw.ServerConf("esi", "h", "225", ""))
        self.sess = [{"name": "shop", "status": "", "paused": True, "alpha": {"path": "/l/shop"},
                      "beta": {"host": "h2", "user": "esi", "port": 226, "path": "/home/esi/shop"}}]

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_project_creates_paused_and_starts_the_copy(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, b"  \xe2\x9c\x93 Project 'shop' added.\n", b"")
        with mock.patch.object(sw.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(sw.App, "start_copy") as start:
            self.app.add_project({"mode": "download", "remote": "/home/esi/shop", "git": True, "streams": 8, "ignores": ["node_modules"]})
            self.assertIn("--paused", seen["argv"])
            start.assert_called_once_with("shop", "download", ["node_modules"], True, 8)
            start.reset_mock()
            self.app.add_project({"mode": "download", "remote": "/home/esi/shop", "streams": 0})
            self.assertNotIn("--paused", seen["argv"])
            start.assert_not_called()
            self.app.add_project({"mode": "download", "remote": "/home/esi/shop", "streams": 500})
            self.assertEqual(start.call_args[0][4], sw.COPY_MAX_STREAMS)

    def test_start_copy_shows_copying_then_the_sync(self):
        gate = __import__("threading").Event()
        with mock.patch.object(sw, "mutagen_sessions", return_value=self.sess), \
                mock.patch.object(sw.FastCopy, "run", lambda job: gate.wait(5)):
            job = self.app.start_copy("shop", "upload", ["node_modules"], True, 4)
            self.assertEqual((job.local, job.remote, job.conf.host, str(job.conf.port)), ("/l/shop", "/home/esi/shop", "h2", "226"))
            marker = Path(job.marker)
            self.assertEqual(marker.read_text(), "shop")
            self.app.invalidate()
            p = self.app.projects()[0]
            self.assertEqual((p["sync"]["state"], p["sync"]["label"], p["sync"]["paused"]), ("busy", "copying ×4", False))
            self.assertIn("percent", p["sync"]["progress"])
            with self.assertRaises(sw.ApiError) as cm:
                self.app.sync_action("shop", "resume")
            self.assertEqual(cm.exception.status, 409)
            job.done = True
            self.app.invalidate()
            self.assertEqual(self.app.projects()[0]["sync"]["label"], "paused")
            gate.set()

    def test_a_copy_left_from_last_run_resumes_its_sync(self):
        d = self.app._copy_dir()
        d.mkdir(parents=True)
        (d / "shop").write_text("shop")
        with mock.patch.object(sw, "mutagen_sessions", return_value=self.sess), mock.patch.object(sw, "mutagen_resume") as res:
            self.app.projects()
            for _ in range(50):
                if res.called:
                    break
                time.sleep(0.02)
        res.assert_called_once_with("shop")
        self.assertFalse((d / "shop").exists())

    def test_script_passes_paused_to_mutagen(self):
        script = (ROOT / "simabworkspace.sh").read_text()
        self.assertIn('[ "${6:-0}" = "1" ] && ign+=(--paused)', script)
        self.assertEqual(script.count('"$git" "$paused"'), 2, "add-server and add-local")
        self.assertEqual(script.count("--paused) paused=1; shift ;;"), 2)


if __name__ == "__main__":
    unittest.main()
