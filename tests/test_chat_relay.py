"""Claude keeps running on the server when the connection drops (the chat relay).
The relay runs here for real: `sh -c` stands in for ssh."""
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gui"))
import server as sw  # noqa: E402

# a slow fake Claude: every message is answered in two steps with a pause between them
SLOW_CLAUDE = r'''
import sys, json, os, time
open(os.environ["FAKE_PIDS"], "a").write("%d\n" % os.getpid())
def out(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
SID = "11111111-2222-3333-4444-555555555555"
for line in sys.stdin:
    m = json.loads(line)
    if m.get("type") == "control_request" and m["request"]["subtype"] == "initialize":
        out({"type": "control_response", "response": {"subtype": "success", "request_id": m["request_id"], "response": {"commands": []}}})
    elif m.get("type") == "user":
        out({"type": "system", "subtype": "init", "session_id": SID, "permissionMode": "default"})
        out({"type": "assistant", "message": {"id": "a", "content": [{"type": "text", "text": "step1"}]}, "session_id": SID})
        time.sleep(1.5)
        out({"type": "assistant", "message": {"id": "b", "content": [{"type": "text", "text": "step2"}]}, "session_id": SID})
        out({"type": "result", "subtype": "success", "session_id": SID})
'''


def wait_for(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:                                          # a zombie is not alive
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


class ChatRelay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.pids = self.home / "pids"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "FAKE_PIDS": str(self.pids)})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []

    def make(self, key, restored=False, resume=""):
        script = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(SLOW_CLAUDE)}"

        def make_argv(mode, model, resume, effort="", relay=None):
            self.calls.append(relay)
            return ["sh", "-c", sw.chat_relay_command(relay[0], relay[1], relay[2], script, relay[3] if len(relay) > 3 else "")]
        built = {"make_argv": make_argv, "cwd": str(self.home), "title": "Claude · p", "resume": resume, "mode": "default",
                 "model": "", "relay": key, "relay_restored": restored, "spec": {"kind": "chat", "project": "p", "where": "server"}}
        s = sw.ChatSession("c1", built)
        s.start()
        return s

    def texts(self, s):
        return [b["text"] for e in s.events if e.get("type") == "assistant" for b in e["message"]["content"]]

    def claude_pids(self):
        return [int(x) for x in self.pids.read_text().split()] if self.pids.exists() else []

    def test_connection_drop_mid_answer_then_same_claude_continues(self):
        s = self.make("0123456789abcdef")
        self.addCleanup(s.kill, True)
        self.assertTrue(wait_for(lambda: s.init is not None))
        self.assertEqual(s.spec["relay"], "0123456789abcdef")
        s.handle({"t": "send", "content": "hi"})
        self.assertTrue(wait_for(lambda: "step1" in self.texts(s)))
        s.proc.kill()                                    # the SSH connection drops mid-answer
        self.assertTrue(wait_for(lambda: any(e.get("type") == "_reconnect" for e in s.events)))
        self.assertTrue(wait_for(lambda: "result" in [e.get("type") for e in s.events]))
        self.assertEqual(self.texts(s), ["step1", "step2"])          # nothing lost, nothing twice
        self.assertTrue(any(e.get("type") == "_reconnected" for e in s.events))
        self.assertIsNone(s.exit_code)
        self.assertFalse(s.busy)
        self.assertEqual(self.calls[-1][0], "attach")
        self.assertEqual(len(self.claude_pids()), 1)                 # never a second Claude
        # the next message goes to the same Claude
        s.handle({"t": "send", "content": "again"})
        self.assertTrue(wait_for(lambda: self.texts(s).count("step2") == 2))
        self.assertEqual(len(self.claude_pids()), 1)
        # closing the tab stops it on the server and removes its folder
        pid = self.claude_pids()[0]
        s.kill()
        self.assertTrue(wait_for(lambda: not alive(pid)))
        self.assertTrue(wait_for(lambda: not (self.home / ".cache/simabworkspace/chats/0123456789abcdef").exists()))

    def test_app_restart_attaches_to_the_running_claude(self):
        s = self.make("fedcba9876543210")
        self.assertTrue(wait_for(lambda: s.init is not None))
        s.shutdown()                                     # the app quits: Claude stays
        pid = self.claude_pids()[0]
        time.sleep(0.3)
        self.assertTrue(alive(pid))
        s2 = self.make("fedcba9876543210", restored=True)    # the restored tab
        self.addCleanup(s2.kill, True)
        self.assertTrue(wait_for(lambda: s2.init is not None))
        s2.handle({"t": "send", "content": "hi"})
        self.assertTrue(wait_for(lambda: "step2" in self.texts(s2)))
        self.assertEqual(self.claude_pids(), [pid])
        s2.kill()
        self.assertTrue(wait_for(lambda: not alive(pid)))

    def test_opening_the_conversation_again_uses_the_claude_already_on_it(self):
        # the user's bug: the tab came back without its relay (or from History / the web version),
        # a second Claude started on the same conversation while the first one was still working
        sid = "11111111-2222-3333-4444-555555555555"
        s = self.make("1111111111111111")
        self.assertTrue(wait_for(lambda: s.init is not None))
        s.handle({"t": "send", "content": "hi"})
        self.assertTrue(wait_for(lambda: "step1" in self.texts(s)))
        conv = self.home / ".cache/simabworkspace/chats/1111111111111111/conv"
        self.assertTrue(wait_for(lambda: conv.exists() and conv.read_text() == sid), "the relay knows the conversation")
        s.shutdown()                                     # the app quits mid-answer: Claude keeps working
        pid = self.claude_pids()[0]
        s2 = self.make("2222222222222222", resume=sid)     # a NEW tab for the same conversation
        self.addCleanup(s2.kill, True)
        self.assertTrue(wait_for(lambda: s2.init is not None))
        self.assertEqual(self.claude_pids(), [pid], "no second Claude")
        self.assertEqual((s2.relay_key, s2.spec["relay"]), ("1111111111111111", "1111111111111111"))
        self.assertFalse((self.home / ".cache/simabworkspace/chats/2222222222222222").exists())
        # its live output reaches the new tab, and the next message goes to the same Claude
        self.assertTrue(wait_for(lambda: "step2" in self.texts(s2)))
        s2.handle({"t": "send", "content": "again"})
        self.assertTrue(wait_for(lambda: self.texts(s2).count("step2") >= 2))
        self.assertEqual(self.claude_pids(), [pid])
        # closing the tab stops that Claude
        s2.kill()
        self.assertTrue(wait_for(lambda: not alive(pid)))

    def test_a_finished_conversation_starts_a_new_claude(self):
        sid = "11111111-2222-3333-4444-555555555555"
        s = self.make("3333333333333333", resume=sid)
        self.addCleanup(s.kill, True)
        self.assertTrue(wait_for(lambda: s.init is not None))
        self.assertEqual(s.relay_key, "3333333333333333")
        self.assertEqual((self.home / ".cache/simabworkspace/chats/3333333333333333/conv").read_text(), sid)

    def test_attach_when_nothing_runs_reports_the_end(self):
        s = self.make("aaaaaaaaaaaaaaaa")
        self.assertTrue(wait_for(lambda: s.init is not None))
        pid = self.claude_pids()[0]
        os.kill(int((self.home / ".cache/simabworkspace/chats/aaaaaaaaaaaaaaaa/pid").read_text()), 15)
        self.assertTrue(wait_for(lambda: s.exit_code is not None))   # a real end, no reconnecting
        self.assertFalse(alive(pid))
        self.assertFalse(any(e.get("type") == "_reconnect" for e in s.events))
        time.sleep(0.5)
        errs = " ".join(str(e.get("text")) for e in s.events if e.get("type") == "_stderr")
        self.assertNotIn("Fatal Python error", errs)          # the relay exits cleanly
        s.kill(True)

    def test_command_and_restart(self):
        cmd = sw.chat_relay_command("start", "0123456789abcdef", 12, "cd x; exec claude")
        self.assertIn("command -v python3", cmd)
        self.assertIn("exec sh -c 'cd x; exec claude'", cmd)          # no python3: as before
        self.assertIn("start 0123456789abcdef 12 'cd x; exec claude'", cmd)
        self.assertIn("kill 0123456789abcdef; else exit 0", sw.chat_relay_command("kill", "0123456789abcdef"))
        sid = "11111111-2222-3333-4444-555555555555"
        self.assertIn("start 0123456789abcdef 0 'cd x' " + sid, sw.chat_relay_command("start", "0123456789abcdef", 0, "cd x", sid))
        self.assertNotIn("evil", sw.chat_relay_command("start", "0123456789abcdef", 0, "cd x", "evil; rm -rf"))
        compile(sw.CHAT_RELAY_PY, "relay", "exec")


class BuildChatRelay(unittest.TestCase):
    def test_build_chat_keeps_or_makes_a_relay_key(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": t}):
            store = sw.Store(Path(t) / "gui.json")
            projects = [{"name": "p", "remote": "/srv/p", "local": "", "localExists": False, "endpoint": {}, "source": "manual"}]
            conf = sw.ServerConf("u", "h", "22", "")
            b = sw.build_chat({"kind": "chat", "project": "p"}, conf, store, projects)
            self.assertRegex(b["relay"], r"^[0-9a-f]{16}$")
            self.assertFalse(b["relay_restored"])
            argv = b["make_argv"]("default", "", "", "medium", relay=("start", b["relay"], 0))
            self.assertIn("SW_RELAY=", argv[-1])
            self.assertFalse(any("ControlMaster" in a for a in argv))       # own connection
            b2 = sw.build_chat({"kind": "chat", "project": "p", "relay": b["relay"]}, conf, store, projects)
            self.assertEqual((b2["relay"], b2["relay_restored"]), (b["relay"], True))
            b3 = sw.build_chat({"kind": "chat", "project": "p", "relay": "../../etc"}, conf, store, projects)
            self.assertNotEqual(b3["relay"], "../../etc")


if __name__ == "__main__":
    unittest.main()
