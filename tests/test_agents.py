"""Other AI agents (opencode, …) in the chat panel: gui/acp_bridge.py between stream-json and ACP."""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "gui" / "acp_bridge.py"

# a tiny ACP agent: answers "hi", runs one tool that needs permission, has models + a plan mode
FAKE = r'''
import json, sys
def send(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
def upd(u): send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "ses_1", "update": u}})
state = {"model": "a/one", "mode": "build"}
for line in sys.stdin:
    m = json.loads(line)
    meth, mid, p = m.get("method"), m.get("id"), m.get("params") or {}
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True},
              "agentInfo": {"name": "fake", "title": "Fake", "version": "9"}}})
    elif meth in ("session/new", "session/load"):
        if meth == "session/load":
            upd({"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "old "}})
            upd({"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "question"}})
            upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "old answer"}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "ses_1",
              "models": {"currentModelId": "a/one", "availableModels": [{"modelId": "a/one", "name": "One"}, {"modelId": "b/two", "name": "Two"}]},
              "modes": {"currentModeId": "build", "availableModes": [{"id": "build", "name": "Build"}, {"id": "plan", "name": "Plan"}]}}})
    elif meth == "session/set_model":
        state["model"] = p["modelId"]; send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif meth == "session/set_mode":
        state["mode"] = p["modeId"]; send({"jsonrpc": "2.0", "id": mid, "result": {}})
        upd({"sessionUpdate": "current_mode_update", "currentModeId": p["modeId"]})
    elif meth == "session/prompt":
        text = p["prompt"][0]["text"]
        if text == "who":
            upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": state["model"] + " " + state["mode"]}})
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}}); continue
        upd({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}})
        upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "h"}})
        upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "i"}})
        upd({"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "ls", "kind": "execute", "status": "pending"})
        upd({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "rawInput": {"command": "ls -la"}})
        send({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission", "params": {"sessionId": "ses_1",
              "toolCall": {"toolCallId": "t1"}, "options": [{"optionId": "y", "kind": "allow_once", "name": "Yes"},
              {"optionId": "yy", "kind": "allow_always", "name": "Always"}, {"optionId": "n", "kind": "reject_once", "name": "No"}]}})
        ans = json.loads(sys.stdin.readline())
        ok = ans["result"]["outcome"].get("optionId") in ("y", "yy")
        upd({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed" if ok else "failed",
             "content": [{"type": "content", "content": {"type": "text", "text": "a.txt" if ok else "denied:" + ans["result"]["outcome"].get("optionId", "")}}]})
        upd({"sessionUpdate": "plan", "entries": [{"content": "step", "status": "completed", "priority": "high"}]})
        upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": " done"}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
'''


class Bridge(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.fake = os.path.join(self.dir, "fake_agent.py")
        Path(self.fake).write_text(FAKE)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
            for f in (p.stdin, p.stdout, p.stderr):
                f.close()

    def start(self, *opts):
        p = subprocess.Popen([sys.executable, "-I", str(BRIDGE), "--cwd", self.dir, "--name", "fake", *opts, "--", sys.executable, "-I", self.fake],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.procs.append(p)
        q = queue.Queue()

        def rd():
            for line in p.stdout:
                q.put(json.loads(line))
        threading.Thread(target=rd, daemon=True).start()
        return p, q

    def put(self, p, obj):
        p.stdin.write((json.dumps(obj) + "\n").encode())
        p.stdin.flush()

    def until(self, q, fn, t=15):
        got = []
        while True:
            ev = q.get(timeout=t)
            got.append(ev)
            if fn(ev):
                return got

    def test_turn(self):
        p, q = self.start()
        self.put(p, {"type": "control_request", "request_id": "sw-1", "request": {"subtype": "initialize"}})
        evs = self.until(q, lambda e: e.get("type") == "system")
        init = next(e for e in evs if e.get("type") == "control_response")["response"]
        self.assertEqual(init["subtype"], "success")
        self.assertEqual([m["value"] for m in init["response"]["models"]], ["a/one", "b/two"])
        self.assertEqual(evs[-1]["session_id"], "ses_1")
        self.put(p, {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}})
        evs = self.until(q, lambda e: e.get("type") == "control_request")
        self.assertTrue(evs[0].get("isReplay"))
        deltas = "".join(e["event"]["delta"].get("text", "") for e in evs if e.get("type") == "stream_event" and e["event"]["type"] == "content_block_delta")
        self.assertEqual(deltas, "hi")
        texts = [b for e in evs if e.get("type") == "assistant" for b in e["message"]["content"]]
        self.assertEqual([b["type"] for b in texts], ["thinking", "text", "tool_use"])
        self.assertEqual((texts[2]["name"], texts[2]["input"]["command"]), ("Bash", "ls -la"))
        ask = evs[-1]
        self.assertEqual(ask["request"]["tool_name"], "Bash")
        self.assertTrue(ask["request"]["permission_suggestions"])
        self.put(p, {"type": "control_response", "response": {"subtype": "success", "request_id": ask["request_id"],
                                                               "response": {"behavior": "allow", "updatedInput": {}}}})
        evs = self.until(q, lambda e: e.get("type") == "result")
        res = [b for e in evs if e.get("type") == "user" for b in e["message"]["content"] if b.get("type") == "tool_result"]
        self.assertEqual((res[0]["tool_use_id"], res[0]["content"], res[0]["is_error"]), ("t1", "a.txt", False))
        todo = [b for e in evs if e.get("type") == "assistant" for b in e["message"]["content"] if b.get("type") == "tool_use"]
        self.assertEqual(todo[0]["name"], "TodoWrite")
        self.assertEqual(evs[-1]["subtype"], "success")
        self.assertEqual(evs[-1]["result"], "hi done")

    def test_deny_model_mode(self):
        p, q = self.start("--model", "b/two", "--mode", "plan")
        self.put(p, {"type": "control_request", "request_id": "sw-1", "request": {"subtype": "initialize"}})
        self.until(q, lambda e: e.get("type") == "system")
        self.put(p, {"type": "user", "message": {"role": "user", "content": "who"}})
        evs = self.until(q, lambda e: e.get("type") == "result")
        self.assertEqual(evs[-1]["result"], "b/two plan")
        self.put(p, {"type": "control_request", "request_id": "sw-2", "request": {"subtype": "set_permission_mode", "mode": "default"}})
        self.put(p, {"type": "control_request", "request_id": "sw-3", "request": {"subtype": "set_model", "model": "a/one"}})
        self.until(q, lambda e: (e.get("response") or {}).get("request_id") == "sw-3")
        self.put(p, {"type": "user", "message": {"role": "user", "content": "who"}})
        self.assertEqual(self.until(q, lambda e: e.get("type") == "result")[-1]["result"], "a/one build")
        self.put(p, {"type": "user", "message": {"role": "user", "content": "go"}})
        ask = self.until(q, lambda e: e.get("type") == "control_request")[-1]
        self.put(p, {"type": "control_response", "response": {"subtype": "success", "request_id": ask["request_id"],
                                                               "response": {"behavior": "deny", "message": "no"}}})
        evs = self.until(q, lambda e: e.get("type") == "result")
        res = [b for e in evs if e.get("type") == "user" for b in e["message"]["content"] if b.get("type") == "tool_result"]
        self.assertEqual((res[0]["content"], res[0]["is_error"]), ("denied:n", True))

    def test_auto_mode_allows_without_asking(self):
        p, q = self.start("--mode", "auto")
        self.put(p, {"type": "control_request", "request_id": "sw-1", "request": {"subtype": "initialize"}})
        self.until(q, lambda e: e.get("type") == "system")
        self.put(p, {"type": "user", "message": {"role": "user", "content": "go"}})
        evs = self.until(q, lambda e: e.get("type") == "result")
        self.assertFalse(any(e.get("type") == "control_request" for e in evs))
        self.assertEqual(evs[-1]["result"], "hi done")

    def test_resume_shows_old_conversation(self):
        p, q = self.start("--resume", "ses_1")
        self.put(p, {"type": "control_request", "request_id": "sw-1", "request": {"subtype": "initialize"}})
        evs = self.until(q, lambda e: e.get("type") == "system")
        users = [e["message"]["content"][0]["text"] for e in evs if e.get("type") == "user"]
        self.assertEqual(users, ["old question"])
        said = [b["text"] for e in evs if e.get("type") == "assistant" for b in e["message"]["content"]]
        self.assertEqual(said, ["old answer"])
        self.assertFalse(any(e.get("type") == "stream_event" for e in evs))

    def test_probe(self):
        r = subprocess.run([sys.executable, "-I", str(BRIDGE), "--probe", "--cwd", self.dir, "--", sys.executable, "-I", self.fake],
                           capture_output=True, timeout=30)
        j = json.loads(r.stdout)
        self.assertTrue(j["ok"])
        self.assertEqual((j["agent"], j["model"], j["modes"]), ("Fake", "a/one", ["build", "plan"]))

    def test_missing_command(self):
        r = subprocess.run([sys.executable, "-I", str(BRIDGE), "--probe", "--", "no-such-agent-xyz", "acp"], capture_output=True, timeout=30)
        j = json.loads(r.stdout)
        self.assertFalse(j["ok"])
        self.assertIn("could not start", j["error"])


sys.path.insert(0, str(ROOT / "gui"))
sys.path.insert(0, str(ROOT / "tests"))
import server as sw  # noqa: E402
import test_onpc  # noqa: E402


class Link(unittest.TestCase):
    """Settings → AI on the computer; the web version's chat reaches it over the Computers link."""
    setUp = test_onpc.EndToEnd.setUp
    tearDown = test_onpc.EndToEnd.tearDown
    onpc_proc = test_onpc.EndToEnd.onpc_proc

    def add_fake(self):
        fake = os.path.join(self.tmp.name, "fake_agent.py")
        Path(fake).write_text(FAKE)
        r = self.app.agents_save({"op": "add", "name": "Fake AI", "cmd": f"{sys.executable} -I {fake}"})
        a = next(x for x in r["agents"] if x["name"] == "Fake AI")
        self.assertFalse(a["on"] if "on" in a else False)
        r = self.app.agents_save({"op": "connect", "id": a["id"]})
        self.assertTrue(r["probe"]["ok"], r["probe"])
        a = next(x for x in r["agents"] if x["id"] == a["id"])
        self.assertTrue(a["on"])
        self.assertEqual(a["model"], "a/one")
        return a

    def test_settings(self):
        a = self.add_fake()
        self.assertEqual(a["id"], "fake-ai")
        ids = [x["id"] for x in self.app.agents_list()["agents"]]
        self.assertEqual(ids[:4], ["opencode", "gemini", "codex", "qwen"])
        self.app.agents_save({"op": "model", "id": a["id"], "model": "b/two"})
        self.assertEqual(next(x for x in self.app.agents_list()["agents"] if x["id"] == a["id"])["model"], "b/two")
        self.app.agents_save({"op": "off", "id": a["id"]})
        with self.assertRaises(sw.ApiError):           # not set up → no chat
            sw.build_agent_chat({"agent": a["id"], "project": "proj"}, self.app)
        self.app.agents_save({"op": "remove", "id": a["id"]})
        self.assertNotIn(a["id"], [x["id"] for x in self.app.agents_list()["agents"]])

    def test_app_chat_runs_in_the_local_copy(self):
        a = self.add_fake()
        b = sw.build_agent_chat({"agent": a["id"], "project": "proj"}, self.app)
        self.assertEqual(b["title"], "Fake AI · proj")
        argv = b["make_argv"]("plan", "b/two", "ses_1", "")
        self.assertEqual(argv[argv.index("--cwd") + 1], self.local)
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--") + 1:], [sys.executable, "-I", os.path.join(self.tmp.name, "fake_agent.py")])
        with self.assertRaises(sw.ApiError):
            sw.build_agent_chat({"agent": a["id"], "project": "proj", "resume": "bad id!"}, self.app)

    def test_chat_session_with_an_agent(self):
        a = self.add_fake()
        info = self.app._agent_chat("agentchat1", {"agent": a["id"], "project": "proj", "kind": "chat"})
        c = self.app.sessions["agentchat1"]
        try:
            self.assertEqual((info["title"], info["spec"]["agent"], info["spec"]["agentName"]), ("Fake AI · proj", a["id"], "Fake AI"))
            self.assertTrue(test_onpc.wait_for(lambda: c.session_id == "ses_1"), "the agent's own conversation id is kept")
            self.assertEqual(c.spec["resume"], "ses_1")
            self.assertEqual([m["value"] for m in c.init["models"]], ["a/one", "b/two"])
            c.handle({"t": "model", "model": "b/two"})
            self.assertEqual(c.model, "b/two")
            self.assertEqual(next(x for x in self.app.agents_list()["agents"] if x["id"] == a["id"])["model"], "b/two")
            c.handle({"t": "send", "content": "who"})
            self.assertTrue(test_onpc.wait_for(lambda: any(e.get("type") == "result" for e in c.events)))
            self.assertEqual([e for e in c.events if e.get("type") == "result"][-1]["result"], "b/two build")
        finally:
            c.kill(wait=True)

    def test_web_chat_through_the_link(self):
        a = self.add_fake()
        listed = json.loads(subprocess.run([sys.executable, "-c", sw.PC_BRIDGE_PY, "agents"], env=dict(os.environ, HOME=self.home),
                                           capture_output=True, timeout=20).stdout)["agents"]
        self.assertEqual([(x["id"], x["devName"]) for x in listed], [(a["id"], "laptop")])
        self.assertNotIn("cmd", listed[0])                       # the command never leaves the computer
        dev = self.hub.cfg()["id"]

        p = subprocess.Popen([sys.executable, os.path.join(self.home, "onpc"), "--agent", dev,
                              json.dumps({"project": "proj", "agent": a["id"], "ticket": "nope"})],
                             env=dict(os.environ, HOME=self.home), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = p.communicate(timeout=20)
        self.assertIn("unlock", json.loads(out)["result"])          # no MFA unlock → no agent
        u = {"id": "u1", "until": time.time() + 60, "by": "phone"}
        self.hub.unlocks["u1"] = u
        t = self.hub.ticket(u, "proj")
        p = subprocess.Popen([sys.executable, os.path.join(self.home, "onpc"), "--agent", dev,
                              json.dumps({"project": "proj", "agent": a["id"], "ticket": t})],
                             env=dict(os.environ, HOME=self.home), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            p.stdin.write(b'{"type": "control_request", "request_id": "sw-1", "request": {"subtype": "initialize"}}\n')
            p.stdin.flush()
            evs = []
            while not evs or evs[-1].get("type") != "system":
                evs.append(json.loads(p.stdout.readline()))
            self.assertEqual(evs[-1]["cwd"], self.local)
            p.stdin.write(b'{"type": "user", "message": {"role": "user", "content": "who"}}\n')
            p.stdin.flush()
            while evs[-1].get("type") != "result":
                evs.append(json.loads(p.stdout.readline()))
            self.assertEqual(evs[-1]["result"], "a/one build")
        finally:
            p.kill()
            p.communicate(timeout=10)
        self.assertIsNone(self.hub.take_ticket(t, "proj"))           # one use only


if __name__ == "__main__":
    unittest.main()
