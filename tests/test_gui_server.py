"""Tests for gui/server.py — run with:  python3 -m unittest discover -s tests -v"""
import base64
import http.client
import json
import os
import re
import shlex
import shutil
import subprocess
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gui"))
import server as sw  # noqa: E402


class TmpHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        self.env.start()
        self.store = sw.Store(Path(self.tmp.name) / "gui.json")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class BashConfig(unittest.TestCase):
    def test_parse_printf_q_values(self):
        text = "# simabworkspace settings\nSSH_USER=esi\nSSH_HOST=203.0.113.10\nSSH_PORT=225\n" \
               "SSH_KEY=/home/a\\ b/.ssh/id_ed25519\nbad-key=x\n"
        c = sw.parse_bash_config(text)
        self.assertEqual(c["SSH_USER"], "esi")
        self.assertEqual(c["SSH_PORT"], "225")
        self.assertEqual(c["SSH_KEY"], "/home/a b/.ssh/id_ed25519")
        self.assertNotIn("bad-key", c)

    def test_empty_quoted_value(self):
        self.assertEqual(sw.parse_bash_config("SSH_KEY=''\n")["SSH_KEY"], "")


class StoreTests(TmpHome):
    def test_defaults_and_settings_validation(self):
        self.assertEqual(self.store.settings["fontSize"], 14)
        s = self.store.update_settings({"fontSize": 99, "copyOnSelect": False, "unknown": 1, "composer": "always"})
        self.assertEqual(s["fontSize"], 32)
        self.assertFalse(s["copyOnSelect"])
        self.assertNotIn("unknown", s)
        with self.assertRaises(sw.ApiError):
            self.store.update_settings({"composer": "sometimes"})
        with self.assertRaises(sw.ApiError):
            self.store.update_settings({"fontSize": "big"})
        self.assertEqual(sw.Store(self.store.path).settings["composer"], "always")
        self.assertEqual(oct(self.store.path.stat().st_mode & 0o777), "0o600")

    def test_commands_roundtrip_and_validation(self):
        out = self.store.set_commands("cheshmeh", [
            {"name": "وب", "cmd": "npm run start-local-server", "where": "local", "url": "http://localhost:4200"},
            {"name": "logs", "cmd": "docker compose logs -f", "where": "server"},
        ])
        self.assertEqual(len(out), 2)
        self.assertTrue(all(len(c["id"]) >= 4 for c in out))
        self.assertEqual(self.store.command("cheshmeh", out[0]["id"])["name"], "وب")
        # ids are kept on re-save
        again = self.store.set_commands("cheshmeh", out)
        self.assertEqual([c["id"] for c in again], [c["id"] for c in out])
        for bad in ([{"name": "", "cmd": "x"}], [{"name": "a", "cmd": ""}],
                    [{"name": "a", "cmd": "x", "where": "mars"}], [{"name": "a", "cmd": "x", "url": "ftp://x"}], "nope"):
            with self.assertRaises(sw.ApiError):
                self.store.set_commands("p", bad)

    def test_project_color(self):
        self.assertEqual(self.store.set_color("p", "#4F8CFF"), "#4f8cff")
        self.assertEqual(sw.merge_projects([], [{"name": "p", "local": "/tmp", "remote": ""}], self.store.data["projects"])[0]["color"], "#4f8cff")
        self.assertEqual(self.store.set_color("p", ""), "")
        for bad in ("red", "#12345", "#gggggg", "url(x)"):
            with self.assertRaises(sw.ApiError):
                self.store.set_color("p", bad)

    def test_manual_projects(self):
        self.store.add_manual({"name": "site", "local": "~/site", "remote": ""})
        self.assertEqual(self.store.manual()[0]["local"], os.path.expanduser("~/site"))
        with self.assertRaises(sw.ApiError):
            self.store.add_manual({"name": "bad name", "local": "/x"})
        with self.assertRaises(sw.ApiError):
            self.store.add_manual({"name": "ok", "local": "", "remote": ""})
        self.store.remove_manual("site")
        self.assertEqual(self.store.manual(), [])


SESSIONS = [
    {"name": "cheshmeh", "identifier": "sync_1", "status": "watching", "paused": False,
     "alpha": {"protocol": "local", "path": "/tmp"},
     "beta": {"protocol": "ssh", "user": "esi", "host": "203.0.113.10", "port": 225, "path": "/home/esi/cheshmeh"},
     "conflicts": [{"root": "a"}]},
    {"identifier": "sync_2", "status": "halted-on-root-deletion", "paused": False, "lastError": "boom",
     "alpha": {"path": "/nonexistent-xyz"}, "beta": {"host": "h", "path": "/srv/x"}},
    {"name": "paused-one", "status": "watching", "paused": True, "alpha": {"path": "/tmp"}, "beta": {"host": "h", "path": "/p"}},
]


class ActivityLog(TmpHome):
    def test_add_list_filter(self):
        a = sw.Activity(Path(self.tmp.name) / "activity.jsonl")
        a.add("prompt", "cheshmeh", "باگ پرداخت", session="s1")
        a.add("run", "shop", "web", cmd="npm start")
        a.add("git-commit", "cheshmeh", "fix", repo="api")
        items = a.list()
        self.assertEqual([i["kind"] for i in items], ["git-commit", "run", "prompt"])     # newest first
        self.assertEqual(a.list(project="shop")[0]["cmd"], "npm start")
        self.assertEqual(len(a.list(query="پرداخت")), 1)
        self.assertEqual(len(a.list(limit=1)), 1)
        self.assertNotIn("session", a.add("x", "", "t", session=""))                     # empty extras dropped

    def test_app_records_runs_and_prompts(self):
        app = sw.App(self.store, sw.ServerConf("", "h"))
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sw, "mutagen_sessions", return_value=[]):
            self.store.add_manual({"name": "loc", "local": d})
            cmds = self.store.set_commands("loc", [{"name": "hi", "cmd": "exit 3", "where": "local"}])
            app.invalidate()
            info = app.create_session({"kind": "run", "project": "loc", "cmdId": cmds[0]["id"]})
            self.assertTrue(wait_for(lambda: any(i["kind"] == "run-exit" for i in app.activity.list())))
            done = next(i for i in app.activity.list() if i["kind"] == "run-exit")
            self.assertEqual((done["project"], done["code"], done["cmdId"]), ("loc", 3, cmds[0]["id"]))
            self.assertEqual(app.activity.list()[-1]["kind"], "run")
            app.close_session(info["id"])
        # a chat message is recorded with its conversation id
        built = {"make_argv": lambda *a: ["/bin/sh", "-c", "cat >/dev/null"], "cwd": "/tmp", "title": "t", "resume": "",
                 "spec": {"kind": "chat", "project": "loc", "where": "server"}}
        c = sw.ChatSession("c", built)
        c.log = app.activity.add
        c.session_id = "11111111-2222-3333-4444-555555555555"
        c.start()
        c.handle({"t": "send", "content": [{"type": "text", "text": "سلام"}, {"type": "image", "source": {}}]})
        it = app.activity.list()[0]
        self.assertEqual((it["kind"], it["text"], it["session"], it["images"]), ("prompt", "سلام", c.session_id, 1))
        c.kill(True)


class ProjectMerge(unittest.TestCase):
    def test_merge(self):
        ps = sw.merge_projects(SESSIONS, [{"name": "manual1", "local": "/tmp", "remote": ""},
                                          {"name": "cheshmeh", "local": "/dup", "remote": ""}],
                               {"cheshmeh": {"commands": [{"id": "abcd", "name": "a", "cmd": "b", "where": "local", "url": ""}]}})
        by = {p["name"]: p for p in ps}
        self.assertEqual(set(by), {"cheshmeh", "sync_2", "paused-one", "manual1"})
        c = by["cheshmeh"]
        self.assertEqual((c["local"], c["remote"], c["source"]), ("/tmp", "/home/esi/cheshmeh", "mutagen"))
        self.assertEqual(c["endpoint"], {"user": "esi", "host": "203.0.113.10", "port": "225"})
        self.assertEqual(c["sync"]["state"], "ok")
        self.assertEqual(c["sync"]["conflicts"], 1)
        self.assertTrue(c["localExists"])
        self.assertEqual(len(c["commands"]), 1)
        self.assertEqual(by["sync_2"]["sync"]["state"], "err")
        self.assertFalse(by["sync_2"]["localExists"])
        self.assertEqual(by["paused-one"]["sync"]["state"], "paused")
        self.assertIsNone(by["manual1"]["sync"])

    def test_transfer_progress(self):
        sess = {"name": "up", "status": "staging-beta", "paused": False, "alpha": {"path": "/tmp"}, "beta": {"host": "h", "path": "/r"},
                "betaState": {"stagingProgress": {"receivedFiles": 30, "expectedFiles": 120, "receivedSize": 5_000_000, "expectedSize": 20_000_000}}}
        pr = sw.merge_projects([sess], [], {})[0]["sync"]["progress"]
        self.assertEqual(pr, {"files": 30, "totalFiles": 120, "bytes": 5_000_000, "totalBytes": 20_000_000, "percent": 25})
        done = dict(sess, status="watching")
        self.assertIsNone(sw.merge_projects([done], [], {})[0]["sync"]["progress"])
        self.assertIsNone(sw.sync_progress({"a": {"expectedFiles": 0}}))

    def test_sync_state(self):
        self.assertEqual(sw.sync_state("staging-beta", False)[0], "busy")
        self.assertEqual(sw.sync_state("connecting-beta", False)[0], "warn")
        self.assertEqual(sw.sync_state("weird", False), ("unknown", "weird"))


class CommandBuilding(TmpHome):
    def setUp(self):
        super().setUp()
        self.conf = sw.ServerConf("esi", "1.2.3.4", "225", "")
        self.projects = sw.merge_projects(SESSIONS, [], {})

    def build(self, **spec):
        return sw.build_spec(spec, self.conf, self.store, self.projects)

    def test_remote_path_expr(self):
        self.assertEqual(sw.remote_path_expr("~"), '"$HOME"')
        self.assertEqual(sw.remote_path_expr("~/a b"), '"$HOME"/\'a b\'')
        self.assertEqual(sw.remote_path_expr("/x/y"), "/x/y")

    def test_server_shell(self):
        b = self.build(kind="shell", where="server", project="cheshmeh")
        argv = b["argv"]
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-t", argv)
        self.assertIn("esi@203.0.113.10", argv)        # endpoint of the sync session wins
        self.assertIn("ControlMaster=auto", argv)
        script = argv[-1]
        self.assertTrue(script.startswith("cd /home/esi/cheshmeh"))
        self.assertTrue(script.endswith('exec "${SHELL:-/bin/bash}" -l'))
        self.assertEqual(b["title"], "cheshmeh · server")

    def test_server_claude_continue_and_tmux(self):
        self.store.update_settings({"serverPersist": True, "claudeCmd": "claude --model opus", "sshMultiplex": False})
        b = self.build(kind="claude", where="server", project="cheshmeh", claudeMode="continue", slot=1)
        argv = b["argv"]
        self.assertNotIn("ControlMaster=auto", argv)
        script = argv[-1]
        self.assertIn("tmux new-session -A -s sw-cheshmeh-claude-2", script)
        self.assertIn("claude --model opus --continue ||", script)
        self.assertIn("anthropic.claude-code-*/resources/native-binary", script)
        self.assertEqual(b["title"], "Claude · cheshmeh · terminal")
        # the script must be valid shell
        self.assertTrue(shlex.split(script))
        self.assertEqual(subprocess.run(["bash", "-n", "-c", script]).returncode, 0)

    def test_claude_is_always_on_server(self):
        b = self.build(kind="claude", where="local", project="cheshmeh")
        self.assertEqual(b["spec"]["where"], "server")
        self.assertEqual(b["argv"][0], "ssh")

    def test_claude_falls_back_to_vscode_extension_binary(self):
        with tempfile.TemporaryDirectory() as home:
            nb = Path(home, ".vscode-server/extensions/anthropic.claude-code-2.1.9-linux-x64/resources/native-binary")
            nb.mkdir(parents=True)
            (nb / "claude").write_text("#!/bin/sh\necho fake-claude \"$@\"\n")
            (nb / "claude").chmod(0o755)
            old = Path(home, ".vscode-server/extensions/anthropic.claude-code-2.1.10-linux-x64/resources/native-binary")
            old.mkdir(parents=True)
            (old / "claude").write_text("#!/bin/sh\necho newest \"$@\"\n")
            (old / "claude").chmod(0o755)
            inner = sw.claude_inner("claude", "continue")
            r = subprocess.run(["/bin/sh", "-c", inner], capture_output=True, text=True, timeout=10,
                               env={"HOME": home, "PATH": "/usr/bin:/bin"})
            self.assertEqual(r.stdout.strip(), "newest --continue")   # 2.1.10 > 2.1.9 (sort -V)

    def test_claude_terminal_opens_a_given_conversation(self):
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        b = self.build(kind="claude", where="server", project="cheshmeh", resume=sid)
        self.assertIn("claude --resume " + sid + " ||", b["argv"][-1])
        self.assertEqual(b["spec"]["resume"], sid)
        self.assertEqual(b["title"], "Claude · cheshmeh · terminal")
        self.assertIn("claude --continue ||", self.build(kind="claude", where="server", project="cheshmeh", claudeMode="continue")["argv"][-1])
        with self.assertRaises(sw.ApiError):
            self.build(kind="claude", where="server", project="cheshmeh", resume="x; rm -rf ~")

    def test_home_server_shell_without_project(self):
        b = self.build(kind="shell", where="server")
        self.assertIn("esi@1.2.3.4", b["argv"])
        self.assertTrue(b["argv"][-1].startswith('cd "$HOME"'))

    def test_local_shell_and_run(self):
        self.store.update_settings({"localShell": "/bin/sh"})
        b = self.build(kind="shell", where="local", project="cheshmeh")
        self.assertEqual(b["argv"], ["/bin/sh", "-l"])
        self.assertEqual(b["cwd"], "/tmp")
        cmds = self.store.set_commands("cheshmeh", [{"name": "web", "cmd": "echo hi", "where": "local", "url": "http://localhost:4200"}])
        self.projects = sw.merge_projects(SESSIONS, [], self.store.data["projects"])
        r = self.build(kind="run", project="cheshmeh", cmdId=cmds[0]["id"], where="server")
        self.assertEqual(r["argv"], ["/bin/sh", "-lic", "echo hi"])    # command's own `where` wins
        self.assertEqual(r["url"], "http://localhost:4200")
        self.assertEqual(r["spec"]["cmdId"], cmds[0]["id"])

    def test_run_command_in_subfolder(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "web").mkdir()
            sess = [dict(SESSIONS[0], alpha={"path": d})]
            cmds = self.store.set_commands("cheshmeh", [
                {"name": "web", "cmd": "npm start", "where": "local", "cwd": "/web/"},
                {"name": "api", "cmd": "go test ./...", "where": "server", "cwd": "services/api"},
                {"name": "bad", "cmd": "x", "where": "local", "cwd": "missing"}])
            self.assertEqual(cmds[0]["cwd"], "web")
            self.projects = sw.merge_projects(sess, [], self.store.data["projects"])
            self.assertEqual(self.build(kind="run", project="cheshmeh", cmdId=cmds[0]["id"])["cwd"], os.path.join(d, "web"))
            srv = self.build(kind="run", project="cheshmeh", cmdId=cmds[1]["id"])
            self.assertTrue(srv["argv"][-1].startswith("cd /home/esi/cheshmeh/services/api "))
            with self.assertRaises(sw.ApiError):
                self.build(kind="run", project="cheshmeh", cmdId=cmds[2]["id"])
        for bad in ("../etc", "a/../../b", "~/x"):
            with self.assertRaises(sw.ApiError):
                sw.clean_subdir(bad)
        self.assertEqual(sw.clean_subdir("./a//b/"), "a/b")
        self.assertEqual(sw.clean_subdir(""), "")

    def test_claude_mode_and_model_are_remembered(self):
        conf = sw.ServerConf("esi", "1.2.3.4", "225", "")
        self.store.set_chat_prefs("cheshmeh", mode="plan", model="opus")
        b = sw.build_chat({"kind": "chat", "project": "cheshmeh"}, conf, self.store, self.projects)
        self.assertEqual((b["mode"], b["model"]), ("plan", "opus"))
        # another project without its own choice gets the last one used anywhere
        self.assertEqual((self.store.settings["chatMode"], self.store.settings["chatModel"]), ("plan", "opus"))
        b2 = sw.build_chat({"kind": "chat"}, conf, self.store, self.projects)
        self.assertEqual((b2["mode"], b2["model"]), ("plan", "opus"))
        self.store.set_chat_prefs("cheshmeh", mode="evil", model="bad model!")         # ignored
        self.assertEqual(self.store.data["projects"]["cheshmeh"]["chatMode"], "plan")
        # changing it inside a chat stores it
        built = {"make_argv": lambda *a: ["/bin/sh", "-c", "cat >/dev/null"], "cwd": "/tmp", "title": "t", "resume": "",
                 "spec": {"kind": "chat", "project": "cheshmeh", "where": "server"}}
        c = sw.ChatSession("c", built)
        c.remember = self.store.set_chat_prefs
        c.start()
        c.handle({"t": "mode", "mode": "acceptEdits"})
        c.handle({"t": "model", "model": "sonnet"})
        c.kill(True)
        pp = self.store.data["projects"]["cheshmeh"]
        self.assertEqual((pp["chatMode"], pp["chatModel"]), ("acceptEdits", "sonnet"))
        self.assertEqual(sw.Store(self.store.path).data["projects"]["cheshmeh"]["chatModel"], "sonnet")   # saved to disk

    def test_add_local_and_add_server_tabs(self):
        with tempfile.TemporaryDirectory() as d:
            b = self.build(kind="add-local", local=d, remote="~/shop", name="shop", git=False)
            self.assertEqual(b["argv"][2:], ["add-local", d, "--yes", "--git", "off", "--remote", "~/shop", "--name", "shop"])
            self.assertTrue(b["argv"][1].endswith("simabworkspace.sh"))
            self.assertEqual(b["title"], "upload · " + os.path.basename(d))
        self.assertEqual(self.build(kind="add-server")["argv"][2:], ["add-server"])
        with self.assertRaises(sw.ApiError):
            self.build(kind="add-local", local="/nonexistent-xyz")
        with self.assertRaises(sw.ApiError):
            self.build(kind="add-local", local="/tmp", name="bad name")

    def test_errors(self):
        with self.assertRaises(sw.ApiError):
            self.build(kind="evil")
        with self.assertRaises(sw.ApiError):
            self.build(kind="shell", where="local", project="missing")
        with self.assertRaises(sw.ApiError):
            self.build(kind="shell", where="local", project="sync_2")     # not synced locally
        with self.assertRaises(sw.ApiError):
            self.build(kind="run", project="cheshmeh", cmdId="nope")
        with self.assertRaises(sw.ApiError):
            sw.build_spec({"kind": "shell", "where": "server"}, sw.ServerConf("", "h"), self.store, [])

    def test_tmux_name(self):
        self.assertEqual(sw.tmux_name("a.b c", "shell", 0), "sw-a-b-c-shell")
        self.assertEqual(sw.tmux_name("", "claude", 2), "sw-home-claude-3")


class Suggestions(unittest.TestCase):
    def test_package_json_and_compose(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "web").mkdir()
            (root / "web" / "package.json").write_text(json.dumps({"scripts": {"start-local-server": "ng serve", "build": "ng build", "prebuild": "x"}}))
            (root / "node_modules" / "x").mkdir(parents=True)
            (root / "node_modules" / "x" / "package.json").write_text(json.dumps({"scripts": {"evil": "x"}}))
            (root / "docker-compose.yml").write_text("services: {}")
            (root / "docker" / "local").mkdir(parents=True)
            (root / "docker" / "local" / "compose.laptop.yml").write_text("services: {}")
            out = sw.suggestions(d)
            cmds = [s["cmd"] for s in out]
            self.assertEqual((out[0]["cmd"], out[0]["cwd"]), ("npm run start-local-server", "web"))
            self.assertIn("npm run build", cmds)
            self.assertFalse(any("prebuild" in c or "evil" in c for c in cmds))
            self.assertTrue(any(s["where"] == "server" and "logs" in s["cmd"] for s in out))
            self.assertTrue(any("compose.laptop.yml" in c for c in cmds))
        self.assertEqual(sw.suggestions("/nonexistent-xyz"), [])


class WebSocketFrames(unittest.TestCase):
    def test_accept_key_rfc_example(self):
        self.assertEqual(sw.ws_accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_roundtrip_lengths(self):
        import io
        for n in (0, 5, 125, 126, 65535, 65536, 200000):
            payload = os.urandom(n)
            frame = sw.ws_encode(payload, 2, mask=b"\x01\x02\x03\x04")
            fin, op, data = sw.ws_read_frame(io.BytesIO(frame))
            self.assertTrue(fin)
            self.assertEqual(op, 2)
            self.assertEqual(data, payload)

    def test_unmasked_client_frame_rejected(self):
        import io
        with self.assertRaises(sw.WebSocketClosed):
            sw.ws_read_frame(io.BytesIO(sw.ws_encode(b"hi")))


class WindowsVsCode(TmpHome):
    def test_key_copied_and_windows_paths_used(self):
        base = Path(self.tmp.name)
        key = base / "id_ed25519"
        key.write_text("PRIVATE")
        Path(str(key) + ".pub").write_text("PUB")
        win_home = base / "winhome"
        win_home.mkdir()
        conf = sw.ServerConf("esi", "1.2.3.4", "225", str(key))
        sw.prepare_windows_vscode(conf, win_home, to_windows=lambda p: "C:\\Users\\me\\.ssh\\" + p.name)
        self.assertEqual((win_home / ".ssh" / "simabworkspace_key").read_text(), "PRIVATE")
        self.assertEqual((win_home / ".ssh" / "simabworkspace_key.pub").read_text(), "PUB")
        cfg = (win_home / ".ssh" / "config").read_text()
        self.assertIn("Host simabworkspace-server", cfg)
        self.assertIn("IdentityFile C:\\Users\\me\\.ssh\\simabworkspace_key", cfg)
        self.assertIn("    Port 225", cfg)


class Fingerprint(unittest.TestCase):
    def test_stable_and_short(self):
        a = sw.code_fingerprint()
        self.assertEqual(a, sw.code_fingerprint())
        self.assertEqual(len(a), 12)


class SshAlias(TmpHome):
    def test_block_is_replaced_not_duplicated(self):
        f = Path(self.tmp.name) / "ssh_config"
        f.write_text("Host other\n    User me\n")
        conf = sw.ServerConf("esi", "1.2.3.4", "225", "/k")
        sw.write_vscode_ssh_alias(conf, f)
        sw.write_vscode_ssh_alias(conf, f)
        text = f.read_text()
        self.assertEqual(text.count(sw.GUI_MARK_BEGIN), 1)
        self.assertTrue(text.startswith(sw.GUI_MARK_BEGIN))
        self.assertIn("Host other", text)
        self.assertIn("    Port 225", text)
        self.assertIn("    IdentityFile /k", text)


def wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class PtySession(unittest.TestCase):
    def built(self, script):
        return {"argv": ["/bin/sh", "-c", script], "cwd": "/tmp", "title": "t",
                "spec": {"kind": "run", "project": "", "where": "local"}, "url": ""}

    def test_output_exit_code_and_size(self):
        s = sw.Session("x", self.built("stty size; printf 'سلام\\n'; exit 3"), cols=100, rows=30)
        s.start()
        self.assertTrue(wait_for(lambda: s.exit_code is not None))
        out = "".join(s.chunks)
        self.assertIn("30 100", out)
        self.assertIn("سلام", out)
        self.assertEqual(s.exit_code, 3)
        self.assertFalse(s.alive)

    def test_input_stop_and_restart(self):
        s = sw.Session("y", self.built("read line; echo got:$line; sleep 30"))
        s.start()
        time.sleep(0.2)
        s.write("abc\r")
        self.assertTrue(wait_for(lambda: "got:abc" in "".join(s.chunks)))
        s.stop()                                    # Ctrl+C ends sleep
        self.assertTrue(wait_for(lambda: s.exit_code is not None, 8))
        s.restart()
        self.assertTrue(s.alive)
        self.assertEqual(s.runs, 2)
        s.kill(wait=True)
        self.assertTrue(wait_for(lambda: not s.alive, 5))


class HttpServer(TmpHome):
    def setUp(self):
        super().setUp()
        self.store.update_settings({"localShell": "/bin/sh"})
        self.app = sw.App(self.store, sw.ServerConf("", "h"))
        self.httpd = sw.make_server(self.app, 0)
        self.port = self.app.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.mut = mock.patch.object(sw, "mutagen_sessions", return_value=[])
        self.mut.start()

    def tearDown(self):
        self.mut.stop()
        self.app.shutdown_sessions()
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, method, path, body=None, token=True, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            headers["X-Token"] = self.app.token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def test_auth_and_host_checks(self):
        self.assertEqual(self.req("GET", "/api/ping", token=False)[0], 401)
        self.assertEqual(self.req("GET", "/api/ping", host="evil.com")[0], 403)
        st, d = self.req("GET", "/api/ping")
        self.assertEqual((st, d["ok"]), (200, True))
        st, raw = self.req("GET", "/", token=False)
        self.assertEqual(st, 200)
        self.assertIn(b"Simab Workspace", raw)
        self.assertEqual(self.req("GET", "/../server.py", token=False)[0], 404)
        self.assertEqual(self.req("GET", "/vendor/xterm.js", token=False)[0], 200)

    def test_state_settings_commands(self):
        st, d = self.req("GET", "/api/state")
        self.assertEqual(st, 200)
        self.assertFalse(d["server"]["configured"])
        st, d = self.req("POST", "/api/settings", {"fontSize": 16})
        self.assertEqual(d["settings"]["fontSize"], 16)
        st, d = self.req("POST", "/api/manual", {"name": "loc", "local": self.tmp.name})
        self.assertEqual(st, 200)
        st, d = self.req("POST", "/api/commands/loc", {"commands": [{"name": "hi", "cmd": "echo hi", "where": "local"}]})
        self.assertEqual(st, 200)
        st, d = self.req("GET", "/api/projects")
        self.assertEqual(d["projects"][0]["commands"][0]["name"], "hi")
        st, d = self.req("POST", "/api/sessions", {"kind": "shell", "where": "server"})
        self.assertEqual(st, 400)       # server not configured → Persian error
        self.assertIn("simabworkspace", d["error"])
        self.assertEqual(self.req("POST", "/api/settings", [1, 2])[0], 400)

    def ws_connect(self, sid, origin=None):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET /ws?id={sid}&t={self.app.token} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                   f"Origin: {origin or f'http://127.0.0.1:{self.port}'}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        f = s.makefile("rb")
        status = f.readline()
        while f.readline() not in (b"\r\n", b""):
            pass
        return s, f, status

    def test_session_over_websocket(self):
        st, info = self.req("POST", "/api/sessions", {"kind": "shell", "where": "local", "cols": 90, "rows": 20})
        self.assertEqual(st, 200, info)
        sid = info["id"]
        self.assertTrue(info["alive"])
        s, f, status = self.ws_connect(sid)
        self.assertIn(b"101", status)
        _, _, data = sw.ws_read_frame(f, require_mask=False)
        self.assertEqual(json.loads(data)["t"], "s")
        msg = json.dumps({"t": "i", "d": "echo ws-$((40+2))\r"}).encode()
        s.sendall(sw.ws_encode(msg, 1, mask=b"abcd"))
        got = ""
        end = time.time() + 5
        while "ws-42" not in got and time.time() < end:
            _, _, data = sw.ws_read_frame(f, require_mask=False)
            m = json.loads(data)
            if m["t"] == "o":
                got += m["d"]
        self.assertIn("ws-42", got)
        # reload: a second client gets the history as snapshot
        s2, f2, _ = self.ws_connect(sid)
        _, _, data = sw.ws_read_frame(f2, require_mask=False)
        self.assertIn("ws-42", json.loads(data)["d"])
        s.close()
        s2.close()
        st, d = self.req("POST", f"/api/sessions/{sid}/rename", {"title": "تب من"})
        self.assertEqual(d["title"], "تب من")
        self.assertEqual(self.store.data["lastTabs"][0]["title"], "تب من")
        self.assertEqual(self.req("DELETE", f"/api/sessions/{sid}")[0], 200)
        self.assertEqual(self.req("DELETE", f"/api/sessions/{sid}")[0], 404)

    def test_websocket_rejects_foreign_origin(self):
        st, info = self.req("POST", "/api/sessions", {"kind": "shell", "where": "local"})
        s, f, status = self.ws_connect(info["id"], origin="http://evil.com")
        self.assertIn(b"403", status)
        s.close()


FAKE_CLAUDE = r'''
import sys, json
def out(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
SID = "11111111-2222-3333-4444-555555555555"
for line in sys.stdin:
    m = json.loads(line)
    if m.get("type") == "control_request":
        r = m["request"]
        if r["subtype"] == "initialize":
            out({"type": "control_response", "response": {"subtype": "success", "request_id": m["request_id"],
                 "response": {"commands": [{"name": "compact", "description": "x"}]}}})
        else:
            out({"type": "seen_control", "subtype": r["subtype"], "args": r})
    elif m.get("type") == "user":
        out({"type": "system", "subtype": "init", "session_id": SID, "permissionMode": "default"})
        out({"type": "user", "message": m["message"], "session_id": SID})
        out({"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1"}}, "session_id": SID})
        out({"type": "control_request", "request_id": "perm-1", "request": {"subtype": "can_use_tool",
             "tool_name": "Bash", "input": {"command": "ls"}, "permission_suggestions": [{"type": "addRules"}]}})
        ans = json.loads(sys.stdin.readline())
        out({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "answer:" + json.dumps(ans["response"]["response"])}]}, "session_id": SID})
        out({"type": "result", "subtype": "success", "session_id": SID, "total_cost_usd": 0.01})
'''


class ChatProtocol(unittest.TestCase):
    def make(self, resume=""):
        argvs = []

        def make_argv(mode, model, resume_):
            argvs.append((mode, model, resume_))
            return [sys.executable, "-c", FAKE_CLAUDE]
        built = {"make_argv": make_argv, "cwd": "/tmp", "title": "Claude · p", "resume": resume, "mode": "default",
                 "model": "", "spec": {"kind": "chat", "project": "p", "where": "server"}}
        changes = []
        s = sw.ChatSession("c1", built, on_change=changes.append)
        s.start()
        self.addCleanup(s.kill, True)
        return s, argvs, changes

    def types(self, s):
        return [e.get("type") for e in s.events]

    def test_full_turn_with_permission(self):
        s, argvs, changes = self.make()
        self.assertTrue(wait_for(lambda: s.init is not None))
        self.assertEqual(s.init["commands"][0]["name"], "compact")
        s.handle({"t": "send", "content": "سلام"})
        self.assertTrue(s.busy)
        self.assertTrue(wait_for(lambda: "perm-1" in s.pending))
        self.assertEqual(s.session_id, "11111111-2222-3333-4444-555555555555")
        self.assertEqual(s.spec["resume"], s.session_id)
        self.assertTrue(changes)                                 # tab list remembers the session id
        self.assertNotIn("stream_event", self.types(s))          # live-only events are not kept
        s.handle({"t": "perm", "id": "perm-1", "allow": True, "always": True})
        self.assertTrue(wait_for(lambda: "result" in self.types(s)))
        self.assertFalse(s.busy)
        text = next(e for e in s.events if e.get("type") == "assistant")["message"]["content"][0]["text"]
        ans = json.loads(text.split("answer:", 1)[1])
        self.assertEqual(ans["behavior"], "allow")
        self.assertEqual(ans["updatedInput"], {"command": "ls"})
        self.assertEqual(ans["updatedPermissions"], [{"type": "addRules"}])
        self.assertIn("_perm_done", self.types(s))
        user = next(e for e in s.events if e.get("type") == "user")
        self.assertEqual(user["message"]["content"], [{"type": "text", "text": "سلام"}])

    def test_deny_mode_model_interrupt(self):
        s, argvs, _ = self.make()
        s.handle({"t": "send", "content": [{"type": "text", "text": "x"}]})
        self.assertTrue(wait_for(lambda: "perm-1" in s.pending))
        s.handle({"t": "perm", "id": "perm-1", "allow": False, "message": "نه"})
        self.assertTrue(wait_for(lambda: "result" in self.types(s)))
        text = next(e for e in s.events if e.get("type") == "assistant")["message"]["content"][0]["text"]
        self.assertEqual(json.loads(text.split("answer:", 1)[1]), {"behavior": "deny", "message": "نه"})
        s.handle({"t": "mode", "mode": "plan"})
        s.handle({"t": "mode", "mode": "bypassPermissions"})     # not offered → ignored
        s.handle({"t": "model", "model": "opus"})
        s.handle({"t": "interrupt"})
        self.assertTrue(wait_for(lambda: self.types(s).count("seen_control") == 3))
        seen = [e["subtype"] for e in s.events if e.get("type") == "seen_control"]
        self.assertEqual(seen, ["set_permission_mode", "set_model", "interrupt"])
        self.assertEqual(s.mode, "plan")

    def test_question_answers_and_plan_mode(self):
        s, _, _ = self.make()
        s.handle({"t": "send", "content": "x"})
        self.assertTrue(wait_for(lambda: "perm-1" in s.pending))
        s.handle({"t": "perm", "id": "perm-1", "allow": True,
                  "input": {"answers": {"کدام؟": "اولی, دومی"}},
                  "permissions": [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}]})
        self.assertTrue(wait_for(lambda: "result" in self.types(s)))
        text = next(e for e in s.events if e.get("type") == "assistant")["message"]["content"][0]["text"]
        ans = json.loads(text.split("answer:", 1)[1])
        # original input kept, answers added (this is how the extension answers AskUserQuestion)
        self.assertEqual(ans["updatedInput"], {"command": "ls", "answers": {"کدام؟": "اولی, دومی"}})
        self.assertEqual(ans["updatedPermissions"][0]["mode"], "acceptEdits")
        self.assertEqual(s.mode, "acceptEdits")
        done = next(e for e in s.events if e.get("type") == "_perm_done")
        self.assertEqual(done["answers"], {"کدام؟": "اولی, دومی"})
        self.assertIn({"type": "_mode", "mode": "acceptEdits"}, list(s.events))

    def test_restart_resumes_and_new_chat_clears(self):
        s, argvs, _ = self.make()
        s.handle({"t": "send", "content": "x"})
        self.assertTrue(wait_for(lambda: "perm-1" in s.pending))
        s.restart()
        self.assertEqual(argvs[-1][2], "11111111-2222-3333-4444-555555555555")
        s.restart(fresh=True)
        self.assertEqual(argvs[-1][2], "")
        self.assertNotIn("resume", s.spec)
        self.assertEqual(len(s.events), 0)

    def test_attach_replays_events(self):
        s, _, _ = self.make()
        self.assertTrue(wait_for(lambda: s.init is not None))
        sent = []
        fake = mock.Mock()
        fake.send_json = sent.append
        s.attach(fake)
        self.assertEqual(sent[0]["t"], "s")
        self.assertEqual(sent[0]["state"]["init"]["commands"][0]["name"], "compact")

    def test_history_that_cannot_be_loaded_is_said_in_the_chat(self):
        built = {"make_argv": lambda *a: ["/bin/sh", "-c", "cat >/dev/null"], "cwd": "/tmp", "title": "t", "resume": "",
                 "history": None, "history_error": "cannot reach the server",
                 "spec": {"kind": "chat", "project": "", "where": "server"}}
        s = sw.ChatSession("h", built)
        self.assertEqual(s.events[0]["type"], "_error")
        self.assertIn("could not be loaded", s.events[0]["text"])

    def test_exit_is_reported(self):
        built = {"make_argv": lambda *a: ["/bin/sh", "-c", "echo boom >&2; exit 3"], "cwd": "/tmp", "title": "t",
                 "resume": "", "spec": {"kind": "chat", "project": "", "where": "server"}}
        s = sw.ChatSession("c2", built)
        s.start()
        self.assertTrue(wait_for(lambda: s.exit_code == 3))
        self.assertIn("_exit", self.types(s))
        self.assertTrue(wait_for(lambda: any(e.get("text") == "boom" for e in s.events)))


class ProjectFiles(unittest.TestCase):
    def test_walk_skips_heavy_folders(self):
        with tempfile.TemporaryDirectory() as d:
            for f in ("src/app/main.ts", "README.md", "node_modules/x/i.js", ".git/HEAD", "docker/data/db/x", "docker/local/air.toml"):
                Path(d, f).parent.mkdir(parents=True, exist_ok=True)
                Path(d, f).write_text("x")
            files = sw.local_files(d)
            self.assertIn("src/app/main.ts", files)
            self.assertIn("docker/local/air.toml", files)
            self.assertFalse(any(f.startswith(("node_modules", ".git", "docker/data")) for f in files))
            withd = sw.with_dirs(files)
            self.assertIn("src/", withd)
            self.assertIn("src/app/", withd)
            self.assertLess(withd.index("src/app/"), withd.index("src/app/main.ts"))

    def test_git_listing_respects_gitignore(self):
        if not shutil.which("git"):
            self.skipTest("git not installed")
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            Path(d, ".gitignore").write_text("secret.txt\n")
            Path(d, "a.py").write_text("x")
            Path(d, "secret.txt").write_text("x")
            files = sw.local_files(d)
            self.assertIn("a.py", files)
            self.assertNotIn("secret.txt", files)


class GitStatusParse(unittest.TestCase):
    def test_branch_and_files(self):
        raw = "## main...origin/main [ahead 2, behind 1]\0 M src/a.go\0?? new file.txt\0R  b2.go\0b.go\0UU c.go\0"
        st = sw.parse_git_status(raw)
        self.assertEqual((st["branch"], st["upstream"], st["ahead"], st["behind"]), ("main", "origin/main", 2, 1))
        self.assertEqual([f["path"] for f in st["files"]], ["src/a.go", "new file.txt", "b2.go", "c.go"])
        self.assertEqual(st["files"][2]["from"], "b.go")
        self.assertTrue(st["files"][1]["untracked"])
        self.assertEqual(st["conflicts"], 1)
        self.assertEqual(sw.parse_git_status("## No commits yet on main\0")["branch"], "main")
        self.assertEqual(sw.parse_git_status("## dev\0")["upstream"], "")

    def test_paths(self):
        self.assertTrue(sw.git_path_ok("a/b.txt"))
        for bad in ("", "/etc/passwd", "../x", "a/../../b", "-rf"):
            self.assertFalse(sw.git_path_ok(bad))


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitFlow(TmpHome):
    def git(self, d, *args):
        return subprocess.run(["git", "-C", d] + list(args), capture_output=True, text=True, check=True).stdout

    def clone(self, remote, name):
        d = os.path.join(self.tmp.name, name)
        subprocess.run(["git", "clone", "-q", remote, d], check=True, capture_output=True)
        self.git(d, "config", "user.email", "t@example.com")
        self.git(d, "config", "user.name", "T")
        return d

    def setUp(self):
        super().setUp()
        self.remote = os.path.join(self.tmp.name, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.remote], check=True)
        self.a = self.clone(self.remote, "a")
        self.git(self.a, "checkout", "-q", "-b", "main")
        Path(self.a, "f.txt").write_text("one\n")
        self.git(self.a, "add", "f.txt")
        self.git(self.a, "commit", "-q", "-m", "first")
        self.app = sw.App(self.store, sw.ServerConf("", "h"))
        self.p = {"name": "a", "source": "manual", "local": self.a, "localExists": True, "remote": ""}

    def test_status_diff_commit_push_pull_conflict(self):
        repo = sw.GitRepo(self.app, self.p)
        st = repo.status()
        self.assertTrue(st["repo"])
        self.assertEqual((st["where"], st["branch"], st["files"]), ("local", "main", []))
        # first push sets the upstream
        repo.push()
        st = repo.status()
        self.assertEqual(st["upstream"], "origin/main")
        # changes: one modified, one new
        Path(self.a, "f.txt").write_text("one\ntwo\n")
        Path(self.a, "سلام.md").write_text("hi\n")
        st = repo.status()
        self.assertEqual(sorted(f["path"] for f in st["files"]), sorted(["f.txt", "سلام.md"]))
        self.assertIn("+two", repo.diff("f.txt", False))
        self.assertIn("+hi", repo.diff("سلام.md", True))
        # commit only one of them
        repo.commit("only f", ["f.txt"])
        st = repo.status()
        self.assertEqual([f["path"] for f in st["files"]], ["سلام.md"])
        self.assertEqual(st["ahead"], 1)
        self.assertIn("only f", st["last"])
        repo.commit("the rest", None)
        self.assertEqual(repo.status()["files"], [])
        repo.push()
        self.assertEqual(repo.status()["ahead"], 0)
        # someone else changes the same line → conflict on pull
        b = self.clone(self.remote, "b")
        Path(b, "f.txt").write_text("one\nTWO from b\n")
        self.git(b, "commit", "-qam", "b change")
        self.git(b, "push", "-q")
        Path(self.a, "f.txt").write_text("one\ntwo from a\n")
        repo.commit("a change", None)
        self.assertEqual(repo.pull(), "conflicts")
        st = repo.status()
        self.assertEqual(st["conflicts"], 1)
        self.assertTrue(st["files"][0]["conflict"])
        with self.assertRaises(sw.ApiError):
            repo.commit("", None)                   # message required
        with self.assertRaises(sw.ApiError):
            repo.diff("../etc/passwd", False)

    def test_repositories_inside_a_project(self):
        proj = os.path.join(self.tmp.name, "proj")
        for sub in ("api", "web", "web/node_modules/dep", "docs"):
            os.makedirs(os.path.join(proj, sub))
        for sub in ("api", "web", "web/node_modules/dep"):
            subprocess.run(["git", "init", "-q", os.path.join(proj, sub)], check=True)
        self.assertEqual(sw.find_local_repos(proj), ["api", "web"])          # node_modules skipped
        p = dict(self.p, local=proj)
        info = sw.git_repos(self.app, p)
        self.assertEqual(info, {"where": "local", "repos": ["api", "web"]})
        Path(proj, "api", "x.go").write_text("package x\n")
        st = sw.GitRepo(self.app, p, "api", "local").status()
        self.assertEqual((st["repo"], st["path"], [f["path"] for f in st["files"]]), (True, "api", ["x.go"]))
        with self.assertRaises(sw.ApiError):
            sw.GitRepo(self.app, p, "../etc")
        self.assertEqual(sw.find_local_repos(self.a), [""])                  # the project itself

    def test_git_summary_for_badges(self):
        repo = sw.GitRepo(self.app, self.p)
        repo.push()
        Path(self.a, "f.txt").write_text("changed\n")
        Path(self.a, "n.txt").write_text("new\n")
        b = self.clone(self.remote, "b2")
        Path(b, "g.txt").write_text("x\n")
        self.git(b, "add", "g.txt")
        self.git(b, "commit", "-qm", "from b")
        self.git(b, "push", "-q")
        g = self.app.git_summary_one(self.p, fetch=True)                 # fetch → knows it is behind
        self.assertEqual((g["changes"], g["ahead"], g["behind"], g["conflicts"], g["repos"]), (2, 0, 1, 0, 1))
        with mock.patch.object(sw, "mutagen_sessions", return_value=[]):
            self.store.add_manual({"name": "a", "local": self.a})
            self.app.invalidate()
            self.assertEqual(self.app.git_summary("a", force=True)["a"]["changes"], 2)

    def test_not_a_repo(self):
        d = os.path.join(self.tmp.name, "plain")
        os.mkdir(d)
        st = sw.GitRepo(self.app, dict(self.p, local=d)).status()
        self.assertFalse(st["repo"])


class ChatHelpers(TmpHome):
    def test_args_and_script(self):
        a = sw.chat_args("auto", "opus", "11111111-2222-3333-4444-555555555555")
        self.assertIn("--permission-prompt-tool=stdio", a)
        self.assertEqual(a[a.index("--permission-mode") + 1], "auto")
        self.assertEqual(a[a.index("--resume") + 1], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(sw.chat_args("bypassPermissions")[-1], "default")
        script = sw.chat_remote_script("/home/esi/my proj", "claude", a)
        self.assertTrue(script.startswith("cd '/home/esi/my proj' || exit 97"))
        self.assertEqual(subprocess.run(["bash", "-n", "-c", script]).returncode, 0)
        self.assertEqual(sw.claude_project_dir("/home/esi/cheshmeh"), "-home-esi-cheshmeh")
        self.assertEqual(sw.claude_project_dir("/home/a.b/x_y"), "-home-a-b-x-y")

    def test_history_is_paged(self):
        home = Path(self.tmp.name) / "home2"
        d = home / ".claude" / "projects" / "-p"
        d.mkdir(parents=True)
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        lines = []
        for i in range(100):
            lines.append({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": f"q{i}"}]}})
            lines.append({"type": "assistant", "message": {"id": f"m{i}", "role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]}})
        (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(l, separators=(",", ":")) for l in lines) + "\n")
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}

        def page(before, limit="40"):
            r = subprocess.run([sys.executable, "-c", sw.REMOTE_HISTORY_PY, "-p", sid, before, limit], capture_output=True, text=True, env=env)
            return json.loads(r.stdout)
        last = page("")
        self.assertEqual((last["total"], last["start"], len(last["entries"])), (200, 160, 40))
        self.assertEqual(last["entries"][0]["message"]["content"][0]["text"], "q80")     # starts at a question
        older = page(str(last["start"]))
        self.assertEqual((older["start"], len(older["entries"])), (120, 40))
        self.assertEqual(page("0")["entries"], [])

    def test_build_chat(self):
        projects = sw.merge_projects(SESSIONS, [], {})
        conf = sw.ServerConf("esi", "1.2.3.4", "225", "")
        b = sw.build_chat({"kind": "chat", "project": "cheshmeh"}, conf, self.store, projects)
        argv = b["make_argv"]("default", "", "")
        self.assertIn("-T", argv)
        self.assertIn("esi@203.0.113.10", argv)
        self.assertIn("cd /home/esi/cheshmeh", argv[-1])
        self.assertEqual(b["title"], "Claude · cheshmeh")
        with self.assertRaises(sw.ApiError):
            sw.build_chat({"project": "cheshmeh", "resume": "../../etc"}, conf, self.store, projects)
        with self.assertRaises(sw.ApiError):
            sw.build_chat({"project": "cheshmeh"}, sw.ServerConf("", "h"), self.store, projects)

    def test_remote_scripts_read_transcripts(self):
        home = Path(self.tmp.name) / "home"
        d = home / ".claude" / "projects" / "-srv-app"
        d.mkdir(parents=True)
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        lines = [
            {"type": "user", "message": {"role": "user", "content": "<command-name>/clear</command-name>"}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "سلام، باگ لاگین را درست کن"},
                                                                    {"type": "image", "source": {"data": "x" * 50}}]}},
            {"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": [
                {"type": "text", "text": "باشه"}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                                    "content": [{"type": "text", "text": "a" * 9000}]}]}},
            {"type": "assistant", "isSidechain": True, "message": {"id": "s", "content": []}},
            {"type": "ai-title", "aiTitle": "رفع باگ لاگین"},
            {"type": "api-request", "x": 1},
        ]
        (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(l, ensure_ascii=False, separators=(",", ":")) for l in lines) + "\n")
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
        r = subprocess.run([sys.executable, "-c", sw.REMOTE_LIST_PY, "-srv-app"], capture_output=True, text=True, env=env)
        lst = json.loads(r.stdout)
        self.assertEqual(lst[0]["id"], sid)
        self.assertEqual(lst[0]["title"], "رفع باگ لاگین")
        self.assertEqual(lst[0]["first"], "سلام، باگ لاگین را درست کن")
        self.assertEqual(lst[0]["prompts"], 1)
        r = subprocess.run([sys.executable, "-c", sw.REMOTE_HISTORY_PY, "-srv-app", sid], capture_output=True, text=True, env=env)
        page = json.loads(r.stdout)
        hist = page["entries"]
        self.assertEqual((page["total"], page["start"]), (4, 0))
        self.assertEqual([e["type"] for e in hist], ["user", "user", "assistant", "user"])
        self.assertEqual(hist[1]["message"]["content"][1], {"type": "text", "text": "[image]"})
        res = hist[3]["message"]["content"][0]
        self.assertEqual(res["type"], "tool_result")
        self.assertLess(len(res["content"]), 8100)


def find_node():
    import glob
    for c in [shutil.which("node")] + sorted(glob.glob(os.path.expanduser("~/.vscode-server/cli/servers/*/server/node"))):
        if c and os.access(c, os.X_OK):
            return c
    return None


class FrontendJs(unittest.TestCase):
    """Runs the browser code (chat.js, app.js) under node with a tiny fake DOM."""

    def test_syntax(self):
        node = find_node()
        if not node:
            self.skipTest("node not found")
        for f in ("app.js", "chat.js"):
            r = subprocess.run([node, "--check", str(ROOT / "gui" / "static" / f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_app_boot_and_layout(self):
        node = find_node()
        if not node:
            self.skipTest("node not found")
        r = subprocess.run([node, str(ROOT / "tests" / "js" / "app_test.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_no_property_clashes_between_app_and_chat_tabs(self):
        # app.js stores layout data on every tab object; it must never overwrite a ChatTab method
        app = (ROOT / "gui" / "static" / "app.js").read_text()
        chat = (ROOT / "gui" / "static" / "chat.js").read_text()
        props = set(re.findall(r"\btab\.([A-Za-z_]\w*)\s*=(?!=)", app))
        methods = set(re.findall(r"\bP\.([A-Za-z_]\w*)\s*=\s*function", chat))
        self.assertIn("group", props)
        self.assertEqual(props & methods, set())

    def test_chat_panel(self):
        node = find_node()
        if not node:
            self.skipTest("node not found")
        r = subprocess.run([node, str(ROOT / "tests" / "js" / "chat_test.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class BashSelfUpdate(unittest.TestCase):
    """The installed copy of the command picks up a newer version from its source folder."""

    def test_update_from_source(self):
        script = (ROOT / "simabworkspace.sh").read_text()
        funcs = []
        for name in ("real_path", "sync_gui_files", "update_from_source"):
            m = re.search(r"^%s\(\) \{.*?^\}" % name, script, re.S | re.M)
            self.assertIsNotNone(m, name)
            funcs.append(m.group(0))
        with tempfile.TemporaryDirectory() as home:
            src = Path(home, "dl", "simabworkspace")
            (src / "gui").mkdir(parents=True)
            (src / "gui" / "server.py").write_text("v2")
            (src / "simabworkspace.sh").write_text("#!/bin/bash\necho v2\n")
            bin_dir = Path(home, ".local", "bin")
            bin_dir.mkdir(parents=True)
            (bin_dir / "simabworkspace").write_text("#!/bin/bash\necho v1\n")
            share = Path(home, ".local", "share", "simabworkspace", "gui")
            share.mkdir(parents=True)
            (share / "server.py").write_text("v1")
            cfg = Path(home, ".config", "simabworkspace")
            cfg.mkdir(parents=True)
            (cfg / "source").write_text(str(src) + "\n")
            prog = "\n".join([
                "APP=simabworkspace", f"CONFIG_DIR={cfg}", f"BIN_DIR={bin_dir}", f"GUI_SHARE={share}",
                "ok() { echo \"OK $*\"; }",
            ] + funcs + ["update_from_source"])
            r = subprocess.run(["bash", "-c", prog, str(bin_dir / "simabworkspace")], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual((share / "server.py").read_text(), "v2")
            self.assertIn("v2", (bin_dir / "simabworkspace").read_text())
            self.assertTrue(os.access(bin_dir / "simabworkspace", os.X_OK))
            # nothing changed → silent
            r = subprocess.run(["bash", "-c", prog, str(bin_dir / "simabworkspace")], capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), "")


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class BashAddLocal(unittest.TestCase):
    """simabworkspace add-local: upload a local project to the server, then sync (ssh/mutagen stubbed)."""

    def run_add(self, home, args, sessions="[]"):
        script = (ROOT / "simabworkspace.sh").read_text()
        funcs = []
        for name in ("add_local_project", "normalize_local", "sanitize_name"):
            m = re.search(r"^%s\(\) \{.*?^\}" % name, script, re.S | re.M)
            self.assertIsNotNone(m, name)
            funcs.append(m.group(0))
        stubs = f"""
IS_WSL=0; SSH_USER=esi; SSH_HOST=h; SSH_PORT=225; DEFAULT_IGNORES=node_modules,dist
REMOTE_HOME={home}/server
title() {{ :; }}; info() {{ echo "INFO $*"; }}; ok() {{ echo "OK $*"; }}; warn() {{ echo "WARN $*"; }}; err() {{ echo "ERR $*"; }}
ssh_ok() {{ return 0; }}; daemon_start() {{ :; }}; git_warning() {{ :; }}
ask() {{ printf '%s' "$2"; }}; confirm() {{ return 1; }}
sessions_json() {{ printf '%s' '{sessions}'; }}
session_names() {{ sessions_json | jq -r '.[].name'; }}
rssh() {{ HOME="$REMOTE_HOME" bash -c "$*"; }}
create_session() {{ echo "CREATE name=$1 local=$2 url=$3 ignores=$4 git=$5"; }}
wait_sync() {{ echo "WAIT $1"; }}
"""
        prog = stubs + "\n".join(funcs) + "\nadd_local_project " + " ".join(shlex.quote(a) for a in args)
        return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, cwd=home)

    def test_upload_creates_remote_folder_and_sync(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "server"))
            proj = os.path.join(home, "work", "My Shop")
            os.makedirs(proj)
            Path(proj, "main.go").write_text("package main")
            r = self.run_add(home, [proj, "--yes"])
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue(os.path.isdir(os.path.join(home, "server", "My Shop")), r.stdout)
            create = next(l for l in r.stdout.splitlines() if l.startswith("CREATE"))
            self.assertIn("name=my-shop", create)
            self.assertIn(f"local={proj}", create)
            self.assertIn(f"url=esi@h:225:{home}/server/My Shop", create)
            self.assertTrue(create.endswith("git=on"))
            self.assertIn("WAIT my-shop", r.stdout)

    def test_refuses_merge_into_full_folder_and_bad_input(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "server", "app"))
            Path(home, "server", "app", "x").write_text("x")
            proj = os.path.join(home, "app")
            os.makedirs(proj)
            Path(proj, "a").write_text("a")
            r = self.run_add(home, [proj, "--yes", "--remote", "~/app"])
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("Not merging automatically", r.stdout)
            self.assertNotIn("CREATE", r.stdout)
            empty = os.path.join(home, "empty")
            os.makedirs(empty)
            self.assertIn("Folder is empty", self.run_add(home, [empty, "--yes"]).stdout)
            r = self.run_add(home, [proj, "--yes", "--remote", "other"], sessions='[{"name":"x","alpha":{"path":"%s"},"beta":{"path":"/r"}}]' % proj)
            self.assertIn("already synced", r.stdout)


class SetupFromWindow(unittest.TestCase):
    """What the terminal wizards did, now done by the window (fake HOME, nothing real touched)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.env = mock.patch.dict(os.environ, {"HOME": self.home, "XDG_CONFIG_HOME": os.path.join(self.home, ".config")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_connection_config_and_ssh_block(self):
        Path(self.home, ".ssh").mkdir()
        Path(self.home, ".ssh", "config").write_text("Host other\n    User x\n")
        sw.write_server_conf(sw.ServerConf("esi", "1.2.3.4", "225", os.path.join(self.home, ".ssh", "work key")))
        c = sw.load_server_conf()
        self.assertEqual((c.user, c.host, c.port, c.key), ("esi", "1.2.3.4", "225", os.path.join(self.home, ".ssh", "work key")))
        cfg = Path(self.home, ".ssh", "config").read_text()
        self.assertIn("Host other", cfg)
        self.assertEqual(cfg.count(sw.RC_MARK_BEGIN), 1)
        self.assertIn("Host 1.2.3.4", cfg)
        # a default key name needs no block; saving again removes it
        sw.write_server_conf(sw.ServerConf("esi", "1.2.3.4", "225", os.path.join(self.home, ".ssh", "id_ed25519")))
        self.assertNotIn(sw.RC_MARK_BEGIN, Path(self.home, ".ssh", "config").read_text())
        # bash can read what Python wrote
        r = subprocess.run(["bash", "-c", f". {self.home}/.config/simabworkspace/config; printf '%s|%s|%s' \"$SSH_USER\" \"$SSH_HOST\" \"$SSH_PORT\""], capture_output=True, text=True)
        self.assertEqual(r.stdout, "esi|1.2.3.4|225")

    def test_autostart_toggle(self):
        Path(self.home, ".bashrc").write_text("export A=1\n")
        self.assertFalse(sw.autostart_enabled())
        sw.set_autostart(True)
        sw.set_autostart(True)
        text = Path(self.home, ".bashrc").read_text()
        self.assertEqual(text.count(sw.RC_MARK_BEGIN), 1)
        self.assertTrue(text.startswith("export A=1"))
        self.assertTrue(sw.autostart_enabled())
        sw.set_autostart(False)
        self.assertEqual(Path(self.home, ".bashrc").read_text().strip(), "export A=1")

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen missing")
    def test_create_key(self):
        k = sw.create_ssh_key()
        self.assertTrue(k.endswith("id_ed25519") and os.path.isfile(k + ".pub"))
        self.assertTrue(sw.create_ssh_key().endswith("id_ed25519_simabworkspace"))
        self.assertEqual(len(sw.list_ssh_keys()), 2)

    def test_install_key_types_the_password_once(self):
        bindir = Path(self.home, "bin")
        bindir.mkdir()
        log = Path(self.home, "got")
        fake = bindir / "ssh-copy-id"
        fake.write_text("#!/bin/bash\nask() { stty -echo; printf \"esi@host's password: \"; read -r pw; stty echo; echo; }\nask\n"
                        f"echo \"$pw\" > {log}\n"
                        "if [ \"$pw\" = 's3cret' ]; then echo 'Number of key(s) added: 1'; exit 0; fi\n"
                        "echo 'Permission denied, please try again.'; ask; exit 1\n")
        fake.chmod(0o755)
        key = Path(self.home, "k")
        key.write_text("x")
        Path(str(key) + ".pub").write_text("ssh-ed25519 AAAA me")
        conf = sw.ServerConf("esi", "host", "225", str(key))
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}:" + os.environ["PATH"]}):
            out = sw.install_ssh_key(conf, "s3cret", timeout=15)
            self.assertIn("added", out)
            self.assertEqual(log.read_text().strip(), "s3cret")
            with self.assertRaises(sw.ApiError):
                sw.install_ssh_key(conf, "wrong", timeout=15)

    def test_add_project_runs_the_script_without_questions(self):
        store = sw.Store(Path(self.home, "gui.json"))
        app = sw.App(store, sw.ServerConf("esi", "h", "225", ""))
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, b"  \xe2\x9c\x93 Project 'shop' added.\n", b"")
        with mock.patch.object(sw.subprocess, "run", side_effect=fake_run), mock.patch.object(sw, "mutagen_sessions", return_value=[]):
            r = app.add_project({"mode": "download", "remote": "/home/esi/shop", "local": "~/w/shop", "git": True})
            self.assertEqual(r["project"], "shop")
            self.assertEqual(seen["argv"][2:], ["add-server", "/home/esi/shop", "--yes", "--no-wait", "--git", "on", "--local", os.path.join(self.home, "w", "shop")])
            proj = Path(self.home, "p")
            proj.mkdir()
            app.add_project({"mode": "upload", "local": str(proj), "name": "pp", "git": False})
            self.assertEqual(seen["argv"][2:], ["add-local", str(proj), "--yes", "--no-wait", "--git", "off", "--name", "pp"])

        def failing(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, b"", b"  \xe2\x9c\x97 This server folder is already synced:\n")
        with mock.patch.object(sw.subprocess, "run", side_effect=failing):
            with self.assertRaises(sw.ApiError) as cm:
                app.add_project({"mode": "download", "remote": "/x"})
            self.assertIn("already synced", cm.exception.message)
        with self.assertRaises(sw.ApiError):
            app.add_project({"mode": "upload", "local": "/nonexistent-xyz"})

    def test_server_folders(self):
        store = sw.Store(Path(self.home, "gui.json"))
        app = sw.App(store, sw.ServerConf("esi", "h", "225", ""))
        out = b"/home/esi/shop\t0\t100\n/home/esi/cheshmeh\t2\t200\n/home/esi/api\t1\t300\n"
        sess = [{"name": "cheshmeh", "status": "watching", "alpha": {"path": "/l"}, "beta": {"host": "h", "path": "/home/esi/cheshmeh"}}]
        with mock.patch.object(sw.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, out, b"")), \
                mock.patch.object(sw, "mutagen_sessions", return_value=sess):
            f = app.server_folders()
        self.assertEqual([(x["name"], x["git"], x["project"]) for x in f],
                         [("shop", "", ""), ("cheshmeh", "repos inside", "cheshmeh"), ("api", "repo", "")])


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class BashMoveAndInstall(unittest.TestCase):
    def funcs(self, *names):
        script = (ROOT / "simabworkspace.sh").read_text()
        out = []
        for name in names:
            m = re.search(r"^%s\(\) \{.*?^\}" % name, script, re.S | re.M)
            self.assertIsNotNone(m, name)
            out.append(m.group(0))
        return "\n".join(out)

    def run_move(self, home, args, old_l, old_r):
        sess = json.dumps([{"name": "shop", "alpha": {"path": old_l}, "beta": {"host": "h", "user": "esi", "port": 225, "path": old_r},
                            "configuration": {"ignores": ["node_modules"]}}])
        prog = f"""
IS_WSL=0; SSH_USER=esi; SSH_HOST=h; SSH_PORT=225; DEFAULT_IGNORES=node_modules
REMOTE_HOME={home}/server
title() {{ :; }}; info() {{ echo "INFO $*"; }}; ok() {{ echo "OK $*"; }}; warn() {{ echo "WARN $*"; }}; err() {{ echo "ERR $*"; }}
ssh_ok() {{ return 0; }}; daemon_running() {{ return 0; }}; daemon_start() {{ :; }}
ask() {{ printf '%s' "$2"; }}; confirm() {{ return 1; }}
sessions_json() {{ printf '%s' '{sess}'; }}
session_git() {{ echo on; }}
rssh() {{ HOME="$REMOTE_HOME" bash -c "$*"; }}
mutagen() {{ echo "MUTAGEN $*"; }}
create_session() {{ echo "CREATE name=$1 local=$2 url=$3 git=$5"; }}
wait_sync() {{ echo "WAIT $*"; return 0; }}
""" + self.funcs("move_project", "normalize_local", "normalize_remote", "sanitize_name") + "\nmove_project " + " ".join(shlex.quote(a) for a in args)
        return subprocess.run(["bash", "-c", prog], capture_output=True, text=True)

    def test_move_both_folders(self):
        with tempfile.TemporaryDirectory() as home:
            old_l, old_r = os.path.join(home, "shop"), os.path.join(home, "server", "shop")
            os.makedirs(old_l); os.makedirs(old_r)
            Path(old_l, "a").write_text("1"); Path(old_r, "a").write_text("1")
            new_l, new_r = os.path.join(home, "work", "shop"), os.path.join(home, "server", "apps", "shop")
            r = self.run_move(home, ["shop", "--local", new_l, "--remote", new_r, "--yes", "--no-wait"], old_l, old_r)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue(os.path.isfile(os.path.join(new_l, "a")) and not os.path.exists(old_l))
            self.assertTrue(os.path.isfile(os.path.join(new_r, "a")) and not os.path.exists(old_r))
            self.assertIn("MUTAGEN sync terminate shop", r.stdout)
            self.assertIn(f"CREATE name=shop local={new_l} url=esi@h:225:{new_r} git=on", r.stdout)
            self.assertNotIn("WAIT shop\n", r.stdout + "\n")          # --no-wait: only the flush wait

    def test_move_refuses_full_target_with_yes(self):
        with tempfile.TemporaryDirectory() as home:
            old_l, old_r = os.path.join(home, "shop"), os.path.join(home, "server", "shop")
            os.makedirs(old_l); os.makedirs(old_r)
            full = os.path.join(home, "full"); os.makedirs(full); Path(full, "x").write_text("x")
            r = self.run_move(home, ["shop", "--local", full, "--yes"], old_l, old_r)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("not empty", r.stdout)
            self.assertNotIn("terminate", r.stdout)
            self.assertTrue(os.path.isdir(old_l))

    def test_window_calls_never_start_the_wizard(self):
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "PATH": "/usr/bin:/bin", "SW_NONINTERACTIVE": "1", "XDG_CONFIG_HOME": os.path.join(home, ".config")}
            r = subprocess.run(["bash", str(ROOT / "simabworkspace.sh"), "add-server", "x"], capture_output=True, text=True,
                               env=env, stdin=subprocess.DEVNULL, timeout=20)
            self.assertEqual(r.returncode, 1)
            self.assertIn("open Settings in the window", r.stdout + r.stderr)
            r = subprocess.run(["bash", str(ROOT / "simabworkspace.sh"), "install", "nonsense"], capture_output=True, text=True,
                               env=env, stdin=subprocess.DEVNULL, timeout=20)
            self.assertEqual(r.returncode, 1)
            self.assertIn("Unknown: nonsense", r.stdout + r.stderr)


class WindowMaintenance(TmpHome):
    def setUp(self):
        super().setUp()
        self.app = sw.App(self.store, sw.ServerConf("esi", "h", "225", ""))

    def test_prereqs_list(self):
        d = self.app.prereqs()
        names = [i["name"] for i in d["items"]]
        self.assertEqual(names[:2], ["mutagen", "jq"])
        self.assertFalse(next(i for i in d["items"] if i["name"] == "jq")["sudo"])
        self.assertTrue(next(i for i in d["items"] if i["name"] == "git")["ok"])

    def test_install_asks_for_password_only_when_needed(self):
        calls = []

        def fake(args, timeout=180, stdin=None, env=None):
            calls.append((args, stdin, env))
            if env and env.get("SW_SUDO"):
                return 0, "ok installed"
            return 1, "sudo: a terminal is required to read the password"
        with mock.patch.object(self.app, "run_script", side_effect=fake):
            with self.assertRaises(sw.ApiError) as cm:
                self.app.install_prereq("git")
            self.assertEqual(cm.exception.status, 428)
            self.app.install_prereq("git", "pw")
        self.assertEqual(calls[-1], (["install", "git"], b"pw\n", {"SW_SUDO": "sudo -S -p ''"}))
        with self.assertRaises(sw.ApiError):
            self.app.install_prereq("rm -rf")

    def test_move_and_uninstall(self):
        sess = [{"name": "shop", "status": "watching", "alpha": {"path": "/l/shop"}, "beta": {"host": "h", "path": "/r/shop"}}]
        seen = []
        with mock.patch.object(sw, "mutagen_sessions", return_value=sess), \
                mock.patch.object(self.app, "run_script", side_effect=lambda a, **k: (seen.append(a), (0, "ok"))[1]):
            self.app.move_project("shop", "~/work/shop", "")
            self.assertEqual(seen[-1], ["move", "shop", "--yes", "--no-wait", "--local", os.path.expanduser("~/work/shop")])
            with self.assertRaises(sw.ApiError):
                self.app.move_project("shop", "", "")
            with self.assertRaises(sw.ApiError):
                self.app.move_project("nope", "/x", "")
            self.app.uninstall()
            self.assertTrue(wait_for(lambda: ["uninstall", "--yes"] in seen, 3))


class SyncDetailsAndConflicts(TmpHome):
    def setUp(self):
        super().setUp()
        self.local = os.path.join(self.tmp.name, "shop")
        os.makedirs(os.path.join(self.local, "src"))
        Path(self.local, "src", "a.go").write_text("local version")
        self.sess = [{"name": "shop", "status": "watching", "successfulCycles": 7,
                      "alpha": {"path": self.local}, "beta": {"host": "h", "user": "esi", "path": "/srv/shop"},
                      "configuration": {"ignores": ["node_modules"], "ignoreVCSMode": "ignore"},
                      "conflicts": [{"root": "src/a.go",
                                     "alphaChanges": [{"path": "src/a.go", "old": {"kind": "file"}, "new": {"kind": "file"}}],
                                     "betaChanges": [{"path": "src/a.go", "old": {"kind": "file"}, "new": None}]}]}]
        self.app = sw.App(self.store, sw.ServerConf("esi", "h", "225", ""))

    def test_details(self):
        with mock.patch.object(sw, "mutagen_sessions", return_value=self.sess):
            d = self.app.sync_details("shop")
        self.assertEqual((d["local"], d["remote"], d["git"], d["cycles"]), (self.local, "/srv/shop", False, 7))
        self.assertEqual(d["conflicts"], [{"path": "src/a.go", "local": [{"path": "src/a.go", "was": "file", "now": "file"}],
                                           "server": [{"path": "src/a.go", "was": "file", "now": "deleted"}]}])

    def test_resolve_keeps_the_chosen_side(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv[-1], kw.get("input")))
            cmd = argv[-1]
            if cmd.startswith("if [ -d"):
                return subprocess.CompletedProcess(argv, 0, b"FILE\n", b"")
            if cmd.startswith("cat "):
                return subprocess.CompletedProcess(argv, 0, b"server version", b"")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        with mock.patch.object(sw, "mutagen_sessions", return_value=self.sess), \
                mock.patch.object(sw.subprocess, "run", side_effect=fake_run), mock.patch.object(sw, "which", return_value=None):
            self.app.resolve_conflict("shop", "src/a.go", "local")
            self.assertEqual(calls[-1], ("mkdir -p \"$(dirname /srv/shop/src/a.go)\" && cat > /srv/shop/src/a.go", b"local version"))
            self.app.resolve_conflict("shop", "src/a.go", "server")
            self.assertEqual(Path(self.local, "src", "a.go").read_text(), "server version")
            for bad in (("../x", "local"), ("src/a.go", "both"), ("other.go", "local")):
                with self.assertRaises(sw.ApiError):
                    self.app.resolve_conflict("shop", *bad)

    def test_linux_shortcut(self):
        with mock.patch.dict(os.environ, {"HOME": self.tmp.name}), mock.patch.object(sw, "is_wsl", return_value=False), \
                mock.patch.object(sw.sys, "platform", "linux"):
            r = self.app.create_shortcut()
            self.assertEqual(r["where"], ["app menu"])
            desk = Path(self.tmp.name, ".local", "share", "applications", "simabworkspace.desktop").read_text()
            self.assertIn("Name=Simab Workspace", desk)
            self.assertRegex(desk, r"Exec=.*simabworkspace(\.sh)? gui")
            self.assertTrue(Path(self.tmp.name, ".local", "share", "icons", "simabworkspace.svg").is_file())


class Release(unittest.TestCase):
    def test_one_file_installer(self):
        with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as home:
            r = subprocess.run(["bash", str(ROOT / "release.sh")], capture_output=True, text=True, env=dict(os.environ, DIST=out), timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            version = re.search(r'^VERSION="(.*)"$', (ROOT / "simabworkspace.sh").read_text(), re.M).group(1)
            listing = subprocess.run(["tar", "-tzf", os.path.join(out, f"simabworkspace-{version}.tar.gz")], capture_output=True, text=True).stdout
            self.assertIn("simabworkspace/gui/server.py", listing)
            self.assertIn("simabworkspace/README.md", listing)
            self.assertNotIn("tests/", listing)
            env = {"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config"), "PATH": "/usr/bin:/bin"}
            r = subprocess.run(["bash", os.path.join(out, "simabworkspace-install.sh"), "--no-open"], capture_output=True, text=True, env=env, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            cmd = os.path.join(home, ".local", "bin", "simabworkspace")
            self.assertEqual(subprocess.run([cmd, "version"], capture_output=True, text=True, env=env).stdout.strip(), f"simabworkspace {version}")
            self.assertTrue(os.path.isfile(os.path.join(home, ".local", "share", "simabworkspace", "gui", "server.py")))
            # the one-line bootstrap (install.sh) with that build
            home2 = os.path.join(home, "second")
            os.makedirs(home2)
            env2 = {"HOME": home2, "PATH": "/usr/bin:/bin", "SIMAB_NO_SETUP": "1",
                    "SIMAB_INSTALLER_URL": "file://" + os.path.join(out, "simabworkspace-install.sh")}
            r = subprocess.run(["bash", str(ROOT / "install.sh")], capture_output=True, text=True, env=env2, timeout=120, stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("simabworkspace gui", r.stdout)
            self.assertTrue(os.path.isfile(os.path.join(home2, ".local", "bin", "simabworkspace")))
            # running the installer again (an update) works too
            r = subprocess.run(["bash", os.path.join(out, "simabworkspace-install.sh"), "--no-open"], capture_output=True, text=True, env=env, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class Updates(TmpHome):
    def test_versions(self):
        self.assertGreater(sw.version_tuple("1.17.10"), sw.version_tuple("1.17.9"))
        self.assertGreater(sw.version_tuple("v2.0.0"), sw.version_tuple("1.99.99"))
        self.assertEqual(sw.version_tuple("x"), (0,))

    def test_check_and_install(self):
        app = sw.App(self.store, sw.ServerConf("", "h"))
        newer = ".".join(str(x + (1 if i == 0 else 0)) for i, x in enumerate(sw.version_tuple(sw.VERSION)))
        fake_installer = Path(self.tmp.name, "inst.sh")
        fake_installer.write_text("#!/bin/bash\necho \"installed $1\" > " + str(Path(self.tmp.name, "ran")) + "\n")
        rel = {"version": newer, "notes": "## Changes\n- x", "url": "https://example/r", "installer": "file://" + str(fake_installer)}
        with mock.patch.object(sw, "fetch_latest_release", return_value=rel):
            u = app.check_update(force=True)
            self.assertTrue(u["available"])
            self.assertEqual((u["current"], u["latest"]), (sw.VERSION, newer))
            started = []
            real_popen = subprocess.Popen

            def popen(argv, *a, **kw):
                if "serve" in argv:                     # the restart of the new version: record it only
                    started.append((argv, kw["env"]))
                    return mock.Mock()
                return real_popen(argv, *a, **kw)        # subprocess.run (the installer) runs for real
            with mock.patch.object(sw.subprocess, "Popen", side_effect=popen), \
                    mock.patch.object(sw, "shutdown_app"):
                r = app.install_update()
            self.assertEqual(r["version"], newer)
            self.assertEqual(Path(self.tmp.name, "ran").read_text().strip(), "installed --no-open")
            argv, env = started[0]
            self.assertIn("serve", argv)
            self.assertEqual((env["SIMAB_TOKEN"], env["SIMAB_RESTART"]), (app.token, "1"))
        with mock.patch.object(sw, "fetch_latest_release", return_value=dict(rel, version=sw.VERSION)):
            self.assertFalse(app.check_update(force=True)["available"])
            with self.assertRaises(sw.ApiError):
                app.install_update()
        with mock.patch.object(sw, "fetch_latest_release", side_effect=OSError("offline")):
            u = app.check_update(force=True)
            self.assertFalse(u["available"])
            self.assertIn("offline", u["error"])

    def test_restart_keeps_address_and_key(self):
        with mock.patch.dict(os.environ, {"SIMAB_TOKEN": "k" * 32}):
            self.assertEqual(sw.App(self.store, sw.ServerConf("", "h")).token, "k" * 32)
        self.assertNotEqual(sw.App(self.store, sw.ServerConf("", "h")).token, "k" * 32)
        busy = socket.socket()
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        threading.Timer(1.0, busy.close).start()                 # the old copy goes away a moment later
        app = sw.App(self.store, sw.ServerConf("", "h"))
        httpd = sw.make_server(app, port, wait=8)
        self.assertEqual(app.port, port)
        httpd.server_close()


class BashScript(unittest.TestCase):
    def test_syntax_and_gui_command(self):
        import subprocess
        script = ROOT / "simabworkspace.sh"
        self.assertEqual(subprocess.run(["bash", "-n", str(script)]).returncode, 0)
        text = script.read_text()
        self.assertIn("gui)", text)
        self.assertIn('VERSION="1.18.0"', text)


if __name__ == "__main__":
    unittest.main()
