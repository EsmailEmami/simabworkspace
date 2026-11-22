"""Local only projects: the SFTP server on the computer hides heavy folders and counts requests."""
import io
import json
import shutil
import subprocess
from unittest import mock
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gui"))
import server as sw  # noqa: E402


def s(b):
    return struct.pack(">I", len(b)) + b


def pkt(kind, rid, payload=b""):
    body = bytes([kind]) + struct.pack(">I", rid) + payload
    return struct.pack(">I", len(body)) + body


def replies(raw):
    out, p = [], 0
    while p < len(raw):
        n = struct.unpack(">I", raw[p:p + 4])[0]
        out.append((raw[p + 4], raw[p + 5:p + 4 + n]))
        p += 4 + n
    return out


def names(payload):
    """READDIR reply (after the request id) → file names."""
    n, p, out = struct.unpack(">I", payload[4:8])[0], 8, []
    for _ in range(n):
        ln = struct.unpack(">I", payload[p:p + 4])[0]
        out.append(payload[p + 4:p + 4 + ln].decode())
        p += 4 + ln
        ln = struct.unpack(">I", payload[p:p + 4])[0]
        p += 4 + ln
        flags = struct.unpack(">I", payload[p:p + 4])[0]
        p += 4 + 8 + 4 + 8 + (8 if flags & 2 else 0)
    return out


class SftpHidesHeavyFolders(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        for x in ("src", "node_modules/lib", ".git/objects", "pkg/node_modules", ".venv"):
            os.makedirs(os.path.join(self.d, x))
        Path(self.d, "src", "a.py").write_text("x")
        Path(self.d, ".git", "HEAD").write_text("ref: refs/heads/main\n")

    def run_srv(self, reqs):
        out, stats = io.BytesIO(), sw.sftp_stats()
        srv = sw.SftpServer(self.d, io.BytesIO(b"".join(reqs)), out, stats=stats)
        srv.serve()
        return replies(out.getvalue()), stats

    def listing(self, path):
        r, _ = self.run_srv([pkt(11, 1, s(path.encode())), pkt(12, 2, s(b"1")), pkt(12, 3, s(b"1"))])
        self.assertEqual(r[0][0], 102)
        return [x for x in names(r[1][1]) if x not in (".", "..")] if r[1][0] == 104 else []

    def test_root_listing_skips_heavy_but_keeps_git(self):
        self.assertEqual(sorted(self.listing("/")), [".git", "pkg", "src"])

    def test_nested_node_modules_hidden(self):
        self.assertEqual(self.listing("/pkg"), [])

    def test_git_folder_looks_empty(self):
        self.assertEqual(self.listing("/.git"), [])

    def test_stat_inside_hidden_is_no_such_file(self):
        r, st = self.run_srv([pkt(17, 1, s(b"/node_modules/lib")), pkt(17, 2, s(b"/.git/HEAD")),
                              pkt(17, 3, s(b"/src/a.py")), pkt(17, 4, s(b"/Node_Modules"))])
        codes = [struct.unpack(">I", p[4:8])[0] if k == 101 else "attrs" for k, p in r]
        self.assertEqual(codes, [sw.SFTP_NOFILE, sw.SFTP_NOFILE, "attrs", sw.SFTP_NOFILE])
        self.assertEqual(st["hidden"], 3)

    def test_stats_count_requests_and_folders(self):
        _, st = self.run_srv([pkt(17, 1, s(b"/src/a.py")), pkt(17, 2, s(b"/src")), pkt(17, 3, s(b"/"))])
        self.assertEqual(st["n"], 3)
        self.assertEqual(st["dirs"], {"src/a.py": 1, "src": 1, "": 1})
        self.assertGreaterEqual(st["ms"], 0)

    def test_hidden_helper(self):
        self.assertTrue(sw.sftp_hidden(["a", "node_modules"]))
        self.assertTrue(sw.sftp_hidden([".git", "HEAD"]))
        self.assertFalse(sw.sftp_hidden([".git"]))
        self.assertFalse(sw.sftp_hidden(["src", "app.py"]))


class MountOptions(unittest.TestCase):
    def test_parallel_first_then_plain_fallback(self):
        self.assertIn("SSHFS_OPTS = [", sw.PC_MOUNT_PY)
        self.assertIn("max_conns=4", sw.PC_MOUNT_PY)
        self.assertIn("for opts in SSHFS_OPTS:", sw.PC_MOUNT_PY)
        compile(sw.PC_MOUNT_PY, "mount", "exec")

    def test_hint_tells_claude_about_hidden_folders(self):
        self.assertIn("node_modules", sw.PC_LOCAL_HINT)
        self.assertIn("mcp__pc__Read", sw.PC_LOCAL_HINT)


class GlobRegex(unittest.TestCase):
    def test_patterns(self):
        m = lambda p, x: bool(sw.glob_regex(p).match(x))
        self.assertTrue(m("**/*.ts", "a.ts"))
        self.assertTrue(m("**/*.ts", "src/x/a.ts"))
        self.assertFalse(m("*.ts", "src/a.ts"))
        self.assertTrue(m("src/**/*.{ts,tsx}", "src/a/b.tsx"))
        self.assertFalse(m("src/**/*.{ts,tsx}", "lib/b.ts"))
        self.assertTrue(m("a?.[jt]s", "ab.js"))
        self.assertFalse(m("*.ts", "a.tsx"))


class PcGrepGlob(unittest.TestCase):
    """Grep / Glob of a Local only project, run on the computer; paths come back in the server's form."""
    SR = "/home/u/proj"

    def setUp(self):
        self.d = tempfile.mkdtemp()
        for f, t in {"src/a.py": "import os\nprint('hello')\n", "src/b.ts": "const hello = 1\n",
                     "node_modules/x/i.js": "hello\n", "README.md": "Hello world\n"}.items():
            os.makedirs(os.path.dirname(os.path.join(self.d, f)) or self.d, exist_ok=True)
            Path(self.d, f).write_text(t)
        os.utime(os.path.join(self.d, "src/a.py"), (1, 1))
        p = mock.patch.object(sw, "_rg", return_value="")          # the Python way (no ripgrep here)
        p.start()
        self.addCleanup(p.stop)
        g = mock.patch.object(sw.shutil, "which", side_effect=lambda x: None if x in ("git", "rg") else shutil.which(x))
        g.start()
        self.addCleanup(g.stop)

    def test_glob_newest_first_server_paths_no_heavy(self):
        out = sw.pc_glob(self.d, self.SR, {"pattern": "**/*.*"}).splitlines()
        self.assertEqual(out[-1], self.SR + "/src/a.py")
        self.assertNotIn(self.SR + "/node_modules/x/i.js", out)
        self.assertEqual(sw.pc_glob(self.d, self.SR, {"pattern": "*.ts", "path": self.SR + "/src"}), self.SR + "/src/b.ts")
        self.assertEqual(sw.pc_glob(self.d, self.SR, {"pattern": "*.go"}), "No files found")

    def test_grep_modes(self):
        self.assertEqual(sw.pc_grep(self.d, self.SR, {"pattern": "hello"}),
                         "Found 2 files\n" + self.SR + "/src/a.py\n" + self.SR + "/src/b.ts")
        self.assertIn(self.SR + "/README.md", sw.pc_grep(self.d, self.SR, {"pattern": "hello", "-i": True}))
        self.assertEqual(sw.pc_grep(self.d, self.SR, {"pattern": "hello", "output_mode": "content", "glob": "*.py"}),
                         self.SR + "/src/a.py:2:print('hello')")
        self.assertEqual(sw.pc_grep(self.d, self.SR, {"pattern": "o", "output_mode": "count", "path": "src/a.py"}),
                         self.SR + "/src/a.py:2")
        self.assertEqual(sw.pc_grep(self.d, self.SR, {"pattern": "zzz"}), "No files found")

    def test_never_outside_or_heavy(self):
        with self.assertRaises(PermissionError):
            sw.pc_grep(self.d, self.SR, {"pattern": "x", "path": "/etc"})
        with self.assertRaises(PermissionError):
            sw.pc_glob(self.d, self.SR, {"pattern": "*", "path": self.SR + "/../.."})
        with self.assertRaises(FileNotFoundError):
            sw.pc_grep(self.d, self.SR, {"pattern": "x", "path": "node_modules"})

    @unittest.skipUnless(shutil.which("rg"), "ripgrep not installed")
    def test_ripgrep_same_answer(self):
        mock.patch.stopall()
        self.assertEqual(sw.pc_grep(self.d, self.SR, {"pattern": "hello", "output_mode": "content", "glob": "*.py"}),
                         self.SR + "/src/a.py:2:print('hello')")


class OnpcMcp(unittest.TestCase):
    """onpc --mcp / --mcp-serve / --local on the server for a Local only project."""
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.f = os.path.join(self.home, "onpc")
        Path(self.f).write_text(sw.ONPC_PY)
        self.proj = os.path.realpath(os.path.join(self.home, "proj"))
        os.makedirs(self.proj)
        d = os.path.join(self.home, ".cache/simabworkspace/pc")
        os.makedirs(d)
        Path(d, "projects.json").write_text(json.dumps({self.proj: "local"}))

    def onpc(self, *a, stdin=b""):
        return subprocess.run([sys.executable, self.f, *a], cwd=self.proj, input=stdin, capture_output=True,
                              timeout=20, env=dict(os.environ, HOME=self.home))

    def test_mcp_config(self):
        cfg = json.loads(self.onpc("--mcp").stdout)
        self.assertEqual(cfg["mcpServers"]["pc"]["args"][-1], "--mcp-serve")

    def test_mcp_serve_lists_tools_and_says_not_connected(self):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "Grep", "arguments": {"pattern": "x"}}}]
        out = [json.loads(x) for x in self.onpc("--mcp-serve", stdin="\n".join(json.dumps(m) for m in msgs).encode()).stdout.splitlines()]
        self.assertEqual([o["id"] for o in out], [1, 2, 3])
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual([t["name"] for t in out[1]["result"]["tools"]], ["Grep", "Glob", "Read", "Edit", "Write"])
        self.assertEqual([t["annotations"]["readOnlyHint"] for t in out[1]["result"]["tools"]], [True, True, True, False, False])
        self.assertTrue(out[2]["result"]["isError"])
        self.assertIn("not connected", out[2]["result"]["content"][0]["text"])

    def test_local_settings_allow_pc_tools(self):
        r = self.onpc("--local")          # not mounted → 3
        self.assertEqual(r.returncode, 3)

    def test_chat_script_passes_mcp(self):
        s = sw.chat_remote_script("~/proj", "claude", ["--x"])
        self.assertIn('--mcp config', s.replace("-config", " config"))
        self.assertIn("--disallowedTools Grep,Glob,Read,Edit,Write,MultiEdit", s)


class PcReadEditWrite(unittest.TestCase):
    """Read / Edit / Write of a Local only project on the computer: one request each."""
    SR = "/home/u/proj"

    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "node_modules", "x"))
        Path(self.d, "a.txt").write_text("one\ntwo\nthree\n")
        Path(self.d, "node_modules", "x", "i.d.ts").write_text("export {}\n")
        Path(self.d, "win.txt").write_bytes(b"a\r\nb\r\n")
        Path(self.d, "p.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0")
        Path(self.d, "bin").write_bytes(b"\0\1\2")

    def test_read_numbered_offset_limit(self):
        self.assertEqual(sw.pc_read(self.d, self.SR, {"file_path": self.SR + "/a.txt"})["text"],
                         "     1\tone\n     2\ttwo\n     3\tthree")
        self.assertEqual(sw.pc_read(self.d, self.SR, {"file_path": self.SR + "/a.txt", "offset": 2, "limit": 1})["text"],
                         "     2\ttwo")
        self.assertIn("export", sw.pc_read(self.d, self.SR, {"file_path": self.SR + "/node_modules/x/i.d.ts"})["text"])

    def test_read_image_binary_outside(self):
        r = sw.pc_read(self.d, self.SR, {"file_path": self.SR + "/p.png"})
        self.assertEqual(r["mime"], "image/png")
        with self.assertRaises(ValueError):
            sw.pc_read(self.d, self.SR, {"file_path": self.SR + "/bin"})
        with self.assertRaises(PermissionError):
            sw.pc_read(self.d, self.SR, {"file_path": "/etc/passwd"})

    def test_edit_unique_all_and_crlf(self):
        f = self.SR + "/a.txt"
        sw.pc_edit(self.d, self.SR, {"file_path": f, "old_string": "two", "new_string": "2"})
        self.assertEqual(Path(self.d, "a.txt").read_text(), "one\n2\nthree\n")
        with self.assertRaises(ValueError):
            sw.pc_edit(self.d, self.SR, {"file_path": f, "old_string": "nope", "new_string": "x"})
        with self.assertRaises(ValueError):
            sw.pc_edit(self.d, self.SR, {"file_path": f, "old_string": "e", "new_string": "E"})
        sw.pc_edit(self.d, self.SR, {"file_path": f, "old_string": "e", "new_string": "E", "replace_all": True})
        self.assertEqual(Path(self.d, "a.txt").read_text(), "onE\n2\nthrEE\n")
        sw.pc_edit(self.d, self.SR, {"file_path": self.SR + "/win.txt", "old_string": "a\nb", "new_string": "a\nX"})
        self.assertEqual(Path(self.d, "win.txt").read_bytes(), b"a\r\nX\r\n")

    def test_write_new_and_keeps_crlf(self):
        r = sw.pc_write(self.d, self.SR, {"file_path": self.SR + "/new/dir/f.py", "content": "x = 1\n"})
        self.assertTrue(r["text"].startswith("File created successfully"))
        self.assertEqual(Path(self.d, "new/dir/f.py").read_text(), "x = 1\n")
        sw.pc_write(self.d, self.SR, {"file_path": self.SR + "/win.txt", "content": "p\nq\n"})
        self.assertEqual(Path(self.d, "win.txt").read_bytes(), b"p\r\nq\r\n")


class PcEditPermission(unittest.TestCase):
    def test_follows_chat_mode(self):
        self.assertIs(sw.pc_edit_verdict("mcp__pc__Edit", "acceptEdits"), True)
        self.assertIs(sw.pc_edit_verdict("mcp__pc__Write", "auto"), True)
        self.assertIs(sw.pc_edit_verdict("mcp__pc__Edit", "plan"), False)
        self.assertIsNone(sw.pc_edit_verdict("mcp__pc__Edit", "default"))
        self.assertIsNone(sw.pc_edit_verdict("Edit", "acceptEdits"))

    def test_link_is_compressed(self):
        conf = mock.Mock()
        with mock.patch.object(sw, "ssh_base", return_value=["ssh"]):
            self.assertIn("Compression=yes", sw.pc_link_argv(conf, "/s", 1))


if __name__ == "__main__":
    unittest.main()
