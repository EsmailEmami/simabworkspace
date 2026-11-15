"""Files tab: listing folders and reading files of a project, here and on the server."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gui"))
sys.path.insert(0, str(ROOT / "tests"))
import server as sw  # noqa: E402
import test_gui_server as base  # noqa: E402  (module import: its tests are not run twice)


def run_like_server(conf, code, args, multiplex=True, timeout=30):
    """remote_python without ssh: the same `python3 -c` the server runs."""
    r = subprocess.run([sys.executable, "-c", code] + args, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise sw.ApiError(502, r.stderr.decode()[-200:])
    return json.loads(r.stdout.decode() or "null")


class FilesTree(base.TmpHome):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(os.path.join(self.root, "src", "deep"))
        os.makedirs(os.path.join(self.root, "node_modules", "dep"))
        os.makedirs(os.path.join(self.root, "logs"))
        Path(self.root, "README.md").write_text("# hi\n")
        Path(self.root, "src", "main.go").write_text("package main\n")
        Path(self.root, "src", "سلام.txt").write_text("سلام دنیا\n")
        Path(self.root, "logs", "a.log").write_text("x\n")
        Path(self.root, "bin.dat").write_bytes(b"\x00\x01\x02binary")
        Path(self.root, "pic.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        Path(self.root, ".gitignore").write_text("logs/\n*.dat\n")
        subprocess.run(["git", "init", "-q", self.root], check=True)
        outside = os.path.join(self.tmp.name, "secret")
        os.makedirs(outside)
        Path(outside, "key").write_text("top secret\n")
        os.symlink(outside, os.path.join(self.root, "escape"))
        self.app = sw.App(self.store, sw.ServerConf("", "h"))
        self.p = {"name": "proj", "source": "manual", "local": self.root, "localExists": True, "remote": ""}

    def test_list_root_dirs_first_git_hidden_ignored_marked(self):
        d = sw.fs_call(self.app, self.p, "list", "")
        self.assertEqual(d["where"], "local")
        names = [e["name"] for e in d["entries"]]
        self.assertNotIn(".git", names)
        dirs = [e["name"] for e in d["entries"] if e["dir"]]
        self.assertEqual(names[:len(dirs)], dirs, "folders come first")
        by = {e["name"]: e for e in d["entries"]}
        self.assertTrue(by["logs"].get("ignored"))           # .gitignore
        self.assertTrue(by["bin.dat"].get("ignored"))
        self.assertTrue(by["node_modules"].get("ignored"))   # heavy folder, even without .gitignore
        self.assertFalse(by["src"].get("ignored"))
        self.assertFalse(by["README.md"].get("ignored"))
        self.assertEqual(by["README.md"]["size"], 5)

    def test_subfolder_and_persian_names(self):
        d = sw.fs_call(self.app, self.p, "list", "src")
        self.assertEqual(d["path"], "src")
        self.assertEqual([e["name"] for e in d["entries"]], ["deep", "main.go", "سلام.txt"])
        f = sw.fs_call(self.app, self.p, "read", "src/سلام.txt")
        self.assertEqual(f["text"], "سلام دنیا\n")
        self.assertEqual(f["path"], "src/سلام.txt")

    def test_binary_image_and_big_files(self):
        self.assertTrue(sw.fs_call(self.app, self.p, "read", "bin.dat").get("binary"))
        img = sw.fs_call(self.app, self.p, "read", "pic.png")
        self.assertTrue(img["image"].startswith("data:image/png;base64,"))
        Path(self.root, "big.txt").write_text("a" * 1_000_001)
        big = sw.fs_call(self.app, self.p, "read", "big.txt")
        self.assertTrue(big.get("tooBig"))
        self.assertNotIn("text", big)

    def test_never_leaves_the_project(self):
        for bad in ("../secret", "/etc", "src/../../secret", "-x"):
            with self.assertRaises(sw.ApiError, msg=bad):
                sw.fs_call(self.app, self.p, "list", bad)
        with self.assertRaises(sw.ApiError) as e:
            sw.fs_call(self.app, self.p, "read", "escape/key")      # symlink pointing outside
        self.assertIn("outside", e.exception.message)
        with self.assertRaises(sw.ApiError):
            sw.fs_call(self.app, self.p, "list", "escape")
        with self.assertRaises(sw.ApiError):
            sw.fs_call(self.app, self.p, "read", "src")             # a folder is not a file
        with self.assertRaises(sw.ApiError):
            sw.fs_call(self.app, self.p, "delete", "README.md")

    def test_on_the_server_same_answers(self):
        self.app = sw.App(self.store, sw.ServerConf("esi", "h"))
        p = dict(self.p, local="", localExists=False, remote=self.root)
        with mock.patch.object(sw, "remote_python", side_effect=run_like_server) as rp:
            d = sw.fs_call(self.app, p, "list", "src")
            self.assertEqual(d["where"], "server")
            self.assertEqual([e["name"] for e in d["entries"]], ["deep", "main.go", "سلام.txt"])
            self.assertEqual(sw.fs_call(self.app, p, "read", "README.md")["text"], "# hi\n")
            with self.assertRaises(sw.ApiError) as e:
                sw.fs_call(self.app, p, "read", "escape/key")
            self.assertIn("outside", e.exception.message)
            self.assertEqual(rp.call_args[0][2][0], "read")

    def test_web_version_always_reads_the_server(self):
        self.app = sw.App(self.store, sw.ServerConf("esi", "h"))
        self.app.web = True
        p = dict(self.p, remote=self.root)
        with mock.patch.object(sw, "remote_python", side_effect=run_like_server) as rp:
            d = sw.fs_call(self.app, p, "list", "", where="local")
        self.assertEqual(d["where"], "server")
        self.assertTrue(rp.called)

    def test_no_folder_at_all(self):
        with self.assertRaises(sw.ApiError):
            sw.fs_call(self.app, dict(self.p, local="", localExists=False), "list", "")


if __name__ == "__main__":
    unittest.main()
