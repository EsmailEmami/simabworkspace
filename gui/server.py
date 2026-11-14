#!/usr/bin/env python3
# =============================================================================
#  simabworkspace GUI — a local web app (opened as its own window) with tabbed
#  terminals for your projects: shells on this machine and on the server,
#  Claude Code (local or on the server), "open in VS Code", per-project run
#  buttons, and the Mutagen sync status of every project.
#
#  Python 3.8+ standard library only. Listens on 127.0.0.1, every API call and
#  websocket needs a random token, so other users / web pages cannot use it.
#
#  Usage (normally through `simabworkspace gui`):
#    server.py [open]     start in the background if needed, then open the window
#    server.py serve      run in the foreground (what `open` starts)
#    server.py url        print the address (with token)
#    server.py status     is it running?
#    server.py stop       close all terminals and stop the GUI server
# =============================================================================

import base64
import codecs
import collections
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = "simabworkspace"
VERSION = "1.19.1"
HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
BUFFER_LIMIT = 1_500_000          # characters of output kept per tab (for reloads)
GUI_MARK_BEGIN = "# >>> simabworkspace-gui >>>"
GUI_MARK_END = "# <<< simabworkspace-gui <<<"
VSCODE_HOST_ALIAS = "simabworkspace-server"


# ----------------------------------------------------------------------------- paths
def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / APP


def bin_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".local" / "bin"


def tool_path() -> str:
    """PATH with ~/.local/bin (where the script installs mutagen) in front."""
    p = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    b = str(bin_dir())
    return p if b in p.split(os.pathsep) else b + os.pathsep + p


def which(name: str):
    return shutil.which(name, path=tool_path())


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


# ----------------------------------------------------------------------------- server settings (bash config)
def parse_bash_config(text: str) -> dict:
    """Read KEY=value lines written by the bash script (`printf %q` quoting)."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            continue
        try:
            parts = shlex.split(val)
            val = parts[0] if parts else ""
        except ValueError:
            pass
        out[key] = val
    return out


class ServerConf:
    def __init__(self, user="", host="", port="22", key=""):
        self.user, self.host, self.port, self.key = user, host, str(port or "22"), key

    @property
    def configured(self) -> bool:
        return bool(self.user and self.host)

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"

    def with_endpoint(self, user=None, host=None, port=None) -> "ServerConf":
        return ServerConf(user or self.user, host or self.host, port or self.port, self.key)

    def to_dict(self):
        return {"user": self.user, "host": self.host, "port": self.port,
                "key": self.key, "configured": self.configured}


def load_server_conf() -> ServerConf:
    f = config_dir() / "config"
    try:
        c = parse_bash_config(f.read_text())
    except OSError:
        c = {}
    return ServerConf(c.get("SSH_USER", ""), c.get("SSH_HOST") or os.environ.get("SIMAB_HOST", ""),
                      c.get("SSH_PORT") or os.environ.get("SIMAB_PORT", "22"), c.get("SSH_KEY", ""))


# ----------------------------------------------------------------------------- GUI store (gui.json)
DEFAULT_SETTINGS = {
    "fontSize": 14,
    "fontFamily": "",
    "claudeCmd": "claude",
    "editorCmd": "code",
    "localShell": "",            # empty = $SHELL
    "serverPersist": False,      # server shells / Claude inside tmux (survive disconnects)
    "sshMultiplex": True,        # one SSH connection shared by all server tabs (fast tabs)
    "persianView": "auto",       # auto | off — readable Persian pane next to the terminal
    "composer": "claude",        # always | claude | never — Persian input box under the terminal
    "chatMode": "default",       # Claude chat: default (ask) | acceptEdits | plan | auto
    "chatModel": "",             # empty = Claude's default
    "copyOnSelect": True,
    "bellNotify": True,
    "confirmClose": True,
    "scrollback": 10000,
    "browser": "",               # command used to open the window (empty = auto)
    "gitWatch": True,            # git badges: check every repository in the background
    "gitAutoFetch": True,        # …and `git fetch` every few minutes so "behind" is current
    "dockerWatch": True,         # check the projects' docker containers; tell me when one crashes
    "port": 0,                   # remembered so the window keeps the same address
}

SETTING_CHOICES = {
    "persianView": ("auto", "off"),
    "composer": ("always", "claude", "never"),
    "chatMode": ("default", "acceptEdits", "plan", "auto"),
}
SETTING_RANGES = {"fontSize": (9, 32), "scrollback": (1000, 100000), "port": (0, 65535)}
WHERE = ("local", "server")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def clean_str(v, limit: int, field: str, required=False) -> str:
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise ApiError(400, f"invalid value for {field}")
    v = v.strip()
    if required and not v:
        raise ApiError(400, f"{field} is required")
    if len(v) > limit:
        raise ApiError(400, f"{field} is too long")
    return v


def clean_subdir(v) -> str:
    """Folder inside the project for a run command ("" = project root)."""
    v = clean_str(v, 300, "folder").replace("\\", "/").strip("/")
    if v in ("", "."):
        return ""
    parts = [x for x in v.split("/") if x not in ("", ".")]
    if any(x == ".." for x in parts) or v.startswith("~"):
        raise ApiError(400, "the folder must be inside the project (e.g. web or services/api)")
    return "/".join(parts)


def valid_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name or ""))


class Store:
    def __init__(self, path: Path = None):
        self.path = path or (config_dir() / "gui.json")
        self.lock = threading.RLock()
        self.data = self._load()

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
            if not isinstance(d, dict):
                d = {}
        except (OSError, ValueError):
            d = {}
        s = dict(DEFAULT_SETTINGS)
        s.update({k: v for k, v in (d.get("settings") or {}).items() if k in DEFAULT_SETTINGS})
        d["settings"] = s
        d.setdefault("projects", {})
        d.setdefault("manual", [])
        d.setdefault("lastTabs", [])
        return d

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    # settings
    @property
    def settings(self) -> dict:
        return self.data["settings"]

    def update_settings(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise ApiError(400, "bad request")
        with self.lock:
            s = dict(self.settings)
            for k, v in patch.items():
                if k not in DEFAULT_SETTINGS:
                    continue
                d = DEFAULT_SETTINGS[k]
                if isinstance(d, bool):
                    if not isinstance(v, bool):
                        raise ApiError(400, f"invalid value for {k}")
                elif isinstance(d, int):
                    if isinstance(v, bool) or not isinstance(v, (int, float)):
                        raise ApiError(400, f"invalid value for {k}")
                    lo, hi = SETTING_RANGES.get(k, (0, 10 ** 9))
                    v = max(lo, min(hi, int(v)))
                else:
                    v = clean_str(v, 400, k)
                    if k in SETTING_CHOICES and v not in SETTING_CHOICES[k]:
                        raise ApiError(400, f"invalid value for {k}")
                    if k == "claudeCmd" and not v:
                        v = "claude"
                    if k == "editorCmd" and not v:
                        v = "code"
                s[k] = v
            self.data["settings"] = s
            self.save()
            return s

    # per-project run commands
    def commands(self, project: str) -> list:
        return list((self.data["projects"].get(project) or {}).get("commands") or [])

    def command(self, project: str, cid: str):
        for c in self.commands(project):
            if c.get("id") == cid:
                return c
        return None

    def set_commands(self, project: str, commands) -> list:
        if not isinstance(commands, list) or len(commands) > 50:
            raise ApiError(400, "invalid command list")
        out, seen = [], set()
        for c in commands:
            if not isinstance(c, dict):
                raise ApiError(400, "invalid command list")
            cid = c.get("id") if isinstance(c.get("id"), str) and re.fullmatch(r"[a-z0-9]{4,16}", c.get("id") or "") else secrets.token_hex(4)
            while cid in seen:
                cid = secrets.token_hex(4)
            seen.add(cid)
            where = c.get("where") or "local"
            if where not in WHERE:
                raise ApiError(400, "where must be local or server")
            url = clean_str(c.get("url"), 300, "URL")
            sub = clean_subdir(c.get("cwd"))
            if url and not re.match(r"https?://", url):
                raise ApiError(400, "the URL must start with http:// or https://")
            out.append({
                "id": cid,
                "name": clean_str(c.get("name"), 60, "name", required=True),
                "cmd": clean_str(c.get("cmd"), 4000, "command", required=True),
                "where": where,
                "url": url,
                "cwd": sub,
            })
        with self.lock:
            self.data["projects"].setdefault(project, {})["commands"] = out
            self.save()
        return out

    # last Claude mode/model chosen in a chat: per project, and as the general default
    def set_chat_prefs(self, project: str, mode=None, model=None):
        with self.lock:
            pp = self.data["projects"].setdefault(project or "", {})
            if mode in CHAT_MODES:
                pp["chatMode"] = mode
                self.data["settings"]["chatMode"] = mode
            if model is not None and re.fullmatch(r"[A-Za-z0-9.\[\]_-]{0,60}", model):
                pp["chatModel"] = model
                self.data["settings"]["chatModel"] = model
            self.save()

    # project colour (tabs of the project are marked with it); "" = automatic
    def set_color(self, project: str, color) -> str:
        color = clean_str(color, 7, "color").lower()
        if color and not re.fullmatch(r"#[0-9a-f]{6}", color):
            raise ApiError(400, "color must look like #4f8cff")
        with self.lock:
            self.data["projects"].setdefault(project, {})["color"] = color
            self.save()
        return color

    # projects added by hand (not synced, or synced outside this tool)
    def manual(self) -> list:
        return list(self.data["manual"])

    def add_manual(self, p: dict) -> dict:
        if not isinstance(p, dict):
            raise ApiError(400, "bad request")
        name = clean_str(p.get("name"), 64, "name", required=True)
        if not valid_name(name):
            raise ApiError(400, "use only letters, digits, ., - and _ in the name")
        local = clean_str(p.get("local"), 1000, "local folder")
        remote = clean_str(p.get("remote"), 1000, "server folder")
        if not local and not remote:
            raise ApiError(400, "enter a local folder, a server folder, or both")
        if local:
            local = os.path.abspath(os.path.expanduser(local))
        item = {"name": name, "local": local, "remote": remote}
        with self.lock:
            self.data["manual"] = [m for m in self.data["manual"] if m.get("name") != name] + [item]
            self.save()
        return item

    def remove_manual(self, name: str):
        with self.lock:
            self.data["manual"] = [m for m in self.data["manual"] if m.get("name") != name]
            self.save()

    def set_last_tabs(self, specs: list):
        with self.lock:
            if specs == self.data.get("lastTabs"):
                return
            self.data["lastTabs"] = specs
            self.save()


# ----------------------------------------------------------------------------- mutagen
SYNC_STATES = [
    # (status prefix, state class, Persian label)
    ("watching", "ok", "in sync"),
    ("scanning", "busy", "scanning"),
    ("waiting-for-rescan", "busy", "scanning"),
    ("reconciling", "busy", "syncing"),
    ("staging", "busy", "syncing"),
    ("transitioning", "busy", "syncing"),
    ("saving", "busy", "syncing"),
    ("connecting", "warn", "connecting"),
    ("disconnected", "err", "disconnected"),
    ("halted", "err", "halted"),
]


def sync_state(status: str, paused: bool):
    if paused:
        return "paused", "paused"
    for prefix, cls, label in SYNC_STATES:
        if (status or "").startswith(prefix):
            return cls, label
    return "unknown", status or "unknown"


def mutagen_sessions(timeout=8) -> list:
    exe = which("mutagen")
    if not exe:
        return []
    try:
        r = subprocess.run([exe, "sync", "list", "--template", "{{ json . }}"],
                           capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, PATH=tool_path()))
        d = json.loads(r.stdout or "[]")
        return d if isinstance(d, list) else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def sync_progress(session: dict):
    """Transfer progress Mutagen reports while staging files (None when idle)."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            if "expectedFiles" in o:
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(session)
    for pr in found:
        ef = int(pr.get("expectedFiles") or 0)
        if ef > 0:
            rf = int(pr.get("receivedFiles") or 0)
            es, rs = int(pr.get("expectedSize") or 0), int(pr.get("receivedSize") or 0)
            pct = int(rs * 100 / es) if es else int(rf * 100 / ef)
            return {"files": rf, "totalFiles": ef, "bytes": rs, "totalBytes": es, "percent": max(0, min(100, pct))}
    return None


def _endpoint(e):
    e = e or {}
    return {"path": e.get("path") or "", "host": e.get("host") or "",
            "user": e.get("user") or "", "port": str(e.get("port") or "")}


def merge_projects(sessions: list, manual: list, store_projects: dict) -> list:
    """Mutagen sessions + hand-added projects → one list for the sidebar."""
    out, names = [], set()
    for s in sessions:
        if not isinstance(s, dict):
            continue
        name = s.get("name") or s.get("identifier")
        if not name or name in names:
            continue
        a, b = _endpoint(s.get("alpha")), _endpoint(s.get("beta"))
        local, remote = (a, b) if not a["host"] else (b, a)
        cls, label = sync_state(s.get("status") or "", bool(s.get("paused")))
        conflicts = s.get("conflicts") or []
        vcs = str(((s.get("configuration") or {}).get("ignoreVCSMode")) or "default")
        names.add(name)
        out.append({
            "name": name,
            "source": "mutagen",
            "local": local["path"] if not local["host"] else "",
            "remote": remote["path"] if remote["host"] else "",
            "endpoint": {"user": remote["user"], "host": remote["host"], "port": remote["port"]},
            "sync": {"state": cls, "label": label, "status": s.get("status") or "",
                     "paused": bool(s.get("paused")), "error": s.get("lastError") or "",
                     "conflicts": len(conflicts) if isinstance(conflicts, list) else 0,
                     "git": vcs not in ("ignore", "1"),
                     "progress": sync_progress(s) if not (s.get("status") or "").startswith("watching") else None,
                     "cycles": int(s.get("successfulCycles") or 0)},
        })
    for m in manual:
        if m.get("name") in names:
            continue
        names.add(m.get("name"))
        out.append({"name": m.get("name"), "source": "manual", "local": m.get("local") or "",
                    "remote": m.get("remote") or "", "endpoint": {}, "sync": None})
    for p in out:
        p["localExists"] = bool(p["local"]) and os.path.isdir(p["local"])
        p["commands"] = list((store_projects.get(p["name"]) or {}).get("commands") or [])
        p["color"] = (store_projects.get(p["name"]) or {}).get("color") or ""
    out.sort(key=lambda p: p["name"].lower())
    return out


# ----------------------------------------------------------------------------- command building
def remote_path_expr(p: str) -> str:
    if not p or p == "~":
        return '"$HOME"'
    if p.startswith("~/"):
        return '"$HOME"/' + shlex.quote(p[2:])
    return shlex.quote(p)


def ssh_base(conf: ServerConf, multiplex=True, batch=False) -> list:
    a = ["ssh", "-p", str(conf.port),
         "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
         "-o", "StrictHostKeyChecking=accept-new"]
    if conf.key and os.path.isfile(os.path.expanduser(conf.key)):
        a += ["-i", os.path.expanduser(conf.key)]
    if multiplex:
        cp = os.path.join(os.path.expanduser("~"), ".ssh", "sw-%C")
        a += ["-o", "ControlMaster=auto", "-o", f"ControlPath={cp}", "-o", "ControlPersist=10m"]
    if batch:
        a += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=6"]
    return a


def tmux_name(project: str, kind: str, slot: int) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]", "-", f"sw-{project or 'home'}-{kind}")
    return base if not slot else f"{base}-{slot + 1}"


def remote_script(path: str, inner: str = None, persist: str = None) -> str:
    """Shell code run by the server's shell: cd into the project, then either a
    login shell (inner=None) or `inner` inside an interactive login shell."""
    cd = (f"cd {remote_path_expr(path)} 2>/dev/null || "
          f"printf 'simabworkspace: folder not found: %s\\n' {shlex.quote(path or '~')} >&2")
    if inner is None:
        plain = 'exec "${SHELL:-/bin/bash}" -l'
        tmux_cmd = ""
    else:
        plain = 'exec "${SHELL:-/bin/bash}" -lic ' + shlex.quote(inner)
        tmux_cmd = " " + shlex.quote('"${SHELL:-/bin/bash}" -lic ' + shlex.quote(inner))
    if persist:
        body = (f"if command -v tmux >/dev/null 2>&1; then exec tmux new-session -A -s "
                f"{shlex.quote(persist)}{tmux_cmd}; else {plain}; fi")
    else:
        body = plain
    return f"{cd}; {body}"


def local_shell(settings: dict) -> str:
    sh = (settings.get("localShell") or os.environ.get("SHELL") or "").strip()
    return sh if sh and which(shlex.split(sh)[0]) else (which("bash") or "/bin/sh")


CLAUDE_MODES = {"new": "", "continue": " --continue", "resume": " --resume"}

# If the `claude` CLI is not installed on the server, use the binary that ships
# inside the VS Code extension (newest version). On failure, keep the message on
# screen until Enter, so the tab does not just say "[exited]".
CLAUDE_FIND = (
    'if ! command -v claude >/dev/null 2>&1; then '
    'd=$(ls -d "$HOME"/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary '
    '"$HOME"/.vscode-server-insiders/extensions/anthropic.claude-code-*/resources/native-binary '
    '"$HOME"/.cursor-server/extensions/anthropic.claude-code-*/resources/native-binary 2>/dev/null '
    '| sort -V | tail -n 1); [ -n "$d" ] && PATH="$d:$PATH"; fi; '
)
CLAUDE_FAIL = (
    ' || { c=$?; printf "\n\033[33mclaude stopped (exit %s).\033[0m\n" "$c"; '
    'command -v claude >/dev/null 2>&1 || printf "claude was not found on this machine. Install it: '
    'curl -fsSL https://claude.ai/install.sh | bash\n"; printf "Press Enter to close. "; read -r _; exit "$c"; }'
)


def claude_inner(claude_cmd: str, mode: str, resume: str = "") -> str:
    """resume: a conversation id → open exactly that conversation."""
    flags = (" --resume " + resume) if resume else CLAUDE_MODES.get(mode, "")
    return CLAUDE_FIND + (claude_cmd or "claude") + flags + CLAUDE_FAIL
KINDS = ("shell", "claude", "run", "sync-info", "sync-monitor", "git-sync-on", "add-local", "add-server", "docker")
KIND_TITLES = {"shell": "", "claude": "Claude", "sync-info": "sync details", "sync-monitor": "sync monitor", "git-sync-on": "sync .git"}


def build_spec(spec: dict, conf: ServerConf, store: Store, projects: list) -> dict:
    """Validate a tab request and turn it into argv/cwd/title."""
    if not isinstance(spec, dict):
        raise ApiError(400, "bad request")
    kind = spec.get("kind")
    if kind not in KINDS:
        raise ApiError(400, "unknown tab type")
    pname = spec.get("project") or ""
    project = None
    if pname:
        project = next((p for p in projects if p["name"] == pname), None)
        if not project:
            raise ApiError(404, f"project {pname} not found")
    s = store.settings
    where = spec.get("where") or "local"
    cmd = None
    if kind == "run":
        if not project:
            raise ApiError(400, "run commands belong to a project")
        cmd = store.command(pname, spec.get("cmdId") or "")
        if not cmd:
            raise ApiError(404, "this command no longer exists")
        where = cmd["where"]
    if kind == "claude":
        where = "server"          # Claude Code always runs on the server
    dk = spec.get("_docker") if kind == "docker" else None
    if kind == "docker":
        where = "server"
        if not project or not isinstance(dk, dict) or not dk.get("inner"):
            raise ApiError(400, "docker tabs belong to a project")
    if kind in ("add-local", "add-server"):
        where = "local"
        if pname:
            raise ApiError(400, "bad request")
    if kind in ("sync-info", "sync-monitor", "git-sync-on"):
        where = "local"
        if not project or project.get("source") != "mutagen":
            raise ApiError(400, "this project is not synced by simabworkspace")
    if where not in WHERE:
        raise ApiError(400, "where must be local or server")

    mode = spec.get("claudeMode") or "new"
    if mode not in CLAUDE_MODES:
        mode = "new"
    inner = None
    if kind == "claude":
        resume = spec.get("resume") or ""
        if resume and not UUID_RE.fullmatch(str(resume)):
            raise ApiError(400, "invalid conversation id")
        inner = claude_inner(s.get("claudeCmd") or "claude", mode, resume)
    elif kind == "run":
        inner = cmd["cmd"]
    elif kind == "docker":
        inner = dk["inner"]

    plabel = pname or "home"
    side = "server" if where == "server" else "local"
    if kind == "shell":
        title = f"{plabel} · {side}"
    elif kind == "run":
        title = cmd["name"]
    elif kind == "add-local":
        title = "upload · " + (os.path.basename(str(spec.get("local") or "").rstrip("/")) or "project")
    elif kind == "add-server":
        title = "add project"
    elif kind == "claude":
        title = f"Claude · {plabel} · terminal"
    elif kind == "docker":
        title = dk.get("title") or "docker"
    else:
        title = f"{KIND_TITLES[kind]} · {plabel}"
    custom = spec.get("title")
    if isinstance(custom, str) and custom.strip():
        title = custom.strip()[:80]

    env_extra = {}
    if where == "server":
        if not conf.configured:
            raise ApiError(400, "the server is not configured — run simabworkspace setup once")
        ep = (project or {}).get("endpoint") or {}
        c = conf.with_endpoint(ep.get("user"), ep.get("host"), ep.get("port"))
        rpath = (project or {}).get("remote") or "~"
        if cmd and cmd.get("cwd"):
            rpath = rpath.rstrip("/") + "/" + cmd["cwd"]
        if project and not project.get("remote"):
            raise ApiError(400, "this project has no server folder")
        persist = None
        if s.get("serverPersist") and kind in ("shell", "claude"):
            slot = spec.get("slot") if isinstance(spec.get("slot"), int) else 0
            persist = tmux_name(pname, kind, max(0, min(slot, 50)))
        argv = ssh_base(c, multiplex=bool(s.get("sshMultiplex"))) + ["-t", c.target,
                                                                     remote_script(rpath, inner, persist)]
        cwd = os.path.expanduser("~")
    else:
        if kind in ("add-local", "add-server"):
            script = HERE.parent / "simabworkspace.sh"
            script = str(script) if script.is_file() else which("simabworkspace")
            if not script:
                raise ApiError(400, "simabworkspace script not found")
            argv = [which("bash") or "/bin/bash", script]
            if kind == "add-server":
                argv += ["add-server"]
            else:
                local = os.path.abspath(os.path.expanduser(clean_str(spec.get("local"), 1000, "local folder", required=True)))
                if not os.path.isdir(local):
                    raise ApiError(400, f"folder not found on this machine: {local}")
                argv += ["add-local", local, "--yes", "--git", "off" if spec.get("git") is False else "on"]
                remote = clean_str(spec.get("remote"), 1000, "server folder")
                if remote:
                    argv += ["--remote", remote]
                name = clean_str(spec.get("name"), 64, "name")
                if name:
                    if not valid_name(name):
                        raise ApiError(400, "use only letters, digits, ., - and _ in the name")
                    argv += ["--name", name]
        elif kind == "git-sync-on":
            script = HERE.parent / "simabworkspace.sh"
            script = str(script) if script.is_file() else which("simabworkspace")
            if not script:
                raise ApiError(400, "simabworkspace script not found")
            argv = [which("bash") or "/bin/bash", script, "git", pname, "on"]
        elif kind in ("sync-info", "sync-monitor"):
            exe = which("mutagen")
            if not exe:
                raise ApiError(400, "mutagen not found")
            argv = [exe, "sync", "list" if kind == "sync-info" else "monitor"]
            argv += (["--long"] if kind == "sync-info" else []) + [pname]
        else:
            sh = shlex.split(local_shell(s))
            argv = sh + (["-l"] if inner is None else ["-lic", inner])
        cwd = (project or {}).get("local") or os.path.expanduser("~")
        if cmd and cmd.get("cwd") and project:
            cwd = os.path.join(cwd, cmd["cwd"])
            if project.get("localExists") and not os.path.isdir(cwd):
                raise ApiError(400, f"folder {cmd['cwd']} does not exist in the local copy")
        if project and not project.get("localExists"):
            if kind in ("shell", "claude", "run"):
                raise ApiError(400, "this project has no folder on this machine (not synced)")
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
    return {
        "argv": argv, "cwd": cwd, "title": title, "env": env_extra,
        "spec": {"kind": kind, "project": pname, "where": where,
                 "cmdId": cmd["id"] if cmd else "", "claudeMode": mode,
                 "slot": spec.get("slot") if isinstance(spec.get("slot"), int) else 0,
                 **({"resume": spec["resume"]} if kind == "claude" and spec.get("resume") else {}),
                 **({"container": dk["container"], "action": dk["action"]} if dk else {})},
        "url": (cmd or {}).get("url") or "",
    }


# ----------------------------------------------------------------------------- run-command suggestions
SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".angular", "vendor", "tmp", "docker", ".next", ".cache"}
PREFERRED_SCRIPTS = ("start-local-server", "start", "dev", "serve", "watch", "test")


def suggestions(local: str) -> list:
    out = []
    if not local or not os.path.isdir(local):
        return out
    root = Path(local)
    pkgs = []
    for depth_glob in ("package.json", "*/package.json", "*/*/package.json"):
        for f in sorted(root.glob(depth_glob)):
            if any(part in SKIP_DIRS for part in f.relative_to(root).parts[:-1]):
                continue
            pkgs.append(f)
    for f in pkgs[:6]:
        try:
            scripts = (json.loads(f.read_text()) or {}).get("scripts") or {}
        except (OSError, ValueError):
            continue
        rel = str(f.parent.relative_to(root))
        sub = "" if rel == "." else rel
        ordered = [k for k in PREFERRED_SCRIPTS if k in scripts] + \
                  sorted(k for k in scripts if k not in PREFERRED_SCRIPTS and not k.startswith(("pre", "post")))
        for k in ordered[:8]:
            run = "npm start" if k == "start" else f"npm run {k}"
            label = k if rel == "." else f"{rel}: {k}"
            out.append({"name": label, "cmd": run, "cwd": sub, "where": "local",
                        "url": "http://localhost:4200" if k in ("start", "start-local-server", "serve") else ""})
    compose = next((n for n in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
                    if (root / n).is_file()), None)
    if compose:
        out += [
            {"name": "api logs", "cmd": "docker compose logs -f --tail=200 api_service", "where": "server", "url": ""},
            {"name": "all logs", "cmd": "docker compose logs -f --tail=100", "where": "server", "url": ""},
            {"name": "compose up", "cmd": "docker compose up -d && docker compose ps", "where": "server", "url": ""},
            {"name": "containers", "cmd": "watch -n 3 docker compose ps", "where": "server", "url": ""},
        ]
        if (root / "docker" / "local" / "compose.laptop.yml").is_file():
            out.append({"name": "web (docker)",
                        "cmd": f"docker compose -f {compose} -f docker/local/compose.laptop.yml up --no-deps web_dev",
                        "where": "local", "url": ""})
    if (root / "go.mod").is_file():
        out.append({"name": "go test", "cmd": "go test ./...", "where": "server", "url": ""})
    return out[:24]


# ----------------------------------------------------------------------------- websocket (RFC 6455, minimal)
class WebSocketClosed(Exception):
    pass


def ws_accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_encode(payload: bytes, opcode: int = 1, mask: bytes = None) -> bytes:
    n = len(payload)
    head = bytes([0x80 | opcode])
    mbit = 0x80 if mask else 0
    if n < 126:
        head += bytes([mbit | n])
    elif n < 65536:
        head += bytes([mbit | 126]) + struct.pack("!H", n)
    else:
        head += bytes([mbit | 127]) + struct.pack("!Q", n)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        head += mask
    return head + payload


def _read_exact(rfile, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = rfile.read(n - len(buf))
        if not chunk:
            raise WebSocketClosed()
        buf += chunk
    return buf


def ws_read_frame(rfile, require_mask=True, max_size=16 * 1024 * 1024):
    b0, b1 = _read_exact(rfile, 2)
    fin, opcode = bool(b0 & 0x80), b0 & 0x0F
    masked, n = bool(b1 & 0x80), b1 & 0x7F
    if n == 126:
        n = struct.unpack("!H", _read_exact(rfile, 2))[0]
    elif n == 127:
        n = struct.unpack("!Q", _read_exact(rfile, 8))[0]
    if n > max_size:
        raise WebSocketClosed()
    if require_mask and not masked:
        raise WebSocketClosed()
    mask = _read_exact(rfile, 4) if masked else None
    data = _read_exact(rfile, n) if n else b""
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return fin, opcode, data


class WebSocket:
    def __init__(self, sock, rfile):
        self.sock, self.rfile = sock, rfile
        self.wlock = threading.Lock()
        self.closed = False

    def send_text(self, text: str):
        self._send(ws_encode(text.encode("utf-8"), 1))

    def send_json(self, obj):
        self.send_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

    def _send(self, frame: bytes):
        if self.closed:
            raise WebSocketClosed()
        with self.wlock:
            try:
                self.sock.sendall(frame)
            except OSError:
                self.closed = True
                raise WebSocketClosed()

    def recv(self):
        """Next complete text/binary message (str/bytes); raises WebSocketClosed."""
        parts, first_op = [], None
        while True:
            fin, op, data = ws_read_frame(self.rfile)
            if op == 8:
                try:
                    self._send(ws_encode(data[:2], 8))
                except WebSocketClosed:
                    pass
                self.closed = True
                raise WebSocketClosed()
            if op == 9:
                self._send(ws_encode(data, 10))
                continue
            if op == 10:
                continue
            if op in (1, 2):
                first_op, parts = op, [data]
            elif op == 0 and first_op is not None:
                parts.append(data)
            else:
                raise WebSocketClosed()
            if fin:
                payload = b"".join(parts)
                return payload.decode("utf-8", "replace") if first_op == 1 else payload

    def close(self):
        if not self.closed:
            try:
                self._send(ws_encode(struct.pack("!H", 1000), 8))
            except WebSocketClosed:
                pass
        self.closed = True


# ----------------------------------------------------------------------------- terminal sessions
def set_winsize(fd: int, rows: int, cols: int):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _child_setup():
    # new session already (start_new_session); make the pty our controlling terminal
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        pass


class Session:
    def __init__(self, sid: str, built: dict, cols=120, rows=32, on_change=None):
        self.id = sid
        self.built = built
        self.title = built["title"]
        self.spec = built["spec"]
        self.url = built.get("url") or ""
        self.cols, self.rows = cols, rows
        self.lock = threading.RLock()
        self.subs = set()
        self.chunks = collections.deque()
        self.size = 0
        self.proc = None
        self.fd = None
        self.exit_code = None
        self.started = 0.0
        self.on_change = on_change
        self.runs = 0

    # lifecycle
    def start(self):
        env = dict(os.environ)
        env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "TERM_PROGRAM": APP,
                    "PATH": tool_path()})
        if not any(env.get(k, "").lower().endswith(("utf-8", "utf8")) for k in ("LC_ALL", "LC_CTYPE", "LANG")):
            env["LANG"] = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
        for k in ("VSCODE_IPC_HOOK_CLI", "VSCODE_GIT_IPC_HANDLE", "GIT_ASKPASS"):
            env.pop(k, None)
        env.update(self.built.get("env") or {})
        master, slave = os.openpty()
        set_winsize(master, self.rows, self.cols)
        try:
            proc = subprocess.Popen(self.built["argv"], cwd=self.built["cwd"], env=env,
                                    stdin=slave, stdout=slave, stderr=slave,
                                    start_new_session=True, preexec_fn=_child_setup, close_fds=True)
        except OSError as e:
            os.close(master)
            os.close(slave)
            raise ApiError(500, f"could not start: {e}")
        os.close(slave)
        with self.lock:
            self.proc, self.fd, self.exit_code = proc, master, None
            self.started = time.time()
            self.runs += 1
        threading.Thread(target=self._pump, args=(proc, master), daemon=True).start()

    def _pump(self, proc, fd):
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError as e:
                if e.errno == errno.EINTR:
                    continue
                data = b""
            if not data:
                break
            text = dec.decode(data)
            if text:
                self._emit(text)
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._signal(proc, signal.SIGKILL)
            code = proc.wait()
        try:
            os.close(fd)
        except OSError:
            pass
        with self.lock:
            if self.proc is proc:
                tail = dec.decode(b"", final=True)
                if tail:
                    self._emit(tail)
                self.exit_code = code
                self.fd = None
                self._broadcast({"t": "x", "code": code})
        if self.on_change:
            self.on_change(self)

    def _emit(self, text: str):
        with self.lock:
            self.chunks.append(text)
            self.size += len(text)
            while self.size > BUFFER_LIMIT and len(self.chunks) > 1:
                self.size -= len(self.chunks.popleft())
            self._broadcast({"t": "o", "d": text})

    def _broadcast(self, msg):
        for ws in list(self.subs):
            try:
                ws.send_json(msg)
            except WebSocketClosed:
                self.subs.discard(ws)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.exit_code is None

    def attach(self, ws: WebSocket):
        with self.lock:
            ws.send_json({"t": "s", "d": "".join(self.chunks), "alive": self.alive, "code": self.exit_code})
            self.subs.add(ws)

    def detach(self, ws: WebSocket):
        with self.lock:
            self.subs.discard(ws)

    def write(self, text: str):
        fd = self.fd
        if fd is None or not self.alive:
            return
        data = text.encode("utf-8")
        while data:
            try:
                n = os.write(fd, data)
                data = data[n:]
            except BlockingIOError:
                time.sleep(0.01)
            except OSError:
                return

    def resize(self, cols: int, rows: int):
        cols, rows = max(10, min(int(cols), 1000)), max(3, min(int(rows), 500))
        self.cols, self.rows = cols, rows
        fd = self.fd
        if fd is not None:
            try:
                set_winsize(fd, rows, cols)
            except OSError:
                pass

    @staticmethod
    def _signal(proc, sig):
        try:
            os.killpg(proc.pid, sig)
        except (OSError, ProcessLookupError):
            try:
                proc.send_signal(sig)
            except OSError:
                pass

    def stop(self):
        """Ctrl+C, then SIGTERM / SIGKILL if it does not stop."""
        proc = self.proc
        if not proc or not self.alive:
            return
        self.write("\x03")

        def later():
            for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 3)):
                time.sleep(wait)
                if proc.poll() is not None:
                    return
                self._signal(proc, sig)
        threading.Thread(target=later, daemon=True).start()

    def kill(self, wait=False):
        proc = self.proc
        if proc and proc.poll() is None:
            self._signal(proc, signal.SIGHUP)

            def later():
                time.sleep(1.5)
                if proc.poll() is None:
                    self._signal(proc, signal.SIGKILL)
            t = threading.Thread(target=later, daemon=True)
            t.start()
            if wait:
                t.join()
        with self.lock:
            self._broadcast({"t": "gone"})
            for ws in list(self.subs):
                ws.close()
            self.subs.clear()

    def restart(self):
        with self.lock:
            proc = self.proc
            self.proc, self.fd = None, None     # the old pump thread must not report its exit
        if proc and proc.poll() is None:
            self._signal(proc, signal.SIGHUP)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._signal(proc, signal.SIGKILL)
        with self.lock:
            self._emit("\r\n\x1b[2m──── restart ────\x1b[0m\r\n")
            self._broadcast({"t": "r"})
        self.start()

    def info(self) -> dict:
        return {"id": self.id, "title": self.title, "spec": self.spec, "url": self.url,
                "alive": self.alive, "exitCode": self.exit_code, "started": self.started,
                "runs": self.runs}


# ----------------------------------------------------------------------------- Claude chat (like the VS Code extension)
# Claude Code runs on the server in "stream-json" mode (the protocol the VS Code
# extension and the Agent SDK use): JSON lines in/out over SSH, permission
# questions arrive as control requests and are answered from the chat panel.
CHAT_MODES = ("default", "acceptEdits", "plan", "auto")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CHAT_EVENT_LIMIT = 4000


def claude_project_dir(remote_path: str) -> str:
    """Folder name Claude uses under ~/.claude/projects for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", remote_path)


def chat_args(mode: str, model: str = "", resume: str = "") -> list:
    a = ["--output-format", "stream-json", "--verbose", "--input-format", "stream-json",
         "--permission-prompt-tool=stdio", "--include-partial-messages", "--replay-user-messages",
         "--setting-sources=user,project,local", "--permission-mode", mode if mode in CHAT_MODES else "default"]
    if model:
        a += ["--model", model]
    if resume:
        a += ["--resume", resume]
    return a


def chat_remote_script(rpath: str, claude_cmd: str, args: list) -> str:
    inner = CLAUDE_FIND + "exec " + (claude_cmd or "claude") + " " + " ".join(shlex.quote(x) for x in args)
    return (f"cd {remote_path_expr(rpath)} || exit 97; "
            f'exec "${{SHELL:-/bin/bash}}" -lc {shlex.quote(inner)}')


# Small Python programs run on the server (python3 is there) to read Claude's
# own transcripts: list past conversations of a folder / load one for display.
REMOTE_LIST_PY = r'''
import sys, os, json, glob
d = os.path.expanduser("~/.claude/projects/" + sys.argv[1])
out = []
for f in sorted(glob.glob(d + "/*.jsonl"), key=os.path.getmtime, reverse=True)[:40]:
    title = first = last = ""
    n = 0
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"ai-title"' not in line and '"last-prompt"' not in line and '"type":"user"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                t = e.get("type")
                if t == "ai-title":
                    title = e.get("aiTitle") or title
                elif t == "last-prompt":
                    last = e.get("lastPrompt") or last
                elif t == "user" and not e.get("isSidechain") and not e.get("isMeta"):
                    c = (e.get("message") or {}).get("content")
                    if isinstance(c, list):
                        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
                    if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                        n += 1
                        first = first or c
    except OSError:
        continue
    out.append({"id": os.path.basename(f)[:-6], "mtime": os.path.getmtime(f), "title": title,
                "first": first[:300], "last": last[:300], "prompts": n, "size": os.path.getsize(f)})
print(json.dumps(out))
'''

HISTORY_PAGE = 40

# args: project dir, conversation id, [before, limit] → one page, newest last:
# {"total": all messages, "start": index of the first one returned, "entries": [...]}
REMOTE_HISTORY_PY = r'''
import sys, os, json
f = os.path.expanduser("~/.claude/projects/%s/%s.jsonl" % (sys.argv[1], sys.argv[2]))
before = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] != "" else None
limit = int(sys.argv[4]) if len(sys.argv) > 4 else 40
LIM = 8000
def cut(s):
    return s if len(s) <= LIM else s[:LIM] + "\n…"
out = []
with open(f, encoding="utf-8", errors="replace") as fh:
    for line in fh:
        if '"type":"user"' not in line and '"type":"assistant"' not in line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("type") not in ("user", "assistant") or e.get("isSidechain"):
            continue
        m = e.get("message") or {}
        c = m.get("content")
        if isinstance(c, list):
            nc = []
            for b in c:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "image":
                    nc.append({"type": "text", "text": "[image]"})
                elif t == "tool_result":
                    r = b.get("content")
                    if isinstance(r, list):
                        r = "\n".join(x.get("text", "[image]") for x in r if isinstance(x, dict))
                    nc.append({"type": "tool_result", "tool_use_id": b.get("tool_use_id"),
                               "is_error": b.get("is_error", False), "content": cut(str(r or ""))})
                elif t == "thinking":
                    nc.append({"type": "thinking", "thinking": cut(b.get("thinking") or "")})
                elif t in ("text", "tool_use"):
                    nc.append(b)
            c = nc
        elif isinstance(c, str):
            c = cut(c)
        if e.get("isMeta"):
            continue
        out.append({"type": e["type"], "message": {"id": m.get("id"), "role": m.get("role"), "content": c}})
total = len(out)
end = total if before is None else max(0, min(before, total))
start = max(0, end - limit)
# do not start a page in the middle of a tool call: move back to a user text message when close
k = start
while k > 0 and k > start - 10 and not (out[k]["type"] == "user" and isinstance(out[k]["message"]["content"], (str, list))
        and any((isinstance(b, dict) and b.get("type") == "text") for b in (out[k]["message"]["content"] if isinstance(out[k]["message"]["content"], list) else [{"type": "text"}]))):
    k -= 1
start = k if k > 0 and out[k]["type"] == "user" else start
print(json.dumps({"total": total, "start": start, "entries": out[start:end]}))
'''


def remote_python(conf: ServerConf, code: str, args: list, multiplex=True, timeout=30):
    cmd = "python3 -c " + shlex.quote(code) + " " + " ".join(shlex.quote(a) for a in args)
    try:
        with tempfile.TemporaryFile() as errf:
            r = subprocess.run(ssh_base(conf, multiplex=multiplex) + ["-T", "-o", "BatchMode=yes", conf.target, cmd],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errf, timeout=timeout)
            errf.seek(0)
            err = errf.read().decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError) as e:
        raise ApiError(502, f"cannot reach the server: {e}")
    if r.returncode != 0:
        raise ApiError(502, "server read failed: " + (err.splitlines() or ["?"])[-1][:200])
    try:
        return json.loads(r.stdout.decode("utf-8", "replace") or "null")
    except ValueError:
        raise ApiError(502, "bad answer from the server")


FILE_SKIP = {"node_modules", ".git", "dist", "build", ".angular", "vendor", "tmp", ".next", ".cache", ".dart_tool",
             "__pycache__", ".venv", ".gradle", ".nuxt", ".idea", "coverage", "docker/data"}
FILE_LIMIT = 30000


def local_files(root: str) -> list:
    """Project files for @-mentions: git's list (respects .gitignore), else a walk."""
    git = which("git")
    if git and os.path.isdir(os.path.join(root, ".git")):
        try:
            r = subprocess.run([git, "-C", root, "ls-files", "-co", "--exclude-standard"], capture_output=True,
                               timeout=20, stdin=subprocess.DEVNULL)
            if r.returncode == 0:
                return r.stdout.decode("utf-8", "replace").splitlines()[:FILE_LIMIT]
        except (OSError, subprocess.SubprocessError):
            pass
    out = []
    for d, dirs, files in os.walk(root):
        rel = os.path.relpath(d, root)
        dirs[:] = sorted(x for x in dirs if x not in FILE_SKIP and not x.startswith(".")
                         and os.path.join(rel, x).lstrip("./") not in FILE_SKIP)
        for f in sorted(files):
            out.append(f if rel == "." else os.path.join(rel, f))
            if len(out) >= FILE_LIMIT:
                return out
    return out


REMOTE_FILES_SH = ("(git ls-files -co --exclude-standard 2>/dev/null || find . -type d \\( -name node_modules "
                   "-o -name .git -o -name dist -o -name vendor -o -name tmp \\) -prune -o -type f -print "
                   "| sed 's|^\\./||') | head -n %d" % FILE_LIMIT)


def with_dirs(files: list) -> list:
    dirs = set()
    for f in files:
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]) + "/")
    return sorted(dirs) + files


class ChatSession:
    """One Claude conversation shown as a chat panel."""

    def __init__(self, sid: str, built: dict, on_change=None):
        self.id = sid
        self.built = built
        self.title = built["title"]
        self.spec = dict(built["spec"])
        self.url = ""
        self.lock = threading.RLock()
        self.subs = set()
        self.events = collections.deque(maxlen=CHAT_EVENT_LIMIT)
        self.proc = None
        self.exit_code = None
        self.started = 0.0
        self.runs = 0
        self.on_change = on_change
        self.session_id = built.get("resume") or ""
        self.mode = built.get("mode") or "default"
        self.model = built.get("model") or ""
        self.busy = False
        self.init = None              # answer to our "initialize" request
        self.pending = {}             # permission requests waiting for the user
        self.my_requests = {}         # request_id -> subtype of requests we sent
        self.wlock = threading.Lock()
        self.on_usage = None          # callback(rate_limit_info)
        self.on_cost = None           # callback(project, usd) — the cost of each answer
        self._cost_seen = 0.0
        h = built.get("history")
        if isinstance(h, list):                        # old format: a plain list
            h = {"total": len(h), "start": 0, "entries": h}
        if h and h.get("entries"):
            self.events.append({"type": "_history", "entries": h["entries"], "start": h.get("start", 0), "total": h.get("total", 0)})
        elif built.get("history_error"):
            self.events.append({"type": "_error", "text": "Earlier messages of this conversation could not be loaded ("
                                + built["history_error"] + "). You can still continue it."})

    # process
    def start(self):
        argv = self.built["make_argv"](self.mode, self.model, self.session_id)
        env = dict(os.environ, PATH=tool_path())
        try:
            proc = subprocess.Popen(argv, cwd=self.built["cwd"], env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except OSError as e:
            raise ApiError(500, f"could not start: {e}")
        with self.lock:
            self.proc, self.exit_code, self.busy = proc, None, False
            self.started = time.time()
            self.runs += 1
            self.pending.clear()
            self._cost_seen = 0.0
        threading.Thread(target=self._read_out, args=(proc,), daemon=True).start()
        threading.Thread(target=self._read_err, args=(proc,), daemon=True).start()
        self._request({"subtype": "initialize", "hooks": None})

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.exit_code is None

    def _write(self, obj, proc=None):
        proc = proc or self.proc
        if not proc or proc.poll() is not None:
            return False
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self.wlock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def _request(self, req: dict):
        rid = "sw-" + secrets.token_hex(6)
        self.my_requests[rid] = req.get("subtype")
        return self._write({"type": "control_request", "request_id": rid, "request": req})

    def _read_out(self, proc):
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                self._log({"type": "_stderr", "text": line[:2000]})
                continue
            if isinstance(ev, dict):
                self._handle_event(ev)
        code = proc.wait()
        for f in (proc.stdout, proc.stdin):
            try:
                f.close()
            except (OSError, ValueError):
                pass
        with self.lock:
            if self.proc is not proc:
                return
            self.exit_code = code
            self.busy = False
            self.pending.clear()
            self._log({"type": "_exit", "code": code})
        if self.on_change:
            self.on_change(self)

    def _read_err(self, proc):
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line and "no job control" not in line and "terminal process group" not in line:
                self._log({"type": "_stderr", "text": line[:2000]})
        try:
            proc.stderr.close()
        except (OSError, ValueError):
            pass

    def _handle_event(self, ev: dict):
        t = ev.get("type")
        sid = ev.get("session_id")
        if isinstance(sid, str) and UUID_RE.fullmatch(sid) and sid != self.session_id:
            self.session_id = sid
            self.spec["resume"] = sid
            if self.on_change:
                self.on_change(self)
        if t == "stream_event":
            self._broadcast({"t": "e", "e": ev})          # live typing only, not kept
            return
        if t == "control_response":
            resp = ev.get("response") or {}
            kind = self.my_requests.pop(resp.get("request_id"), None)
            if kind == "initialize" and resp.get("subtype") == "success":
                self.init = resp.get("response") or {}
                self._log({"type": "_init", "info": self.init})
            elif resp.get("subtype") == "error":
                self._log({"type": "_error", "text": str(resp.get("error") or "")[:500]})
            return
        if t == "control_request":
            req = ev.get("request") or {}
            if req.get("subtype") == "can_use_tool":
                with self.lock:
                    self.pending[ev.get("request_id")] = ev
            else:
                # not supported here: answer so Claude does not wait forever
                self._write({"type": "control_response", "response": {
                    "subtype": "error", "request_id": ev.get("request_id"), "error": "not supported by this client"}})
                return
        elif t == "control_cancel_request":
            with self.lock:
                self.pending.pop(ev.get("request_id"), None)
        elif t == "result":
            self.busy = False
            tc = ev.get("total_cost_usd")          # the whole process so far
            if isinstance(tc, (int, float)) and not isinstance(tc, bool):
                d = tc - self._cost_seen if tc >= self._cost_seen else tc
                self._cost_seen = tc
                if self.on_cost and d > 0:
                    self.on_cost(self.spec.get("project") or "", d)
        elif t == "rate_limit_event":
            if self.on_usage:
                self.on_usage(ev.get("rate_limit_info"))
            self._broadcast({"t": "u", "u": ev.get("rate_limit_info")})
            return
        elif t == "system" and ev.get("subtype") == "init":
            if ev.get("permissionMode") in CHAT_MODES:
                self.mode = ev["permissionMode"]
        elif t == "keep_alive":
            return
        self._log(ev)

    def _log(self, ev):
        with self.lock:
            self.events.append(ev)
            self._broadcast({"t": "e", "e": ev})

    def _broadcast(self, msg):
        with self.lock:
            for ws in list(self.subs):
                try:
                    ws.send_json(msg)
                except WebSocketClosed:
                    self.subs.discard(ws)

    def state(self) -> dict:
        return {"busy": self.busy, "alive": self.alive, "code": self.exit_code, "mode": self.mode,
                "model": self.model, "sessionId": self.session_id, "init": self.init}

    # websocket
    def attach(self, ws: WebSocket):
        with self.lock:
            ws.send_json({"t": "s", "events": list(self.events), "state": self.state()})
            self.subs.add(ws)

    def detach(self, ws: WebSocket):
        with self.lock:
            self.subs.discard(ws)

    def handle(self, m: dict):
        t = m.get("t")
        if t == "send":
            content = m.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list) or not content:
                return
            if not self.alive:
                self.start()
            self.busy = True
            self._broadcast({"t": "busy", "busy": True})
            if getattr(self, "log", None):
                txt = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text").strip()
                imgs = sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")
                self.log("prompt", self.spec.get("project"), txt or f"[{imgs} image(s)]", session=self.session_id, images=imgs or None)
            if not self._write({"type": "user", "session_id": self.session_id or "",
                                "message": {"role": "user", "content": content}, "parent_tool_use_id": None}):
                self.busy = False
                self._log({"type": "_error", "text": "message not sent — Claude is not running"})
        elif t == "perm":
            self.answer_permission(m.get("id"), m.get("allow") is True, m.get("always") is True,
                                   m.get("message") if isinstance(m.get("message"), str) else "",
                                   m.get("input") if isinstance(m.get("input"), dict) else None,
                                   m.get("permissions") if isinstance(m.get("permissions"), list) else None)
        elif t == "interrupt":
            self._request({"subtype": "interrupt"})
        elif t == "mode" and m.get("mode") in CHAT_MODES:
            self.mode = m["mode"]
            if getattr(self, "remember", None):
                self.remember(self.spec.get("project"), mode=self.mode)
            self._request({"subtype": "set_permission_mode", "mode": self.mode})
            self._log({"type": "_mode", "mode": self.mode})
        elif t == "model" and isinstance(m.get("model"), str) and re.fullmatch(r"[A-Za-z0-9.\[\]_-]{0,60}", m["model"]):
            self.model = m["model"]
            if getattr(self, "remember", None):
                self.remember(self.spec.get("project"), model=self.model)
            self._request({"subtype": "set_model", "model": self.model or "default"})
            self._log({"type": "_model", "model": self.model})

    def answer_permission(self, rid, allow: bool, always: bool, message: str, new_input=None, permissions=None):
        """allow/deny a tool. new_input: answers to AskUserQuestion (questions + answers);
        permissions: e.g. [{"type": "setMode", ...}] when a plan is approved."""
        with self.lock:
            ev = self.pending.pop(rid, None)
        if not ev:
            return
        req = ev.get("request") or {}
        if allow:
            resp = {"behavior": "allow", "updatedInput": req.get("input") or {}}
            if new_input is not None:
                resp["updatedInput"] = dict(resp["updatedInput"], **new_input)
            if always and isinstance(req.get("permission_suggestions"), list):
                resp["updatedPermissions"] = req["permission_suggestions"]
            if permissions:
                resp["updatedPermissions"] = [x for x in permissions if isinstance(x, dict)]
                for x in resp["updatedPermissions"]:
                    if x.get("type") == "setMode" and x.get("mode") in CHAT_MODES:
                        self.mode = x["mode"]
        else:
            resp = {"behavior": "deny", "message": message or "The user denied this action."}
        self._write({"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": resp}})
        done = {"type": "_perm_done", "request_id": rid, "allow": allow, "always": always}
        if new_input and isinstance(new_input.get("answers"), dict):
            done["answers"] = new_input["answers"]
        if not allow and message:
            done["message"] = message[:500]
        self._log(done)
        if permissions:
            self._log({"type": "_mode", "mode": self.mode})

    # tab actions (same names as Session)
    def _terminate(self, proc, wait=False):
        if not proc or proc.poll() is not None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass

        def later():
            for sig, delay in ((signal.SIGTERM, 1.5), (signal.SIGKILL, 2)):
                time.sleep(delay)
                if proc.poll() is not None:
                    return
                Session._signal(proc, sig)
        t = threading.Thread(target=later, daemon=True)
        t.start()
        if wait:
            t.join()

    def stop(self):
        self._request({"subtype": "interrupt"})

    def kill(self, wait=False):
        with self.lock:
            proc = self.proc
            self._broadcast({"t": "gone"})
            for ws in list(self.subs):
                ws.close()
            self.subs.clear()
        self._terminate(proc, wait)

    def restart(self, fresh=False):
        with self.lock:
            proc = self.proc
            self.proc = None
        self._terminate(proc)
        if fresh:
            with self.lock:
                self.events.clear()
                self.session_id = ""
                self.spec.pop("resume", None)
            self._broadcast({"t": "clear"})
        else:
            self._log({"type": "_restart"})
        self.start()

    def resize(self, *_):
        pass

    def write(self, *_):
        pass

    def info(self) -> dict:
        return {"id": self.id, "title": self.title, "spec": self.spec, "url": "",
                "alive": self.alive, "exitCode": self.exit_code, "started": self.started, "runs": self.runs}


def build_chat(spec: dict, conf: ServerConf, store: Store, projects: list) -> dict:
    pname = spec.get("project") or ""
    project = next((p for p in projects if p["name"] == pname), None) if pname else None
    if pname and not project:
        raise ApiError(404, f"project {pname} not found")
    if not conf.configured:
        raise ApiError(400, "the server is not configured — run simabworkspace setup once")
    if project and not project.get("remote"):
        raise ApiError(400, "this project has no server folder")
    s = store.settings
    ep = (project or {}).get("endpoint") or {}
    c = conf.with_endpoint(ep.get("user"), ep.get("host"), ep.get("port"))
    rpath = (project or {}).get("remote") or "~"
    resume = spec.get("resume") or ""
    if resume and not UUID_RE.fullmatch(resume):
        raise ApiError(400, "invalid conversation id")
    pp = store.data["projects"].get(pname or "", {})
    mode = spec.get("mode") if spec.get("mode") in CHAT_MODES else (pp.get("chatMode") or s.get("chatMode", "default"))
    model = pp["chatModel"] if "chatModel" in pp else s.get("chatModel", "")
    claude_cmd = s.get("claudeCmd") or "claude"
    multiplex = bool(s.get("sshMultiplex"))

    def make_argv(mode_, model_, resume_):
        return ssh_base(c, multiplex=multiplex) + ["-T", c.target,
                                                   chat_remote_script(rpath, claude_cmd, chat_args(mode_, model_, resume_))]

    title = spec.get("title") if isinstance(spec.get("title"), str) and spec.get("title").strip() else f"Claude · {pname or 'home'}"
    return {"make_argv": make_argv, "cwd": os.path.expanduser("~"), "title": title.strip()[:80],
            "resume": resume, "mode": mode, "model": model, "conf": c, "rpath": rpath,
            "spec": {"kind": "chat", "project": pname, "where": "server", "cmdId": "", "claudeMode": "new",
                     "slot": 0, **({"resume": resume} if resume else {})}}



# ----------------------------------------------------------------------------- git
# Git runs where the project has a folder on this machine (fast, your own git
# credentials); otherwise on the server over SSH. Before and after anything that
# changes files, the Mutagen sync is flushed so both sides agree.
CONFLICT_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"}


def parse_git_status(raw: str) -> dict:
    """`git status --porcelain=v1 -b -z` → branch info + files."""
    out = {"branch": "", "upstream": "", "ahead": 0, "behind": 0, "files": [], "detached": False}
    parts = raw.split("\0")
    i = 0
    while i < len(parts):
        e = parts[i]
        i += 1
        if not e:
            continue
        if e.startswith("## "):
            head = e[3:]
            m = re.match(r"(?:No commits yet on |Initial commit on )?(.+?)(?:\.\.\.(\S+))?(?: \[(.+)\])?$", head)
            if m:
                out["branch"], out["upstream"] = m.group(1), m.group(2) or ""
                for k, n in re.findall(r"(ahead|behind) (\d+)", m.group(3) or ""):
                    out[k] = int(n)
            out["detached"] = head.startswith("HEAD (no branch)")
            continue
        xy, path = e[:2], e[3:]
        item = {"path": path, "x": xy[0], "y": xy[1], "conflict": xy in CONFLICT_CODES, "untracked": xy == "??"}
        if xy[0] in "RC":                      # rename/copy: the old path follows
            item["from"] = parts[i] if i < len(parts) else ""
            i += 1
        out["files"].append(item)
    out["conflicts"] = sum(1 for f in out["files"] if f["conflict"])
    return out


def git_path_ok(path: str) -> bool:
    return bool(path) and not path.startswith(("/", "-")) and ".." not in path.split("/") and "\0" not in path


GIT_SKIP = {"node_modules", "vendor", "dist", "build", ".angular", "tmp", ".cache", "docker"}


def find_local_repos(root: str, depth: int = 3) -> list:
    """Relative paths of git repositories in a project ("" = the project itself)."""
    out = []
    root = os.path.abspath(root)
    for d, dirs, files in os.walk(root):
        rel = os.path.relpath(d, root)
        level = 0 if rel == "." else rel.count(os.sep) + 1
        if ".git" in dirs or ".git" in files:
            out.append("" if rel == "." else rel.replace(os.sep, "/"))
        dirs[:] = sorted(x for x in dirs if x != ".git" and x not in GIT_SKIP and not x.startswith(".")) if level < depth else []
    return out


REMOTE_FIND_REPOS = ("find . -maxdepth 4 \\( -name node_modules -o -name vendor -o -name dist -o -name docker \\) -prune "
                     "-o -name .git -print 2>/dev/null | sed -e 's|/\\.git$||' -e 's|^\\.$||' -e 's|^\\./||' | sort")


def git_repos(app, project: dict) -> dict:
    """Where git runs for this project and which repositories it has. Local copy first
    (your own git setup); the server when the local copy has no repositories."""
    local = find_local_repos(project["local"]) if project.get("localExists") else []
    if local:
        return {"where": "local", "repos": local}
    if project.get("remote") and app.conf.configured:
        conf = app.conf
        ep = project.get("endpoint") or {}
        conf = conf.with_endpoint(ep.get("user"), ep.get("host"), ep.get("port"))
        cmd = f"cd {remote_path_expr(project['remote'])} && " + REMOTE_FIND_REPOS
        try:
            r = subprocess.run(ssh_base(conf, multiplex=bool(app.store.settings.get("sshMultiplex"))) + ["-T", "-o", "BatchMode=yes", conf.target, cmd],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
            remote = [x.strip() for x in r.stdout.decode("utf-8", "replace").splitlines()]
            remote = [x for x in remote if x == "" or git_path_ok(x)]
            # the root shows up as "" once; keep order stable
            remote = sorted(set(remote), key=lambda x: (x != "", x))
            if r.returncode == 0 and (remote or r.stdout.strip()):
                return {"where": "server", "repos": remote}
        except (OSError, subprocess.SubprocessError):
            pass
    return {"where": "local" if project.get("localExists") else "server", "repos": []}


class GitRepo:
    def __init__(self, app, project: dict, repo: str = "", where: str = None):
        self.app, self.p = app, project
        if repo and not git_path_ok(repo):
            raise ApiError(400, "bad repository path")
        self.repo = repo
        self.where = where or ("local" if project.get("localExists") else "server")
        if self.where == "local" and not project.get("localExists"):
            self.where = "server"
        if self.where == "server" and not project.get("remote"):
            raise ApiError(400, "this project has no folder for git")

    def run(self, args: list, timeout=60, stdin: bytes = None):
        env = dict(os.environ, PATH=tool_path(), **GIT_ENV)
        if self.where == "local":
            git = which("git")
            if not git:
                raise ApiError(400, "git is not installed on this machine")
            argv, cwd = [git] + args, os.path.join(self.p["local"], self.repo) if self.repo else self.p["local"]
        else:
            conf = self.app.conf
            ep = self.p.get("endpoint") or {}
            conf = conf.with_endpoint(ep.get("user"), ep.get("host"), ep.get("port"))
            envs = " ".join(f"{k}={shlex.quote(v)}" for k, v in GIT_ENV.items())
            rpath = self.p["remote"].rstrip("/") + ("/" + self.repo if self.repo else "")
            cmd = f"cd {remote_path_expr(rpath)} && {envs} git " + " ".join(shlex.quote(a) for a in args)
            argv = ssh_base(conf, multiplex=bool(self.app.store.settings.get("sshMultiplex"))) + ["-T", "-o", "BatchMode=yes", conf.target, cmd]
            cwd = os.path.expanduser("~")
        try:
            r = subprocess.run(argv, cwd=cwd, env=env, input=stdin, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ApiError(504, "git took too long (" + " ".join(args[:2]) + ")")
        except OSError as e:
            raise ApiError(500, f"git did not start: {e}")
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")

    def check(self, args, timeout=60):
        code, out, err = self.run(args, timeout)
        if code != 0:
            raise ApiError(400, (err.strip() or out.strip() or "git failed")[-1500:])
        return out

    def flush(self):
        """Let Mutagen finish syncing so git sees (and leaves) the same files on both sides."""
        if self.p.get("source") != "mutagen":
            return
        exe = which("mutagen")
        if exe:
            try:
                subprocess.run([exe, "sync", "flush", self.p["name"]], capture_output=True, timeout=90,
                               env=dict(os.environ, PATH=tool_path()))
            except (OSError, subprocess.SubprocessError):
                pass

    def status(self) -> dict:
        code, out, err = self.run(["status", "--porcelain=v1", "-b", "-z", "--untracked-files=all"], timeout=30)
        if code != 0:
            if "not a git repository" in err:
                return {"repo": False, "where": self.where, "files": [], "conflicts": 0}
            raise ApiError(400, err.strip()[-500:] or "git status failed")
        st = parse_git_status(out)
        st.update(repo=True, where=self.where, path=self.repo)
        code, last, _ = self.run(["log", "-1", "--format=%h %s"], timeout=15)
        st["last"] = last.strip() if code == 0 else ""
        return st

    def diff(self, path: str, untracked: bool) -> str:
        if not git_path_ok(path):
            raise ApiError(400, "bad path")
        if untracked:
            code, out, err = self.run(["diff", "--no-color", "--no-index", "--", "/dev/null", path], timeout=30)
        else:
            code, out, err = self.run(["diff", "--no-color", "HEAD", "--", path], timeout=30)
            if code != 0 and "bad revision" in err:          # no commits yet
                code, out, err = self.run(["diff", "--no-color", "--cached", "--", path], timeout=30)
        text = out or err
        return text if len(text) < 400_000 else text[:400_000] + "\n… (cut)"

    def commit(self, message: str, files) -> str:
        message = clean_str(message, 5000, "commit message", required=True)
        self.flush()
        if files is None:
            self.check(["add", "-A"])
        else:
            if not isinstance(files, list) or not files or not all(isinstance(f, str) and git_path_ok(f) for f in files):
                raise ApiError(400, "pick at least one file")
            self.check(["add", "-A", "--"] + files)
        args = ["commit", "-m", message]
        if files is not None:
            args += ["--"] + files
        out = self.check(args)
        self.flush()
        return out

    def pull(self) -> str:
        self.flush()
        # merge like VS Code does, unless the user configured pull.rebase / pull.ff themselves
        args = ["pull", "--no-edit"]
        c1, _, _ = self.run(["config", "--get", "pull.rebase"], timeout=15)
        c2, _, _ = self.run(["config", "--get", "pull.ff"], timeout=15)
        if c1 != 0 and c2 != 0:
            args.append("--no-rebase")
        code, out, err = self.run(args, timeout=180)
        self.flush()
        if code != 0:
            st = self.status()
            if st.get("conflicts"):
                return "conflicts"
            raise ApiError(400, (err.strip() or out.strip())[-1500:])
        return (out + err).strip()

    def push(self) -> str:
        self.flush()
        code, out, err = self.run(["push"], timeout=180)
        if code != 0 and ("no upstream" in err or "has no upstream" in err):
            code, out, err = self.run(["push", "-u", "origin", "HEAD"], timeout=180)
        if code != 0:
            raise ApiError(400, (err.strip() or out.strip())[-1500:])
        return (out + err).strip()

    def fetch(self) -> str:
        return self.check(["fetch", "--prune"], timeout=120)


# ----------------------------------------------------------------------------- activity history
class Activity:
    """What you did, newest last: one JSON object per line in activity.jsonl."""
    LIMIT = 5000

    def __init__(self, path: Path = None):
        self.path = path or (config_dir() / "activity.jsonl")
        self.lock = threading.Lock()

    def add(self, kind: str, project: str = "", text: str = "", **extra):
        item = {"t": round(time.time(), 3), "kind": kind, "project": project or "", "text": (text or "")[:500]}
        item.update({k: v for k, v in extra.items() if v not in (None, "")})
        line = json.dumps(item, ensure_ascii=False) + "\n"
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
                os.chmod(self.path, 0o600)
                if self.path.stat().st_size > 2_000_000:
                    lines = self.path.read_text(encoding="utf-8").splitlines()[-self.LIMIT:]
                    self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            except OSError:
                pass
        return item

    def list(self, limit=500, project="", query="") -> list:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out, q = [], (query or "").lower()
        for line in reversed(lines):
            try:
                it = json.loads(line)
            except ValueError:
                continue
            if project and it.get("project") != project:
                continue
            if q and q not in (it.get("text", "") + " " + it.get("project", "") + " " + it.get("kind", "")).lower():
                continue
            out.append(it)
            if len(out) >= limit:
                break
        return out


# ----------------------------------------------------------------------------- docker on the server
# One SSH call lists every container whose compose working folder is inside a
# project's server folder (argv: name=path …); the longest matching folder wins.
DOCKER_PY = r'''
import sys, os, json, subprocess
roots = []
for a in sys.argv[1:]:
    name, _, path = a.partition("=")
    p = os.path.realpath(os.path.expanduser(path))
    roots.append((name, p.rstrip("/") or "/"))
roots.sort(key=lambda r: -len(r[1]))
def run(args):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=25)
try:
    r = run(["docker", "ps", "-aq", "--no-trunc"])
except FileNotFoundError:
    print(json.dumps({"error": "docker is not installed on the server", "containers": []})); sys.exit()
if r.returncode:
    print(json.dumps({"error": (r.stderr.strip().splitlines() or ["docker failed"])[-1][:300], "containers": []})); sys.exit()
ids = r.stdout.split()
data = []
if ids:
    r = run(["docker", "inspect"] + ids)
    try:
        data = json.loads(r.stdout or "[]")
    except ValueError:
        data = []
out = []
for c in data:
    cfg = c.get("Config") or {}
    lab = cfg.get("Labels") or {}
    wd = lab.get("com.docker.compose.project.working_dir") or ""
    rwd = os.path.realpath(wd) if wd else ""
    proj = ""
    for name, root in roots:
        if rwd and (rwd == root or rwd.startswith(root + "/")):
            proj = name
            break
    if not proj:
        continue
    st = c.get("State") or {}
    ports, seen = [], set()
    for key, binds in ((c.get("NetworkSettings") or {}).get("Ports") or {}).items():
        port, _, proto = key.partition("/")
        for b in binds or []:
            hp = b.get("HostPort") or ""
            if hp and (hp, proto) not in seen:
                seen.add((hp, proto))
                ports.append({"host": int(hp), "port": int(port), "proto": proto or "tcp", "ip": b.get("HostIp") or ""})
    out.append({
        "id": (c.get("Id") or "")[:12], "name": (c.get("Name") or "").lstrip("/"), "image": cfg.get("Image") or "",
        "state": st.get("Status") or "", "health": (st.get("Health") or {}).get("Status") or "",
        "exitCode": st.get("ExitCode") or 0, "startedAt": st.get("StartedAt") or "", "finishedAt": st.get("FinishedAt") or "",
        "restarts": c.get("RestartCount") or 0, "compose": lab.get("com.docker.compose.project") or "",
        "service": lab.get("com.docker.compose.service") or "", "workdir": wd,
        "configFiles": lab.get("com.docker.compose.project.config_files") or "", "ports": sorted(ports, key=lambda p: p["host"]),
        "proj": proj})
print(json.dumps({"containers": out}))
'''
DOCKER_ACT_PY = r'''
import sys, json, subprocess
r = subprocess.run(["docker"] + sys.argv[1:], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=140)
print(json.dumps({"code": r.returncode, "err": r.stderr.strip()[-300:]}))
'''
DOCKER_ACTIONS = ("start", "stop", "restart")
DOCKER_TABS = ("logs", "shell", "rebuild")
CONTAINER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def docker_error(msg: str) -> str:
    if "permission denied" in msg.lower() and "docker" in msg.lower():
        return "your server user may not use docker — on the server run: sudo usermod -aG docker $USER (then log in again)"
    return msg


def docker_crash(old: dict, new: dict) -> str:
    """Why a container needs attention now, or "" (only on a change from a good state)."""
    if new.get("state") == "restarting" and old.get("state") != "restarting":
        return "keeps restarting"
    if new.get("health") == "unhealthy" and old.get("health") != "unhealthy":
        return "unhealthy"
    if old.get("state") == "running" and new.get("state") in ("exited", "dead") and new.get("exitCode") not in (0, 137, 143):
        return f"stopped with exit code {new.get('exitCode')}"
    return ""


def docker_tab_inner(c: dict, action: str) -> str:
    name = shlex.quote(c["name"])
    if action == "logs":
        return f"docker logs -f --tail 300 {name}"
    if action == "shell":
        return f"docker exec -it {name} sh -c 'command -v bash >/dev/null 2>&1 && exec bash || exec sh'"
    if not c.get("compose") or not c.get("service") or not c.get("workdir"):
        raise ApiError(400, "this container was not started by docker compose")
    files = "".join(" -f " + shlex.quote(f) for f in (c.get("configFiles") or "").split(",") if f)
    dc = f"docker compose -p {shlex.quote(c['compose'])}{files}"
    svc = shlex.quote(c["service"])
    return (f"cd {shlex.quote(c['workdir'])} && {dc} up -d --build {svc} && "
            f"echo && echo '✓ {c['service']} rebuilt and started — its logs follow (Ctrl+C to stop watching)' && "
            f"{dc} logs -f --tail 50 {svc}")


# ----------------------------------------------------------------------------- port tunnels (ssh -L)
def clean_port(v, field: str) -> int:
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 65535:
        raise ApiError(400, f"{field} must be a port number (1–65535)")
    return v


def clean_tunnels(items) -> list:
    if not isinstance(items, list) or len(items) > 40:
        raise ApiError(400, "invalid tunnel list")
    out, ids, ports = [], set(), set()
    for t in items:
        if not isinstance(t, dict):
            raise ApiError(400, "invalid tunnel list")
        tid = t.get("id") if isinstance(t.get("id"), str) and re.fullmatch(r"[a-z0-9]{4,16}", t.get("id") or "") else secrets.token_hex(4)
        while tid in ids:
            tid = secrets.token_hex(4)
        ids.add(tid)
        host = clean_str(t.get("remoteHost"), 200, "server address") or "127.0.0.1"
        if not re.fullmatch(r"[A-Za-z0-9.:_-]+", host):
            raise ApiError(400, "the address on the server may only contain letters, digits, . : - _")
        rp = clean_port(t.get("remotePort"), "the server port")
        lp = clean_port(t.get("localPort") or (rp if rp >= 1024 else rp + 10000), "the local port")
        if lp in ports:
            raise ApiError(400, f"local port {lp} is used twice")
        ports.add(lp)
        out.append({"id": tid, "name": clean_str(t.get("name"), 60, "name") or f"port {rp}",
                    "localPort": lp, "remoteHost": host, "remotePort": rp, "on": t.get("on") is True,
                    "http": t.get("http") is True})
    return out


def local_port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def port_problem(port: int) -> str:
    """Why this local port cannot be used (it was found not free)."""
    if port < 1024 and hasattr(os, "geteuid") and os.geteuid() != 0:
        return (f"port {port} is below 1024 — only an administrator may use it on this machine; "
                f"use a local port like {port + 10000}")
    return f"port {port} is already used on this machine — pick another local port"


def tunnel_argv(conf: ServerConf, t: dict) -> list:
    return ssh_base(conf, multiplex=False, batch=True) + [
        "-N", "-T", "-o", "ExitOnForwardFailure=yes",
        "-L", f"127.0.0.1:{t['localPort']}:{t['remoteHost']}:{t['remotePort']}", conf.target]


class Tunnel:
    """Keeps one `ssh -L` running; reconnects with a growing pause when it drops."""

    def __init__(self, project: str, spec: dict, conf_fn, popen=None, free=None):
        self.project = project
        self.spec = dict(spec)
        self.conf_fn = conf_fn
        self.popen = popen or (lambda argv: subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=dict(os.environ, PATH=tool_path()), start_new_session=True))
        self.free = free or local_port_free
        self.state = "off"            # off | connecting | up | error
        self.error = ""
        self.since = 0.0
        self.proc = None
        self.wanted = False
        self.wake = threading.Event()
        self.thread = None

    def start(self):
        if self.wanted:
            return
        self.wanted = True
        self.wake.clear()
        self.state, self.error = "connecting", ""
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.wanted = False
        self.wake.set()
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass
        self.state, self.error = "off", ""

    def _loop(self):
        pause = 2
        while self.wanted:
            lp = self.spec["localPort"]
            if not self.free(lp):
                self.state, self.error = "error", port_problem(lp)
                self.wanted = False
                return
            self.state = "connecting"
            started = time.time()
            try:
                self.proc = p = self.popen(tunnel_argv(self.conf_fn(), self.spec))
            except OSError as e:
                self.state, self.error = "error", f"could not start ssh: {e}"
                self.wanted = False
                return
            while self.wanted and p.poll() is None:
                if self.state != "up" and time.time() - started > 2.5:
                    self.state, self.error, self.since = "up", "", time.time()
                    pause = 2
                self.wake.wait(0.5)
            if not self.wanted:
                return
            err = ""
            try:
                err = (p.stderr.read() or b"").decode("utf-8", "replace").strip() if p.stderr else ""
            except (OSError, ValueError):
                pass
            last = [l for l in err.splitlines() if l.strip() and "Warning: Permanently added" not in l]
            self.state = "error"
            self.error = (last[-1] if last else "the connection closed")[:200] + f" — retrying in {pause}s"
            if self.wake.wait(pause):
                return
            pause = min(pause * 2, 60)

    def info(self) -> dict:
        return dict(self.spec, project=self.project, state=self.state, error=self.error, since=self.since,
                    on=self.wanted)


# ----------------------------------------------------------------------------- Claude usage (limits, cost)
USAGE_WINDOWS = ("five_hour", "seven_day", "seven_day_overage_included")


def merge_limits(cur: dict, info: dict, now: float = None) -> dict:
    """Fold a rate_limit_event's rate_limit_info into the last known limits."""
    now = time.time() if now is None else now
    out = {"windows": dict((cur or {}).get("windows") or {}), "status": (cur or {}).get("status") or "",
           "updated": now}
    if not isinstance(info, dict):
        info = {}
    if info.get("status") in ("allowed", "allowed_warning", "rejected"):
        out["status"] = info["status"]
        out["limiting"] = info.get("rateLimitType") or ""
        out["resetsAt"] = info.get("resetsAt") or 0
    wins = info.get("unifiedWindows")
    if isinstance(wins, dict):
        for k in USAGE_WINDOWS:
            w = wins.get(k)
            if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
                out["windows"][k] = {"used": float(w["utilization"]), "resetsAt": int(w.get("resetsAt") or 0),
                                     "status": w.get("status") or ""}
    elif isinstance(info.get("utilization"), (int, float)) and info.get("rateLimitType") in USAGE_WINDOWS:
        out["windows"][info["rateLimitType"]] = {"used": float(info["utilization"]),
                                                 "resetsAt": int(info.get("resetsAt") or 0),
                                                 "status": info.get("status") or ""}
    # a window whose reset time has passed starts again from zero
    for k, w in list(out["windows"].items()):
        if w.get("resetsAt") and w["resetsAt"] < now:
            out["windows"].pop(k)
    return out


class Usage:
    """Claude limits (last seen) and the API-equivalent cost per day and project."""
    KEEP_DAYS = 60

    def __init__(self, path: Path = None):
        self.path = path or (config_dir() / "usage.json")
        self.lock = threading.Lock()
        try:
            d = json.loads(self.path.read_text())
            d = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            d = {}
        self.data = {"limits": d.get("limits") if isinstance(d.get("limits"), dict) else {},
                     "days": d.get("days") if isinstance(d.get("days"), dict) else {}}
        self._dirty = 0.0

    def _save(self, force=False):
        if not force and time.time() - self._dirty < 5:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data))
            os.replace(tmp, self.path)
            self._dirty = time.time()
        except OSError:
            pass

    def limits(self, info: dict):
        with self.lock:
            self.data["limits"] = merge_limits(self.data["limits"], info)
            self._save()

    def cost(self, project: str, usd: float, day: str = None):
        if not isinstance(usd, (int, float)) or usd <= 0:
            return
        day = day or time.strftime("%Y-%m-%d")
        with self.lock:
            d = self.data["days"].setdefault(day, {})
            d[project or "home"] = round(d.get(project or "home", 0.0) + float(usd), 6)
            for old in sorted(self.data["days"])[:-self.KEEP_DAYS]:
                self.data["days"].pop(old, None)
            self._save(force=True)

    def summary(self, now: float = None) -> dict:
        now = time.time() if now is None else now
        with self.lock:
            lim = merge_limits(self.data["limits"], None, now) if self.data["limits"] else {}
            if self.data["limits"]:
                lim["updated"] = self.data["limits"].get("updated", 0)
            days = {k: dict(v) for k, v in self.data["days"].items()}
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        week = [time.strftime("%Y-%m-%d", time.localtime(now - i * 86400)) for i in range(7)]
        return {"limits": lim, "today": round(sum(days.get(today, {}).values()), 4),
                "week": round(sum(sum(days.get(d, {}).values()) for d in week), 4),
                "days": [{"day": d, "projects": days.get(d, {}), "total": round(sum(days.get(d, {}).values()), 4)}
                         for d in sorted(days, reverse=True)[:30]]}


# ----------------------------------------------------------------------------- setup from the window
# Same files and marked blocks the bash script writes, so both stay compatible.
RC_MARK_BEGIN = "# >>> simabworkspace >>>"
RC_MARK_END = "# <<< simabworkspace <<<"
DEFAULT_KEY_NAMES = ("id_rsa", "id_ecdsa", "id_ecdsa_sk", "id_ed25519", "id_ed25519_sk", "id_dsa")


def home_path(*parts) -> Path:
    return Path(os.path.expanduser("~"), *parts)


def strip_block(text: str) -> str:
    return re.sub(re.escape(RC_MARK_BEGIN) + r".*?" + re.escape(RC_MARK_END) + r"\n?", "", text, flags=re.S)


def write_server_conf(conf: ServerConf):
    f = config_dir() / "config"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# simabworkspace settings\n" + "".join(
        f"{k}={shlex.quote(v)}\n" for k, v in (("SSH_USER", conf.user), ("SSH_HOST", conf.host), ("SSH_PORT", str(conf.port)), ("SSH_KEY", conf.key or ""))))
    os.chmod(f, 0o600)
    # Mutagen runs plain ssh: a key with a custom name must be in ~/.ssh/config
    cfg = home_path(".ssh", "config")
    cfg.parent.mkdir(mode=0o700, exist_ok=True)
    old = cfg.read_text() if cfg.is_file() else ""
    new = strip_block(old)
    if conf.key and os.path.basename(conf.key) not in DEFAULT_KEY_NAMES:
        new = new.rstrip("\n") + ("\n" if new.strip() else "") + f"\n{RC_MARK_BEGIN}\nHost {conf.host}\n    IdentityFile {conf.key}\n{RC_MARK_END}\n"
    if new != old:
        cfg.write_text(new)
        os.chmod(cfg, 0o600)


def list_ssh_keys() -> list:
    out = []
    for pub in sorted(home_path(".ssh").glob("*.pub")):
        priv = pub.with_suffix("")
        if not priv.is_file():
            continue
        parts = pub.read_text(errors="replace").split()
        out.append({"path": str(priv), "type": (parts[0] if parts else "").replace("ssh-", "").replace("ecdsa-sha2-", "ecdsa-"),
                    "comment": " ".join(parts[2:]) if len(parts) > 2 else ""})
    return out


def create_ssh_key() -> str:
    path = home_path(".ssh", "id_ed25519")
    if path.exists():
        path = home_path(".ssh", "id_ed25519_simabworkspace")
    if path.exists():
        raise ApiError(400, f"{path} already exists — pick it from the list")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    r = subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-C", f"{os.environ.get('USER', 'me')}@{socket.gethostname()}", "-f", str(path)],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise ApiError(500, "ssh-keygen failed: " + r.stderr.strip()[-200:])
    return str(path)


def install_ssh_key(conf: ServerConf, password: str, timeout=45) -> str:
    """ssh-copy-id with the server password typed into its prompt (once). The password is never stored."""
    import pty
    if not conf.key or not os.path.isfile(conf.key + ".pub"):
        raise ApiError(400, "choose or create a key first")
    tool = which("ssh-copy-id")
    if tool:
        argv = [tool, "-o", "StrictHostKeyChecking=accept-new", "-i", conf.key + ".pub", "-p", str(conf.port), conf.target]
    else:
        pub = Path(conf.key + ".pub").read_text().strip()
        argv = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-p", str(conf.port), conf.target,
                "umask 077; mkdir -p ~/.ssh; grep -qxF " + shlex.quote(pub) + " ~/.ssh/authorized_keys 2>/dev/null || echo " + shlex.quote(pub) + " >> ~/.ssh/authorized_keys"]
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)
    out, sent, end, mark = b"", 0, time.time() + timeout, 0
    import select
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.5)
        if not r:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
        # a NEW prompt only counts in output that came after the last answer (a prompt can
        # arrive in pieces, e.g. "password:" then " ")
        if re.search(rb"assword[^\n]*:[ \t]*$", out[mark:]) and sent < 2:
            time.sleep(0.15)                       # let the prompt finish setting up the terminal
            os.write(fd, password.encode() + b"\n")
            sent += 1
            out += b"\n"
            mark = len(out)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        _, st = os.waitpid(pid, 0)
    except OSError:
        st = 1
    text = out.decode("utf-8", "replace")
    if sent >= 2 or re.search(r"Permission denied", text):
        raise ApiError(400, "wrong password (or password login is disabled on the server)")
    return text[-500:]


def desktop_quote(arg: str) -> str:
    """Quoting rules of .desktop Exec lines (double quotes only)."""
    if re.fullmatch(r"[A-Za-z0-9_./~-]+", arg):
        return arg
    return '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'


def autostart_enabled() -> bool:
    return any(RC_MARK_BEGIN in (home_path(rc).read_text(errors="replace") if home_path(rc).is_file() else "") for rc in (".bashrc", ".zshrc"))


def set_autostart(on: bool):
    for rc in (".bashrc", ".zshrc"):
        f = home_path(rc)
        if not f.is_file():
            continue
        text = strip_block(f.read_text(errors="replace"))
        if on:
            text = text.rstrip("\n") + f"\n\n{RC_MARK_BEGIN}\ncommand -v mutagen >/dev/null 2>&1 && mutagen daemon start >/dev/null 2>&1\n{RC_MARK_END}\n"
        f.write_text(text)


# ----------------------------------------------------------------------------- updates (GitHub releases)
GITHUB_REPO = "EsmailEmami/simabworkspace"


def version_tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)


def fetch_latest_release(timeout=10) -> dict:
    req = urllib.request.Request(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "simabworkspace"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    asset = next((a for a in d.get("assets") or [] if a.get("name") == "simabworkspace-install.sh"), None)
    return {"version": (d.get("tag_name") or "").lstrip("v"), "notes": (d.get("body") or "")[:6000],
            "url": d.get("html_url") or "", "installer": (asset or {}).get("browser_download_url") or ""}

# ----------------------------------------------------------------------------- application
class App:
    def __init__(self, store: Store = None, conf: ServerConf = None):
        self.store = store or Store()
        self.activity = Activity(self.store.path.parent / "activity.jsonl")
        self._conf = conf
        tok = os.environ.pop("SIMAB_TOKEN", "")
        self.token = tok if re.fullmatch(r"[A-Za-z0-9_-]{20,64}", tok or "") else secrets.token_urlsafe(24)
        self.port = 0
        self.sessions = {}            # id -> Session (insertion order = tab order)
        self.lock = threading.RLock()
        self._projects_cache = (0.0, [])
        self._status = {"ssh": "unknown", "sshError": "", "daemon": False, "checked": 0.0}
        self._status_lock = threading.Lock()
        self.httpd = None
        self.usage = Usage(self.store.path.parent / "usage.json")
        self.tunnels = {}             # (project, id) -> Tunnel
        self._docker_seen = {}        # container id -> last seen container
        self._docker_quiet = {}       # container id -> when we stopped/restarted it ourselves
        self._docker_events = collections.deque(maxlen=50)
        self._docker_seq = 0
        self._docker_error = ""
        self._docker_checked = 0.0

    @property
    def conf(self) -> ServerConf:
        return self._conf or load_server_conf()

    # projects / status
    def projects(self, max_age=3.0) -> list:
        t, cached = self._projects_cache
        if time.time() - t < max_age:
            return cached
        p = merge_projects(mutagen_sessions(), self.store.manual(), self.store.data["projects"])
        self._projects_cache = (time.time(), p)
        return p

    def invalidate(self):
        self._projects_cache = (0.0, [])

    def status(self, force=False) -> dict:
        with self._status_lock:
            st = dict(self._status)
            if not force and time.time() - st["checked"] < 20:
                return st
            conf = self.conf
            if not conf.configured:
                st.update(ssh="unconfigured", sshError="")
            else:
                try:
                    # stderr to a file, not a pipe: a ControlPersist master may keep a pipe open
                    with tempfile.TemporaryFile() as errf:
                        r = subprocess.run(ssh_base(conf, multiplex=bool(self.store.settings.get("sshMultiplex")),
                                                    batch=True) + [conf.target, "true"],
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errf,
                                           timeout=15)
                        errf.seek(0)
                        err = errf.read().decode("utf-8", "replace").strip()
                    ok = r.returncode == 0
                    st.update(ssh="ok" if ok else "err",
                              sshError="" if ok else (err.splitlines() or [""])[-1][:200])
                except (OSError, subprocess.SubprocessError) as e:
                    st.update(ssh="err", sshError=str(e)[:200])
            st["daemon"] = daemon_running()
            st["checked"] = time.time()
            self._status = st
            return dict(st)

    # sessions
    def create_session(self, body: dict) -> dict:
        body = body if isinstance(body, dict) else {}
        sid = secrets.token_hex(6)
        if body.get("kind") == "chat":
            built = build_chat(body, self.conf, self.store, self.projects(max_age=10))
            if built["resume"]:
                try:
                    built["history"] = remote_python(built["conf"], REMOTE_HISTORY_PY,
                                                     [claude_project_dir(built["rpath"]), built["resume"], "", str(HISTORY_PAGE)],
                                                     multiplex=bool(self.store.settings.get("sshMultiplex")))
                except ApiError as e:
                    built["history"] = None
                    built["history_error"] = e.message
            c = ChatSession(sid, built, on_change=self._changed)
            c.log = self.activity.add
            c.remember = self.store.set_chat_prefs
            c.on_usage = self.usage.limits
            c.on_cost = self.usage.cost
            c.start()
            self.activity.add("chat", built["spec"]["project"], c.title, session=built.get("resume") or "")
            with self.lock:
                self.sessions[sid] = c
            self._remember()
            return c.info()
        body = {k: v for k, v in body.items() if k != "_docker"}
        if body.get("kind") == "docker":
            body["_docker"] = self.docker_tab(body.get("project") or "", body.get("container"), body.get("action"))
        built = build_spec(body, self.conf, self.store, self.projects(max_age=10))
        cols, rows = body.get("cols"), body.get("rows")
        cols = int(cols) if isinstance(cols, (int, float)) and not isinstance(cols, bool) else 120
        rows = int(rows) if isinstance(rows, (int, float)) and not isinstance(rows, bool) else 32
        s = Session(sid, built, max(10, min(cols, 1000)), max(3, min(rows, 500)), on_change=self._changed)
        s.start()
        sp = built["spec"]
        if sp["kind"] == "run":
            cmd = self.store.command(sp["project"], sp["cmdId"]) or {}
            self.activity.add("run", sp["project"], s.title, cmdId=sp["cmdId"], cmd=cmd.get("cmd", ""), where=sp["where"])
        elif sp["kind"] == "docker":
            self.activity.add("docker-" + sp["action"], sp["project"], sp["container"])
        elif sp["kind"] in ("shell", "claude"):
            self.activity.add("terminal" if sp["kind"] == "shell" else "claude-tui", sp["project"], s.title, where=sp["where"],
                              session=sp.get("resume") or "")
        with self.lock:
            self.sessions[sid] = s
        self._remember()
        return s.info()

    def get(self, sid: str) -> Session:
        s = self.sessions.get(sid)
        if not s:
            raise ApiError(404, "this tab no longer exists")
        return s

    def close_session(self, sid: str):
        s = self.get(sid)
        with self.lock:
            self.sessions.pop(sid, None)
        s.kill()
        self._remember()

    def order(self, ids):
        if not isinstance(ids, list):
            raise ApiError(400, "bad request")
        with self.lock:
            cur = self.sessions
            new = {i: cur[i] for i in ids if isinstance(i, str) and i in cur}
            for i, s in cur.items():
                new.setdefault(i, s)
            self.sessions = new
        self._remember()

    def rename(self, sid: str, title):
        s = self.get(sid)
        s.title = clean_str(title, 80, "title", required=True)
        self._remember()
        return s.info()

    def _changed(self, s):
        self._remember()
        if isinstance(s, Session) and not s.alive and s.spec.get("kind") == "run" and s.exit_code is not None:
            self.activity.add("run-exit", s.spec.get("project"), s.title, code=s.exit_code,
                              seconds=round(time.time() - s.started, 1), cmdId=s.spec.get("cmdId"))

    # running the bash script without questions ----------------------------------
    def script(self) -> str:
        sc = HERE.parent / "simabworkspace.sh"
        sc = str(sc) if sc.is_file() else which("simabworkspace")
        if not sc:
            raise ApiError(400, "simabworkspace script not found")
        return sc

    def run_script(self, args: list, timeout=180, stdin: bytes = None, env: dict = None):
        """→ (exit code, output without colours). SW_NONINTERACTIVE: the script never starts a wizard."""
        e = dict(os.environ, PATH=tool_path(), SW_NONINTERACTIVE="1")
        e.update(env or {})
        try:
            r = subprocess.run([which("bash") or "/bin/bash", self.script()] + args, input=stdin,
                               stdin=None if stdin is not None else subprocess.DEVNULL,
                               capture_output=True, timeout=timeout, env=e, cwd=os.path.expanduser("~"))
        except subprocess.TimeoutExpired:
            raise ApiError(504, "this took too long — check `simabworkspace status` in a terminal")
        out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", (r.stdout + r.stderr).decode("utf-8", "replace"))
        return r.returncode, out

    @staticmethod
    def script_error(out: str, fallback="failed") -> str:
        errs = [l.strip().lstrip("✗").strip() for l in out.splitlines() if "✗" in l]
        return (" ".join(errs) or out.strip())[-600:] or fallback

    # this machine: prerequisites -------------------------------------------------
    def prereqs(self) -> dict:
        items = []
        for name, why in (("mutagen", "keeps the folders in sync"), ("jq", "used by the sync scripts"),
                          ("ssh", "connects to the server"), ("git", "the Git panel"), ("curl", "downloads"), ("tar", "unpacks downloads")):
            path = which("mutagen" if name == "mutagen" else name)
            ver = ""
            if path and name in ("mutagen", "jq", "git"):
                try:
                    ver = subprocess.run([path, "version" if name == "mutagen" else "--version"], capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0][:40]
                except (OSError, subprocess.SubprocessError, IndexError):
                    ver = ""
            items.append({"name": name, "ok": bool(path), "version": ver, "why": why,
                          "sudo": name not in ("mutagen", "jq")})
        docker = which("docker")
        dok = False
        if docker:
            try:
                dok = subprocess.run([docker, "compose", "version"], capture_output=True, timeout=10).returncode == 0
            except (OSError, subprocess.SubprocessError):
                dok = False
        sudo_free = False
        if which("sudo"):
            try:
                sudo_free = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=10).returncode == 0
            except (OSError, subprocess.SubprocessError):
                pass
        return {"items": items, "docker": dok, "wsl": is_wsl(), "platform": sys.platform, "sudoWithoutPassword": sudo_free}

    def install_prereq(self, name: str, password: str = "") -> dict:
        if name not in ("mutagen", "jq", "ssh", "git", "curl", "tar"):
            raise ApiError(400, "unknown prerequisite")
        env, stdin = {}, None
        if password:
            env["SW_SUDO"] = "sudo -S -p ''"
            stdin = (password + "\n").encode()
        code, out = self.run_script(["install", name], timeout=900, stdin=stdin, env=env)
        if code != 0:
            low = out.lower()
            if "password" in low or "a terminal is required" in low or "sudo:" in low:
                raise ApiError(428 if not password else 400, "needs your computer's password (sudo)" if not password else "the sudo password was not accepted")
            raise ApiError(400, self.script_error(out, "install failed"))
        self.activity.add("install", "", "installed " + name)
        return {"ok": True, "output": out[-1500:]}

    # move a synced project's folders ---------------------------------------------
    def move_project(self, name: str, local: str, remote: str) -> dict:
        p = next((x for x in self.projects(max_age=5) if x["name"] == name), None)
        if not p or p.get("source") != "mutagen":
            raise ApiError(404, "this project is not synced")
        local = clean_str(local, 1000, "local folder")
        remote = clean_str(remote, 1000, "server folder")
        if not local and not remote:
            raise ApiError(400, "nothing to change")
        args = ["move", name, "--yes", "--no-wait"]
        if local:
            args += ["--local", os.path.abspath(os.path.expanduser(local))]
        if remote:
            args += ["--remote", remote]
        code, out = self.run_script(args, timeout=900)
        self.invalidate()
        if code != 0:
            raise ApiError(400, self.script_error(out, "could not move the project"))
        self.activity.add("move", name, "moved folders" + (" → " + local if local else "") + (" | server → " + remote if remote else ""))
        return {"ok": True, "output": out[-1500:]}

    def uninstall(self) -> dict:
        def run():
            time.sleep(0.5)
            self.run_script(["uninstall", "--yes"], timeout=300)
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    # add a project from the window (no terminal) ------------------------------
    def server_folders(self) -> list:
        conf = self.conf
        if not conf.configured:
            raise ApiError(400, "the server is not configured — run simabworkspace setup once")
        cmd = ('for d in "$HOME"/*/; do d=${d%/}; [ -d "$d" ] || continue; g=0; [ -e "$d/.git" ] && g=1; '
               'if [ "$g" = 0 ]; then for s in "$d"/*/.git; do [ -e "$s" ] && { g=2; break; }; done; fi; '
               'printf "%s\t%s\t%s\n" "$d" "$g" "$(stat -c %Y "$d" 2>/dev/null || echo 0)"; done')
        try:
            r = subprocess.run(ssh_base(conf, multiplex=bool(self.store.settings.get("sshMultiplex"))) + ["-T", "-o", "BatchMode=yes", conf.target, cmd],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            raise ApiError(502, f"cannot reach the server: {e}")
        if r.returncode != 0:
            raise ApiError(502, "cannot list server folders: " + (r.stderr.decode("utf-8", "replace").strip().splitlines() or ["?"])[-1][:200])
        synced = {p.get("remote"): p["name"] for p in self.projects(max_age=5) if p.get("remote")}
        out = []
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            path, g, mt = parts[0], parts[1], parts[2]
            out.append({"path": path, "name": os.path.basename(path), "git": {"1": "repo", "2": "repos inside"}.get(g, ""),
                        "mtime": int(mt) if mt.isdigit() else 0, "project": synced.get(path, "")})
        return out

    def add_project(self, body: dict) -> dict:
        mode = body.get("mode")
        if mode not in ("download", "upload"):
            raise ApiError(400, "mode must be download or upload")
        name = clean_str(body.get("name"), 64, "name")
        if name and not valid_name(name):
            raise ApiError(400, "use only letters, digits, ., - and _ in the name")
        local = clean_str(body.get("local"), 1000, "local folder")
        remote = clean_str(body.get("remote"), 1000, "server folder")
        git = "off" if body.get("git") is False else "on"
        argv = []
        if mode == "download":
            if not remote:
                raise ApiError(400, "pick a server folder")
            argv += ["add-server", remote, "--yes", "--no-wait", "--git", git]
            if local:
                argv += ["--local", os.path.abspath(os.path.expanduser(local))]
        else:
            if not local:
                raise ApiError(400, "pick a folder on this machine")
            local = os.path.abspath(os.path.expanduser(local))
            if not os.path.isdir(local):
                raise ApiError(400, f"folder not found on this machine: {local}")
            argv += ["add-local", local, "--yes", "--no-wait", "--git", git]
            if remote:
                argv += ["--remote", remote]
        if name:
            argv += ["--name", name]
        code, out = self.run_script(argv, timeout=180)
        if code != 0:
            raise ApiError(400, self.script_error(out, "could not add the project"))
        self.invalidate()
        added = re.search(r"Project '([^']+)' added", out)
        pname = added.group(1) if added else name
        self.activity.add("add-" + mode, pname, ("downloaded from " + remote) if mode == "download" else ("uploaded " + local))
        return {"ok": True, "project": pname, "output": out[-2000:]}

    # connection, key, sync service -------------------------------------------
    def connection(self) -> dict:
        c = load_server_conf()
        return {"user": c.user, "host": c.host, "port": c.port, "key": c.key, "keys": list_ssh_keys(),
                "autostart": autostart_enabled(), "daemon": daemon_running()}

    def save_connection(self, body: dict) -> dict:
        user = clean_str(body.get("user"), 64, "user", required=True)
        host = clean_str(body.get("host"), 255, "server address", required=True)
        port = clean_str(str(body.get("port") or "22"), 5, "port")
        key = clean_str(body.get("key"), 1000, "key")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", user) or not re.fullmatch(r"[A-Za-z0-9.:-]+", host) or not port.isdigit():
            raise ApiError(400, "check the user, address and port")
        if key and not os.path.isfile(os.path.expanduser(key)):
            raise ApiError(400, f"key not found: {key}")
        write_server_conf(ServerConf(user, host, port, os.path.expanduser(key) if key else ""))
        self._status["checked"] = 0
        return self.status(force=True)

    def sync_service(self, action: str) -> dict:
        exe = which("mutagen")
        if not exe:
            raise ApiError(400, "mutagen not found — run simabworkspace setup")
        env = dict(os.environ, PATH=tool_path())
        steps = {"pause-all": [["sync", "pause", "--all"]], "resume-all": [["daemon", "start"], ["sync", "resume", "--all"]],
                 "stop": [["sync", "pause", "--all"], ["daemon", "stop"]], "start": [["daemon", "start"], ["sync", "resume", "--all"]]}.get(action)
        if not steps:
            raise ApiError(400, "unknown action")
        for st in steps:
            subprocess.run([exe] + st, capture_output=True, timeout=60, env=env)
        self.invalidate()
        self._status["checked"] = 0
        self.activity.add("sync-" + action, "", {"pause-all": "paused all syncing", "resume-all": "resumed all syncing",
                                               "stop": "stopped the sync service", "start": "started the sync service"}[action])
        return {"daemon": daemon_running()}

    def stop_sync(self, name: str) -> dict:
        p = next((x for x in self.projects(max_age=5) if x["name"] == name), None)
        if not p or p.get("source") != "mutagen":
            raise ApiError(404, "this project is not synced")
        exe = which("mutagen")
        r = subprocess.run([exe, "sync", "terminate", name], capture_output=True, text=True, timeout=60, env=dict(os.environ, PATH=tool_path()))
        if r.returncode != 0:
            raise ApiError(500, (r.stderr or r.stdout).strip()[-300:] or "failed")
        # keep it in the list as a server-only project, with both folders
        self.store.add_manual({"name": name if valid_name(name) else re.sub(r"[^A-Za-z0-9._-]", "-", name), "local": p.get("local") or "", "remote": p.get("remote") or ""})
        self.invalidate()
        self.activity.add("sync-stop", name, "stopped syncing (files kept on both sides)")
        return {"ok": True}

    def git_sync(self, name: str, on: bool) -> dict:
        script = HERE.parent / "simabworkspace.sh"
        script = str(script) if script.is_file() else which("simabworkspace")
        if not script:
            raise ApiError(400, "simabworkspace script not found")
        argv = [which("bash") or "/bin/bash", script, "git", name, "on" if on else "off"]

        def run():
            r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=3600, env=dict(os.environ, PATH=tool_path()))
            self.invalidate()
            self.activity.add("git-sync", name, (".git sync on" if on else ".git sync off") + ("" if r.returncode == 0 else " — failed"))
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    # sync details + conflicts ------------------------------------------------------
    def _session(self, name: str) -> dict:
        for x in mutagen_sessions():
            if (x.get("name") or x.get("identifier")) == name:
                return x
        raise ApiError(404, "this project is not synced")

    @staticmethod
    def _kind(entry) -> str:
        if not entry:
            return "deleted"
        k = entry.get("kind")
        return {0: "folder", 1: "file", 2: "link", "directory": "folder"}.get(k, str(k or "?"))

    def sync_details(self, name: str) -> dict:
        x = self._session(name)
        a, b = _endpoint(x.get("alpha")), _endpoint(x.get("beta"))
        local_is_alpha = not a["host"]
        loc, rem = (a, b) if local_is_alpha else (b, a)
        cls, label = sync_state(x.get("status") or "", bool(x.get("paused")))
        cfg = x.get("configuration") or {}
        conflicts = []
        for c in x.get("conflicts") or []:
            la, lb = c.get("alphaChanges") or [], c.get("betaChanges") or []
            lc, rc = (la, lb) if local_is_alpha else (lb, la)

            def side(ch):
                return [{"path": i.get("path", ""), "was": self._kind(i.get("old")), "now": self._kind(i.get("new"))} for i in ch]
            conflicts.append({"path": c.get("root", ""), "local": side(lc), "server": side(rc)})
        return {"name": name, "state": cls, "label": label, "status": x.get("status") or "", "paused": bool(x.get("paused")),
                "local": loc["path"], "remote": rem["path"], "server": f"{rem['user'] or self.conf.user}@{rem['host']}",
                "error": x.get("lastError") or "", "cycles": int(x.get("successfulCycles") or 0),
                "ignores": cfg.get("ignores") or [], "git": str(cfg.get("ignoreVCSMode") or "default") not in ("ignore", "1"),
                "mode": cfg.get("synchronizationMode") or "", "progress": sync_progress(x), "conflicts": conflicts}

    def resolve_conflict(self, name: str, path: str, keep: str) -> dict:
        """Make both sides equal: copy the chosen side's version over the other (or delete it there)."""
        if keep not in ("local", "server") or not git_path_ok(path):
            raise ApiError(400, "bad request")
        d = self.sync_details(name)
        if not any(c["path"] == path or path.startswith(c["path"].rstrip("/") + "/") for c in d["conflicts"]):
            raise ApiError(404, "no such conflict (it may be resolved already)")
        lp = os.path.join(d["local"], path)
        conf = self.conf
        rp = d["remote"].rstrip("/") + "/" + path
        q = shlex.quote(rp)
        ssh = ssh_base(conf, multiplex=bool(self.store.settings.get("sshMultiplex"))) + ["-T", "-o", "BatchMode=yes", conf.target]
        try:
            if keep == "local":
                if os.path.isdir(lp):
                    raise ApiError(400, "folder conflicts: resolve them in VS Code or a terminal")
                if os.path.isfile(lp):
                    if os.path.getsize(lp) > 100 * 1024 * 1024:
                        raise ApiError(400, "file is larger than 100 MB")
                    data = Path(lp).read_bytes()
                    r = subprocess.run(ssh + [f"mkdir -p \"$(dirname {q})\" && cat > {q}"], input=data, capture_output=True, timeout=300)
                else:
                    r = subprocess.run(ssh + [f"rm -f {q}"], capture_output=True, timeout=60)
                if r.returncode != 0:
                    raise ApiError(502, "server: " + r.stderr.decode("utf-8", "replace").strip()[-200:])
            else:
                r = subprocess.run(ssh + [f"if [ -d {q} ]; then echo DIR; elif [ -e {q} ]; then echo FILE; else echo NONE; fi"],
                                   capture_output=True, timeout=60)
                what = r.stdout.decode().strip()
                if what == "DIR":
                    raise ApiError(400, "folder conflicts: resolve them in VS Code or a terminal")
                if what == "FILE":
                    r = subprocess.run(ssh + [f"cat {q}"], capture_output=True, timeout=300)
                    if r.returncode != 0:
                        raise ApiError(502, "server: " + r.stderr.decode("utf-8", "replace").strip()[-200:])
                    os.makedirs(os.path.dirname(lp), exist_ok=True)
                    Path(lp).write_bytes(r.stdout)
                elif os.path.isfile(lp):
                    os.remove(lp)
        except subprocess.SubprocessError as e:
            raise ApiError(502, f"cannot reach the server: {e}")
        exe = which("mutagen")
        if exe:
            subprocess.Popen([exe, "sync", "flush", name], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, env=dict(os.environ, PATH=tool_path()))
        self.invalidate()
        self.activity.add("conflict", name, f"{path}: kept the {'local' if keep == 'local' else 'server'} version")
        return {"ok": True}

    # app shortcut (open the window without a terminal) ----------------------------
    def create_shortcut(self) -> dict:
        script = self.script()
        installed = home_path(".local", "bin", "simabworkspace")
        cmd = str(installed) if installed.is_file() else script
        icon_src = STATIC / "icon.svg"
        made = []
        if is_wsl():
            distro = os.environ.get("WSL_DISTRO_NAME", "")
            wh = windows_home()
            if not wh:
                raise ApiError(500, "could not find your Windows user folder")
            ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs\\Simab Workspace.lnk\");"
                  "$s.TargetPath='wsl.exe';"
                  f"$s.Arguments='{('-d ' + distro + ' ') if distro else ''}-- bash -lc \"{cmd} gui\"';"
                  "$s.WindowStyle=7;$s.Description='Simab Workspace';$s.Save()")
            r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True, timeout=60,
                               cwd="/mnt/c" if os.path.isdir("/mnt/c") else None)
            if r.returncode != 0:
                raise ApiError(500, "could not create the Start menu shortcut: " + r.stderr.decode("utf-8", "replace").strip()[-200:])
            made.append("Windows Start menu")
        elif sys.platform == "darwin":
            app = home_path("Applications", "Simab Workspace.command")
            app.parent.mkdir(parents=True, exist_ok=True)
            app.write_text(f"#!/bin/bash\n{shlex.quote(cmd)} gui\n")
            app.chmod(0o755)
            made.append(str(app))
        else:
            d = home_path(".local", "share", "applications")
            d.mkdir(parents=True, exist_ok=True)
            icon_dir = home_path(".local", "share", "icons")
            icon_dir.mkdir(parents=True, exist_ok=True)
            icon = icon_dir / "simabworkspace.svg"
            icon.write_bytes(icon_src.read_bytes())
            (d / "simabworkspace.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Simab Workspace\nComment=Projects, terminals and Claude\n"
                f"Exec={desktop_quote(cmd)} gui\nIcon={icon}\nTerminal=false\nCategories=Development;\n")
            made.append("app menu")
        self.activity.add("shortcut", "", "added to " + ", ".join(made))
        return {"ok": True, "where": made}

    # updates ------------------------------------------------------------------------
    def check_update(self, force=False) -> dict:
        cache = self.__dict__.setdefault("_upd", {"t": 0, "data": None})
        if force or not cache["data"] or time.time() - cache["t"] > 6 * 3600:
            try:
                cache["data"] = fetch_latest_release()
                cache["error"] = ""
            except Exception as e:  # noqa: BLE001 - offline / filtered: just no update notice
                cache["error"] = str(e)[:200]
            cache["t"] = time.time()
        d = cache["data"] or {}
        latest = d.get("version") or ""
        return {"current": VERSION, "latest": latest, "available": bool(latest) and version_tuple(latest) > version_tuple(VERSION) and bool(d.get("installer")),
                "notes": d.get("notes", ""), "url": d.get("url", ""), "error": cache.get("error", "")}

    def install_update(self) -> dict:
        info = self.check_update(force=True)
        if not info["available"]:
            raise ApiError(400, "no newer version" + (f" ({info['error']})" if info["error"] else ""))
        url = (self._upd["data"] or {}).get("installer")
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "simabworkspace-install.sh")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "simabworkspace"})
            with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
                shutil.copyfileobj(r, f)
        except Exception as e:  # noqa: BLE001
            raise ApiError(502, f"download failed: {e}")
        r = subprocess.run([which("bash") or "/bin/bash", path, "--no-open"], stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=300, env=dict(os.environ, PATH=tool_path()), cwd=os.path.expanduser("~"))
        shutil.rmtree(tmp, ignore_errors=True)
        if r.returncode != 0:
            raise ApiError(500, "update failed: " + re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", (r.stdout + r.stderr).decode("utf-8", "replace")).strip()[-400:])
        self.activity.add("update", "", f"updated {VERSION} → {info['latest']}")
        # start the new version on the same address with the same key, then stop this one
        cmd = home_path(".local", "bin", "simabworkspace")
        argv = [str(cmd), "gui", "serve"] if cmd.is_file() else [sys.executable, str(HERE / "server.py"), "serve"]
        log = config_dir() / "gui.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as lf:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, start_new_session=True,
                             cwd=os.path.expanduser("~"), env=dict(os.environ, PATH=tool_path(), SIMAB_TOKEN=self.token, SIMAB_RESTART="1"))
        threading.Thread(target=shutdown_app, args=(self,), daemon=True).start()
        return {"ok": True, "version": info["latest"]}

    # git badges ---------------------------------------------------------------
    def git_summary_one(self, p: dict, fetch=False) -> dict:
        info = git_repos(self, p)
        out = {"changes": 0, "ahead": 0, "behind": 0, "conflicts": 0, "repos": len(info["repos"]),
               "noUpstream": 0, "t": time.time(), "where": info["where"]}
        for r in info["repos"]:
            repo = GitRepo(self, p, r, info["where"])
            try:
                if fetch:
                    repo.run(["fetch", "--quiet", "--prune"], timeout=60)
                st = repo.status()
            except ApiError:
                continue
            if not st.get("repo"):
                continue
            out["changes"] += len(st["files"])
            out["ahead"] += st.get("ahead", 0)
            out["behind"] += st.get("behind", 0)
            out["conflicts"] += st.get("conflicts", 0)
            out["noUpstream"] += 0 if st.get("upstream") else 1
        return out

    def git_summary(self, project: str = "", force=False) -> dict:
        cache = self.__dict__.setdefault("_git_sum", {})
        if project and force:
            p = next((x for x in self.projects(max_age=10) if x["name"] == project), None)
            if p:
                cache[project] = self.git_summary_one(p)
        return dict(cache)

    def git_watch_loop(self, interval=30, fetch_every=300):
        last_fetch = {}
        while not getattr(self, "_stopping", False):
            s = self.store.settings
            if s.get("gitWatch", True):
                cache = self.__dict__.setdefault("_git_sum", {})
                for p in self.projects(max_age=20):
                    if not (p.get("localExists") or p.get("remote")):
                        continue
                    fetch = bool(s.get("gitAutoFetch", True)) and time.time() - last_fetch.get(p["name"], 0) > fetch_every
                    try:
                        cache[p["name"]] = self.git_summary_one(p, fetch=fetch)
                        if fetch:
                            last_fetch[p["name"]] = time.time()
                    except Exception:  # noqa: BLE001 - a bad repo must not stop the watcher
                        pass
            for _ in range(interval * 2):
                if getattr(self, "_stopping", False):
                    return
                time.sleep(0.5)

    def project_files(self, project: str) -> list:
        cache = self.__dict__.setdefault("_files_cache", {})
        hit = cache.get(project)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        p = next((x for x in self.projects(max_age=10) if x["name"] == project), None)
        if not p:
            raise ApiError(404, "project not found")
        if p.get("localExists"):
            files = local_files(p["local"])
        elif p.get("remote"):
            conf = self.conf
            ep = p.get("endpoint") or {}
            conf = conf.with_endpoint(ep.get("user"), ep.get("host"), ep.get("port"))
            cmd = f"cd {remote_path_expr(p['remote'])} && " + REMOTE_FILES_SH
            try:
                r = subprocess.run(ssh_base(conf, multiplex=bool(self.store.settings.get("sshMultiplex")))
                                   + ["-T", "-o", "BatchMode=yes", conf.target, cmd],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
            except (OSError, subprocess.SubprocessError) as e:
                raise ApiError(502, f"could not list files on the server: {e}")
            files = [x for x in r.stdout.decode("utf-8", "replace").splitlines() if x][:FILE_LIMIT]
        else:
            files = []
        files = with_dirs(files)
        cache[project] = (time.time(), files)
        return files

    def chat_history_page(self, sid: str, before: int) -> dict:
        s = self.get(sid)
        if not isinstance(s, ChatSession):
            raise ApiError(400, "not a chat tab")
        conv = s.built.get("resume") or ""
        if not conv or "conf" not in s.built:
            return {"total": 0, "start": 0, "entries": []}
        return remote_python(s.built["conf"], REMOTE_HISTORY_PY,
                             [claude_project_dir(s.built["rpath"]), conv, str(max(0, int(before))), str(HISTORY_PAGE)],
                             multiplex=bool(self.store.settings.get("sshMultiplex")))

    def claude_sessions(self, project: str) -> list:
        p = next((x for x in self.projects(max_age=10) if x["name"] == project), None) if project else None
        if project and (not p or not p.get("remote")):
            raise ApiError(404, "project or its server folder not found")
        conf = self.conf
        if not conf.configured:
            raise ApiError(400, "the server is not configured")
        ep = (p or {}).get("endpoint") or {}
        conf = conf.with_endpoint(ep.get("user"), ep.get("host"), ep.get("port"))
        rpath = (p or {}).get("remote") or f"/home/{conf.user}"
        out = remote_python(conf, REMOTE_LIST_PY, [claude_project_dir(rpath)],
                            multiplex=bool(self.store.settings.get("sshMultiplex")))
        return out if isinstance(out, list) else []

    def _remember(self):
        specs = []
        for s in list(self.sessions.values()):
            sp = dict(s.spec)
            if sp.get("kind") in ("sync-info",):
                continue
            sp["title"] = s.title
            specs.append(sp)
        self.store.set_last_tabs(specs)

    def shutdown_sessions(self):
        for s in list(self.sessions.values()):
            s.kill()
        self.sessions = {}
        for t in list(self.tunnels.values()):
            t.stop()

    def _project(self, name: str) -> dict:
        p = next((x for x in self.projects(max_age=10) if x["name"] == name), None)
        if not p:
            raise ApiError(404, "project not found")
        return p

    def _project_conf(self, p: dict) -> ServerConf:
        ep = p.get("endpoint") or {}
        return self.conf.with_endpoint(ep.get("user"), ep.get("host"), ep.get("port"))

    # docker ----------------------------------------------------------------------
    def docker_list(self, project: dict = None) -> dict:
        if not self.conf.configured:
            raise ApiError(400, "the server is not configured")
        projs = [project] if project else [p for p in self.projects(max_age=20) if p.get("remote")]
        groups = {}
        for p in projs:
            if not p.get("remote"):
                raise ApiError(400, "this project has no server folder")
            c = self._project_conf(p)
            groups.setdefault((c.target, str(c.port)), (c, []))[1].append(p)
        out, errors = [], []
        for c, ps in groups.values():
            r = remote_python(c, DOCKER_PY, [f"{p['name']}={p['remote']}" for p in ps],
                              multiplex=bool(self.store.settings.get("sshMultiplex")), timeout=40) or {}
            if r.get("error"):
                errors.append(docker_error(str(r["error"])))
            out += [x for x in r.get("containers") or [] if isinstance(x, dict)]
        return {"containers": out, "error": "; ".join(errors)}

    def _docker_update(self, d: dict, only: str = None):
        now = time.time()
        with self.lock:
            prev = self._docker_seen
            seen = {k: v for k, v in prev.items() if v.get("proj") != only} if only else {}
            for c in d.get("containers") or []:
                old = prev.get(c.get("id"))
                why = docker_crash(old, c) if old else ""
                if why and now - self._docker_quiet.get(c["id"], 0) > 90:
                    self._docker_seq += 1
                    self._docker_events.append({"seq": self._docker_seq, "project": c.get("proj"), "name": c.get("name"),
                                                "service": c.get("service"), "why": why, "t": now})
                    self.activity.add("docker-crash", c.get("proj") or "", f"{c.get('name')}: {why}")
                seen[c.get("id")] = c
            self._docker_seen = seen
            if not only:
                self._docker_error = d.get("error") or ""
                self._docker_checked = now

    def docker_project(self, name: str) -> dict:
        p = self._project(name)
        d = self.docker_list(p)
        self._docker_update(d, only=name)
        return d

    def _container(self, name: str, container) -> tuple:
        if not isinstance(container, str) or not CONTAINER_RE.fullmatch(container):
            raise ApiError(400, "invalid container")
        p = self._project(name)
        c = next((x for x in self.docker_list(p)["containers"] if container in (x.get("name"), x.get("id"))), None)
        if not c:
            raise ApiError(404, f"container {container} not found in {name}")
        return p, c

    def docker_action(self, name: str, container, action) -> dict:
        if action not in DOCKER_ACTIONS:
            raise ApiError(400, "unknown docker action")
        p, c = self._container(name, container)
        if action in ("stop", "restart"):
            self._docker_quiet[c["id"]] = time.time()
        r = remote_python(self._project_conf(p), DOCKER_ACT_PY, [action, c["name"]],
                          multiplex=bool(self.store.settings.get("sshMultiplex")), timeout=150) or {}
        if r.get("code"):
            raise ApiError(502, docker_error(r.get("err") or f"docker {action} failed"))
        self.activity.add("docker-" + action, name, c["name"])
        return self.docker_project(name)

    def docker_tab(self, name: str, container, action) -> dict:
        if action not in DOCKER_TABS:
            raise ApiError(400, "unknown docker action")
        _, c = self._container(name, container)
        title = {"logs": "logs", "shell": "shell", "rebuild": "rebuild"}[action] + " · " + (c.get("service") or c["name"])
        return {"inner": docker_tab_inner(c, action), "title": title, "container": c["name"], "action": action}

    def docker_summary(self, since: int = 0) -> dict:
        with self.lock:
            per = {}
            for c in self._docker_seen.values():
                s = per.setdefault(c.get("proj") or "", {"total": 0, "running": 0, "bad": 0})
                s["total"] += 1
                s["running"] += 1 if c.get("state") == "running" else 0
                bad = c.get("state") == "restarting" or c.get("health") == "unhealthy" or (
                    c.get("state") in ("exited", "dead") and c.get("exitCode") not in (0, 137, 143))
                s["bad"] += 1 if bad else 0
            return {"summary": per, "events": [e for e in self._docker_events if e["seq"] > since],
                    "seq": self._docker_seq, "error": self._docker_error, "checked": self._docker_checked}

    def docker_watch_loop(self, interval=30):
        pause = 0
        while not getattr(self, "_stopping", False):
            if pause <= 0 and self.store.settings.get("dockerWatch", True) and self.conf.configured \
                    and self._status.get("ssh") != "err":
                try:
                    d = self.docker_list()
                    self._docker_update(d)
                    # no docker on the server: look again much later
                    pause = 20 if "not installed" in (d.get("error") or "") else 0
                except ApiError as e:
                    self._docker_error = e.message
                except Exception as e:  # noqa: BLE001 - the watcher must keep running
                    log_line(f"docker watch: {e}")
            else:
                pause -= 1
            for _ in range(interval * 2):
                if getattr(self, "_stopping", False):
                    return
                time.sleep(0.5)

    # port tunnels ----------------------------------------------------------------
    def _tunnel_conf(self, project: str):
        def conf():
            p = next((x for x in self.projects(max_age=30) if x["name"] == project), None)
            return self._project_conf(p) if p else self.conf
        return conf

    def start_tunnels(self):
        with self.lock:
            for project, pd in list(self.store.data["projects"].items()):
                for spec in pd.get("tunnels") or []:
                    key = (project, spec.get("id"))
                    if key not in self.tunnels:
                        self.tunnels[key] = Tunnel(project, spec, self._tunnel_conf(project))
                        if spec.get("on") and self.conf.configured:
                            self.tunnels[key].start()

    def tunnel_list(self) -> list:
        out = []
        for project, pd in self.store.data["projects"].items():
            for spec in pd.get("tunnels") or []:
                t = self.tunnels.get((project, spec.get("id")))
                out.append(t.info() if t else dict(spec, project=project, state="off", error="", since=0, on=False))
        return out

    def set_tunnels(self, project: str, items) -> list:
        self._project(project)
        new = clean_tunnels(items)
        mine = {t["localPort"] for t in new}
        for other, pd in self.store.data["projects"].items():
            if other == project:
                continue
            for t in pd.get("tunnels") or []:
                if t.get("localPort") in mine:
                    raise ApiError(400, f"local port {t['localPort']} is already used by a tunnel of {other}")
        with self.lock:
            for key in [k for k in self.tunnels if k[0] == project and k[1] not in {t["id"] for t in new}]:
                self.tunnels.pop(key).stop()
            for spec in new:
                key = (project, spec["id"])
                t = self.tunnels.get(key)
                same = t and all(t.spec.get(k) == spec[k] for k in ("localPort", "remoteHost", "remotePort"))
                if t and not same:
                    t.stop()
                if not same:
                    t = self.tunnels[key] = Tunnel(project, spec, self._tunnel_conf(project))
                t.spec = dict(spec)
                if spec["on"] and not t.wanted:
                    t.start()
                elif not spec["on"] and t.wanted:
                    t.stop()
            self.store.data["projects"].setdefault(project, {})["tunnels"] = new
            self.store.save()
        self.activity.add("tunnels", project, ", ".join(f"{t['name']} :{t['localPort']}" for t in new))
        return self.tunnel_list()

    def toggle_tunnel(self, project: str, tid: str, on: bool) -> list:
        specs = [dict(t) for t in (self.store.data["projects"].get(project) or {}).get("tunnels") or []]
        hit = next((t for t in specs if t.get("id") == tid), None)
        if not hit:
            raise ApiError(404, "this tunnel no longer exists")
        hit["on"] = on
        if on:
            t = self.tunnels.get((project, tid))
            if (not t or not t.wanted) and not local_port_free(hit["localPort"]):
                raise ApiError(409, port_problem(hit["localPort"]))
        return self.set_tunnels(project, specs)

    # editor
    def open_editor(self, body: dict) -> dict:
        body = body if isinstance(body, dict) else {}
        where = body.get("where") or "local"
        p = next((x for x in self.projects(max_age=10) if x["name"] == body.get("project")), None)
        if not p:
            raise ApiError(404, "project not found")
        try:
            editor = shlex.split(self.store.settings.get("editorCmd") or "code")
        except ValueError:
            raise ApiError(400, "invalid editor command")
        if not editor or not which(editor[0]):
            raise ApiError(400, f"{editor[0] if editor else 'code'} not found — set the editor command in Settings")
        if where == "local":
            if not p.get("localExists"):
                raise ApiError(400, "this project is not synced to this machine")
            argv = editor + [p["local"]]
        else:
            if not p.get("remote"):
                raise ApiError(400, "this project has no server folder")
            conf = self.conf
            ep = p.get("endpoint") or {}
            conf = conf.with_endpoint(ep.get("user"), ep.get("host"), ep.get("port"))
            if not conf.configured:
                raise ApiError(400, "the server is not configured")
            conf = ServerConf(conf.user, conf.host, conf.port, conf.key or default_identity())
            write_vscode_ssh_alias(conf)
            if is_wsl():
                wh = windows_home()
                if wh:
                    try:
                        prepare_windows_vscode(conf, wh)
                    except OSError as e:
                        raise ApiError(500, f"could not set up Windows SSH for VS Code: {e}")
            argv = editor + ["--remote", f"ssh-remote+{VSCODE_HOST_ALIAS}", p["remote"]]
        try:
            subprocess.Popen(argv, cwd=os.path.expanduser("~"), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, env=dict(os.environ, PATH=tool_path()))
        except OSError as e:
            raise ApiError(500, f"could not open: {e}")
        self.activity.add("vscode", p["name"], "VS Code " + ("on server" if where != "local" else "local folder"), where=where)
        return {"ok": True}

    # mutagen actions
    def sync_action(self, name: str, action: str) -> dict:
        if action not in ("flush", "pause", "resume"):
            raise ApiError(400, "unknown action")
        p = next((x for x in self.projects(max_age=10) if x["name"] == name), None)
        if not p or p.get("source") != "mutagen":
            raise ApiError(404, "this project is not synced")
        exe = which("mutagen")
        if not exe:
            raise ApiError(400, "mutagen not found")
        argv = [exe, "sync", action, name]
        env = dict(os.environ, PATH=tool_path())
        if action == "flush":
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, env=env)
        else:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=20, env=env)
            if r.returncode != 0:
                raise ApiError(500, (r.stderr or r.stdout).strip()[:300] or "failed")
        self.invalidate()
        self.activity.add("sync-" + action, name, {"flush": "sync now", "pause": "sync paused", "resume": "sync resumed"}[action])
        return {"ok": True}

    def state(self) -> dict:
        return {"version": VERSION, "server": self.conf.to_dict(), "settings": self.store.settings,
                "projects": self.projects(), "sessions": [s.info() for s in self.sessions.values()],
                "lastTabs": self.store.data.get("lastTabs") or [], "wsl": is_wsl(),
                "platform": sys.platform}


def daemon_running() -> bool:
    try:
        r = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", "mutagen daemon run"],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def default_identity() -> str:
    """The key ssh would use: the configured one, else the first default key."""
    for n in ("id_ed25519", "id_ecdsa", "id_rsa"):
        p = Path(os.path.expanduser("~")) / ".ssh" / n
        if p.is_file():
            return str(p)
    return ""


def windows_home():
    """WSL: the Windows user folder as a Linux path (/mnt/c/Users/me), or None."""
    try:
        r = subprocess.run(["cmd.exe", "/c", "echo %USERPROFILE%"], capture_output=True, text=True,
                           timeout=10, cwd="/mnt/c" if os.path.isdir("/mnt/c") else None)
        win = r.stdout.strip().splitlines()[-1].strip()
        r = subprocess.run(["wslpath", "-u", win], capture_output=True, text=True, timeout=5)
        p = r.stdout.strip()
        return Path(p) if p and os.path.isdir(p) else None
    except (OSError, IndexError, subprocess.SubprocessError):
        return None


def wsl_to_windows(path: Path) -> str:
    r = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True, timeout=5)
    return r.stdout.strip() or str(path)


def prepare_windows_vscode(conf: ServerConf, win_home: Path, to_windows=wsl_to_windows):
    """WSL: VS Code runs on Windows and reads the WINDOWS ~/.ssh/config, so the
    host entry (and a copy of the key — Windows ssh cannot use keys inside WSL)
    must be there too."""
    ssh_dir = win_home / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    key = conf.key or default_identity()
    win_key = ""
    if key and os.path.isfile(os.path.expanduser(key)):
        dst = ssh_dir / "simabworkspace_key"
        data = Path(os.path.expanduser(key)).read_bytes()
        if not dst.is_file() or dst.read_bytes() != data:
            dst.write_bytes(data)
        pub = Path(os.path.expanduser(key) + ".pub")
        if pub.is_file():
            (ssh_dir / "simabworkspace_key.pub").write_bytes(pub.read_bytes())
        win_key = to_windows(dst)
    write_vscode_ssh_alias(ServerConf(conf.user, conf.host, conf.port, win_key), ssh_dir / "config", chmod=False)


def write_vscode_ssh_alias(conf: ServerConf, ssh_config: Path = None, chmod=True):
    """Marked block in ~/.ssh/config so VS Code Remote-SSH can reach the server."""
    f = ssh_config or Path(os.path.expanduser("~")) / ".ssh" / "config"
    f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        old = f.read_text()
    except OSError:
        old = ""
    old = re.sub(re.escape(GUI_MARK_BEGIN) + r".*?" + re.escape(GUI_MARK_END) + r"\n?", "", old, flags=re.S)
    lines = [GUI_MARK_BEGIN, f"Host {VSCODE_HOST_ALIAS}", f"    HostName {conf.host}",
             f"    User {conf.user}", f"    Port {conf.port}"]
    if conf.key:
        key = f'"{conf.key}"' if " " in conf.key else conf.key
        lines.append(f"    IdentityFile {key}")
    lines.append(GUI_MARK_END)
    # Put our Host block first: ssh uses the first value it finds per option.
    new = "\n".join(lines) + "\n" + ("\n" + old.lstrip("\n") if old.strip() else "")
    f.write_text(new)
    if chmod:
        os.chmod(f, 0o600)


# ----------------------------------------------------------------------------- HTTP
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".woff2": "font/woff2", ".svg": "image/svg+xml",
        ".png": "image/png", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP}/{VERSION}"
    app: App = None   # set on the subclass

    def log_message(self, fmt, *args):   # quiet
        pass

    # helpers
    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _token_ok(self, token: str) -> bool:
        return bool(token) and hmac.compare_digest(token.encode(), self.app.token.encode())

    def _send(self, status: int, body: bytes, ctype: str, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise ApiError(413, "request too large")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError(400, "invalid JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "bad request")
        return body

    # verbs
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        url = urllib.parse.urlsplit(self.path)
        path, query = url.path, urllib.parse.parse_qs(url.query)
        try:
            if method == "GET" and path == "/ws":
                return self._websocket(query)
            if path.startswith("/api/"):
                if not self._token_ok(self.headers.get("X-Token") or ""):
                    raise ApiError(401, "invalid token — open the window with simabworkspace gui")
                return self._json(200, self._api(method, path, query))
            if method == "GET":
                return self._static(path)
            raise ApiError(404, "not found")
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001 - report, keep serving
            try:
                self._json(500, {"error": f"internal error: {e}"})
            except OSError:
                pass

    def _static(self, path: str):
        if path in ("/", "/index.html"):
            path = "/index.html"
        rel = urllib.parse.unquote(path).lstrip("/")
        f = (STATIC / rel).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            raise ApiError(404, "not found")
        extra = {"Content-Security-Policy": "default-src 'self'; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; "
                                            "img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'"} \
            if f.suffix == ".html" else None
        self._send(200, f.read_bytes(), MIME.get(f.suffix, "application/octet-stream"), extra)

    def _api(self, method: str, path: str, query: dict):
        app = self.app
        parts = [urllib.parse.unquote(p) for p in path.split("/")[2:]]
        route = (method, parts[0] if parts else "")
        if route == ("GET", "ping"):
            return {"ok": True, "version": VERSION, "pid": os.getpid()}
        if route == ("GET", "state"):
            return app.state()
        if route == ("GET", "projects"):
            return {"projects": app.projects()}
        if route == ("GET", "status"):
            return app.status(force=query.get("force") == ["1"])
        if route == ("POST", "settings"):
            s = app.store.update_settings(self._body())
            return {"settings": s}
        if route == ("POST", "commands") and len(parts) == 2:
            cmds = app.store.set_commands(parts[1], self._body().get("commands"))
            app.invalidate()
            return {"commands": cmds}
        if route == ("GET", "suggest") and len(parts) == 2:
            p = next((x for x in app.projects() if x["name"] == parts[1]), None)
            return {"suggestions": suggestions(p["local"]) if p and p.get("localExists") else []}
        if route == ("POST", "color") and len(parts) == 2:
            c = app.store.set_color(parts[1], self._body().get("color"))
            app.invalidate()
            return {"color": c}
        if route == ("POST", "manual"):
            item = app.store.add_manual(self._body())
            app.invalidate()
            return {"project": item}
        if route == ("DELETE", "manual") and len(parts) == 2:
            app.store.remove_manual(parts[1])
            app.invalidate()
            return {"ok": True}
        if route == ("POST", "sessions") and len(parts) == 1:
            return app.create_session(self._body())
        if route == ("POST", "sessions") and len(parts) == 3:
            s = app.get(parts[1])
            act = parts[2]
            if act == "restart":
                s.restart()
                return s.info()
            if act == "new-chat":
                if not isinstance(s, ChatSession):
                    raise ApiError(400, "not a chat tab")
                s.restart(fresh=True)
                app._remember()
                return s.info()
            if act == "stop":
                s.stop()
                return s.info()
            if act == "rename":
                return app.rename(parts[1], self._body().get("title"))
            raise ApiError(404, "not found")
        if route == ("DELETE", "sessions") and len(parts) == 2:
            app.close_session(parts[1])
            return {"ok": True}
        if route == ("POST", "order"):
            app.order(self._body().get("ids"))
            return {"ok": True}
        if route == ("POST", "editor"):
            return app.open_editor(self._body())
        if route == ("GET", "files") and len(parts) == 2:
            return {"files": app.project_files(parts[1])}
        if parts and parts[0] == "git" and len(parts) >= 3:
            p = next((x for x in app.projects(max_age=10) if x["name"] == parts[1]), None)
            if not p:
                raise ApiError(404, "project not found")
            act = parts[2]
            if method == "GET" and act == "repos":
                info = git_repos(app, p)
                info["gitSync"] = (p.get("sync") or {}).get("git", True) if p.get("source") == "mutagen" else None
                info["statuses"] = {}
                for r in info["repos"]:
                    try:
                        info["statuses"][r] = GitRepo(app, p, r, info["where"]).status()
                    except ApiError as e:
                        info["statuses"][r] = {"repo": False, "error": e.message, "files": [], "conflicts": 0}
                return info
            body = self._body() if method == "POST" else {}
            rq = body.get("repo") if method == "POST" else (query.get("repo") or [""])[0]
            wh = body.get("where") if method == "POST" else (query.get("where") or [None])[0]
            repo = GitRepo(app, p, rq if isinstance(rq, str) else "", wh if wh in WHERE else None)
            if method == "GET" and act == "status":
                return repo.status()
            if method == "GET" and act == "diff":
                return {"diff": repo.diff((query.get("path") or [""])[0], (query.get("untracked") or ["0"])[0] == "1")}
            if method == "POST" and act in ("commit", "pull", "push", "fetch"):
                if act == "commit":
                    out = repo.commit(body.get("message"), body.get("files"))
                else:
                    out = getattr(repo, act)()
                app.invalidate()
                text = (body.get("message") or "").strip() if act == "commit" else ("conflicts" if out == "conflicts" else "")
                app.activity.add("git-" + act, p["name"], text, repo=repo.repo)
                return {"output": out, "status": repo.status()}
            raise ApiError(404, "not found")
        if route == ("GET", "docker") and len(parts) == 2:
            return app.docker_project(parts[1])
        if route == ("POST", "docker") and len(parts) == 2:
            b = self._body()
            return app.docker_action(parts[1], b.get("container"), b.get("action"))
        if route == ("GET", "docker-summary"):
            since = (query.get("since") or ["0"])[0]
            return app.docker_summary(int(since) if since.isdigit() else 0)
        if route == ("GET", "tunnels"):
            return {"tunnels": app.tunnel_list()}
        if route == ("POST", "tunnels") and len(parts) == 2:
            return {"tunnels": app.set_tunnels(parts[1], self._body().get("tunnels"))}
        if route == ("POST", "tunnels") and len(parts) == 3:
            return {"tunnels": app.toggle_tunnel(parts[1], parts[2], self._body().get("on") is True)}
        if route == ("GET", "usage"):
            return app.usage.summary()
        if route == ("GET", "connection"):
            return app.connection()
        if route == ("POST", "connection"):
            return app.save_connection(self._body())
        if route == ("POST", "ssh-key") and len(parts) == 2 and parts[1] == "create":
            return {"key": create_ssh_key(), "keys": list_ssh_keys()}
        if route == ("POST", "ssh-key") and len(parts) == 2 and parts[1] == "install":
            pw = self._body().get("password")
            if not isinstance(pw, str) or not pw:
                raise ApiError(400, "enter the server password")
            install_ssh_key(load_server_conf(), pw)
            app._status["checked"] = 0
            return app.status(force=True)
        if route == ("POST", "autostart"):
            set_autostart(self._body().get("on") is True)
            return {"autostart": autostart_enabled()}
        if route == ("POST", "sync-service") and len(parts) == 2:
            return app.sync_service(parts[1])
        if route == ("POST", "sync") and len(parts) == 3 and parts[2] == "stop":
            return app.stop_sync(parts[1])
        if route == ("POST", "git-sync") and len(parts) == 2:
            return app.git_sync(parts[1], self._body().get("on") is True)
        if route == ("GET", "sync") and len(parts) == 3 and parts[2] == "details":
            return app.sync_details(parts[1])
        if route == ("POST", "sync") and len(parts) == 3 and parts[2] == "resolve":
            b = self._body()
            return app.resolve_conflict(parts[1], b.get("path") or "", b.get("keep") or "")
        if route == ("POST", "shortcut"):
            return app.create_shortcut()
        if route == ("GET", "update"):
            return app.check_update(force=query.get("force") == ["1"])
        if route == ("POST", "update"):
            return app.install_update()
        if route == ("GET", "prereqs"):
            return app.prereqs()
        if route == ("POST", "prereqs") and len(parts) == 2:
            pw = self._body().get("password")
            return app.install_prereq(parts[1], pw if isinstance(pw, str) else "")
        if route == ("POST", "move") and len(parts) == 2:
            b = self._body()
            return app.move_project(parts[1], b.get("local"), b.get("remote"))
        if route == ("POST", "uninstall"):
            if self._body().get("confirm") != "uninstall":
                raise ApiError(400, "type uninstall to confirm")
            return app.uninstall()
        if route == ("GET", "server-folders"):
            return {"folders": app.server_folders()}
        if route == ("POST", "add-project"):
            return app.add_project(self._body())
        if route == ("GET", "git-summary"):
            return {"summary": app.git_summary((query.get("project") or [""])[0], (query.get("force") or ["0"])[0] == "1")}
        if route == ("GET", "activity"):
            lim = (query.get("limit") or ["500"])[0]
            return {"items": app.activity.list(int(lim) if lim.isdigit() else 500,
                                               (query.get("project") or [""])[0], (query.get("q") or [""])[0])}
        if route == ("GET", "chat-history") and len(parts) == 2:
            b = (query.get("before") or ["0"])[0]
            return app.chat_history_page(parts[1], int(b) if b.isdigit() else 0)
        if route == ("GET", "claude-sessions"):
            return {"sessions": app.claude_sessions(parts[1] if len(parts) > 1 else "")}
        if route == ("POST", "sync") and len(parts) == 3:
            return app.sync_action(parts[1], parts[2])
        if route == ("POST", "quit"):
            threading.Thread(target=shutdown_app, args=(app,), daemon=True).start()
            return {"ok": True}
        raise ApiError(404, "not found")

    def _websocket(self, query: dict):
        if not self._token_ok((query.get("t") or [""])[0]):
            raise ApiError(401, "bad token")
        port = self.server.server_address[1]
        origin = self.headers.get("Origin") or ""
        if origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            raise ApiError(403, "bad origin")
        key = self.headers.get("Sec-WebSocket-Key")
        if (self.headers.get("Upgrade") or "").lower() != "websocket" or not key:
            raise ApiError(400, "websocket upgrade required")
        sess = self.app.get((query.get("id") or [""])[0])
        self.close_connection = True
        self.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {ws_accept_key(key)}\r\n\r\n").encode())
        self.wfile.flush()
        self.connection.settimeout(None)
        ws = WebSocket(self.connection, self.rfile)
        try:
            sess.attach(ws)
            while True:
                msg = ws.recv()
                if not isinstance(msg, str):
                    continue
                try:
                    m = json.loads(msg)
                except ValueError:
                    continue
                if not isinstance(m, dict):
                    continue
                t = m.get("t")
                if isinstance(sess, ChatSession):
                    if t == "p":
                        ws.send_json({"t": "p"})
                    else:
                        sess.handle(m)
                    continue
                if t == "i" and isinstance(m.get("d"), str):
                    sess.write(m["d"])
                elif t == "r":
                    try:
                        sess.resize(int(m.get("c")), int(m.get("r")))
                    except (TypeError, ValueError):
                        pass
                elif t == "p":
                    ws.send_json({"t": "p"})
        except (WebSocketClosed, OSError):
            pass
        finally:
            sess.detach(ws)


def make_server(app: App, port: int = 0, wait: float = 0) -> ThreadingHTTPServer:
    """wait: keep trying the preferred port for a while (the old copy is still closing after an update)."""
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = None
    end = time.time() + wait
    while port and httpd is None:
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            if time.time() >= end:
                break
            time.sleep(0.3)
    if httpd is None:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    app.port = httpd.server_address[1]
    app.httpd = httpd
    return httpd


def code_fingerprint() -> str:
    """Changes whenever the GUI files change — a running older copy is then replaced."""
    h = hashlib.sha1()
    for f in [HERE / "server.py"] + sorted(p for p in STATIC.glob("*") if p.is_file()):
        try:
            h.update(f.name.encode() + f.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:12]


def state_file() -> Path:
    return config_dir() / "gui.state"


def shutdown_app(app: App):
    app._stopping = True
    time.sleep(0.2)
    app.shutdown_sessions()
    try:
        st = json.loads(state_file().read_text())
        if st.get("pid") == os.getpid():
            state_file().unlink()
    except (OSError, ValueError):
        pass
    if app.httpd:
        app.httpd.shutdown()


# ----------------------------------------------------------------------------- CLI
def read_state():
    try:
        st = json.loads(state_file().read_text())
        return st if isinstance(st, dict) else None
    except (OSError, ValueError):
        return None


def ping(st) -> bool:
    if not st:
        return False
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{st['port']}/api/ping",
                                     headers={"X-Token": st["token"], "Host": f"127.0.0.1:{st['port']}"})
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read()).get("ok") is True
    except Exception:  # noqa: BLE001
        return False


def app_url(st) -> str:
    return f"http://127.0.0.1:{st['port']}/#t={st['token']}"


def log_line(msg: str):
    """Timestamped line in gui.log (stdout/stderr of the background service go there)."""
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, file=sys.stderr, flush=True)


def install_crash_logging():
    def hook(exc_type, exc, tb):
        import traceback
        log_line("CRASH " + "".join(traceback.format_exception(exc_type, exc, tb)).strip())
    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)


def wsl_keepalive_argv(distro: str, pid: int) -> list:
    """WSL stops a distro (killing every process in it) a few seconds after its last
    terminal window closes. A hidden Windows-side `wsl.exe` that lives exactly as long
    as this service keeps it running."""
    if distro and not re.fullmatch(r"[A-Za-z0-9._-]+", distro):
        distro = ""
    # one argument string with explicit quotes (Windows PowerShell 5.1 does not quote list items)
    line = (f"-d {distro} " if distro else "") + f'-e sh -c "while kill -0 {int(pid)} 2>/dev/null; do sleep 10; done"'
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command",
            "Start-Process -WindowStyle Hidden -FilePath wsl.exe -ArgumentList '" + line.replace("'", "''") + "'"]


def start_wsl_keepalive():
    if not is_wsl() or not shutil.which("powershell.exe"):
        return
    try:
        subprocess.Popen(wsl_keepalive_argv(os.environ.get("WSL_DISTRO_NAME", ""), os.getpid()),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, cwd="/mnt/c" if os.path.isdir("/mnt/c") else None)
        log_line("WSL keep-alive started")
    except OSError as e:
        log_line(f"WSL keep-alive failed: {e}")


def serve(foreground_msg=True):
    install_crash_logging()
    store = Store()
    app = App(store)
    httpd = make_server(app, int(store.settings.get("port") or 0), wait=15 if os.environ.get("SIMAB_RESTART") else 0)
    if store.settings.get("port") != app.port:
        store.update_settings({"port": app.port})
    st = {"pid": os.getpid(), "port": app.port, "token": app.token, "version": VERSION,
          "code": code_fingerprint(), "path": str(HERE)}
    state_file().parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    os.chmod(tmp, 0o600)
    os.replace(tmp, state_file())
    exe = which("mutagen")
    if exe:
        subprocess.Popen([exe, "daemon", "start"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, env=dict(os.environ, PATH=tool_path()))
    threading.Thread(target=app.git_watch_loop, daemon=True).start()
    threading.Thread(target=app.docker_watch_loop, daemon=True).start()
    threading.Thread(target=app.start_tunnels, daemon=True).start()
    start_wsl_keepalive()
    log_line(f"started {VERSION} on 127.0.0.1:{app.port} (pid {os.getpid()})")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: threading.Thread(target=shutdown_app, args=(app,), daemon=True).start())
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    if foreground_msg:
        print(f"{APP} GUI: {app_url(st)}", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        app.shutdown_sessions()
        log_line("stopped")


def stop_running(st) -> bool:
    if not ping(st):
        return False
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{st['port']}/api/quit", data=b"{}", method="POST",
                                     headers={"X-Token": st["token"], "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:  # noqa: BLE001
        try:
            os.kill(int(st["pid"]), signal.SIGTERM)
        except (OSError, ValueError, KeyError):
            pass
    end = time.time() + 5
    while time.time() < end and ping(st):
        time.sleep(0.2)
    return True


def ensure_running(timeout=8.0):
    st = read_state()
    if ping(st):
        if st.get("code") == code_fingerprint():
            return st
        # an older version is still running in the background: replace it
        print(f"  › New version of the window ({VERSION}) — restarting it (open tabs are closed).")
        stop_running(st)
    log = config_dir() / "gui.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as lf:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve"], stdin=subprocess.DEVNULL,
                         stdout=lf, stderr=lf, start_new_session=True, cwd=os.path.expanduser("~"))
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.2)
        st = read_state()
        if ping(st):
            return st
    raise SystemExit(f"GUI did not start — see {log}")


def browser_candidates(url: str, profile: str) -> list:
    """Commands to open `url` as an app window, best first."""
    app_args = [f"--app={url}", f"--user-data-dir={profile}", "--class=simabworkspace",
                "--no-first-run", "--no-default-browser-check"]
    out = []
    if is_wsl():
        # Windows Edge/Chrome; WSL forwards 127.0.0.1 to Windows.
        out.append(["cmd.exe", "/c", "start", "", "msedge", f"--app={url}"])
        out.append(["cmd.exe", "/c", "start", "", "chrome", f"--app={url}"])
        out.append(["cmd.exe", "/c", "start", "", url])
    elif sys.platform == "darwin":
        for name in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            if os.path.isdir(f"/Applications/{name}.app"):
                out.append(["open", "-na", name, "--args"] + app_args)
        out.append(["open", url])
    else:
        for b in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                  "microsoft-edge", "microsoft-edge-stable", "brave-browser", "vivaldi"):
            if shutil.which(b):
                out.append([b] + app_args)
        if shutil.which("xdg-open"):
            out.append(["xdg-open", url])
    return out


def open_window(st):
    url = app_url(st)
    store = Store()
    custom = (store.settings.get("browser") or "").strip()
    profile = str(config_dir() / "window-profile")
    cands = []
    if custom:
        try:
            parts = shlex.split(custom)
            cands.append([a.replace("{url}", url) for a in parts] if "{url}" in custom else parts + [url])
        except ValueError:
            pass
    cands += browser_candidates(url, profile)
    for argv in cands:
        if not shutil.which(argv[0]):
            continue
        try:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, cwd="/mnt/c" if argv[0] == "cmd.exe" and os.path.isdir("/mnt/c") else None)
            print(f"  ✓ {APP} GUI opened  ({url.split('#')[0]})")
            return
        except OSError:
            continue
    print(f"  ! Could not find a browser. Open this address yourself:\n    {url}")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "open"
    if cmd == "serve":
        serve()
    elif cmd == "open":
        open_window(ensure_running())
    elif cmd == "url":
        print(app_url(ensure_running()))
    elif cmd == "status":
        st = read_state()
        print(f"files:   {VERSION} ({code_fingerprint()})  {HERE}")
        if ping(st):
            same = "up to date" if st.get("code") == code_fingerprint() else "OLD — run `simabworkspace gui` to restart it"
            print(f"running: {st.get('version', '?')} ({st.get('code', '?')})  pid {st['pid']}  "
                  f"{app_url(st).split('#')[0]}  — {same}")
        else:
            print("running: no")
    elif cmd == "stop":
        print("  ✓ GUI stopped" if stop_running(read_state()) else "  GUI is not running")
    else:
        print(__doc__ or "usage: server.py [open|serve|url|status|stop]")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv) or 0)
