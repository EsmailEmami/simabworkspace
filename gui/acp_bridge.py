"""Other AI agents in the chat panel: opencode, Gemini CLI, Codex, Qwen Code, any ACP agent.

Stdlib only. Runs on the user's computer (never on the server):
    python3 acp_bridge.py [--cwd DIR] [--resume ID] [--model M] [--mode MODE] [--name NAME] -- CMD...
    python3 acp_bridge.py --probe [--cwd DIR] -- CMD...      → one JSON line: works? models, modes, sign-in

The chat panel speaks Claude Code's stream-json (stdin / stdout, one JSON per line); the agent speaks the
Agent Client Protocol (JSON-RPC 2.0 over its stdin / stdout). This file translates both ways, so the
panel shows the agent like Claude: live text, thinking, tool cards, permission questions, the result line.
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid

ACP_VERSION = 1
SIGN_IN = "-32000"


def out(obj):
    """One event to the chat panel."""
    data = json.dumps(obj, ensure_ascii=False) + "\n"
    with OUT_LOCK:
        sys.stdout.write(data)
        sys.stdout.flush()


OUT_LOCK = threading.Lock()


def resolve(cmd):
    """opencode → its full path (on Windows opencode.cmd); unknown → as is (Popen says why)."""
    exe = shutil.which(cmd[0]) or cmd[0]
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", exe] + cmd[1:]
    return [exe] + cmd[1:]


def text_of(content):
    """ACP content (one block or a list of tool call contents) → plain text."""
    if isinstance(content, dict):
        if content.get("type") == "text":
            return content.get("text") or ""
        if content.get("type") == "content":
            return text_of(content.get("content"))
        if content.get("type") == "diff":
            return diff_text(content)
        if content.get("type") == "resource":
            return ((content.get("resource") or {}).get("text")) or ""
        if content.get("type") == "resource_link":
            return content.get("uri") or ""
        return ""
    if isinstance(content, list):
        return "\n".join(t for t in (text_of(c) for c in content) if t)
    return content if isinstance(content, str) else ""


def diff_text(d):
    old = (d.get("oldText") or "").splitlines()
    new = (d.get("newText") or "").splitlines()
    import difflib
    return "\n".join(difflib.unified_diff(old, new, d.get("path") or "", d.get("path") or "", lineterm="", n=2))


def claude_tool(t):
    """An ACP tool call → (Claude tool name, input) so the panel draws its usual card."""
    kind = t.get("kind") or "other"
    raw = t.get("rawInput") if isinstance(t.get("rawInput"), dict) else {}
    title = t.get("title") or kind
    locs = [x.get("path") for x in t.get("locations") or [] if isinstance(x, dict) and x.get("path")]
    path = raw.get("filePath") or raw.get("file_path") or raw.get("path") or (locs[0] if locs else "")
    diffs = [c for c in t.get("content") or [] if isinstance(c, dict) and c.get("type") == "diff"]
    if kind == "execute":
        return "Bash", {"command": raw.get("command") or raw.get("cmd") or title, "description": raw.get("description") or ""}
    if kind == "edit":
        if diffs:
            d = diffs[0]
            if d.get("oldText") in (None, ""):
                return "Write", {"file_path": d.get("path") or path, "content": d.get("newText") or ""}
            return "Edit", {"file_path": d.get("path") or path, "old_string": d.get("oldText") or "", "new_string": d.get("newText") or ""}
        if "content" in raw and "oldString" not in raw:
            return "Write", {"file_path": path, "content": raw.get("content") or ""}
        return "Edit", {"file_path": path, "old_string": raw.get("oldString") or raw.get("old_string") or "",
                        "new_string": raw.get("newString") or raw.get("new_string") or ""}
    if kind == "read":
        return "Read", {"file_path": path or title}
    if kind == "search":
        return "Grep", {"pattern": raw.get("pattern") or raw.get("query") or title, "path": raw.get("path") or ""}
    if kind == "fetch":
        return "WebFetch", {"url": raw.get("url") or title}
    if kind == "think":
        return "Think", {"description": title}
    return title[:60], dict(raw, description=title)


class Bridge:
    def __init__(self, cmd, cwd, name="agent", resume="", model="", mode="default"):
        self.cmd, self.cwd, self.name = cmd, cwd, name
        self.resume, self.want_model, self.mode = resume, model, mode
        self.proc = None
        self.next_id = 0
        self.waiting = {}          # our request id -> [Event, result, error]
        self.wlock = threading.Lock()
        self.sid = ""
        self.ready = threading.Event()
        self.ready_error = ""
        self.init = {}              # the agent's initialize answer
        self.models, self.model, self.model_opt = [], "", ""
        self.modes, self.agent_mode = [], ""
        self.commands = []
        self.prompts = queue.Queue()
        self.asks = {}              # our request id (to the panel) -> (agent's JSON-RPC id, options)
        # the turn now being shown
        self.msg_id, self.block, self.block_type, self.block_text = "", -1, "", ""
        self.turn_text = []
        self.tools = {}             # toolCallId -> {"t": merged call, "shown": bool, "done": bool}
        self.loading = False        # session/load replays the old conversation

    # the agent --------------------------------------------------------------
    def start(self):
        self.proc = subprocess.Popen(resolve(self.cmd), cwd=self.cwd or None, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=sys.stderr, env=dict(os.environ))
        threading.Thread(target=self._read_agent, daemon=True).start()

    def send(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self.wlock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def call(self, method, params, timeout=None):
        with self.wlock:
            self.next_id += 1
            rid = self.next_id
        w = [threading.Event(), None, None]
        self.waiting[rid] = w
        if not self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}):
            raise RuntimeError(self.name + " is not running")
        if not w[0].wait(timeout):
            self.waiting.pop(rid, None)
            raise RuntimeError(f"{self.name} did not answer {method}")
        if w[2] is not None:
            e = w[2]
            raise AgentError(e.get("code"), str(e.get("message") or e)[:500], e.get("data"))
        return w[1] if isinstance(w[1], dict) else {}

    def notify(self, method, params):
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def _read_agent(self):
        for line in self.proc.stdout:
            try:
                m = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            if "method" in m and "id" in m:
                self._agent_request(m)
            elif "method" in m:
                if m["method"] == "session/update":
                    try:
                        self.on_update((m.get("params") or {}).get("update") or {})
                    except Exception as e:      # one odd update must not end the chat
                        sys.stderr.write(f"acp_bridge: update: {e!r}\n")
            elif "id" in m:
                w = self.waiting.pop(m["id"], None)
                if w:
                    w[1], w[2] = m.get("result"), m.get("error")
                    w[0].set()
        for w in list(self.waiting.values()):
            w[2] = {"message": self.name + " stopped"}
            w[0].set()
        code = self.proc.wait()
        os._exit(code if code else 0)

    def _agent_request(self, m):
        p = m.get("params") or {}
        if m["method"] == "session/request_permission":
            self.ask(m["id"], p)
            return
        self.send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "not supported by this client"}})

    # setup ------------------------------------------------------------------
    def setup(self):
        try:
            self.init = self.call("initialize", {"protocolVersion": ACP_VERSION, "clientInfo": {"name": "simabworkspace", "version": "1"},
                                                 "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}},
                                  timeout=60)
            caps = self.init.get("agentCapabilities") or {}
            p = {"cwd": self.cwd or os.getcwd(), "mcpServers": []}
            r = None
            if self.resume and caps.get("loadSession"):
                self.loading = True
                try:
                    r = self.call("session/load", dict(p, sessionId=self.resume), timeout=120)
                    self.sid = self.resume
                except AgentError as e:
                    sys.stderr.write(f"acp_bridge: could not continue {self.resume}: {e}\n")
                    r = None
                finally:
                    self.finish_tools()
                    self.close_block()
                    self.loading = False
            if r is None:
                r = self.call("session/new", p, timeout=120)
                self.sid = r.get("sessionId") or ""
            self._session_info(r)
            if self.want_model and self.want_model != self.model:
                self.set_model(self.want_model)
            self.set_mode(self.mode)
        except AgentError as e:
            self.ready_error = sign_in_hint(self, e)
        except RuntimeError as e:
            self.ready_error = str(e)
        self.ready.set()

    def _session_info(self, r):
        ms = r.get("models") if isinstance(r.get("models"), dict) else {}
        self.models = [{"value": x.get("modelId"), "displayName": x.get("name") or x.get("modelId"),
                        "description": x.get("description") or ""} for x in ms.get("availableModels") or [] if x.get("modelId")]
        self.model = ms.get("currentModelId") or ""
        for o in r.get("configOptions") or []:          # newer agents: models / modes as config options
            if not isinstance(o, dict) or (o.get("category") or o.get("id")) != "model":
                continue
            self.model_opt = o.get("id") or "model"
            opts = []
            for x in o.get("options") or []:
                opts += x.get("options") if isinstance(x.get("options"), list) else [x]      # grouped or flat
            self.models = [{"value": x.get("value"), "displayName": x.get("name") or x.get("value"),
                            "description": x.get("description") or ""} for x in opts if x.get("value")]
            self.model = o.get("currentValue") or self.model
        md = r.get("modes") if isinstance(r.get("modes"), dict) else {}
        self.modes = [x for x in md.get("availableModes") or [] if isinstance(x, dict) and x.get("id")]
        self.agent_mode = md.get("currentModeId") or ""

    def set_model(self, model):
        if not self.sid or not model:
            return
        try:
            if self.model_opt:
                self.call("session/set_config_option", {"sessionId": self.sid, "configId": self.model_opt, "value": model}, timeout=30)
            else:
                self.call("session/set_model", {"sessionId": self.sid, "modelId": model}, timeout=30)
            self.model = model
        except (AgentError, RuntimeError) as e:
            out({"type": "_bridge_note", "text": f"model {model}: {e}"})

    def set_mode(self, mode):
        """Our modes → the agent's: plan → its plan mode (if any), the rest → its normal (first other) mode.
        acceptEdits / auto are also answered here (see ask)."""
        self.mode = mode
        if not self.sid or not self.modes:
            return
        ids = [x["id"] for x in self.modes]
        want = next((i for i in ids if i.lower() == "plan"), "") if mode == "plan" else ""
        want = want or next((i for i in ids if i.lower() != "plan"), ids[0])
        if want != self.agent_mode:
            try:
                self.call("session/set_mode", {"sessionId": self.sid, "modeId": want}, timeout=30)
                self.agent_mode = want
            except (AgentError, RuntimeError):
                pass

    def init_answer(self):
        info = self.init.get("agentInfo") or {}
        return {"commands": self.commands, "models": self.models, "agent": {"name": self.name, "title": info.get("title") or info.get("name") or self.name,
                                                                             "version": info.get("version") or ""},
                "modes": [x["id"] for x in self.modes], "account": {}, "output_style": "default"}

    # the panel → the agent ----------------------------------------------------
    def panel_loop(self):
        threading.Thread(target=self._prompt_loop, daemon=True).start()
        for line in sys.stdin:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if isinstance(m, dict):
                try:
                    self.from_panel(m)
                except Exception as e:
                    sys.stderr.write(f"acp_bridge: {e!r}\n")
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        time.sleep(1)
        self.proc.terminate()

    def reply(self, rid, resp=None, error=""):
        if error:
            out({"type": "control_response", "response": {"subtype": "error", "request_id": rid, "error": error}})
        else:
            out({"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": resp or {}}})

    def from_panel(self, m):
        t = m.get("type")
        if t == "control_request":
            rid, req = m.get("request_id"), m.get("request") or {}
            st = req.get("subtype")
            if st == "initialize":
                def later():
                    self.ready.wait()
                    if self.ready_error:
                        self.reply(rid, error=self.ready_error)
                        return
                    self.reply(rid, self.init_answer())
                    out({"type": "system", "subtype": "init", "session_id": self.sid, "model": self.model,
                         "permissionMode": self.mode, "cwd": self.cwd, "tools": [], "slash_commands": [c["name"] for c in self.commands],
                         "agent": self.name})
                threading.Thread(target=later, daemon=True).start()
            elif st == "set_permission_mode":
                self.ready.wait()
                self.set_mode(req.get("mode") or "default")
                self.reply(rid)
            elif st == "set_model":
                self.ready.wait()
                self.set_model(req.get("model") or "")
                self.reply(rid)
            elif st == "interrupt":
                if self.sid:
                    self.notify("session/cancel", {"sessionId": self.sid})
                for k in list(self.asks):                 # questions left open: the agent waits on them
                    self.answer_ask(k, None)
                self.reply(rid)
            elif st == "generate_session_title":
                title = " ".join(str(req.get("description") or "").split())[:60]
                self.reply(rid, {"title": title} if title else {})
            else:                                          # effort, rename, …: nothing to do for an ACP agent
                self.reply(rid)
        elif t == "control_response":
            resp = m.get("response") or {}
            self.answer_ask(resp.get("request_id"), resp.get("response") or {})
        elif t == "user":
            content = (m.get("message") or {}).get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            out({"type": "user", "message": {"role": "user", "content": content}, "session_id": self.sid, "isReplay": True})
            self.prompts.put(content or [])

    def _prompt_loop(self):
        while True:
            content = self.prompts.get()
            self.ready.wait()
            started = time.time()
            if self.ready_error:
                self.result(started, "error", self.ready_error)
                continue
            blocks = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    blocks.append({"type": "text", "text": b.get("text") or ""})
                elif b.get("type") == "image" and (b.get("source") or {}).get("type") == "base64":
                    s = b["source"]
                    blocks.append({"type": "image", "mimeType": s.get("media_type") or "image/png", "data": s.get("data") or ""})
            self.turn_text = []
            try:
                r = self.call("session/prompt", {"sessionId": self.sid, "prompt": blocks})
                stop = r.get("stopReason") or "end_turn"
                self.finish_tools()
                self.close_block()
                if stop == "end_turn":
                    self.result(started, "success", "".join(self.turn_text))
                elif stop == "cancelled":
                    self.result(started, "error_during_execution", "Stopped.")
                else:
                    self.result(started, "error_during_execution", f"{self.name} stopped: {stop}")
            except (AgentError, RuntimeError) as e:
                self.finish_tools()
                self.close_block()
                self.result(started, "error_during_execution", sign_in_hint(self, e) if isinstance(e, AgentError) else str(e))

    def result(self, started, subtype, text):
        out({"type": "result", "subtype": subtype, "is_error": subtype != "success", "result": text,
             "duration_ms": int((time.time() - started) * 1000), "num_turns": 1, "session_id": self.sid})

    # permission questions -------------------------------------------------------
    def ask(self, jid, p):
        call = p.get("toolCall") or {}
        tid = call.get("toolCallId") or ""
        x = self.tools.get(tid)
        if x:
            x["t"].update({k: v for k, v in call.items() if v not in (None, "", [], {})})
            call = x["t"]
        opts = [o for o in p.get("options") or [] if isinstance(o, dict) and o.get("optionId")]
        kind = call.get("kind") or ""
        auto = self.mode == "auto" or (self.mode == "acceptEdits" and kind == "edit")
        if auto:
            o = pick(opts, ("allow_once", "allow_always"))
            if o:
                self.send({"jsonrpc": "2.0", "id": jid, "result": {"outcome": {"outcome": "selected", "optionId": o["optionId"]}}})
                return
        if x and not x["shown"]:
            self.show_tool(tid)
        name, inp = claude_tool(call)
        rid = "acp-" + uuid.uuid4().hex[:12]
        self.asks[rid] = (jid, opts)
        req = {"subtype": "can_use_tool", "tool_name": name, "input": inp, "tool_use_id": tid}
        if pick(opts, ("allow_always",)):
            req["permission_suggestions"] = [{"type": "acp", "kind": "allow_always"}]
        out({"type": "control_request", "request_id": rid, "request": req})

    def answer_ask(self, rid, resp):
        a = self.asks.pop(rid, None) if isinstance(rid, str) else None
        if not a:
            return
        jid, opts = a
        if resp is None:
            self.send({"jsonrpc": "2.0", "id": jid, "result": {"outcome": {"outcome": "cancelled"}}})
            out({"type": "control_cancel_request", "request_id": rid})
            return
        if resp.get("behavior") == "allow":
            o = pick(opts, ("allow_always", "allow_once") if resp.get("updatedPermissions") else ("allow_once", "allow_always"))
        else:
            o = pick(opts, ("reject_once", "reject_always"))
        if o:
            self.send({"jsonrpc": "2.0", "id": jid, "result": {"outcome": {"outcome": "selected", "optionId": o["optionId"]}}})
        else:
            self.send({"jsonrpc": "2.0", "id": jid, "result": {"outcome": {"outcome": "cancelled"}}})

    # the agent → the panel ------------------------------------------------------
    def new_message(self):
        self.msg_id = "msg_acp_" + uuid.uuid4().hex[:16]
        self.block = -1
        out({"type": "stream_event", "event": {"type": "message_start", "message": {"id": self.msg_id, "role": "assistant"}},
             "session_id": self.sid})

    def close_block(self):
        """The text / thinking typed so far becomes a finished assistant message."""
        if not self.block_type:
            return
        kind, text = self.block_type, self.block_text
        self.block_type, self.block_text = "", ""
        if kind == "user":
            out({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}, "session_id": self.sid})
            return
        if not self.loading:
            out({"type": "stream_event", "event": {"type": "content_block_stop", "index": self.block}, "session_id": self.sid})
        b = {"type": "text", "text": text} if kind == "text" else {"type": "thinking", "thinking": text}
        out({"type": "assistant", "message": {"id": self.msg_id, "role": "assistant", "model": self.model, "content": [b]},
             "session_id": self.sid, "parent_tool_use_id": None})

    def chunk(self, kind, text):
        if self.block_type != kind:
            self.close_block()
            self.block_type = kind
            if kind == "user":
                self.block_text += text
                return
            if self.loading:
                self.msg_id = "msg_acp_" + uuid.uuid4().hex[:16]
            else:
                self.new_message()
            self.block += 1
            if not self.loading:
                out({"type": "stream_event", "event": {"type": "content_block_start", "index": self.block,
                                                       "content_block": {"type": kind, kind: ""}}, "session_id": self.sid})
        self.block_text += text
        if kind == "text" and not self.loading:
            self.turn_text.append(text)
        if not self.loading:
            d = {"type": "text_delta", "text": text} if kind == "text" else {"type": "thinking_delta", "thinking": text}
            out({"type": "stream_event", "event": {"type": "content_block_delta", "index": self.block, "delta": d}, "session_id": self.sid})

    def show_tool(self, tid):
        x = self.tools[tid]
        x["shown"] = True
        self.close_block()
        name, inp = claude_tool(x["t"])
        out({"type": "assistant", "message": {"id": "msg_acp_" + uuid.uuid4().hex[:16], "role": "assistant", "model": self.model,
                                              "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]},
             "session_id": self.sid, "parent_tool_use_id": None})

    def tool_done(self, tid):
        x = self.tools[tid]
        if not x["shown"]:
            self.show_tool(tid)
        x["done"] = True
        t = x["t"]
        txt = text_of(t.get("content") or [])
        if not txt and t.get("rawOutput") is not None:
            ro = t["rawOutput"]
            txt = ro.get("output") if isinstance(ro, dict) and isinstance(ro.get("output"), str) else \
                ro if isinstance(ro, str) else json.dumps(ro, ensure_ascii=False)[:20000]
        out({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                                       "content": txt or "", "is_error": t.get("status") == "failed"}]},
             "session_id": self.sid, "parent_tool_use_id": None})

    def finish_tools(self):
        for tid, x in list(self.tools.items()):
            if not x["done"]:
                x["t"].setdefault("status", "completed")
                self.tool_done(tid)
        self.tools.clear()

    def on_update(self, u):
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            self.flush_tools()
            self.chunk("text", text_of(u.get("content")))
        elif kind == "agent_thought_chunk":
            self.flush_tools()
            self.chunk("thinking", text_of(u.get("content")))
        elif kind == "user_message_chunk":          # an old conversation (session/load)
            if self.loading:
                self.flush_tools()
                self.chunk("user", text_of(u.get("content")))
        elif kind == "tool_call":
            tid = u.get("toolCallId") or uuid.uuid4().hex
            self.tools[tid] = {"t": dict(u), "shown": False, "done": False}
            if u.get("status") in ("in_progress", "completed", "failed") or self.loading:
                self.show_tool(tid)
            if u.get("status") in ("completed", "failed"):
                self.tool_done(tid)
        elif kind == "tool_call_update":
            tid = u.get("toolCallId") or ""
            x = self.tools.get(tid)
            if not x:
                x = self.tools[tid] = {"t": {"toolCallId": tid}, "shown": False, "done": False}
            x["t"].update({k: v for k, v in u.items() if k != "sessionUpdate" and v is not None})
            st = x["t"].get("status")
            if st in ("in_progress", "completed", "failed") and not x["shown"]:
                self.show_tool(tid)
            if st in ("completed", "failed") and not x["done"]:
                self.tool_done(tid)
        elif kind == "plan":
            self.close_block()
            todos = [{"content": e.get("content") or "", "activeForm": e.get("content") or "",
                      "status": {"in_progress": "in_progress", "completed": "completed"}.get(e.get("status"), "pending")}
                     for e in u.get("entries") or [] if isinstance(e, dict)]
            tid = "plan_" + uuid.uuid4().hex[:10]
            out({"type": "assistant", "message": {"id": "msg_acp_" + uuid.uuid4().hex[:16], "role": "assistant", "model": self.model,
                                                  "content": [{"type": "tool_use", "id": tid, "name": "TodoWrite", "input": {"todos": todos}}]},
                 "session_id": self.sid, "parent_tool_use_id": None})
            out({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": ""}]},
                 "session_id": self.sid, "parent_tool_use_id": None})
        elif kind == "available_commands_update":
            self.commands = [{"name": c.get("name"), "description": c.get("description") or ""}
                             for c in u.get("availableCommands") or [] if isinstance(c, dict) and c.get("name")]
        elif kind == "current_mode_update":
            self.agent_mode = u.get("currentModeId") or self.agent_mode

    def flush_tools(self):
        """Text after a tool call: a card not drawn yet is drawn now (its order stays right)."""
        for tid, x in list(self.tools.items()):
            if not x["shown"]:
                self.show_tool(tid)


class AgentError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.data = code, data


def pick(opts, kinds):
    for k in kinds:
        o = next((o for o in opts if o.get("kind") == k), None)
        if o:
            return o
    return None


def sign_in_hint(b, e):
    msg = str(e)
    if str(e.code) == SIGN_IN or "auth" in msg.lower():
        how = ", ".join(m.get("name") or m.get("id") or "" for m in b.init.get("authMethods") or [] if isinstance(m, dict))
        return (f"{b.name} needs you to sign in on this computer: open a terminal and run {b.cmd[0]} "
                f"(e.g. `{b.cmd[0]} auth login`){' — ' + how if how else ''}. ({msg})")
    return f"{b.name}: {msg}"


def probe(b):
    """--probe: does the agent start and open a session? Its models / modes, for Settings."""
    def stop_later():
        time.sleep(150)
        os._exit(3)
    threading.Thread(target=stop_later, daemon=True).start()
    try:
        b.start()
    except OSError as e:
        print(json.dumps({"ok": False, "error": f"{b.cmd[0]} could not start: {e}"}))
        return 2
    b.setup()
    info = b.init.get("agentInfo") or {}
    r = {"ok": not b.ready_error, "error": b.ready_error, "models": b.models, "model": b.model,
         "modes": [x["id"] for x in b.modes], "agent": info.get("title") or info.get("name") or "", "version": info.get("version") or ""}
    print(json.dumps(r, ensure_ascii=False))
    sys.stdout.flush()
    os._exit(0)


def parse(argv):
    if "--" not in argv:
        raise SystemExit("usage: acp_bridge.py [options] -- CMD...")
    i = argv.index("--")
    opts, cmd = argv[:i], argv[i + 1:]
    if not cmd:
        raise SystemExit("acp_bridge: no agent command")
    o = {"cwd": "", "resume": "", "model": "", "mode": "default", "name": cmd[0], "probe": False}
    k = 0
    while k < len(opts):
        a = opts[k]
        if a == "--probe":
            o["probe"] = True
        elif a.startswith("--") and a[2:] in o and k + 1 < len(opts):
            o[a[2:]] = opts[k + 1]
            k += 1
        k += 1
    return o, cmd


def main(argv):
    o, cmd = parse(argv)
    b = Bridge(cmd, o["cwd"], o["name"], o["resume"], o["model"], o["mode"])
    if o["probe"]:
        return probe(b)
    try:
        b.start()
    except OSError as e:
        sys.stderr.write(f"acp_bridge: {cmd[0]} could not start: {e}\n")
        out({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": f"{cmd[0]} could not start on this computer: {e}"})
        return 127
    threading.Thread(target=b.setup, daemon=True).start()
    b.panel_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
