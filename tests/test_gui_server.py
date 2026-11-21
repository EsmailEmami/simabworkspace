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

    def test_starred_setting(self):
        self.assertEqual(self.store.settings["starred"], [])
        s = self.store.update_settings({"starred": [" menu:Git… ", "cmd:shop: files", "menu:Git…", "", "x" * 300]})
        self.assertEqual(s["starred"], ["menu:Git…", "cmd:shop: files", "x" * 200])
        for bad in ("menu:Git…", [1], {"a": 1}):
            with self.assertRaises(sw.ApiError):
                self.store.update_settings({"starred": bad})
        self.assertEqual(sw.Store(self.store.path).settings["starred"][:2], ["menu:Git…", "cmd:shop: files"])
        self.assertEqual(sw.DEFAULT_SETTINGS["starred"], [])                      # the default is never changed in place

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

    def test_project_starter_message(self):
        self.assertEqual(self.store.set_starter("p", "  Read memory first.\nپاسخ فارسی  "), "Read memory first.\nپاسخ فارسی")
        self.assertEqual(sw.Store(self.store.path).data["projects"]["p"]["starter"], "Read memory first.\nپاسخ فارسی")
        self.assertEqual(self.store.set_starter("p", None), "")
        with self.assertRaises(sw.ApiError):
            self.store.set_starter("p", "x" * 20001)
        with self.assertRaises(sw.ApiError):
            self.store.set_starter("p", 5)

    def test_project_title(self):
        # the user can rename a project in the list; its real name (sync session, tabs) stays
        self.assertEqual(self.store.set_title("p", "  My   shop فروشگاه "), "My shop فروشگاه")
        ps = sw.merge_projects([], [{"name": "p", "local": "/tmp", "remote": ""}, {"name": "a", "local": "/tmp", "remote": ""}], self.store.data["projects"])
        self.assertEqual([(x["name"], x["title"]) for x in ps], [("a", ""), ("p", "My shop فروشگاه")])
        self.assertEqual(self.store.set_title("p", "p"), "")
        self.assertEqual(self.store.set_title("p", None), "")
        with self.assertRaises(sw.ApiError):
            self.store.set_title("p", "x" * 81)
        self.store.set_title("p", "Shop")
        self.store.add_manual({"name": "p", "local": "/tmp", "remote": ""})
        self.store.remove_manual("p")
        self.assertNotIn("title", self.store.data["projects"]["p"])

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

    def test_delete_rows_conversation_and_clear(self):
        a = sw.Activity(Path(self.tmp.name) / "activity.jsonl")
        a.add("prompt", "cheshmeh", "one", session="s1")
        a.add("prompt", "cheshmeh", "two", session="s1")
        a.add("run", "shop", "web")
        a.add("git-commit", "cheshmeh", "fix")
        a.add("run", "", "home")
        self.assertEqual(a.delete(session="s1"), 2)                       # a whole conversation
        run = next(i for i in a.list() if i["text"] == "web")
        self.assertEqual(a.delete(ids=[run["id"], "x"]), 1)                # one row; junk ids ignored
        self.assertEqual(a.delete(clear=True, project="cheshmeh"), 1)      # clear one project
        self.assertEqual([i["text"] for i in a.list()], ["home"])
        a.delete(clear=True)
        self.assertEqual(a.list(), [])

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
        self.assertEqual(pr, {"files": 30, "totalFiles": 120, "bytes": 0, "totalBytes": 0, "eta": None, "percent": 25},
                         "old Mutagen: by files only, the sizes are of the file being copied now")
        # Mutagen 0.18: the whole upload = what this machine has minus what the server has
        up = {"name": "up2", "status": "staging-beta", "alpha": {"path": "/tmp", "totalFileSize": 1_000_000_000},
              "beta": {"host": "h", "path": "/r", "totalFileSize": 200_000_000,
                       "stagingProgress": {"path": "big.iso", "receivedFiles": 7, "expectedFiles": 15269,
                                           "receivedSize": 100_000_000, "expectedSize": 400_000_000, "totalReceivedSize": 200_000_000}}}
        pr = sw.sync_progress(up, now=1000)
        self.assertEqual((pr["bytes"], pr["totalBytes"], pr["percent"], pr["eta"]), (200_000_000, 800_000_000, 25, None))
        up["beta"]["stagingProgress"]["totalReceivedSize"] = 260_000_000
        pr = sw.sync_progress(up, now=1060 - 30)     # 60 MB in 30 s = 2 MB/s; 540 MB left
        self.assertEqual(pr["eta"], 270)
        # the total is never less than what is already known
        up["beta"]["totalFileSize"] = 990_000_000
        self.assertEqual(sw.sync_progress(up, now=1031)["totalBytes"], 260_000_000 + 300_000_000)
        # download: the server sends, this machine receives
        down = {"name": "d", "alpha": {"path": "/l", "totalFileSize": 0,
                                       "stagingProgress": {"receivedFiles": 1, "expectedFiles": 2, "totalReceivedSize": 10}},
                "beta": {"path": "/r", "totalFileSize": 40}}
        self.assertEqual(sw.sync_progress(down, now=5)["percent"], 25)
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

    def test_read_from_the_server_folder(self):
        # the web version (and a project with no local copy) reads the server folder: the same
        # script, run with python3 on the server; every suggestion then runs on the server
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
            def fake_remote(conf, code, args, **kw):
                r = subprocess.run([sys.executable, "-c", code] + args, capture_output=True, text=True)
                return json.loads(r.stdout)
            app = mock.Mock(web=True, conf=mock.Mock(configured=True), store=mock.Mock(settings={}))
            with mock.patch.object(sw, "remote_python", side_effect=fake_remote):
                out = sw.project_suggestions(app, {"name": "p", "remote": d, "local": d, "localExists": True})
            self.assertEqual([(s["cmd"], s["where"]) for s in out], [("npm run dev", "server")])
            app.web = False
            self.assertEqual(sw.project_suggestions(app, {"name": "p", "remote": d, "local": d, "localExists": True})[0]["where"], "local")
            self.assertEqual(sw.project_suggestions(app, None), [])
        self.assertNotIn(("GET", "suggest"), sw.WEB_BLOCKED)


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


class SshControlPath(unittest.TestCase):
    """macOS: ssh refused ControlPath when the home folder has a space in it."""

    def opts(self, argv):
        return [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]

    def test_normal_home_uses_dot_ssh(self):
        with mock.patch.dict(os.environ, {"HOME": "/Users/abdollah"}):
            argv = sw.ssh_base(sw.ServerConf("u", "h", "22", ""))
        self.assertIn("ControlPath=/Users/abdollah/.ssh/sw-%C", self.opts(argv))

    def test_home_with_a_space_or_too_long(self):
        for home in ("/Users/Abdollah M", "/Users/" + "x" * 60):
            with mock.patch.dict(os.environ, {"HOME": home}):
                argv = sw.ssh_base(sw.ServerConf("u", "h", "22", ""))
            cp = [o for o in self.opts(argv) if o.startswith("ControlPath=")]
            self.assertEqual(len(cp), 1, home)
            self.assertNotRegex(cp[0], r"\s")
            self.assertEqual(cp[0], f"ControlPath=/tmp/sw-{os.getuid()}/sw-%C")
            self.assertLessEqual(len(cp[0]) - len("ControlPath=") + 17 + 40 - 2, 104)
            st = os.stat(f"/tmp/sw-{os.getuid()}")
            self.assertEqual(oct(st.st_mode & 0o777), "0o700")

    def test_background_ssh_skips_forwardings_from_ssh_config(self):
        # "Warning: remote port forwarding failed for listen port 47123" in the chat details
        conf = sw.ServerConf("u", "h", "22", "")
        self.assertIn("ClearAllForwardings=yes", self.opts(sw.ssh_base(conf)))
        self.assertNotIn("ClearAllForwardings=yes", self.opts(sw.ssh_base(conf, forwards=True)))
        t = sw.tunnel_argv(conf, {"localPort": 15432, "remoteHost": "127.0.0.1", "remotePort": 5432}) \
            if hasattr(sw, "tunnel_argv") else None
        if t:
            self.assertNotIn("ClearAllForwardings=yes", self.opts(t))

    def test_no_shared_connection_when_tmp_folder_is_not_ours(self):
        with mock.patch.dict(os.environ, {"HOME": "/Users/A B"}), \
                mock.patch.object(sw.os, "lstat", return_value=os.stat_result((0o40700, 0, 0, 0, os.getuid() + 1, 0, 0, 0, 0, 0))):
            argv = sw.ssh_base(sw.ServerConf("u", "h", "22", ""))
        self.assertFalse(any(o.startswith(("ControlPath", "ControlMaster")) for o in self.opts(argv)))
        self.assertIn("ServerAliveInterval=15", self.opts(argv))


REAL_RUN = subprocess.run


class ZshOnTheServer(TmpHome):
    """A server whose login shell is zsh: "no matches found: …/*/.git" when listing folders."""

    def setUp(self):
        super().setUp()
        self.zsh = shutil.which("zsh")
        if not self.zsh:
            self.skipTest("zsh not installed")
        self.home = Path(self.tmp.name) / "home"
        (self.home / "empty-project").mkdir(parents=True)            # no sub folders: */.git matches nothing
        (self.home / "repo" / ".git").mkdir(parents=True)
        (self.home / "mono" / "api" / ".git").mkdir(parents=True)
        self.app = sw.App(self.store, sw.ServerConf("esi", "h", "22", ""))

    def run_with_zsh(self, argv, **kw):
        """Instead of ssh: the command line ssh would send, run by zsh (like sshd does)."""
        kw.pop("stdin", None)
        kw.pop("capture_output", None)
        for k in ("stdout", "stderr"):
            kw.pop(k, None)
        env = dict(os.environ, HOME=str(self.home))
        return REAL_RUN([self.zsh, "-c", argv[-1]], capture_output=True, env=env, timeout=30,
                              cwd=kw.get("cwd"), input=kw.get("input"))

    def chat_run(self, home_setup):
        home = Path(self.tmp.name) / "h"
        (home / "proj").mkdir(parents=True, exist_ok=True)
        (home / ".zshrc").write_text('export PATH="$HOME/.local/bin:$PATH"\n')     # not read by zsh -lc
        home_setup(home)
        script = sw.chat_remote_script("~/proj", "claude", ["--output-format", "stream-json"])
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": self.zsh}
        return REAL_RUN([self.zsh, "-c", script], capture_output=True, text=True, env=env, timeout=20)

    def test_chat_finds_claude_in_local_bin_with_zsh(self):
        # the user's bug: "Claude stopped (exit 127)" — zsh -lc does not read ~/.zshrc
        def setup(home):
            b = home / ".local" / "bin"
            b.mkdir(parents=True)
            (b / "claude").write_text('#!/bin/sh\necho "claude in $(pwd) $*"\n')
            (b / "claude").chmod(0o755)
        r = self.chat_run(setup)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.strip().endswith("/h/proj --output-format stream-json"), r.stdout)
        self.assertNotIn("no matches found", r.stderr)

    def test_chat_says_clearly_when_claude_is_missing(self):
        r = self.chat_run(lambda home: None)
        self.assertEqual(r.returncode, 127)
        self.assertIn("claude was not found on the server", r.stderr)
        self.assertNotIn("no matches found", r.stderr)

    def test_claude_terminal_inner_works_in_zsh(self):
        home = Path(self.tmp.name) / "h2"
        nb = home / ".vscode-server/extensions/anthropic.claude-code-2.1.9-linux-x64/resources/native-binary"
        nb.mkdir(parents=True)
        (nb / "claude").write_text('#!/bin/sh\necho "vscode claude $*"\n')
        (nb / "claude").chmod(0o755)
        r = REAL_RUN([self.zsh, "-c", sw.claude_inner("claude", "continue")], capture_output=True, text=True,
                     env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, timeout=20)
        self.assertEqual(r.stdout.strip(), "vscode claude --continue", r.stderr)

    def test_server_folders_and_repos_and_files_with_zsh(self):
        with mock.patch.object(sw.subprocess, "run", side_effect=self.run_with_zsh), \
                mock.patch.object(sw, "mutagen_sessions", return_value=[]):
            folders = {f["name"]: f["git"] for f in self.app.server_folders()}
            self.assertEqual(folders, {"empty-project": "", "repo": "repo", "mono": "repos inside"})
            p = {"name": "mono", "remote": str(self.home / "mono"), "local": "", "localExists": False, "endpoint": {}}
            self.assertEqual(sw.git_repos(self.app, p), {"where": "server", "repos": []}, "an empty .git is no repository")
            mono = self.home / "mono"
            REAL_RUN(["git", "init", "-q", str(mono / "api")], check=True)
            (mono / "broken" / ".git").mkdir(parents=True)
            self.assertEqual(sw.git_repos(self.app, p), {"where": "server", "repos": ["api"]})
            # the user's case: a stray `git init` in the project folder, over the real repositories
            REAL_RUN(["git", "init", "-q", str(mono)], check=True)
            self.assertEqual(sw.git_repos(self.app, p), {"where": "server", "repos": ["api"]})
            g = ["git", "-C", str(mono), "-c", "user.name=t", "-c", "user.email=t@t"]
            REAL_RUN(g + ["commit", "-q", "--allow-empty", "-m", "x"], check=True)
            self.assertEqual(sw.git_repos(self.app, p), {"where": "server", "repos": ["", "api"]}, "a real one is kept")

    def test_posix_sh_keeps_the_command_intact(self):
        cmd = 'x="a b"; for f in "$HOME"/nothing-*/; do echo "$f"; done; printf "%s|" "$x" \'q\''
        r = subprocess.run([self.zsh, "-c", sw.posix_sh(cmd)], capture_output=True, text=True,
                           env=dict(os.environ, HOME=str(self.home)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.endswith("a b|q|"), r.stdout)


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

    def test_browser_errors_go_to_the_log(self):
        lines = []
        with mock.patch.object(sw, "log_line", side_effect=lines.append):
            st, d = self.req("POST", "/api/client-error", {"message": "x is undefined\nsecond line", "where": "chat.js:10:5",
                                                          "stack": "at f (chat.js:10)", "ua": "Android tablet", "size": "800x1280"})
            self.assertEqual((st, d), (200, {"ok": True}))
            self.req("POST", "/api/client-error", {"message": ["not", "a string"]})
            for _ in range(40):
                self.req("POST", "/api/client-error", {"message": "loop"})
        self.assertIn("CLIENT ERROR [app] x is undefined second line @ chat.js:10:5 | 800x1280 | Android tablet", lines[0])
        self.assertIn("at f (chat.js:10)", lines[0])
        self.assertEqual(len(lines), 30, "at most 30 a minute")

    def test_files_tab_lives_among_the_tabs(self):
        root = os.path.join(self.tmp.name, "loc")
        os.makedirs(os.path.join(root, "a"))
        Path(root, "a", "b.txt").write_text("hello\n")
        st, d = self.req("POST", "/api/manual", {"name": "loc", "local": root})
        self.assertEqual(st, 200, d)
        st, tab = self.req("POST", "/api/sessions", {"kind": "files", "project": "loc"})
        self.assertEqual(st, 200, tab)
        self.assertEqual((tab["spec"]["kind"], tab["title"], tab["alive"]), ("files", "Files · loc", True))
        st, s = self.req("GET", "/api/state")
        self.assertIn(tab["id"], [x["id"] for x in s["sessions"]])
        self.assertIn("files", [x["kind"] for x in self.store.data["lastTabs"]])
        st, d = self.req("GET", "/api/fs/loc/list?path=a")
        self.assertEqual((st, [e["name"] for e in d["entries"]]), (200, ["b.txt"]))
        st, d = self.req("GET", "/api/fs/loc/read?path=a/b.txt")
        self.assertEqual(d["text"], "hello\n")
        self.assertEqual(self.req("GET", "/api/fs/loc/read?path=../x")[0], 400)
        self.assertEqual(self.req("GET", "/api/fs/nope/list")[0], 404)
        st, d = self.req("POST", f"/api/sessions/{tab['id']}/rename", {"title": "Tree"})
        self.assertEqual(d["title"], "Tree")
        self.assertEqual(self.req("DELETE", f"/api/sessions/{tab['id']}")[0], 200)
        self.assertEqual(self.req("POST", "/api/sessions", {"kind": "files", "project": "nope"})[0], 404)

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
def control(m):
    r = m["request"]
    if r["subtype"] == "initialize":
        out({"type": "control_response", "response": {"subtype": "success", "request_id": m["request_id"],
             "response": {"commands": [{"name": "compact", "description": "x"}]}}})
    elif r["subtype"] == "generate_session_title":
        out({"type": "seen_title", "args": r})
        t = "" if r["description"] == "no title" else "Title: " + r["description"][:20]
        out({"type": "control_response", "response": {"subtype": "success", "request_id": m["request_id"],
             "response": {"title": t or None}}})
    elif r["subtype"] == "rename_session":
        out({"type": "seen_control", "subtype": r["subtype"], "args": r})
        out({"type": "system", "subtype": "session_title_changed", "title": r["title"], "session_id": SID})
        out({"type": "control_response", "response": {"subtype": "success", "request_id": m["request_id"]}})
    else:
        return False
    return True
for line in sys.stdin:
    m = json.loads(line)
    if m.get("type") == "control_request":
        r = m["request"]
        if control(m):
            pass
        else:
            out({"type": "seen_control", "subtype": r["subtype"], "args": r})
    elif m.get("type") == "user":
        out({"type": "system", "subtype": "init", "session_id": SID, "permissionMode": "default"})
        out({"type": "user", "message": m["message"], "session_id": SID})
        out({"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1"}}, "session_id": SID})
        out({"type": "control_request", "request_id": "perm-1", "request": {"subtype": "can_use_tool",
             "tool_name": "Bash", "input": {"command": "ls"}, "permission_suggestions": [{"type": "addRules"}]}})
        while True:
            ans = json.loads(sys.stdin.readline())
            if not (ans.get("type") == "control_request" and control(ans)):
                break
        out({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "answer:" + json.dumps(ans["response"]["response"])}]}, "session_id": SID})
        out({"type": "result", "subtype": "success", "session_id": SID, "total_cost_usd": 0.01})
'''


class ChatProtocol(unittest.TestCase):
    def make(self, resume=""):
        argvs = []

        def make_argv(mode, model, resume_, effort=""):
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
        s.handle({"t": "effort", "effort": "high"})
        s.handle({"t": "effort", "effort": "ultra"})               # unknown → ignored
        s.handle({"t": "interrupt"})
        self.assertTrue(wait_for(lambda: self.types(s).count("seen_control") == 4))
        seen = [e["subtype"] for e in s.events if e.get("type") == "seen_control"]
        self.assertEqual(seen, ["set_permission_mode", "set_model", "apply_flag_settings", "interrupt"])
        self.assertEqual((s.mode, s.effort), ("plan", "high"))
        self.assertEqual(s.state()["effort"], "high")

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

    # Claude names the conversation, like in the VS Code extension -------------------------
    def answer_perm(self, s):
        self.assertTrue(wait_for(lambda: "perm-1" in s.pending))
        s.handle({"t": "perm", "id": "perm-1", "allow": True})
        self.assertTrue(wait_for(lambda: not s.busy))

    def test_claude_names_a_new_conversation(self):
        s, _, changes = self.make()
        sent = []
        fake = mock.Mock()
        fake.send_json = sent.append
        s.attach(fake)
        s.handle({"t": "send", "content": "fix the login page please"})
        self.answer_perm(s)
        self.assertTrue(wait_for(lambda: s.title == "Title: fix the login page p"))
        req = next(e for e in s.events if e.get("type") == "seen_title")["args"]
        self.assertEqual(req, {"subtype": "generate_session_title", "description": "fix the login page please", "persist": True})
        self.assertEqual(s.spec["named"], "ai")
        self.assertIn({"t": "title", "title": "Title: fix the login page p"}, sent)
        self.assertIs(changes[-1], s)                          # the tab list keeps the new name
        self.assertEqual(s.info()["title"], "Title: fix the login page p")
        s.handle({"t": "send", "content": "and the signup page"})   # named: not asked again
        self.answer_perm(s)
        self.assertEqual(self.types(s).count("seen_title"), 1)

    def test_no_title_is_asked_again_three_times_at_most(self):
        s, _, _ = self.make()
        for _ in range(4):
            s.handle({"t": "send", "content": "no title"})
            self.answer_perm(s)
        self.assertTrue(wait_for(lambda: self.types(s).count("seen_title") == 3))
        self.assertEqual(s.title, "Claude · p")
        self.assertNotIn("named", s.spec)
        self.assertNotIn("_error", self.types(s))

    def test_slash_commands_do_not_name_it(self):
        s, _, _ = self.make()
        s.handle({"t": "send", "content": "/compact"})
        self.answer_perm(s)
        self.assertNotIn("seen_title", self.types(s))

    def test_rename_tab_renames_the_conversation_in_claude(self):
        s, _, _ = self.make()
        s.handle({"t": "send", "content": "hello"})
        self.answer_perm(s)
        self.assertTrue(wait_for(lambda: s.spec.get("named") == "ai"))
        s.rename("My own name")
        self.assertTrue(wait_for(lambda: "seen_control" in self.types(s)))
        req = next(e for e in s.events if e.get("type") == "seen_control")["args"]
        self.assertEqual(req, {"subtype": "rename_session", "title": "My own name", "source": "host",
                               "session_id": "11111111-2222-3333-4444-555555555555"})
        self.assertEqual((s.title, s.spec["named"]), ("My own name", "custom"))
        self.assertNotIn("session_title_changed", [e.get("subtype") for e in s.events])   # handled, not shown

    def test_renamed_before_claude_runs_is_told_later(self):
        built = {"make_argv": lambda *a: [sys.executable, "-c", FAKE_CLAUDE], "cwd": "/tmp", "title": "t",
                 "resume": "11111111-2222-3333-4444-555555555555", "spec": {"kind": "chat", "project": "", "where": "server"}}
        s = sw.ChatSession("r", built)
        s.rename("Later")
        self.assertEqual(s.rename_later, "Later")
        s.start()
        self.addCleanup(s.kill, True)
        self.assertTrue(wait_for(lambda: "seen_control" in self.types(s)))
        self.assertEqual(s.rename_later, "")

    def test_session_title_changed_from_elsewhere(self):
        s, _, _ = self.make()
        s._handle_event({"type": "system", "subtype": "session_title_changed", "title": "  From\n /rename  "})
        self.assertEqual((s.title, s.spec["named"]), ("From /rename", "custom"))

    def test_resumed_conversation_shows_its_title(self):
        h = {"total": 0, "start": 0, "entries": [], "title": "Claude's name", "custom": False, "first": "x"}
        built = {"make_argv": lambda *a: ["/bin/true"], "cwd": "/tmp", "title": "Claude · first words", "resume": "",
                 "history": h, "spec": {"kind": "chat", "project": "", "where": "server"}}
        s = sw.ChatSession("h1", built)
        self.assertEqual((s.title, s.spec["named"]), ("Claude's name", "ai"))
        built["history"] = dict(h, custom=True)
        self.assertEqual(sw.ChatSession("h2", built).spec["named"], "custom")

    def test_old_untitled_conversation_is_named_after_its_first_message(self):
        built = {"make_argv": lambda *a: [sys.executable, "-c", FAKE_CLAUDE], "cwd": "/tmp", "title": "t", "resume": "",
                 "history": {"total": 1, "start": 0, "entries": [], "title": "", "first": "the first question"},
                 "spec": {"kind": "chat", "project": "", "where": "server"}}
        s = sw.ChatSession("o", built)
        s.start()
        self.addCleanup(s.kill, True)
        s.handle({"t": "send", "content": "go on"})
        self.answer_perm(s)
        self.assertTrue(wait_for(lambda: s.title == "Title: the first question"))

    def test_new_chat_forgets_the_name(self):
        s, _, _ = self.make()
        s.handle({"t": "send", "content": "hello"})
        self.answer_perm(s)
        self.assertTrue(wait_for(lambda: s.spec.get("named") == "ai"))
        s.restart(fresh=True)
        self.assertEqual(s.title, "Claude · p")
        self.assertNotIn("named", s.spec)


class TranscriptTitles(unittest.TestCase):
    """REMOTE_LIST_PY / REMOTE_HISTORY_PY read the names Claude writes into its transcripts."""
    def run_py(self, code, *args):
        home = self.home
        out = subprocess.run([sys.executable, "-c", code, *args], env=dict(os.environ, HOME=home),
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        d = os.path.join(self.home, ".claude", "projects", "-p")
        os.makedirs(d)
        lines = [
            {"type": "user", "message": {"role": "user", "content": "first question"}},
            {"type": "ai-title", "aiTitle": "AI name", "sessionId": "x"},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}},
        ]
        self.f = os.path.join(d, "11111111-2222-3333-4444-555555555555.jsonl")
        with open(self.f, "w") as fh:
            fh.write("".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines))

    def test_ai_title_then_custom_title_wins(self):
        conv = "11111111-2222-3333-4444-555555555555"
        h = self.run_py(sw.REMOTE_HISTORY_PY, "-p", conv)
        self.assertEqual((h["title"], h["custom"], h["first"], h["total"]), ("AI name", False, "first question", 2))
        self.assertEqual(self.run_py(sw.REMOTE_LIST_PY, "-p")[0]["title"], "AI name")
        with open(self.f, "a") as fh:
            fh.write(json.dumps({"type": "custom-title", "customTitle": "Mine", "sessionId": "x"}, separators=(",", ":")) + "\n")
            fh.write(json.dumps({"type": "ai-title", "aiTitle": "AI again", "sessionId": "x"}, separators=(",", ":")) + "\n")
        h = self.run_py(sw.REMOTE_HISTORY_PY, "-p", conv)
        self.assertEqual((h["title"], h["custom"]), ("Mine", True))
        self.assertEqual(self.run_py(sw.REMOTE_LIST_PY, "-p")[0]["title"], "Mine")


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

    def test_several_repositories_in_one_project_each_with_its_gitignore(self):
        # the project folder is not a repository itself, but holds some: their .gitignore counts
        if not shutil.which("git"):
            self.skipTest("git not installed")
        with tempfile.TemporaryDirectory() as d:
            for repo in ("api", "web"):
                subprocess.run(["git", "init", "-q", str(Path(d, repo))], check=True)
            Path(d, "api", ".gitignore").write_text(".env\nbin/\n")
            Path(d, "web", ".gitignore").write_text("*.log\n")
            for f in ("api/main.go", "api/.env", "api/bin/server", "web/app.ts", "web/debug.log", "notes.md", "node_modules/x.js"):
                Path(d, f).parent.mkdir(parents=True, exist_ok=True)
                Path(d, f).write_text("x")
            files = sorted(sw.local_files(d))
            self.assertEqual(files, ["api/.gitignore", "api/main.go", "notes.md", "web/.gitignore", "web/app.ts"])
            # the same on the server (the script run with python3 -c)
            r = subprocess.run([sys.executable, "-c", sw.FS_PY, "all", d], capture_output=True, text=True)
            self.assertEqual(sorted(json.loads(r.stdout)["files"]), files)


class ProjectFilesNestedRepos(unittest.TestCase):
    def test_a_repository_holding_repositories_lists_their_files_once(self):
        # the user's project: the folder is a repository AND holds repositories (one tracked as a
        # link, one new) — @ found only the first level, and the new one showed twice
        if not shutil.which("git"):
            self.skipTest("git not installed")
        g = lambda *a: subprocess.run(["git"] + list(a), check=True, capture_output=True)
        with tempfile.TemporaryDirectory() as d:
            g("init", "-q", d)
            Path(d, "notes.md").write_text("x")
            for repo in ("tax-admin", "tax-baskool"):
                g("init", "-q", str(Path(d, repo)))
                Path(d, repo, "src").mkdir()
                Path(d, repo, "src", "deep.ts").write_text("x")
            Path(d, "tax-admin", ".gitignore").write_text("dist/\n")
            Path(d, "tax-admin", "dist").mkdir()
            Path(d, "tax-admin", "dist", "out.js").write_text("x")
            g("-C", str(Path(d, "tax-admin")), "-c", "user.email=t@e", "-c", "user.name=t", "add", "-A")
            g("-C", str(Path(d, "tax-admin")), "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "x")
            g("-C", d, "add", "tax-admin", "notes.md")           # tracked as a link (gitlink)
            want = ["notes.md", "tax-admin/.gitignore", "tax-admin/src/deep.ts", "tax-baskool/src/deep.ts"]
            self.assertEqual(sorted(sw.local_files(d)), want)
            r = subprocess.run([sys.executable, "-c", sw.FS_PY, "all", d], capture_output=True, text=True)
            self.assertEqual(sorted(json.loads(r.stdout)["files"]), want)
            withd = sw.with_dirs(sw.local_files(d))
            self.assertEqual(withd.count("tax-baskool/"), 1)
            self.assertIn("tax-baskool/src/", withd)
        self.assertEqual(sw.with_dirs(["x/", "x/a"]), ["x/", "x/a"])


class OpenInVSCode(TmpHome):
    def test_server_folder_with_tilde_is_given_to_vscode_in_full(self):
        app = sw.App(self.store, sw.ServerConf("esi", "h", "22", "/k"))
        p = {"name": "shop", "source": "manual", "local": "", "localExists": False, "remote": "~/work/shop"}
        app.projects = lambda max_age=0: [p]
        ls = mock.Mock(return_value={"home": "/home/esi", "entries": []})
        app.server_ls = ls
        with mock.patch.object(sw, "which", return_value="/usr/bin/code"), \
                mock.patch.object(sw, "write_vscode_ssh_alias"), mock.patch.object(sw, "is_wsl", return_value=False), \
                mock.patch.object(sw.subprocess, "Popen") as po:
            app.open_editor({"project": "shop", "where": "server"})
            self.assertEqual(po.call_args[0][0], ["/usr/bin/code", "--remote", "ssh-remote+" + sw.VSCODE_HOST_ALIAS, "/home/esi/work/shop"])
            app.open_editor({"project": "shop", "where": "server"})
            self.assertEqual(ls.call_count, 1, "the home folder is asked once")
            p["remote"] = "/srv/shop"
            app.open_editor({"project": "shop", "where": "server"})
            self.assertEqual(po.call_args[0][0][-1], "/srv/shop")
        ls.return_value = {"home": ""}
        app._server_homes.clear()
        p["remote"] = "~"
        with mock.patch.object(sw, "which", return_value="/usr/bin/code"), \
                mock.patch.object(sw, "write_vscode_ssh_alias"), mock.patch.object(sw, "is_wsl", return_value=False), \
                mock.patch.object(sw.subprocess, "Popen") as po:
            with self.assertRaises(sw.ApiError):
                app.open_editor({"project": "shop", "where": "server"})
            po.assert_not_called()


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

    def test_discard_changes(self):
        repo = sw.GitRepo(self.app, self.p)
        Path(self.a, "f.txt").write_text("changed\n")
        Path(self.a, "new.txt").write_text("new\n")
        Path(self.a, "staged.txt").write_text("staged\n")
        self.git(self.a, "add", "staged.txt")
        os.makedirs(os.path.join(self.a, "dir"))
        Path(self.a, "dir", "x.txt").write_text("x\n")
        self.assertEqual(len(repo.status()["files"]), 4)
        with self.assertRaises(sw.ApiError):
            repo.discard(["not-changed.txt"])
        with self.assertRaises(sw.ApiError):
            repo.discard(["../f.txt"])
        repo.discard(["f.txt"])
        self.assertEqual(Path(self.a, "f.txt").read_text(), "one\n")
        repo.discard(["new.txt", "staged.txt", "dir/x.txt"])
        self.assertEqual(repo.status()["files"], [])
        self.assertFalse(os.path.exists(os.path.join(self.a, "new.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.a, "staged.txt")))

    def make_conflict(self, extra=False):
        """a and b both change f.txt; a pulls b's commit → conflict in a."""
        repo = sw.GitRepo(self.app, self.p)
        Path(self.a, "f.txt").write_text("top\none\nend\n")
        self.git(self.a, "commit", "-q", "-am", "base")
        repo.push()
        b = self.clone(self.remote, "b")
        Path(b, "f.txt").write_text("top\nTHEIRS\nend\n")
        self.git(b, "commit", "-q", "-am", "theirs")
        self.git(b, "push", "-q")
        Path(self.a, "f.txt").write_text("top\nOURS\nend\n")
        if extra:
            Path(self.a, "other.txt").write_text("x\n")
            self.git(self.a, "add", "other.txt")
        self.git(self.a, "commit", "-q", "-am", "ours")
        self.assertEqual(repo.pull(), "conflicts")
        return repo

    def test_resolve_conflict_with_picked_text(self):
        repo = self.make_conflict()
        st = repo.status()
        self.assertTrue(st["merging"])
        self.assertEqual(st["conflicts"], 1)
        c = repo.conflict("f.txt")
        self.assertIn("<<<<<<< HEAD\nOURS\n=======\nTHEIRS\n>>>>>>>", c["text"])
        self.assertTrue(c["merging"] and c["exists"] and not c["binary"])
        with self.assertRaises(sw.ApiError):
            repo.resolve("f.txt", "top\n<<<<<<< HEAD\nx\n")          # markers left
        with self.assertRaises(sw.ApiError):
            repo.conflict("other.txt")                                  # no conflict there
        repo.resolve("f.txt", "top\nOURS\nTHEIRS\nend\n")
        self.assertEqual(Path(self.a, "f.txt").read_text(), "top\nOURS\nTHEIRS\nend\n")
        st = repo.status()
        self.assertEqual(st["conflicts"], 0)
        self.assertTrue(st["merging"], "still needs the merge commit")
        # committing a merge with a file list must not be a "partial commit"
        repo.commit("merge", ["f.txt"])
        self.assertFalse(repo.status()["merging"])
        self.assertIn("THEIRS", self.git(self.a, "show", "HEAD:f.txt"))

    def test_resolve_conflict_by_side_and_abort(self):
        repo = self.make_conflict(extra=True)
        repo.resolve("f.txt", side="theirs")
        self.assertEqual(Path(self.a, "f.txt").read_text(), "top\nTHEIRS\nend\n")
        self.assertEqual(repo.status()["conflicts"], 0)
        repo.merge_abort()
        self.assertFalse(repo.status()["merging"])
        self.assertEqual(Path(self.a, "f.txt").read_text(), "top\nOURS\nend\n")
        with self.assertRaises(sw.ApiError):
            repo.merge_abort()

    def test_status_says_when_the_remote_was_last_asked(self):
        repo = sw.GitRepo(self.app, self.p)
        repo.push()
        self.assertIsNone(repo.status()["fetched"], "never fetched yet")
        t = time.time()
        repo.fetch()
        self.assertGreaterEqual(repo.status()["fetched"], t)
        # per repository: another project with the same folder has its own time
        self.assertIsNone(sw.GitRepo(self.app, dict(self.p, name="b")).status()["fetched"])
        # pull and the watcher's fetch count too
        self.app._git_fetched.clear()
        repo.pull()
        self.assertIsNotNone(repo.status()["fetched"])
        self.app._git_fetched.clear()
        self.app.git_summary_one(self.p, fetch=True)
        self.assertIsNotNone(repo.status()["fetched"])

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
        # an empty .git, and a stray `git init` (no commits) over the repositories: not listed
        os.makedirs(os.path.join(proj, "docs", ".git"))
        subprocess.run(["git", "init", "-q", proj], check=True)
        self.assertEqual(sw.git_repos(self.app, p), {"where": "local", "repos": ["api", "web"]})
        subprocess.run(["git", "-C", proj, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        self.assertEqual(sw.git_repos(self.app, p)["repos"], ["", "api", "web"])

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

    def test_branches_list_switch_create_track_stash(self):
        repo = sw.GitRepo(self.app, self.p)
        repo.push()
        b = self.clone(self.remote, "b3")
        self.git(b, "checkout", "-qb", "feature/login")
        Path(b, "login.txt").write_text("login\n")
        self.git(b, "add", "login.txt")
        self.git(b, "commit", "-qm", "login page")
        self.git(b, "push", "-q", "-u", "origin", "feature/login")
        repo.fetch()
        br = repo.branches()
        self.assertEqual(br["current"], "main")
        self.assertEqual([x["name"] for x in br["local"]], ["main"])
        self.assertEqual(br["local"][0]["upstream"], "origin/main")
        rem = {x["name"]: x for x in br["remote"]}
        self.assertEqual(set(rem), {"origin/main", "origin/feature/login"})        # no origin/HEAD
        self.assertTrue(rem["origin/main"]["local"])
        self.assertFalse(rem["origin/feature/login"]["local"])
        self.assertEqual(rem["origin/feature/login"]["subject"], "login page")
        # a branch that only exists on GitHub becomes a local one that tracks it
        repo.switch("origin/feature/login", track=True)
        st = repo.status()
        self.assertEqual((st["branch"], st["upstream"]), ("feature/login", "origin/feature/login"))
        self.assertTrue(Path(self.a, "login.txt").exists())
        # back, and a new branch from here
        repo.switch("main")
        repo.switch("fix/typo", create=True)
        self.assertEqual(repo.branches()["current"], "fix/typo")
        self.assertEqual(sw.GitRepo(self.app, self.p).status()["branch"], "fix/typo")
        # uncommitted changes that clash with the other branch: asked, then stashed
        repo.switch("main")
        Path(self.a, "login.txt").write_text("mine, not committed\n")
        with self.assertRaises(sw.ApiError) as e:
            repo.switch("feature/login")
        self.assertEqual(e.exception.status, 409)
        self.assertEqual(repo.branches()["current"], "main")
        repo.switch("feature/login", stash=True)
        self.assertEqual(repo.branches()["current"], "feature/login")
        self.assertIn("simab: before switching to feature/login", self.git(self.a, "stash", "list"))
        self.assertEqual(Path(self.a, "login.txt").read_text(), "login\n")
        # bad names never reach git switch
        for bad in ("", "-x", "a..b", "with space", "--orphan", None):
            with self.assertRaises(sw.ApiError, msg=repr(bad)):
                repo.switch(bad, create=True)
        # the summary knows the branch
        self.assertEqual(self.app.git_summary_one(self.p)["branch"], "feature/login")

    def test_history_shows_what_to_push_and_pull(self):
        repo = sw.GitRepo(self.app, self.p)
        self.assertEqual([c["state"] for c in repo.log()["commits"]], ["local"])        # no upstream yet
        repo.push()
        lg = repo.log()
        self.assertEqual((lg["upstream"], lg["ahead"], lg["behind"]), ("origin/main", 0, 0))
        self.assertEqual([(c["subject"], c["state"]) for c in lg["commits"]], [("first", "synced")])
        # someone else pushes; we commit locally
        b = self.clone(self.remote, "b4")
        Path(b, "x.txt").write_text("x\n")
        self.git(b, "add", "x.txt")
        self.git(b, "commit", "-qm", "from b")
        self.git(b, "push", "-q")
        Path(self.a, "f.txt").write_text("two\n")
        repo.commit("mine", None)
        repo.fetch()
        lg = repo.log()
        self.assertEqual((lg["ahead"], lg["behind"]), (1, 1))
        states = {c["subject"]: c["state"] for c in lg["commits"]}
        self.assertEqual(states, {"mine": "push", "from b": "pull", "first": "synced"})
        c = next(c for c in lg["commits"] if c["subject"] == "mine")
        self.assertEqual((c["author"], len(c["hash"]), c["merge"]), ("T", 40, False))
        self.assertIn("HEAD -> main", c["refs"])
        shown = repo.show(c["short"])
        self.assertIn("mine", shown)
        self.assertIn("+two", shown)
        for bad in ("HEAD", "--all", "x" * 3, "../etc"):
            with self.assertRaises(sw.ApiError):
                repo.show(bad)
        # an empty repository: no history, no error
        e = os.path.join(self.tmp.name, "empty")
        subprocess.run(["git", "init", "-q", e], check=True)
        self.assertEqual(sw.GitRepo(self.app, dict(self.p, local=e)).log()["commits"], [])

    def test_files_in_gitignore_but_tracked_are_marked_and_can_be_untracked(self):
        repo = sw.GitRepo(self.app, self.p)
        Path(self.a, ".env").write_text("A=1\n")
        self.git(self.a, "add", ".env")
        self.git(self.a, "commit", "-qm", "oops, .env")
        Path(self.a, ".gitignore").write_text(".env\nlogs/\n")
        Path(self.a, ".env").write_text("A=2\n")
        Path(self.a, "logs").mkdir()
        Path(self.a, "logs", "x.log").write_text("x")             # ignored and untracked: not listed at all
        st = repo.status()
        by = {f["path"]: f for f in st["files"]}
        self.assertEqual(set(by), {".env", ".gitignore"})
        self.assertTrue(by[".env"].get("ignored"))
        self.assertFalse(by[".gitignore"].get("ignored"))
        self.assertEqual(st["ignoredTracked"], 1)
        repo.untrack([".env"])
        st = repo.status()
        by = {f["path"]: f for f in st["files"]}
        self.assertEqual((by[".env"]["x"], by[".env"].get("ignored")), ("D", None))     # removed from git, still on disk
        self.assertTrue(Path(self.a, ".env").exists())
        with self.assertRaises(sw.ApiError):
            repo.untrack(["../x"])

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
        self.assertEqual(a[a.index("--model") + 1], "opus")
        self.assertEqual(a[a.index("--effort") + 1], "medium")
        b = sw.chat_args("bypassPermissions")
        self.assertEqual(b[b.index("--permission-mode") + 1], "default")
        # no vague "default model": empty / "default" → Opus 5.5; effort medium unless chosen
        for m in ("", "default", None):
            c = sw.chat_args("default", m)
            self.assertEqual(c[c.index("--model") + 1], "opus")
        c = sw.chat_args("default", "sonnet", "", "high")
        self.assertEqual((c[c.index("--model") + 1], c[c.index("--effort") + 1]), ("sonnet", "high"))
        c = sw.chat_args("default", "sonnet", "", "ultra")
        self.assertEqual(c[c.index("--effort") + 1], "medium")
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
        self.assertEqual((b["model"], b["effort"]), ("opus", "medium"))
        argv = b["make_argv"]("default", "", "")
        self.assertIn("--effort medium", argv[-1])
        self.store.set_chat_prefs("cheshmeh", effort="xhigh")
        self.assertEqual(sw.build_chat({"kind": "chat", "project": "cheshmeh"}, conf, self.store, projects)["effort"], "xhigh")
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
        for f in ("app.js", "chat.js", "files.js", "tasks.js", "computers.js", "prompts.js", "voice.js", "sounds.js"):
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
        files = (ROOT / "gui" / "static" / "files.js").read_text()
        self.assertEqual(props & set(re.findall(r"\bP\.([A-Za-z_]\w*)\s*=\s*function", files)), set())
        tasks = (ROOT / "gui" / "static" / "tasks.js").read_text()
        self.assertEqual(props & set(re.findall(r"\bP\.([A-Za-z_]\w*)\s*=\s*function", tasks)), set())

    def test_web_version(self):
        node = find_node()
        if not node:
            self.skipTest("node not found")
        r = subprocess.run([node, str(ROOT / "tests" / "js" / "web_test.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_voice(self):
        node = find_node()
        if not node:
            self.skipTest("node not found")
        r = subprocess.run([node, str(ROOT / "tests" / "js" / "voice_test.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_sounds(self):
        node = find_node()
        if not node:
            self.skipTest("node not found")
        r = subprocess.run([node, str(ROOT / "tests" / "js" / "sounds_test.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

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
            self.assertIn("ignores=node_modules,dist git", create, "defaults when not given")
            r = self.run_add(home, [proj, "--yes", "--no-wait", "--remote", "~/b", "--name", "b", "--ignores", "vendor,/out"])
            self.assertIn("ignores=vendor,/out git", next(l for l in r.stdout.splitlines() if l.startswith("CREATE")))
            r = self.run_add(home, [proj, "--yes", "--no-wait", "--remote", "~/c", "--name", "c", "--ignores", ""])
            self.assertIn("ignores= git", next(l for l in r.stdout.splitlines() if l.startswith("CREATE")), "empty list = sync everything")

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
            app.add_project({"mode": "upload", "local": str(proj), "git": True, "ignores": [" node_modules ", "/dist", "node_modules"]})
            self.assertEqual(seen["argv"][2:], ["add-local", str(proj), "--yes", "--no-wait", "--git", "on", "--ignores", "node_modules,/dist"])
            app.add_project({"mode": "upload", "local": str(proj), "ignores": []})
            self.assertEqual(seen["argv"][-2:], ["--ignores", ""])
            with self.assertRaises(sw.ApiError):
                app.add_project({"mode": "upload", "local": str(proj), "ignores": ["a,b"]})

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

    def run_ignore(self, args, ignores=("node_modules",), create_fails=False):
        sess = json.dumps([{"name": "shop", "alpha": {"path": "/l/shop"}, "beta": {"host": "h", "user": "esi", "port": 225, "path": "/r/shop"},
                            "configuration": {"ignores": list(ignores)}}])
        prog = f"""
IS_WSL=0; SSH_USER=esi; SSH_HOST=h; SSH_PORT=225; DEFAULT_IGNORES=node_modules,dist
title() {{ :; }}; info() {{ echo "INFO $*"; }}; ok() {{ echo "OK $*"; }}; warn() {{ echo "WARN $*"; }}; err() {{ echo "ERR $*"; }}
daemon_running() {{ return 0; }}; daemon_start() {{ :; }}
ask() {{ printf '%s' "$2"; }}; confirm() {{ return 1; }}
sessions_json() {{ printf '%s' '{sess}'; }}
session_git() {{ echo off; }}
mutagen() {{ echo "MUTAGEN $*" >&2; }}
N=0; create_session() {{ N=$((N+1)); echo "CREATE name=$1 local=$2 url=$3 ignores=[$4] git=$5" >&2; {"[ $N -gt 1 ]" if create_fails else "true"}; }}
wait_sync() {{ echo "WAIT $*"; return 0; }}
""" + self.funcs("ignore_set", "sanitize_name") + "\nignore_set " + " ".join(shlex.quote(a) for a in args)
        r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True)
        r.stdout += r.stderr
        return r

    def test_ignore_recreates_the_session_with_the_new_list(self):
        r = self.run_ignore(["shop", " node_modules, *.log,,/dist ,node_modules"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("WAIT shop flush", r.stdout)
        self.assertIn("MUTAGEN sync terminate shop", r.stdout)
        self.assertIn("CREATE name=shop local=/l/shop url=esi@h:225:/r/shop ignores=[node_modules,*.log,/dist] git=off", r.stdout)

    def test_ignore_defaults_empty_and_no_change(self):
        r = self.run_ignore(["shop", "--default"], ignores=())
        self.assertIn("ignores=[node_modules,dist] git=off", r.stdout)
        r = self.run_ignore(["shop", ""])
        self.assertIn("ignores=[] git=off", r.stdout)
        r = self.run_ignore(["shop", "node_modules"])
        self.assertIn("No change", r.stdout)
        self.assertNotIn("terminate", r.stdout)

    def test_ignore_restores_the_old_list_when_mutagen_refuses(self):
        r = self.run_ignore(["shop", "bad["], create_fails=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("CREATE name=shop local=/l/shop url=esi@h:225:/r/shop ignores=[node_modules] git=off", r.stdout)
        self.assertIn("Restoring", r.stdout)

    def test_create_session_ignores_flag(self):
        prog = 'LABEL=x; mutagen() { printf "<%s>" "$@"; echo; }\n' + self.funcs("create_session") + \
               '\ncreate_session n /l u "a,b" on; create_session n /l u "" off'
        out = subprocess.run(["bash", "-c", "set -u\n" + prog], capture_output=True, text=True).stdout.splitlines()
        self.assertIn("<--ignore><a,b>", out[0])
        self.assertNotIn("--ignore>", out[1])
        self.assertIn("<--ignore-vcs>", out[1])

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


class PruneScript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for d in ("a/node_modules/x", "node_modules", ".git/node_modules", "dist", "web/dist", "logs", "out"):
            os.makedirs(os.path.join(self.root, d))
        for f in ("a/node_modules/x/i.js", "x.log", "logs/y.log", "web/dist/z", "out/keep"):
            Path(self.root, f).write_text("abc")
        os.symlink(os.path.join(self.root, "out"), os.path.join(self.root, "web", "lnk"))
        self.ns = {"__name__": "t"}
        exec(sw.PRUNE_PY, self.ns)

    def tearDown(self):
        self.tmp.cleanup()

    def paths(self, pats):
        return [x["path"] for x in self.ns["prune_find"](self.root, pats)["items"]]

    def test_patterns(self):
        self.assertEqual(self.paths(["node_modules"]), ["a/node_modules", "node_modules"])     # .git never
        self.assertEqual(self.paths(["/dist"]), ["dist"])
        self.assertEqual(self.paths(["web/dist"]), ["web/dist"])
        self.assertEqual(self.paths(["*.log"]), ["logs/y.log", "x.log"])
        self.assertEqual(self.paths(["log*/"]), ["logs"])
        self.assertEqual(self.paths(["**/i.js"]), ["a/node_modules/x/i.js"])
        self.assertEqual(self.paths(["!x.log", "", "# c"]), [])
        f = self.ns["prune_find"](self.root, ["*.log"])
        self.assertEqual(f["total"], 6)
        self.assertEqual(self.ns["prune_find"](self.root + "/nope", ["x"])["items"], [])

    def test_delete_only_listed_and_matching_never_follows_links(self):
        r = self.ns["prune_delete"](self.root, ["node_modules", "lnk"], ["a/node_modules", "web/lnk", ".git/node_modules", "out", "../x"])
        self.assertEqual(r, {"deleted": 2, "errors": []})
        self.assertFalse(os.path.exists(os.path.join(self.root, "a", "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "node_modules")))           # not in the confirmed list
        self.assertTrue(os.path.isdir(os.path.join(self.root, ".git", "node_modules")))
        self.assertFalse(os.path.lexists(os.path.join(self.root, "web", "lnk")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "out", "keep")))           # link target kept

    def test_runs_as_python_c(self):
        r = subprocess.run([sys.executable, "-c", sw.PRUNE_PY, "find", self.root, '["*.log"]'], capture_output=True, text=True)
        self.assertEqual([x["path"] for x in json.loads(r.stdout)["items"]], ["logs/y.log", "x.log"])


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
        self.assertEqual(d["ignores"], ["node_modules"])
        self.assertIn("node_modules", d["defaultIgnores"])
        self.assertIn(".venv", d["defaultIgnores"])
        self.assertEqual((d["local"], d["remote"], d["git"], d["cycles"]), (self.local, "/srv/shop", False, 7))
        self.assertEqual(d["conflicts"], [{"path": "src/a.go", "local": [{"path": "src/a.go", "was": "file", "now": "file"}],
                                           "server": [{"path": "src/a.go", "was": "file", "now": "deleted"}]}])

    def test_set_ignores_runs_the_script(self):
        seen = []
        with mock.patch.object(sw, "mutagen_sessions", return_value=self.sess), \
                mock.patch.object(self.app, "run_script", side_effect=lambda a, **k: (seen.append(a), (0, "ok"))[1]):
            d = self.app.set_ignores("shop", [" node_modules ", "*.log", "", "node_modules", "/dist"])
            self.assertEqual(d["ignores"], ["node_modules", "*.log", "/dist"])
            self.assertEqual(seen[-1], ["ignore", "shop", "node_modules,*.log,/dist"])
            self.app.set_ignores("shop", [])
            self.assertEqual(seen[-1], ["ignore", "shop", ""])
            for bad in ("x", ["a,b"], ["a\nb"], [3]):
                with self.assertRaises(sw.ApiError):
                    self.app.set_ignores("shop", bad)
            with self.assertRaises(sw.ApiError) as cm:
                self.app.set_ignores("nope", ["x"])
            self.assertEqual(cm.exception.status, 404)
        self.assertEqual(len(seen), 2)
        with mock.patch.object(sw, "mutagen_sessions", return_value=self.sess), \
                mock.patch.object(self.app, "run_script", return_value=(1, "ERR Could not recreate the sync")):
            with self.assertRaises(sw.ApiError):
                self.app.set_ignores("shop", ["["])

    def test_preview_and_delete_only_what_is_new_and_ticked(self):
        for d in ("node_modules/x", "web/dist", "dist", ".git/node_modules"):
            os.makedirs(os.path.join(self.local, d))
        Path(self.local, "node_modules", "x", "i.js").write_text("12345")
        Path(self.local, "dist", "a.js").write_text("1")
        Path(self.local, "web", "dist", "b.js").write_text("1")
        sess = [dict(self.sess[0], status="watching")]
        seen, remote = [], []

        def fake_remote(conf, code, args, multiplex=True, timeout=30):
            remote.append(args)
            return {"items": [{"path": "dist", "dir": True, "size": 9}], "total": 9, "truncated": False} if args[0] == "find" \
                else {"deleted": 1, "errors": []}
        with mock.patch.object(sw, "mutagen_sessions", return_value=sess), mock.patch.object(sw, "remote_python", side_effect=fake_remote), \
                mock.patch.object(self.app, "run_script", side_effect=lambda a, **k: (seen.append(a), (0, "ok"))[1]):
            pv = self.app.ignores_preview("shop", ["node_modules", "/dist"])
            self.assertEqual(pv["added"], ["/dist"])                      # node_modules was ignored already
            self.assertEqual([x["path"] for x in pv["local"]["items"]], ["dist"])   # web/dist: not at the top
            self.assertEqual(pv["server"]["total"], 9)
            self.assertEqual(remote[-1][:2], ["find", "/srv/shop"])
            self.assertEqual(json.loads(remote[-1][2]), ["/dist"])
            r = self.app.set_ignores("shop", ["node_modules", "/dist"], {"local": ["dist", "web/dist", "node_modules"]})
            self.assertEqual(seen[-1], ["ignore", "shop", "node_modules,/dist"])
            self.assertEqual(r["deleted"], {"local": {"deleted": 1, "errors": []}})
            self.assertFalse(os.path.exists(os.path.join(self.local, "dist")))
            self.assertTrue(os.path.isfile(os.path.join(self.local, "web", "dist", "b.js")))
            self.assertTrue(os.path.isfile(os.path.join(self.local, "node_modules", "x", "i.js")))  # not new: never deleted
            self.assertEqual(len([a for a in remote if a[0] == "delete"]), 0)                       # server not ticked
            self.app.set_ignores("shop", ["node_modules", "/dist", "*.js"], {"server": ["dist"]})
            self.assertEqual(remote[-1][0], "delete")
            self.assertEqual(json.loads(remote[-1][2]), ["/dist", "*.js"])
            self.assertEqual(json.loads(remote[-1][3]), ["dist"])
        with mock.patch.object(sw, "mutagen_sessions", return_value=sess), \
                mock.patch.object(self.app, "run_script", return_value=(1, "ERR no")), mock.patch.object(sw, "remote_python") as rp:
            with self.assertRaises(sw.ApiError):
                self.app.set_ignores("shop", ["node_modules", "*.go"], {"local": ["src/a.go"], "server": ["x"]})
            rp.assert_not_called()
        self.assertTrue(os.path.isfile(os.path.join(self.local, "src", "a.go")))   # sync not changed → nothing deleted

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


class BashProgress(unittest.TestCase):
    """Long setup steps show they are alive (spinner, seconds, latest output line)."""

    def prog(self, body):
        script = (ROOT / "simabworkspace.sh").read_text()
        fn = re.search(r"^run_step\(\) \{.*?^\}", script, re.S | re.M).group(0)
        return "C_DIM=; C_0=\nok() { echo \"OK $*\"; }\nerr() { echo \"ERR $*\" >&2; }\n" + fn + "\n" + body

    def test_without_a_terminal(self):
        r = subprocess.run(["bash", "-c", self.prog("run_step 'Installing jq' bash -c 'echo Unpacking jq; sleep 0.3'")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertRegex(r.stdout, r"^OK Installing jq \(\d+s\)\n$")
        r = subprocess.run(["bash", "-c", self.prog("run_step 'Installing x' bash -c 'echo E: Unable to locate package x; exit 100'")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 100)
        self.assertIn("ERR Installing x — failed:", r.stderr)
        self.assertIn("Unable to locate package x", r.stderr)

    def test_in_a_terminal_it_shows_progress(self):
        import pty
        pid, fd = pty.fork()
        if pid == 0:
            os.execvp("bash", ["bash", "-c", self.prog("run_step 'Updating the package list' bash -c 'echo Get:1 http://archive jammy InRelease; sleep 1.2; echo Reading package lists'")])
        out = b""
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        os.waitpid(pid, 0)
        text = out.decode("utf-8", "replace")
        self.assertIn("Updating the package list  0s", text)
        self.assertIn("Get:1 http://archive jammy InRelease", text)       # the user sees it moving
        self.assertRegex(text, r"Updating the package list  1s")
        self.assertIn("OK Updating the package list", text)


class StayingAlive(unittest.TestCase):
    def test_wsl_keepalive_command(self):
        argv = sw.wsl_keepalive_argv("Ubuntu-22.04", 4321)
        self.assertEqual(argv[:2], ["powershell.exe", "-NoProfile"])
        self.assertEqual(argv[-1], "Start-Process -WindowStyle Hidden -FilePath wsl.exe -ArgumentList "
                                   "'-d Ubuntu-22.04 -e sh -c \"while kill -0 4321 2>/dev/null; do sleep 10; done\"'")
        self.assertNotIn("-d", sw.wsl_keepalive_argv("bad name;rm", 1)[-1].split("ArgumentList")[1].split("-e")[0])

    def test_keepalive_only_on_wsl(self):
        with mock.patch.object(sw, "is_wsl", return_value=False), mock.patch.object(sw.subprocess, "Popen") as p:
            sw.start_wsl_keepalive()
            p.assert_not_called()
        with mock.patch.object(sw, "is_wsl", return_value=True), mock.patch.object(sw.shutil, "which", return_value="/x/powershell.exe"), \
                mock.patch.object(sw.subprocess, "Popen") as p:
            sw.start_wsl_keepalive()
            self.assertEqual(p.call_args[0][0][0], "powershell.exe")

    def test_wsl_vm_ip_only_nat_range(self):
        def run(out):
            return mock.patch.object(sw.subprocess, "run", return_value=mock.Mock(stdout=out))
        with mock.patch.object(sw, "is_wsl", return_value=True):
            with run("2: eth0    inet 172.25.144.7/20 brd 172.25.159.255 scope global eth0"):
                self.assertEqual(sw.wsl_vm_ip(), "172.25.144.7")
            with run("2: eth0    inet 192.168.1.20/24 brd 192.168.1.255 scope global eth0"):   # mirrored: LAN address
                self.assertEqual(sw.wsl_vm_ip(), "")
        with mock.patch.object(sw, "is_wsl", return_value=False):
            self.assertEqual(sw.wsl_vm_ip(), "")

    def test_wsl_window_falls_back_to_vm_ip(self):
        st = {"port": 45027, "token": "tok", "wslIp": "172.25.144.7"}
        for reaches, host in ((False, "172.25.144.7"), (True, "127.0.0.1")):
            with mock.patch.object(sw, "is_wsl", return_value=True), mock.patch.object(sw, "Store"), \
                    mock.patch.object(sw, "windows_reaches", return_value=reaches), \
                    mock.patch.object(sw.shutil, "which", return_value="/x/cmd.exe"), \
                    mock.patch.object(sw.subprocess, "Popen") as p, mock.patch("builtins.print"):
                sw.Store.return_value.settings = {}
                sw.open_window(st)
                self.assertIn(f"--app=http://{host}:45027/#t=tok", p.call_args[0][0])

    def test_wsl_alt_host_is_accepted(self):
        app = mock.Mock(alt_hosts={"172.25.144.7:45027"})
        h = type("H", (sw.Handler,), {"app": app})
        o = h.__new__(h)
        o.server = mock.Mock(server_address=("127.0.0.1", 45027))
        for host, ok in (("172.25.144.7:45027", True), ("127.0.0.1:45027", True), ("10.0.0.5:45027", False)):
            o.headers = {"Host": host}
            self.assertEqual(o._host_ok(), ok, host)

    def test_crashes_are_logged_with_a_time(self):
        code = ("import sys,threading; sys.path.insert(0, %r); import server as s; s.install_crash_logging(); "
                "t=threading.Thread(target=lambda: 1/0); t.start(); t.join()") % str(ROOT / "gui")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertRegex(r.stderr, r"(?s)\d{4}-\d\d-\d\d \d\d:\d\d:\d\d CRASH .*ZeroDivisionError")


class BashScript(unittest.TestCase):
    def test_syntax_and_gui_command(self):
        import subprocess
        script = ROOT / "simabworkspace.sh"
        self.assertEqual(subprocess.run(["bash", "-n", str(script)]).returncode, 0)
        text = script.read_text()
        self.assertIn("gui)", text)
        self.assertIn('VERSION="1.59.0"', text)


if __name__ == "__main__":
    unittest.main()

class GitApplyCached(GitFlow):
    """Tests for per-hunk staging: GitRepo.apply_cached and commit(patches=...)."""

    def test_apply_cached_raises_on_empty_patch(self):
        repo = sw.GitRepo(self.app, self.p, "", "local")
        with self.assertRaises(sw.ApiError):
            repo.apply_cached("")
        with self.assertRaises(sw.ApiError):
            repo.apply_cached("   ")

    def test_commit_with_patch_stages_selected_hunk(self):
        repo = sw.GitRepo(self.app, self.p, "", "local")
        # Set up a file with two independent change sites
        (Path(self.a) / "pfile.txt").write_text(
            "aaa\nbbb\nccc\nddd\neee\nfff\nggg\n")
        self.git(self.a, "add", "pfile.txt")
        self.git(self.a, "commit", "-m", "base")
        # Modify both areas
        (Path(self.a) / "pfile.txt").write_text(
            "aaa\nBBB\nccc\nddd\neee\nFFF\nggg\n")
        # Build a valid patch for only the second change (FFF at line 6)
        patch = (
            "--- a/pfile.txt\n"
            "+++ b/pfile.txt\n"
            "@@ -4,4 +4,4 @@\n"
            " ddd\n"
            " eee\n"
            "-fff\n"
            "+FFF\n"
            " ggg\n"
        )
        # commit() calls apply_cached internally
        repo.commit("hunk commit", files=[], patches={"pfile.txt": patch})
        log = repo.log()
        self.assertEqual(log["commits"][0]["subject"], "hunk commit")
        # Working tree still has the first change (BBB) — not committed
        content = (Path(self.a) / "pfile.txt").read_text()
        self.assertIn("BBB", content)

    def test_commit_with_mixed_files_and_patches(self):
        repo = sw.GitRepo(self.app, self.p, "", "local")
        (Path(self.a) / "whole.txt").write_text("whole\n")
        (Path(self.a) / "patched.txt").write_text("line1\nline2\nline3\n")
        self.git(self.a, "add", ".")
        self.git(self.a, "commit", "-m", "init mixed")
        (Path(self.a) / "whole.txt").write_text("whole changed\n")
        (Path(self.a) / "patched.txt").write_text("line1\nCHANGED\nline3\n")
        patch = (
            "--- a/patched.txt\n"
            "+++ b/patched.txt\n"
            "@@ -1,3 +1,3 @@\n"
            " line1\n"
            "-line2\n"
            "+CHANGED\n"
            " line3\n"
        )
        repo.commit("mixed commit", files=["whole.txt"], patches={"patched.txt": patch})
        log = repo.log()
        self.assertEqual(log["commits"][0]["subject"], "mixed commit")


class SkillsRegistry(TmpHome):
    """Skills Registry: store, App methods, and HTTP routes."""

    def setUp(self):
        super().setUp()
        self.app = sw.App(self.store, sw.ServerConf("", "h"))

    # -- Store --

    def test_skill_repos_start_empty(self):
        self.assertEqual(self.store.skill_repos(), [])

    def test_skill_repos_roundtrip(self):
        repos = [{"url": "https://github.com/owner/repo", "name": "owner/repo"}]
        out = self.store.set_skill_repos(repos)
        self.assertEqual(out, repos)
        self.assertEqual(self.store.skill_repos(), repos)
        # persists across reload
        self.assertEqual(sw.Store(self.store.path).skill_repos(), repos)

    # -- App.set_skill_repos validation --

    def test_set_skill_repos_cleans_input(self):
        dirty = [
            {"url": "https://github.com/a/b", "name": "a/b"},
            {"url": "", "name": "empty url"},        # dropped
            "not a dict",                             # dropped
            {"url": "https://github.com/c/d", "name": "x" * 200},  # name truncated
        ]
        out = self.app.set_skill_repos(dirty)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["url"], "https://github.com/a/b")
        self.assertEqual(len(out[1]["name"]), 100)

    # -- list_project_skills, install_skill, uninstall_skill --

    def test_list_install_uninstall_skill(self):
        with tempfile.TemporaryDirectory() as remote:
            self.store.add_manual({"name": "sk-proj", "remote": remote})

            def fake_remote(conf, code, args, **kw):
                stdin = kw.get("stdin") or b""
                r = subprocess.run(
                    [sys.executable, "-I", "-c", code] + args,
                    capture_output=True, input=stdin, timeout=10
                )
                return json.loads(r.stdout)

            with mock.patch.object(sw, "remote_python", side_effect=fake_remote):
                # list: empty at start
                lst = self.app.list_project_skills("sk-proj")
                self.assertEqual(lst, [])

                # install
                self.app.install_skill("sk-proj", "myhook.md", "# My Hook\ncontent")
                lst2 = self.app.list_project_skills("sk-proj")
                self.assertEqual(lst2, ["myhook.md"])

                # uninstall
                self.app.uninstall_skill("sk-proj", "myhook.md")
                lst3 = self.app.list_project_skills("sk-proj")
                self.assertEqual(lst3, [])

    def test_install_skill_rejects_bad_name(self):
        with tempfile.TemporaryDirectory() as remote:
            self.store.add_manual({"name": "sk-proj2", "remote": remote})
            with self.assertRaises(sw.ApiError):
                self.app.install_skill("sk-proj2", "../escape.md", "bad")
            with self.assertRaises(sw.ApiError):
                self.app.install_skill("sk-proj2", "no-extension.txt", "bad")
