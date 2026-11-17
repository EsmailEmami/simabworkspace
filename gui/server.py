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
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

IS_WIN = os.name == "nt"          # native Windows (Python for Windows + Git for Windows), not WSL
if not IS_WIN:
    import fcntl
    import termios
SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)
SIGHUP = getattr(signal, "SIGHUP", signal.SIGTERM)

APP = "simabworkspace"
VERSION = "1.35.0"
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
    p = p if b in p.split(os.pathsep) else b + os.pathsep + p
    if IS_WIN:                  # Git for Windows' ssh, ssh-keygen, curl, tar — after Windows' own
        for c in git_bash_candidates():
            usr = os.path.join(os.path.dirname(os.path.dirname(c)), "usr", "bin")
            if os.path.isdir(usr) and usr not in p.split(os.pathsep):
                p += os.pathsep + usr
                break
    return p


def which(name: str):
    return shutil.which(name, path=tool_path())


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def installed_script():
    """The installed bash script (on Windows not the simabworkspace.cmd launcher next to it)."""
    if IS_WIN:
        f = bin_dir() / "simabworkspace"
        return str(f) if f.is_file() else None
    return which("simabworkspace")


def git_bash_candidates(env=None) -> list:
    """Where Git for Windows keeps bash.exe (never C:\\Windows\\System32\\bash.exe — that is WSL)."""
    env = os.environ if env is None else env
    out = []
    git = shutil.which("git", path=env.get("PATH"))
    if git:                                     # …\Git\cmd\git.exe or …\Git\bin\git.exe
        root = os.path.dirname(os.path.dirname(git))
        out += [os.path.join(root, "bin", "bash.exe"), os.path.join(root, "usr", "bin", "bash.exe")]
    for base in (env.get("ProgramFiles"), env.get("ProgramW6432"), env.get("ProgramFiles(x86)"),
                 os.path.join(env.get("LOCALAPPDATA") or "", "Programs") if env.get("LOCALAPPDATA") else None):
        if base:
            out.append(os.path.join(base, "Git", "bin", "bash.exe"))
    return out


def bash_exe() -> str:
    """bash for the simabworkspace script: Git Bash on Windows, the normal bash elsewhere."""
    if IS_WIN:
        for c in git_bash_candidates():
            if os.path.isfile(c):
                return c
        raise ApiError(400, "Git for Windows is needed (it brings bash): winget install --id Git.Git -e")
    return which("bash") or "/bin/bash"


def msys_to_win(path: str) -> str:
    """Git Bash writes paths like /c/Users/me/.ssh/id_ed25519; Windows Python needs C:/Users/…"""
    if IS_WIN and path:
        m = re.fullmatch(r"/([A-Za-z])(/.*)?", path)
        if m:
            return m.group(1).upper() + ":" + (m.group(2) or "/")
    return path


def win_python(gui=False) -> str:
    """Windows: python.exe (console, for the hidden background server) or pythonw.exe (no window)."""
    exe = Path(sys.executable)
    want = "pythonw.exe" if gui else "python.exe"
    alt = exe.with_name(want)
    return str(alt) if alt.is_file() else str(exe)


CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def detached_kwargs() -> dict:
    """Popen options for a background process that outlives us (and on Windows has a hidden console,
    so the ssh/git it runs never flash a window)."""
    if IS_WIN:
        return {"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


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
                      c.get("SSH_PORT") or os.environ.get("SIMAB_PORT", "22"), msys_to_win(c.get("SSH_KEY", "")))


# ----------------------------------------------------------------------------- SQLite
# One simab.db per workspace (the laptop app: ~/.config/simabworkspace/simab.db; the web
# version: one per signed-in user@server, plus gate.db for sign-ins). Older versions kept
# JSON files: they are imported once and renamed to *.migrated.
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects (name TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS manual (name TEXT PRIMARY KEY, pos INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, kind TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', extra TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS activity_project ON activity (project, id);
CREATE TABLE IF NOT EXISTS cost (day TEXT NOT NULL, project TEXT NOT NULL, usd REAL NOT NULL, PRIMARY KEY (day, project));
CREATE TABLE IF NOT EXISTS web_sessions (hash TEXT PRIMARY KEY, ws TEXT NOT NULL, exp REAL NOT NULL, last REAL NOT NULL, ip TEXT NOT NULL DEFAULT '');
"""
_DBS = {}
_DBS_LOCK = threading.Lock()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), timeout=15, isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=15000")
        self.conn.executescript(SCHEMA)
        for f in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            try:
                os.chmod(f, 0o600)
            except OSError:
                pass

    def rows(self, sql: str, args=()) -> list:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def run(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def get(self, key: str, default=None):
        r = self.rows("SELECT value FROM state WHERE key = ?", (key,))
        if not r:
            return default
        try:
            return json.loads(r[0][0])
        except ValueError:
            return default

    def put(self, key: str, value, conn=None):
        (conn or self).execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                               (key, json.dumps(value, ensure_ascii=False)))

    def execute(self, sql, args=()):
        return self.run(sql, args)

    def import_once(self, name: str, legacy, load) -> bool:
        """Import an old JSON file once (load(path) does the work); then rename it *.migrated."""
        with self.lock:
            if self.rows("SELECT 1 FROM meta WHERE key = ?", ("imported:" + name,)):
                return False
            done = False
            if legacy is not None and Path(legacy).is_file():
                load(Path(legacy))
                done = True
                try:
                    os.replace(legacy, str(legacy) + ".migrated")
                except OSError:
                    pass
            self.run("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("imported:" + name, str(time.time())))
            return done


def open_db(path: Path) -> Database:
    key = str(Path(path).expanduser().resolve())
    with _DBS_LOCK:
        db = _DBS.get(key)
        if db is None:
            db = _DBS[key] = Database(Path(key))
        return db


def db_and_legacy(path, default_legacy: str):
    """Constructors accept the database file, or (older callers, tests) the old JSON file next to it."""
    path = Path(path) if path else config_dir() / "simab.db"
    if path.suffix in (".json", ".jsonl"):
        return open_db(path.with_name("simab.db")), path
    return open_db(path), path.with_name(default_legacy)


# ----------------------------------------------------------------------------- GUI store (settings, projects)
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
    "chatModel": "opus",         # Opus 5.5 (the latest Opus)
    "chatEffort": "medium",      # thinking effort: low, medium, high, xhigh, max
    "copyOnSelect": True,
    "bellNotify": True,
    "confirmClose": True,
    "scrollback": 10000,
    "browser": "",               # command used to open the window (empty = auto)
    "gitWatch": True,            # git badges: check every repository in the background
    "gitAutoFetch": True,        # …and `git fetch` every few minutes so "behind" is current
    "dockerWatch": True,         # check the projects' docker containers; tell me when one crashes
    "port": 0,                   # remembered so the window keeps the same address
    "starred": [],               # starred commands (Ctrl+Shift+P) and project ⋯ menu entries, shown first
}

SETTING_CHOICES = {
    "persianView": ("auto", "off"),
    "composer": ("always", "claude", "never"),
    "chatMode": ("default", "acceptEdits", "plan", "auto"),
    "chatEffort": ("low", "medium", "high", "xhigh", "max"),
}
SETTING_RANGES = {"fontSize": (9, 32), "scrollback": (1000, 100000), "port": (0, 65535)}
WHERE = ("local", "server")
WIN_RE = re.compile(r"[a-z0-9]{4,16}")
WIN_TTL = 180          # an extra window that has not said hello for this long is gone (hidden windows tick slowly)
WIN_GRACE = 6          # after "closing": seconds to come back (a reload) before its tabs go home


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
        self.db, legacy = db_and_legacy(path, "gui.json")
        self.path = self.db.path
        self.lock = threading.RLock()
        imported = {}

        def load(f):
            try:
                d = json.loads(f.read_text())
                imported.update(d if isinstance(d, dict) else {})
            except (OSError, ValueError):
                pass
        self.db.import_once("gui.json", legacy, load)
        self.data = self._load(imported or None)
        if imported:
            self.save()

    def _read(self) -> dict:
        d = {"settings": self.db.get("settings", {}), "lastTabs": self.db.get("lastTabs", [])}
        d["projects"] = {}
        for name, data in self.db.rows("SELECT name, data FROM projects"):
            try:
                d["projects"][name] = json.loads(data)
            except ValueError:
                pass
        d["manual"] = []
        for (data,) in self.db.rows("SELECT data FROM manual ORDER BY pos"):
            try:
                d["manual"].append(json.loads(data))
            except ValueError:
                pass
        return d

    def _load(self, imported: dict = None) -> dict:
        d = imported if imported is not None else self._read()
        if not isinstance(d.get("projects"), dict):
            d["projects"] = {}
        if not isinstance(d.get("manual"), list):
            d["manual"] = []
        s = dict(DEFAULT_SETTINGS)
        s.update({k: v for k, v in (d.get("settings") or {}).items() if k in DEFAULT_SETTINGS})
        d["settings"] = s
        d.setdefault("projects", {})
        d.setdefault("manual", [])
        d.setdefault("lastTabs", [])
        return d

    def save(self):
        with self.lock, self.db.tx() as c:
            self.db.put("settings", self.data["settings"], c)
            self.db.put("lastTabs", self.data.get("lastTabs") or [], c)
            c.execute("DELETE FROM projects")
            c.executemany("INSERT INTO projects (name, data) VALUES (?, ?)",
                          [(k, json.dumps(v, ensure_ascii=False)) for k, v in self.data["projects"].items()])
            c.execute("DELETE FROM manual")
            c.executemany("INSERT INTO manual (name, pos, data) VALUES (?, ?, ?)",
                          [(m.get("name"), i, json.dumps(m, ensure_ascii=False)) for i, m in enumerate(self.data["manual"])
                           if isinstance(m, dict) and m.get("name")])

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
                if isinstance(d, list):
                    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                        raise ApiError(400, f"invalid value for {k}")
                    v = list(dict.fromkeys(x.strip()[:200] for x in v if x.strip()))[:300]
                elif isinstance(d, bool):
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
    def set_chat_prefs(self, project: str, mode=None, model=None, effort=None):
        with self.lock:
            pp = self.data["projects"].setdefault(project or "", {})
            if mode in CHAT_MODES:
                pp["chatMode"] = mode
                self.data["settings"]["chatMode"] = mode
            if model is not None and re.fullmatch(r"[A-Za-z0-9.\[\]_-]{0,60}", model):
                pp["chatModel"] = model
                self.data["settings"]["chatModel"] = model
            if effort in CHAT_EFFORTS:
                pp["chatEffort"] = effort
                self.data["settings"]["chatEffort"] = effort
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

    def set_title(self, project: str, title) -> str:
        """The name shown in the list ("" = the project's own name). The real name stays: sync
        sessions, tabs and settings are keyed by it."""
        title = " ".join(clean_str(title, 200, "title").split())
        if len(title) > 80:
            raise ApiError(400, "the name is too long (80 characters at most)")
        if title == project:
            title = ""
        with self.lock:
            self.data["projects"].setdefault(project, {})["title"] = title
            self.save()
        return title

    def set_starter(self, project: str, text) -> str:
        """The text every new Claude chat of this project starts with (the user adds their request)."""
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ApiError(400, "invalid starter message")
        text = text.strip()
        if len(text) > 20000:
            raise ApiError(400, "the starter message is too long")
        with self.lock:
            self.data["projects"].setdefault(project, {})["starter"] = text
            self.save()
        return text

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
            (self.data["projects"].get(name) or {}).pop("title", None)
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


FALLBACK_IGNORES = ["node_modules", ".angular", "dist", "tmp", "docker/data", ".dart_tool", "build", ".next", ".nuxt",
                    ".cache", "__pycache__", ".venv", ".gradle"]


def default_ignores() -> list:
    """The script's DEFAULT_IGNORES (what new projects never sync)."""
    for f in (HERE.parent / "simabworkspace.sh", installed_script()):
        try:
            m = re.search(r'^DEFAULT_IGNORES="([^"]*)"', Path(f).read_text(), re.M) if f else None
        except OSError:
            m = None
        if m:
            return [x for x in m.group(1).split(",") if x]
    return list(FALLBACK_IGNORES)


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
        p["starter"] = (store_projects.get(p["name"]) or {}).get("starter") or ""
        p["title"] = (store_projects.get(p["name"]) or {}).get("title") or ""
    out.sort(key=lambda p: (p["title"] or p["name"]).lower())
    return out


# ----------------------------------------------------------------------------- command building
def remote_path_expr(p: str) -> str:
    if not p or p == "~":
        return '"$HOME"'
    if p.startswith("~/"):
        return '"$HOME"/' + shlex.quote(p[2:])
    return shlex.quote(p)


def control_path() -> str:
    """Where ssh keeps the shared connection. ssh refuses a ControlPath with a space in it
    ("keyword controlpath extra arguments at end of line" — e.g. a Mac home folder with a
    space), and a socket path must stay short (104 bytes on macOS, ssh adds 17 while making
    it). Then a private folder in /tmp is used; "" = no shared connection at all."""
    d = os.path.join(os.path.expanduser("~"), ".ssh")
    if not re.search(r"\s", d) and len(d) <= 45:
        return os.path.join(d, "sw-%C")
    try:
        uid = os.getuid()
        d = f"/tmp/sw-{uid}"
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
        if st.st_uid != uid or not stat.S_ISDIR(st.st_mode):
            return ""                      # someone else's folder: do not share connections through it
        os.chmod(d, 0o700)
    except (OSError, AttributeError):
        return ""
    return os.path.join(d, "sw-%C")


def posix_sh(cmd: str) -> str:
    """Run a command line with /bin/sh on the server, whatever the user's login shell is.
    zsh stops at a glob that matches nothing ("no matches found: …/*/.git"), fish has its
    own syntax — our commands are written for sh."""
    return "sh -c " + shlex.quote(cmd)


def ssh_base(conf: ServerConf, multiplex=True, batch=False, forwards=False) -> list:
    """forwards=False (background work: chat, git, lists): port forwardings from the user's
    ~/.ssh/config (RemoteForward…) are not opened — they only fail ("remote port forwarding
    failed for listen port …") and end up in the chat. Terminals and tunnels keep them."""
    a = ["ssh", "-p", str(conf.port),
         "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
         "-o", "StrictHostKeyChecking=accept-new"]
    if conf.key and os.path.isfile(os.path.expanduser(conf.key)):
        a += ["-i", os.path.expanduser(conf.key)]
    cp = control_path() if multiplex and not IS_WIN else ""      # Windows OpenSSH has no ControlMaster
    if cp:
        a += ["-o", "ControlMaster=auto", "-o", f"ControlPath={cp}", "-o", "ControlPersist=10m"]
    if batch:
        a += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=6"]
    if not forwards:
        a += ["-o", "ClearAllForwardings=yes"]
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
    sh = (settings.get("localShell") or ("" if IS_WIN else os.environ.get("SHELL")) or "").strip()
    try:
        ok = bool(sh) and which(shlex.split(sh, posix=not IS_WIN)[0])
    except ValueError:
        ok = False
    if ok:
        return sh
    if IS_WIN:
        return which("pwsh") or which("powershell") or "powershell.exe"
    return which("bash") or "/bin/sh"


def local_shell_argv(sh: str, inner: str = None) -> list:
    """The local shell (interactive) or `inner` run by it — bash style, or PowerShell / cmd on Windows."""
    argv = shlex.split(sh, posix=not IS_WIN)
    argv = [a.strip('"') for a in argv] if IS_WIN else argv
    name = re.split(r"[\\/]", argv[0])[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name in ("powershell", "pwsh"):
        return argv + (["-NoLogo"] if inner is None else ["-NoLogo", "-Command", inner])
    if name == "cmd":
        return argv + ([] if inner is None else ["/c", inner])
    return argv + (["-l"] if inner is None else ["-lic", inner])


CLAUDE_MODES = {"new": "", "continue": " --continue", "resume": " --resume"}

# If the `claude` CLI is not installed on the server, use the binary that ships
# inside the VS Code extension (newest version). On failure, keep the message on
# screen until Enter, so the tab does not just say "[exited]".
# Where claude may be when it is not on the PATH of a non-interactive login shell (zsh does
# not read ~/.zshrc there, and that is where installers usually add ~/.local/bin). sh syntax:
# the scripts below always run it with sh, whatever the user's shell is.
CLAUDE_FIND = (
    'if ! command -v claude >/dev/null 2>&1; then '
    'for d in "$HOME/.local/bin" "$HOME/.claude/local" "$HOME/.npm-global/bin" "$HOME/.bun/bin" "$HOME/.volta/bin" '
    '/usr/local/bin /opt/homebrew/bin /home/linuxbrew/.linuxbrew/bin '
    '"$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -n 1)" '
    '"$(ls -d "$HOME"/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary '
    '"$HOME"/.vscode-server-insiders/extensions/anthropic.claude-code-*/resources/native-binary '
    '"$HOME"/.cursor-server/extensions/anthropic.claude-code-*/resources/native-binary 2>/dev/null | sort -V | tail -n 1)"; '
    'do if [ -n "$d" ] && [ -x "$d/claude" ]; then PATH="$d:$PATH"; export PATH; break; fi; done; fi; '
)
CLAUDE_MISSING = (
    'command -v claude >/dev/null 2>&1 || { echo "claude was not found on the server (looked in PATH, ~/.local/bin, '
    '~/.claude/local, npm, bun, volta, Homebrew, nvm, VS Code). Install it there: curl -fsSL https://claude.ai/install.sh | bash '
    '— or set the full path of claude in Settings." >&2; exit 127; }; '
)
CLAUDE_FAIL = (
    ' || { c=$?; printf "\n\033[33mclaude stopped (exit %s).\033[0m\n" "$c"; '
    'command -v claude >/dev/null 2>&1 || printf "claude was not found on this machine. Install it: '
    'curl -fsSL https://claude.ai/install.sh | bash\n"; printf "Press Enter to close. "; read -r _; exit "$c"; }'
)


def claude_inner(claude_cmd: str, mode: str, resume: str = "") -> str:
    """resume: a conversation id → open exactly that conversation."""
    flags = (" --resume " + resume) if resume else CLAUDE_MODES.get(mode, "")
    return "sh -c " + shlex.quote(CLAUDE_FIND + (claude_cmd or "claude") + flags + CLAUDE_FAIL)
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
        argv = ssh_base(c, multiplex=bool(s.get("sshMultiplex")), forwards=True) + ["-t", c.target,
                                                                     remote_script(rpath, inner, persist)]
        cwd = os.path.expanduser("~")
    else:
        if kind in ("add-local", "add-server"):
            script = HERE.parent / "simabworkspace.sh"
            script = str(script) if script.is_file() else installed_script()
            if not script:
                raise ApiError(400, "simabworkspace script not found")
            argv = [bash_exe(), script]
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
            script = str(script) if script.is_file() else installed_script()
            if not script:
                raise ApiError(400, "simabworkspace script not found")
            argv = [bash_exe(), script, "git", pname, "on"]
        elif kind in ("sync-info", "sync-monitor"):
            exe = which("mutagen")
            if not exe:
                raise ApiError(400, "mutagen not found")
            argv = [exe, "sync", "list" if kind == "sync-info" else "monitor"]
            argv += (["--long"] if kind == "sync-info" else []) + [pname]
        else:
            argv = local_shell_argv(local_shell(s), inner)
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


# ----------------------------------------------------------------------------- Windows terminals (ConPTY)
# Native Windows has no pty: Windows 10 1809+ has a "pseudo console" instead. ConPty starts
# a program inside one; .read()/.write()/.resize() work like the pty master on Linux.
def win_env_block(env: dict) -> str:
    """CreateProcessW environment: KEY=value\\0 … \\0, sorted case-insensitively."""
    items = sorted(((str(k), str(v)) for k, v in env.items() if k and "=" not in str(k)[1:]), key=lambda kv: kv[0].upper())
    return "".join(f"{k}={v}\0" for k, v in items) + "\0"


def win_command_line(argv: list) -> str:
    exe = shutil.which(argv[0], path=tool_path()) or argv[0]
    return subprocess.list2cmdline([exe] + list(argv[1:]))


class WinProc:
    """The little of subprocess.Popen that Session uses, for a process started by ConPty."""
    def __init__(self, k32, handle, pid):
        self.k32, self.handle, self.pid, self.returncode = k32, handle, pid, None

    def poll(self):
        import ctypes
        if self.returncode is None and self.k32.WaitForSingleObject(self.handle, 0) == 0:
            code = ctypes.c_ulong(0)
            self.k32.GetExitCodeProcess(self.handle, ctypes.byref(code))
            self.returncode = int(code.value)
        return self.returncode

    def wait(self, timeout=None):
        ms = 0xFFFFFFFF if timeout is None else int(timeout * 1000)
        if self.returncode is None and self.k32.WaitForSingleObject(self.handle, ms) != 0:
            raise subprocess.TimeoutExpired("conpty", timeout)
        return self.poll()

    def kill(self):
        if self.poll() is None:
            self.k32.TerminateProcess(self.handle, 1)

    terminate = kill

    def send_signal(self, sig):
        self.kill()


class ConPty:
    def __init__(self, argv, cwd, env, cols, rows):
        import ctypes
        import msvcrt
        from ctypes import wintypes as wt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not hasattr(k32, "CreatePseudoConsole"):
            raise OSError("terminals need Windows 10 (version 1809) or newer")
        self.k32 = k32

        class COORD(ctypes.Structure):
            _fields_ = [("X", wt.SHORT), ("Y", wt.SHORT)]

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                        ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                        ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                        ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                        ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
                        ("hStdError", wt.HANDLE)]

        class STARTUPINFOEXW(ctypes.Structure):
            _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]

        self.COORD = COORD
        k32.CreatePipe.argtypes = [ctypes.POINTER(wt.HANDLE), ctypes.POINTER(wt.HANDLE), ctypes.c_void_p, wt.DWORD]
        k32.CreatePseudoConsole.argtypes = [COORD, wt.HANDLE, wt.HANDLE, wt.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        k32.CreatePseudoConsole.restype = ctypes.c_long
        k32.ResizePseudoConsole.argtypes = [ctypes.c_void_p, COORD]
        k32.ResizePseudoConsole.restype = ctypes.c_long
        k32.ClosePseudoConsole.argtypes = [ctypes.c_void_p]
        k32.ClosePseudoConsole.restype = None
        k32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_size_t)]
        k32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                                  ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
        k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        k32.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD,
                                       ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(STARTUPINFOEXW), ctypes.POINTER(PROCESS_INFORMATION)]
        k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
        k32.WaitForSingleObject.restype = wt.DWORD
        k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_ulong)]
        k32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
        k32.CloseHandle.argtypes = [wt.HANDLE]

        def fail(what):
            raise OSError(f"{what} failed (Windows error {ctypes.get_last_error()})")

        in_r, in_w, out_r, out_w = wt.HANDLE(), wt.HANDLE(), wt.HANDLE(), wt.HANDLE()
        if not k32.CreatePipe(ctypes.byref(in_r), ctypes.byref(in_w), None, 0) or \
                not k32.CreatePipe(ctypes.byref(out_r), ctypes.byref(out_w), None, 0):
            fail("CreatePipe")
        self.hpc = ctypes.c_void_p()
        hr = k32.CreatePseudoConsole(COORD(cols, rows), in_r, out_w, 0, ctypes.byref(self.hpc))
        # the pseudo console has its own copies of its pipe ends
        k32.CloseHandle(in_r)
        k32.CloseHandle(out_w)
        if hr != 0:
            k32.CloseHandle(in_w)
            k32.CloseHandle(out_r)
            raise OSError(f"CreatePseudoConsole failed (0x{hr & 0xFFFFFFFF:08x})")
        size = ctypes.c_size_t(0)
        k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attrs = ctypes.create_string_buffer(size.value)
        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.StartupInfo.dwFlags = 0x00000100        # STARTF_USESTDHANDLES with none: use the pseudo console,
        si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)    # not our own (redirected) handles
        pi = PROCESS_INFORMATION()
        try:
            if not k32.InitializeProcThreadAttributeList(si.lpAttributeList, 1, 0, ctypes.byref(size)):
                fail("InitializeProcThreadAttributeList")
            if not k32.UpdateProcThreadAttribute(si.lpAttributeList, 0, 0x00020016, self.hpc,  # PSEUDOCONSOLE
                                                 ctypes.sizeof(ctypes.c_void_p), None, None):
                fail("UpdateProcThreadAttribute")
            cmdline = ctypes.create_unicode_buffer(win_command_line(argv))
            block = ctypes.create_unicode_buffer(win_env_block(env))
            if not k32.CreateProcessW(None, cmdline, None, None, False,
                                      0x00080000 | 0x00000400,    # EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT
                                      ctypes.cast(block, ctypes.c_void_p), cwd, ctypes.byref(si), ctypes.byref(pi)):
                fail("starting " + argv[0])
        except OSError:
            k32.ClosePseudoConsole(self.hpc)
            k32.CloseHandle(in_w)
            k32.CloseHandle(out_r)
            raise
        finally:
            k32.DeleteProcThreadAttributeList(si.lpAttributeList)
        k32.CloseHandle(pi.hThread)
        self.proc = WinProc(k32, pi.hProcess, pi.dwProcessId)
        self.rfd = msvcrt.open_osfhandle(out_r.value, os.O_RDONLY | os.O_BINARY)
        self.wfd = msvcrt.open_osfhandle(in_w.value, os.O_WRONLY | os.O_BINARY)
        self._closed = False
        self._lock = threading.Lock()
        # When the program ends the pseudo console must be closed, or reading never sees the end.
        threading.Thread(target=self._close_when_done, daemon=True).start()

    def _close_when_done(self):
        try:
            self.proc.wait()
        except subprocess.TimeoutExpired:
            pass
        self.close_console()

    def close_console(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.k32.ClosePseudoConsole(self.hpc)

    def read(self, n=65536) -> bytes:
        try:
            return os.read(self.rfd, n)
        except OSError:
            return b""

    def write(self, data: bytes) -> int:
        return os.write(self.wfd, data)

    def resize(self, cols, rows):
        with self._lock:
            if not self._closed:
                self.k32.ResizePseudoConsole(self.hpc, self.COORD(cols, rows))

    def close(self):
        self.close_console()
        for fd in (self.rfd, self.wfd):
            try:
                os.close(fd)
            except OSError:
                pass


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
    win = ""          # the window it is shown in ("" = the main window)
    moved = 0.0       # when it last changed window

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
        if IS_WIN:
            try:
                master = ConPty(self.built["argv"], self.built["cwd"], env, self.cols, self.rows)
            except OSError as e:
                raise ApiError(500, f"could not start: {e}")
            proc = master.proc
        else:
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
                data = fd.read(65536) if IS_WIN else os.read(fd, 65536)
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
            self._signal(proc, SIGKILL)
            code = proc.wait()
        try:
            fd.close() if IS_WIN else os.close(fd)
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
                n = fd.write(data) if IS_WIN else os.write(fd, data)
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
                fd.resize(cols, rows) if IS_WIN else set_winsize(fd, rows, cols)
            except OSError:
                pass

    @staticmethod
    def _signal(proc, sig):
        if IS_WIN:                    # no signals: end it (the whole tree for a normal process)
            try:
                if isinstance(proc, WinProc):
                    proc.kill()
                else:
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=10)
                    if proc.poll() is None:
                        proc.kill()
            except (OSError, subprocess.SubprocessError):
                pass
            return
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
            for sig, wait in ((signal.SIGTERM, 3), (SIGKILL, 3)):
                time.sleep(wait)
                if proc.poll() is not None:
                    return
                self._signal(proc, sig)
        threading.Thread(target=later, daemon=True).start()

    def kill(self, wait=False):
        proc = self.proc
        if proc and proc.poll() is None:
            self._signal(proc, SIGHUP)

            def later():
                time.sleep(1.5)
                if proc.poll() is None:
                    self._signal(proc, SIGKILL)
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
            self._signal(proc, SIGHUP)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._signal(proc, SIGKILL)
        with self.lock:
            self._emit("\r\n\x1b[2m──── restart ────\x1b[0m\r\n")
            self._broadcast({"t": "r"})
        self.start()

    def info(self) -> dict:
        return {"id": self.id, "title": self.title, "spec": self.spec, "url": self.url,
                "alive": self.alive, "exitCode": self.exit_code, "started": self.started,
                "runs": self.runs, "win": self.win, "moved": self.moved}


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


DEFAULT_CHAT_MODEL = "opus"           # the latest Opus (Opus 5.5) — "default" told you nothing
CHAT_EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_CHAT_EFFORT = "medium"


def chat_model(model) -> str:
    """'' or 'default' (older versions) → the default model."""
    return model if isinstance(model, str) and model and model != "default" else DEFAULT_CHAT_MODEL


def chat_effort(effort) -> str:
    return effort if effort in CHAT_EFFORTS else DEFAULT_CHAT_EFFORT


def chat_args(mode: str, model: str = "", resume: str = "", effort: str = "") -> list:
    a = ["--output-format", "stream-json", "--verbose", "--input-format", "stream-json",
         "--permission-prompt-tool=stdio", "--include-partial-messages", "--replay-user-messages",
         "--setting-sources=user,project,local", "--permission-mode", mode if mode in CHAT_MODES else "default"]
    a += ["--model", chat_model(model), "--effort", chat_effort(effort)]
    if resume:
        a += ["--resume", resume]
    return a


def chat_remote_script(rpath: str, claude_cmd: str, args: list) -> str:
    cmd = claude_cmd or "claude"
    inner = (CLAUDE_FIND + (CLAUDE_MISSING if cmd == "claude" else "") + "exec " + cmd + " "
             + " ".join(shlex.quote(x) for x in args))
    # the login shell gives the user's environment; our part runs with sh (zsh/fish differ)
    return (f"cd {remote_path_expr(rpath)} || exit 97; "
            f'exec "${{SHELL:-/bin/bash}}" -lc {shlex.quote("exec sh -c " + shlex.quote(inner))}')


# Claude keeps running on the server when the connection drops. A small program
# (the relay) starts it detached from SSH: Claude reads a FIFO and writes a log
# file, so the work goes on; the next connection attaches to the same Claude and
# reads the log from where it left off (no second Claude on the same conversation).
#   ~/.cache/simabworkspace/chats/<key>/  in (FIFO) · out.log · err.log · pid · exit · seen
# relay start KEY OFFSET SCRIPT   attach to the running Claude, or start SCRIPT
# relay attach KEY OFFSET         attach only (reconnect); nothing running → exit
# relay kill KEY                  stop it and remove its folder (the tab was closed)
# A Claude nobody is attached to that has written nothing for 30 minutes is stopped.
CHAT_RELAY_PY = r'''
import os, sys, json, time, signal, shutil, subprocess, threading
mode, key = sys.argv[1], sys.argv[2]
base = os.path.expanduser("~/.cache/simabworkspace/chats")
d = os.path.join(base, key)
IDLE = 1800
DM = []                                  # the daemon, when this relay started it (reaped here)
def P(n):
    return os.path.join(d, n)
def pid_alive(folder=None):
    if folder is None and DM and DM[0].poll() is not None:
        return 0
    try:
        pid = int(open(os.path.join(folder or d, "pid")).read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return 0
def claude_status(dpid):
    """busy / idle of the Claude our daemon runs (Claude Code writes ~/.claude/sessions/<pid>.json)."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return ""
    for n in names:
        if not n.isdigit():
            continue
        try:
            st = open("/proc/%s/stat" % n).read()
            if int(st[st.rindex(")") + 2:].split()[1]) != dpid:
                continue
        except (OSError, ValueError, IndexError):
            continue
        try:
            return str(json.load(open(os.path.expanduser("~/.claude/sessions/%s.json" % n))).get("status") or "")
        except (OSError, ValueError):
            return ""
    return ""
def unanswered():
    """Questions / permission requests of the current turn nobody answered yet (a reopened app
    did not see them: Claude would wait for ever). Answers are noted in answers.log."""
    reqs = {}
    try:
        with open(P("out.log"), "rb") as f:
            for raw in f:
                if b'"type":"result"' in raw:
                    reqs.clear()                 # a finished turn has nothing open any more
                elif b'"control_request"' in raw and b'can_use_tool' in raw:
                    try:
                        e = json.loads(raw)
                    except ValueError:
                        continue
                    if e.get("type") == "control_request" and e.get("request_id"):
                        reqs[e["request_id"]] = e
                elif b'control_cancel_request' in raw:
                    try:
                        reqs.pop(json.loads(raw).get("request_id"), None)
                    except ValueError:
                        pass
    except OSError:
        return []
    try:
        with open(P("answers.log"), "rb") as f:
            for raw in f:
                try:
                    reqs.pop((json.loads(raw).get("response") or {}).get("request_id"), None)
                except ValueError:
                    pass
    except OSError:
        pass
    return list(reqs.values())
def write(n, text):
    try:
        with open(P(n + ".tmp"), "w") as f:
            f.write(text)
        os.replace(P(n + ".tmp"), P(n))
    except OSError:
        pass
def size(n):
    try:
        return os.path.getsize(P(n))
    except OSError:
        return 0
def touch(n):
    try:
        with open(P(n), "a"):
            os.utime(P(n), None)
    except OSError:
        pass
def say(o):
    sys.stdout.buffer.write((json.dumps(o) + "\n").encode())
    sys.stdout.buffer.flush()
def exit_code():
    try:
        return int(open(P("exit")).read().strip())
    except (OSError, ValueError):
        return -1
def others(skip=0):
    """Other app connections on this Claude (the same chat open on a laptop and a tablet):
    closing the tab on one of them must not stop it for the other."""
    n = 0
    try:
        pids = os.listdir(P("clients"))
    except OSError:
        return 0
    for c in pids:
        try:
            pid = int(c)
            if pid in (os.getpid(), skip):
                continue
            os.kill(pid, 0)
            if os.path.isdir("/proc") and b"_sw_relay" not in open("/proc/%d/cmdline" % pid, "rb").read():
                raise OSError
            n += 1
        except (OSError, ValueError):
            try:
                os.remove(os.path.join(P("clients"), c))
            except OSError:
                pass
    return n
def leave():
    try:
        os.remove(os.path.join(P("clients"), str(os.getpid())))
    except OSError:
        pass

if mode == "daemon":                     # owns Claude; runs detached from any SSH
    fd = os.open(P("in"), os.O_RDWR)     # read-write: Claude never sees end-of-input
    out, err = open(P("out.log"), "ab"), open(P("err.log"), "ab")
    child = subprocess.Popen(["sh", "-c", sys.argv[3]], stdin=fd, stdout=out, stderr=err, start_new_session=True)
    stopping = [0]
    def term(*_):
        stopping[0] = stopping[0] or time.time()
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except OSError:
            pass
    signal.signal(signal.SIGTERM, term)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    while True:
        try:
            code = child.wait(timeout=20)
            break
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        def age(n):
            try:
                return now - os.path.getmtime(P(n))
            except OSError:
                return 1e9
        if stopping[0] and now - stopping[0] > 10:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
        elif not stopping[0] and age("seen") > IDLE and age("out.log") > IDLE:
            term()
    with open(P("exit.tmp"), "w") as f:
        f.write(str(code))
    os.replace(P("exit.tmp"), P("exit"))
    sys.exit(0)

if mode == "kill":                       # argv[3]: the closing tab's relay (not counted), -1 = stop anyway
    skip = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].lstrip("-").isdigit() else 0
    if skip >= 0 and others(skip):
        sys.exit(0)
    pid = pid_alive()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        end = time.time() + 15
        while pid_alive() and time.time() < end:
            time.sleep(0.2)
    shutil.rmtree(d, ignore_errors=True)
    sys.exit(0)

os.makedirs(d, mode=0o700, exist_ok=True)
off = int(sys.argv[3])
reopened = off < 0                       # a tab of a restarted app: it has seen nothing of this Claude
conv = sys.argv[5] if mode == "start" and len(sys.argv) > 5 else ""
pid = pid_alive()
if not pid and conv:
    # this conversation may already have a Claude working on it (opened again after the app
    # restarted, from History, from the web version…): use that one, never a second Claude
    try:
        names = sorted(os.listdir(base))
    except OSError:
        names = []
    for k in names:
        o = os.path.join(base, k)
        if k == key:
            continue
        try:
            same = open(os.path.join(o, "conv")).read().strip() == conv
        except OSError:
            same = False
        if same and pid_alive(o):
            try:
                os.rmdir(d)
            except OSError:
                pass
            key, d = k, o
            pid = pid_alive()
            off = size("out.log")          # only what comes next; earlier messages come from the history
            break
if pid:
    state = "attached"
elif mode == "attach":
    if not (os.path.exists(P("exit")) and 0 <= off < size("out.log")):
        say({"type": "_sw_relay", "state": "exit", "code": exit_code(), "off": off})
        sys.exit(0)
    state = "replay"                     # it ended while we were away: the rest of its output
else:
    try:                                 # old folders of finished conversations
        for k in os.listdir(base):
            o = os.path.join(base, k)
            if k != key and time.time() - os.path.getmtime(o) > 3 * 86400 and not os.path.exists(os.path.join(o, "pid")):
                shutil.rmtree(o, ignore_errors=True)
    except OSError:
        pass
    for n in ("out.log", "err.log", "exit", "pid"):
        try:
            os.remove(P(n))
        except OSError:
            pass
    if not os.path.exists(P("in")):
        os.mkfifo(P("in"), 0o600)
    touch("out.log")
    touch("err.log")
    touch("seen")
    if conv:
        write("conv", conv)
    dm = subprocess.Popen([sys.executable, "-c", os.environ["SW_RELAY"], "daemon", key, sys.argv[4]],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          start_new_session=True, close_fds=True)
    DM.append(dm)
    with open(P("pid.tmp"), "w") as f:
        f.write(str(dm.pid))
    os.replace(P("pid.tmp"), P("pid"))
    state, off = "started", 0
if off < 0 or off > size("out.log"):
    off = size("out.log")
touch("seen")
if state != "replay":
    try:
        os.makedirs(P("clients"), exist_ok=True)
        open(os.path.join(P("clients"), str(os.getpid())), "w").close()
    except OSError:
        pass
say({"type": "_sw_relay", "state": state, "off": off, "key": key, "pid": os.getpid(),
     "status": claude_status(pid) if state == "attached" else ""})
if state == "attached" and reopened:
    for q in unanswered():
        say({"type": "_sw_relay", "state": "pending", "event": q})

def errors(pos):
    try:
        f = open(P("err.log"), "rb")
        f.seek(pos)
        while True:
            b = f.read(65536)
            if b:
                sys.stderr.buffer.write(b)
                sys.stderr.buffer.flush()
            else:
                time.sleep(0.3)
    except (OSError, ValueError):
        pass
threading.Thread(target=errors, args=(0 if state == "started" else size("err.log"),), daemon=True).start()

def inputs():
    w = None
    try:
        for line in sys.stdin.buffer:
            if line.strip() in (b'{"type": "_sw_relay_kill"}', b'{"type": "_sw_relay_stop"}'):
                p = pid_alive()
                if p and (b"_stop" in line or not others()):
                    os.kill(p, signal.SIGTERM)
                continue
            if line.startswith(b'{"type": "_sw_relay_conv"'):
                try:
                    c = json.loads(line).get("id") or ""
                except ValueError:
                    c = ""
                if c and all(ch in "0123456789abcdef-" for ch in c):
                    write("conv", c)       # which conversation this Claude is on (found again by it)
                continue
            if w is None:
                w = os.open(P("in"), os.O_WRONLY)
            os.write(w, line)
            if line.startswith(b'{"type": "control_response"'):
                try:
                    with open(P("answers.log"), "ab") as a:
                        a.write(line if line.endswith(b"\n") else line + b"\n")
                except OSError:
                    pass
    except OSError:
        pass
    leave()
    os._exit(0)                          # the connection is gone: Claude keeps working
if state != "replay":
    threading.Thread(target=inputs, daemon=True).start()

f = open(P("out.log"), "rb")
f.seek(off)
beat = 0
while True:
    b = f.read(65536)
    if b:
        sys.stdout.buffer.write(b)
        sys.stdout.buffer.flush()
        continue
    if not pid_alive():
        time.sleep(0.2)
        b = f.read()
        if b:
            sys.stdout.buffer.write(b)
        say({"type": "_sw_relay", "state": "exit", "code": exit_code()})
        leave()
        os._exit(0)                      # not sys.exit: the stdin thread is still reading (Python aborts)
    if time.time() - beat > 10:
        touch("seen")
        beat = time.time()
    time.sleep(0.1)
'''


def chat_relay_command(mode: str, key: str, offset: int = 0, script: str = "", conv: str = "") -> str:
    """The remote command for the relay; without python3 on the server Claude runs directly (as before).
    conv: the conversation being resumed — a Claude already working on it is used instead of a new one."""
    conv = conv if conv and UUID_RE.fullmatch(conv) else ""
    args = " ".join(shlex.quote(str(a)) for a in ([mode, key, offset]
                                                    + ([script] + ([conv] if conv else []) if mode == "start" else [])))
    fallback = f"exec sh -c {shlex.quote(script)}" if mode == "start" else "exit 0"
    return (f"if command -v python3 >/dev/null 2>&1; then SW_RELAY={shlex.quote(CHAT_RELAY_PY)}; export SW_RELAY; exec python3 -c \"$SW_RELAY\" {args}; "
            f"else {fallback}; fi")


RELAY_KEY_RE = re.compile(r"[0-9a-f]{16}")
# how a chat tab got its name: "ai" = Claude named the conversation, "custom" = renamed by the user
CHAT_NAMED = ("ai", "custom")
TITLE_TRIES = 3          # ask Claude for a title on at most this many messages (like the VS Code extension)


def clean_title(t) -> str:
    return " ".join(t.split())[:80] if isinstance(t, str) else ""


# Small Python programs run on the server (python3 is there) to read Claude's
# own transcripts: list past conversations of a folder / load one for display.
REMOTE_LIST_PY = r'''
import sys, os, json, glob
d = os.path.expanduser("~/.claude/projects/" + sys.argv[1])
out = []
for f in sorted(glob.glob(d + "/*.jsonl"), key=os.path.getmtime, reverse=True)[:40]:
    title = custom = first = last = ""
    n = 0
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '-title"' not in line and '"last-prompt"' not in line and '"type":"user"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                t = e.get("type")
                if t == "custom-title":
                    custom = e.get("customTitle") or custom
                elif t == "ai-title":
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
    out.append({"id": os.path.basename(f)[:-6], "mtime": os.path.getmtime(f), "title": custom or title,
                "first": first[:300], "last": last[:300], "prompts": n, "size": os.path.getsize(f)})
print(json.dumps(out))
'''

HISTORY_PAGE = 40

# Claude sessions running on the server — from this app, VS Code or a terminal. Claude Code
# writes ~/.claude/sessions/<pid>.json while it runs (sessionId, cwd, status busy/idle,
# entrypoint cli / claude-vscode / sdk-cli, procStart = field 22 of /proc/<pid>/stat).
#   list                 → {"home", "sessions": [...]}   only processes that really run
#   stop PID SESSION_ID  → stops it (only when that pid still runs that conversation)
CLAUDE_RUNNING_PY = r'''
import os, sys, json, glob, time, signal
D = os.path.expanduser("~/.claude/sessions")
def proc(pid):
    try:
        st = open("/proc/%d/stat" % pid).read()
        rest = st[st.rindex(")") + 2:].split()
        return int(rest[1]), rest[19]              # ppid, start time
    except (OSError, ValueError, IndexError):
        return None, None
def cmd(pid):
    try:
        return open("/proc/%d/cmdline" % pid, "rb").read().decode("utf-8", "replace").split("\0")
    except OSError:
        return []
def running(info):
    pid = info.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    ppid, start = proc(pid)
    if start is not None and info.get("procStart") and str(info["procStart"]) != start:
        return False                               # the pid was reused: an old file
    if os.path.exists("/proc/%d/stat" % pid):
        try:
            if open("/proc/%d/stat" % pid).read().split(")")[-1].split()[0] == "Z":
                return False
        except OSError:
            pass
    return True
def owner(info):
    pid = info["pid"]
    ep = str(info.get("entrypoint") or "")
    exe = (cmd(pid) or [""])[0]
    if "vscode" in ep or ".vscode-server" in exe or ".cursor-server" in exe:
        return "vscode", ""
    if ep == "cli":
        return "terminal", ""
    # our relay starts Claude through exec only: it is Claude's parent (a Claude started by
    # something running inside one of our chats is not ours)
    p, _s = proc(pid)
    c = cmd(p) if p and p > 1 else []
    if "simabworkspace/chats" in " ".join(c[:3]) and "daemon" in c:
        k = c.index("daemon")
        return "simab", c[k + 1] if len(c) > k + 1 else ""
    return "other", ""
def sessions():
    out = []
    for f in glob.glob(os.path.join(D, "*.json")):
        try:
            info = json.load(open(f))
        except (OSError, ValueError):
            continue
        if not isinstance(info, dict) or not running(info):
            continue
        who, relay = owner(info)
        ms = lambda k: (info.get(k) or 0) / 1000.0
        out.append({"pid": info["pid"], "sessionId": str(info.get("sessionId") or ""), "cwd": str(info.get("cwd") or ""),
                    "status": str(info.get("status") or ""), "entrypoint": str(info.get("entrypoint") or ""),
                    "name": str(info.get("name") or ""), "tmux": str(info.get("tmux") or ""), "owner": who, "relay": relay,
                    "started": ms("startedAt"), "updated": ms("statusUpdatedAt") or ms("updatedAt")})
    out.sort(key=lambda x: -x["started"])
    return out
op = sys.argv[1] if len(sys.argv) > 1 else "list"
if op == "list":
    print(json.dumps({"home": os.path.expanduser("~"), "sessions": sessions()}))
elif op == "stop":
    pid, sid = int(sys.argv[2]), sys.argv[3]
    me = [x for x in sessions() if x["pid"] == pid and x["sessionId"] == sid]
    if not me:
        print(json.dumps({"error": "this Claude session is not running any more"}))
        sys.exit()
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print(json.dumps({"error": "cannot stop it: " + (e.strerror or str(e))}))
        sys.exit()
    end = time.time() + 6
    while time.time() < end and any(x["pid"] == pid for x in sessions()):
        time.sleep(0.2)
    if any(x["pid"] == pid for x in sessions()):
        try:
            os.kill(pid, 9)                      # SIGKILL
        except OSError:
            pass
    print(json.dumps({"ok": True}))
'''

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
ai = custom = first = ""
with open(f, encoding="utf-8", errors="replace") as fh:
    for line in fh:
        if '"type":"user"' not in line and '"type":"assistant"' not in line and '-title"' not in line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("type") == "ai-title":
            ai = e.get("aiTitle") or ai
            continue
        if e.get("type") == "custom-title":
            custom = e.get("customTitle") or custom
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
        if not first and e["type"] == "user":
            first = c if isinstance(c, str) else " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
            first = "" if first.lstrip().startswith("<") else first.strip()
        out.append({"type": e["type"], "message": {"id": m.get("id"), "role": m.get("role"), "content": c},
                    **{k: e[k] for k in ("isSynthetic", "origin") if k in e}})
total = len(out)
end = total if before is None else max(0, min(before, total))
start = max(0, end - limit)
# do not start a page in the middle of a tool call: move back to a user text message when close
k = start
while k > 0 and k > start - 10 and not (out[k]["type"] == "user" and isinstance(out[k]["message"]["content"], (str, list))
        and any((isinstance(b, dict) and b.get("type") == "text") for b in (out[k]["message"]["content"] if isinstance(out[k]["message"]["content"], list) else [{"type": "text"}]))):
    k -= 1
start = k if k > 0 and out[k]["type"] == "user" else start
print(json.dumps({"total": total, "start": start, "entries": out[start:end], "title": (custom or ai)[:200], "custom": bool(custom), "first": first[:2000]}))
'''


# args: project dir, conversation id, byte offset → what was added to the transcript since
# (whole lines only): {"off": new offset, "size": file size, "entries": [...]} — for watching
REMOTE_TAIL_PY = r'''
import sys, os, json
f = os.path.expanduser("~/.claude/projects/%s/%s.jsonl" % (sys.argv[1], sys.argv[2]))
off = int(sys.argv[3])
LIM = 8000
def cut(s):
    return s if len(s) <= LIM else s[:LIM] + "\n…"
try:
    size = os.path.getsize(f)
except OSError:
    print(json.dumps({"off": off, "size": -1, "entries": []})); sys.exit()
if off < 0 or off > size:
    off = size
out = []
with open(f, "rb") as fh:
    fh.seek(off)
    data = fh.read(4000000)
end = data.rfind(b"\n") + 1
for raw in data[:end].split(b"\n"):
    line = raw.decode("utf-8", "replace")
    if '"type":"user"' not in line and '"type":"assistant"' not in line:
        continue
    try:
        e = json.loads(line)
    except Exception:
        continue
    if e.get("type") not in ("user", "assistant") or e.get("isSidechain") or e.get("isMeta"):
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
    out.append({"type": e["type"], "message": {"id": m.get("id"), "role": m.get("role"), "content": c},
                **{k: e[k] for k in ("isSynthetic", "origin") if k in e}})
print(json.dumps({"off": off + end, "size": size, "entries": out}))
'''


def remote_python(conf: ServerConf, code: str, args: list, multiplex=True, timeout=30, stdin: bytes = None):
    cmd = "python3 -c " + shlex.quote(code) + " " + " ".join(shlex.quote(a) for a in args)
    try:
        with tempfile.TemporaryFile() as errf:
            r = subprocess.run(ssh_base(conf, multiplex=multiplex) + ["-T", "-o", "BatchMode=yes", conf.target, cmd],
                               input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=errf, timeout=timeout)
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


BROWSE_PY = r'''
import sys, os, json
p = os.path.realpath(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "~"))
if not os.path.isdir(p):
    print(json.dumps({"error": "not a folder: " + p})); sys.exit()
try:
    names = sorted(os.listdir(p), key=lambda n: (n.startswith("."), n.lower()))
except OSError as e:
    print(json.dumps({"error": "cannot open " + p + ": " + (e.strerror or str(e))})); sys.exit()
dirs = []
for n in names:
    f = os.path.join(p, n)
    if not os.path.isdir(f):
        continue
    git = "repo" if os.path.exists(os.path.join(f, ".git")) else ""
    dirs.append({"name": n, "git": git, "hidden": n.startswith(".")})
    if len(dirs) >= 1000:
        break
print(json.dumps({"path": p, "parent": os.path.dirname(p) if p != "/" else "", "home": os.path.expanduser("~"),
                  "git": os.path.exists(os.path.join(p, ".git")), "dirs": dirs}))
'''

FILE_LIMIT = 30000

# The server's load right now: CPU (two samples 0.3 s apart), memory, the disk of the home
# folder. "top": also the processes using the most CPU / memory. Linux (/proc) only.
SERVER_STATS_PY = r'''
import os, sys, json, time, subprocess
def cpu():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return sum(v), idle
try:
    t1, i1 = cpu(); time.sleep(0.3); t2, i2 = cpu()
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            mem[k] = int(rest.split()[0]) * 1024
except (OSError, ValueError, IndexError):
    print(json.dumps({"error": "not available on this server"})); sys.exit()
home = os.path.expanduser("~")
st = os.statvfs(home)
total, avail = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
used = total - st.f_bfree * st.f_frsize
out = {"cpu": round(100.0 * (1 - (i2 - i1) / float(t2 - t1)), 1) if t2 > t1 else 0.0,
       "cores": os.cpu_count() or 1, "load": [round(x, 2) for x in os.getloadavg()],
       "memTotal": mem.get("MemTotal", 0), "memUsed": mem.get("MemTotal", 0) - mem.get("MemAvailable", mem.get("MemFree", 0)),
       "swapTotal": mem.get("SwapTotal", 0), "swapUsed": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
       "diskTotal": total, "diskUsed": used, "diskFree": avail, "diskPath": home}
if len(sys.argv) > 1 and sys.argv[1] == "top":
    def top(key):
        try:
            pr = subprocess.Popen(["ps", "-eo", "pid,user,pcpu,pmem,rss,comm", "--sort=-" + key, "--no-headers"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            text = pr.communicate(timeout=10)[0]
        except (OSError, subprocess.SubprocessError):
            return []
        rows = []
        for line in text.splitlines():
            p = line.split(None, 5)
            if len(rows) >= 8:
                break
            if len(p) == 6 and int(p[0]) not in (pr.pid, os.getpid()):
                rows.append({"pid": int(p[0]), "user": p[1], "cpu": float(p[2]), "mem": float(p[3]), "rss": int(p[4]) * 1024, "name": p[5][:60]})
        return rows
    out["topCpu"], out["topMem"] = top("pcpu"), top("rss")
print(json.dumps(out))
'''

# One folder on the server, for completing paths typed in the chat (~/… or /…): the files of
# the SSH user, like a shell would complete them.
SERVER_LS_PY = r'''
import os, sys, json
raw = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "~/"
p = os.path.expanduser(raw)
if not p.endswith("/"):
    p = os.path.dirname(p) + "/"
try:
    names = os.listdir(p)
except OSError as e:
    print(json.dumps({"error": e.strerror or str(e), "home": os.path.expanduser("~")})); sys.exit()
out = []
for n in names[:3000]:
    try:
        d = os.path.isdir(os.path.join(p, n))
    except OSError:
        d = False
    out.append({"name": n, "dir": d})
out.sort(key=lambda x: (not x["dir"], x["name"].startswith("."), x["name"].lower()))
print(json.dumps({"path": raw, "home": os.path.expanduser("~"), "entries": out, "more": len(names) > 3000}))
'''


def local_files(root: str) -> list:
    """Project files for Find file / @-mentions (see fs_all in FS_PY)."""
    return fs_local()["fs_all"](root)


def with_dirs(files: list) -> list:
    dirs = set()
    for f in files:
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]) + "/")
    return sorted(dirs) + files


class ChatSession:
    win = ""
    moved = 0.0
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
        self.model = chat_model(built.get("model"))
        self.effort = chat_effort(built.get("effort"))
        self.busy = False
        self.init = None              # answer to our "initialize" request
        self.pending = {}             # permission requests waiting for the user
        self.my_requests = {}         # request_id -> subtype of requests we sent
        self.wlock = threading.Lock()
        self.on_usage = None          # callback(rate_limit_info)
        self.on_cost = None           # callback(project, usd) — the cost of each answer
        self._cost_seen = 0.0
        # the relay on the server (Claude survives a dropped connection); "" = run Claude directly
        self.relay_key = built.get("relay") or ""
        if self.relay_key:
            self.spec["relay"] = self.relay_key
        self.relay_off = -1 if built.get("relay_restored") else 0    # bytes of Claude's output read so far
        self.relay_on = False         # this connection goes through the relay
        self.linking = False          # connecting to the relay; not known yet whether Claude still works
        self.relay_exit = None        # Claude's exit code, reported by the relay
        self.reconnecting = 0         # attempts since the connection dropped
        self.relay_pid = 0            # our relay process on the server (not counted as another device)
        self.closed = False
        h = built.get("history")
        if isinstance(h, list):                        # old format: a plain list
            h = {"total": len(h), "start": 0, "entries": h}
        self.title_tries = 0
        self.rename_later = ""        # renamed while Claude was not running: told when it starts
        self.title_hint = ""          # an old conversation without a title: named after its first message
        if isinstance(h, dict) and clean_title(h.get("title")):
            self.title = clean_title(h["title"])          # the name Claude (or /rename) gave this conversation
            self.spec["named"] = "custom" if h.get("custom") else "ai"
        elif isinstance(h, dict) and isinstance(h.get("first"), str):
            self.title_hint = h["first"]
        if h and h.get("entries"):
            self.events.append({"type": "_history", "entries": h["entries"], "start": h.get("start", 0), "total": h.get("total", 0)})
        elif built.get("history_error"):
            self.events.append({"type": "_error", "text": "Earlier messages of this conversation could not be loaded ("
                                + built["history_error"] + "). You can still continue it."})

    # process
    def start(self):
        self._connect("start")

    def _connect(self, how="start"):
        if self.relay_key:
            argv = self.built["make_argv"](self.mode, self.model, self.session_id, self.effort,
                                           relay=(how, self.relay_key, self.relay_off, self.session_id or ""))
        else:
            argv = self.built["make_argv"](self.mode, self.model, self.session_id, self.effort)
        env = dict(os.environ, PATH=tool_path())
        try:
            proc = subprocess.Popen(argv, cwd=self.built["cwd"], env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except OSError as e:
            raise ApiError(500, f"could not start: {e}")
        with self.lock:
            again = how == "attach"
            self.proc, self.exit_code = proc, None
            self.relay_on, self.relay_exit = False, None
            self.linking = bool(self.relay_key)
            if not again:
                self.busy = False
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

    def _relay_marker(self, ev: dict):
        st = ev.get("state")
        if st == "pending":                       # an unanswered question from before the app was reopened
            if isinstance(ev.get("event"), dict):
                self._handle_event(ev["event"])
            return
        if isinstance(ev.get("off"), int):
            self.relay_off = ev["off"]
        if isinstance(ev.get("pid"), int) and ev["pid"] > 0:
            self.relay_pid = ev["pid"]
        k = ev.get("key")
        if isinstance(k, str) and RELAY_KEY_RE.fullmatch(k) and k != self.relay_key:
            self.relay_key = k                     # the Claude that was already on this conversation
            self.spec["relay"] = k
            if self.on_change:
                self.on_change(self)
        if st in ("started", "attached") and self.session_id:
            self._write({"type": "_sw_relay_conv", "id": self.session_id})
        was_linking, self.linking = self.linking, False
        if st == "exit":
            self.relay_exit = ev.get("code") if isinstance(ev.get("code"), int) else -1
            self._broadcast({"t": "link", "linking": False, "busy": False})
            return
        busy = st == "attached" and ev.get("status") == "busy"
        if busy and not self.busy:
            self.busy = True
        if st == "attached" and was_linking and not self.reconnecting:
            # the app was restarted / reopened while this Claude kept running on the server
            self._log({"type": "_reattached", "busy": busy})
        self._broadcast({"t": "link", "linking": False, "busy": self.busy})
        self.relay_on = True
        if self.reconnecting:
            self.reconnecting = 0
            self._log({"type": "_reconnected", "running": st == "attached"})

    def _read_out(self, proc):
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                if raw.endswith(b"\n"):
                    self.relay_off += len(raw) if self.relay_off >= 0 else 0
                self._log({"type": "_stderr", "text": line[:2000]})
                continue
            if isinstance(ev, dict) and ev.get("type") == "_sw_relay":
                self._relay_marker(ev)
                continue
            if raw.endswith(b"\n") and self.relay_off >= 0:
                self.relay_off += len(raw)        # only whole lines: a cut-off one is read again next time
            if self.linking:                      # no relay marker (no python3 on the server): Claude answers directly
                self.linking = False
                self._broadcast({"t": "link", "linking": False, "busy": self.busy})
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
            if self.relay_on and self.relay_exit is None and not self.closed:
                # the connection dropped, Claude is still working on the server: attach again
                self.reconnecting += 1
                if self.reconnecting == 1:
                    self._log({"type": "_reconnect", "code": code})
                delay = min(30, [1, 2, 4, 8, 15][min(self.reconnecting - 1, 4)])
                threading.Thread(target=self._reconnect_later, args=(proc, delay), daemon=True).start()
                return
            if self.relay_exit is not None:
                code = self.relay_exit
            self.reconnecting = 0
            self.exit_code = code
            self.busy = False
            self.pending.clear()
            self._log({"type": "_exit", "code": code})
        if self.on_change:
            self.on_change(self)

    def _reconnect_later(self, proc, delay):
        time.sleep(delay)
        with self.lock:
            if self.closed or self.proc is not proc:
                return
        try:
            self._connect("attach")
        except ApiError:
            with self.lock:
                if self.proc is proc:
                    self.exit_code = -1
            self._log({"type": "_exit", "code": -1})

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
            if self.relay_on:
                self._write({"type": "_sw_relay_conv", "id": sid})
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
                self._send_rename()
            elif kind in ("generate_session_title", "rename_session"):
                t = clean_title((resp.get("response") or {}).get("title"))
                if kind == "generate_session_title" and resp.get("subtype") == "success" and t \
                        and self.spec.get("named") not in CHAT_NAMED:
                    self.set_title(t, "ai")
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
        elif t == "system" and ev.get("subtype") == "session_title_changed":
            if clean_title(ev.get("title")):                # /rename, or a rename from another client
                self.set_title(clean_title(ev["title"]), "custom")
            return
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
        return {"busy": self.busy, "linking": self.linking, "alive": self.alive, "code": self.exit_code, "mode": self.mode,
                "model": self.model, "effort": self.effort, "sessionId": self.session_id, "init": self.init}

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
            if not self.alive or self.proc.poll() is not None:
                self._connect("start")          # the relay attaches if Claude still runs on the server
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
                return
            self._ask_title(content)
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
            self.model = chat_model(m["model"])
            if getattr(self, "remember", None):
                self.remember(self.spec.get("project"), model=self.model)
            self._request({"subtype": "set_model", "model": self.model})
            self._log({"type": "_model", "model": self.model})
        elif t == "effort" and m.get("effort") in CHAT_EFFORTS:
            self.effort = m["effort"]
            if getattr(self, "remember", None):
                self.remember(self.spec.get("project"), effort=self.effort)
            self._request({"subtype": "apply_flag_settings", "settings": {"effortLevel": self.effort}})
            self._log({"type": "_effort", "effort": self.effort})

    def _ask_title(self, content):
        """Like the VS Code extension: Claude names the conversation from the first message
        (and writes it into the transcript, so every Claude client shows the same name)."""
        if self.spec.get("named") in CHAT_NAMED or self.title_tries >= TITLE_TRIES:
            return
        txt = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text").strip()
        txt = self.title_hint or txt
        if not txt or txt.startswith("/"):
            return
        self.title_tries += 1
        self._request({"subtype": "generate_session_title", "description": txt[:4000], "persist": True})

    def set_title(self, title: str, how: str):
        with self.lock:
            changed = title != self.title or self.spec.get("named") != how
            self.title = title
            self.spec["named"] = how
            if changed:
                self._broadcast({"t": "title", "title": title})
        if changed and self.on_change:
            self.on_change(self)

    def rename(self, title: str):
        """The user renamed the tab: the conversation gets the same name in Claude (custom-title)."""
        self.title = title
        self.spec["named"] = "custom"
        self.rename_later = title
        self._send_rename()

    def _send_rename(self):
        if self.rename_later and self.alive and self.session_id:
            if self._request({"subtype": "rename_session", "title": self.rename_later, "source": "host",
                              "session_id": self.session_id}):
                self.rename_later = ""

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
            for sig, delay in ((signal.SIGTERM, 1.5), (SIGKILL, 2)):
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

    def _stop_remote(self, proc, key, force=False):
        """Stop Claude on the server (the relay keeps it running otherwise)."""
        if not key:
            return
        # not force: another device attached to the same Claude keeps it (the relay checks)
        skip = -1 if force else self.relay_pid
        if proc and proc.poll() is None and self._write({"type": "_sw_relay_stop" if force else "_sw_relay_kill"}, proc):
            time.sleep(0.3)

        def kill_remote():
            try:
                argv = self.built["make_argv"](self.mode, self.model, "", self.effort, relay=("kill", key, skip))
                subprocess.run(argv, cwd=self.built["cwd"], env=dict(os.environ, PATH=tool_path()),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40)
            except (OSError, subprocess.SubprocessError, TypeError):
                pass
        threading.Thread(target=kill_remote, daemon=True).start()

    def kill(self, wait=False, remote=True):
        """Close the tab. remote=False (the app quits / restarts): Claude keeps running on the
        server and the restored tab attaches to it again."""
        with self.lock:
            proc = self.proc
            self.closed = True
            self._broadcast({"t": "gone"})
            for ws in list(self.subs):
                ws.close()
            self.subs.clear()
        if remote:
            self._stop_remote(proc, self.relay_key)
        self._terminate(proc, wait)

    def shutdown(self):
        self.kill(remote=False)

    def restart(self, fresh=False):
        with self.lock:
            proc = self.proc
            self.proc = None
            old = self.relay_key
            if old:                                # a new Claude: a new relay folder
                self.relay_key = secrets.token_hex(8)
                self.spec["relay"] = self.relay_key
                self.relay_off = 0
            self.reconnecting = 0
            self.relay_pid = 0
        self._stop_remote(proc, old, force=not fresh)     # Restart = this Claude really goes
        self._terminate(proc)
        if fresh:
            with self.lock:
                self.events.clear()
                self.session_id = ""
                self.spec.pop("resume", None)
                self.spec.pop("named", None)
                self.title_tries, self.title_hint = 0, ""
                self.title = "Claude · " + (self.spec.get("project") or "home")
            self._broadcast({"t": "clear"})
            self._broadcast({"t": "title", "title": self.title})
            if self.on_change:
                self.on_change(self)
        else:
            self._log({"type": "_restart"})
        self.start()

    def resize(self, *_):
        pass

    def write(self, *_):
        pass

    def info(self) -> dict:
        return {"id": self.id, "title": self.title, "spec": self.spec, "url": "",
                "alive": self.alive, "exitCode": self.exit_code, "started": self.started, "runs": self.runs,
                "win": self.win, "moved": self.moved}


class WatchSession:
    """A chat tab that only watches a conversation another Claude works on (VS Code, a
    terminal…): the transcript on the server is read as it grows. Nothing is sent to Claude."""
    win = ""
    moved = 0.0
    POLL = 1.5

    def __init__(self, sid: str, built: dict, offset: int, status_fn=None, multiplex=True):
        self.id = sid
        self.built = built
        self.title = built["title"]
        self.spec = dict(built["spec"], watch=True)
        self.conv = built["resume"]
        self.offset = offset
        self.status_fn = status_fn            # () -> running info of the conversation, or None
        self.multiplex = multiplex
        self.lock = threading.RLock()
        self.subs = set()
        self.events = collections.deque(maxlen=4000)
        self.closed = False
        self.alive = True
        self.exit_code = None
        self.started = time.time()
        self.runs = 1
        self.watch = {"running": True, "status": "", "owner": ""}
        h = built.get("history")
        if h and h.get("entries"):
            self.events.append({"type": "_history", "entries": h["entries"], "start": h.get("start", 0), "total": h.get("total", 0)})
        self.update_status()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def update_status(self):
        r = self.status_fn() if self.status_fn else None
        w = {"running": bool(r), "status": (r or {}).get("status", ""), "owner": (r or {}).get("owner", "")}
        if w != self.watch:
            self.watch = w
            self._log({"type": "_watch", **w})

    def _loop(self):
        n = 0
        while not self.closed:
            time.sleep(self.POLL)
            if self.closed:
                break
            try:
                r = remote_python(self.built["conf"], REMOTE_TAIL_PY,
                                  [claude_project_dir(self.built["rpath"]), self.conv, str(self.offset)], multiplex=self.multiplex)
            except ApiError:
                continue
            if isinstance(r, dict) and isinstance(r.get("off"), int):
                self.offset = r["off"]
                for e in r.get("entries") or []:
                    if isinstance(e, dict):
                        self._log(e)
            n += 1
            if n % 3 == 0:
                self.update_status()

    def _log(self, ev):
        with self.lock:
            self.events.append(ev)
            for ws in list(self.subs):
                try:
                    ws.send_json({"t": "e", "e": ev})
                except WebSocketClosed:
                    self.subs.discard(ws)

    def state(self) -> dict:
        return {"busy": self.watch.get("status") == "busy", "alive": True, "code": None, "mode": "default", "model": "",
                "effort": "", "sessionId": self.conv, "init": None, "watch": self.watch}

    def attach(self, ws):
        with self.lock:
            ws.send_json({"t": "s", "events": list(self.events), "state": self.state()})
            self.subs.add(ws)

    def detach(self, ws):
        with self.lock:
            self.subs.discard(ws)

    def handle(self, m):
        pass                                   # watching only

    def kill(self, wait=False, remote=True):
        with self.lock:
            self.closed = True
            for ws in list(self.subs):
                try:
                    ws.send_json({"t": "gone"})
                    ws.close()
                except (WebSocketClosed, OSError):
                    pass
            self.subs.clear()

    def shutdown(self):
        self.kill()

    def stop(self):
        pass

    def restart(self, fresh=False):
        pass

    def info(self) -> dict:
        return {"id": self.id, "title": self.title, "spec": self.spec, "url": "", "alive": True, "exitCode": None,
                "started": self.started, "runs": 1, "win": self.win, "moved": self.moved}


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
    model = chat_model(pp["chatModel"] if "chatModel" in pp else s.get("chatModel", ""))
    effort = chat_effort(pp["chatEffort"] if "chatEffort" in pp else s.get("chatEffort", ""))
    claude_cmd = s.get("claudeCmd") or "claude"
    multiplex = bool(s.get("sshMultiplex"))

    def make_argv(mode_, model_, resume_, effort_="", relay=None):
        """relay: (start|attach|kill, key, offset) — Claude kept running on the server (see CHAT_RELAY_PY)."""
        script = chat_remote_script(rpath, claude_cmd, chat_args(mode_, model_, resume_, effort_))
        if relay:
            script = chat_relay_command(relay[0], relay[1], relay[2], script, relay[3] if len(relay) > 3 else "")
        return ssh_base(c, multiplex=multiplex and not relay) + ["-T", c.target, script]

    relay = spec.get("relay") if isinstance(spec.get("relay"), str) and RELAY_KEY_RE.fullmatch(spec.get("relay")) else ""

    title = spec.get("title") if isinstance(spec.get("title"), str) and spec.get("title").strip() else f"Claude · {pname or 'home'}"
    return {"make_argv": make_argv, "cwd": os.path.expanduser("~"), "title": title.strip()[:80],
            "resume": resume, "mode": mode, "model": model, "effort": effort, "conf": c, "rpath": rpath,
            "relay": relay or secrets.token_hex(8), "relay_restored": bool(relay),
            "spec": {"kind": "chat", "project": pname, "where": "server", "cmdId": "", "claudeMode": "new",
                     "slot": 0, **({"resume": resume} if resume else {}),
                     **({"named": spec["named"]} if spec.get("named") in CHAT_NAMED else {})}}



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
            r = subprocess.run(ssh_base(conf, multiplex=bool(app.store.settings.get("sshMultiplex"))) + ["-T", "-o", "BatchMode=yes", conf.target, posix_sh(cmd)],
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
            argv = ssh_base(conf, multiplex=bool(self.app.store.settings.get("sshMultiplex"))) + ["-T", "-o", "BatchMode=yes", conf.target, posix_sh(cmd)]
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
        # tracked files that .gitignore names: .gitignore does not apply to files git already
        # tracks, so their changes keep showing — mark them (they can be "untracked")
        tracked = [f["path"] for f in st["files"] if not f["untracked"] and f["x"] != "D" and f["y"] != "D"]
        if tracked:
            c2, ign, _ = self.run(["check-ignore", "--no-index", "-z", "--stdin"], timeout=20,
                                  stdin="\0".join(tracked).encode())
            names = set(x for x in ign.split("\0") if x) if c2 in (0, 1) else set()
            for f in st["files"]:
                if f["path"] in names:
                    f["ignored"] = True
            st["ignoredTracked"] = sum(1 for f in st["files"] if f.get("ignored"))
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

    def untrack(self, files) -> str:
        """Stop tracking files (they stay on disk) — for files .gitignore names."""
        if not isinstance(files, list) or not files or not all(isinstance(f, str) and git_path_ok(f) for f in files):
            raise ApiError(400, "pick at least one file")
        self.flush()
        out = self.check(["rm", "-r", "--cached", "--quiet", "--"] + files)
        self.flush()
        return out

    # history --------------------------------------------------------------------
    def log(self, limit=80) -> dict:
        """Recent commits of this branch and of its upstream, each marked: push (only here),
        pull (only on the remote, as of the last fetch), synced, or local (no upstream)."""
        code, up, _ = self.run(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], timeout=15)
        up = up.strip() if code == 0 else ""
        refs = ["HEAD"] + ([up] if up else [])
        fmt = "%H%x00%h%x00%an%x00%at%x00%D%x00%P%x00%s"
        code, out, err = self.run(["log", "--date-order", f"-n{int(limit)}", "--format=" + fmt] + refs + ["--"], timeout=30)
        if code != 0:
            if any(x in err for x in ("does not have any commits", "bad default revision", "unknown revision", "bad revision 'HEAD'")):
                return {"upstream": up, "commits": [], "ahead": 0, "behind": 0}
            raise ApiError(400, err.strip()[-500:] or "git log failed")
        ahead, behind = set(), set()
        if up:
            for rng, into in ((f"{up}..HEAD", ahead), (f"HEAD..{up}", behind)):
                c, o, _ = self.run(["rev-list", "-n2000", rng, "--"], timeout=30)
                if c == 0:
                    into.update(o.split())
        commits = []
        for line in out.splitlines():
            parts = line.split("\0")
            if len(parts) < 7:
                continue
            full, short, author, date, refs_, parents, subject = parts[:7]
            state = "push" if full in ahead else "pull" if full in behind else "synced" if up else "local"
            commits.append({"hash": full, "short": short, "author": author, "date": int(date) if date.isdigit() else 0,
                            "refs": [r.strip() for r in refs_.split(",") if r.strip()], "merge": len(parents.split()) > 1,
                            "subject": subject[:300], "state": state})
        return {"upstream": up, "commits": commits, "ahead": len(ahead), "behind": len(behind)}

    def show(self, rev: str) -> str:
        if not isinstance(rev, str) or not re.fullmatch(r"[0-9a-f]{4,40}", rev):
            raise ApiError(400, "bad commit")
        text = self.check(["show", "--no-color", "--stat", "--patch", "--format=commit %H%nAuthor: %an <%ae>%nDate:   %ad%n%n%B", rev, "--"],
                          timeout=30)
        return text if len(text) < 400_000 else text[:400_000] + "\n… (cut)"

    # branches -----------------------------------------------------------------
    def branches(self) -> dict:
        code, cur, _ = self.run(["branch", "--show-current"], timeout=15)
        fmt = "%(refname)%00%(refname:short)%00%(committerdate:unix)%00%(upstream:short)%00%(contents:subject)"
        out = self.check(["for-each-ref", "--sort=-committerdate", "--format=" + fmt, "refs/heads", "refs/remotes"], timeout=30)
        local, remote = [], []
        for line in out.splitlines():
            parts = line.split("\0")
            if len(parts) < 5:
                continue
            ref, short, date, up, subject = parts[:5]
            item = {"name": short, "date": int(date) if date.isdigit() else 0, "subject": subject[:200]}
            if ref.startswith("refs/heads/"):
                item["upstream"] = up
                local.append(item)
            elif ref.startswith("refs/remotes/") and not ref.endswith("/HEAD"):
                remote.append(item)
        tracked = {b["upstream"] for b in local if b.get("upstream")} | {b["name"] for b in local}
        for r in remote:                         # origin/x with a local x: already switchable as x
            r["local"] = r["name"] in tracked or r["name"].split("/", 1)[-1] in {b["name"] for b in local}
        return {"current": cur.strip() if code == 0 else "", "local": local, "remote": remote}

    def valid_branch(self, name) -> str:
        if not isinstance(name, str) or not name.strip() or name.startswith("-") or len(name) > 200:
            raise ApiError(400, "give a branch name")
        name = name.strip()
        code, _, _ = self.run(["check-ref-format", "--branch", name], timeout=15)
        if code != 0:
            raise ApiError(400, f"“{name}” is not a valid branch name")
        return name

    def switch(self, name, create=False, track=False, stash=False) -> str:
        """Switch to a branch (create: a new one from here; track: a remote branch like
        origin/x becomes local x). stash: put uncommitted changes aside first."""
        name = self.valid_branch(name)
        self.flush()
        if stash:
            self.check(["stash", "push", "--include-untracked", "-m", f"simab: before switching to {name}"], timeout=60)
        if create:
            args = ["switch", "-c", name]
        elif track:
            args = ["switch", "--track", name]
        else:
            args = ["switch", name]
        code, out, err = self.run(args, timeout=120)
        self.flush()
        if code != 0:
            text = (err.strip() or out.strip())
            if "would be overwritten" in text or "commit your changes or stash them" in text:
                raise ApiError(409, "Your uncommitted changes would be overwritten by this branch.")
            raise ApiError(400, text[-1500:] or "git switch failed")
        return (out + err).strip()


# ----------------------------------------------------------------------------- file explorer
# The Files tab lists folders and shows files of a project. The same small script
# runs here (exec'd in-process) for the local copy, and on the server over SSH
# (python3 -c) — so both sides behave the same. Paths never leave the project:
# every request is resolved with realpath and must stay inside the project root.
FS_PY = r'''
import os, sys, json, base64, subprocess

FS_SKIP = {"node_modules", "dist", "build", ".angular", "vendor", "tmp", ".next", ".cache", ".dart_tool",
           "__pycache__", ".venv", ".gradle", ".nuxt", "coverage"}
FS_MAX_LIST = 3000
FS_MAX_TEXT = 1000000
FS_MAX_IMAGE = 3000000
FS_IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
             ".webp": "image/webp", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".bmp": "image/bmp"}


class FsError(Exception):
    pass


def fs_resolve(root, rel):
    root = os.path.realpath(os.path.expanduser(root or "~"))
    if not os.path.isdir(root):
        raise FsError("the project folder does not exist")
    rel = (rel or "").strip("/")
    if rel and (rel.startswith("-") or ".." in rel.split("/") or "\0" in rel):
        raise FsError("bad path")
    full = os.path.realpath(os.path.join(root, rel)) if rel else root
    if full != root and not full.startswith(root.rstrip(os.sep) + os.sep):
        raise FsError("this path is outside the project")
    return root, full


def fs_ignored(folder, names):
    """Names git ignores in this folder (empty when it is not inside a repository)."""
    if not names:
        return set()
    try:
        r = subprocess.run(["git", "-C", folder, "check-ignore", "-z", "--stdin"], input="\0".join(names).encode(),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
                           env=dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C"))
    except (OSError, subprocess.SubprocessError):
        return set()
    return {x for x in r.stdout.decode("utf-8", "replace").split("\0") if x}


def fs_list(root, rel):
    root, full = fs_resolve(root, rel)
    if not os.path.isdir(full):
        raise FsError("not a folder")
    out = []
    try:
        it = sorted(os.scandir(full), key=lambda e: e.name.lower())
    except OSError as e:
        raise FsError("cannot read this folder: " + (e.strerror or str(e)))
    for e in it:
        if e.name == ".git":
            continue
        try:
            isdir = e.is_dir()
            st = e.stat() if not e.is_symlink() or os.path.exists(e.path) else e.stat(follow_symlinks=False)
        except OSError:
            continue
        out.append({"name": e.name, "dir": isdir, "size": 0 if isdir else st.st_size,
                    "mtime": int(st.st_mtime), "link": e.is_symlink()})
    out.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    more = len(out) > FS_MAX_LIST
    out = out[:FS_MAX_LIST]
    # symlinks are left out: git stops at the first path "beyond a symbolic link"
    ign = fs_ignored(full, [x["name"] + ("/" if x["dir"] else "") for x in out if not x["link"]])
    for x in out:
        if x["name"] + ("/" if x["dir"] else "") in ign or x["name"] in ign or (x["dir"] and x["name"] in FS_SKIP):
            x["ignored"] = True
    return {"path": (rel or "").strip("/"), "entries": out, "more": more}


def fs_read(root, rel):
    root, full = fs_resolve(root, rel)
    if not rel or not os.path.isfile(full):
        raise FsError("not a file")
    size = os.path.getsize(full)
    res = {"path": rel.strip("/"), "size": size, "mtime": int(os.path.getmtime(full))}
    ext = os.path.splitext(full)[1].lower()
    if ext in FS_IMAGES:
        if size > FS_MAX_IMAGE:
            res["tooBig"] = True
            return res
        with open(full, "rb") as f:
            res["image"] = "data:" + FS_IMAGES[ext] + ";base64," + base64.b64encode(f.read()).decode()
        return res
    if size > FS_MAX_TEXT:
        res["tooBig"] = True
        return res
    with open(full, "rb") as f:
        data = f.read()
    if b"\0" in data[:8000]:
        res["binary"] = True
        return res
    res["text"] = data.decode("utf-8", "replace")
    return res


WALK_SKIP = FS_SKIP | {".git", ".idea", "docker/data"}


def fs_git_files(folder, prefix):
    """git's own list of a repository: tracked + untracked, without what .gitignore names."""
    try:
        r = subprocess.run(["git", "-C", folder, "ls-files", "-co", "--exclude-standard", "-z"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=30,
                           env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return [prefix + x for x in r.stdout.decode("utf-8", "replace").split("\0") if x]


def fs_all(root, limit=30000):
    """Every file of a project, for Find file and @-mentions. Inside git repositories git's
    list is used (so .gitignore counts) — also when the project folder holds several of them."""
    root = os.path.realpath(os.path.expanduser(root or "~"))
    if os.path.exists(os.path.join(root, ".git")):
        got = fs_git_files(root, "")
        if got is not None:
            return got[:limit]
    out = []
    for d, dirs, files in os.walk(root):
        rel = os.path.relpath(d, root)
        rel = "" if rel == "." else rel.replace(os.sep, "/") + "/"
        if rel and os.path.exists(os.path.join(d, ".git")):
            got = fs_git_files(d, rel)
            if got is not None:
                out.extend(got)
                dirs[:] = []
                if len(out) >= limit:
                    break
                continue
        dirs[:] = sorted(x for x in dirs if x not in WALK_SKIP and not x.startswith(".") and (rel + x) not in WALK_SKIP)
        out.extend(rel + f for f in sorted(files))
        if len(out) >= limit:
            break
    return out[:limit]


def fs_target(root, rel):
    """For changes: the real folder it is in (inside root), the last name kept as it is — so a symbolic
    link is renamed / removed itself, never what it points to."""
    root = os.path.realpath(os.path.expanduser(root or "~"))
    rel = (rel or "").strip("/")
    if not rel or rel.startswith("-") or ".." in rel.split("/") or "\0" in rel:
        raise FsError("bad path")
    parent = os.path.realpath(os.path.join(root, os.path.dirname(rel))) if "/" in rel else root
    if parent != root and not parent.startswith(root.rstrip(os.sep) + os.sep):
        raise FsError("this path is outside the folder")
    return root, os.path.join(parent, os.path.basename(rel))


def fs_name_ok(name):
    if not name or name in (".", "..") or "/" in name or "\0" in name or len(name) > 255:
        raise FsError("bad name")
    return name


def fs_free(folder, name):
    """name, or "name copy", "name copy 2"... when it is taken in folder."""
    if not os.path.lexists(os.path.join(folder, name)):
        return name
    stem, ext = os.path.splitext(name)
    if name.startswith(".") and stem == name:
        ext = ""
    for i in range(1, 1000):
        cand = stem + " copy" + ("" if i == 1 else " " + str(i)) + ext
        if not os.path.lexists(os.path.join(folder, cand)):
            return cand
    raise FsError("too many copies")


def fs_rel(root, full):
    return os.path.relpath(full, root).replace(os.sep, "/")


def fs_change(root, op, rel, arg="", text=None):
    """mkdir / newfile / rename (arg = new name) / copy, move (arg = target folder) / delete / write
    (text; arg = the mtime it was read at, "" = overwrite). Answers {"path": the new or changed path}."""
    import shutil
    if op == "mkdir" or op == "newfile":
        root, full = fs_target(root, rel)
        if os.path.lexists(full):
            raise FsError("“" + os.path.basename(full) + "” already exists")
        fs_name_ok(os.path.basename(full))
        if not os.path.isdir(os.path.dirname(full)):
            raise FsError("the folder does not exist")
        if op == "mkdir":
            os.mkdir(full)
        else:
            open(full, "x").close()
        return {"path": fs_rel(root, full)}
    if op == "rename":
        root, full = fs_target(root, rel)
        if not os.path.lexists(full):
            raise FsError("it is not there any more")
        new = os.path.join(os.path.dirname(full), fs_name_ok(arg))
        if new == full:
            return {"path": fs_rel(root, full)}
        if os.path.lexists(new):
            raise FsError("“" + arg + "” already exists")
        os.rename(full, new)
        return {"path": fs_rel(root, new)}
    if op in ("copy", "move"):
        root, src = fs_target(root, rel)
        if not os.path.lexists(src):
            raise FsError("it is not there any more")
        root, dst_dir = fs_resolve(root, arg)
        if not os.path.isdir(dst_dir):
            raise FsError("not a folder")
        if os.path.isdir(src) and not os.path.islink(src) and (dst_dir + os.sep).startswith(src.rstrip(os.sep) + os.sep):
            raise FsError("a folder cannot go inside itself")
        if op == "move" and os.path.dirname(src) == dst_dir:
            return {"path": fs_rel(root, src)}
        dst = os.path.join(dst_dir, fs_free(dst_dir, os.path.basename(src)))
        if op == "move":
            shutil.move(src, dst)
        elif os.path.isdir(src) and not os.path.islink(src):
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst, follow_symlinks=False)
        return {"path": fs_rel(root, dst)}
    if op == "delete":
        root, full = fs_target(root, rel)
        if not os.path.lexists(full):
            return {"path": fs_rel(root, full)}
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full)
        else:
            os.unlink(full)
        return {"path": fs_rel(root, full)}
    if op == "write":
        root, full = fs_target(root, rel)
        if os.path.isdir(full):
            raise FsError("not a file")
        if arg and os.path.exists(full) and str(int(os.path.getmtime(full))) != str(arg):
            raise FsError("changed: the file was changed on the server since you opened it")
        data = (text or "").encode("utf-8")
        if len(data) > FS_MAX_TEXT * 5:
            raise FsError("too big to save from here")
        tmp = full + ".sw-save"
        mode = os.stat(full).st_mode & 0o7777 if os.path.exists(full) else None
        with open(tmp, "wb") as f:
            f.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, full)
        return {"path": fs_rel(root, full), "mtime": int(os.path.getmtime(full)), "size": len(data)}
    raise FsError("bad request")


FS_CHANGES = ("mkdir", "newfile", "rename", "copy", "move", "delete", "write")


def fs_main(argv):
    try:
        op, root, rel = argv[0], argv[1], argv[2] if len(argv) > 2 else ""
        if op in FS_CHANGES:
            text = sys.stdin.buffer.read().decode("utf-8") if op == "write" else None
            print(json.dumps(fs_change(root, op, rel, argv[3] if len(argv) > 3 else "", text)))
            return
        if op == "upload":
            # argv: upload root folder name  — raw bytes on stdin
            folder = argv[3] if len(argv) > 3 else ""
            name = argv[4] if len(argv) > 4 else "upload"
            fs_name_ok(name)
            root_real = os.path.realpath(os.path.expanduser(root or "~"))
            if not os.path.isdir(root_real):
                raise FsError("the project folder does not exist")
            folder = (folder or "").strip("/")
            if folder and (".." in folder.split("/") or folder.startswith("-") or "\0" in folder):
                raise FsError("bad path")
            dest_dir = os.path.join(root_real, folder) if folder else root_real
            dest_dir = os.path.realpath(dest_dir)
            if dest_dir != root_real and not dest_dir.startswith(root_real.rstrip(os.sep) + os.sep):
                raise FsError("this path is outside the project")
            if not os.path.isdir(dest_dir):
                raise FsError("folder not found")
            data = sys.stdin.buffer.read()
            final_name = fs_free(dest_dir, name)
            dest = os.path.join(dest_dir, final_name)
            tmp = dest + ".sw-up"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
            print(json.dumps({"path": fs_rel(root_real, dest), "size": len(data)}))
            return
        if op not in ("list", "read", "all"):
            raise FsError("bad request")
        if op == "all":
            print(json.dumps({"files": fs_all(root)}))
            return
        print(json.dumps(fs_list(root, rel) if op == "list" else fs_read(root, rel)))
    except FsError as e:
        print(json.dumps({"error": str(e)}))
    except OSError as e:
        print(json.dumps({"error": e.strerror or str(e)}))


if __name__ == "__main__":
    fs_main(sys.argv[1:])
'''

_FS = {}

# The server's own files (the Files tab without a project): the SSH user's home folder.
SERVER_FILES = {"name": "", "remote": "~", "local": "", "localExists": False, "endpoint": {}}


def fs_project(app, name: str) -> dict:
    return dict(SERVER_FILES) if name in ("", "~") else app._project(name)


def fs_change_call(app, project: dict, op: str, body: dict) -> dict:
    """Create, rename, copy, move, delete or save, in a project's folder or the server's home."""
    path, arg = body.get("path"), body.get("to", body.get("name", body.get("mtime", "")))
    ns = fs_local()
    if op not in ns["FS_CHANGES"]:
        raise ApiError(404, "not found")
    if not isinstance(path, str) or not git_path_ok(path.strip("/")):
        raise ApiError(400, "bad path")
    arg = "" if arg is None else str(arg)
    if op in ("copy", "move") and not fs_path_ok(arg):
        raise ApiError(400, "bad target folder")
    if op == "write" and body.get("force"):
        arg = ""
    text = body.get("text") if op == "write" else None
    if op == "write" and not isinstance(text, str):
        raise ApiError(400, "no text")
    where = "server" if app.web else body.get("where")
    if where not in WHERE:
        where = "local" if project.get("localExists") else "server"
    if where == "local" and not project.get("localExists"):
        where = "server"
    if where == "server" and not (project.get("remote") and app.conf.configured):
        raise ApiError(400, "this project has no folder to change")
    if where == "local":
        try:
            res = ns["fs_change"](project["local"], op, path, arg, text)
        except ns["FsError"] as e:
            raise ApiError(409 if str(e).startswith("changed:") else 400, str(e).replace("changed: ", ""))
        except OSError as e:
            raise ApiError(400, e.strerror or str(e))
    else:
        conf = app._project_conf(project) if project.get("name") else app.conf
        res = remote_python(conf, FS_PY, [op, project["remote"], path, arg],
                            multiplex=bool(app.store.settings.get("sshMultiplex")),
                            timeout=120, stdin=text.encode("utf-8") if op == "write" else None)
        if not isinstance(res, dict):
            raise ApiError(502, "bad answer from the server")
        if res.get("error"):
            e = str(res["error"])
            raise ApiError(409 if e.startswith("changed:") else 400, e.replace("changed: ", ""))
    res["where"] = where
    name = project.get("name") or "server"
    app.activity.add("files", project.get("name") or "", f"{op} {path}" + (f" → {arg}" if op in ("rename", "copy", "move") else ""))
    log_line(f"files: {name}: {op} {path}")
    return res


def fs_upload_call(app, project: dict, query: dict, data: bytes) -> dict:
    """Upload raw bytes to a project folder (or server home). Finds a free name if the file exists."""
    folder = (query.get("path") or [""])[0].strip("/")
    name = (query.get("name") or [""])[0]
    ns = fs_local()
    try:
        ns["fs_name_ok"](name)
    except ns["FsError"] as e:
        raise ApiError(400, str(e))
    if folder and not git_path_ok(folder):
        raise ApiError(400, "bad path")
    if not data:
        raise ApiError(400, "no data")
    if len(data) > 50_000_000:
        raise ApiError(413, "file too large (max 50 MB)")
    where = "server" if app.web else ("local" if project.get("localExists") else "server")
    if where == "server" and not (project.get("remote") and app.conf.configured):
        raise ApiError(400, "this project has no folder")
    if where == "local":
        import os as _os
        root = _os.path.realpath(_os.path.expanduser(project["local"]))
        dest_dir = _os.path.join(root, folder) if folder else root
        dest_dir = _os.path.realpath(dest_dir)
        if dest_dir != root and not dest_dir.startswith(root.rstrip(_os.sep) + _os.sep):
            raise ApiError(400, "path outside project")
        if not _os.path.isdir(dest_dir):
            raise ApiError(400, "folder not found")
        final_name = ns["fs_free"](dest_dir, name)
        dest = _os.path.join(dest_dir, final_name)
        tmp = dest + ".sw-up"
        with open(tmp, "wb") as f:
            f.write(data)
        _os.replace(tmp, dest)
        rel = _os.path.relpath(dest, root).replace(_os.sep, "/")
        app.activity.add("files", project.get("name") or "", f"upload {rel}")
        return {"path": rel, "size": len(data)}
    else:
        conf = app._project_conf(project) if project.get("name") else app.conf
        res = remote_python(conf, FS_PY, ["upload", project["remote"], folder, name],
                            multiplex=bool(app.store.settings.get("sshMultiplex")),
                            timeout=120, stdin=data)
        if not isinstance(res, dict):
            raise ApiError(502, "bad answer from the server")
        if res.get("error"):
            raise ApiError(400, str(res["error"]))
        app.activity.add("files", project.get("name") or "", f"upload {res.get('path', name)}")
        return res


# ----------------------------------------------------------------------------- prune (not-synced copies)
# Finds / deletes what a list of Mutagen ignore patterns names inside a project folder — for
# copies made before a pattern was added. Same script both ways: exec'd here for this machine,
# `python3 -c` over SSH for the server. Patterns: no "/" → any level (by name); "/x" or "a/b"
# → from the top; "dir/" folders only; * ? [..] inside a name, ** across folders; "!x" skipped.
# .git is never entered or touched; links are removed, never followed.
PRUNE_PY = r"""
import sys, os, re, json, shutil, stat

MAX_ITEMS, MAX_SCAN = 500, 400000


def prune_rx(glob):
    out, i = "", 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**/", i):
            out += "(?:.*/)?"; i += 3; continue
        if glob.startswith("**", i):
            out += ".*"; i += 2; continue
        if c == "*":
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "[":
            j = glob.find("]", i + 1)
            if j < 0:
                out += re.escape(c)
            else:
                body = glob[i + 1:j]
                out += "[" + ("^" + body[1:] if body[:1] == "!" else body).replace("\\", "\\\\") + "]"
                i = j
        else:
            out += re.escape(c)
        i += 1
    return re.compile(out + r"\Z")


def prune_compile(patterns):
    rules = []
    for p in patterns:
        p = (p or "").strip()
        if not p or p.startswith("!") or p.startswith("#"):
            continue
        dir_only = p.endswith("/")
        p = p.rstrip("/")
        anchored = "/" in p
        p = p.lstrip("/")
        if p:
            rules.append((prune_rx(p), anchored, dir_only))
    return rules


def prune_match(rules, rel, is_dir):
    name = rel.rsplit("/", 1)[-1]
    for rx, anchored, dir_only in rules:
        if dir_only and not is_dir:
            continue
        if rx.match(rel if anchored else name):
            return True
    return False


def prune_size(path, budget):
    total = 0
    for top, dirs, files in os.walk(path):
        for f in files:
            budget[0] -= 1
            try:
                total += os.lstat(os.path.join(top, f)).st_size
            except OSError:
                pass
        if budget[0] <= 0:
            break
    return total


def prune_find(root, patterns):
    root = os.path.realpath(os.path.expanduser(root))
    if not os.path.isdir(root):
        return {"items": [], "total": 0, "truncated": False, "missing": True}
    rules = prune_compile(patterns)
    items, budget, stack, truncated = [], [MAX_SCAN], [""], False
    while stack and rules:
        rel_dir = stack.pop()
        try:
            entries = sorted(os.scandir(os.path.join(root, rel_dir) if rel_dir else root), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            budget[0] -= 1
            if e.name == ".git":
                continue
            rel = rel_dir + "/" + e.name if rel_dir else e.name
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if prune_match(rules, rel, is_dir):
                if len(items) >= MAX_ITEMS:
                    truncated = True
                    continue
                try:
                    size = prune_size(e.path, budget) if is_dir else e.stat(follow_symlinks=False).st_size
                except OSError:
                    size = 0
                items.append({"path": rel, "dir": is_dir, "size": size})
            elif is_dir:
                stack.append(rel)
        if budget[0] <= 0:
            truncated = True
            break
    items.sort(key=lambda x: x["path"])
    return {"items": items, "total": sum(x["size"] for x in items), "truncated": truncated}


def prune_delete(root, patterns, paths):
    # deletes only what the patterns still name now AND the user saw in the list
    root = os.path.realpath(os.path.expanduser(root))
    want = set(paths or [])
    found = [x for x in prune_find(root, patterns)["items"] if x["path"] in want]
    done, errors = 0, []
    for x in found:
        full = os.path.join(root, x["path"])
        parent = os.path.realpath(os.path.dirname(full))
        if not (parent == root or parent.startswith(root + os.sep)) or ".git" in x["path"].split("/"):
            errors.append(x["path"] + ": outside the project"); continue
        try:
            st = os.lstat(full)
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(full)
            else:
                os.unlink(full)
            done += 1
        except OSError as e:
            errors.append(x["path"] + ": " + (e.strerror or str(e)))
    return {"deleted": done, "errors": errors[:20]}


def prune_main(argv):
    try:
        op, root, patterns = argv[0], argv[1], json.loads(argv[2])
        if op == "find":
            print(json.dumps(prune_find(root, patterns)))
        elif op == "delete":
            print(json.dumps(prune_delete(root, patterns, json.loads(argv[3]))))
        else:
            print(json.dumps({"error": "bad request"}))
    except (OSError, ValueError, IndexError) as e:
        print(json.dumps({"error": str(e)}))


if __name__ == "__main__":
    prune_main(sys.argv[1:])
"""
_PRUNE = {}


def prune_call(app, project: dict, where: str, op: str, patterns: list, paths: list = None) -> dict:
    """Find (op "find") or delete (op "delete") what the patterns name, on one side of a project."""
    args = [op, project["local"] if where == "local" else project["remote"], json.dumps(patterns)]
    if op == "delete":
        args.append(json.dumps(paths or []))
    if where == "local":
        if not _PRUNE:
            ns = {"__name__": "simab_prune"}
            exec(compile(PRUNE_PY, "<prune>", "exec"), ns)
            _PRUNE.update(ns)
        f = _PRUNE["prune_find"] if op == "find" else _PRUNE["prune_delete"]
        res = f(*([args[1], patterns] + ([paths or []] if op == "delete" else [])))
    else:
        res = remote_python(app._project_conf(project), PRUNE_PY, args,
                            multiplex=bool(app.store.settings.get("sshMultiplex")), timeout=300 if op == "delete" else 90)
    if not isinstance(res, dict):
        raise ApiError(502, "bad answer from the server")
    if res.get("error"):
        raise ApiError(400, str(res["error"]))
    return res


def fs_local():
    """The FS_PY functions, loaded once into this process."""
    if not _FS:
        ns = {"__name__": "simab_fs"}
        exec(compile(FS_PY, "<fs>", "exec"), ns)
        _FS.update(ns)
    return _FS


def fs_path_ok(path: str) -> bool:
    return path == "" or git_path_ok(path.strip("/"))


def fs_call(app, project: dict, op: str, path: str, where: str = None) -> dict:
    """List a folder / read a file of a project, on this machine or on the server."""
    if op not in ("list", "read"):
        raise ApiError(404, "not found")
    if not isinstance(path, str) or not fs_path_ok(path):
        raise ApiError(400, "bad path")
    if app.web:
        where = "server"
    if where not in WHERE:
        where = "local" if project.get("localExists") else "server"
    if where == "local" and not project.get("localExists"):
        where = "server"
    if where == "server" and not (project.get("remote") and app.conf.configured):
        raise ApiError(400, "this project has no folder to show")
    if where == "local":
        ns = fs_local()
        try:
            res = ns["fs_list"](project["local"], path) if op == "list" else ns["fs_read"](project["local"], path)
        except ns["FsError"] as e:
            raise ApiError(400, str(e))
        except OSError as e:
            raise ApiError(400, e.strerror or str(e))
    else:
        conf = app._project_conf(project) if project.get("name") else app.conf
        res = remote_python(conf, FS_PY, [op, project["remote"], path],
                            multiplex=bool(app.store.settings.get("sshMultiplex")), timeout=40)
        if not isinstance(res, dict):
            raise ApiError(502, "bad answer from the server")
        if res.get("error"):
            raise ApiError(400, str(res["error"]))
    res["where"] = where
    return res


class FilesSession:
    """A Files tab: no process, it only keeps its place among the tabs (and after a restart)."""
    win = ""
    moved = 0.0

    def __init__(self, sid: str, project: str, title: str = ""):
        self.id = sid
        self.spec = {"kind": "files", "project": project, "where": ""}
        self.title = title or "Files · " + project
        self.alive = True
        self.exit_code = None
        self.started = time.time()

    def info(self) -> dict:
        return {"id": self.id, "title": self.title, "spec": self.spec, "url": "",
                "alive": True, "exitCode": None, "started": self.started, "runs": 1,
                "win": self.win, "moved": self.moved}

    def attach(self, ws):
        raise WebSocketClosed()

    def detach(self, ws):
        pass

    def kill(self, wait=False):
        pass

    def stop(self):
        pass

    def restart(self):
        pass


# ----------------------------------------------------------------------------- activity history
class Activity:
    """What you did (newest first when listed), in the activity table."""
    LIMIT = 50000

    def __init__(self, path: Path = None):
        self.db, legacy = db_and_legacy(path, "activity.jsonl")
        self.path = self.db.path

        def load(f):
            rows = []
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    it = json.loads(line)
                except ValueError:
                    continue
                if isinstance(it, dict) and it.get("kind"):
                    rows.append(self._row(it))
            with self.db.tx() as c:
                c.executemany("INSERT INTO activity (t, kind, project, text, extra) VALUES (?, ?, ?, ?, ?)", rows)
        self.db.import_once("activity.jsonl", legacy, load)

    @staticmethod
    def _row(it: dict) -> tuple:
        extra = {k: v for k, v in it.items() if k not in ("t", "kind", "project", "text")}
        return (float(it.get("t") or 0), str(it.get("kind")), str(it.get("project") or ""), str(it.get("text") or "")[:500],
                json.dumps(extra, ensure_ascii=False))

    def add(self, kind: str, project: str = "", text: str = "", **extra):
        item = {"t": round(time.time(), 3), "kind": kind, "project": project or "", "text": (text or "")[:500]}
        item.update({k: v for k, v in extra.items() if v not in (None, "")})
        try:
            cur = self.db.run("INSERT INTO activity (t, kind, project, text, extra) VALUES (?, ?, ?, ?, ?)", self._row(item))
            if cur.lastrowid and cur.lastrowid % 1000 == 0:
                self.db.run("DELETE FROM activity WHERE id <= ?", (cur.lastrowid - self.LIMIT,))
        except sqlite3.Error as e:
            log_line(f"history: {e}")
        return item

    def list(self, limit=500, project="", query="") -> list:
        sql, args = "SELECT t, kind, project, text, extra FROM activity WHERE 1=1", []
        if project:
            sql += " AND project = ?"
            args.append(project)
        if query:
            sql += " AND instr(lower(text || ' ' || project || ' ' || kind), ?) > 0"
            args.append(query.lower())
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 5000)))
        out = []
        for t, kind, proj, text, extra in self.db.rows(sql, args):
            it = {"t": t, "kind": kind, "project": proj, "text": text}
            try:
                it.update(json.loads(extra))
            except ValueError:
                pass
            out.append(it)
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
    return ssh_base(conf, multiplex=False, batch=True, forwards=True) + [
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
        self.db, legacy = db_and_legacy(path, "usage.json")
        self.path = self.db.path
        self.lock = threading.Lock()

        def load(f):
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                return
            if not isinstance(d, dict):
                return
            if isinstance(d.get("limits"), dict):
                self.db.put("claudeLimits", d["limits"])
            for day, per in (d.get("days") or {}).items():
                for proj, usd in (per or {}).items():
                    if isinstance(usd, (int, float)):
                        self._add(day, proj, usd)
        self.db.import_once("usage.json", legacy, load)

    def _add(self, day, project, usd):
        self.db.run("INSERT INTO cost (day, project, usd) VALUES (?, ?, ?) "
                    "ON CONFLICT(day, project) DO UPDATE SET usd = usd + excluded.usd", (day, project, float(usd)))

    def limits(self, info: dict):
        with self.lock:
            self.db.put("claudeLimits", merge_limits(self.db.get("claudeLimits", {}), info))

    def cost(self, project: str, usd: float, day: str = None):
        if not isinstance(usd, (int, float)) or isinstance(usd, bool) or usd <= 0:
            return
        with self.lock:
            self._add(day or time.strftime("%Y-%m-%d"), project or "home", usd)
            self.db.run("DELETE FROM cost WHERE day NOT IN (SELECT DISTINCT day FROM cost ORDER BY day DESC LIMIT ?)",
                        (self.KEEP_DAYS,))

    def summary(self, now: float = None) -> dict:
        now = time.time() if now is None else now
        stored = self.db.get("claudeLimits", {}) or {}
        lim = merge_limits(stored, None, now) if stored else {}
        if stored:
            lim["updated"] = stored.get("updated", 0)
        days = {}
        for day, proj, usd in self.db.rows("SELECT day, project, usd FROM cost"):
            days.setdefault(day, {})[proj] = round(usd, 6)
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
    if IS_WIN and conf.key:       # C:/Users/me/.ssh/id_ed25519 works for Python, Git Bash and ssh alike
        conf = ServerConf(conf.user, conf.host, conf.port, conf.key.replace("\\", "/"))
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
    r = subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-C", f"{os.environ.get('USER') or os.environ.get('USERNAME') or 'me'}@{socket.gethostname()}", "-f", str(path)],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise ApiError(500, "ssh-keygen failed: " + r.stderr.strip()[-200:])
    return str(path)


def install_ssh_key(conf: ServerConf, password: str, timeout=45) -> str:
    """ssh-copy-id with the server password typed into its prompt (once). The password is never stored."""
    if not conf.key or not os.path.isfile(conf.key + ".pub"):
        raise ApiError(400, "choose or create a key first")
    tool = which("ssh-copy-id")
    if tool:
        argv = [tool, "-o", "StrictHostKeyChecking=accept-new", "-i", conf.key + ".pub", "-p", str(conf.port), conf.target]
    else:
        pub = Path(conf.key + ".pub").read_text().strip()
        argv = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-p", str(conf.port), conf.target,
                "umask 077; mkdir -p ~/.ssh; grep -qxF " + shlex.quote(pub) + " ~/.ssh/authorized_keys 2>/dev/null || echo " + shlex.quote(pub) + " >> ~/.ssh/authorized_keys"]
    sent, text = ssh_with_password(argv, password, timeout)
    if sent >= 2 or re.search(r"Permission denied", text):
        raise ApiError(400, "wrong password (or password login is disabled on the server)")
    return text[-500:]


def ssh_with_password(argv: list, password: str, timeout=45) -> tuple:
    """Run argv in a terminal and type the password at each NEW password prompt (at most twice).
    Returns (prompts answered, output)."""
    if IS_WIN:
        return ssh_with_password_win(argv, password, timeout)
    import pty
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
        os.kill(pid, SIGKILL)
    except OSError:
        pass
    try:
        _, st = os.waitpid(pid, 0)
    except OSError:
        st = 1
    try:
        os.close(fd)
    except OSError:
        pass
    return sent, out.decode("utf-8", "replace")


ANSI_RE = re.compile(rb"\x1b(\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[()][0-9A-Za-z]|[=>78])")


def asks_password(out: bytes) -> bool:
    """A password prompt at the end of the output (terminal escape codes ignored — Windows'
    pseudo console sends plenty of them)."""
    return bool(re.search(rb"assword[^\n]*:[ \t]*$", ANSI_RE.sub(b"", out).replace(b"\r", b"")))


def ssh_with_password_win(argv: list, password: str, timeout=45) -> tuple:
    """Windows: the same, in a pseudo console (ssh reads the password from the console)."""
    env = dict(os.environ, PATH=tool_path())
    try:
        con = ConPty(argv, os.path.expanduser("~"), env, 200, 50)
    except OSError as e:
        raise ApiError(500, f"could not start ssh: {e}")
    chunks = collections.deque()

    def reader():
        while True:
            c = con.read(4096)
            if not c:
                break
            chunks.append(c)
        chunks.append(None)
    threading.Thread(target=reader, daemon=True).start()
    out, sent, end, mark, done = b"", 0, time.time() + timeout, 0, False
    while time.time() < end and not done:
        if not chunks:
            time.sleep(0.05)
            continue
        c = chunks.popleft()
        if c is None:
            break
        out += c
        if asks_password(out[mark:]) and sent < 2:
            time.sleep(0.15)
            try:
                con.write(password.encode() + b"\r")
            except OSError:
                break
            sent += 1
            out += b"\n"
            mark = len(out)
    con.proc.kill()
    con.close()
    return sent, ANSI_RE.sub(b"", out).decode("utf-8", "replace")


def png_to_ico(png: bytes) -> bytes:
    """An .ico holding one PNG image (Windows Vista+ reads those) — for the Start menu shortcut."""
    w, h = struct.unpack(">II", png[16:24])
    return (struct.pack("<HHH", 0, 1, 1)
            + struct.pack("<BBBBHHII", w if w < 256 else 0, h if h < 256 else 0, 0, 0, 1, 32, len(png), 22) + png)


def ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def windows_shortcut_script(lnk: str, target: str, args: str, workdir: str, icon: str) -> str:
    return (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({ps_quote(lnk)});"
            f"$s.TargetPath={ps_quote(target)};$s.Arguments={ps_quote(args)};"
            f"$s.WorkingDirectory={ps_quote(workdir)};$s.IconLocation={ps_quote(icon)};"
            "$s.Description='Simab Workspace';$s.Save()")


def make_windows_shortcut() -> str:
    """Start menu entry: pythonw.exe server.py open (no console window)."""
    icon = config_dir() / "simabworkspace.ico"
    icon.parent.mkdir(parents=True, exist_ok=True)
    icon.write_bytes(png_to_ico((STATIC / "icon-192.png").read_bytes()))
    progs = Path(os.environ.get("APPDATA") or home_path("AppData", "Roaming"), "Microsoft", "Windows", "Start Menu", "Programs")
    progs.mkdir(parents=True, exist_ok=True)
    ps = windows_shortcut_script(str(progs / "Simab Workspace.lnk"), win_python(gui=True),
                                 subprocess.list2cmdline([str(HERE / "server.py"), "open"]), os.path.expanduser("~"), str(icon))
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, timeout=60)
    if r.returncode != 0:
        raise ApiError(500, "could not create the Start menu shortcut: " + r.stderr.decode("utf-8", "replace").strip()[-200:])
    return "Windows Start menu"


def open_in_file_manager(path: str):
    if IS_WIN:
        os.startfile(path)                     # Explorer
        return
    if is_wsl():
        win = subprocess.run(["wslpath", "-w", path], capture_output=True, text=True, timeout=5).stdout.strip() or path
        argv = ["explorer.exe", win]
    elif sys.platform == "darwin":
        argv = ["open", path]
    else:
        argv = ["xdg-open", path]
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     cwd="/mnt/c" if argv[0] == "explorer.exe" and os.path.isdir("/mnt/c") else None, **detached_kwargs())


FOLDER_DIALOG_PS = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
    "$d.Description='Folder for the project on this computer';$d.ShowNewFolderButton=$true;"
    "$d.SelectedPath=$env:SW_START;"
    "$o=New-Object System.Windows.Forms.Form -Property @{TopMost=$true};"
    "if($d.ShowDialog($o) -eq 'OK'){[Console]::OutputEncoding=[Text.Encoding]::UTF8;[Console]::Write($d.SelectedPath)}")


def folder_dialog_argv(start: str):
    """The native "choose a folder" dialog of this machine, or None."""
    if IS_WIN or is_wsl():
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command", FOLDER_DIALOG_PS]
    if sys.platform == "darwin":
        return ["osascript", "-e", 'POSIX path of (choose folder with prompt "Folder for the project")']
    if shutil.which("zenity"):
        return ["zenity", "--file-selection", "--directory", "--filename=" + start.rstrip("/") + "/"]
    if shutil.which("kdialog"):
        return ["kdialog", "--getexistingdirectory", start]
    return None


def pick_folder_dialog(start: str) -> str:
    """The folder picked in the native dialog ("" = cancelled)."""
    argv = folder_dialog_argv(start)
    if not argv:
        raise ApiError(400, "no folder dialog on this machine — type the path")
    env = dict(os.environ, SW_START=start)
    if is_wsl():
        win = subprocess.run(["wslpath", "-w", start], capture_output=True, text=True, timeout=5).stdout.strip()
        env["SW_START"] = win
        env["WSLENV"] = (os.environ.get("WSLENV", "") + ":SW_START").lstrip(":")
    try:
        r = subprocess.run(argv, capture_output=True, timeout=600, env=env, stdin=subprocess.DEVNULL,
                           cwd="/mnt/c" if is_wsl() and os.path.isdir("/mnt/c") else None)
    except (OSError, subprocess.SubprocessError) as e:
        raise ApiError(500, f"could not open the folder dialog: {e}")
    path = r.stdout.decode("utf-8", "replace").strip()
    if r.returncode != 0 or not path:
        return ""
    if is_wsl():
        path = subprocess.run(["wslpath", "-u", path], capture_output=True, text=True, timeout=5).stdout.strip() or path
    return path.rstrip("/") if len(path) > 1 and not re.fullmatch(r"[A-Za-z]:[\\/]", path) else path


def desktop_quote(arg: str) -> str:
    """Quoting rules of .desktop Exec lines (double quotes only)."""
    if re.fullmatch(r"[A-Za-z0-9_./~-]+", arg):
        return arg
    return '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'


def windows_startup_file() -> Path:
    return Path(os.environ.get("APPDATA") or home_path("AppData", "Roaming"), "Microsoft", "Windows", "Start Menu",
                "Programs", "Startup", "simabworkspace-sync.cmd")


def autostart_enabled() -> bool:
    if IS_WIN:
        return windows_startup_file().is_file()
    return any(RC_MARK_BEGIN in (home_path(rc).read_text(errors="replace") if home_path(rc).is_file() else "") for rc in (".bashrc", ".zshrc"))


def set_autostart(on: bool):
    if IS_WIN:                  # a tiny script in the Startup folder starts the sync service at sign-in
        f = windows_startup_file()
        exe = which("mutagen")
        if on:
            if not exe:
                raise ApiError(400, "mutagen is not installed yet")
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f'@echo off\r\n"{exe}" daemon start >nul 2>&1\r\n')
        elif f.is_file():
            f.unlink()
        return
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
    def __init__(self, store: Store = None, conf: ServerConf = None, web: bool = False):
        self.web = web                # a workspace of the web version: server only, no local side
        self.store = store or Store()
        self.activity = Activity(self.store.path.parent / "activity.jsonl")      # same simab.db; imports the old file
        self._conf = conf
        tok = os.environ.pop("SIMAB_TOKEN", "")
        self.token = tok if re.fullmatch(r"[A-Za-z0-9_-]{20,64}", tok or "") else secrets.token_urlsafe(24)
        self.port = 0
        self.sessions = {}            # id -> Session (insertion order = tab order)
        self.windows = {}             # extra windows: id -> {"seen": time, "n": number shown to the user}
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
        p = merge_projects([] if self.web else mutagen_sessions(), self.store.manual(), self.store.data["projects"])
        if self.web:                  # "local" would be the web server's own disk
            for x in p:
                x["local"], x["localExists"] = "", False
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
        wid = self._win_id(body.get("win"))
        info = self._create_session(body)
        if wid:
            self.set_window(info["id"], wid)
            info = self.get(info["id"]).info()
        return info

    def _create_session(self, body: dict) -> dict:
        sid = secrets.token_hex(6)
        if body.get("kind") == "chat" and isinstance(body.get("resume"), str) and body["resume"]:
            same = next((x for x in list(self.sessions.values()) if isinstance(x, ChatSession) and not x.closed
                         and body["resume"] in (x.session_id, x.spec.get("resume"))), None)
            if same:
                return same.info()          # already open (maybe in another window): that tab, not a second Claude
        if body.get("kind") == "chat" and body.get("watch") is True:
            conv = body.get("resume") if isinstance(body.get("resume"), str) else ""
            if not UUID_RE.fullmatch(conv):
                raise ApiError(400, "which conversation?")
            built = build_chat(dict(body, relay=None), self.conf, self.store, self.projects(max_age=10))
            pname = built["spec"]["project"]
            if not (isinstance(body.get("title"), str) and body["title"].strip()):
                built["title"] = "Watch · " + (pname or "home")
            try:
                built["history"] = remote_python(built["conf"], REMOTE_HISTORY_PY,
                                                 [claude_project_dir(built["rpath"]), conv, "", str(HISTORY_PAGE)],
                                                 multiplex=bool(self.store.settings.get("sshMultiplex")))
                start = remote_python(built["conf"], REMOTE_TAIL_PY, [claude_project_dir(built["rpath"]), conv, "-1"],
                                      multiplex=bool(self.store.settings.get("sshMultiplex")))
                offset = start.get("off", 0) if isinstance(start, dict) else 0
            except ApiError as e:
                raise ApiError(502, "cannot read this conversation on the server: " + e.message)

            def status():
                return self.claude_running_one(pname, conv)
            w = WatchSession(sid, built, offset, status_fn=status, multiplex=bool(self.store.settings.get("sshMultiplex")))
            w.start()
            with self.lock:
                self.sessions[sid] = w
            self._remember()
            return w.info()
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
        if body.get("kind") == "files":
            title = body.get("title") if isinstance(body.get("title"), str) and body.get("title").strip() else ""
            if not body.get("project"):                 # the server's own files (home folder)
                if not self.conf.configured:
                    raise ApiError(400, "the server is not configured")
                s = FilesSession(sid, "", title[:80] or "Files · server")
            else:
                p = self._project(body.get("project") or "")
                if not (p.get("remote") or (p.get("localExists") and not self.web)):
                    raise ApiError(400, "this project has no folder to show")
                s = FilesSession(sid, p["name"], title[:80])
            with self.lock:
                self.sessions[sid] = s
            self._remember()
            return s.info()
        body = {k: v for k, v in body.items() if k != "_docker"}
        if self.web:
            if body.get("kind") not in WEB_KINDS:
                raise ApiError(403, "not available in the web version")
            if body.get("kind") == "shell":
                body["where"] = "server"
            if body.get("kind") == "run":
                c = self.store.command(body.get("project") or "", body.get("cmdId") or "")
                if c and c.get("where") != "server":
                    raise ApiError(403, "only server commands run in the web version")
        if body.get("kind") == "docker":
            body["_docker"] = self.docker_tab(body.get("project") or "", body.get("container"), body.get("action"))
        built = build_spec(body, self.conf, self.store, self.projects(max_age=10))
        if self.web and built["spec"]["where"] != "server":
            raise ApiError(403, "only server commands run in the web version")
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

    # extra windows (tabs moved out to a second monitor) ---------------------------
    def _win_id(self, wid) -> str:
        if wid in ("", None):
            return ""
        if not isinstance(wid, str) or not WIN_RE.fullmatch(wid):
            raise ApiError(400, "bad window")
        return wid

    def window_seen(self, wid: str):
        wid = self._win_id(wid)
        if not wid:
            return
        with self.lock:
            w = self.windows.get(wid)
            if w:
                w["seen"] = time.time()
            else:
                used = {x["n"] for x in self.windows.values()}
                n = 2
                while n in used:
                    n += 1
                self.windows[wid] = {"seen": time.time(), "n": n}

    def window_closed(self, wid: str):
        """The window is closing — or reloading: its tabs go back to the main window
        unless it says hello again within a few seconds."""
        wid = self._win_id(wid)
        with self.lock:
            w = self.windows.get(wid)
            if w:
                w["seen"] = min(w["seen"], time.time() - WIN_TTL + WIN_GRACE)

    def live_windows(self) -> list:
        """Forget windows that stopped answering; their tabs return to the main window."""
        now = time.time()
        with self.lock:
            for wid, w in list(self.windows.items()):
                if now - w["seen"] > WIN_TTL:
                    del self.windows[wid]
            for s in self.sessions.values():
                if s.win and s.win not in self.windows:
                    s.win, s.moved = "", now
            return sorted(({"id": k, "n": v["n"]} for k, v in self.windows.items()), key=lambda x: x["n"])

    def set_window(self, sid: str, wid) -> dict:
        s = self.get(sid)
        wid = self._win_id(wid)
        self.window_seen(wid)
        s.win, s.moved = wid, time.time()
        return s.info()

    def tabs(self) -> dict:
        wins = self.live_windows()
        return {"sessions": [s.info() for s in self.sessions.values()], "windows": wins, "now": time.time()}

    def get(self, sid: str) -> Session:
        s = self.sessions.get(sid)
        if not s:
            raise ApiError(404, "this tab no longer exists")
        return s

    def close_session(self, sid: str, keep=False):
        """keep: a chat tab closed while Claude works — Claude finishes on the server (it can be
        opened again from Sessions); otherwise it is stopped unless another device is on it too."""
        s = self.get(sid)
        with self.lock:
            self.sessions.pop(sid, None)
        if keep and isinstance(s, ChatSession):
            s.kill(remote=False)
        else:
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
        t = clean_str(title, 80, "title", required=True)
        if isinstance(s, ChatSession):
            s.rename(t)
        else:
            s.title = t
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
        sc = str(sc) if sc.is_file() else installed_script()
        if not sc:
            raise ApiError(400, "simabworkspace script not found")
        return sc

    def run_script(self, args: list, timeout=180, stdin: bytes = None, env: dict = None):
        """→ (exit code, output without colours). SW_NONINTERACTIVE: the script never starts a wizard."""
        e = dict(os.environ, PATH=tool_path(), SW_NONINTERACTIVE="1")
        e.update(env or {})
        try:
            r = subprocess.run([bash_exe(), self.script()] + args, input=stdin,
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
                          "sudo": name not in ("mutagen", "jq") and not IS_WIN})
        docker = which("docker")
        dok = False
        if docker:
            try:
                dok = subprocess.run([docker, "compose", "version"], capture_output=True, timeout=10).returncode == 0
            except (OSError, subprocess.SubprocessError):
                dok = False
        sudo_free = False
        if which("sudo") and not IS_WIN:
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

    @staticmethod
    def _clean_ignores(ignores) -> list:
        if not isinstance(ignores, list) or len(ignores) > 200:
            raise ApiError(400, "bad list")
        out, seen = [], set()
        for x in ignores:
            if not isinstance(x, str):
                raise ApiError(400, "bad list")
            x = x.strip()
            if not x or x in seen:
                continue
            if len(x) > 300 or "," in x or any(ord(c) < 32 for c in x):
                raise ApiError(400, f"not a valid pattern: {x[:60]}")
            seen.add(x)
            out.append(x)
        return out

    def _synced(self, name: str) -> dict:
        p = next((x for x in self.projects(max_age=5) if x["name"] == name), None)
        if not p or p.get("source") != "mutagen":
            raise ApiError(404, "this project is not synced")
        return p

    def _sides(self, p: dict) -> list:
        return [w for w in ("server", "local")
                if (w == "local" and p.get("local") and os.path.isdir(p["local"])) or (w == "server" and p.get("remote"))]

    def ignores_preview(self, name: str, ignores) -> dict:
        """What the newly added patterns name on each side now (copies made before) — shown before Save."""
        p = self._synced(name)
        new = self._clean_ignores(ignores)
        cur = (self._session(name).get("configuration") or {}).get("ignores") or []
        added = [x for x in new if x not in cur]
        out = {"added": added}
        for w in ("server", "local"):
            if not added or w not in self._sides(p):
                out[w] = {"items": [], "total": 0, "truncated": False}
                continue
            try:
                out[w] = prune_call(self, p, w, "find", added)
            except ApiError as e:
                out[w] = {"items": [], "total": 0, "truncated": False, "error": e.message}
        return out

    def set_ignores(self, name: str, ignores, delete=None) -> dict:
        """What is never synced for a project (Mutagen ignore patterns); the sync is recreated.
        delete = {"server": [paths], "local": [paths]}: copies of newly added patterns to remove
        afterwards (only after the new sync ignores them, so the deletion is not synced over)."""
        p = self._synced(name)
        out = self._clean_ignores(ignores)
        delete = delete if isinstance(delete, dict) else {}
        cur = []
        if delete:
            cur = (self._session(name).get("configuration") or {}).get("ignores") or []
        added = [x for x in out if x not in cur]
        code, text = self.run_script(["ignore", name, ",".join(out)], timeout=900)
        self.invalidate()
        if code != 0:
            raise ApiError(400, self.script_error(text, "could not change what is not synced"))
        self.activity.add("sync-ignores", name, "not synced: " + (", ".join(out) or "nothing"))
        res = {"ok": True, "ignores": out, "deleted": {}}
        for w in ("server", "local"):
            paths = delete.get(w)
            if not paths or not added or w not in self._sides(p):
                continue
            if not isinstance(paths, list) or not all(isinstance(x, str) for x in paths):
                raise ApiError(400, "bad list")
            try:
                r = prune_call(self, p, w, "delete", added, paths)
            except ApiError as e:
                r = {"deleted": 0, "errors": [e.message]}
            res["deleted"][w] = r
            self.activity.add("sync-prune", name, f"deleted {r.get('deleted', 0)} not-synced item(s) on "
                              + ("the server" if w == "server" else "this machine"))
        return res

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
            r = subprocess.run(ssh_base(conf, multiplex=bool(self.store.settings.get("sshMultiplex"))) + ["-T", "-o", "BatchMode=yes", conf.target, posix_sh(cmd)],
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

    def server_stats(self, top=False) -> dict:
        """CPU / memory / disk of the server (cached 3 s: every window asks)."""
        cache = self.__dict__.setdefault("_stats", {"t": 0.0, "data": None})
        if not top and cache["data"] is not None and time.time() - cache["t"] < 3:
            return cache["data"]
        if not self.conf.configured:
            return {"error": "the server is not configured"}
        try:
            r = remote_python(self.conf, SERVER_STATS_PY, ["top"] if top else [],
                              multiplex=bool(self.store.settings.get("sshMultiplex")), timeout=20) or {}
        except ApiError as e:
            r = {"error": e.message}
        r["t"] = time.time()
        if not top:
            cache["t"], cache["data"] = time.time(), r
        return r

    def server_ls(self, path, project: str = "") -> dict:
        """The entries of one folder on the server (files too) — path completion in the chat."""
        path = clean_str(path, 2000, "path") or "~/"
        if "\0" in path:
            raise ApiError(400, "bad path")
        conf = self._project_conf(self._project(project)) if project else self.conf
        if not conf.configured:
            raise ApiError(400, "the server is not configured")
        r = remote_python(conf, SERVER_LS_PY, [path], multiplex=bool(self.store.settings.get("sshMultiplex"))) or {}
        if r.get("error"):
            raise ApiError(400, str(r["error"])[:300])
        return r

    def browse(self, path) -> dict:
        """Folders inside one folder on the server — for picking a project folder."""
        path = clean_str(path, 1000, "folder") or "~"
        r = remote_python(self.conf, BROWSE_PY, [path], multiplex=bool(self.store.settings.get("sshMultiplex"))) or {}
        if r.get("error"):
            raise ApiError(400, str(r["error"])[:300])
        used = {}
        for p in self.projects(max_age=10):
            if p.get("remote"):
                used[p["remote"].rstrip("/")] = p["name"]
                if p["remote"].startswith("~/") and r.get("home"):
                    used[r["home"] + p["remote"][1:].rstrip("/")] = p["name"]
        for d in r.get("dirs") or []:
            d["project"] = used.get(r["path"].rstrip("/") + "/" + d["name"], "")
        r["project"] = used.get(r["path"].rstrip("/"), "")
        return r

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
        script = str(script) if script.is_file() else installed_script()
        if not script:
            raise ApiError(400, "simabworkspace script not found")
        argv = [bash_exe(), script, "git", name, "on" if on else "off"]

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
                "ignores": cfg.get("ignores") or [], "defaultIgnores": default_ignores(), "git": str(cfg.get("ignoreVCSMode") or "default") not in ("ignore", "1"),
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
        made = [make_windows_shortcut()] if IS_WIN else self._unix_shortcut()
        self.activity.add("shortcut", "", "added to " + ", ".join(made))
        return {"ok": True, "where": made}

    def _unix_shortcut(self) -> list:
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
        return made

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
        r = subprocess.run([bash_exe(), path, "--no-open"], stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=300, env=dict(os.environ, PATH=tool_path()), cwd=os.path.expanduser("~"))
        shutil.rmtree(tmp, ignore_errors=True)
        if r.returncode != 0:
            raise ApiError(500, "update failed: " + re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", (r.stdout + r.stderr).decode("utf-8", "replace")).strip()[-400:])
        self.activity.add("update", "", f"updated {VERSION} → {info['latest']}")
        # start the new version on the same address with the same key, then stop this one
        cmd = home_path(".local", "bin", "simabworkspace")
        if IS_WIN:                  # the installer refreshed the copy this server runs from
            argv = [win_python(), str(HERE / "server.py"), "serve"]
        else:
            argv = [str(cmd), "gui", "serve"] if cmd.is_file() else [sys.executable, str(HERE / "server.py"), "serve"]
        log = config_dir() / "gui.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as lf:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, **detached_kwargs(),
                             cwd=os.path.expanduser("~"), env=dict(os.environ, PATH=tool_path(), SIMAB_TOKEN=self.token, SIMAB_RESTART="1"))
        threading.Thread(target=shutdown_app, args=(self,), daemon=True).start()
        return {"ok": True, "version": info["latest"]}

    # git badges ---------------------------------------------------------------
    def git_summary_one(self, p: dict, fetch=False) -> dict:
        info = git_repos(self, p)
        out = {"changes": 0, "ahead": 0, "behind": 0, "conflicts": 0, "repos": len(info["repos"]),
               "noUpstream": 0, "t": time.time(), "where": info["where"], "branch": "", "branches": 0}
        names = set()
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
            names.add(st.get("branch") or "")
        out["branches"] = len(names)
        out["repo"] = info["repos"][0] if len(info["repos"]) == 1 else None
        out["branch"] = next(iter(names)) if len(names) == 1 else ""
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
            res = remote_python(self._project_conf(p), FS_PY, ["all", p["remote"]],
                                multiplex=bool(self.store.settings.get("sshMultiplex")), timeout=60) or {}
            if res.get("error"):
                raise ApiError(400, str(res["error"]))
            files = [x for x in res.get("files") or [] if isinstance(x, str)][:FILE_LIMIT]
        else:
            files = []
        files = with_dirs(files)
        cache[project] = (time.time(), files)
        return files

    def chat_history_page(self, sid: str, before: int) -> dict:
        s = self.get(sid)
        if not isinstance(s, (ChatSession, WatchSession)):
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

    # Claude sessions running on the server (this app, VS Code, terminals) -------------
    def claude_running(self, force=False) -> dict:
        """{project name: [running sessions]} for every project, cached for a few seconds."""
        cache = self.__dict__.setdefault("_running", {"t": 0.0, "data": {}})
        if not force and time.time() - cache["t"] < 4:
            return cache["data"]
        if not self.conf.configured:
            return {}
        groups = {}
        for p in self.projects(max_age=10):
            if not p.get("remote"):
                continue
            c = self._project_conf(p)
            groups.setdefault((c.target, str(c.port)), (c, []))[1].append(p)
        out = {}
        here = {}
        for s in list(self.sessions.values()):
            if isinstance(s, ChatSession) and not s.closed:
                if s.session_id:
                    here[s.session_id] = s.id
                if s.relay_key:
                    here["relay:" + s.relay_key] = s.id
        for conf, projects in groups.values():
            try:
                r = remote_python(conf, CLAUDE_RUNNING_PY, ["list"], multiplex=bool(self.store.settings.get("sshMultiplex"))) or {}
            except ApiError:
                continue
            home = str(r.get("home") or "")
            roots = []
            for p in projects:
                rp = p["remote"].rstrip("/") or "/"
                if rp == "~" or rp.startswith("~/"):
                    rp = home + rp[1:]
                roots.append((rp, p["name"]))
            roots.sort(key=lambda x: -len(x[0]))
            for x in r.get("sessions") or []:
                if not isinstance(x, dict):
                    continue
                cwd = str(x.get("cwd") or "")
                name = next((n for rp, n in roots if cwd == rp or cwd.startswith(rp.rstrip("/") + "/")), "")   # "" = no project
                x["tab"] = here.get(x.get("sessionId")) or here.get("relay:" + str(x.get("relay") or "")) or ""
                out.setdefault(name, []).append(x)
        cache["t"], cache["data"] = time.time(), out
        return out

    def claude_running_one(self, project: str, conv: str):
        for x in (self.claude_running() or {}).get(project, []):
            if x.get("sessionId") == conv:
                return x
        return None

    def claude_stop(self, project: str, pid, conv) -> dict:
        conf = self._project_conf(self._project(project)) if project else self.conf     # "" = no project
        if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(conv, str) or not UUID_RE.fullmatch(conv):
            raise ApiError(400, "which Claude session?")
        r = remote_python(conf, CLAUDE_RUNNING_PY, ["stop", str(pid), conv],
                          multiplex=bool(self.store.settings.get("sshMultiplex")), timeout=30) or {}
        if r.get("error"):
            raise ApiError(409, str(r["error"]))
        self.activity.add("claude-stop", project, conv)
        return {"ok": True, "running": self.claude_running(force=True).get(project, [])}

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
            getattr(s, "shutdown", s.kill)()
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
            editor = [a.strip('"') for a in shlex.split(self.store.settings.get("editorCmd") or "code", posix=not IS_WIN)]
        except ValueError:
            raise ApiError(400, "invalid editor command")
        exe = which(editor[0]) if editor else None
        if not exe:
            raise ApiError(400, f"{editor[0] if editor else 'code'} not found — set the editor command in Settings")
        editor[0] = exe        # Windows: `code` is code.cmd — only found by its full path
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
        windows = self.live_windows()
        return {"version": VERSION, "windows": windows, "now": time.time(), "server": self.conf.to_dict(), "settings": self.store.settings,
                "projects": self.projects(), "sessions": [s.info() for s in self.sessions.values()],
                "lastTabs": self.store.data.get("lastTabs") or [], "wsl": False if self.web else is_wsl(),
                "platform": sys.platform, "web": self.web, "home": "" if self.web else os.path.expanduser("~")}

    # the local folder of a project in Explorer / Finder / the file manager
    def open_folder(self, name: str) -> dict:
        p = self._project(name)
        if not p.get("localExists"):
            raise ApiError(400, "this project has no folder on this machine")
        try:
            open_in_file_manager(p["local"])
        except OSError as e:
            raise ApiError(500, f"could not open the folder: {e}")
        return {"ok": True, "path": p["local"]}

    def pick_folder(self, start: str = "") -> dict:
        start = os.path.expanduser(start or "~")
        for _ in range(64):                    # the nearest folder that exists
            if os.path.isdir(start):
                break
            up = os.path.dirname(start.rstrip("/\\"))
            start = "" if not up or up == start else up
            if not start:
                break
        return {"path": pick_folder_dialog(start or os.path.expanduser("~"))}


def daemon_running() -> bool:
    if IS_WIN:
        try:
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq mutagen.exe", "/NH"], capture_output=True, text=True, timeout=10)
            return "mutagen.exe" in r.stdout.lower()
        except (OSError, subprocess.SubprocessError):
            return False
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


# ----------------------------------------------------------------------------- web version
# `server.py web` serves the same window to browsers (a tablet…) over the network. People
# sign in with an SSH user and password of an allowed server; the password is only used
# to check it and to put a key of this workspace on that server — never stored. Each
# user@server gets its own workspace (projects, chats, history). Nothing runs on the
# web server's own disk: no local terminals, sync, VS Code, port forwarding or upkeep.
WEB_COOKIE = "sw_web"
WEB_KINDS = ("chat", "shell", "claude", "run", "docker")
WEB_BLOCKED = {("POST", "editor"), ("POST", "open-folder"), ("POST", "pick-folder"), ("POST", "sync"), ("POST", "git-sync"), ("GET", "connection"), ("POST", "connection"),
               ("POST", "ssh-key"), ("POST", "autostart"), ("POST", "sync-service"), ("POST", "shortcut"),
               ("GET", "update"), ("POST", "update"), ("GET", "prereqs"), ("POST", "prereqs"), ("POST", "move"),
               ("POST", "uninstall"), ("POST", "quit"), ("POST", "tunnels"), ("GET", "suggest"), ("POST", "add-project")}
SSH_USER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}")
SSH_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}|\[?[0-9A-Fa-f:]+\]?")


def web_verify_password(conf: ServerConf, password: str) -> None:
    """Sign in with the password only (keys off) and, in the same call, put this workspace's key
    on the server. Raises ApiError when the password is wrong."""
    pub = Path(conf.key + ".pub").read_text().strip()
    marker = "SW-OK-" + secrets.token_hex(8)
    q = shlex.quote(pub)
    argv = ["ssh", "-p", str(conf.port), "-o", "StrictHostKeyChecking=accept-new", "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=password,keyboard-interactive", "-o", "NumberOfPasswordPrompts=1",
            "-o", "ConnectTimeout=10", "-o", "ControlMaster=no", "-o", "ControlPath=none", conf.target,
            f"umask 077; mkdir -p ~/.ssh && {{ grep -qxF {q} ~/.ssh/authorized_keys 2>/dev/null || echo {q} >> ~/.ssh/authorized_keys; }} && echo {marker}"]
    sent, text = ssh_with_password(argv, password, timeout=30)
    if marker in text:
        return
    if sent == 0 and re.search(r"Could not resolve|Connection refused|timed out|No route", text):
        raise ApiError(502, "cannot reach " + conf.host + ":" + str(conf.port))
    if sent == 0:
        raise ApiError(401, "this server does not accept password sign-in")
    raise ApiError(401, "wrong user or password")


def ensure_ssh_key(path: Path):
    if path.is_file() and Path(str(path) + ".pub").is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "simabworkspace-web", "-f", str(path)],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    if r.returncode != 0:
        raise ApiError(500, "could not create a key: " + r.stderr.decode("utf-8", "replace")[-200:])


class WebGate:
    SESSION_DAYS = 30
    IDLE_DAYS = 7
    FAIL_WINDOW = 900
    FAILS_PER_IP = 5
    FAILS_ALL = 30

    def __init__(self, root: Path, hosts=None, trust_proxy=False, secure=True, verify=None, start_loops=True, cert_download=""):
        self.cert_download = cert_download      # the server's own certificate, offered for installing on a device
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.hosts = [h for h in (hosts or []) if h]          # "host" or "host:port"; empty = any server
        self.trust_proxy = trust_proxy
        self.secure = secure
        self.verify = verify or web_verify_password
        self.start_loops = start_loops
        self.lock = threading.RLock()
        self.apps = {}
        self.fails = {}
        self.all_fails = collections.deque()
        self.db = open_db(self.root / "gate.db")

        def load(f):
            try:
                old = json.loads(f.read_text())
            except (OSError, ValueError):
                return
            for h, rec in (old.items() if isinstance(old, dict) else []):
                if isinstance(rec, dict) and rec.get("ws"):
                    self.db.run("INSERT OR REPLACE INTO web_sessions (hash, ws, exp, last, ip) VALUES (?, ?, ?, ?, ?)",
                                (h, rec["ws"], rec.get("exp", 0), rec.get("last", 0), rec.get("ip", "")))
        self.db.import_once("sessions.json", self.root / "sessions.json", load)

    @staticmethod
    def _hash(tok: str) -> str:
        return hashlib.sha256(tok.encode()).hexdigest()

    def info(self) -> dict:
        one = self.hosts[0] if len(self.hosts) == 1 else ""
        host, _, port = one.rpartition(":") if ":" in one and not one.startswith("[") else (one, "", "")
        return {"web": True, "version": VERSION, "hosts": self.hosts,
                "host": host or one, "port": port or ("22" if one else ""), "cert": bool(self.cert_download)}

    def host_allowed(self, host: str, port: str) -> bool:
        return not self.hosts or host in self.hosts or f"{host}:{port}" in self.hosts

    def _throttle(self, ip: str):
        now = time.time()
        with self.lock:
            while self.all_fails and now - self.all_fails[0] > self.FAIL_WINDOW:
                self.all_fails.popleft()
            mine = [t for t in self.fails.get(ip, []) if now - t < self.FAIL_WINDOW]
            self.fails[ip] = mine
            if len(mine) >= self.FAILS_PER_IP or len(self.all_fails) >= self.FAILS_ALL:
                raise ApiError(429, "too many failed sign-ins — wait 15 minutes")

    def _failed(self, ip: str):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())
            self.all_fails.append(time.time())

    def login(self, ip: str, body: dict) -> str:
        self._throttle(ip)
        host = clean_str(body.get("host"), 255, "server", required=True)
        port = str(body.get("port") or "22").strip()
        user = clean_str(body.get("user"), 32, "user", required=True)
        pw = body.get("password")
        if not SSH_HOST_RE.fullmatch(host) or not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ApiError(400, "check the server address and port")
        if not SSH_USER_RE.fullmatch(user):
            raise ApiError(400, "check the user name")
        if not isinstance(pw, str) or not pw or len(pw) > 1024 or "\n" in pw:
            raise ApiError(400, "enter the password")
        if not self.host_allowed(host, port):
            self._failed(ip)
            raise ApiError(403, "this server is not allowed here")
        wsid = hashlib.sha256(f"{user}@{host}:{port}".encode()).hexdigest()[:16]
        d = self.root / wsid
        d.mkdir(mode=0o700, exist_ok=True)
        key = d / "id_ed25519"
        ensure_ssh_key(key)
        try:
            self.verify(ServerConf(user, host, port, str(key)), pw)
        except ApiError as e:
            if e.status in (401, 403):
                self._failed(ip)
            raise
        (d / "workspace.json").write_text(json.dumps({"user": user, "host": host, "port": port}))
        tok = secrets.token_urlsafe(32)
        now = time.time()
        with self.db.tx() as c:
            c.execute("DELETE FROM web_sessions WHERE exp < ? OR last < ?", (now, now - self.IDLE_DAYS * 86400))
            c.execute("INSERT INTO web_sessions (hash, ws, exp, last, ip) VALUES (?, ?, ?, ?, ?)",
                      (self._hash(tok), wsid, now + self.SESSION_DAYS * 86400, now, ip))
        log_line(f"web: {user}@{host}:{port} signed in from {ip}")
        return tok

    def logout(self, tok: str):
        if not tok:
            return
        self.db.run("DELETE FROM web_sessions WHERE hash = ?", (self._hash(tok),))

    def app_for(self, tok: str):
        if not tok or len(tok) > 200:
            return None
        h = self._hash(tok)
        now = time.time()
        r = self.db.rows("SELECT ws, exp, last FROM web_sessions WHERE hash = ?", (h,))
        if not r:
            return None
        ws, exp, last = r[0]
        if exp < now or now - last > self.IDLE_DAYS * 86400:
            self.db.run("DELETE FROM web_sessions WHERE hash = ?", (h,))
            return None
        if now - last > 60:
            self.db.run("UPDATE web_sessions SET last = ? WHERE hash = ?", (now, h))
        with self.lock:
            return self._app(ws)

    def _app(self, wsid: str):
        app = self.apps.get(wsid)
        if app:
            return app
        d = self.root / wsid
        try:
            meta = json.loads((d / "workspace.json").read_text())
        except (OSError, ValueError):
            return None
        app = App(Store(d / "simab.db"), ServerConf(meta["user"], meta["host"], str(meta["port"]), str(d / "id_ed25519")), web=True)
        self.apps[wsid] = app
        if self.start_loops:
            threading.Thread(target=app.git_watch_loop, daemon=True).start()
            threading.Thread(target=app.docker_watch_loop, daemon=True).start()
        return app

    def shutdown(self):
        for app in list(self.apps.values()):
            app._stopping = True
            app.shutdown_sessions()


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    ssl_ctx = None

    def finish_request(self, request, client_address):
        if self.ssl_ctx is not None:            # TLS handshake here, in the request's own thread
            request.settimeout(15)
            try:
                request = self.ssl_ctx.wrap_socket(request, server_side=True)
            except (OSError, ValueError):
                return
            request.settimeout(None)
        super().finish_request(request, client_address)


def make_web_server(gate: WebGate, bind="127.0.0.1", port=0, cert="", key="") -> WebServer:
    handler = type("WebHandler", (Handler,), {"app": None, "gate": gate})
    httpd = WebServer((bind, port), handler)
    if cert:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key or None)
        httpd.ssl_ctx = ctx
    return httpd


def self_signed_cert(d: Path, name: str) -> tuple:
    cert, key = d / "cert.pem", d / "key.pem"
    if not cert.is_file() or not key.is_file():
        san = f"IP:{name}" if re.fullmatch(r"[0-9.]+|[0-9a-fA-F:]+", name) else f"DNS:{name}"
        r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "825",
                            "-subj", f"/CN={name}", "-addext", f"subjectAltName={san}",
                            "-keyout", str(key), "-out", str(cert)], capture_output=True, timeout=60)
        if r.returncode != 0:
            raise SystemExit("openssl failed: " + r.stderr.decode("utf-8", "replace")[-300:])
        os.chmod(key, 0o600)
    return str(cert), str(key)


def serve_web(args: list):
    import argparse
    ap = argparse.ArgumentParser(prog="simabworkspace web", description="The workspace in the browser, for other devices.")
    ap.add_argument("--bind", default="0.0.0.0", help="address to listen on (127.0.0.1 behind a proxy)")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--hosts", default="", help="servers people may sign in to: host[:port],… (empty = any)")
    ap.add_argument("--cert", default="", help="TLS certificate (PEM); without it a self-signed one is made")
    ap.add_argument("--key", default="", help="TLS key (PEM)")
    ap.add_argument("--name", default="", help="address in the self-signed certificate (IP or domain)")
    ap.add_argument("--proxy", action="store_true", help="behind an HTTPS proxy (nginx…): plain HTTP, trust X-Real-IP")
    ap.add_argument("--data", default="", help="folder for workspaces (default ~/.config/simabworkspace/web)")
    ap.add_argument("--cert-download", default="", help="certificate (PEM, no key) people may download to trust this server")
    a = ap.parse_args(args)
    install_crash_logging()
    root = Path(a.data or (config_dir() / "web")).expanduser()
    gate = WebGate(root, [h.strip() for h in a.hosts.split(",")], trust_proxy=a.proxy, secure=True)
    cert, key = a.cert, a.key
    if not a.proxy and not cert:
        cert, key = self_signed_cert(root, a.name or "localhost")
        gate.cert_download = cert
    if a.cert_download:
        gate.cert_download = a.cert_download
    httpd = make_web_server(gate, a.bind, a.port, "" if a.proxy else cert, key)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
    scheme = "http" if a.proxy else "https"
    log_line(f"web {VERSION} on {scheme}://{a.bind}:{a.port} (hosts: {a.hosts or 'any'})")
    print(f"{APP} web: {scheme}://{a.bind}:{a.port}  (servers: {a.hosts or 'any'})", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        gate.shutdown()


# ----------------------------------------------------------------------------- HTTP
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".woff2": "font/woff2", ".svg": "image/svg+xml",
        ".png": "image/png", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8",
        ".webmanifest": "application/manifest+json", ".crt": "application/x-x509-ca-cert"}


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP}/{VERSION}"
    app: App = None   # set on the subclass
    gate = None       # WebGate in the web version (one server, many signed-in users)

    # web version ---------------------------------------------------------------
    def _client_ip(self) -> str:
        if self.gate.trust_proxy:
            ip = (self.headers.get("X-Real-IP") or "").strip()
            if ip:
                return ip[:64]
        return str(self.client_address[0] if self.client_address else "?")

    def _cookie(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == WEB_COOKIE:
                return v.strip()
        return ""

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return False
        return urllib.parse.urlsplit(origin).netloc.lower() == (self.headers.get("Host") or "").lower()

    def _set_cookie(self, value: str, max_age: int) -> dict:
        secure = "; Secure" if self.gate.secure else ""
        return {"Set-Cookie": f"{WEB_COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"}

    def _web_dispatch(self, method: str):
        url = urllib.parse.urlsplit(self.path)
        path, query = url.path, urllib.parse.parse_qs(url.query)
        gate = self.gate
        try:
            if (method != "GET" or path == "/ws") and not self._origin_ok():
                raise ApiError(403, "forbidden")
            if path == "/api/web-info" and method == "GET":
                return self._json(200, gate.info())
            if path == "/simab-server.crt" and method == "GET":
                if not gate.cert_download or not os.path.isfile(gate.cert_download):
                    raise ApiError(404, "not found")
                pem = Path(gate.cert_download).read_bytes()
                if b"PRIVATE KEY" in pem:                       # never hand out a key by mistake
                    raise ApiError(404, "not found")
                return self._send(200, pem, "application/x-x509-ca-cert",
                                  {"Content-Disposition": 'attachment; filename="simab-server.crt"'})
            if path == "/sw.js" and method == "GET":
                return self._send(200, (STATIC / "sw.js").read_bytes(), MIME[".js"], {"Service-Worker-Allowed": "/"})
            if path == "/api/login" and method == "POST":
                if self.headers.get("X-SW") != "1":
                    raise ApiError(403, "forbidden")
                tok = gate.login(self._client_ip(), self._body())
                body = json.dumps({"ok": True}).encode()
                return self._send(200, body, "application/json; charset=utf-8", self._set_cookie(tok, WebGate.SESSION_DAYS * 86400))
            if path == "/api/logout" and method == "POST":
                gate.logout(self._cookie())
                return self._send(200, b'{"ok": true}', "application/json; charset=utf-8", self._set_cookie("", 0))
            if path.startswith("/api/") or path == "/ws":
                app = gate.app_for(self._cookie())
                if app is None:
                    return self._json(401, {"error": "sign in", "login": True})
                self.app = app
                if path == "/ws":
                    return self._websocket(query)
                if self.headers.get("X-SW") != "1":
                    raise ApiError(403, "forbidden")
                return self._json(200, self._api(method, path, query))
            if method == "GET":
                return self._static(path)
            raise ApiError(404, "not found")
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001 - report, keep serving
            log_line(f"web: {e}")
            try:
                self._json(500, {"error": "internal error"})
            except OSError:
                pass

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
        extra = dict(extra or {})
        self.send_header("Cache-Control", extra.pop("Cache-Control", "no-store"))
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
        if getattr(self, "_cached_body", None) is not None:
            return self._cached_body
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
        self._cached_body = body
        return body

    # verbs
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        self._cached_body = None
        if self.gate is not None:
            return self._web_dispatch(method)
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
        if self.gate is not None:
            host = re.sub(r"[^A-Za-z0-9.:\[\]-]", "", self.headers.get("Host") or "")
            connect = f"'self' wss://{host} ws://{host}"
        else:
            connect = "'self' ws://127.0.0.1:* ws://localhost:*"
        extra = {"Content-Security-Policy": f"default-src 'self'; connect-src {connect}; "
                                            "img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'; "
                                            "frame-ancestors 'none'",
                 "X-Frame-Options": "DENY"} if f.suffix == ".html" else None
        if f.suffix in (".svg", ".png", ".woff2", ".ico") or rel.startswith("vendor/"):
            extra = {"Cache-Control": "public, max-age=86400"}     # icons, fonts, libraries: rarely change
        self._send(200, f.read_bytes(), MIME.get(f.suffix, "application/octet-stream"), extra)

    def _api(self, method: str, path: str, query: dict):
        app = self.app
        parts = [urllib.parse.unquote(p) for p in path.split("/")[2:]]
        route = (method, parts[0] if parts else "")
        if app.web and (route in WEB_BLOCKED or (route == ("GET", "sync") and parts[-1:] != ["details"])):
            raise ApiError(403, "not available in the web version")
        if app.web and route == ("POST", "commands"):
            b = self._body()
            cmds = b.get("commands") if isinstance(b.get("commands"), list) else []
            for c in cmds:
                if isinstance(c, dict):
                    c["where"] = "server"
            self._cached_body = {"commands": cmds}
        if app.web and route == ("POST", "manual"):
            b = dict(self._body(), local="")
            self._cached_body = b
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
        if route == ("POST", "starter") and len(parts) == 2:
            app._project(parts[1])
            t = app.store.set_starter(parts[1], self._body().get("text"))
            app.invalidate()
            return {"starter": t}
        if route == ("POST", "title") and len(parts) == 2:
            app._project(parts[1])
            t = app.store.set_title(parts[1], self._body().get("title"))
            app.invalidate()
            return {"title": t}
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
            if act == "win":
                return app.set_window(parts[1], self._body().get("win"))
            raise ApiError(404, "not found")
        if route == ("DELETE", "sessions") and len(parts) == 2:
            app.close_session(parts[1], keep=query.get("keep") == ["1"])
            return {"ok": True}
        if route == ("GET", "claude-running"):
            return {"projects": app.claude_running(force=query.get("force") == ["1"]), "t": time.time()}
        if route == ("POST", "claude-running") and parts[1:] == ["stop"]:
            b = self._body()
            return app.claude_stop(b.get("project") or "", b.get("pid"), b.get("sessionId"))
        if route == ("GET", "tabs"):
            return app.tabs()
        if route == ("POST", "windows") and len(parts) == 2:
            app.window_seen(parts[1])
            return {"ok": True}
        if route == ("DELETE", "windows") and len(parts) == 2:
            app.window_closed(parts[1])
            return {"ok": True}
        if route == ("POST", "order"):
            app.order(self._body().get("ids"))
            return {"ok": True}
        if route == ("POST", "editor"):
            return app.open_editor(self._body())
        if route == ("POST", "open-folder") and len(parts) == 2:
            return app.open_folder(parts[1])
        if route == ("POST", "pick-folder"):
            return app.pick_folder(str(self._body().get("start") or ""))
        if route == ("GET", "fs") and len(parts) == 3:
            p = fs_project(app, parts[1])
            return fs_call(app, p, parts[2], (query.get("path") or [""])[0], (query.get("where") or [None])[0])
        if route == ("POST", "fs") and len(parts) == 3 and parts[2] == "upload":
            p = fs_project(app, parts[1])
            n = int(self.headers.get("Content-Length") or 0)
            if n > 50_000_000:
                raise ApiError(413, "file too large (max 50 MB)")
            data = self.rfile.read(n) if n else b""
            return fs_upload_call(app, p, query, data)
        if route == ("POST", "fs") and len(parts) == 3:
            return fs_change_call(app, fs_project(app, parts[1]), parts[2], self._body())
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
            if app.web:
                wh = "server"
            repo = GitRepo(app, p, rq if isinstance(rq, str) else "", wh if wh in WHERE else None)
            if method == "GET" and act == "status":
                return repo.status()
            if method == "GET" and act == "log":
                return repo.log()
            if method == "GET" and act == "show":
                return {"diff": repo.show((query.get("hash") or [""])[0])}
            if method == "POST" and act == "untrack":
                repo.untrack(body.get("files"))
                app.invalidate()
                return {"status": repo.status()}
            if method == "GET" and act == "branches":
                return repo.branches()
            if method == "POST" and act == "switch":
                out = repo.switch(body.get("branch"), create=body.get("create") is True, track=body.get("track") is True,
                                  stash=body.get("stash") is True)
                app.invalidate()
                st = repo.status()
                app.activity.add("git-switch", p["name"], st.get("branch") or "", repo=repo.repo)
                cache = app.__dict__.get("_git_sum")
                if isinstance(cache, dict):
                    try:
                        cache[p["name"]] = app.git_summary_one(p)
                    except ApiError:
                        pass
                return {"output": out, "status": st}
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
        if route == ("POST", "sync") and len(parts) == 3 and parts[2] == "ignores":
            b = self._body()
            return app.set_ignores(parts[1], b.get("ignores"), b.get("delete"))
        if route == ("POST", "sync") and len(parts) == 4 and parts[2] == "ignores" and parts[3] == "preview":
            return app.ignores_preview(parts[1], self._body().get("ignores"))
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
        if route == ("GET", "server-stats"):
            return app.server_stats(top=query.get("top") == ["1"])
        if route == ("GET", "server-ls"):
            return app.server_ls((query.get("path") or [""])[0], (query.get("project") or [""])[0])
        if route == ("GET", "browse"):
            return app.browse((query.get("path") or [""])[0])
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
        if self.gate is None:
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
                if isinstance(sess, (ChatSession, WatchSession)):
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
    if hasattr(signal, "SIGHUP"):
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
        subprocess.Popen([win_python() if IS_WIN else sys.executable, str(Path(__file__).resolve()), "serve"],
                         stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, cwd=os.path.expanduser("~"), **detached_kwargs())
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.2)
        st = read_state()
        if ping(st):
            return st
    raise SystemExit(f"GUI did not start — see {log}")


WEBBROWSER = "<default browser>"     # open_window: Python's webbrowser module (the system default)


def browser_candidates(url: str, profile: str) -> list:
    """Commands to open `url` as an app window, best first."""
    app_args = [f"--app={url}", f"--user-data-dir={profile}", "--class=simabworkspace",
                "--no-first-run", "--no-default-browser-check"]
    out = []
    if IS_WIN:
        env = os.environ
        for rel in (("Microsoft", "Edge", "Application", "msedge.exe"), ("Google", "Chrome", "Application", "chrome.exe"),
                    ("BraveSoftware", "Brave-Browser", "Application", "brave.exe")):
            for base in (env.get("ProgramFiles(x86)"), env.get("ProgramFiles"), env.get("LOCALAPPDATA")):
                if base and os.path.isfile(os.path.join(base, *rel)):
                    out.append([os.path.join(base, *rel)] + app_args)
                    break
        out.append([WEBBROWSER, url])
        return out
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
        if argv[0] == WEBBROWSER:
            import webbrowser
            if webbrowser.open(argv[1]):
                print(f"  ✓ {APP} GUI opened  ({url.split('#')[0]})")
                return
            continue
        if not shutil.which(argv[0]):
            continue
        try:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             cwd="/mnt/c" if argv[0] == "cmd.exe" and os.path.isdir("/mnt/c") else None, **detached_kwargs())
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
    elif cmd == "web":
        serve_web(argv[2:])
    elif cmd == "stop":
        print("  ✓ GUI stopped" if stop_running(read_state()) else "  GUI is not running")
    elif cmd == "shortcut" and IS_WIN:        # used by install.ps1
        print("  ✓ Added to the " + make_windows_shortcut())
    else:
        print(__doc__ or "usage: server.py [open|serve|url|status|stop]")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv) or 0)
