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
import ipaddress
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
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

IS_WIN = os.name == "nt"          # native Windows (Python for Windows + Git for Windows), not WSL
if IS_WIN:                        # Windows reads files as cp1252 by default: UTF-8 for every python we start
    os.environ["PYTHONUTF8"] = "1"
if not IS_WIN:
    import fcntl
    import termios
SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)
SIGHUP = getattr(signal, "SIGHUP", signal.SIGTERM)

APP = "simabworkspace"
VERSION = "1.66.1"
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
NO_WINDOW = {"creationflags": CREATE_NO_WINDOW} if IS_WIN else {}     # a short command, no console flash


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
    "keepComposer": False,       # chat: never hide the message box when away from the tab
    "copyOnSelect": True,
    "bellNotify": True,
    "confirmClose": True,
    "scrollback": 10000,
    "browser": "",               # command used to open the window (empty = auto)
    "gitWatch": True,            # git badges: check every repository in the background
    "gitAutoFetch": True,        # …and `git fetch` every few minutes so "behind" is current
    "dockerWatch": True,         # check the projects' docker containers; tell me when one crashes
    "shareDevices": True,        # same projects + open tabs on every device (installed app ↔ web)
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

    # skill repos
    def skill_repos(self) -> list:
        with self.lock:
            return list(self.db.get("skillRepos") or [])

    def set_skill_repos(self, repos: list) -> list:
        with self.lock:
            self.db.put("skillRepos", repos)
            return repos

    # per-project run commands
    def commands(self, project: str) -> list:
        return list((self.data["projects"].get(project) or {}).get("commands") or [])

    def command(self, project: str, cid: str):
        for c in self.commands(project):
            if c.get("id") == cid:
                return c
        return None

    def set_commands(self, project: str, commands) -> list:
        out = clean_commands(commands)
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

    def set_chat_pc(self, project: str, conv: str, dev: str):
        """The computer chosen in one conversation (its ⚒ chip) — kept for when it is reopened."""
        with self.lock:
            m = self.data["projects"].setdefault(project or "", {}).setdefault("chatPc", {})
            m.pop(conv, None)
            if dev:
                m[conv] = dev
            while len(m) > 200:
                m.pop(next(iter(m)))
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

    def set_group(self, project: str, group) -> str:
        """The sidebar group the project is listed under ("" = no group)."""
        group = " ".join(clean_str(group, 200, "group").split())
        if len(group) > 40:
            raise ApiError(400, "the group name is too long (40 characters at most)")
        with self.lock:
            self.data["projects"].setdefault(project, {})["group"] = group
            self.save()
        return group

    def rename_group(self, old, new) -> int:
        """Rename a group (new "" = ungroup its projects); returns how many projects moved."""
        old = " ".join(clean_str(old, 200, "group", required=True).split())
        new = " ".join(clean_str(new, 200, "group").split())
        if len(new) > 40:
            raise ApiError(400, "the group name is too long (40 characters at most)")
        n = 0
        with self.lock:
            colors = self.group_colors()
            if old in colors:
                c = colors.pop(old)
                if new:
                    colors[new] = c
                self.db.put("groupColors", colors)
            for v in self.data["projects"].values():
                if isinstance(v, dict) and v.get("group") == old:
                    v["group"] = new
                    n += 1
            self.save()
        return n

    def set_group_color(self, group, color) -> str:
        """Colour of a sidebar group tab ("" = automatic)."""
        group = " ".join(clean_str(group, 200, "group", required=True).split())
        color = clean_str(color, 7, "color").lower()
        if color and not re.fullmatch(r"#[0-9a-f]{6}", color):
            raise ApiError(400, "color must look like #4f8cff")
        with self.lock:
            colors = self.group_colors()
            if color:
                colors[group] = color
            else:
                colors.pop(group, None)
            self.db.put("groupColors", colors)
        return color

    def group_colors(self) -> dict:
        c = self.db.get("groupColors")
        return dict(c) if isinstance(c, dict) else {}

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

    # project display order (list of names; projects not in list come last)
    def project_order(self) -> list:
        return list(self.db.get("projectOrder") or [])

    def set_project_order(self, order: list) -> None:
        with self.lock:
            self.db.put("projectOrder", [str(n) for n in order if n][:2000])

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


_RATE = {}   # session -> [(time, bytes received so far)], for the time left


def sync_progress(session: dict, now=None):
    """Transfer progress of the whole staging step (None when idle).
    Mutagen's receivedSize/expectedSize are the file being copied now; totalReceivedSize is
    everything so far. The total is not reported, so it is the size the sending side scanned
    minus what the receiving side already has (never less than what is already known)."""
    sides = [(k, session.get(k)) for k in ("alpha", "beta", "alphaState", "betaState")]
    sides = [(k, v) for k, v in sides if isinstance(v, dict)]
    pr = to = frm = None
    for k, v in sides:
        sp = v.get("stagingProgress")
        if isinstance(sp, dict) and int(sp.get("expectedFiles") or 0) > 0:
            me = "alpha" if k.startswith("alpha") else "beta"
            other = "beta" if me == "alpha" else "alpha"
            pr = sp
            frm = next((x for kk, x in sides if kk.startswith(other) and "totalFileSize" in x), None)
            to = next((x for kk, x in sides if kk.startswith(me) and "totalFileSize" in x), None)
            break
    if pr is None:
        found = []

        def walk(o):
            if isinstance(o, dict):
                if int(o.get("expectedFiles") or 0) > 0:
                    found.append(o)
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(session)
        if not found:
            return None
        pr = found[0]
    ef, rf = int(pr.get("expectedFiles") or 0), int(pr.get("receivedFiles") or 0)
    cur_size, cur_got = int(pr.get("expectedSize") or 0), int(pr.get("receivedSize") or 0)
    got = int(pr.get("totalReceivedSize") or 0) if "totalReceivedSize" in pr else None
    out = {"files": rf, "totalFiles": ef, "bytes": 0, "totalBytes": 0, "eta": None,
           "percent": max(0, min(100, int(rf * 100 / ef)))}
    if got is None:
        return out
    total = 0
    if frm is not None:
        total = int(frm.get("totalFileSize") or 0) - int((to or {}).get("totalFileSize") or 0)
    total = max(total, got + max(0, cur_size - cur_got))
    out["bytes"], out["totalBytes"] = got, total
    if total:
        out["percent"] = max(0, min(99 if rf < ef else 100, int(got * 100 / total)))
    # time left from the speed over the last ~minute
    now = time.time() if now is None else now
    key = session.get("identifier") or session.get("name") or ""
    hist = [x for x in _RATE.get(key, []) if now - x[0] <= 60 and x[1] <= got]
    if not hist or hist[-1][0] < now - 1:
        hist.append((now, got))
    _RATE[key] = hist[-30:]
    t0, b0 = hist[0]
    if now - t0 >= 5 and got > b0 and total > got:
        out["eta"] = int((total - got) / ((got - b0) / (now - t0)))
    return out


def _endpoint(e):
    e = e or {}
    return {"path": e.get("path") or "", "host": e.get("host") or "",
            "user": e.get("user") or "", "port": str(e.get("port") or "")}


def clean_commands(commands) -> list:
    """A checked list of run commands (from the editor or a project's .simab.json)."""
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
    return out


# Shared run commands live in the project itself: .simab.json in its root (on the server, and in
# the synced local copy) — every device and every person on the project sees the same list.
SHARED_FILE = ".simab.json"
SHARED_PY = r'''
import json, os, sys
op, root = sys.argv[1], os.path.expanduser(sys.argv[2])
f = os.path.join(root, ".simab.json")
try:
    with open(f, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        data = {}
except (OSError, ValueError):
    data = {}
if op == "write":
    if not os.path.isdir(root):
        print(json.dumps({"error": "project folder not found"})); sys.exit()
    new = json.loads(sys.stdin.read() or "[]")
    if new:
        data["commands"] = new
    else:
        data.pop("commands", None)
    if data:
        tmp = f + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, f)
    elif os.path.exists(f):
        os.remove(f)
    print(json.dumps({"ok": True}))
else:
    print(json.dumps({"commands": data.get("commands") if isinstance(data.get("commands"), list) else []}))
'''


def shared_id(c: dict) -> str:
    return "s" + hashlib.sha1((c.get("name", "") + "\0" + c.get("cmd", "")).encode()).hexdigest()[:9]


def shared_clean(raw) -> list:
    """Commands from a .simab.json (written by hand maybe): bad entries are skipped, ids come from the content."""
    out = []
    for c in (raw if isinstance(raw, list) else [])[:50]:
        try:
            x = clean_commands([dict(c, id="tmp0")])[0] if isinstance(c, dict) else None
        except ApiError:
            x = None
        if x:
            x["id"], x["shared"] = shared_id(x), True
            out.append(x)
    return out


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
        p["group"] = (store_projects.get(p["name"]) or {}).get("group") or ""
        lo = (store_projects.get(p["name"]) or {}).get("localOnly") or ""
        if lo and p.get("source") == "manual":
            p["localOnly"] = lo                 # id of the computer it lives on (files never on the server)
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


# A path with a space after IdentityFile / CertificateFile ("/Users/simab co/.ssh/key"), unquoted: ssh says
# "extra arguments at end of line … terminating" and refuses the WHOLE file — every ssh stops. Such a line
# is never valid, so quoting it can only fix it.
SSH_PATH_LINE = re.compile(r"(?mi)^([ \t]*(?:IdentityFile|CertificateFile)(?:[ \t]*=[ \t]*|[ \t]+))"
                           r"([^\"\n][^\n]*?[ \t][^\n]*?)[ \t]*$")


def quote_ssh_paths(text: str) -> str:
    return SSH_PATH_LINE.sub(lambda m: m.group(1) + '"' + m.group(2) + '"', text)


def win_ssh_config() -> list:
    """The -F file every ssh of the app gets (all three call sites), or [] = the user's own config.
    - Windows: ssh refuses ~/.ssh/config when its owner/ACL is "wrong" ("Bad owner or permissions on
      C:\\Users\\…/.ssh/config"); a file given with -F is not checked → always a copy.
    - any OS: a copy with unquoted space-paths quoted (quote_ssh_paths) when the user's file has one
      (the macOS user "simab co"), so our ssh works even before / without healing the file itself.
    The copy keeps Host aliases, Include, proxies. No config → an empty one (Windows)."""
    try:
        src = Path(home_path(".ssh", "config"))
        data = src.read_bytes() if src.is_file() else b""
        text = data.decode("utf-8", "surrogateescape")
        fixed = quote_ssh_paths(text).encode("utf-8", "surrogateescape")
        if not IS_WIN and fixed == data:
            return []
        dst = config_dir() / "ssh_config"
        if not dst.is_file() or dst.read_bytes() != fixed:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(fixed)
            os.chmod(dst, 0o600)
        return ["-F", str(dst)]
    except OSError:
        return []


def ssh_base(conf: ServerConf, multiplex=True, batch=False, forwards=False) -> list:
    """forwards=False (background work: chat, git, lists): port forwardings from the user's
    ~/.ssh/config (RemoteForward…) are not opened — they only fail ("remote port forwarding
    failed for listen port …") and end up in the chat. Terminals and tunnels keep them."""
    a = ["ssh", *win_ssh_config(), "-p", str(conf.port),
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
KINDS = ("shell", "pcterm", "claude", "run", "sync-info", "sync-monitor", "git-sync-on", "add-local", "add-server", "docker")
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
        cmd = spec.get("_cmd") if isinstance(spec.get("_cmd"), dict) else store.command(pname, spec.get("cmdId") or "")
        if not cmd:
            raise ApiError(404, "this command no longer exists")
        where = cmd["where"]
        if project.get("buildsOn") in ("computers", "local") and project.get("localExists"):
            where = "local"           # the project is built / run on this computer, not on the server
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
    if kind == "pcterm":                    # a terminal of the user's computer (Computers), through the server
        c = conf.with_endpoint(None, None, None)
        script = 'exec python3 "$HOME/.local/bin/onpc" --term ' + shlex.quote(pname)
        if spec.get("_ticket"):                         # one use, a minute: onpc hands it to the computer at once
            script += " --ticket " + shlex.quote(spec["_ticket"])
        return {"argv": ssh_base(c, multiplex=bool(s.get("sshMultiplex"))) + ["-tt", c.target, posix_sh(script)],
                "cwd": os.path.expanduser("~"), "title": (pname or "home") + " · your computer", "env": {},
                "spec": {"kind": kind, "project": pname, "where": "server", "cmdId": "", "claudeMode": "new", "slot": 0}, "url": ""}

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
# SUGGEST_PY is one script: exec'd here for the local copy, sent as `python3 -c` for the server
# (the web version, or a project without a folder on this machine). argv: FOLDER → JSON list.
SUGGEST_PY = r'''
import json, os, sys
from pathlib import Path
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


if __name__ == "__main__":
    print(json.dumps(suggestions(os.path.expanduser(sys.argv[1]))))
'''
_SUGGEST = {}


def suggestions(local: str) -> list:
    if not _SUGGEST:
        _SUGGEST["__name__"] = "simab_suggest"
        exec(compile(SUGGEST_PY, "<suggest>", "exec"), _SUGGEST)
    return _SUGGEST["suggestions"](local)


def project_suggestions(app, p: dict) -> list:
    """Run-command ideas read from the local copy, else from the server folder (then every
    suggestion runs on the server — in the web version nothing else can run)."""
    if not p:
        return []
    if p.get("localExists") and not app.web:
        return suggestions(p["local"])
    if not (p.get("remote") and app.conf.configured):
        return []
    res = remote_python(app._project_conf(p), SUGGEST_PY, [p["remote"]],
                        multiplex=bool(app.store.settings.get("sshMultiplex")), timeout=30)
    out = [x for x in res if isinstance(x, dict)] if isinstance(res, list) else []
    for x in out:
        x["where"] = "server"
    return out


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
    """Folder name Claude uses under ~/.claude/projects for a working directory. A path with ~
    (e.g. a Local only project: ~/.simab-mnt/x) is passed as it is: Claude names the folder after the
    FULL path (-home-esi--simab-mnt-x), which only the server knows — the remote scripts turn it into
    the name (PROJ_DIR_PY)."""
    if (remote_path or "").startswith("~"):
        return remote_path
    return re.sub(r"[^A-Za-z0-9]", "-", remote_path)


# start of the remote scripts that get a project folder name (or a ~ path) in argv[1]
PROJ_DIR_PY = r'''
import re as _re
if sys.argv[1].startswith("~"):
    sys.argv[1] = _re.sub(r"[^A-Za-z0-9]", "-", os.path.expanduser(sys.argv[1]))
'''


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


PC_EDIT_TOOLS = {"mcp__pc__Edit", "mcp__pc__Write"}


def pc_edit_verdict(tool, mode):
    """Local only: Edit / Write run on the computer as MCP tools, which Claude's modes do not cover —
    accept edits / auto / bypass → allowed, plan → denied, else None (the user is asked, like Claude's Edit)."""
    if tool not in PC_EDIT_TOOLS:
        return None
    return True if mode in ("acceptEdits", "auto", "bypassPermissions") else False if mode == "plan" else None


def chat_remote_script(rpath: str, claude_cmd: str, args: list, chat: str = "", pc: str = "") -> str:
    """chat: the relay key of the chat tab — onpc sends it, so a question about a command goes to
    exactly that chat (not to whatever tab is open)."""
    cmd = claude_cmd or "claude"
    # a project built on the user's own computers: Claude is told to use onpc (see PC_HINT)
    inner = (CLAUDE_FIND + (CLAUDE_MISSING if cmd == "claude" else "")
             + (f"SIMAB_CHAT={chat}; export SIMAB_CHAT; " if RELAY_KEY_RE.fullmatch(chat or "") else "")
             + (f"SIMAB_PC={pc}; export SIMAB_PC; " if PC_ID_RE.fullmatch(pc or "") else "")
             + 'H=$("$HOME/.local/bin/onpc" --hint 2>/dev/null); L=$("$HOME/.local/bin/onpc" --local 2>/dev/null); '
             + 'if [ $? = 3 ]; then echo "simabworkspace: this project lives on your computer and it is not connected'
             + ' — open Simab Workspace there (Computers → Connect this computer)" >&2; exit 98; fi; '
             + 'M=$("$HOME/.local/bin/onpc" --mcp 2>/dev/null); exec ' + cmd + " "
             + " ".join(shlex.quote(x) for x in args) + ' ${H:+--append-system-prompt "$H"} ${L:+--settings "$L"}'
             + ' ${M:+--mcp-config "$M" --disallowedTools Grep,Glob,Read,Edit,Write,MultiEdit}')
    # the login shell gives the user's environment; our part runs with sh (zsh/fish differ)
    # a Local only project (~/.simab-mnt/x): no folder = the link to the computer, not the server (96)
    gone = ("{ echo 'simabworkspace: this project lives on your computer and its folder is not reachable from the"
            " server — the link dropped or the computer is off / asleep / not connected' >&2; exit 96; }"
            if "/.simab-mnt/" in rpath else "exit 97")
    return (f"cd {remote_path_expr(rpath)} || {gone}; "
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
HB = 5
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
beat = sent = 0
while True:
    b = f.read(65536)
    if b:
        sys.stdout.buffer.write(b)
        sys.stdout.buffer.flush()
        sent = time.time()
        continue
    if time.time() - sent > HB:          # a sign of life: the app drops a link that stays silent
        say({"type": "_sw_relay", "state": "hb"})
        sent = time.time()
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
''' + PROJ_DIR_PY + r'''
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
SKILL_LIST_PY = r'''import json,os,sys
p=sys.argv[1]
d=os.path.join(p,'.claude','skills')
print(json.dumps(sorted(f for f in os.listdir(d) if f.endswith('.md')) if os.path.isdir(d) else []))
'''

SKILL_WRITE_PY = r'''import json,os,sys
p,name=sys.argv[1],sys.argv[2]
d=os.path.join(p,'.claude','skills')
os.makedirs(d,exist_ok=True)
open(os.path.join(d,name),'w').write(sys.stdin.read())
print(json.dumps({'ok':True}))
'''

SKILL_DELETE_PY = r'''import json,os,sys
p,name=sys.argv[1],sys.argv[2]
path=os.path.join(p,'.claude','skills',name)
if os.path.isfile(path): os.remove(path)
print(json.dumps({'ok':True}))
'''

# {"total": all messages, "start": index of the first one returned, "entries": [...]}
REMOTE_HISTORY_PY = r'''
import sys, os, json
''' + PROJ_DIR_PY + r'''
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
''' + PROJ_DIR_PY + r'''
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


# The commands one chat ran on my computers (RunNote of onpc): the last 5 of the last 10 minutes, each
# with the end of its output (64 KB); older ones removed after a day.
PC_RUNS_PY = r'''
import os, sys, json, time
d = os.path.expanduser("~/.cache/simabworkspace/pc/runs/" + sys.argv[1])
runs, now = [], time.time()
for f in sorted(os.listdir(d) if os.path.isdir(d) else []):
    p = os.path.join(d, f)
    if f.endswith((".json", ".log")) and now - os.path.getmtime(p) > 86400:
        try:
            os.remove(p)
        except OSError:
            pass
        continue
    if not f.endswith(".json"):
        continue
    try:
        m = json.load(open(p))
    except (OSError, ValueError):
        continue
    if now - os.path.getmtime(p) > 600 and m.get("state") == "done":
        continue
    try:
        with open(p[:-5] + ".log", "rb") as lf:
            lf.seek(0, 2)
            lf.seek(max(0, lf.tell() - 65536))
            m["out"] = lf.read().decode("utf-8", "replace")
    except OSError:
        m["out"] = ""
    runs.append(m)
print(json.dumps({"runs": runs[-5:]}))
'''


# Each Local only chat's line (Mux.note of onpc): {"chats": {key: {n, rtt}}}; a minute old → no rtt,
# 10 minutes → gone (and removed).
PC_LINKS_PY = r'''
import os, json, time
d = os.path.expanduser("~/.cache/simabworkspace/pc/links")
out, now = {}, time.time()
for f in (os.listdir(d) if os.path.isdir(d) else []):
    if not f.endswith(".json"):
        continue
    p = os.path.join(d, f)
    try:
        j = json.load(open(p))
    except (OSError, ValueError):
        continue
    age = now - (j.get("at") or 0)
    if age > 600:
        try:
            os.remove(p)
        except OSError:
            pass
        continue
    out[f[:-5]] = {"n": j.get("n") or 0, "rtt": j.get("rtt") if age < 60 else None}
# a command running on the computer for a chat is a line too (RunNote, less than an hour old)
rd = os.path.expanduser("~/.cache/simabworkspace/pc/runs")
for key in (os.listdir(rd) if os.path.isdir(rd) else []):
    for f in os.listdir(os.path.join(rd, key)):
        p = os.path.join(rd, key, f)
        try:
            if f.endswith(".json") and now - os.path.getmtime(p) < 3600 and json.load(open(p)).get("state") == "running":
                out.setdefault(key, {"n": 0, "rtt": None})["n"] += 1
        except (OSError, ValueError):
            pass
print(json.dumps({"chats": out}))
'''


# The @ list of a Local only project opened on another of my computers: onpc on the server asks the
# computer it lives on (cd into its mount so onpc knows the project). → {"files"} or {"error"}
PC_FILES_PY = r'''
import os, sys, json, subprocess
try:
    os.chdir(os.path.expanduser(sys.argv[1]))
    r = subprocess.run([sys.executable, os.path.expanduser("~/.local/bin/onpc"), "--fs",
                        sys.argv[2] if len(sys.argv) > 2 else "files", "{}"],
                       capture_output=True, timeout=80)
    out = r.stdout.decode("utf-8", "replace").strip()
    print(out if out.startswith("{") else json.dumps({"error": (r.stderr.decode("utf-8", "replace").strip()
                                                                or "onpc gave no answer")[-300:]}))
except (OSError, subprocess.SubprocessError) as e:
    print(json.dumps({"error": "the computer of this project cannot be reached: " + str(e)[:200]}))
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
    except subprocess.TimeoutExpired:                # str(e) is the whole command (FS_PY): never show it
        raise ApiError(504, f"cannot reach the server: no answer in {timeout}s")
    except (OSError, subprocess.SubprocessError) as e:
        raise ApiError(502, "cannot reach the server: " + (e.strerror if isinstance(e, OSError) and e.strerror else type(e).__name__))
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
    files = [f for f in files if not f.endswith("/")]       # folders come from the paths, once
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
        self.on_notify = None         # callback(session, "done" | "ask", detail) — phone notifications
        self._linked_at = 0.0         # when the relay last (re)attached: what follows at once is replayed
        self._cost_seen = 0.0
        # the relay on the server (Claude survives a dropped connection); "" = run Claude directly
        self.relay_key = built.get("relay") or ""
        if self.relay_key:
            self.spec["relay"] = self.relay_key
        self.relay_off = -1 if built.get("relay_restored") else 0    # bytes of Claude's output read so far
        self._hb, self._rx_at = False, time.time()       # relay heartbeat seen / last byte from it
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
        if how == "start" and self.built.get("prepare"):
            why = self.built["prepare"](self.exit_code in (96, 97))     # Local only: mount its folder first
            if why:
                self._log({"type": "_error", "text": why})
        if self.relay_key:
            argv = self.built["make_argv"](self.mode, self.model, self.session_id, self.effort, **self._pc_arg(),
                                           relay=(how, self.relay_key, self.relay_off, self.session_id or ""))
        else:
            argv = self.built["make_argv"](self.mode, self.model, self.session_id, self.effort, **self._pc_arg())
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
            self._hb, self._rx_at = False, time.time()
            self.linking = bool(self.relay_key)
            if not again:
                self.busy = False
                self.started = time.time()
                self.runs += 1
                self.pending.clear()
                self._cost_seen = 0.0
        threading.Thread(target=self._read_out, args=(proc,), daemon=True).start()
        threading.Thread(target=self._read_err, args=(proc,), daemon=True).start()
        threading.Thread(target=self._watch_link, args=(proc,), daemon=True).start()
        self._request({"subtype": "initialize", "hooks": None})

    LINK_DEAD = 20          # s without a byte (the relay beats every 5 s) → the link is dead

    def _watch_link(self, proc):
        """A bad internet can freeze the SSH link without closing it (ssh notices after a minute or
        more). The relay beats every few seconds; when nothing arrives the link is dropped, and the
        usual reconnect attaches again to the Claude still working on the server."""
        while proc.poll() is None and self.proc is proc and not self.closed:
            time.sleep(2)
            if self._hb and self.relay_on and time.time() - self._rx_at > self.LINK_DEAD:
                try:
                    proc.kill()
                except OSError:
                    pass
                return

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
        if st == "hb":                            # the relay sends these: a silent link is a dead one
            self._hb = True
            return
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
        self._linked_at = time.time()
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
            self._rx_at = time.time()
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
            # a failed try (the line is still down) does not end the chat: it is tried again
            if (self.relay_on or self.reconnecting) and self.relay_exit is None and not self.closed:
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

    def _notify(self, kind: str, detail: str):
        if self.on_notify and time.time() - self._linked_at > 3:
            try:
                self.on_notify(self, kind, detail)
            except Exception as e:  # noqa: BLE001 - a notification must never break the chat
                log_line(f"notify: {e}")

    def _handle_event(self, ev: dict):
        t = ev.get("type")
        sid = ev.get("session_id")
        if isinstance(sid, str) and UUID_RE.fullmatch(sid) and sid != self.session_id:
            self.session_id = sid
            self.spec["resume"] = sid
            if self.relay_on:
                self._write({"type": "_sw_relay_conv", "id": sid})
            if self.spec.get("pc") and getattr(self, "remember_pc", None):    # chosen before the first message
                self.remember_pc(self.spec.get("project"), sid, self.spec["pc"])
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
                    fresh = ev.get("request_id") not in self.pending
                    self.pending[ev.get("request_id")] = ev
                auto = pc_edit_verdict(req.get("tool_name"), self.mode)
                if auto is not None:            # Local only Edit / Write on the computer: as Claude's own would be
                    self.answer_permission(ev.get("request_id"), auto, False,
                                           "" if auto else "Plan mode: do not change files yet — finish the plan first.")
                elif fresh:
                    self._notify("ask", str(req.get("tool_name") or ""))
            else:
                # not supported here: answer so Claude does not wait forever
                self._write({"type": "control_response", "response": {
                    "subtype": "error", "request_id": ev.get("request_id"), "error": "not supported by this client"}})
                return
        elif t == "control_cancel_request":
            with self.lock:
                self.pending.pop(ev.get("request_id"), None)
        elif t == "result":
            if self.busy:
                self._notify("done", "")
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
        elif t == "pc" and isinstance(m.get("pc"), str) and (m["pc"] == "" or PC_ID_RE.fullmatch(m["pc"])):
            self.set_pc(m["pc"])

    def _pc_arg(self) -> dict:
        return {"pc": self.spec["pc"]} if self.spec.get("pc") else {}

    def set_pc(self, dev: str):
        """The computer of this chat ("" = the project's / default choice): onpc reads it at once
        (chatpins.json by relay key), a restarted Claude gets it as SIMAB_PC, a reopened chat from the store."""
        if dev:
            self.spec["pc"] = dev
        else:
            self.spec.pop("pc", None)
        if self.session_id and getattr(self, "remember_pc", None):
            self.remember_pc(self.spec.get("project"), self.session_id, dev)
        key, conf = self.relay_key, self.built.get("conf")

        def tell():
            try:
                remote_python(conf, PC_ADMIN_PY, ["chatpin", key, dev])
            except ApiError as e:
                log_line(f"chatpin: {e.message}")
        if key and conf:
            threading.Thread(target=tell, daemon=True).start()
        self._log({"type": "_pc", "pc": dev})
        if self.on_change:
            self.on_change(self)

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
    pc = spec.get("pc") if isinstance(spec.get("pc"), str) and PC_ID_RE.fullmatch(spec.get("pc")) else \
        ((pp.get("chatPc") or {}).get(resume) or "" if resume else "")

    def make_argv(mode_, model_, resume_, effort_="", relay=None, pc=""):
        """relay: (start|attach|kill, key, offset) — Claude kept running on the server (see CHAT_RELAY_PY).
        pc: the computer chosen in this chat (onpc runs its commands there)."""
        script = chat_remote_script(rpath, claude_cmd, chat_args(mode_, model_, resume_, effort_), relay[1] if relay else "", pc)
        if relay:
            script = chat_relay_command(relay[0], relay[1], relay[2], script, relay[3] if len(relay) > 3 else "")
        ext = ["-o", "ConnectTimeout=15"] if relay else []      # a dead line fails fast and is tried again
        return ssh_base(c, multiplex=multiplex and not relay) + ext + ["-T", c.target, script]

    relay = spec.get("relay") if isinstance(spec.get("relay"), str) and RELAY_KEY_RE.fullmatch(spec.get("relay")) else ""

    title = spec.get("title") if isinstance(spec.get("title"), str) and spec.get("title").strip() else f"Claude · {pname or 'home'}"
    return {"make_argv": make_argv, "cwd": os.path.expanduser("~"), "title": title.strip()[:80],
            "resume": resume, "mode": mode, "model": model, "effort": effort, "conf": c, "rpath": rpath,
            "relay": relay or secrets.token_hex(8), "relay_restored": bool(relay),
            "spec": {"kind": "chat", "project": pname, "where": "server", "cmdId": "", "claudeMode": "new",
                     "slot": 0, **({"resume": resume} if resume else {}),
                     **({"named": spec["named"]} if spec.get("named") in CHAT_NAMED else {}),
                     **({"pc": pc} if pc else {})}}



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


# Every folder with a .git, then only the real repositories: git must say the folder is its own top
# (an empty or broken .git would make git use a repository further up). "nohead:" = no commits yet.
REMOTE_FIND_REPOS = ("find . -maxdepth 4 \\( -name node_modules -o -name vendor -o -name dist -o -name docker \\) -prune "
                     "-o -name .git -print 2>/dev/null | sed -e 's|/\\.git$||' -e 's|^\\.$||' -e 's|^\\./||' | sort | "
                     "while IFS= read -r d; do t=$(git -C \"./$d\" rev-parse --show-toplevel 2>/dev/null) || continue; "
                     "[ \"$(cd \"./$d\" && pwd -P)\" = \"$(cd \"$t\" && pwd -P)\" ] || continue; "
                     "git -C \"./$d\" rev-parse -q --verify HEAD >/dev/null 2>&1 || printf nohead:; printf '%s\\n' \"$d\"; done")


def real_repos(found: list) -> list:
    """found = (path, has commits). The project folder itself is left out when it has no commits
    but holds the real repositories: a stray `git init` there would list them all as new files."""
    paths = [p for p, _ in found]
    return [p for p, head in found if not (p == "" and not head and len(paths) > 1)]


def local_repo_ok(root: str, rel: str):
    """None = not a repository of its own; else True/False = has commits."""
    git = which("git")
    d = os.path.join(root, rel) if rel else root
    if not git:
        return True
    env = dict(os.environ, PATH=tool_path(), **GIT_ENV)
    try:
        r = subprocess.run([git, "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=15, env=env)
        if r.returncode != 0 or os.path.realpath(r.stdout.strip()) != os.path.realpath(d):
            return None
        h = subprocess.run([git, "-C", d, "rev-parse", "-q", "--verify", "HEAD"], capture_output=True, timeout=15, env=env)
        return h.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True


def git_repos(app, project: dict) -> dict:
    """Where git runs for this project and which repositories it has. Local copy first
    (your own git setup); the server when the local copy has no repositories."""
    local = find_local_repos(project["local"]) if project.get("localExists") else []
    if local:
        checked = [(r, local_repo_ok(project["local"], r)) for r in local]
        local = real_repos([(r, ok) for r, ok in checked if ok is not None])
    if local:
        return {"where": "local", "repos": local}
    if project.get("remote") and project.get("localOnly") and app.conf.configured:     # found on its computer
        try:
            r = remote_python(app.conf, PC_FILES_PY, [project["remote"], "repos"], timeout=60) or {}
        except ApiError:
            r = {}
        return {"where": "server", "repos": [x for x in r.get("repos") or [] if isinstance(x, str)]}
    if project.get("remote") and app.conf.configured:
        conf = app.conf
        ep = project.get("endpoint") or {}
        conf = conf.with_endpoint(ep.get("user"), ep.get("host"), ep.get("port"))
        cmd = f"cd {remote_path_expr(project['remote'])} && " + REMOTE_FIND_REPOS
        try:
            r = subprocess.run(ssh_base(conf, multiplex=bool(app.store.settings.get("sshMultiplex"))) + ["-T", "-o", "BatchMode=yes", conf.target, posix_sh(cmd)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
            found = []
            for x in r.stdout.decode("utf-8", "replace").splitlines():
                x = x.strip()
                head = not x.startswith("nohead:")
                x = x[len("nohead:"):] if not head else x
                if (x == "" or git_path_ok(x)) and x not in [f[0] for f in found]:
                    found.append((x, head))
            # the root shows up as "" once; keep order stable
            remote = sorted(real_repos(found), key=lambda x: (x != "", x))
            if r.returncode == 0 and (remote or r.stdout.strip()):
                return {"where": "server", "repos": remote}
        except (OSError, subprocess.SubprocessError):
            pass
    return {"where": "local" if project.get("localExists") else "server", "repos": []}


def git_fetched(app) -> dict:
    """When each repository last asked its remote for news (fetch/pull, ours or the watcher's) —
    the window shows it, so "up to date" never looks surer than it is."""
    return app.__dict__.setdefault("_git_fetched", {})


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

    def run(self, args: list, timeout=60, stdin: bytes = None, prog: str = "git"):
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
            cmd = f"cd {remote_path_expr(rpath)} && {envs} {prog} " + " ".join(shlex.quote(a) for a in args)
            if self.p.get("localOnly"):     # lives on one of my computers: git runs there (the mount's .git is empty)
                cmd = (f"cd {remote_path_expr(self.p['remote'])} && \"$HOME/.local/bin/onpc\" --git {shlex.quote(prog)} "
                       f"{shlex.quote(json.dumps(args))} {shlex.quote(self.repo)}")
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
        st["fetched"] = git_fetched(self.app).get(self.key())
        st["merging"] = self.merging() if st["conflicts"] or st["files"] else False
        return st

    def key(self) -> str:
        return f"{self.p.get('name')}\0{self.repo}\0{self.where}"

    def mark_fetched(self):
        git_fetched(self.app)[self.key()] = time.time()

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

    def commit(self, message: str, files, patches: dict = None) -> str:
        message = clean_str(message, 5000, "commit message", required=True)
        self.flush()
        if patches:
            # Stage whole files first
            whole = [f for f in (files or []) if f not in patches]
            if whole:
                if not all(isinstance(f, str) and git_path_ok(f) for f in whole):
                    raise ApiError(400, "invalid file path")
                self.check(["add", "-A", "--"] + whole)
            # Stage selected hunks per file
            for f_path, patch_str in patches.items():
                self.apply_cached(patch_str)
            # Commit whatever is now staged
            out = self.check(["commit", "-m", message])
        elif files is None:
            self.check(["add", "-A"])
            out = self.check(["commit", "-m", message])
        else:
            if not isinstance(files, list) or not files or not all(isinstance(f, str) and git_path_ok(f) for f in files):
                raise ApiError(400, "pick at least one file")
            self.check(["add", "-A", "--"] + files)
            # a merge can only be committed whole (git refuses "partial commit during a merge")
            out = self.check(["commit", "-m", message] + ([] if self.merging() else ["--"] + files))
        self.flush()
        return out

    def apply_cached(self, patch: str) -> str:
        """Stage a subset of changes given as a unified diff (for per-hunk staging)."""
        if not isinstance(patch, str) or not patch.strip():
            raise ApiError(400, "empty patch")
        code, out, err = self.run(["apply", "--cached", "--whitespace=fix", "--recount"],
                                   stdin=patch.encode("utf-8", "replace"), timeout=30)
        if code != 0:
            raise ApiError(400, (err.strip() or out.strip())[-500:] or "git apply --cached failed")
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
        if code == 0 or "CONFLICT" in out + err:
            self.mark_fetched()
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
        out = self.check(["fetch", "--prune"], timeout=120)
        self.mark_fetched()
        return out

    def untrack(self, files) -> str:
        """Stop tracking files (they stay on disk) — for files .gitignore names."""
        if not isinstance(files, list) or not files or not all(isinstance(f, str) and git_path_ok(f) for f in files):
            raise ApiError(400, "pick at least one file")
        self.flush()
        out = self.check(["rm", "-r", "--cached", "--quiet", "--"] + files)
        self.flush()
        return out

    def discard(self, files) -> str:
        """Throw away the changes to these files: back to the last commit (new files are deleted)."""
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ApiError(400, "pick at least one file")
        st = {f["path"]: f for f in self.status().get("files", [])}
        new, back, added = [], [], []
        for p in files:
            if not isinstance(p, str) or not git_path_ok(p.rstrip("/")) or p not in st:
                raise ApiError(400, "not a changed file: " + str(p)[:200])
            f = st[p]
            if f["conflict"]:
                raise ApiError(400, p + " has a merge conflict — resolve it first")
            (new if f["untracked"] else added if f["x"] == "A" else back).append(p)
        self.flush()
        out = ""
        if back:
            out += self.check(["checkout", "HEAD", "--"] + back)
        if added:
            out += self.check(["rm", "-f", "-q", "--"] + added)
        if new:
            out += self.check(["clean", "-f", "-q", "--"] + new)
        self.flush()
        return out

    # merge conflicts ----------------------------------------------------------
    def merging(self) -> bool:
        return self.run(["rev-parse", "-q", "--verify", "MERGE_HEAD"], timeout=15)[0] == 0

    def conflict(self, path: str) -> dict:
        """The conflicted file as it is now (with <<<<<<< markers), for resolving in the dialog."""
        if not git_path_ok(path):
            raise ApiError(400, "bad path")
        f = next((x for x in self.status().get("files", []) if x["path"] == path), None)
        if not f or not f["conflict"]:
            raise ApiError(400, path + " has no merge conflict")
        if self.where == "local":
            full = os.path.join(self.p["local"], self.repo, path)
            try:
                with open(full, "rb") as fh:
                    raw = fh.read(2_000_001)
                code = 0
            except OSError:
                raw, code = b"", 1
        else:
            code, text, _ = self.run(["--", path], timeout=30, prog="cat")
            raw = text.encode("utf-8")
        binary = b"\0" in raw[:8000]
        text = raw.decode("utf-8", "replace")
        return {"path": path, "code": f["x"] + f["y"], "exists": code == 0, "binary": binary,
                "big": len(raw) > 2_000_000, "text": "" if binary or len(raw) > 2_000_000 else text,
                "merging": self.merging()}

    def resolve(self, path: str, text=None, side: str = None) -> str:
        """Mark a conflict resolved: with the given final text, or by taking one whole side
        ("ours" = current / HEAD, "theirs" = incoming)."""
        if not git_path_ok(path):
            raise ApiError(400, "bad path")
        f = next((x for x in self.status().get("files", []) if x["path"] == path), None)
        if not f or not f["conflict"]:
            raise ApiError(400, path + " has no merge conflict")
        self.flush()
        if side in ("ours", "theirs"):
            stage = "2" if side == "ours" else "3"
            code, ls, _ = self.run(["ls-files", "-u", "--", path], timeout=15)
            if not any(l.split()[2:3] == [stage] for l in ls.splitlines() if len(l.split()) > 2):
                out = self.check(["rm", "-q", "-f", "--", path])      # that side deleted the file
            else:
                self.check(["checkout", "--" + side, "--", path])
                out = self.check(["add", "--", path])
        elif isinstance(text, str):
            if len(text) > 5_000_000:
                raise ApiError(400, "too big to save from here")
            if re.search(r"^(<{7}|>{7})( |$)", text, re.M):
                raise ApiError(400, "conflict markers are still in the file")
            code, ls, _ = self.run(["ls-files", "-u", "--", path], timeout=15)
            modes = {l.split()[2]: l.split()[0] for l in ls.splitlines() if len(l.split()) > 2}
            mode = modes.get("2") or modes.get("3") or "100644"
            if mode not in ("100644", "100755"):
                raise ApiError(400, "not a normal file — take one side instead")
            code, sha, err = self.run(["hash-object", "-w", "--stdin", "--path=" + path], timeout=30, stdin=text.encode("utf-8"))
            sha = sha.strip()
            if code != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
                raise ApiError(400, (err.strip() or "git hash-object failed")[-500:])
            # a stage-0 entry replaces the conflict stages; then write the file from it
            code, out, err = self.run(["update-index", "--index-info"], timeout=30,
                                      stdin=f"{mode} {sha} 0\t{path}\n".encode("utf-8"))
            if code != 0:
                raise ApiError(400, (err.strip() or "git update-index failed")[-500:])
            out = self.check(["checkout-index", "-f", "--", path])
        else:
            raise ApiError(400, "nothing to resolve with")
        self.flush()
        return out

    def merge_abort(self) -> str:
        self.flush()
        if self.merging():
            out = self.check(["merge", "--abort"])
        elif self.run(["rev-parse", "-q", "--verify", "REBASE_HEAD"], timeout=15)[0] == 0:
            out = self.check(["rebase", "--abort"])
        else:
            raise ApiError(400, "no merge in progress")
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


def fs_git_tree(folder, prefix, limit, depth=0):
    """git's list, with repositories inside the repository opened too: git names a nested
    repository only as "name/" (new) or "name" (tracked as a link) — never its files."""
    got = fs_git_files(folder, prefix)
    if got is None:
        return None
    out = []
    for x in got:
        rel = x[len(prefix):].rstrip("/")
        full = os.path.join(folder, rel)
        if (x.endswith("/") or os.path.isdir(full)) and depth < 5 and os.path.exists(os.path.join(full, ".git")):
            sub = fs_git_tree(full, prefix + rel + "/", limit - len(out), depth + 1)
            if sub is not None:
                out.extend(sub)
                if len(out) >= limit:
                    break
                continue
        if not x.endswith("/"):
            out.append(x)
            if len(out) >= limit:
                break
    return out


def fs_all(root, limit=30000):
    """Every file of a project, for Find file and @-mentions. Inside git repositories git's
    list is used (so .gitignore counts) — also when the project folder holds several of them,
    and when a repository holds other repositories."""
    root = os.path.realpath(os.path.expanduser(root or "~"))
    if os.path.exists(os.path.join(root, ".git")):
        got = fs_git_tree(root, "", limit)
        if got is not None:
            return got[:limit]
    out = []
    for d, dirs, files in os.walk(root):
        rel = os.path.relpath(d, root)
        rel = "" if rel == "." else rel.replace(os.sep, "/") + "/"
        if rel and os.path.exists(os.path.join(d, ".git")):
            got = fs_git_tree(d, rel, limit - len(out))
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
            # argv: upload root folder name  — raw bytes on stdin (folder = rel, argv[2])
            folder = rel
            name = argv[3] if len(argv) > 3 else "upload"
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
        res = fs_remote(project, conf, [op, project["remote"], path, arg],
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
RMDIR_PY = r"""
import sys, os, json, shutil
def rm_project_dir(path):
    # deletes a whole project folder, refusing anything that looks like a home, a root or a mount
    if not path:
        return {"error": "no folder"}
    p = os.path.realpath(os.path.expanduser(path))
    home = os.path.realpath(os.path.expanduser("~"))
    parts = [x for x in p.replace("\\", "/").split("/") if x and not x.endswith(":")]
    if p == home or home.startswith(p.rstrip(os.sep) + os.sep) or len(parts) < 2 or os.path.ismount(p):
        return {"error": "refusing to delete " + p}
    if not os.path.exists(p):
        return {"deleted": False, "path": p}
    if not os.path.isdir(p):
        return {"error": "not a folder: " + p}
    errs = []
    shutil.rmtree(p, onerror=lambda f, x, e: errs.append(x + ": " + str(e[1])[:100]))
    return {"deleted": not os.path.exists(p), "path": p, "errors": errs[:10]}
if __name__ == "__main__":
    print(json.dumps(rm_project_dir(sys.argv[1] if len(sys.argv) > 1 else "")))
"""
_RMDIR = {}


def rm_project_dir(path: str) -> dict:
    if not _RMDIR:
        exec(compile(RMDIR_PY, "<rmdir>", "exec"), _RMDIR)
    return _RMDIR["rm_project_dir"](path)


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


# ----------------------------------------------------------------------------- fast first copy
# Add project with "Parallel copies": the Mutagen session is created paused, the files go over
# N separate ssh connections at once as tar streams (Mutagen alone sends one file after another
# over one connection — slow with many files), then the session is resumed and Mutagen only
# checks that both sides match. Best effort: whatever fails here, Mutagen copies afterwards.
COPY_LIST_PY = PRUNE_PY.split("\nif __name__")[0] + r"""

VCS_DIRS = {".git", ".svn", ".hg", ".bzr", "_darcs"}


def copy_list(root, patterns, git):
    # every file / link under root that is synced: [[rel, size], …]
    root = os.path.realpath(os.path.expanduser(root))
    if not os.path.isdir(root):
        return {"error": "folder not found: " + root}
    rules, out, stack = prune_compile(patterns), [], [""]
    while stack:
        rel_dir = stack.pop()
        try:
            entries = list(os.scandir(os.path.join(root, rel_dir) if rel_dir else root))
        except OSError:
            continue
        for e in entries:
            rel = rel_dir + "/" + e.name if rel_dir else e.name
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                if not git and is_dir and e.name in VCS_DIRS:
                    continue
                if prune_match(rules, rel, is_dir):
                    continue
                if is_dir:
                    stack.append(rel)
                elif e.is_file(follow_symlinks=False) or e.is_symlink():
                    out.append([rel, e.stat(follow_symlinks=False).st_size])
            except OSError:
                continue
    return {"files": out}


if __name__ == "__main__":
    print(json.dumps(copy_list(sys.argv[1], json.loads(sys.argv[2]), sys.argv[3] == "on")))
"""
_COPY_NS = {}
COPY_MAX_STREAMS = 16


def copy_list_local(root, patterns, git):
    if not _COPY_NS:
        ns = {"__name__": "simab_copy"}
        exec(compile(COPY_LIST_PY, "<copy>", "exec"), ns)
        _COPY_NS.update(ns)
    return _COPY_NS["copy_list"](root, patterns, git)


def copy_buckets(files, n):
    """Split [[rel, size], …] into n lists of about the same size (each sorted by path)."""
    n = max(1, min(int(n or 1), len(files) or 1))
    buckets, sizes = [[] for _ in range(n)], [0] * n
    for rel, size in sorted(files, key=lambda f: -f[1]):
        i = sizes.index(min(sizes))
        buckets[i].append(rel)
        sizes[i] += int(size) + 1024           # + tar header/padding, so many tiny files spread too
    return [sorted(b) for b in buckets if b]


def tar_size(size):
    return 512 + (int(size) + 511) // 512 * 512


class _Counted:
    """A pipe that counts the bytes going through it (the copy's progress)."""
    def __init__(self, f, job):
        self.f, self.job = f, job

    def write(self, b):
        self.job.add(len(b))
        return self.f.write(b)

    def read(self, n=-1):
        b = self.f.read(n)
        self.job.add(len(b))
        return b


class FastCopy:
    """One project's first copy (upload: this machine → server, download: server → here)."""
    def __init__(self, name, mode, local, remote, conf, ignores, git, streams, marker=None, on_done=None):
        self.name, self.mode, self.local, self.remote, self.conf = name, mode, local, remote, conf
        self.ignores, self.git, self.streams = list(ignores or []), bool(git), max(1, min(int(streams), COPY_MAX_STREAMS))
        self.marker, self.on_done = marker, on_done
        self.lock = threading.Lock()
        self.bytes = self.total = self.files = self.total_files = 0
        self.failed = []
        self.started = time.time()
        self.done = False
        self.stop = False

    def add(self, n):
        with self.lock:
            self.bytes += n

    def progress(self):
        with self.lock:
            got, total = self.bytes, max(self.total, self.bytes)
        out = {"files": self.files, "totalFiles": self.total_files, "bytes": got, "totalBytes": total,
               "eta": None, "percent": int(got * 100 / total) if total else 0, "streams": self.streams}
        out["percent"] = max(0, min(99, out["percent"]))
        el = time.time() - self.started
        if el >= 5 and got and total > got:
            out["eta"] = int((total - got) / (got / el))
        return out

    def _ssh(self, cmd, **kw):
        # its own connection each (no ControlMaster): the point is several TCP streams at once
        argv = ssh_base(self.conf, multiplex=False) + ["-T", "-o", "BatchMode=yes", "-o", "Compression=no", self.conf.target, cmd]
        return subprocess.Popen(argv, **kw)

    def _list(self):
        if self.mode == "upload":
            return copy_list_local(self.local, self.ignores, self.git)
        return remote_python(self.conf, COPY_LIST_PY, [self.remote, json.dumps(self.ignores), "on" if self.git else "off"],
                             multiplex=False, timeout=600)

    def _upload(self, rels):
        q = shlex.quote(self.remote)
        with tempfile.TemporaryFile() as errf:
            p = self._ssh(f"mkdir -p {q} && cd {q} && tar -xf -", stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errf)
            try:
                with tarfile.open(fileobj=_Counted(p.stdin, self), mode="w|") as tf:
                    for rel in rels:
                        if self.stop:
                            break
                        try:
                            tf.add(os.path.join(self.local, *rel.split("/")), arcname=rel, recursive=False)
                        except OSError:
                            self.failed.append(rel)
                        self.files += 1
                p.stdin.close()
            except (OSError, ValueError) as e:      # broken pipe: the server side stopped
                self.failed.append(str(e))
            rc = p.wait()
            if rc != 0:
                errf.seek(0)
                self.failed.append("tar on the server: " + (errf.read().decode("utf-8", "replace").strip().splitlines() or [str(rc)])[-1][:200])

    def _download(self, rels):
        q = shlex.quote(self.remote)
        root = os.path.realpath(self.local)
        with tempfile.TemporaryFile() as errf:
            p = self._ssh(f"cd {q} && tar -cf - --null -T -", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errf)

            def feed():
                try:
                    p.stdin.write(b"\0".join(r.encode("utf-8", "surrogateescape") for r in rels) + b"\0")
                    p.stdin.close()
                except OSError:
                    pass
            threading.Thread(target=feed, daemon=True).start()
            try:
                with tarfile.open(fileobj=_Counted(p.stdout, self), mode="r|") as tf:
                    for m in tf:
                        if self.stop:
                            break
                        try:
                            if hasattr(tarfile, "data_filter"):
                                tf.extract(m, root, filter="data")
                            elif not (m.name.startswith("/") or ".." in m.name.split("/") or m.issym() or m.islnk()):
                                tf.extract(m, root)
                        except (tarfile.TarError, OSError):
                            self.failed.append(m.name)
                        if not m.isdir():
                            self.files += 1
            except (tarfile.TarError, OSError) as e:
                self.failed.append(str(e))
            p.stdout.close()
            p.wait()       # GNU tar exits 1 when a file changed while it was read: fine, Mutagen checks it

    def run(self):
        try:
            got = self._list() or {}
            if got.get("error"):
                raise RuntimeError(got["error"])
            files = got.get("files") or []
            self.total_files = len(files)
            self.total = sum(tar_size(f[1]) for f in files) + 1024 * self.streams
            if self.mode == "download":
                os.makedirs(self.local, exist_ok=True)
            work = [threading.Thread(target=self._upload if self.mode == "upload" else self._download, args=(b,), daemon=True)
                    for b in copy_buckets(files, self.streams)]
            for t in work:
                t.start()
            for t in work:
                t.join()
        except Exception as e:      # noqa: BLE001 — Mutagen copies whatever is left
            self.failed.append(str(e))
        finally:
            self.done = True
            mutagen_resume(self.name)
            if self.marker:
                try:
                    os.unlink(self.marker)
                except OSError:
                    pass
            if self.on_done:
                self.on_done(self)


def mutagen_resume(name):
    exe = which("mutagen")
    if exe:
        try:
            subprocess.run([exe, "sync", "resume", name], capture_output=True, timeout=60,
                           env=dict(os.environ, PATH=tool_path()))
        except (OSError, subprocess.SubprocessError):
            pass


def fs_local():
    """The FS_PY functions, loaded once into this process."""
    if not _FS:
        ns = {"__name__": "simab_fs"}
        exec(compile(FS_PY, "<fs>", "exec"), ns)
        _FS.update(ns)
    return _FS


def fs_path_ok(path: str) -> bool:
    return path == "" or git_path_ok(path.strip("/"))


def fs_remote(project: dict, conf, args: list, **kw):
    """remote_python for a project's files; a Local only project whose computer is off says so in one line."""
    try:
        return remote_python(conf, FS_PY, args, **kw)
    except ApiError as e:
        if project.get("localOnly") and e.status in (502, 504):
            raise ApiError(503, "offline: the computer this project lives on is not connected")
        raise


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
        res = fs_remote(project, conf, [op, project["remote"], path],
                            multiplex=bool(app.store.settings.get("sshMultiplex")), timeout=40)
        if not isinstance(res, dict):
            raise ApiError(502, "bad answer from the server")
        if res.get("error"):
            raise ApiError(400, str(res["error"]))
    res["where"] = where
    return res


class FilesSession:
    """A Files or Changes tab: no process, it only keeps its place among the tabs (and after a restart)."""
    win = ""
    moved = 0.0

    def __init__(self, sid: str, project: str, title: str = "", kind: str = "files"):
        self.id = sid
        self.spec = {"kind": kind, "project": project, "where": ""}
        self.title = title or ("Changes · " if kind == "changes" else "Files · ") + project
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


# ----------------------------------------------------------------------------- devices
# The installed app (each laptop) and the web version share their projects and open tabs through
# one file on the SERVER (~/.config/simabworkspace/devices.json). Projects: every device puts its
# own list there (a snapshot); the others show those they do not have. Tabs: one shared set with
# small add / remove operations (a removed tab leaves a tombstone, so every device closes it too).
SHARE_PY = r"""
import json, os, sys, time, fcntl
d = os.path.expanduser("~/.config/simabworkspace")
os.makedirs(d, exist_ok=True)
f = os.path.join(d, "devices.json")
op = json.loads(sys.stdin.read() or "{}")
lk = open(f + ".lock", "a")
fcntl.flock(lk, fcntl.LOCK_EX)
try:
    with open(f, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        data = {}
except (OSError, ValueError):
    data = {}
now = time.time()
devs = {k: v for k, v in (data.get("devices") or {}).items() if isinstance(v, dict) and now - float(v.get("at") or 0) < 60 * 86400}
tabs = {k: v for k, v in (data.get("tabs") or {}).items() if isinstance(v, dict)}
closed = {k: v for k, v in (data.get("closed") or {}).items() if isinstance(v, dict) and now - float(v.get("at") or 0) < 7 * 86400}
me = op.get("device") or ""
if me:
    rec = devs.get(me) or {}
    rec.update(name=op.get("name") or "", at=now)
    if isinstance(op.get("projects"), list):
        rec["projects"] = op["projects"]
    devs[me] = rec
for t in op.get("add") or []:
    k = t.get("key")
    if k and k not in tabs:
        tabs[k] = {"spec": t.get("spec") or {}, "title": t.get("title") or "", "at": now, "by": me}
    if k:
        closed.pop(k, None)
for t in op.get("remove") or []:
    k = t.get("key")
    if k:
        tabs.pop(k, None)
        closed[k] = {"at": now, "by": me, "keep": bool(t.get("keep"))}
while len(tabs) > 60:
    tabs.pop(min(tabs, key=lambda k: tabs[k].get("at") or 0))
lay = data.get("layout") if isinstance(data.get("layout"), dict) else {}
groups = {k: v for k, v in (lay.get("groups") or {}).items() if isinstance(v, list) and len(v) == 2}
colors = {k: v for k, v in (lay.get("colors") or {}).items() if isinstance(v, list) and len(v) == 2}
order = lay.get("order") if isinstance(lay.get("order"), list) and len(lay.get("order")) == 2 else None
for ch in op.get("layout") or []:      # newest change wins; at 0 = a device's first list, only where nothing is yet
    if not isinstance(ch, dict):
        continue
    at, k, v = float(ch.get("at") or 0), ch.get("k"), ch.get("v")
    if k == "g" and isinstance(ch.get("r"), str) and ch["r"] and isinstance(v, str):
        cur = groups.get(ch["r"])
        if cur is None or at > float(cur[1] or 0):
            groups[ch["r"]] = [v[:40], at]
    elif k == "c" and isinstance(ch.get("g"), str) and ch["g"] and isinstance(v, str):
        cur = colors.get(ch["g"])
        if cur is None or at > float(cur[1] or 0):
            colors[ch["g"]] = [v[:7], at]
    elif k == "o" and isinstance(v, list):
        if order is None or at > float(order[1] or 0):
            order = [[str(x) for x in v if x][:2000], at]
data = {"devices": devs, "tabs": tabs, "closed": closed,
        "layout": {"groups": groups, "colors": colors, "order": order}}
tmp = f + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False)
os.replace(tmp, f)
print(json.dumps(data))
"""


def lan_ips() -> list:
    """This computer's address(es) on the local network (Wi-Fi / LAN), main one first: a phone on the same
    network opens http://IP:PORT. The UDP connect sends nothing; it only asks which interface would be used."""
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            out.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            out.append(info[4][0])
    except OSError:
        pass
    res = []
    for ip in out:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if a.is_private and not a.is_loopback and not a.is_link_local and ip not in res:
            res.append(ip)
    return res[:4]


def share_run_commands(p: dict) -> list:
    """A project's own run commands (not .simab.json ones) as another device sees them: where each runs."""
    on_pc = p.get("buildsOn") in ("computers", "local") and p.get("localExists")
    out = []
    for c in p.get("commands") or []:
        if not isinstance(c, dict) or c.get("shared") or not c.get("id"):
            continue
        where = "local" if on_pc else ("server" if c.get("where") == "server" else "local")
        if where == "local" and not p.get("localExists"):
            continue                  # this device could not run it
        out.append({"id": str(c["id"])[:40], "name": str(c.get("name") or "")[:80], "cmd": str(c.get("cmd") or "")[:2000],
                    "cwd": str(c.get("cwd") or "")[:300], "where": where})
    return out[:50]


def layout_key(remote) -> str:
    """Groups / order are shared by the project's server folder: devices may name the same folder differently."""
    r = str(remote or "").strip().rstrip("/")
    if r.startswith("~/"):
        r = r[2:]
    elif r.startswith("/home/") and r.count("/") >= 3:
        r = r.split("/", 3)[3]          # /home/esi/x and ~/x are the same folder
    return r


def share_tab_key(spec: dict) -> str:
    """The same tab on every device: a server chat by its conversation, Files / Changes by project,
    Tasks. Terminals and local tabs stay on their device ("")."""
    kind, proj = spec.get("kind"), spec.get("project") or ""
    if kind == "chat":
        conv = spec.get("resume") or ""
        if spec.get("watch") or spec.get("where") != "server" or not UUID_RE.fullmatch(conv):
            return ""
        return "chat:" + conv
    if kind in ("files", "changes") and proj:
        return kind + ":" + proj
    if kind == "tasks":
        return "tasks"
    return ""


# ----------------------------------------------------------------------------- tasks
# The user's own to-do list. It lives on the SERVER (~/.config/simabworkspace/tasks.json), not
# in this app's database, so the installed app on every laptop and the web version all see the
# same list. Every change is one small operation applied under a file lock (never "save the
# whole list"), so two devices editing at once do not undo each other.
TASKS_PY = r"""
import json, os, sys, time, secrets, fcntl
d = os.path.expanduser("~/.config/simabworkspace")
os.makedirs(d, exist_ok=True)
f = os.path.join(d, "tasks.json")
op = json.loads(sys.stdin.read() or "{}")
lk = open(f + ".lock", "a")
fcntl.flock(lk, fcntl.LOCK_EX)
try:
    with open(f, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        data = {}
except (OSError, ValueError):
    data = {}
tasks = [t for t in data.get("tasks") or [] if isinstance(t, dict) and t.get("id")]
for t in tasks:                      # lists from 1.41.0 had no status
    if t.get("status") not in ("backlog", "todo", "doing", "done"):
        t["status"] = "done" if t.get("done") else "todo"
kind, now = op.get("op") or "list", time.time()
by = {t["id"]: t for t in tasks}
t = by.get(op.get("id"))
changed = False
def set_status(t, st):
    if st != t.get("status"):
        t["status"], t["done"] = st, st == "done"
        t["doneAt"] = now if st == "done" else 0
if "status" not in op and "done" in op:
    op["status"] = "done" if op["done"] else "todo"
if kind == "add":
    if len(tasks) >= 2000:
        print(json.dumps({"error": "too many tasks (2000) — clear the done ones"})); sys.exit()
    t = {"id": secrets.token_hex(6), "text": op["text"], "project": op.get("project") or "",
         "prio": op.get("prio") or 2, "status": "todo", "done": False, "at": now, "doneAt": 0}
    set_status(t, op.get("status") or "todo")
    tasks.insert(0 if op.get("top") else len(tasks), t)
    changed = True
elif kind == "update" and t:
    for k in ("text", "project", "prio"):
        if k in op:
            t[k] = op[k]
    if op.get("status"):
        set_status(t, op["status"])
    changed = True
elif kind == "delete" and t:
    tasks.remove(t)
    changed = True
elif kind == "move" and t:
    # to another column (status) and/or another place: before / after a task, else at the end
    if op.get("status"):
        set_status(t, op["status"])
        changed = True
    ref = by.get(op.get("before") or op.get("after") or "")
    if ref is not t:
        tasks.remove(t)
        if ref is None:
            tasks.append(t)
        else:
            tasks.insert(tasks.index(ref) + (1 if op.get("after") else 0), t)
        changed = True
elif kind == "order":
    # these tasks, in this order, in the places they already take (the others stay put)
    want = [by[i] for i in op.get("ids") or [] if i in by]
    slots = sorted(tasks.index(x) for x in want)
    for i, x in zip(slots, want):
        tasks[i] = x
    changed = bool(want)
elif kind == "clear":
    p = op.get("project")
    tasks = [x for x in tasks if not (x.get("done") and (p is None or (x.get("project") or "") == p))]
    changed = True
rev = int(data.get("rev") or 0)
if changed:
    rev += 1
    data.update({"tasks": tasks, "rev": rev})
    tmp = f + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, f)
print(json.dumps({"tasks": tasks, "rev": rev}))
"""

# Persian voice made on the server, for devices that have none (▶ read). One docker image with
# Piper (offline, the text stays on the server) and edge-tts (Microsoft's voices: the text is sent
# to Microsoft). A container keeps the model loaded and answers on a unix socket (no port);
# the first call builds the image in the background and says "installing".
TTS_IMAGE = "simab-tts:1"
TTS_ENGINES = ("piper", "edge")
TTS_EDGE_VOICES = ("fa-IR-DilaraNeural", "fa-IR-FaridNeural")
TTS_MAX = 1500                    # characters per call: one piece of an answer (sounds.js cuts at 220)

TTS_SERVE = r'''
import asyncio, io, json, os, socketserver, wave
from http.server import BaseHTTPRequestHandler
from piper import PiperVoice
SOCK = "/run/tts/tts.sock"
piper = PiperVoice.load("/voices/fa.onnx")

def speak_piper(text):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        piper.synthesize_wav(text, w)
    return buf.getvalue(), "audio/wav"

def speak_edge(text, voice):
    import edge_tts
    async def run():
        out = b""
        async for c in edge_tts.Communicate(text, voice).stream():
            if c.get("type") == "audio":
                out += c["data"]
        return out
    return asyncio.run(run()), "audio/mpeg"

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_POST(self):
        try:
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if b.get("engine") == "edge":
                data, mime = speak_edge(b["text"], b.get("voice") or "fa-IR-DilaraNeural")
            else:
                data, mime = speak_piper(b["text"])
            code = 200 if data else 502
        except Exception as e:
            data, mime, code = str(e)[:300].encode(), "text/plain", 500
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

class S(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    def get_request(self):
        req, _ = self.socket.accept()
        return req, ("tts", 0)

if os.path.exists(SOCK):
    os.remove(SOCK)
srv = S(SOCK, H)
os.chmod(SOCK, 0o666)
srv.serve_forever()
'''

TTS_DOCKERFILE = r'''FROM python:3.12-slim
RUN pip install --no-cache-dir piper-tts==1.8.0 edge-tts==7.3.1
RUN mkdir -p /voices /run/tts && python -c "import urllib.request as u; b='https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/fa/fa_IR/gyro/medium/fa_IR-gyro-medium.onnx'; u.urlretrieve(b, '/voices/fa.onnx'); u.urlretrieve(b + '.json', '/voices/fa.onnx.json')"
COPY serve.py /app/serve.py
CMD ["python", "/app/serve.py"]
'''

TTS_PY = r"""
import base64, http.client, json, os, socket, subprocess, sys, time
IMAGE, NAME = sys.argv[1], "simab-tts"
HOME = os.path.expanduser("~/.cache/simab-tts")
RUN, BUILD = os.path.join(HOME, "run"), os.path.join(HOME, "build")
SOCK = os.path.join(RUN, "tts.sock")
req = json.loads(sys.stdin.read() or "{}")
def out(o):
    print(json.dumps(o)); sys.exit()
def sh(args, timeout=60):
    try:
        return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
if sh(["docker", "version", "-f", "{{.Server.Version}}"], 20) is None:
    out({"error": "the Persian voice needs docker on the server"})
def running():
    r = sh(["docker", "inspect", "-f", "{{.State.Running}} {{.Config.Image}}", NAME], 20)
    return r is not None and r.returncode == 0 and r.stdout.decode().split() == ["true", IMAGE]
def connect():
    class C(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(90)
            self.sock.connect(SOCK)
    return C("tts")
if not running():
    img = sh(["docker", "image", "inspect", IMAGE], 20)
    if img is None or img.returncode != 0:
        log = os.path.join(HOME, "build.log")
        busy = os.path.exists(log) and time.time() - os.path.getmtime(log) < 900
        if not busy:
            os.makedirs(BUILD, exist_ok=True)
            with open(os.path.join(BUILD, "Dockerfile"), "w") as f: f.write(req.get("dockerfile", ""))
            with open(os.path.join(BUILD, "serve.py"), "w") as f: f.write(req.get("serve", ""))
            with open(log, "w") as lf:
                subprocess.Popen(["docker", "build", "-t", IMAGE, BUILD], stdout=lf, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        out({"error": "installing", "installing": True})
    os.makedirs(RUN, exist_ok=True)
    sh(["docker", "rm", "-f", NAME], 30)
    if os.path.exists(SOCK): os.remove(SOCK)
    r = sh(["docker", "run", "-d", "--name", NAME, "--restart", "unless-stopped", "--memory", "1g",
            "-v", RUN + ":/run/tts", IMAGE], 60)
    if r is None or r.returncode != 0:
        out({"error": "cannot start the Persian voice: " + (r.stdout.decode("utf-8", "replace").strip()[-200:] if r else "?")})
    for _ in range(60):
        if os.path.exists(SOCK): break
        time.sleep(0.5)
body = json.dumps({"engine": req.get("engine"), "text": req.get("text"), "voice": req.get("voice")}).encode()
try:
    c = connect()
    c.request("POST", "/speak", body, {"Content-Type": "application/json"})
    r = c.getresponse()
    data = r.read()
except OSError as e:
    out({"error": "the Persian voice did not answer: " + str(e)})
if r.status != 200:
    out({"error": "the Persian voice failed: " + data.decode("utf-8", "replace")[:200]})
out({"audio": base64.b64encode(data).decode(), "mime": r.getheader("Content-Type") or "audio/wav"})
"""


def clean_tts(b) -> dict:
    """One piece to speak, checked here (the script on the server trusts it)."""
    if not isinstance(b, dict) or b.get("engine") not in TTS_ENGINES:
        raise ApiError(400, "unknown voice engine")
    text = b.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "nothing to read")
    if len(text) > TTS_MAX:
        raise ApiError(400, "text too long")
    voice = b.get("voice") if b.get("voice") in TTS_EDGE_VOICES else TTS_EDGE_VOICES[0]
    return {"engine": b["engine"], "text": text.strip(), "voice": voice,
            "dockerfile": TTS_DOCKERFILE, "serve": TTS_SERVE}

TASK_OPS = ("list", "add", "update", "delete", "move", "order", "clear")
TASK_ID_RE = re.compile(r"[0-9a-f]{12}")
TASK_STATUSES = ("backlog", "todo", "doing", "done")


def clean_task_op(b) -> dict:
    """One change to the task list, checked here (the script on the server trusts it)."""
    if not isinstance(b, dict) or b.get("op") not in TASK_OPS:
        raise ApiError(400, "unknown task operation")
    out = {"op": b["op"]}
    for k in ("id", "before", "after"):
        if b.get(k) is not None:
            if not (isinstance(b[k], str) and TASK_ID_RE.fullmatch(b[k])):
                raise ApiError(400, f"invalid {k}")
            out[k] = b[k]
    if b["op"] in ("update", "delete", "move") and "id" not in out:
        raise ApiError(400, "id is required")
    if "text" in b or b["op"] == "add":
        out["text"] = clean_str(b.get("text"), 1000, "text", required=True)
    if "project" in b and b["op"] != "clear" or b["op"] == "clear" and b.get("project") is not None:
        out["project"] = clean_str(b.get("project"), 200, "project")
    if "prio" in b:
        if b["prio"] not in (1, 2, 3):
            raise ApiError(400, "priority must be 1, 2 or 3")
        out["prio"] = b["prio"]
    if "done" in b:
        out["done"] = bool(b["done"])
    if b.get("status") is not None:
        if b["status"] not in TASK_STATUSES:
            raise ApiError(400, "invalid status")
        out["status"] = b["status"]
    if b["op"] == "order":
        ids = b.get("ids")
        if not (isinstance(ids, list) and len(ids) <= 2000 and all(isinstance(i, str) and TASK_ID_RE.fullmatch(i) for i in ids)
                and len(set(ids)) == len(ids)):
            raise ApiError(400, "invalid ids")
        out["ids"] = ids
    if b.get("top"):
        out["top"] = True
    return out


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
        sql, args = "SELECT id, t, kind, project, text, extra FROM activity WHERE 1=1", []
        if project:
            sql += " AND project = ?"
            args.append(project)
        if query:
            sql += " AND instr(lower(text || ' ' || project || ' ' || kind), ?) > 0"
            args.append(query.lower())
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 5000)))
        out = []
        for rid, t, kind, proj, text, extra in self.db.rows(sql, args):
            it = {"t": t, "kind": kind, "project": proj, "text": text}
            try:
                it.update(json.loads(extra))
            except ValueError:
                pass
            it["id"] = rid
            out.append(it)
        return out

    def delete(self, ids=None, session: str = "", clear: bool = False, project: str = "") -> int:
        """Remove rows: given ids, every row of one Claude conversation, or all (of one project). Only the list —
        the conversations themselves stay with Claude."""
        if clear:
            cur = self.db.run("DELETE FROM activity WHERE project = ?", (project,)) if project else self.db.run("DELETE FROM activity")
            return cur.rowcount
        n = 0
        if session:
            n += self.db.run("DELETE FROM activity WHERE json_extract(extra, '$.session') = ?", (str(session),)).rowcount
        ids = [int(i) for i in (ids or []) if str(i).isdigit()][:5000]
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            n += self.db.run(f"DELETE FROM activity WHERE id IN ({','.join('?' * len(part))})", part).rowcount
        return n


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
    """No ExitOnForwardFailure: a RemoteForward of the user's ~/.ssh/config that fails on the server
    (a user's Mac) would end the tunnel too. Tunnel checks the local port itself (`listening`)."""
    return ssh_base(conf, multiplex=False, batch=True, forwards=True) + [
        "-N", "-T", "-L", f"127.0.0.1:{t['localPort']}:{t['remoteHost']}:{t['remotePort']}", conf.target]


class Tunnel:
    """Keeps one `ssh -L` running; reconnects with a growing pause when it drops."""

    def __init__(self, project: str, spec: dict, conf_fn, popen=None, free=None, listening=None):
        self.project = project
        self.spec = dict(spec)
        self.conf_fn = conf_fn
        self.popen = popen or (lambda argv: subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=dict(os.environ, PATH=tool_path()), start_new_session=True))
        self.free = free or local_port_free
        self.listening = listening or (lambda port: not local_port_free(port))     # our ssh took the port
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
                    if not self.listening(lp):          # ssh is up but the local port did not open
                        self.state, self.error = "error", f"local port {lp} did not open — retrying in {pause}s"
                        try:
                            p.terminate()
                        except OSError:
                            pass
                        break
                    self.state, self.error, self.since = "up", "", time.time()
                    pause = 2
                self.wake.wait(0.5)
            if self.state == "error" and self.wanted:
                if self.wake.wait(pause):
                    return
                pause = min(pause * 2, 60)
                continue
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


# ----------------------------------------------------------------------------- builds on your own computer (onpc)
# Claude stays on the server; a project's builds / tests / runs can go to the user's own
# computers. Each computer that turned it on (Settings → Computers) keeps
# `ssh -R <home>/.cache/simabworkspace/pc/<id>.sock:127.0.0.1:<app port>` open: the folder is
# 0700 and the socket 0600, so only this server user (and root) can reach it; every request
# also carries the computer's own secret. `onpc CMD` on the server sends CMD there; the app
# maps the server folder to its synced copy, asks the user (rules: PC_DEFAULT), flushes
# Mutagen, runs it and streams the output back.
# Frames of the answer: b"o" (stdout) / b"e" (stderr) + 4-byte length + bytes; b"x" + 4-byte exit code.
# A project is built on the computers only when the user chose it ("Builds run on"):
# ~/.cache/simabworkspace/pc/projects.json {real server path: "computers"}; then every chat in
# it gets PC_HINT as extra system prompt (chat_remote_script asks `onpc --hint`).
PC_ASK_WAIT = 120
PC_MODES = ("chat", "every", "new", "all")       # chat = like the Claude chat that asks (auto → no question)
PC_OUTPUTS = ("chat", "panel")
PC_ANSWERS = ("once", "always", "hour", "deny")
PC_DEFAULT = {"on": False, "id": "", "secret": "", "name": "", "mode": "chat", "allow": [],
              "deny": ["sudo *", "git push*", "rm -rf /*", "rm -rf ~*"], "projects": [], "always": {},
              "output": "chat", "autoOpen": False,
              "shareRevoked": [],     # Share on your domain: guests taken back on this computer (refused here)
              "trusted": None,        # projects whose hooked commands follow the chat: set HERE only (None = not yet taken over)
              "mfa": "", "mfaOn": False, "mfaStep": 0,   # (old single key: moved into mfaDevs)
              "mfaDevs": [],          # phone access: one two-step key per device (phone, tablet…): id, name, key, step, at, last
              "mfaNew": None}         # a device being added (key shown, its first code not given yet)
PC_UNLOCK_TTL = 3600     # an MFA unlock from the phone / web: terminal, run commands, answers — no limits for 1 h
PC_TICKET_TTL = 60       # one terminal of the web version per ticket
PC_MFA_FAILS = 5         # wrong codes per 15 minutes before unlocking is refused
PC_ID_RE = re.compile(r"[0-9a-f]{12}")
PC_ONSERVER = "onserver "     # a Bash command starting with it stays on THIS server (the user asked so)
PC_ONSERVER_HINT = (
    "Only when the user explicitly asks to run something on this server (not on their computer), start the "
    "Bash command with `onserver ` (e.g. `onserver ls`): it then runs here, in this folder.")
PC_HINT = (
    "This project's commands run on the user's own computer, not on this server. The files here are "
    "the server's copy, synced both ways with the copy on that computer (Read, Edit, Write, Grep and Glob "
    "work here as usual). Every Bash command is run on the user's computer automatically, in the same "
    "folder there, after the sync caught up — so tools, node, git, docker etc. are the ones installed there; "
    "if it is Windows the shell is Git Bash. Your own files (~/.claude: memory, plans, settings; /tmp) are "
    "on this server: use Read/Write/Glob for them; a Bash command that mentions ~/.claude runs here. Git "
    "commands (only git, cd, head, tail …) run here too, where the repo is. `cd` "
    "does not carry over to the next command: use `cd dir && command`. Give long builds a long Bash timeout "
    "(600000 ms). When no computer is connected, a command runs on this server by itself (its output says so) — "
    "go on with the work. Dev servers (ng serve, npm start …): `onpc --bg NAME COMMAND` starts it on the "
    "computer and returns at once with its URL; `onpc --logs NAME`, `onpc --stop NAME`, `onpc --ps`. Never "
    "run a dev server in the foreground. " + PC_ONSERVER_HINT)

PC_LOCAL_HINT = (
    "This project lives only on the user's own computer: this folder is a live mount of it (nothing is "
    "stored on this server). Your file tools for it are mcp__pc__Read / Edit / Write / Grep / Glob: they run "
    "on the user's computer, where the files are — one round trip each, fast even on big projects and weak "
    "internet; use them (same arguments as Claude's own), not cat/grep/find/sed in Bash. Every Bash command is "
    "run on the user's computer automatically (in the same folder there) — so tools, node, git, docker "
    "etc. are the ones installed there; if it is Windows the shell is Git Bash. Your own files (~/.claude: "
    "memory, plans, settings; /tmp) are on this server, not there: read and write them with Read/Write/Glob, "
    "and put temporary files for commands inside the project folder, not in /tmp. A Bash command that "
    "mentions ~/.claude runs on this server. `cd` does not carry over "
    "to the next command: use `cd dir && command`. Give long builds a long Bash timeout (600000 ms). If a "
    "command says the computer is not connected, stop and tell the user. Dev servers (ng serve, npm start "
    "…): `onpc --bg NAME COMMAND` starts it on the computer and returns at once with its URL; "
    "`onpc --logs NAME`, `onpc --stop NAME`, `onpc --ps`. Never run a dev server in the foreground. "
    "Grep / Glob leave out heavy folders (node_modules, .venv, venv, __pycache__, .next, .gradle …) and .git; "
    "mcp__pc__Read can still read a file inside them. git status / log / diff go through Bash (it runs on "
    "the computer, where the repo is). " + PC_ONSERVER_HINT)

ONPC_PY = r'''#!/usr/bin/env python3
# onpc — run a command on your own computer instead of this server (Simab Workspace).
#   onpc npm run build        onpc "npm ci && npm test"        onpc --on laptop go test ./...
#   onpc --list               your computers and which are connected
import os, re, sys, json, socket, struct, signal, shlex
D = os.path.expanduser("~/.cache/simabworkspace/pc")
HINT = @@HINT@@
LOCAL_HINT = @@LOCAL_HINT@@
NOT_HERE = -404
USAGE = ("usage: onpc COMMAND…   run it on your own computer, in the synced copy of this folder\n"
         "       onpc --on NAME COMMAND…   on that computer\n"
         "       onpc --bg NAME COMMAND…   start a dev server there (keeps running); --logs NAME, --stop NAME, --ps\n"
         "       onpc --list   your computers")


def load(name, default):
    try:
        with open(os.path.join(D, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def reach(d):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect(os.path.join(D, d["id"] + ".sock"))
        return s
    except OSError:
        s.close()
        return None


def devices():
    out = []
    try:
        names = sorted(os.listdir(D))
    except OSError:
        names = []
    for f in names:
        if f.endswith(".json") and f != "projects.json":
            d = load(f, None)
            if isinstance(d, dict) and d.get("id"):
                s = reach(d)
                d["online"] = s is not None
                if s:
                    s.close()
                out.append(d)
    out.sort(key=lambda d: -(d.get("since") or 0))
    return out


def project_on(cwd):
    """→ ("computers" | "local" | "", its server folder)"""
    m = load("projects.json", {})
    for p, v in (m.items() if isinstance(m, dict) else []):
        if v in ("computers", "local") and (cwd == p or cwd.startswith(p.rstrip("/") + "/")):
            return v, p
    return "", ""


def pinned(cwd, why=False):
    """The computer chosen for this chat (its ⚒ chip), else for this project (Computers → Commands run on),
    else the default of all projects; "" = any connected one. why: → (id, "chat" | "project" | "default" | "")."""
    key = os.environ.get("SIMAB_CHAT", "")
    c = load("chatpins.json", {}) if key else {}
    if isinstance(c, dict) and key in c:            # changed in this chat ("" = back to the project's choice)
        dev = c[key] if isinstance(c[key], str) else ""
    else:
        dev = os.environ.get("SIMAB_PC", "")       # chosen in this chat before Claude started
    if dev and dev.isalnum():
        return (dev, "chat") if why else dev
    m = load("pins.json", {})
    m = m if isinstance(m, dict) else {}
    best, dev = "", ""
    for p, v in m.items():
        if p != "*" and (cwd == p or cwd.startswith(p.rstrip("/") + "/")) and len(p) > len(best):
            best, dev = p, v
    how = "project" if dev else ""
    if not dev and isinstance(m.get("*"), str) and m["*"]:
        dev, how = m["*"], "default"
    return (dev, how) if why else dev


CHOSEN = {"chat": "chosen for this chat", "project": "chosen for this project", "default": "your default computer"}


class RunNote:
    """A copy of a command's output on the server, for the web version / phone: it cannot reach the
    computer's app to see it live. D/runs/<chat key>/<id>.json {id, cmd, dev, start, state, code} + .log
    (first 2 MB). Only for a chat's commands (SIMAB_CHAT)."""
    def __init__(self, cmd, dev):
        import time
        self.time = time
        key = re.sub(r"[^0-9a-f]", "", os.environ.get("SIMAB_CHAT", ""))[:16]
        self.dir = os.path.join(D, "runs", key) if len(key) == 16 else ""
        self.meta = {"id": "%d%04d" % (time.time() * 1000, os.getpid() % 10000), "cmd": cmd[:2000],
                     "dev": dev.get("name") or "", "start": int(time.time()), "state": "running", "code": None}
        self.size = 0
        self._meta()

    def _meta(self):
        if not self.dir:
            return
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = os.path.join(self.dir, self.meta["id"] + ".tmp")
            with open(tmp, "w") as f:
                json.dump(self.meta, f)
            os.replace(tmp, os.path.join(self.dir, self.meta["id"] + ".json"))
        except OSError:
            self.dir = ""

    def add(self, b):
        if not self.dir or self.size > 2_000_000:
            return
        try:
            with open(os.path.join(self.dir, self.meta["id"] + ".log"), "ab") as f:
                f.write(b)
            self.size += len(b)
        except OSError:
            pass

    def done(self, code):
        self.meta.update(state="done", code=code)
        self._meta()


def send(d, cwd, cmd):
    s = reach(d)
    if s is None:
        return NOT_HERE
    s.settimeout(None)
    body = json.dumps({"cwd": cwd, "cmd": cmd, "chat": os.environ.get("SIMAB_CHAT", "")[:64],
                       "hooked": bool(project_on(cwd)[0])}).encode()
    head = ("POST /pc/run HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-PC: %s\r\nContent-Type: application/json\r\n"
            "Content-Length: %d\r\nConnection: close\r\n\r\n") % (int(d.get("port") or 0), d.get("secret") or "", len(body))
    try:
        s.sendall(head.encode() + body)
        f = s.makefile("rb")
        status = f.readline().split(b" ")
    except OSError:
        return NOT_HERE
    code = int(status[1]) if len(status) > 1 and status[1].isdigit() else 0
    while True:
        line = f.readline()
        if line in (b"\r\n", b"\n", b""):
            break
    if code != 200:
        try:
            msg = json.loads(f.read().decode("utf-8", "replace") or "{}").get("error") or ""
        except ValueError:
            msg = ""
        if code in (0, 404):
            return NOT_HERE
        sys.stderr.write("onpc: %s: %s\n" % (d.get("name") or "your computer", msg or "refused (HTTP %d)" % code))
        return 126 if code == 403 else 255
    note = RunNote(cmd, d)
    while True:
        k = f.read(1)
        if not k:
            sys.stderr.write("onpc: the connection to %s was lost — the command may not have finished.\n" % (d.get("name") or "your computer"))
            note.done(255)
            return 255
        n = struct.unpack(">i", f.read(4))[0]
        if k == b"x":
            note.done(n)
            return n
        out = sys.stdout if k == b"o" else sys.stderr
        chunk = f.read(n)
        out.buffer.write(chunk)
        out.flush()
        note.add(chunk)


def on_server(cmd, why):
    """No computer to run on, but the folder is this server's own (not a Local only mount): run it here."""
    sys.stderr.write("onpc: %s — ran on the server instead (the project folder is here).\n" % why)
    sys.stderr.flush()
    import subprocess
    return subprocess.call(["bash", "-c", cmd])


def run(cmd, on):
    cwd = os.path.realpath(os.getcwd())
    pin, how = ("", "") if on else pinned(cwd, True)
    devs = [d for d in devices() if d["online"] and (not on or on in (d.get("name"), d.get("id"))) and (not pin or d["id"] == pin)]
    here = not on and "/.simab-mnt/" not in cwd + "/"      # a Local only project lives only on its computer
    if not devs and here:
        name = next((d.get("name") for d in devices() if d["id"] == pin), "") if pin else ""
        return on_server(cmd, "%s (%s) is not connected" % (name or "the chosen computer", CHOSEN.get(how, how)) if pin
                         else "none of your computers is connected")
    if pin and not devs:
        name = next((d.get("name") for d in devices() if d["id"] == pin), "") or "the chosen computer"
        sys.stderr.write("onpc: %s (%s) is not connected — the command was NOT run.\n"
                         "Do not run it on the server or elsewhere: tell the user to open Simab Workspace there,\n"
                         "or to pick another computer (the ⚒ chip of this chat).\n" % (name, CHOSEN.get(how, how)))
        return 255
    if not devs:
        sys.stderr.write("onpc: none of your computers is connected%s — the command was NOT run.\n"
                         "Do not run it on the server instead: tell the user to open Simab Workspace on their "
                         "computer (Settings → Computers), then try again.\n" % (" (" + on + ")" if on else ""))
        return 255
    for d in devs:
        code = send(d, cwd, cmd)
        if code != NOT_HERE:
            return code
    sys.stderr.write("onpc: this folder is not synced to %s — the command was NOT run.\n"
                     % ", ".join(d.get("name") or "?" for d in devs))
    return 255


def sftp(dev_id, project):
    """sshfs runs this as its "ssh": SFTP over the Computers link to that computer's project folder."""
    d = load(dev_id + ".json", None) if dev_id.isalnum() else None
    s = reach(d) if isinstance(d, dict) else None
    if s is None:
        sys.stderr.write("onpc: the computer of this project is not connected\n")
        return 255
    s.settimeout(None)
    body = json.dumps({"project": project, "uid": os.getuid(), "gid": os.getgid()}).encode()
    s.sendall((("POST /pc/sftp HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-PC: %s\r\nContent-Type: application/json\r\n"
                "Content-Length: %d\r\n\r\n") % (int(d.get("port") or 0), d.get("secret") or "", len(body))).encode() + body)
    f = s.makefile("rb", buffering=0)
    head = b""
    while not head.endswith(b"\r\n\r\n"):
        c = f.read(1)
        if not c:
            return 255
        head += c
    if b" 200 " not in head.split(b"\r\n")[0] + b" ":
        sys.stderr.write("onpc: " + head.split(b"\r\n")[0].decode("latin-1") + "\n")
        return 255
    import threading

    def down():
        while True:
            b = s.recv(65536)
            if not b:
                break
            os.write(1, b)
        os._exit(0)
    threading.Thread(target=down, daemon=True).start()
    while True:
        b = os.read(0, 65536)
        if not b:
            break
        s.sendall(b)
    try:
        s.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    return 0


def term(project, ticket=""):
    """A terminal of the user's computer, in this one (the web version's tab runs this over ssh -tt).
    The computer opens it only with a one-time ticket of an MFA unlock (the web version gets one).
    To the computer: b"d"+len+keys, b"r"+cols+rows; back: what the terminal prints."""
    import select, termios, tty
    cols, rows = 80, 24
    try:
        cols, rows = os.get_terminal_size(0)
    except OSError:
        pass
    body = json.dumps({"project": project, "cols": cols, "rows": rows, "ticket": ticket}).encode()
    s, why = None, ""
    for d in [x for x in devices() if x["online"]]:
        s = reach(d)
        if s is None:
            continue
        s.settimeout(None)
        s.sendall((("POST /pc/term HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-PC: %s\r\nContent-Type: application/json\r\n"
                    "Content-Length: %d\r\n\r\n") % (int(d.get("port") or 0), d.get("secret") or "", len(body))).encode() + body)
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            c = s.recv(1)
            if not c:
                break
            head += c
        if b" 200 " in head.split(b"\r\n")[0] + b" ":
            break
        if b" 403 " in head.split(b"\r\n")[0] + b" ":
            try:
                s.settimeout(3)
                why = json.loads(s.recv(65536).decode("utf-8", "replace") or "{}").get("error") or why
            except (OSError, ValueError):
                pass
        s.close()
        s = None
    if s is None:
        sys.stderr.write("onpc: %s\r\n" % (why or "none of your connected computers has %s" % (project or "a home folder")))
        return 255
    old = termios.tcgetattr(0) if os.isatty(0) else None
    if old:
        tty.setraw(0)

    def winch(*_):
        try:
            c, r = os.get_terminal_size(0)
            s.sendall(b"r" + struct.pack(">HH", c, r))
        except OSError:
            pass
    signal.signal(signal.SIGWINCH, winch)
    try:
        while True:
            try:
                r, _, _ = select.select([0, s], [], [])
            except InterruptedError:
                continue
            if s in r:
                b = s.recv(65536)
                if not b:
                    break
                os.write(1, b)
            if 0 in r:
                b = os.read(0, 65536)
                if not b:
                    break
                s.sendall(b"d" + struct.pack(">I", len(b)) + b)
    finally:
        if old:
            termios.tcsetattr(0, termios.TCSADRAIN, old)
    return 0


def hook():
    """PreToolUse hook of a Local only project: every Bash command runs on the user's computer."""
    try:
        i = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    ti = dict(i.get("tool_input") or {})
    cmd = ti.get("command")
    me = os.path.realpath(os.path.abspath(sys.argv[0]))
    if not isinstance(cmd, str) or not cmd.strip() or cmd.lstrip().startswith(me):
        return 0
    if cmd.lstrip().startswith("onserver "):      # the user asked for this server (PC_ONSERVER): run it here as it is
        ti["command"] = cmd.lstrip()[len("onserver "):]
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": ti}}))
        return 0
    if cmd.lstrip().startswith("onpc "):          # Claude calls onpc itself (--bg …): just make sure it is found
        ti["command"] = shlex.quote(me) + cmd.lstrip()[4:]
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": ti}}))
        return 0
    # Claude's own files (memory, plans, settings, its task outputs) are on THIS server, not on the
    # computer: such a command stays here
    home = os.path.expanduser("~")
    if any(x in cmd for x in ("~/.claude", "$HOME/.claude", "${HOME}/.claude", home + "/.claude", "/tmp/claude-")):
        return 0
    if git_stays_here(cmd, i.get("cwd") or os.getcwd()):
        return 0
    ti["command"] = shlex.quote(me) + " " + shlex.quote(cmd)
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": ti}}))
    return 0


GIT_HELPERS = {"cd", "head", "tail", "grep", "wc", "sort", "echo", "true", "cat"}


def git_stays_here(cmd, cwd):
    """A synced project's copy on the computer often has no .git (sync without .git: "not a git
    repository"), but the repo is here: a command of only git (+ cd / head / tail …) runs on this
    server. Not for a Local only mount (the folder is the computer's own, mounted here)."""
    segs = [x.strip() for x in re.split(r"&&|\|\||[;|\n]", cmd) if x.strip()]
    first = [x.split()[0] for x in segs]
    if "git" not in first or any(w not in GIT_HELPERS and w != "git" for w in first):
        return False
    d = os.path.realpath(cwd or ".")
    while True:
        if d != "/" and os.path.ismount(d):
            return False
        if os.path.exists(os.path.join(d, ".git")):
            return True
        up = os.path.dirname(d)
        if up == d:
            return False
        d = up


def fs_call(op, args):
    """One Grep / Glob / Read / Edit / Write of a Local only project, done on the computer → (answer, error)."""
    cwd = os.path.realpath(os.getcwd())
    pin = pinned(cwd)
    devs = [d for d in devices() if d["online"] and (not pin or d["id"] == pin)]
    if not devs:
        return {}, ("the user's computer is not connected — nothing was done. Do not work around it on the server: "
                    "tell the user to open Simab Workspace on their computer.")
    raw = json.dumps({"cwd": cwd, "op": op, "args": args}).encode()
    why = ""
    for d in devs:
        s = reach(d)
        if s is None:
            continue
        s.settimeout(330)
        try:
            s.sendall((("POST /pc/fs HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-PC: %s\r\nContent-Type: application/json\r\n"
                        "Content-Length: %d\r\nConnection: close\r\n\r\n") % (int(d.get("port") or 0), d.get("secret") or "", len(raw))).encode() + raw)
            data = b""
            while True:
                c = s.recv(65536)
                if not c:
                    break
                data += c
        except OSError as e:
            why = str(e)
            continue
        finally:
            s.close()
        head, _, rest = data.partition(b"\r\n\r\n")
        try:
            j = json.loads(rest.decode("utf-8", "replace") or "{}")
        except ValueError:
            j = {}
        if b" 200 " in head.split(b"\r\n")[0] + b" ":
            return j, j.get("error") or ""
        old = b" 404 " in head[:20] and j.get("error") in (None, "", "not found")
        why = (j.get("error") if not old else "") or ("Simab Workspace on the computer is too old for this — update it there; until "
                                 "then search with Bash (grep / find run on the computer)" if old
                                 else head.split(b"\r\n")[0].decode("latin-1"))
    return {}, "onpc: " + (why or "the computer did not answer")


class Mux:
    """ONE connection to the computer, open for the whole chat (POST /pc/fsmux): requests carry an id,
    so many go at once and the answers come back in any order. Quality on any line:
    - heartbeat: a ping every 5 s while something waits (20 s idle); no answer in 12 s → the line is
      dead: closed at once, opened again (not ssh's own minute, nor the 330 s of a request);
    - lost on the way → read / grep / glob / write are tried once more on a new connection (doing
      them twice is harmless); an edit is not (it could be done twice) — Claude is told to read again;
    - big ones (a Write over 1 MB, images) go alone (fs_call), so they never hold up the small ones;
    - a computer whose app is older (no /pc/fsmux) → each call alone."""
    SAFE = {"grep", "glob", "read", "write"}
    IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp")

    def __init__(self, ping=5.0, idle=20.0, dead=12.0):
        import threading, time
        self.T, self.time = threading, time
        self.lock, self.send_lock, self.open_lock = threading.Lock(), threading.Lock(), threading.Lock()
        self.sock, self.next, self.waiting, self.old = None, 0, {}, False
        self.ping, self.idle, self.dead = ping, idle, dead

    def _open(self):
        cwd = os.path.realpath(os.getcwd())
        pin = pinned(cwd)
        for d in [x for x in devices() if x["online"] and (not pin or x["id"] == pin)]:
            s = reach(d)
            if s is None:
                continue
            s.settimeout(15)
            try:
                s.sendall((("POST /pc/fsmux HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-PC: %s\r\nX-Chat: %s\r\nContent-Length: 0\r\n\r\n")
                           % (int(d.get("port") or 0), d.get("secret") or "",
                              re.sub(r"[^\w-]", "", os.environ.get("SIMAB_CHAT", ""))[:64])).encode())
                head = b""
                while not head.endswith(b"\r\n\r\n") and len(head) < 8192:
                    c = s.recv(1)
                    if not c:
                        break
                    head += c
            except OSError:
                s.close()
                continue
            if b" 200 " in head.split(b"\r\n")[0] + b" ":
                s.settimeout(None)
                self.dev = d.get("id") or ""
                return s
            if b" 404 " in head.split(b"\r\n")[0] + b" ":
                self.old = True
            s.close()
        return None

    def note(self, n, rtt=None):
        """This chat's line, for the web version / phone (they cannot ask the computer's app):
        D/links/<chat key>.json {n: open connections, rtt: ms of the last ping, at, dev}."""
        key = re.sub(r"[^0-9a-f]", "", os.environ.get("SIMAB_CHAT", ""))[:16]
        if len(key) != 16:
            return
        try:
            d = os.path.join(D, "links")
            os.makedirs(d, exist_ok=True)
            tmp = os.path.join(d, key + ".tmp")
            with open(tmp, "w") as f:
                json.dump({"n": n, "rtt": rtt, "at": int(self.time.time()), "dev": getattr(self, "dev", "")}, f)
            os.replace(tmp, os.path.join(d, key + ".json"))
        except OSError:
            pass

    def _kill(self, s):
        with self.lock:
            if self.sock is s:
                self.sock = None
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        s.close()

    def _reader(self, s, st):
        f = s.makefile("rb")
        try:
            while True:
                head = f.read(4)
                if len(head) < 4:
                    break
                raw = f.read(struct.unpack(">I", head)[0])
                st["rx"] = self.time.time()
                try:
                    j = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if j.get("pong"):
                    if st.get("sent"):
                        st["rtt"] = int((st["rx"] - st["sent"]) * 1000)     # this chat's real round trip
                        self.note(1, st["rtt"])
                    continue
                with self.lock:
                    w = self.waiting.pop(j.pop("id", None), None)
                if w:
                    w[1].append(j)
                    w[0].set()
        except (OSError, ValueError):
            pass
        st["gone"] = True
        self.note(0, None)
        with self.lock:                                # the line dropped: whoever still waits is told now
            if self.sock is s:
                self.sock = None
            lost = [k for k, w in self.waiting.items() if w[2] is s]
            ws = [self.waiting.pop(k) for k in lost]
        for w in ws:
            w[0].set()

    def _heart(self, s, st):
        sent = 0.0
        while not st.get("gone"):
            self.time.sleep(min(self.ping, 1.0))
            now = self.time.time()
            with self.lock:
                busy = any(w[2] is s for w in self.waiting.values())
            if sent and st["rx"] < sent and now - sent > self.dead:
                self._kill(s)                          # no answer, not even to a ping: the line is dead
                return
            if now - max(sent, st["rx"]) >= (self.ping if busy else self.idle):
                sent = now if not sent or st["rx"] >= sent else sent
                st["sent"] = sent
                raw = json.dumps({"id": 0, "op": "ping", "rtt": st.get("rtt")}).encode()
                try:
                    with self.send_lock:
                        s.sendall(struct.pack(">I", len(raw)) + raw)
                except OSError:
                    self._kill(s)
                    return

    def _conn(self):
        with self.open_lock:                           # calls at the same moment: one opens, the rest use it
            with self.lock:
                s = self.sock
            if s is None and not self.old:
                s = self._open()
                if s is not None:
                    with self.lock:
                        self.sock = s
                    st = {"rx": self.time.time()}
                    self.note(1, None)
                    self.T.Thread(target=self._reader, args=(s, st), daemon=True).start()
                    self.T.Thread(target=self._heart, args=(s, st), daemon=True).start()
            return s

    def _once(self, op, args):
        """→ (answer, error, lost: the line dropped while it was out)"""
        s = self._conn()
        if s is None:
            j, err = fs_call(op, args)                 # old app on the computer, or none connected (it says so)
            return j, err, False
        ev, box = self.T.Event(), []
        with self.lock:
            self.next += 1
            rid = self.next
            self.waiting[rid] = (ev, box, s)
        raw = json.dumps({"id": rid, "cwd": os.path.realpath(os.getcwd()), "op": op, "args": args}).encode()
        try:
            with self.send_lock:
                s.sendall(struct.pack(">I", len(raw)) + raw)
        except OSError:
            with self.lock:
                self.waiting.pop(rid, None)
            self._kill(s)
            return {}, "onpc: the connection to the computer was lost — try again", True
        ev.wait(330)
        with self.lock:
            self.waiting.pop(rid, None)
        if not box:
            return {}, ("onpc: the connection to the computer was lost — it may or may not have been done "
                        "there; read the file again before retrying"), True
        return box[0], box[0].get("error") or "", False

    def big(self, op, args):
        if op == "write":
            return len(str(args.get("content") or "")) > 1_000_000
        return op == "read" and str(args.get("file_path") or "").lower().endswith(self.IMAGES)

    def call(self, op, args):
        if self.big(op, args):
            return fs_call(op, args)
        j, err, lost = self._once(op, args)
        if lost and op in self.SAFE:
            j, err, _ = self._once(op, args)           # a new connection; harmless to do it again
        return j, err


MCP_TOOLS = [
    {"name": "Grep", "description": (
        "Search file contents of this project with a regular expression (ripgrep syntax). It runs on the "
        "user's computer, where the files are — fast even on big projects; use it instead of grep/rg in Bash. "
        "output_mode: files_with_matches (default, file paths), content (matching lines; -n, -A/-B/-C, "
        "head_limit), count. Filter with glob (e.g. \"*.ts\", \"**/*.{ts,tsx}\") or type (js, py, rust, go …). "
        "Heavy folders (node_modules, .venv …) and .git are not searched."),
     "inputSchema": {"type": "object", "properties": {
         "pattern": {"type": "string", "description": "The regular expression to search for"},
         "path": {"type": "string", "description": "File or folder to search in (default: the project folder)"},
         "glob": {"type": "string", "description": "Glob of files to search, e.g. \"*.js\""},
         "type": {"type": "string", "description": "File type (rg --type), e.g. js, py"},
         "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"]},
         "-i": {"type": "boolean", "description": "Case insensitive"},
         "-n": {"type": "boolean", "description": "Line numbers (content mode, default true)"},
         "-A": {"type": "number"}, "-B": {"type": "number"}, "-C": {"type": "number"},
         "multiline": {"type": "boolean", "description": "Patterns may span lines; . matches newlines"},
         "head_limit": {"type": "number", "description": "Only the first N lines / files / counts"},
         "offset": {"type": "number", "description": "Skip the first N before head_limit"}},
         "required": ["pattern"]}},
    {"name": "Glob", "description": (
        "Find files of this project by name pattern (\"**/*.js\", \"src/**/*.{ts,tsx}\"), newest first, at most "
        "100. It runs on the user's computer, where the files are — fast even on big projects. .gitignore is "
        "kept; heavy folders (node_modules, .venv …) and .git are left out."),
     "inputSchema": {"type": "object", "properties": {
         "pattern": {"type": "string", "description": "The glob pattern"},
         "path": {"type": "string", "description": "Folder to search in (default: the project folder)"}},
         "required": ["pattern"]}},
    {"name": "Read", "description": (
        "Read a file of this project (absolute path). It is read on the user's computer, where the files are: "
        "one round trip. Lines come numbered (\"     1\\tcode\"); up to 2000 lines from offset (1-based) — use "
        "offset/limit for big files. Images (png, jpg, gif, webp) come as images. Files inside node_modules "
        "and other heavy folders can be read too."),
     "inputSchema": {"type": "object", "properties": {
         "file_path": {"type": "string", "description": "Absolute path of the file"},
         "offset": {"type": "number", "description": "First line (1-based)"},
         "limit": {"type": "number", "description": "How many lines"}},
         "required": ["file_path"]}},
    {"name": "Edit", "description": (
        "Replace text in a file of this project, on the user's computer. old_string must match the file "
        "exactly (without the line-number prefix of Read) and be unique unless replace_all. Windows line ends "
        "(CRLF) are matched and kept. Read the file first."),
     "inputSchema": {"type": "object", "properties": {
         "file_path": {"type": "string", "description": "Absolute path of the file"},
         "old_string": {"type": "string", "description": "The exact text to replace"},
         "new_string": {"type": "string", "description": "The new text"},
         "replace_all": {"type": "boolean", "description": "Replace every match"}},
         "required": ["file_path", "old_string", "new_string"]}},
    {"name": "Write", "description": (
        "Write a whole file of this project on the user's computer (creates folders; replaces the file). "
        "Prefer Edit for changes to an existing file."),
     "inputSchema": {"type": "object", "properties": {
         "file_path": {"type": "string", "description": "Absolute path of the file"},
         "content": {"type": "string", "description": "The whole new content"}},
         "required": ["file_path", "content"]}},
]
MCP_RO = {"Grep", "Glob", "Read"}


def mcp_serve():
    """MCP server (stdio, JSON lines) Claude starts for a Local only project: its file tools run on the
    user's computer instead of crawling the sshfs mount one file at a time. Tool calls run side by
    side (Claude often sends 5 Reads at once: one round trip, not five)."""
    import threading
    lock = threading.Lock()
    mux = Mux()

    def reply(out):
        with lock:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()

    def call(mid, prm):
        name, args = prm.get("name"), prm.get("arguments") or {}
        if name not in ("Grep", "Glob", "Read", "Edit", "Write") or not isinstance(args, dict):
            j, err = {}, "no such tool"
        else:
            j, err = mux.call(name.lower(), args)
        if not err and j.get("image"):
            res = {"content": [{"type": "image", "data": j["image"], "mimeType": j.get("mime") or "image/png"}]}
        else:
            res = {"content": [{"type": "text", "text": err or j.get("text") or ""}], "isError": bool(err)}
        reply({"jsonrpc": "2.0", "id": mid, "result": res})

    workers = []
    for line in sys.stdin:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if not isinstance(m, dict) or "id" not in m:
            continue                                  # notifications (initialized, cancelled …)
        meth, prm, res = m.get("method"), m.get("params") or {}, None
        if meth == "initialize":
            res = {"protocolVersion": prm.get("protocolVersion") or "2024-11-05", "capabilities": {"tools": {}},
                   "serverInfo": {"name": "pc", "version": "1"}}
        elif meth == "tools/list":
            res = {"tools": [dict(t, annotations={"readOnlyHint": t["name"] in MCP_RO}) for t in MCP_TOOLS]}
        elif meth == "tools/call":
            t = threading.Thread(target=call, args=(m["id"], prm), daemon=True)
            t.start()
            workers = [w for w in workers if w.is_alive()] + [t]
            continue
        elif meth == "ping":
            res = {}
        if res is None:
            out = {"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "method not found"}}
        else:
            out = {"jsonrpc": "2.0", "id": m["id"], "result": res}
        reply(out)
    for w in workers:                                 # stdin closed: let the last answers out
        w.join(330)
    return 0


def bg(body):
    """Dev servers on the computer, kept by the app there: --bg NAME CMD | --logs NAME | --stop NAME | --ps"""
    pin, how = pinned(os.path.realpath(os.getcwd()), True) if body.get("op") == "start" else ("", "")
    devs = [d for d in devices() if d["online"] and (not pin or d["id"] == pin)]
    if not devs:
        sys.stderr.write("onpc: %s is not connected — nothing was started or stopped.\n"
                         % ("the computer " + CHOSEN.get(how, "chosen") if pin else "none of your computers"))
        return 255
    body["cwd"] = os.path.realpath(os.getcwd())
    body["chat"] = os.environ.get("SIMAB_CHAT", "")[:64]
    for d in devs:
        s = reach(d)
        if s is None:
            continue
        s.settimeout(None)
        raw = json.dumps(body).encode()
        s.sendall((("POST /pc/bg HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-PC: %s\r\nContent-Type: application/json\r\n"
                    "Content-Length: %d\r\nConnection: close\r\n\r\n") % (int(d.get("port") or 0), d.get("secret") or "", len(raw))).encode() + raw)
        data = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            data += c
        s.close()
        head, _, rest = data.partition(b"\r\n\r\n")
        code = head.split(b" ")[1] if b" " in head else b"0"
        try:
            r = json.loads(rest.decode("utf-8", "replace") or "{}")
        except ValueError:
            r = {}
        if code == b"404" and body["op"] != "ps":
            continue                      # not this computer's project / process: try the next one
        if code != b"200":
            sys.stderr.write("onpc: %s\n" % (r.get("error") or "refused (HTTP %s)" % code.decode()))
            return 126 if code == b"403" else 255
        if r.get("text"):
            sys.stdout.write(r["text"] + ("" if r["text"].endswith("\n") else "\n"))
        return 0
    sys.stderr.write("onpc: not found on your connected computers\n")
    return 255


def main(a):
    if a[:1] == ["--term"]:
        t = a[a.index("--ticket") + 1] if "--ticket" in a[:-1] else ""
        rest = [x for i, x in enumerate(a[1:], 1) if x != "--ticket" and a[i - 1] != "--ticket"]
        return term(rest[0] if rest else "", t)
    if a[:1] == ["--bg"] and len(a) >= 3:
        return bg({"op": "start", "name": a[1], "cmd": a[2] if len(a) == 3 else " ".join(shlex.quote(x) for x in a[2:])})
    if a[:1] in (["--logs"], ["--stop"]) and len(a) >= 2:
        return bg({"op": a[0][2:], "name": a[1], "lines": int(a[2]) if len(a) > 2 and a[2].isdigit() else 80})
    if a[:1] == ["--ps"]:
        return bg({"op": "ps"})
    if a[:1] == ["--sftp"] and len(a) >= 3:
        return sftp(a[1], a[2])
    if a[:1] == ["--hook"]:
        return hook()
    if a[:1] == ["--hint"]:
        on = project_on(os.path.realpath(os.getcwd()))[0]
        if on:
            sys.stdout.write(LOCAL_HINT if on == "local" else HINT)
        return 0
    if a[:1] == ["--local"]:          # Local only / My computers: Claude's settings (the Bash hook); 3 = not mounted
        on, root = project_on(os.path.realpath(os.getcwd()))
        if not on:
            return 0
        if on == "local" and not os.path.ismount(root):
            return 3
        me = shlex.quote(sys.executable) + " " + shlex.quote(os.path.realpath(os.path.abspath(sys.argv[0])))
        st = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": me + " --hook"}]}]}}
        if on == "local":       # Grep / Glob of the computer (--mcp): read-only, never asked; the mount's own are off
            st["permissions"] = {"allow": ["mcp__pc__Grep", "mcp__pc__Glob", "mcp__pc__Read"], "deny": ["Grep", "Glob"]}
        sys.stdout.write(json.dumps(st))
        return 0
    if a[:1] == ["--mcp"]:            # Local only: the MCP config of Grep / Glob on the computer
        on = project_on(os.path.realpath(os.getcwd()))[0]
        if on == "local":
            # Claude gives an MCP server only a few variables (HOME, PATH…), not SIMAB_CHAT / SIMAB_PC: passed here,
            # else this chat's connection is filed under no chat (its badge always said 0)
            env = {k: os.environ[k] for k in ("SIMAB_CHAT", "SIMAB_PC") if os.environ.get(k)}
            sys.stdout.write(json.dumps({"mcpServers": {"pc": dict({"type": "stdio", "command": sys.executable,
                                         "args": [os.path.realpath(os.path.abspath(sys.argv[0])), "--mcp-serve"]},
                                         **({"env": env} if env else {}))}}))
        return 0
    if a[:1] == ["--mcp-serve"]:
        return mcp_serve()
    if a[:1] == ["--git"] and len(a) == 4:         # --git PROG ARGV-JSON REPO: git on the computer, its output as git's
        try:
            argv = json.loads(a[2])
        except ValueError:
            argv = None
        import base64
        j, err = fs_call("git", {"prog": a[1], "argv": argv, "repo": a[3],
                                 "stdin": base64.b64encode(sys.stdin.buffer.read()).decode()})
        if err:
            sys.stderr.write(err + "\n")
            return 255
        sys.stdout.buffer.write(base64.b64decode(j.get("out") or ""))
        sys.stderr.write(j.get("err") or "")
        return int(j.get("code") or 0)
    if a[:1] == ["--fs"] and len(a) == 3:          # one file request of this Local only folder → JSON
        try:
            args = json.loads(a[2])
        except ValueError:
            args = None
        j, err = fs_call(a[1], args) if isinstance(args, dict) else ({}, "bad arguments")
        sys.stdout.write(json.dumps({"error": err} if err else j))
        return 0
    if a[:1] == ["--list"]:
        ds = devices()
        for d in ds:
            print(("connected " if d["online"] else "offline   ") + (d.get("name") or "?") + "  (" + (d.get("os") or "?") + ")")
        if not ds:
            print("no computers yet — turn it on in Simab Workspace: Settings → Computers")
        return 0
    on = ""
    if a[:1] == ["--on"] and len(a) > 2:
        on, a = a[1], a[2:]
    if not a or a[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if a else 2
    return run(a[0] if len(a) == 1 else " ".join(shlex.quote(x) for x in a), on)


if __name__ == "__main__":
    for sig in ("SIGTERM", "SIGHUP"):
        signal.signal(getattr(signal, sig), lambda *_: sys.exit(143))
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
'''.replace("@@HINT@@", json.dumps(PC_HINT)).replace("@@LOCAL_HINT@@", json.dumps(PC_LOCAL_HINT))

# Installs onpc and this computer's card on the server. stdin: {"onpc", "device", "paths": {project: server path}}
PC_SETUP_PY = r'''
import os, sys, json
cfg = json.loads(sys.stdin.read() or "{}")
D = os.path.expanduser("~/.cache/simabworkspace/pc")
os.makedirs(D, exist_ok=True)
os.chmod(D, 0o700)
def put(path, text, mode):
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path + ".tmp", mode)
    os.replace(path + ".tmp", path)
if cfg.get("onpc"):
    b = os.path.expanduser("~/.local/bin")
    os.makedirs(b, exist_ok=True)
    put(os.path.join(b, "onpc"), cfg["onpc"], 0o755)
dev = cfg.get("device")
if isinstance(dev, dict) and dev.get("id"):
    put(os.path.join(D, os.path.basename(dev["id"]) + ".json"), json.dumps(dev), 0o600)
    # the socket of the last link stays behind (the computer slept, the app restarted) and sshd
    # does not replace it (StreamLocalBindUnlink is off by default on the server side):
    # "remote port forwarding failed for listen path"
    try:
        os.remove(os.path.join(D, os.path.basename(dev["id"]) + ".sock"))
    except OSError:
        pass
real = {k: os.path.realpath(os.path.expanduser(v)) for k, v in (cfg.get("paths") or {}).items() if isinstance(v, str) and v}
print(json.dumps({"home": os.path.expanduser("~"), "dir": D, "real": real}))
'''

# list (stdin {"paths"}) | remove ID | builds PATH computers|server
PC_ADMIN_PY = r'''
import os, sys, json, socket
D = os.path.expanduser("~/.cache/simabworkspace/pc")
op = sys.argv[1]
def load(n, d):
    try:
        with open(os.path.join(D, n)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return d
def builds_of(paths):
    m = load("projects.json", {})
    out = {}
    for name, p in paths.items():
        r = os.path.realpath(os.path.expanduser(p)) if isinstance(p, str) and p else ""
        out[name] = m.get(r) if r and m.get(r) in ("computers", "local") else "server"
    return out
if op == "list":
    cfg = json.loads(sys.stdin.read() or "{}")
    devs = []
    for f in sorted(os.listdir(D)) if os.path.isdir(D) else []:
        if not f.endswith(".json") or f == "projects.json":
            continue
        d = load(f, None)
        if not isinstance(d, dict) or not d.get("id"):
            continue
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        try:
            s.connect(os.path.join(D, d["id"] + ".sock"))
            on = True
        except OSError:
            on = False
        finally:
            s.close()
        devs.append({"id": d.get("id"), "name": d.get("name") or "", "os": d.get("os") or "",
                     "since": d.get("since") or 0, "version": d.get("version") or "", "online": on})
    pins = load("pins.json", {})
    pinned = {}
    for name, p in (cfg.get("paths") or {}).items():
        r = os.path.realpath(os.path.expanduser(p)) if isinstance(p, str) and p else ""
        if r and isinstance(pins, dict) and pins.get(r):
            pinned[name] = pins[r]
    print(json.dumps({"devices": devs, "builds": builds_of(cfg.get("paths") or {}), "pins": pinned,
                      "default": pins.get("*") or "" if isinstance(pins, dict) else ""}))
elif op == "link":                     # Share on your domain: the web version forwards to this socket
    i = os.path.basename(sys.argv[2])
    d = load(i + ".json", None)
    d = d if isinstance(d, dict) else {}
    print(json.dumps({"sock": os.path.join(D, i + ".sock"), "secret": d.get("secret") or "", "port": d.get("port") or 0}))
elif op == "remove":
    for ext in (".json", ".sock"):
        try:
            os.remove(os.path.join(D, os.path.basename(sys.argv[2]) + ext))
        except OSError:
            pass
    print("{}")
elif op == "builds":
    r = os.path.realpath(os.path.expanduser(sys.argv[2]))
    os.makedirs(D, exist_ok=True)
    os.chmod(D, 0o700)
    m = load("projects.json", {})
    m = m if isinstance(m, dict) else {}
    if sys.argv[3] in ("computers", "local"):
        m[r] = sys.argv[3]
    else:
        m.pop(r, None)
    fd = os.open(os.path.join(D, "projects.json.tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(m, f)
    os.replace(os.path.join(D, "projects.json.tmp"), os.path.join(D, "projects.json"))
    # which computer (empty = any connected one); Local only: the computer it lives on, so every computer
    # of this user knows the owner and onpc never sends its work to another one
    pins = load("pins.json", {})
    pins = pins if isinstance(pins, dict) else {}
    dev = sys.argv[4] if len(sys.argv) > 4 and sys.argv[3] in ("computers", "local") else ""
    if dev:
        pins[r] = dev
    else:
        pins.pop(r, None)
    fd = os.open(os.path.join(D, "pins.json.tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(pins, f)
    os.replace(os.path.join(D, "pins.json.tmp"), os.path.join(D, "pins.json"))
    print(json.dumps({"path": r, "on": sys.argv[3]}))
elif op in ("default", "chatpin"):     # default DEV | chatpin CHAT-KEY DEV ("" = any / back to the project's choice)
    os.makedirs(D, exist_ok=True)
    os.chmod(D, 0o700)
    name = "pins.json" if op == "default" else "chatpins.json"
    m = load(name, {})
    m = m if isinstance(m, dict) else {}
    k, dev = ("*", sys.argv[2]) if op == "default" else (sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    dev = dev if dev.isalnum() else ""
    if op == "default" and not dev:
        m.pop("*", None)
    else:
        m.pop(k, None)
        m[k] = dev
        while op == "chatpin" and len(m) > 300:       # old chats: the oldest go first
            m.pop(next(iter(m)))
    fd = os.open(os.path.join(D, name + ".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(m, f)
    os.replace(os.path.join(D, name + ".tmp"), os.path.join(D, name))
    print("{}")
'''

# mount | umount; stdin {"dev": computer id, "mounts": [{"name", "path"}]} → {name: "ok" | why not}
# sshfs: the system one, else our own copy unpacked from the distribution's package (no root needed).
PC_MOUNT_PY = r'''
import os, sys, json, shutil, subprocess, tempfile, glob, errno
cfg = json.loads(sys.stdin.read() or "{}")
op = sys.argv[1]
LIB = os.path.expanduser("~/.local/lib/simabworkspace/sshfs")
ONPC = os.path.expanduser("~/.local/bin/onpc")
# 4 parallel lines (a slow read no longer holds up every stat) + folder lists kept 20 s, symlinks 60 s;
# file sizes/times stay 3 s (Bash changes files on the computer, behind sshfs' back). An older sshfs
# that refuses an option gets the next, plainer set.
SSHFS_OPTS = ["reconnect,ServerAliveInterval=15,dir_cache=yes,dcache_timeout=3,dcache_dir_timeout=20,"
              "dcache_link_timeout=60,max_conns=4",
              "reconnect,ServerAliveInterval=15,dir_cache=yes,dcache_timeout=3,max_conns=1"]
def sshfs():
    p = shutil.which("sshfs")
    if p:
        return p, ""
    own = os.path.join(LIB, "usr", "bin", "sshfs")
    if not os.path.isfile(own) and shutil.which("apt-get") and shutil.which("dpkg-deb"):
        t = tempfile.mkdtemp()
        try:
            subprocess.run(["apt-get", "download", "sshfs"], cwd=t, capture_output=True, timeout=180)
            debs = glob.glob(os.path.join(t, "sshfs_*.deb"))
            if debs:
                os.makedirs(LIB, exist_ok=True)
                subprocess.run(["dpkg-deb", "-x", debs[0], LIB], capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            shutil.rmtree(t, ignore_errors=True)
    if os.path.isfile(own):
        return own, ""
    return "", "sshfs is not on the server — ask its admin to run: sudo apt install sshfs"
def fm():
    return shutil.which("fusermount3") or shutil.which("fusermount") or "fusermount"
def unmount(path):
    subprocess.run([fm(), "-u", "-z", path], capture_output=True, timeout=30)
def state(path):
    try:
        os.listdir(path)
    except OSError as e:
        if e.errno in (errno.ENOTCONN, errno.EIO):
            return "dead"
        if e.errno == errno.ENOENT:
            return "none"
        raise
    return "mounted" if os.path.ismount(path) else "dir"
out = {}
if op == "mount":
    exe, err = sshfs()
    for m in cfg.get("mounts") or []:
        name, path = m["name"], os.path.expanduser(m["path"])
        try:
            st = state(path)
            if st == "mounted":
                out[name] = "ok"
                continue
            if st == "dead":
                unmount(path)
            if err:
                out[name] = err
                continue
            os.makedirs(path, exist_ok=True)
            if os.listdir(path):
                out[name] = "the server folder " + path + " is not empty — it must stay empty"
                continue
            for opts in SSHFS_OPTS:
                with tempfile.TemporaryFile() as ef:
                    r = subprocess.run([exe, "pc:", path, "-o", "ssh_command=" + sys.executable + " " + ONPC + " --sftp " + cfg["dev"] + " " + name,
                                        "-o", opts], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=ef, timeout=60)
                    ef.seek(0)
                    msg = ef.read().decode("utf-8", "replace").strip()
                if r.returncode == 0 and os.path.ismount(path):
                    break
            out[name] = "ok" if r.returncode == 0 and os.path.ismount(path) else (msg.splitlines() or ["mounting failed"])[-1][:300]
        except (OSError, subprocess.SubprocessError) as e:
            out[name] = str(e)[:300]
    # the owner of each Local only project, kept on the server (pins.json): the web version, the phone and
    # my other computers learn from it which computer the project lives on (else they show "PC · off")
    dev = str(cfg.get("dev") or "")
    if dev.isalnum():
        try:
            D = os.path.expanduser("~/.cache/simabworkspace/pc")
            try:
                pins = json.load(open(os.path.join(D, "pins.json")))
            except (OSError, ValueError):
                pins = {}
            pins = pins if isinstance(pins, dict) else {}
            new = dict(pins, **{os.path.realpath(os.path.expanduser(m["path"])): dev for m in cfg.get("mounts") or []})
            if new != pins:
                fd = os.open(os.path.join(D, "pins.json.tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump(new, f)
                os.replace(os.path.join(D, "pins.json.tmp"), os.path.join(D, "pins.json"))
        except OSError:
            pass
elif op == "umount":
    for m in cfg.get("mounts") or []:
        path = os.path.expanduser(m["path"])
        try:
            if state(path) in ("mounted", "dead"):
                unmount(path)
            if os.path.isdir(path) and not os.listdir(path):
                os.rmdir(path)
            out[m["name"]] = "ok"
        except (OSError, subprocess.SubprocessError) as e:
            out[m["name"]] = str(e)[:300]
print(json.dumps(out))
'''


# Connect a computer from the web version: a one-time code (10 minutes, one use) for THIS server
# user. The new computer runs one line (install.sh / install.ps1 with SIMAB_PAIR=<url>), which
# installs the app and runs `simabworkspace pair <url>`: GET → the server's user/host/port, POST its
# new public key → the web version adds it to the user's authorized_keys (over its own ssh login).
PAIR_TTL = 600
PAIRS = {}                       # code -> {"conf": ServerConf, "until": time, "used": bool}
PAIR_LOCK = threading.Lock()
PUBKEY_RE = re.compile(r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/=]{40,1000}( [^\r\n]{0,100})?")
ADD_KEY_PY = r'''
import os, sys
key = sys.stdin.read().strip()
d = os.path.expanduser("~/.ssh")
os.makedirs(d, mode=0o700, exist_ok=True)
f = os.path.join(d, "authorized_keys")
have = open(f).read() if os.path.exists(f) else ""
if key.split(" ")[1] not in have:
    with open(f, "a") as fh:
        fh.write(("" if not have or have.endswith("\n") else "\n") + key + "\n")
os.chmod(f, 0o600)
print("{}")
'''


def pair_new(conf: "ServerConf") -> dict:
    code = secrets.token_urlsafe(24)
    with PAIR_LOCK:
        now = time.time()
        for k in [k for k, v in PAIRS.items() if v["until"] < now]:
            del PAIRS[k]
        PAIRS[code] = {"conf": conf, "until": now + PAIR_TTL, "used": False}
    return {"code": code, "until": now + PAIR_TTL}


def pair_take(code: str, consume=False):
    with PAIR_LOCK:
        p = PAIRS.get(code)
        if not p or p["used"] or p["until"] < time.time():
            return None
        if consume:
            p["used"] = True
        return p


def pair_computer(url: str) -> int:
    """`simabworkspace pair <url>` on the new computer: key, server, config, Computers on."""
    if not re.fullmatch(r"https?://[^/\s]+/pair/[A-Za-z0-9_-]{20,64}", url or ""):
        print("  ✗ not a pairing link — make a new one: web version → Computers → Connect a computer")
        return 1
    ctx = None
    if os.environ.get("SIMAB_PAIR_INSECURE") == "1":          # the web version with its own (self-signed) certificate
        import ssl
        ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(url, timeout=20, context=ctx) as r:
            info = json.loads(r.read().decode())
    except (OSError, ValueError) as e:
        print(f"  ✗ the link does not work (used, older than 10 minutes, or unreachable): {e}")
        return 1
    key = home_path(".ssh", "id_ed25519")
    if not key.exists() or not Path(str(key) + ".pub").exists():
        key = Path(create_ssh_key())
    pub = Path(str(key) + ".pub").read_text().strip()
    body = json.dumps({"pubkey": pub, "name": socket.gethostname().split(".")[0][:40], "os": pc_os()}).encode()
    try:
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
            json.loads(r.read().decode() or "{}")
    except (OSError, ValueError) as e:
        print(f"  ✗ the server did not take this computer's key: {e}")
        return 1
    conf = ServerConf(info["user"], info["host"], str(info.get("port") or 22), str(key))
    write_server_conf(conf)
    store = Store()
    c = dict(PC_DEFAULT)
    c.update(store.db.get("pc", {}) or {})
    c["on"] = True
    store.db.put("pc", c)
    print(f"  ✓ connected to {conf.target} (port {conf.port}) — Computers is on; open the app: simabworkspace gui")
    return 0


# The web version (and the phone) see and answer the permission questions of the user's computers:
# this runs on the server as the user and asks every connected computer over its socket.
# asks → {"asks": [...with dev, devName], "online": n};  answer DEV ID ANSWER → {}
PC_BRIDGE_PY = r'''
import os, sys, json, socket
D = os.path.expanduser("~/.cache/simabworkspace/pc")
def call(d, body):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(os.path.join(D, d["id"] + ".sock"))
    raw = json.dumps(body).encode()
    s.sendall(("POST /pc/remote HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-PC: %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
               % (int(d.get("port") or 0), d.get("secret") or "", len(raw))).encode() + raw)
    data = b""
    while True:
        c = s.recv(65536)
        if not c:
            break
        data += c
    s.close()
    return json.loads(data.partition(b"\r\n\r\n")[2] or b"{}")
devs = []
for f in sorted(os.listdir(D)) if os.path.isdir(D) else []:
    if f.endswith(".json") and f != "projects.json":
        try:
            d = json.load(open(os.path.join(D, f)))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("id"):
            devs.append(d)
out = {"asks": [], "online": 0}
if sys.argv[1] == "asks":
    for d in devs:
        try:
            r = call(d, {"op": "asks"})
        except (OSError, ValueError):
            continue
        out["online"] += 1
        out.setdefault("on", []).append(d["id"])
        for a in r.get("asks") or []:
            a.update(dev=d["id"], devName=d.get("name") or "")
            out["asks"].append(a)
        for x in r.get("bg") or []:
            x.update(dev=d["id"], devName=d.get("name") or "")
            out.setdefault("bg", []).append(x)
elif sys.argv[1] == "call":          # call DEV|"" [JSON]: that computer, or the first that knows it (not 404)
    body = json.loads(sys.argv[3]) if len(sys.argv) > 3 else json.load(sys.stdin)   # stdin: tokens stay out of ps
    out = {"error": "none of your computers is connected"}
    for d in devs:
        if sys.argv[2] and d["id"] != sys.argv[2]:
            continue
        try:
            r = call(d, body)
        except (OSError, ValueError):
            continue
        out = r
        if not (r.get("error") or "").startswith(("this project is not", "this run command is not", "not running here")):
            break
elif sys.argv[1] == "answer":
    d = next((x for x in devs if x["id"] == sys.argv[2]), None)
    out = call(d, {"op": "answer", "id": sys.argv[3], "answer": sys.argv[4]}) if d else {"error": "no such computer"}
print(json.dumps(out))
'''


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


# Setup check (⚙ → Setup check…): what the server has — Claude installed and signed in, sshfs for
# Local only projects (the system one, our own copy, or apt-get to fetch it).
SERVER_CHECK_PY = r'''
import os, sys, json, shutil, glob
H = os.path.expanduser("~")
c = shutil.which("claude") or ""
if not c:
    dirs = [H + "/.local/bin", H + "/.claude/local", H + "/.npm-global/bin", H + "/.bun/bin", H + "/.volta/bin",
            "/usr/local/bin", "/opt/homebrew/bin"]
    dirs += sorted(glob.glob(H + "/.nvm/versions/node/*/bin"))[-1:]
    dirs += sorted(glob.glob(H + "/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary"))[-1:]
    c = next((d + "/claude" for d in dirs if os.access(d + "/claude", os.X_OK)), "")
login = os.path.exists(H + "/.claude/.credentials.json") or bool(os.environ.get("ANTHROPIC_API_KEY"))
if not login:
    try:
        login = "oauthAccount" in json.load(open(H + "/.claude.json"))
    except (OSError, ValueError):
        pass
fs = "system" if shutil.which("sshfs") else "own" if os.path.isfile(H + "/.local/lib/simabworkspace/sshfs/usr/bin/sshfs") \
    else "apt" if shutil.which("apt-get") and shutil.which("dpkg-deb") else ""
print(json.dumps({"claude": c, "login": login, "sshfs": fs, "fuse": os.path.exists("/dev/fuse")}))
'''


# Saved prompts: kept on the SERVER (~/.config/simabworkspace/prompts.json) so the app and the web
# version share them. get → {rev, prompts}; put (stdin {rev, prompts}) → saved, or conflict + current.
PROMPTS_PY = r'''
import os, sys, json
d = os.path.expanduser("~/.config/simabworkspace")
f = os.path.join(d, "prompts.json")
try:
    cur = json.load(open(f))
except (OSError, ValueError):
    cur = {"rev": 0, "prompts": []}
if sys.argv[1] == "put":
    new = json.loads(sys.stdin.read() or "{}")
    if int(new.get("rev", -1)) != int(cur.get("rev", 0)):
        print(json.dumps(dict(cur, conflict=True)))
        sys.exit()
    os.makedirs(d, exist_ok=True)
    cur = {"rev": int(cur.get("rev", 0)) + 1, "prompts": new.get("prompts") or []}
    with open(f + ".tmp", "w") as fh:
        json.dump(cur, fh, ensure_ascii=False)
    os.replace(f + ".tmp", f)
print(json.dumps(cur, ensure_ascii=False))
'''


# History → "in chats": the text of every Claude conversation on the server (newest first).
# stdin {q, dirs: {project: server path}, only: bool} → {hits: [{project, dir, id, title, snippet, who, mtime}]}
CHAT_SEARCH_PY = r'''
import os, sys, json, re, glob
cfg = json.loads(sys.stdin.read() or "{}")
q = (cfg.get("q") or "").lower()
base = os.path.expanduser("~/.claude/projects")
def dname(x):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.expanduser(x))
names = {dname(v): k for k, v in (cfg.get("dirs") or {}).items() if v}
def text_of(e):
    c = (e.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    return " ".join(b.get("text") or "" for b in c if isinstance(b, dict) and b.get("type") == "text") if isinstance(c, list) else ""
def title_of(f):
    with open(f, errors="replace") as fh:
        for n, line in enumerate(fh):
            if n > 400:
                break
            if '"user"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") != "user" or e.get("isMeta"):
                continue
            t = text_of(e).strip()
            if t and not t.startswith("<"):
                return t.splitlines()[0][:90]
    return ""
files = []
for d in (os.listdir(base) if os.path.isdir(base) else []):
    if cfg.get("only") and d not in names:
        continue
    for f in glob.glob(os.path.join(base, d, "*.jsonl")):
        try:
            files.append((os.path.getmtime(f), d, f))
        except OSError:
            pass
files.sort(reverse=True)
hits = []
for mt, d, f in files[:800]:
    if not q:
        break
    found = None
    try:
        with open(f, errors="replace") as fh:
            for line in fh:
                if q not in line.lower() or len(line) > 3000000:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") not in ("user", "assistant") or e.get("isMeta"):
                    continue
                t = text_of(e)
                i = t.lower().find(q)
                if i >= 0:
                    found = (("…" if i > 60 else "") + t[max(0, i - 60):i + len(q) + 90].replace("\n", " "), e.get("type"))
                    break
    except OSError:
        continue
    if found:
        hits.append({"project": names.get(d, ""), "dir": d, "id": os.path.basename(f)[:-6], "title": title_of(f),
                     "snippet": found[0], "who": found[1], "mtime": mt})
        if len(hits) >= 40:
            break
print(json.dumps({"hits": hits}, ensure_ascii=False))
'''


def clean_prompts(v) -> list:
    if not isinstance(v, list) or len(v) > 200:
        raise ApiError(400, "invalid prompts")
    out = []
    for x in v:
        if not isinstance(x, dict):
            raise ApiError(400, "invalid prompt")
        out.append({"id": x.get("id") if isinstance(x.get("id"), str) and re.fullmatch(r"[0-9a-f]{8}", x.get("id") or "") else secrets.token_hex(4),
                    "name": clean_str(x.get("name"), 60, "name", required=True),
                    "text": clean_str(x.get("text"), 20000, "text", required=True),
                    "project": clean_str(x.get("project"), 64, "project")})
    return out


def pc_os() -> str:
    if IS_WIN:
        return "windows"
    if sys.platform == "darwin":
        return "mac"
    return "wsl" if is_wsl() else "linux"


def pc_segments(cmd: str) -> list:
    """`a && b; c | d` → each command on its own (rules apply to every part)."""
    return [s.strip() for s in re.split(r"&&|\|\||[;|\n]", cmd or "") if s.strip()]


def pc_match(seg: str, patterns) -> bool:
    import fnmatch
    return any(isinstance(p, str) and p.strip() and fnmatch.fnmatchcase(seg, p.strip()) for p in patterns or [])


def pc_guard(c: dict, project: str, cmd: str) -> str:
    """Why the allowed projects refuse it ("" = they do not). Applies to every command."""
    if c.get("projects") and project not in c["projects"]:
        return f"project {project} may not run commands on this computer (Settings → Computers)"
    return ""


def pc_denied(c: dict, cmd: str) -> str:
    """A part on the deny list → why. The user's wish (2026-11-22): such a command is not refused but
    ASKED every time (no mode, Always, 1 hour or auto chat skips the question); the user may allow it."""
    bad = [s for s in pc_segments(cmd) or [cmd.strip()] if pc_match(s, c.get("deny"))]
    return f"“{bad[0]}” is on the deny list of this computer" if bad else ""


def pc_decide(c: dict, project: str, cmd: str, hour: dict, now: float):
    """→ ("allow" | "ask" | "deny", why). Allowed projects win over everything; the deny list asks always."""
    why = pc_guard(c, project, cmd)
    if why:
        return "deny", why
    why = pc_denied(c, cmd)
    if why:
        return "ask", why
    segs = pc_segments(cmd) or [cmd.strip()]
    mode = c.get("mode")
    if mode == "all":
        return "allow", ""
    if mode == "every":
        return "ask", ""
    if all(pc_match(s, c.get("allow")) for s in segs):
        return "allow", ""
    if cmd.strip() in ((c.get("always") or {}).get(project) or []):
        return "allow", ""
    if hour.get(project, 0) > now:
        return "allow", ""
    return "ask", ""


PC_RETRY_MAX = 10        # the computer's link comes back within 10 s of the line (Local only lives on it)


def pc_link_argv(conf: ServerConf, sock: str, port: int) -> list:
    """No ExitOnForwardFailure: a RemoteForward in the user's ~/.ssh/config that fails on the server
    ("remote port forwarding failed for listen port 47123", a user's Mac) would end OUR link too.
    ClearAllForwardings would also drop our -R. So the link is checked on the server instead (_verify)."""
    # a dead line is noticed in ~15 s, not ssh_base's 60 (everything of Local only hangs meanwhile);
    # replaced in place: for ssh the FIRST value of an option wins, a later -o would be ignored
    fast = {"ServerAliveInterval=15": "ServerAliveInterval=5", "ServerAliveCountMax=4": "ServerAliveCountMax=3"}
    return [fast.get(x, x) for x in ssh_base(conf, multiplex=False, batch=True, forwards=True)] + [
        "-N", "-T", "-o", "StreamLocalBindUnlink=yes", "-o", "Compression=yes",     # weak lines: text shrinks 3-5x
        "-R", f"{sock}:127.0.0.1:{port}", conf.target]


def pc_shell_argv(cmd: str) -> list:
    """A login shell, so nvm / sdkman / Homebrew paths are there like in the user's own terminal."""
    if IS_WIN:
        return [bash_exe(), "-lc", cmd]
    return [os.environ.get("SHELL") or "/bin/sh", "-lc", cmd]


def mutagen_flush(name: str):
    exe = which("mutagen")
    if exe:
        try:
            subprocess.run([exe, "sync", "flush", name], capture_output=True, timeout=90,
                           env=dict(os.environ, PATH=tool_path()))
        except (OSError, subprocess.SubprocessError):
            pass


class PcHub:
    """This computer's side of onpc: settings, the ssh -R link, permission asks, running commands.
    Not used by the web version (it has no computer of its own)."""

    def __init__(self, app, popen=None, remote=None):
        self.app = app
        self.popen = popen or (lambda argv: subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=dict(os.environ, PATH=tool_path()), start_new_session=True))
        self.remote = remote or remote_python
        self.lock = threading.RLock()
        self.state, self.error, self.since = "off", "", 0.0
        self.proc = None
        self.wanted = False
        self.wake = threading.Event()
        self.home = ""
        self.real = {}                 # project -> its real path on the server
        self.asks = {}                 # id -> waiting permission question
        self.guests = {}               # id -> someone asks to open a shared link (Share on your domain, "Ask me")
        self.hour = {}                 # project -> everything allowed until (time)
        self.runs = collections.deque(maxlen=20)
        self.mounts = {}               # Local only project -> "ok" | why it is not mounted on the server
        self.mount_lock = threading.Lock()  # connect's mount and a chat's mount at once: one sshfs per folder
        self.meter = [0, 0]            # bytes over the link: [from the server (down), to the server (up)]
        self.fsstats = sftp_stats()    # Local only: file requests from the server (all mounts together)
        self.chatlinks = {}            # Local only: chat relay key → {n: open connections, rtt: ms of its ping}
        self.bg = {}                   # dev servers here (onpc --bg): name -> info; kept when the link drops
        self.net = {"rtt": None, "down": 0.0, "up": 0.0}
        self._net_thread = None
        self.unlocks = {}              # MFA unlocks from the phone / web: id -> {id, hash, by, at, until}
        self.tickets = {}              # one-time terminal tickets: hash -> {unlock, project, until}
        self.terms = {}                # terminals opened from the server: id -> {id, unlock, project, at, by, proc}
        self.mfa_fails = collections.deque()

    # settings ---------------------------------------------------------------
    def cfg(self) -> dict:
        with self.lock:
            saved = self.app.store.db.get("pc", {}) or {}
            c = dict(PC_DEFAULT)
            c.update({k: v for k, v in saved.items() if k in PC_DEFAULT} if isinstance(saved, dict) else {})
            if not PC_ID_RE.fullmatch(c.get("id") or "") or len(c.get("secret") or "") < 32:
                c["id"] = c["id"] if PC_ID_RE.fullmatch(c.get("id") or "") else secrets.token_hex(6)
                c["secret"] = c["secret"] if len(c.get("secret") or "") >= 32 else secrets.token_urlsafe(32)
                self.app.store.db.put("pc", c)
            if c.get("mfa") and c.get("mfaOn") and not c.get("mfaDevs"):      # one key of an older version → its first device
                c["mfaDevs"] = [{"id": secrets.token_hex(6), "name": "Phone", "key": c["mfa"], "step": int(c.get("mfaStep") or 0),
                                 "at": time.time(), "last": 0}]
                c["mfa"], c["mfaStep"] = "", 0
                self.app.store.db.put("pc", c)
            c["mfaDevs"] = [d for d in (c.get("mfaDevs") or []) if isinstance(d, dict) and d.get("key")]
            c["mfaOn"] = bool(c["mfaDevs"])
            if not c.get("name"):
                c["name"] = (socket.gethostname() or "computer").split(".")[0][:40]
            return c

    def trust(self, names, on: bool):
        """Projects whose hooked commands follow the chat ("same as chat"): only what was chosen on THIS computer."""
        names = [names] if isinstance(names, str) else list(names)
        with self.lock:
            c = self.cfg()
            t = [x for x in (c.get("trusted") or []) if x not in names] + (names if on else [])
            c["trusted"] = t
            self.app.store.db.put("pc", c)

    def public(self) -> dict:
        c = self.cfg()
        for k in ("secret", "mfa", "mfaStep", "mfaDevs", "mfaNew"):
            c.pop(k, None)
        return {"config": c, "state": self.state, "error": self.error, "since": self.since, "os": pc_os(),
                "mounts": dict(self.mounts)}

    def update(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise ApiError(400, "bad request")
        with self.lock:
            c = self.cfg()
            old = (c["on"], c["name"])
            if "on" in patch:
                c["on"] = bool(patch["on"])
            if "autoOpen" in patch:
                c["autoOpen"] = bool(patch["autoOpen"])
            if "name" in patch:
                c["name"] = clean_str(patch["name"], 40, "name", required=True)
            if "mode" in patch:
                if patch["mode"] not in PC_MODES:
                    raise ApiError(400, "invalid mode")
                c["mode"] = patch["mode"]
            if "output" in patch:
                if patch["output"] not in PC_OUTPUTS:
                    raise ApiError(400, "invalid output")
                c["output"] = patch["output"]
            for k in ("allow", "deny", "projects"):
                if k in patch:
                    v = patch[k]
                    if not isinstance(v, list) or len(v) > 200 or not all(isinstance(x, str) and len(x) <= 300 for x in v):
                        raise ApiError(400, f"invalid {k} list")
                    c[k] = [x.strip() for x in v if x.strip()]
            if "always" in patch:
                v = patch["always"]
                if not isinstance(v, dict) or not all(isinstance(x, list) and all(isinstance(y, str) for y in x) for x in v.values()):
                    raise ApiError(400, "invalid always list")
                c["always"] = {k: [y for y in x if y.strip()][:200] for k, x in v.items() if x}
            self.app.store.db.put("pc", c)
        if (c["on"], c["name"]) != old:
            if old[0] and not c["on"]:
                threading.Thread(target=self.unmount, daemon=True).start()     # turned off: the folders leave the server
            self.stop()
            if c["on"]:
                self.start()
        return self.public()

    # the link ---------------------------------------------------------------
    def start(self):
        if self.app.web or self.wanted or not self.cfg()["on"] or not self.app.conf.configured:
            return
        self.wanted = True
        self.wake.clear()
        self.state, self.error = "connecting", ""
        threading.Thread(target=self._loop, daemon=True).start()

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

    def _setup(self) -> str:
        """Installs onpc + this computer's card on the server; → the socket path there."""
        c = self.cfg()
        paths = {p["name"]: p["remote"] for p in self.app.projects() if p.get("remote")}
        dev = {"id": c["id"], "name": c["name"], "os": pc_os(), "port": self.app.port, "secret": c["secret"],
               "since": time.time(), "version": VERSION}
        r = self.remote(self.app.conf, PC_SETUP_PY, [], multiplex=False, timeout=30,
                        stdin=json.dumps({"onpc": ONPC_PY, "device": dev, "paths": paths}).encode())
        if not isinstance(r, dict) or not r.get("dir"):
            raise ApiError(502, "bad answer from the server")
        self.home, self.real = r.get("home") or "", r.get("real") or {}
        return r["dir"].rstrip("/") + "/" + c["id"] + ".sock"

    def _loop(self):
        self.wake.clear()
        pause = 2
        while self.wanted:
            self.state = "connecting"
            started = time.time()
            try:
                sock = self._setup()
                self.proc = p = self.popen(pc_link_argv(self.app.conf, sock, self.app.port))
            except (ApiError, OSError) as e:
                self.state, self.error = "error", (getattr(e, "message", "") or str(e))[:200] + f" — retrying in {pause}s"
                if self.wake.wait(pause):
                    return
                pause = min(pause * 2, PC_RETRY_MAX)
                continue
            while self.wanted and p.poll() is None:
                if self.state != "connected" and time.time() - started > 2.5:
                    why = self._verify()
                    if why:                         # ssh is up but our socket is not there
                        self.state, self.error = "error", why[:200] + f" — retrying in {pause}s"
                        try:
                            p.terminate()
                        except OSError:
                            pass
                        break
                    self.state, self.error, self.since = "connected", "", time.time()
                    pause = 2
                    threading.Thread(target=self.mount, daemon=True).start()
                    if not (self._net_thread and self._net_thread.is_alive()):
                        self._net_thread = threading.Thread(target=self._net_loop, daemon=True)
                        self._net_thread.start()
                self.wake.wait(0.5)
            if self.state == "error" and self.wanted:
                if self.wake.wait(pause):
                    return
                pause = min(pause * 2, PC_RETRY_MAX)
                continue
            if not self.wanted:
                return
            err = ""
            try:
                err = (p.stderr.read() or b"").decode("utf-8", "replace").strip() if p.stderr else ""
            except (OSError, ValueError):
                pass
            last = [x for x in err.splitlines() if x.strip() and "Warning: Permanently added" not in x]
            self.state = "error"
            self.error = (last[-1] if last else "the connection closed")[:200] + f" — retrying in {pause}s"
            if self.wake.wait(pause):
                return
            pause = min(pause * 2, PC_RETRY_MAX)

    # the link's quality: round trip to the server (TCP connect to its ssh port) + traffic per second
    def _net_loop(self, tick=1.0):
        last, t0, n, rtts = list(self.meter), time.time(), 0, []
        lastn = self.fsstats["n"]
        while self.wanted:
            if self.wake.wait(tick):
                if not self.wanted:
                    break
            now = time.time()
            dt = max(now - t0, 1e-3)
            cur = list(self.meter)
            self.net["down"], self.net["up"] = (cur[0] - last[0]) / dt, (cur[1] - last[1]) / dt
            last, t0 = cur, now
            self.net["ops"], lastn = (self.fsstats["n"] - lastn) / dt, self.fsstats["n"]
            if n % 3 == 0:
                rtts = (rtts + [pc_rtt(self.app.conf.host, self.app.conf.port)])[-3:]
                ok = [x for x in rtts if x is not None]
                self.net["rtt"] = sorted(ok)[len(ok) // 2] if ok and rtts[-1] is not None else None
            n += 1
        self.net.update(rtt=None, down=0.0, up=0.0, ops=0.0)

    def net_info(self) -> dict:
        rtt = self.net["rtt"] if self.state == "connected" else None
        bars = 0 if rtt is None else 5 if rtt < 60 else 4 if rtt < 120 else 3 if rtt < 250 else 2 if rtt < 500 else 1
        st = self.fsstats
        busy = sorted(st["dirs"].items(), key=lambda kv: -kv[1])[:5]
        return {"state": self.state, "rtt": rtt, "bars": bars, "down": round(self.net["down"]), "up": round(self.net["up"]),
                "chats": self.chat_links(),
                "ops": round(self.net.get("ops", 0.0)), "files": {"n": st["n"], "hidden": st["hidden"],
                "ms": round(st["ms"] / st["n"], 2) if st["n"] else 0, "busy": [["/" + k, v] for k, v in busy]}}

    # Local only projects: their folders mounted on the server --------------------
    def local_projects(self) -> list:
        me = self.cfg()["id"]
        return [p for p in self.app.projects() if p.get("localOnly") == me and p.get("localExists") and p.get("remote")]

    def mount(self, names=None) -> dict:
        ps = [p for p in self.local_projects() if names is None or p["name"] in names]
        if not ps or self.app.web:
            return {}
        with self.mount_lock:
            return self._mount(ps)

    def _mount(self, ps) -> dict:
        try:
            r = self.remote(self.app.conf, PC_MOUNT_PY, ["mount"], multiplex=False, timeout=300,
                            stdin=json.dumps({"dev": self.cfg()["id"], "mounts": [{"name": p["name"], "path": p["remote"]} for p in ps]}).encode())
        except ApiError as e:
            r = {p["name"]: e.message for p in ps}
        self.mounts.update(r if isinstance(r, dict) else {})
        return r if isinstance(r, dict) else {}

    def ensure_mount(self, name: str, force=False) -> str:
        """Before a chat starts Claude in a Local only project: its folder must be mounted on the server
        (else `cd ~/.simab-mnt/x` fails, exit 96) — a new project, a mount still running or failed, a
        folder gone (force: Claude just stopped with 96/97). → "" or a plain reason for the user that
        says WHOSE problem it is (the link, the computer, the server). Not my project: ""."""
        if self.app.web or not any(p["name"] == name for p in self.local_projects()):
            return ""
        if self.state == "connecting":
            return ("This project lives on your computer, which is still connecting to the server — "
                    "wait a moment, then send again.")
        if self.state != "connected":
            why = f" ({self.error})" if self.error else ""
            return ("This project lives on your computer, which is not connected to the server" + why + ": "
                    "check its internet and that Simab Workspace is open there (⚙ → Computers → Connect this computer).")
        if self.mounts.get(name) == "ok" and not force:
            return ""
        why = self.mount([name]).get(name) or "mounting failed"
        if why == "ok":
            return ""
        if "sshfs" in why:
            return "The server cannot open folders of your computer: " + why
        if "not empty" in why:
            return "The server's side of this project is in the way: " + why
        return ("The link to your computer failed while opening the project folder (" + why + "): "
                "check its internet and that Simab Workspace is open there, then send again.")

    def unmount(self, projects=None):
        ps = projects if projects is not None else self.local_projects()
        if not ps:
            return
        try:
            self.remote(self.app.conf, PC_MOUNT_PY, ["umount"], multiplex=False, timeout=60,
                        stdin=json.dumps({"mounts": [{"name": p["name"], "path": p["remote"]} for p in ps]}).encode())
        except ApiError:
            pass
        for p in ps:
            self.mounts.pop(p["name"], None)

    def serve_sftp(self, h):
        """POST /pc/sftp from onpc --sftp (sshfs on the server): the files of one Local only project."""
        c = self.cfg()
        if self.app.web or not c["on"]:
            return h._json(403, {"error": "this computer is not connected"})
        if not hmac.compare_digest((h.headers.get("X-PC") or "").encode(), c["secret"].encode()):
            return h._json(403, {"error": "this computer does not know this server user"})
        b = h._body()
        p = next((x for x in self.local_projects() if x["name"] == b.get("project")), None)
        if not p:
            return h._json(404, {"error": "no such Local only project on this computer"})
        h.send_response(200)
        h.send_header("Connection", "close")
        h.end_headers()
        h.wfile.flush()
        h.close_connection = True
        SftpServer(p["local"], h.rfile, h.wfile, uid=b.get("uid"), gid=b.get("gid"), meter=self.meter, stats=self.fsstats).serve()

    def serve_fs(self, h):
        """POST /pc/fs {cwd, op: grep|glob, args} from onpc --mcp-serve: Claude's Grep / Glob of a Local
        only project, run on this computer → {text}; only the answer goes back over the internet."""
        c = self.cfg()
        if self.app.web or not c["on"]:
            return h._json(403, {"error": "this computer is not connected"})
        if not hmac.compare_digest((h.headers.get("X-PC") or "").encode(), c["secret"].encode()):
            return h._json(403, {"error": "this computer does not know this server user"})
        n = int(h.headers.get("Content-Length") or 0)          # a Write may carry a big file (not _body's 1 MB)
        if n > 30_000_000:
            return h._json(413, {"error": "over 30 MB — write it with Bash"})
        try:
            b = json.loads(h.rfile.read(n).decode("utf-8")) if n else {}
        except ValueError:
            return h._json(400, {"error": "bad request"})
        res = self.fs_do(b, c)
        return h._json(res.pop("_code", 200), res)

    def fs_do(self, b: dict, c: dict) -> dict:
        """One file request {cwd, op, args} of a Local only project → its answer ({text} / {image} /
        {error}; "_code" when it is not even a fair request)."""
        cwd, op, args = b.get("cwd"), b.get("op"), b.get("args")
        if not isinstance(cwd, str) or not cwd.startswith("/") or op not in PCFS_OPS or not isinstance(args, dict):
            return {"_code": 400, "error": "bad request"}
        hit = self.map_path(cwd)
        if not hit or not hit[0].get("localOnly"):
            return {"_code": 404, "error": f"{cwd} is not a Local only project of {c['name']}"}
        project, _, root = hit
        t0 = time.perf_counter()
        try:
            return PCFS_OPS[op](project["local"], root, args)
        except (OSError, ValueError) as e:
            return {"error": str(e)[:1000]}
        finally:
            self.fsstats["n"] += 1
            self.fsstats["ms"] += (time.perf_counter() - t0) * 1000

    def serve_fsmux(self, h):
        """POST /pc/fsmux from onpc --mcp-serve: ONE connection that stays open for the whole chat; on it
        frames (4-byte length + JSON) {id, cwd, op, args} → {id, …answer}, many at a time (each in its own
        thread) — one round trip per file request, no channel to open each time."""
        c = self.cfg()
        if self.app.web or not c["on"]:
            return h._json(403, {"error": "this computer is not connected"})
        if not hmac.compare_digest((h.headers.get("X-PC") or "").encode(), c["secret"].encode()):
            return h._json(403, {"error": "this computer does not know this server user"})
        key = h.headers.get("X-Chat") or ""
        key = key if RELAY_KEY_RE.fullmatch(key) else "?"
        h.send_response(200)
        h.send_header("Connection", "close")
        h.end_headers()
        h.wfile.flush()
        h.close_connection = True
        lock = threading.Lock()
        links = getattr(self, "chatlinks", None)
        link = links.setdefault(key, {"n": 0, "rtt": None, "at": 0.0}) if isinstance(links, dict) else {"n": 0}
        link["n"] += 1

        def one(b):
            res = self.fs_do(b, c)
            res.pop("_code", None)
            res["id"] = b.get("id")
            raw = json.dumps(res).encode()
            try:
                with lock:
                    h.wfile.write(struct.pack(">I", len(raw)) + raw)
                    h.wfile.flush()
                    self.meter[1] += len(raw) + 4
            except OSError:
                pass
        try:
            while True:
                head = h.rfile.read(4)
                if len(head) < 4:
                    return
                n = struct.unpack(">I", head)[0]
                if n > 30_000_000:
                    return
                raw = h.rfile.read(n)
                if len(raw) < n:
                    return
                self.meter[0] += n + 4
                try:
                    b = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                if isinstance(b, dict) and b.get("op") == "ping":       # heartbeat: answered at once
                    if isinstance(b.get("rtt"), (int, float)) and not isinstance(b.get("rtt"), bool):
                        link["rtt"], link["at"] = int(b["rtt"]), time.time()    # what the chat measured
                    pong = json.dumps({"id": b.get("id"), "pong": True}).encode()
                    with lock:
                        h.wfile.write(struct.pack(">I", len(pong)) + pong)
                        h.wfile.flush()
                elif isinstance(b, dict):
                    threading.Thread(target=one, args=(b,), daemon=True).start()
        except OSError:
            return
        finally:
            link["n"] -= 1

    def chat_links(self) -> dict:
        """Per chat (relay key): open connections + its own measured round trip; link-wide rtt as the
        fallback before a chat's first ping. Chats gone for 2 minutes are forgotten."""
        now, out = time.time(), {}
        for k, v in list(self.chatlinks.items()):
            if v["n"] <= 0 and now - v.get("at", 0) > 120:
                self.chatlinks.pop(k, None)
                continue
            fresh = v.get("rtt") is not None and now - v.get("at", 0) < 60
            out[k] = {"n": max(v["n"], 0), "rtt": v["rtt"] if fresh else None}
        runs = getattr(self, "runs", None)
        for r in list(runs) if isinstance(runs, (list, collections.deque)) else []:   # a running command is a line too
            if r.get("chat") and r.get("state") in ("running", "asking"):
                out.setdefault(r["chat"], {"n": 0, "rtt": None})["n"] += 1
        return out

    # dev servers (onpc --bg) -------------------------------------------------------
    def serve_bg(self, h):
        """POST /pc/bg {op: start|logs|stop|ps, name, cmd, cwd, chat} from onpc → JSON {text}."""
        c = self.cfg()
        if self.app.web or not c["on"]:
            return h._json(403, {"error": "builds on this computer are turned off (Settings → Computers)"})
        if not hmac.compare_digest((h.headers.get("X-PC") or "").encode(), c["secret"].encode()):
            return h._json(403, {"error": "this computer does not know this server user"})
        b = h._body()
        op, name = b.get("op"), b.get("name") or ""
        if op == "ps":
            rows = [f"{x['name']}  {x['state']}{' :' + str(x['code']) if x['state'] == 'exited' else ''}  {' '.join(x['urls']) or '-'}  {x['project']}: {x['cmd']}"
                    for x in self.bg.values()]
            return h._json(200, {"text": "\n".join(rows) or f"no dev servers on {c['name']}"})
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name):
            return h._json(400, {"error": "the name: letters, digits, . _ - (up to 40)"})
        x = self.bg.get(name)
        if op == "logs":
            if not x:
                return h._json(404, {"error": f"no dev server {name} on {c['name']}"})
            n = max(1, min(int(b.get("lines") or 80), 2000))
            head = f"[{name}: {x['state']}{' exit ' + str(x['code']) if x['state'] == 'exited' else ''}{' · ' + x['url'] if x['url'] else ''}]\n"
            return h._json(200, {"text": head + "\n".join(x["out"].splitlines()[-n:])})
        if op == "stop":
            if not x:
                return h._json(404, {"error": f"no dev server {name} on {c['name']}"})
            self.stop_bg(name)
            return h._json(200, {"text": f"stopped {name} on {c['name']}"})
        if op != "start":
            return h._json(400, {"error": "bad request"})
        cmd, cwd = b.get("cmd"), b.get("cwd")
        if not isinstance(cmd, str) or not cmd.strip() or not isinstance(cwd, str):
            return h._json(400, {"error": "bad request"})
        if x and x["state"] == "running":
            return h._json(409, {"error": f"{name} is already running on {c['name']} ({x['url'] or 'no URL yet'}) — onpc --stop {name} first"})
        hit = self.map_path(cwd)
        if not hit or not os.path.isdir(hit[1]):
            return h._json(404, {"error": f"{cwd} is not on {c['name']}"})
        project, local, root = hit
        chat = b.get("chat") if isinstance(b.get("chat"), str) and RELAY_KEY_RE.fullmatch(b.get("chat") or "") else ""
        paths = []
        if project.get("localOnly"):
            lroot = project["local"].replace("\\", "/") if IS_WIN else project["local"]
            cmd = cmd.replace(root, lroot)
            paths = [(project["local"], root), (lroot, root)]
        else:
            verdict, why = pc_decide(c, project["name"], cmd, self.hour, time.time())
            if verdict == "deny":
                return h._json(403, {"error": why})
            run = {"id": secrets.token_hex(6), "project": project["name"], "cmd": cmd.strip(), "cwd": local, "chat": chat,
                   "start": time.time(), "end": 0, "code": None, "state": "asking", "out": ""}
            if verdict == "ask":
                self.runs.appendleft(run)
                ok = self.ask(project["name"], "[dev server] " + cmd.strip(), run, why)
                run.update(state="running" if ok else "denied", end=time.time())
                if not ok:
                    return h._json(403, {"error": f"the user did not allow it on {c['name']} — it was NOT started"})
        if project.get("source") == "mutagen":
            mutagen_flush(project["name"])
        x = self.start_bg(name, project["name"], cmd.strip(), local, chat, paths)
        end = time.time() + 15
        while time.time() < end and x["state"] == "running" and not x["url"]:
            time.sleep(0.2)
        first = "\n".join(x["out"].splitlines()[-40:])
        if x["state"] != "running":
            return h._json(422, {"error": f"{name} stopped at once (exit {x['code']}) on {c['name']}:\n{first}"})
        return h._json(200, {"text": f"started {name} on {c['name']} — keeps running (onpc --logs {name}, onpc --stop {name})\n"
                                     + (f"URL: {' '.join(x['urls'])}\n" if x["urls"] else "no URL seen yet\n") + "--- output so far ---\n" + first})

    def start_bg(self, name, project, cmd, cwd, chat="", paths=()) -> dict:
        kw = {"creationflags": 0x00000200 | 0x08000000} if IS_WIN else {"start_new_session": True}
        p = subprocess.Popen(pc_shell_argv(cmd), cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env=dict(os.environ, PATH=tool_path()), **kw)
        x = {"name": name, "project": project, "cmd": cmd, "pid": p.pid, "start": time.time(), "url": "", "urls": [],
             "state": "running", "code": None, "out": "", "chat": chat, "_p": p}
        self.bg[name] = x

        def read():
            ansi = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
            for chunk in iter(lambda: p.stdout.read1(65536), b""):
                t = ansi.sub("", chunk.decode("utf-8", "replace"))
                for a, b in paths:
                    t = t.replace(a, b)
                x["out"] = (x["out"] + t)[-200000:]
                if len(x["urls"]) < 8:
                    x["urls"] = pc_urls(x["out"][-(len(t) + 300):], x["urls"])
                    x["url"] = x["urls"][0] if x["urls"] else ""
            code = p.wait()
            if x["state"] == "running":
                x["state"], x["code"] = "exited", code
            try:
                p.stdout.close()
            except OSError:
                pass
        threading.Thread(target=read, daemon=True).start()
        return x

    def stop_bg(self, name=None):
        for n in ([name] if name else list(self.bg)):
            x = self.bg.get(n)
            if x and x["state"] == "running":
                x["state"] = "stopped"
                pc_kill(x["_p"])

    def stop_port(self, port: int) -> dict:
        """Stop what listens on a port here (a shared port): its dev server if we started it, else the program."""
        mine = [x for x in self.bg.values() if x["state"] == "running" and any(pc_port(u) == port for u in x.get("urls") or [])]
        for x in mine:
            self.stop_bg(x["name"])
        pids = port_pids(port)
        if not pids and not mine:
            raise ApiError(404, f"nothing runs on :{port} here")
        port_kill(pids)
        return {"ok": True, "port": port, "bg": [x["name"] for x in mine]}

    def bg_list(self) -> list:
        return [{k: v for k, v in x.items() if k not in ("_p", "out")} for x in list(self.bg.values())]

    def share_allowed(self, project: str, port: int) -> bool:
        """Only ports allowed HERE (asked once, "always" remembered per project): the secret alone, which
        anything on the server can read, must not open every port of this computer. Many requests of
        the first page wait for the one question."""
        cmd = f"share port {port} on the internet"
        now = time.time()
        ok = self.__dict__.setdefault("share_ok", {})      # (project, port) -> allowed until: "once" / "1 hour" = 1 h
        if cmd in (self.cfg()["always"].get(project) or []) or ok.get((project, port), 0) > now or self.hour.get(project, 0) > now:
            return True
        with self.lock:
            waits = self.__dict__.setdefault("share_waits", {})
            w = waits.get((project, port))
            mine = w is None
            if mine:
                w = waits[(project, port)] = {"ev": threading.Event(), "ok": False}
        if not mine:
            w["ev"].wait(PC_ASK_WAIT + 5)
            return w["ok"]
        try:
            w["ok"] = self.ask(project, cmd, {"id": "share", "chat": ""})
            if w["ok"]:                     # a page is many requests: never ask again for each of them
                ok[(project, port)] = time.time() + 3600
            self.audit("share", f"port {port} of {project} " + ("allowed" if w["ok"] else "refused") + " for the web")
        finally:
            w["ev"].set()
            with self.lock:
                waits.pop((project, port), None)
        return w["ok"]

    def serve_probe(self, h):
        """POST /pc/probe {ports} from the web version: which of these ports run here now (no data passes)."""
        c = self.cfg()
        if self.app.web or not c["on"]:
            return h._json(403, {"error": "this computer is not connected"})
        if not hmac.compare_digest((h.headers.get("X-PC") or "").encode(), c["secret"].encode()):
            return h._json(403, {"error": "this computer does not know this server user"})
        ports = [p for p in (h._body().get("ports") or [])[:SHARE_MAX] if isinstance(p, int) and 1 <= p <= 65535]
        return h._json(200, {"open": [p for p in ports if port_open(p)]})

    def serve_guest(self, h):
        """POST /pc/guest from the web version: {op: ask, id, text, until} waits for the answer of this
        computer's user (or the web's: {op: drop, id}) → {answer: hour | day | always | deny | gone}."""
        c = self.cfg()
        if self.app.web or not c["on"]:
            return h._json(403, {"error": "this computer is not connected"})
        if not hmac.compare_digest((h.headers.get("X-PC") or "").encode(), c["secret"].encode()):
            return h._json(403, {"error": "this computer does not know this server user"})
        b = h._body()
        gid = b.get("id") if isinstance(b.get("id"), str) and re.fullmatch(r"[0-9a-f]{12}", b.get("id") or "") else ""
        if not gid:
            return h._json(400, {"error": "bad request"})
        if b.get("op") == "drop":
            self.guest_answer(gid, "gone")
            return h._json(200, {"ok": True})
        until = min(float(b.get("until") or 0), time.time() + SHARE_ASK_WAIT)
        g = {"id": gid, "text": str(b.get("text") or "")[:300], "until": until, "answer": "", "_ev": threading.Event()}
        with self.lock:
            self.guests[gid] = g
        g["_ev"].wait(max(0.0, until - time.time()))
        with self.lock:
            self.guests.pop(gid, None)
        if g["answer"] in SHARE_ANSWERS:
            self.audit("guest", f"{g['answer']}: {g['text']}")
        return h._json(200, {"answer": g["answer"] or "gone"})

    def guest_answer(self, gid, ans) -> dict:
        if ans not in SHARE_ANSWERS + ("gone",):
            raise ApiError(400, "invalid answer")
        with self.lock:
            g = self.guests.get(gid) if isinstance(gid, str) else None
            if not g:
                raise ApiError(404, "this question is no longer open")
            if not g["answer"]:
                g["answer"] = ans
                g["_ev"].set()
        return {"ok": True}

    def share_revoke(self, gid: str):
        """Revoke from this computer's app: refused here at once, its open connections closed."""
        if not re.fullmatch(r"[0-9a-f]{16}", gid or ""):
            raise ApiError(400, "bad request")
        with self.lock:
            c = self.cfg()
            c["shareRevoked"] = ([g for g in (c.get("shareRevoked") or []) if g != gid] + [gid])[-500:]
            self.app.store.db.put("pc", c)
            conns = list(self.__dict__.setdefault("share_conns", {}).pop(gid, set()))
        for x in conns:
            try:
                x.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def serve_proxy(self, h):
        """POST /pc/proxy {port} from the web version (Share on your domain): after the "200", the bytes of
        one connection to port PORT of this computer (127.0.0.1, else ::1) go both ways."""
        c = self.cfg()
        if self.app.web or not c["on"]:
            return h._json(403, {"error": "this computer is not connected"})
        if not hmac.compare_digest((h.headers.get("X-PC") or "").encode(), c["secret"].encode()):
            return h._json(403, {"error": "this computer does not know this server user"})
        b = h._body()
        try:
            port = int(b.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        project = b.get("project") if isinstance(b.get("project"), str) else ""
        if not 1 <= port <= 65535 or port == self.app.port or not project:
            return h._json(400, {"error": "bad port"})
        guest = b.get("guest") if isinstance(b.get("guest"), str) and re.fullmatch(r"[0-9a-f]{16}", b.get("guest") or "") else ""
        if guest and guest in (c.get("shareRevoked") or []):          # taken back here: refused even if the web still lets it in
            return h._json(403, {"error": "your access to this link was taken back"})
        if not self.share_allowed(project, port):
            return h._json(403, {"error": f"port {port} was not allowed on {c['name'] or 'your computer'}"})
        up = None
        for fam, addr in ((socket.AF_INET, ("127.0.0.1", port)), (socket.AF_INET6, ("::1", port, 0, 0))):
            try:
                s = socket.socket(fam, socket.SOCK_STREAM)
            except OSError:              # no IPv6 here
                continue
            s.settimeout(3)
            try:
                s.connect(addr)
                up = s
                break
            except OSError:
                s.close()
        if up is None:
            return h._json(502, {"error": f"nothing is running on port {port} of {c['name'] or 'your computer'}"})
        up.settimeout(None)
        h.send_response(200)
        h.send_header("Connection", "close")
        h.end_headers()
        h.wfile.flush()
        h.close_connection = True
        if guest:
            with self.lock:
                self.__dict__.setdefault("share_conns", {}).setdefault(guest, set()).update({h.connection, up})
        try:
            share_pipe(h.rfile, h.connection, up)
        finally:
            if guest:
                with self.lock:
                    self.__dict__.setdefault("share_conns", {}).get(guest, set()).difference_update({h.connection, up})

    def serve_term(self, h):
        """POST /pc/term {project, cols, rows} from onpc --term: a shell here in that project's folder."""
        c = self.cfg()
        if self.app.web or not c["on"]:
            return h._json(403, {"error": "this computer is not connected"})
        if not hmac.compare_digest((h.headers.get("X-PC") or "").encode(), c["secret"].encode()):
            return h._json(403, {"error": "this computer does not know this server user"})
        b = h._body()
        cwd = os.path.expanduser("~")
        if b.get("project"):
            p = next((x for x in self.app.projects() if x["name"] == b.get("project") and x.get("localExists")), None)
            if not p:
                return h._json(404, {"error": "this project is not on " + c["name"]})
            cwd = p["local"]
        u = self.take_ticket(b.get("ticket"), b.get("project") or "")
        if not u:                       # only a terminal the phone / web unlocked with this computer's MFA code
            self.audit("term-refused", "terminal without an unlock" + (" in " + b["project"] if b.get("project") else ""))
            return h._json(403, {"error": f"unlock {c['name']} with your MFA code first (Computers in the web version)"})
        cols = max(20, min(int(b.get("cols") or 80), 500))
        rows = max(5, min(int(b.get("rows") or 24), 300))
        argv = local_shell_argv(local_shell(self.app.store.settings))
        env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor", PATH=tool_path())
        if IS_WIN:
            pt = ConPty(argv, cwd, env, cols, rows)
            proc, rd, wr, rs = pt.proc, pt.read, pt.write, pt.resize
        else:
            master, slave = os.openpty()
            set_winsize(master, rows, cols)
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
                                    start_new_session=True, preexec_fn=_child_setup, close_fds=True)
            os.close(slave)
            rd = lambda n=65536: os.read(master, n)
            wr = lambda d: os.write(master, d)
            rs = lambda c2, r2: set_winsize(master, r2, c2)
        tid = secrets.token_hex(6)
        with self.lock:
            self.terms[tid] = {"id": tid, "unlock": u["id"], "project": b.get("project") or "", "at": time.time(), "by": u["by"], "proc": proc}
        self.audit("term", "terminal opened in " + (b.get("project") or "home"), u["by"])
        h.send_response(200)
        h.send_header("Connection", "close")
        h.end_headers()
        h.wfile.flush()
        h.close_connection = True

        def out():
            while True:
                try:
                    d = rd(65536)
                except OSError:
                    d = b""
                if not d:
                    break
                try:
                    h.wfile.write(d)
                    h.wfile.flush()
                    self.meter[1] += len(d)
                except OSError:
                    break
            try:
                h.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        threading.Thread(target=out, daemon=True).start()
        r = h.rfile
        try:
            while proc.poll() is None:
                k = r.read(1)
                if not k:
                    break
                if k == b"d":
                    n = struct.unpack(">I", r.read(4))[0]
                    d = r.read(n)
                    self.meter[0] += n
                    wr(d)
                elif k == b"r":
                    c2, r2 = struct.unpack(">HH", r.read(4))
                    rs(max(20, c2), max(5, r2))
        except (OSError, struct.error):
            pass
        finally:
            if proc.poll() is None:
                pc_kill(proc)
            with self.lock:
                self.terms.pop(tid, None)
            self.audit("term-end", "terminal closed in " + (b.get("project") or "home"), u["by"])
            if not IS_WIN:
                try:
                    os.close(master)
                except OSError:
                    pass

    def serve_remote(self, h):
        """POST /pc/remote {op: asks | answer} from PC_BRIDGE_PY: the web version / the phone."""
        c = self.cfg()
        if self.app.web or not c["on"]:
            return h._json(403, {"error": "this computer is not connected"})
        if not hmac.compare_digest((h.headers.get("X-PC") or "").encode(), c["secret"].encode()):
            return h._json(403, {"error": "this computer does not know this server user"})
        b = h._body()
        toks = b.get("toks") if isinstance(b.get("toks"), dict) else {}
        u = self.unlocked(toks.get(c["id"]))          # this computer's MFA unlock from that web workspace
        if b.get("op") == "asks":
            return h._json(200, {"asks": self.pending()["asks"], "bg": self.bg_list()})
        if b.get("op") == "unlock":
            try:
                return h._json(200, self.unlock(b.get("code"), str(b.get("by") or "web")))
            except ApiError as e:
                return h._json(e.status, {"error": e.message})
        if b.get("op") == "lock":
            if u:
                self.lock_out(u["id"], "locked from " + u["by"])
            return h._json(200, {"ok": True})
        if b.get("op") == "ticket":                # one terminal (onpc --term) for this unlock
            if b.get("project") and not any(x["name"] == b["project"] and x.get("localExists") for x in self.app.projects()):
                return h._json(404, {"error": "this project is not on " + c["name"]})
            if not u:
                return h._json(403, {"error": f"unlock {c['name']} with your MFA code first", "locked": True})
            return h._json(200, {"ticket": self.ticket(u, str(b.get("project") or "")), "dev": c["id"]})
        if b.get("op") == "run":                 # a run command of a project here, started from the phone / web
            p = next((x for x in self.app.projects() if x["name"] == b.get("project") and x.get("localExists")), None)
            if not p:
                return h._json(404, {"error": "this project is not on " + c["name"]})
            cmd = self.app.command(p["name"], b.get("id") or "")
            if not cmd:
                return h._json(404, {"error": "this run command is not on " + c["name"]})
            cwd = os.path.join(p["local"], *[x for x in (cmd.get("cwd") or "").split("/") if x and x != ".."])
            if not os.path.isdir(cwd):
                return h._json(404, {"error": f"{cwd} is not on {c['name']}"})
            name = "run-" + cmd["id"]
            x = self.bg.get(name)
            if x and x["state"] == "running":
                return h._json(409, {"error": cmd["name"] + " is already running on " + c["name"]})
            if not u:
                return h._json(403, {"error": f"unlock {c['name']} with your MFA code first", "locked": True})
            self.start_bg(name, p["name"], cmd["cmd"], cwd)
            self.bg[name].update(label=cmd["name"], unlock=u["id"])
            self.audit("run", f"{p['name']}: {cmd['name']} — {cmd['cmd']}", u["by"])
            return h._json(200, {"name": name, "dev": c["id"]})
        if b.get("op") == "log":
            x = self.bg.get(b.get("name") or "")
            if not x:
                return h._json(404, {"error": "not running here"})
            return h._json(200, {"name": x["name"], "state": x["state"], "code": x["code"], "url": x["url"], "out": x["out"][-60000:]})
        if b.get("op") == "stop":
            if b.get("name") not in self.bg:
                return h._json(404, {"error": "not running here"})
            self.stop_bg(b["name"])
            return h._json(200, {"ok": True})
        if b.get("op") == "portstop":            # a shared port's program, stopped from the phone / web
            port = clean_port(b.get("port"), "port")
            if not port_pids(port) and not any(x["state"] == "running" and any(pc_port(v) == port for v in x.get("urls") or []) for x in self.bg.values()):
                return h._json(404, {"error": f"nothing runs on :{port} on {c['name']}"})
            if not u:
                return h._json(403, {"error": f"unlock {c['name']} with your MFA code first", "locked": True})
            r = self.stop_port(port)
            self.audit("stop", f"port {port}", u["by"])
            return h._json(200, dict(r, dev=c["id"]))
        if b.get("op") == "answer":
            if b.get("answer") != "deny" and not u:    # deny is always fine; allowing needs the unlock
                return h._json(403, {"error": f"unlock {c['name']} with your MFA code to allow it from here", "locked": True})
            a = self.asks.get(b.get("id")) if isinstance(b.get("id"), str) else None
            if a and u:
                self.audit("answer", f"{b.get('answer')}: {a['project']}: {a['cmd']}", u["by"])
            try:
                return h._json(200, self.answer(b.get("id"), b.get("answer")))
            except ApiError as e:
                return h._json(e.status, {"error": e.message})
        return h._json(400, {"error": "bad request"})

    def _verify(self) -> str:
        """"" when the server reaches this computer through its socket, else why not."""
        try:
            r = self.remote(self.app.conf, PC_ADMIN_PY, ["list"], multiplex=False, timeout=20, stdin=b"{}")
        except ApiError as e:
            return e.message
        me = self.cfg()["id"]
        d = next((x for x in (r or {}).get("devices") or [] if x.get("id") == me), None)
        if d and d.get("online"):
            return ""
        return "the server cannot reach this computer through the link (its socket was not opened)"

    # a command from the server ------------------------------------------------
    def map_path(self, cwd: str):
        """Server folder → (project, folder on this computer) or None."""
        best = None
        host = self.app.conf.host
        for p in self.app.projects():
            ep = p.get("endpoint") or {}
            if not p.get("localExists") or not p.get("remote") or (ep.get("host") and ep.get("host") != host):
                continue
            r = p["remote"]
            roots = {self.real.get(p["name"]) or "",
                     (self.home + r[1:]) if self.home and (r == "~" or r.startswith("~/")) else r}
            for root in roots:
                root = root.rstrip("/") or "/"
                if root.startswith("/") and (cwd == root or cwd.startswith(root + "/")) and (best is None or len(root) > len(best[1])):
                    best = (p, root)
        if not best:
            return None
        rest = [x for x in cwd[len(best[1]):].split("/") if x]
        if any(x == ".." for x in rest):
            return None
        return best[0], os.path.join(best[0]["local"], *rest) if rest else best[0]["local"], best[1]

    def serve_run(self, h):
        """POST /pc/run from onpc (through the ssh link). h = the HTTP handler."""
        c = self.cfg()
        if self.app.web or not c["on"]:
            return h._json(403, {"error": "builds on this computer are turned off (Settings → Computers)"})
        if not hmac.compare_digest((h.headers.get("X-PC") or "").encode(), c["secret"].encode()):
            return h._json(403, {"error": "this computer does not know this server user"})
        b = h._body()
        cwd, cmd = b.get("cwd"), b.get("cmd")
        if not isinstance(cwd, str) or not cwd.startswith("/") or not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 20000:
            return h._json(400, {"error": "bad request"})
        hit = self.map_path(cwd)
        if not hit:
            return h._json(404, {"error": f"{cwd} is not synced to {c['name']}"})
        project, local, root = hit
        if not os.path.isdir(local):
            return h._json(404, {"error": f"{cwd} is not on {c['name']} yet (sync not finished?)"})
        paths = []
        verdict, why = pc_decide(c, project["name"], cmd, self.hour, time.time())
        if project.get("localOnly") or b.get("hooked") is True:
            # Local only / My computers: the hook sends every Bash command here; the server's paths mean
            # this computer's folder, and back in the output. Claude's own modes decide ("same as chat")
            # only for projects marked HERE — "hooked" comes from the server, anyone there can send it.
            lroot = project["local"].replace("\\", "/") if IS_WIN else project["local"]
            for sv in {root, (self.home + project["remote"][1:]) if self.home and project["remote"].startswith("~/") else ""} - {""}:
                cmd = cmd.replace(sv, lroot)
            paths = [(project["local"].encode(), root.encode()), (lroot.encode(), root.encode())]
            if c.get("mode") == "chat" and (project.get("localOnly") or project["name"] in (c.get("trusted") or [])):
                why = pc_guard(c, project["name"], cmd)
                verdict = "deny" if why else "allow"
                if verdict == "allow":
                    why = pc_denied(c, cmd)
                    verdict = "ask" if why else "allow"
            else:
                verdict, why = pc_decide(c, project["name"], cmd, self.hour, time.time())
        chat = b.get("chat") if isinstance(b.get("chat"), str) and RELAY_KEY_RE.fullmatch(b.get("chat") or "") else ""
        run = {"id": secrets.token_hex(6), "project": project["name"], "cmd": cmd.strip(), "cwd": local, "chat": chat,
               "start": time.time(), "end": 0, "code": None, "state": "asking" if verdict == "ask" else "running", "out": ""}
        if verdict == "deny":
            run.update(state="denied", end=time.time(), out=why)
            self.runs.appendleft(run)
            return h._json(403, {"error": why})
        self.runs.appendleft(run)
        h.send_response(200)
        h.send_header("Content-Type", "application/octet-stream")
        h.send_header("Connection", "close")
        h.end_headers()
        h.close_connection = True
        wlock = threading.Lock()

        def frame(kind: bytes, data=b"", code=None):
            self.meter[1] += len(data) + 5
            with wlock:
                if code is not None:
                    h.wfile.write(kind + struct.pack(">i", code))
                else:
                    h.wfile.write(kind + struct.pack(">i", len(data)) + data)
                h.wfile.flush()
        try:
            if verdict == "ask":
                frame(b"e", f"onpc: waiting for the user to approve it on {c['name']}…\n".encode())
                if not self.ask(project["name"], cmd.strip(), run, why):
                    run.update(state="denied", end=time.time(), out="denied by the user")
                    frame(b"e", f"onpc: the user did not allow this command on {c['name']} — it was NOT run.\n".encode())
                    frame(b"x", code=126)
                    return
            if pc_hung_up(h.connection):           # Claude stopped onpc while the user was asked
                run.update(state="stopped", end=time.time())
                return
            run["state"] = "running"
            if project.get("source") == "mutagen":
                mutagen_flush(project["name"])      # Claude's last edits are here before the build
            code = self._exec(cmd, local, frame, run, h.connection, paths)
            if project.get("source") == "mutagen":
                mutagen_flush(project["name"])
            run.update(state="done", end=time.time(), code=code)
            frame(b"x", code=code)
        except OSError:                            # onpc went away
            if run["state"] != "done":
                run.update(state="stopped", end=time.time())

    def _exec(self, cmd: str, cwd: str, frame, run: dict, conn, paths=()) -> int:
        import select
        kw = {"creationflags": 0x00000200 | 0x08000000} if IS_WIN else {"start_new_session": True}
        p = subprocess.Popen(pc_shell_argv(cmd), cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=dict(os.environ, PATH=tool_path()), **kw)
        gone = threading.Event()

        def pump(stream, kind):
            for chunk in iter(lambda: stream.read1(65536) if hasattr(stream, "read1") else stream.read(4096), b""):
                for a, b in paths:
                    chunk = chunk.replace(a, b)
                run["out"] = (run["out"] + chunk.decode("utf-8", "replace"))[-65536:]
                if not gone.is_set():
                    try:
                        frame(kind, chunk)
                    except OSError:
                        gone.set()
        ts = [threading.Thread(target=pump, args=(p.stdout, b"o"), daemon=True),
              threading.Thread(target=pump, args=(p.stderr, b"e"), daemon=True)]
        for t in ts:
            t.start()
        while p.poll() is None and not gone.is_set():
            try:                                   # onpc closed (Claude stopped it) → stop the command
                r, _, _ = select.select([conn], [], [], 0.5)
                if r and not conn.recv(1, socket.MSG_PEEK):
                    gone.set()
            except (OSError, ValueError):
                gone.set()
        if gone.is_set() and p.poll() is None:
            pc_kill(p)
        code = p.wait()
        for t in ts:
            t.join(5)
        for f in (p.stdout, p.stderr):
            try:
                f.close()
            except OSError:
                pass
        if gone.is_set():
            raise OSError("onpc went away")
        return code

    def ask(self, project: str, cmd: str, run: dict, warn: str = "") -> bool:
        """warn = why it is on the deny list: the window never answers it on its own, only Once / Deny."""
        a = {"id": secrets.token_hex(6), "project": project, "cmd": cmd, "at": time.time(), "chat": run.get("chat") or "",
             "warn": warn,
             "until": time.time() + PC_ASK_WAIT, "answer": "", "_ev": threading.Event(), "run": run["id"]}
        with self.lock:
            self.asks[a["id"]] = a
        a["_ev"].wait(PC_ASK_WAIT)
        with self.lock:
            self.asks.pop(a["id"], None)
        ans = a["answer"] or "deny"
        if warn and ans != "deny":
            ans = "once"                     # a deny-list command is never remembered
        if ans == "always":
            with self.lock:
                c = self.cfg()
                lst = c["always"].setdefault(project, [])
                if cmd not in lst:
                    lst.append(cmd)
                self.app.store.db.put("pc", c)
        elif ans == "hour":
            self.hour[project] = time.time() + 3600
        return ans != "deny"

    # phone / web access: unlocked with THIS computer's own MFA code ------------------
    # The server only carries the code and the token; the key and the check stay here, so nothing
    # on the server (Claude in a chat included) can open a terminal or answer for you on its own.
    def mfa_setup(self, name: str = "") -> dict:
        """Add a device (phone, tablet…): a new key of its own; it counts after its first code (mfa_add)."""
        name = re.sub(r"\s+", " ", str(name or "")).strip()[:40] or "Phone"
        with self.lock:
            c = self.cfg()
            c["mfaNew"] = {"key": base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="), "name": name, "at": time.time()}
            self.app.store.db.put("pc", c)
        key = c["mfaNew"]["key"]
        uri = ("otpauth://totp/" + urllib.parse.quote("Simab " + c["name"] + ":" + name) + "?secret=" + key
               + "&issuer=Simab%20Workspace")
        return {"secret": key, "uri": uri}

    def mfa_check(self, code, at: float = None):
        """The device whose code this is (each code works once) or None."""
        code = str(code or "").replace(" ", "")
        if not re.fullmatch(r"\d{6}", code):
            return None
        step = int((at or time.time()) // 30)
        with self.lock:
            c = self.cfg()
            for d in c["mfaDevs"]:
                for st in (step, step - 1, step + 1):
                    if st > int(d.get("step") or 0) and hmac.compare_digest(totp_code(d["key"], st), code):
                        d["step"], d["last"] = st, time.time()
                        self.app.store.db.put("pc", c)
                        return dict(d)
        return None

    def mfa_add(self, code) -> dict:
        """The device being added counts once its app shows the right code."""
        code = str(code or "").replace(" ", "")
        step = int(time.time() // 30)
        with self.lock:
            c = self.cfg()
            n = c.get("mfaNew") if isinstance(c.get("mfaNew"), dict) else None
            if not n:
                raise ApiError(409, "start again: Add a device")
            ok = next((st for st in (step, step - 1, step + 1) if hmac.compare_digest(totp_code(n["key"], st), code)), None)
            if ok is None:
                raise ApiError(400, "wrong code")
            c["mfaDevs"] = c["mfaDevs"] + [{"id": secrets.token_hex(6), "name": n["name"], "key": n["key"], "step": ok,
                                            "at": time.time(), "last": 0}]
            c["mfaNew"] = None
            self.app.store.db.put("pc", c)
        self.audit("mfa", "device added: " + n["name"])
        return self.remote_access()

    def mfa_remove(self, did) -> dict:
        """Remove one device ("all" = phone access off): asked here only, so no code — it only takes access away."""
        with self.lock:
            c = self.cfg()
            gone = [d for d in c["mfaDevs"] if did == "all" or d["id"] == did]
            if not gone:
                raise ApiError(404, "no such device")
            c["mfaDevs"] = [d for d in c["mfaDevs"] if d not in gone]
            c["mfaNew"] = None if did == "all" else c.get("mfaNew")
            ids = {d["id"] for d in gone}
            self.app.store.db.put("pc", c)
            uids = [u["id"] for u in self.unlocks.values() if did == "all" or u.get("dev") in ids]
        for uid in uids:
            self.lock_out(uid, "device removed")
        self.audit("mfa", "phone access off" if did == "all" else "device removed: " + gone[0]["name"])
        return self.remote_access()

    def unlock(self, code, by: str) -> dict:
        now = time.time()
        with self.lock:
            while self.mfa_fails and now - self.mfa_fails[0] > 900:
                self.mfa_fails.popleft()
            if len(self.mfa_fails) >= PC_MFA_FAILS:
                raise ApiError(429, "too many wrong codes — wait 15 minutes")
        c = self.cfg()
        if not c.get("mfaOn"):
            raise ApiError(403, f"phone access is off on {c['name']} — turn it on there (Computers → Phone access)")
        by = (by or "web")[:80]
        d = self.mfa_check(code)
        if not d:
            with self.lock:
                self.mfa_fails.append(now)
            self.audit("unlock-failed", "wrong code", by)
            raise ApiError(403, "wrong code — check the time on your phone and try the newest code")
        tok = secrets.token_urlsafe(32)
        by = (d["name"] + " · " + by)[:120]
        u = {"id": secrets.token_hex(6), "hash": hashlib.sha256(tok.encode()).hexdigest(), "by": by, "dev": d["id"], "at": now,
             "until": now + PC_UNLOCK_TTL}
        with self.lock:
            self.unlocks[u["id"]] = u
        self.audit("unlock", "unlocked for 1 hour", by)
        return {"token": tok, "until": u["until"], "id": u["id"]}

    def unlocked(self, tok):
        """The unlock this token belongs to (still valid) or None."""
        if not isinstance(tok, str) or not tok:
            return None
        h, now = hashlib.sha256(tok.encode()).hexdigest(), time.time()
        with self.lock:
            for k in [k for k, u in self.unlocks.items() if u["until"] < now]:
                del self.unlocks[k]
            return next((u for u in self.unlocks.values() if hmac.compare_digest(u["hash"], h)), None)

    def ticket(self, u: dict, project: str) -> str:
        t = secrets.token_urlsafe(24)
        with self.lock:
            now = time.time()
            for k in [k for k, x in self.tickets.items() if x["until"] < now]:
                del self.tickets[k]
            self.tickets[hashlib.sha256(t.encode()).hexdigest()] = {"unlock": u["id"], "project": project, "until": now + PC_TICKET_TTL}
        return t

    def take_ticket(self, t, project: str):
        """The unlock of a valid one-time terminal ticket (used up now) or None."""
        if not isinstance(t, str) or not t:
            return None
        with self.lock:
            x = self.tickets.pop(hashlib.sha256(t.encode()).hexdigest(), None)
            u = self.unlocks.get(x["unlock"]) if x else None
        if not x or x["until"] < time.time() or x["project"] != project or not u or u["until"] < time.time():
            return None
        return u

    def lock_out(self, uid: str, why: str = "disconnected here") -> dict:
        """End an unlock (or "all"): its terminals close, its run commands stop."""
        with self.lock:
            gone = [u for k, u in self.unlocks.items() if uid == "all" or k == uid]
            ids = {u["id"] for u in gone}
            for u in gone:
                self.unlocks.pop(u["id"], None)
            for k in [k for k, x in self.tickets.items() if x["unlock"] in ids]:
                del self.tickets[k]
            terms = [t for t in self.terms.values() if uid == "all" or t["unlock"] in ids]
            runs = [n for n, x in self.bg.items() if x.get("unlock") and (uid == "all" or x["unlock"] in ids) and x["state"] == "running"]
        for t in terms:
            pc_kill(t.get("proc"))
        for n in runs:
            self.stop_bg(n)
        for u in gone:
            self.audit("lock", why, u["by"])
        return self.remote_access()

    def audit(self, kind: str, text: str, by: str = ""):
        """What was done here from the server / the phone (kept, newest last, 300)."""
        with self.lock:
            log = self.app.store.db.get("pcAudit", []) or []
            log = (log if isinstance(log, list) else [])[-299:] + [{"t": time.time(), "kind": kind, "text": str(text)[:300], "by": by[:80]}]
            self.app.store.db.put("pcAudit", log)

    def remote_access(self) -> dict:
        """Computers → Phone access: who is in now (unlocks, terminals, run commands) and the log."""
        self.unlocked("-")                                   # drops the expired ones
        with self.lock:
            unl = [{k: u.get(k) for k in ("id", "by", "dev", "at", "until")} for u in self.unlocks.values()]
            terms = [{k: t[k] for k in ("id", "unlock", "project", "at", "by")} for t in self.terms.values()]
            runs = [{"name": n, "project": x["project"], "cmd": x["cmd"], "unlock": x["unlock"], "start": x.get("start", 0)}
                    for n, x in self.bg.items() if x.get("unlock") and x["state"] == "running"]
        log = self.app.store.db.get("pcAudit", []) or []
        c = self.cfg()
        devs = [{k: d.get(k) for k in ("id", "name", "at", "last")} for d in c["mfaDevs"]]
        return {"mfa": c["mfaOn"], "devices": devs, "unlocks": unl, "terms": terms, "runs": runs,
                "log": list(reversed(log if isinstance(log, list) else []))[:100], "now": time.time()}

    def answer(self, aid, ans) -> dict:
        if ans not in PC_ANSWERS:
            raise ApiError(400, "invalid answer")
        with self.lock:
            a = self.asks.get(aid) if isinstance(aid, str) else None
            if not a:
                raise ApiError(404, "this question is no longer open")
            if not a["answer"]:
                a["answer"] = ans
                a["_ev"].set()
        return {"ok": True}

    def pending(self) -> dict:
        with self.lock:
            asks = [{k: v for k, v in a.items() if not k.startswith("_")} for a in self.asks.values() if not a["answer"]]
            guests = [{k: v for k, v in g.items() if not k.startswith("_")} for g in self.guests.values() if not g["answer"]]
        tabs = {(x.spec or {}).get("relay"): sid for sid, x in list(self.app.sessions.items()) if (x.spec or {}).get("relay")}
        for a in asks:
            a["session"] = tabs.get(a["chat"], "") if a["chat"] else ""     # the chat tab that ran onpc (asked only there)
        runs = [{k: v for k, v in r.items() if k != "out"} for r in list(self.runs)]
        for r in runs:
            r["session"] = tabs.get(r.get("chat") or "", "") if r.get("chat") else ""   # its output shows in that chat
        c = self.cfg()
        return {"asks": asks, "guests": guests, "runs": runs, "now": time.time(), "output": c["output"], "mode": c["mode"], "autoOpen": c["autoOpen"],
                "bg": self.bg_list(), "link": self.state}

    def run_info(self, rid: str) -> dict:
        for r in list(self.runs):
            if r["id"] == rid:
                return dict(r)
        raise ApiError(404, "no such command")


PC_URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\])(?::\d+)?[^\s'\"<>]*")


def pc_port(url: str) -> int:
    """The port of a dev server's printed URL (http://localhost:5173/ → 5173)."""
    m = re.match(r"(https?)://[^/:]+(?::(\d+))?", url or "")
    return int(m.group(2)) if m and m.group(2) else 443 if m and m.group(1) == "https" else 80 if m else 0


def pc_urls(text: str, seen=()) -> list:
    """Every local address a dev server printed, one per port (a project may serve an API and a panel);
    SEEN: the ones found before (only the new output is searched again)."""
    def port(u):
        m = re.search(r"://[^/]*:(\d+)", u)
        return m.group(1) if m else ("443" if u.startswith("https") else "80")
    out = list(seen)
    ports = {port(u) for u in out}
    for m in PC_URL_RE.finditer(text):
        u = m.group(0).rstrip("/.,)").replace("0.0.0.0", "localhost").replace("[::]", "localhost")
        if port(u) not in ports:
            ports.add(port(u))
            out.append(u)
            if len(out) >= 8:
                break
    return out


def pc_rtt(host: str, port) -> float:
    """Milliseconds for a TCP handshake with the server's ssh port (None = not reachable)."""
    t = time.time()
    try:
        with socket.create_connection((host, int(port or 22)), timeout=3):
            return round((time.time() - t) * 1000, 1)
    except (OSError, ValueError):
        return None


def ssh_speed(conf: ServerConf, down_bytes=8 << 20, up_bytes=4 << 20, run=None) -> dict:
    """Real speed between this computer and the server over ssh (what sync, mounts and onpc use):
    zeros from the server's /dev/zero (down) and to its /dev/null (up), after one warm-up call."""
    run = run or (lambda argv, data=None: subprocess.run(argv, input=data, capture_output=True, timeout=120))
    base = ssh_base(conf, multiplex=True, batch=True) + ["-T", conf.target]
    out = {"rtt": pc_rtt(conf.host, conf.port)}
    try:
        if run(base + ["true"]).returncode != 0:
            raise ApiError(502, "cannot reach the server")
        t = time.time()
        r = run(base + [f"head -c {int(down_bytes)} /dev/zero"])
        dt = max(time.time() - t, 1e-3)
        if r.returncode != 0 or not r.stdout:
            raise ApiError(502, "download test failed")
        out["down"] = round(len(r.stdout) * 8 / dt / 1e6, 1)
        t = time.time()
        r = run(base + ["cat >/dev/null"], b"\0" * int(up_bytes))
        dt = max(time.time() - t, 1e-3)
        if r.returncode != 0:
            raise ApiError(502, "upload test failed")
        out["up"] = round(up_bytes * 8 / dt / 1e6, 1)
    except subprocess.TimeoutExpired:
        raise ApiError(504, "the test took too long — the connection is very slow")
    except OSError as e:
        raise ApiError(500, f"ssh did not start: {e}")
    return out


def pc_hung_up(conn) -> bool:
    import select
    try:
        r, _, _ = select.select([conn], [], [], 0)
        return bool(r) and not conn.recv(1, socket.MSG_PEEK)
    except (OSError, ValueError):
        return True


def port_pids(port: int) -> list:
    """Pids listening on a TCP port of this computer (never this process)."""
    pids = set()
    try:
        if IS_WIN:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=10).stdout
            for ln in out.splitlines():
                f = ln.split()
                if len(f) >= 5 and f[3].upper() == "LISTENING" and f[1].rsplit(":", 1)[-1] == str(port) and f[4].isdigit():
                    pids.add(int(f[4]))
        else:
            for cmd in (["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], ["fuser", f"{port}/tcp"]):
                if not shutil.which(cmd[0]):
                    continue
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                pids.update(int(x) for x in re.findall(r"\d+", r.stdout))
                if pids:
                    break
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    pids.discard(os.getpid())
    return sorted(pids)


def port_kill(pids: list):
    for pid in pids:
        try:
            if IS_WIN:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
            else:
                os.kill(pid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            pass
    if not IS_WIN:
        end = time.time() + 3
        while time.time() < end and any(pid_alive(x) for x in pids):
            time.sleep(0.2)
        for pid in pids:
            if pid_alive(pid):
                try:
                    os.kill(pid, getattr(signal, "SIGKILL"))
                except OSError:
                    pass


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pc_kill(p):
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, getattr(signal, "SIGKILL"))      # not on Windows (never reached there)
    except (OSError, subprocess.SubprocessError):
        pass



# ----------------------------------------------------------------------------- Local only projects (sftp)
# A "Local only" project stays on the user's computer: the server mounts it (sshfs) through the
# Computers link and Claude works on it with all its own tools; every Bash command goes to the
# computer (a PreToolUse hook turns it into onpc). The files come from this small SFTP v3 server,
# which serves ONLY that project's folder (nothing else of the computer).
SFTP_EXT = {"posix-rename@openssh.com": "1", "fsync@openssh.com": "1", "hardlink@openssh.com": "1"}
SFTP_OK, SFTP_EOF, SFTP_NOFILE, SFTP_PERM, SFTP_FAIL, SFTP_UNSUP = 0, 1, 2, 3, 4, 8
# Heavy folders the server never sees (Claude's own scans of a big project would cross the internet
# for every file in them); Bash runs on the computer, so commands still see them. ".git" shows as an
# empty folder: git (and Claude's start-up git scan) finds no repo on the mount, ripgrep still keeps
# to .gitignore; git itself runs on the computer through Bash.
SFTP_HIDE = {"node_modules", "__pycache__", ".venv", "venv", ".gradle", ".next", ".nuxt", ".angular", ".turbo",
             ".parcel-cache", ".pytest_cache", ".mypy_cache", ".tox", ".dart_tool", ".pub-cache"}


def sftp_hidden(parts) -> bool:
    """parts = the path's components below the project folder."""
    low = [x.lower() for x in parts]
    return any(x in SFTP_HIDE for x in low) or ".git" in low[:-1]


# Grep / Glob of a Local only project, done HERE on the computer (onpc --mcp-serve on the server
# sends one request; only the answer crosses the internet) — like VS Code Remote: the work runs
# where the files are. ripgrep when the computer has it, else git ls-files / a walk + Python re.
PCFS_MAX = 200_000                     # characters of one answer (a weak line must not choke on it)


def glob_regex(pat: str) -> "re.Pattern":
    """Glob → regex on /-paths: ** any folders, * and ? inside one name, {a,b}, [..]."""
    out, i, n = "", 0, len(pat)
    while i < n:
        c = pat[i]
        if pat.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pat.startswith("**", i):
            out, i = out + ".*", i + 2
        elif c == "*":
            out, i = out + "[^/]*", i + 1
        elif c == "?":
            out, i = out + "[^/]", i + 1
        elif c == "{" and "}" in pat[i:]:
            j = pat.index("}", i)
            out, i = out + "(?:" + "|".join(re.escape(x) for x in pat[i + 1:j].split(",")) + ")", j + 1
        elif c == "[" and "]" in pat[i + 1:]:
            j = pat.index("]", i + 1)
            body = pat[i + 1:j]
            out, i = out + "[" + ("^" + body[1:] if body[:1] == "!" else body).replace("\\", "\\\\") + "]", j + 1
        else:
            out, i = out + re.escape(c), i + 1
    return re.compile(out + r"\Z")


def _rg() -> str:
    return shutil.which("rg") or ""


def pc_files(base: str) -> list:
    """Files under base, /-relative to it: ripgrep's list (.gitignore kept), else git's, else a walk —
    heavy folders (SFTP_HIDE) and .git never."""
    def ok(rel):
        parts = rel.split("/")
        return not any(x.lower() in SFTP_HIDE or x == ".git" for x in parts)
    for argv in ([_rg(), "--files", "--hidden", "--path-separator", "/", "-g", "!.git"] if _rg() else None,
                 ["git", "ls-files", "-co", "--exclude-standard", "-z"] if shutil.which("git") else None):
        if not argv:
            continue
        try:
            r = subprocess.run(argv, cwd=base, capture_output=True, timeout=120, **NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode in (0, 1) and (r.stdout or argv[0] != "git"):
            sep = b"\0" if argv[0] == "git" else b"\n"
            return [x for x in (y.decode("utf-8", "surrogateescape").replace("\\", "/") for y in r.stdout.split(sep)) if x and ok(x)]
    out = []
    for d, dirs, files in os.walk(base):
        dirs[:] = [x for x in dirs if x.lower() not in SFTP_HIDE and x != ".git"]
        rel = os.path.relpath(d, base).replace(os.sep, "/")
        out += [f if rel == "." else rel + "/" + f for f in files]
    return out


def pcfs_base(local_root: str, server_root: str, path, heavy_ok=False) -> tuple:
    """Claude's path (server form, or relative) → (folder or file here, its server form); never outside.
    heavy_ok: one file (Read / Edit / Write) may be inside node_modules & co — only searches skip them."""
    p = (path or "").strip() or server_root
    if not p.startswith("/"):
        p = server_root.rstrip("/") + "/" + p
    p = os.path.normpath(p).replace("\\", "/")
    sr = server_root.rstrip("/")
    if p != sr and not p.startswith(sr + "/"):
        raise PermissionError(f"{path} is outside the project ({server_root})")
    rel = p[len(sr):].strip("/")
    if not heavy_ok and (sftp_hidden(rel.split("/") if rel else []) or rel.split("/")[-1].lower() in SFTP_HIDE):
        raise FileNotFoundError(f"{path}: a heavy folder, not searched — use Bash (it runs on the computer)")
    return os.path.join(local_root, *rel.split("/")) if rel else local_root, p


def _cut(text: str) -> str:
    return text if len(text) <= PCFS_MAX else text[:PCFS_MAX] + "\n… (answer cut: narrow the path or pattern)"


def pc_glob(local_root: str, server_root: str, a: dict) -> str:
    pat = str(a.get("pattern") or "").strip()
    if not pat:
        raise ValueError("pattern is required")
    base, sbase = pcfs_base(local_root, server_root, a.get("path"))
    if pat.startswith("/"):
        pat = pat[len(sbase.rstrip("/")) + 1:] if pat.startswith(sbase.rstrip("/") + "/") else pat.lstrip("/")
    rx = glob_regex(pat)
    hits = []
    for rel in pc_files(base):
        if rx.match(rel):
            try:
                hits.append((os.path.getmtime(os.path.join(base, rel)), rel))
            except OSError:
                pass
    hits.sort(key=lambda x: -x[0])
    if not hits:
        return "No files found"
    lines = [sbase.rstrip("/") + "/" + r for _, r in hits[:100]]
    more = "\n(Results are truncated. Consider using a more specific path or pattern.)" if len(hits) > 100 else ""
    return _cut("\n".join(lines) + more)


def pc_grep(local_root: str, server_root: str, a: dict) -> str:
    pat = a.get("pattern")
    if not isinstance(pat, str) or not pat:
        raise ValueError("pattern is required")
    base, sbase = pcfs_base(local_root, server_root, a.get("path"))
    mode = a.get("output_mode") if a.get("output_mode") in ("content", "files_with_matches", "count") else "files_with_matches"
    head = a.get("head_limit") if isinstance(a.get("head_limit"), int) and a.get("head_limit") > 0 else 0
    skip = a.get("offset") if isinstance(a.get("offset"), int) and a.get("offset") > 0 else 0
    ci, ml = bool(a.get("-i")), bool(a.get("multiline"))
    ctx = {k: a.get(k) for k in ("-A", "-B", "-C", "context") if isinstance(a.get(k), int) and a.get(k) >= 0}
    num = a.get("-n", True) is not False
    single = os.path.isfile(base)
    prefix = sbase.rstrip("/") + ("" if single else "/")
    lines = []
    if _rg():
        argv = [_rg(), "--hidden", "--no-messages", "--path-separator", "/", "--max-columns", "500", "-g", "!.git"]
        for h_ in SFTP_HIDE:
            argv += ["-g", "!" + h_]
        if ci:
            argv.append("-i")
        if ml:
            argv += ["-U", "--multiline-dotall"]
        if isinstance(a.get("type"), str) and re.fullmatch(r"[\w+-]+", a["type"]):
            argv += ["--type", a["type"]]
        if isinstance(a.get("glob"), str) and a["glob"].strip():
            argv += ["-g", a["glob"].strip()]
        if mode == "files_with_matches":
            argv.append("-l")
        elif mode == "count":
            argv.append("-c")
        else:
            argv += (["-n"] if num else []) + ["--no-heading", "--with-filename"]
            for k, flag in (("-A", "-A"), ("-B", "-B"), ("-C", "-C"), ("context", "-C")):
                if k in ctx:
                    argv += [flag, str(ctx[k])]
        argv += ["-e", pat, "--", os.path.basename(base) if single else "."]
        try:
            r = subprocess.run(argv, cwd=os.path.dirname(base) if single else base, capture_output=True, timeout=300, **NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise OSError("the search took over 5 minutes — narrow the path or pattern")
        if r.returncode not in (0, 1):
            raise ValueError((r.stderr.decode("utf-8", "replace").strip() or "ripgrep failed")[:500])
        for x in r.stdout.decode("utf-8", "replace").splitlines():
            if x == "--":
                lines.append(x)
                continue
            x = x[2:] if x.startswith("./") else x
            if single:
                x = x[len(os.path.basename(base)):] if x.startswith(os.path.basename(base)) else x
            lines.append(prefix + x if not single else sbase + (x if x[:1] in (":", "-") else (":" + x if x else "")))
    else:
        try:
            rx = re.compile(pat, (re.I if ci else 0) | (re.S | re.M if ml else re.M))
        except re.error as e:
            raise ValueError(f"bad pattern: {e}")
        grx = glob_regex(a["glob"].strip() if "/" in a.get("glob", "") else "**/" + a["glob"].strip()) \
            if isinstance(a.get("glob"), str) and a["glob"].strip() else None
        files = [""] if single else pc_files(base)
        for rel in files:
            if grx and not grx.match(rel):
                continue
            fp = base if single else os.path.join(base, rel)
            try:
                with open(fp, "rb") as f:
                    raw = f.read(20_000_000)
            except OSError:
                continue
            if b"\0" in raw[:8000]:
                continue
            text = raw.decode("utf-8", "replace")
            shown = sbase if single else prefix + rel
            if mode != "content":
                n = sum(1 for ln in text.splitlines() if rx.search(ln)) if mode == "count" else (1 if rx.search(text) else 0)
                if n:
                    lines.append(shown if mode == "files_with_matches" else f"{shown}:{n}")
                continue
            for i, ln in enumerate(text.splitlines(), 1):
                if rx.search(ln):
                    lines.append(f"{shown}:{i}:{ln[:500]}" if num else f"{shown}:{ln[:500]}")
    if mode == "files_with_matches":
        lines.sort()
    lines = lines[skip:]
    total = len(lines)
    if head:
        lines = lines[:head]
    if not lines:
        return "No matches found" if mode == "content" else "No files found"
    if mode == "files_with_matches":
        return _cut(f"Found {total} file{'s' if total != 1 else ''}\n" + "\n".join(lines))
    return _cut("\n".join(lines))


PCFS_IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


def _file_of(local_root, server_root, a):
    fp = a.get("file_path")
    if not isinstance(fp, str) or not fp.strip():
        raise ValueError("file_path is required")
    return pcfs_base(local_root, server_root, fp, heavy_ok=True)


def pc_read(local_root: str, server_root: str, a: dict) -> dict:
    """Claude's Read, here: {"text"} numbered like Claude's own ("     1\tline"), or {"image", "mime"}."""
    fp, sp = _file_of(local_root, server_root, a)
    if os.path.isdir(fp):
        raise IsADirectoryError(f"{sp} is a folder — use Glob or Bash ls")
    ext = os.path.splitext(fp)[1].lower()
    if ext in PCFS_IMAGES:
        if os.path.getsize(fp) > 3_700_000:
            raise ValueError(f"{sp}: image over 3.7 MB — make a smaller copy with Bash first")
        with open(fp, "rb") as f:
            return {"image": base64.b64encode(f.read()).decode(), "mime": PCFS_IMAGES[ext]}
    off = a.get("offset") if isinstance(a.get("offset"), int) and a.get("offset") > 0 else 1
    lim = a.get("limit") if isinstance(a.get("limit"), int) and a.get("limit") > 0 else 2000
    size = os.path.getsize(fp)
    if size > 256_000 and not (a.get("offset") or a.get("limit")):
        raise ValueError(f"{sp} is {size // 1000} KB — read it in parts with offset and limit, or Grep it")
    out, n = [], 0
    with open(fp, "rb") as f:
        head = f.read(8000)
        if b"\0" in head:
            raise ValueError(f"{sp} is a binary file{' (PDF: use Bash, e.g. pdftotext)' if ext == '.pdf' else ''}")
        f.seek(0)
        for raw in f:
            n += 1
            if n < off:
                continue
            if n >= off + lim:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            out.append(f"{n:>6}\t{line[:2000]}" + ("… (line cut)" if len(line) > 2000 else ""))
    if not out:
        return {"text": "<system-reminder>Warning: the file exists but the contents are empty.</system-reminder>"
                if size == 0 else f"<system-reminder>Warning: the file has only {n} lines; offset {off} is past the end.</system-reminder>"}
    return {"text": _cut("\n".join(out))}


def _text_of(fp, sp) -> str:
    with open(fp, "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{sp} is not UTF-8 text — edit it with Bash")


def _put(fp, text):
    with open(fp, "w", encoding="utf-8", newline="") as f:     # in place: keeps the file's mode, links, owner
        f.write(text)


def pc_edit(local_root: str, server_root: str, a: dict) -> dict:
    fp, sp = _file_of(local_root, server_root, a)
    old, new = a.get("old_string"), a.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        raise ValueError("old_string and new_string are required")
    if old == new:
        raise ValueError("old_string and new_string are the same — nothing to change")
    if old == "":
        if os.path.exists(fp) and os.path.getsize(fp):
            raise ValueError("old_string is empty but the file is not — use Write, or give the text to replace")
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        _put(fp, new)
        return {"text": f"The file {sp} has been updated successfully."}
    if not os.path.isfile(fp):
        raise FileNotFoundError(f"{sp} does not exist")
    text = _text_of(fp, sp)
    if old not in text and "\r\n" in text and "\n" in old:          # a Windows (CRLF) file: Claude writes \n
        old, new = old.replace("\r\n", "\n").replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
    k = text.count(old)
    if k == 0:
        raise ValueError("String to replace not found in file (read it again: it may have changed).")
    if k > 1 and not a.get("replace_all"):
        raise ValueError(f"Found {k} matches of the string to replace, but replace_all is false. Give more "
                         "context to make it unique, or set replace_all.")
    _put(fp, text.replace(old, new) if a.get("replace_all") else text.replace(old, new, 1))
    return {"text": f"The file {sp} has been updated successfully." + (f" ({k} replacements)" if k > 1 else "")}


def pc_write(local_root: str, server_root: str, a: dict) -> dict:
    fp, sp = _file_of(local_root, server_root, a)
    content = a.get("content")
    if not isinstance(content, str):
        raise ValueError("content is required")
    if os.path.isdir(fp):
        raise IsADirectoryError(f"{sp} is a folder")
    had = os.path.exists(fp)
    if had and "\r\n" in _text_of(fp, sp) and "\r\n" not in content:     # keep a Windows file's line ends
        content = content.replace("\n", "\r\n")
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    _put(fp, content)
    return {"text": f"The file {sp} has been updated successfully." if had else f"File created successfully at: {sp}"}


# git of a Local only project opened elsewhere (web version, phone, another computer): run on the computer it
# lives on. The server is not trusted: only the Changes panel's own subcommands, config only to read, no
# option that runs a program (--upload-pack, --receive-pack, --exec…).
PCGIT_SUBS = {"add", "rm", "rev-parse", "diff", "commit", "push", "pull", "fetch", "ls-files", "log", "config",
              "checkout", "switch", "restore", "reset", "update-index", "status", "stash", "show", "rev-list",
              "rebase", "merge", "hash-object", "for-each-ref", "clean", "checkout-index", "check-ref-format",
              "check-ignore", "branch", "apply", "cherry-pick", "revert", "tag", "mv", "ls-tree", "cat-file",
              "symbolic-ref", "merge-base", "remote", "describe", "blame", "ls-remote"}
PCGIT_BAD = ("--upload-pack", "--receive-pack", "--exec", "--config", "--git-dir", "--work-tree", "--ext-diff",
             "--textconv")


def pc_git(local_root: str, server_root: str, a: dict) -> dict:
    """{prog: git|cat, argv, repo, stdin (base64)} → {code, out (base64), err}."""
    argv, prog, repo = a.get("argv"), a.get("prog") or "git", str(a.get("repo") or "")
    if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv) or not argv:
        raise ValueError("bad git arguments")
    base, _ = pcfs_base(local_root, server_root, repo, heavy_ok=True)
    if prog == "cat":                                  # a file's text for the diff view (cat -- path)
        fp, _ = pcfs_base(local_root, server_root, (repo + "/" if repo else "") + argv[-1], heavy_ok=True)
        with open(fp, "rb") as f:
            return {"code": 0, "out": base64.b64encode(f.read(30_000_000)).decode(), "err": ""}
    if prog != "git" or argv[0] not in PCGIT_SUBS:
        raise PermissionError(f"git {argv[0]} is not run on the computer from the server")
    if argv[0] == "config" and "--get" not in argv:
        raise PermissionError("git config only reads here")
    if any(x.startswith(PCGIT_BAD) for x in argv):
        raise PermissionError("that git option runs a program — not from the server")
    git = shutil.which("git")
    if not git:
        raise FileNotFoundError("git is not installed on this computer")
    try:
        r = subprocess.run([git] + argv, cwd=base, input=base64.b64decode(a.get("stdin") or ""), capture_output=True,
                           timeout=300, env=dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_OPTIONAL_LOCKS="0"),
                           **NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise OSError("git took over 5 minutes")
    return {"code": r.returncode, "out": base64.b64encode(r.stdout).decode(), "err": r.stderr.decode("utf-8", "replace")[-20000:]}


def pc_repos(local_root: str, server_root: str, a: dict) -> dict:
    """The repositories of a Local only project, found on its computer (the mount's .git is empty)."""
    found = find_local_repos(local_root)
    checked = [(r, local_repo_ok(local_root, r)) for r in found]
    return {"repos": real_repos([(r, ok) for r, ok in checked if ok is not None])}


PCFS_OPS = {"grep": lambda *x: {"text": pc_grep(*x)}, "glob": lambda *x: {"text": pc_glob(*x)},
            "read": pc_read, "edit": pc_edit, "write": pc_write,
            "files": lambda lr, sr, a: {"files": pc_files(lr)[:FILE_LIMIT]},      # the @ list of another computer
            "git": pc_git, "repos": pc_repos}


def sftp_stats() -> dict:
    """What the server asked for over the link (Local only): requests, time spent here, busiest folders."""
    return {"n": 0, "ms": 0.0, "hidden": 0, "dirs": {}}


class SftpServer:
    """SFTP version 3 on a pair of binary streams, confined to `root`. Paths are /-separated,
    "/" = root. Symlinks may not lead out of root."""

    def __init__(self, root: str, rfile, wfile, uid=None, gid=None, meter=None, stats=None):
        self.meter = meter if meter is not None else [0, 0]      # [bytes from the server, bytes to it]
        self.stats = stats if stats is not None else sftp_stats()
        self.last = ""
        self.root = os.path.realpath(root)
        self.ids = (uid, gid) if isinstance(uid, int) and isinstance(gid, int) else None   # the server user's
        self.r, self.w = rfile, wfile
        self.handles = {}
        self.next = 1

    # wire ---------------------------------------------------------------------
    def _read(self, n):
        b = b""
        while len(b) < n:
            c = self.r.read(n - len(b))
            if not c:
                raise EOFError
            b += c
        self.meter[0] += n
        return b

    def _send(self, kind: int, rid, payload: bytes):
        body = bytes([kind]) + (struct.pack(">I", rid) if rid is not None else b"") + payload
        self.meter[1] += len(body) + 4
        self.w.write(struct.pack(">I", len(body)) + body)
        self.w.flush()

    @staticmethod
    def _s(b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + b

    def status(self, rid, code, msg=""):
        self._send(101, rid, struct.pack(">I", code) + self._s(msg.encode()) + self._s(b""))

    def attrs(self, st) -> bytes:
        ids = struct.pack(">II", *self.ids) if self.ids else b""
        return (struct.pack(">I", 1 | (2 if self.ids else 0) | 4 | 8) + struct.pack(">Q", st.st_size) + ids
                + struct.pack(">I", st.st_mode & 0xFFFFFFFF)
                + struct.pack(">II", int(st.st_atime) & 0xFFFFFFFF, int(st.st_mtime) & 0xFFFFFFFF))

    # paths --------------------------------------------------------------------
    def local(self, p: str, follow=True) -> str:
        rel = [x for x in p.replace("\\", "/").split("/") if x and x != "."]
        out = []
        for x in rel:
            if x == "..":
                if out:
                    out.pop()
            else:
                out.append(x)
        self.last = "/".join(out[:2])
        if sftp_hidden(out):
            self.stats["hidden"] += 1
            raise FileNotFoundError("not shown on the server (heavy folder): use Bash, it runs on the computer")
        full = os.path.join(self.root, *out) if out else self.root
        check = os.path.realpath(full) if follow else os.path.join(os.path.realpath(os.path.dirname(full)), os.path.basename(full)) if out else self.root
        if check != self.root and not check.startswith(self.root.rstrip(os.sep) + os.sep):
            raise PermissionError("outside the project folder")
        return full

    def virt(self, local: str) -> str:
        rel = os.path.relpath(os.path.realpath(local), self.root)
        return "/" if rel == "." else "/" + rel.replace(os.sep, "/")

    # loop ---------------------------------------------------------------------
    def serve(self):
        try:
            while True:
                n = struct.unpack(">I", self._read(4))[0]
                if n > 1 << 20:
                    return
                pkt = self._read(n)
                self.handle(pkt[0], pkt[1:])
        except (EOFError, OSError):
            pass
        finally:
            for h in self.handles.values():
                if not isinstance(h, list):
                    try:
                        os.close(h)
                    except OSError:
                        pass

    def handle(self, kind, data):
        if kind == 1:                                       # INIT
            ext = b"".join(self._s(k.encode()) + self._s(v.encode()) for k, v in SFTP_EXT.items())
            self._send(2, None, struct.pack(">I", 3) + ext)
            return
        rid = struct.unpack(">I", data[:4])[0]
        self.p, self.data = 4, data
        t0, self.last = time.perf_counter(), ""
        try:
            fn = SFTP_OPS.get(kind)
            if not fn:
                return self.status(rid, SFTP_UNSUP, "unsupported")
            fn(self, rid)
        except FileNotFoundError as e:
            self.status(rid, SFTP_NOFILE, str(e))
        except PermissionError as e:
            self.status(rid, SFTP_PERM, str(e))
        except (OSError, ValueError, KeyError, struct.error) as e:
            self.status(rid, SFTP_FAIL, str(e))
        finally:
            st = self.stats
            st["n"] += 1
            st["ms"] += (time.perf_counter() - t0) * 1000
            d = st["dirs"]
            if self.last in d or len(d) < 500:
                d[self.last] = d.get(self.last, 0) + 1

    def u32(self):
        v = struct.unpack(">I", self.data[self.p:self.p + 4])[0]
        self.p += 4
        return v

    def u64(self):
        v = struct.unpack(">Q", self.data[self.p:self.p + 8])[0]
        self.p += 8
        return v

    def string(self) -> bytes:
        n = self.u32()
        v = self.data[self.p:self.p + n]
        self.p += n
        return v

    def path(self) -> str:
        return self.string().decode("utf-8", "replace")

    def read_attrs(self) -> dict:
        f = self.u32()
        a = {}
        if f & 1:
            a["size"] = self.u64()
        if f & 2:
            self.u32(), self.u32()
        if f & 4:
            a["mode"] = self.u32()
        if f & 8:
            a["atime"], a["mtime"] = self.u32(), self.u32()
        if f & 0x80000000:
            for _ in range(self.u32()):
                self.string(), self.string()
        return a

    def apply(self, target, a: dict):
        """target: a path or an open fd."""
        if "size" in a:
            os.truncate(target, a["size"])
        if "mode" in a:
            if isinstance(target, int):
                if hasattr(os, "fchmod"):
                    os.fchmod(target, a["mode"] & 0o7777)
            else:
                os.chmod(target, a["mode"] & 0o7777)
        if "mtime" in a:
            os.utime(target, (a["atime"], a["mtime"]))

    def new_handle(self, obj) -> bytes:
        h = str(self.next).encode()
        self.next += 1
        self.handles[h] = obj
        return h

    # operations ---------------------------------------------------------------
    def op_open(self, rid):
        p = self.local(self.path())
        pf = self.u32()
        self.read_attrs()
        fl = getattr(os, "O_BINARY", 0)
        fl |= os.O_RDWR if pf & 1 and pf & 2 else os.O_WRONLY if pf & 2 else os.O_RDONLY
        if pf & 4:
            fl |= os.O_APPEND
        if pf & 8:
            fl |= os.O_CREAT
        if pf & 16:
            fl |= os.O_TRUNC
        if pf & 32:
            fl |= os.O_EXCL
        self._send(102, rid, self._s(self.new_handle(os.open(p, fl, 0o666))))

    def op_close(self, rid):
        h = self.handles.pop(self.string())
        if not isinstance(h, list):
            os.close(h)
        self.status(rid, SFTP_OK)

    def op_read(self, rid):
        fd, off, n = self.handles[self.string()], self.u64(), self.u32()
        os.lseek(fd, off, 0)
        b = os.read(fd, min(n, 256 * 1024))
        if not b:
            return self.status(rid, SFTP_EOF, "end of file")
        self._send(103, rid, self._s(b))

    def op_write(self, rid):
        fd, off, b = self.handles[self.string()], self.u64(), self.string()
        os.lseek(fd, off, 0)
        while b:
            b = b[os.write(fd, b):]
        self.status(rid, SFTP_OK)

    def op_lstat(self, rid):
        self._send(105, rid, self.attrs(os.lstat(self.local(self.path(), follow=False))))

    def op_stat(self, rid):
        self._send(105, rid, self.attrs(os.stat(self.local(self.path()))))

    def op_fstat(self, rid):
        self._send(105, rid, self.attrs(os.fstat(self.handles[self.string()])))

    def op_setstat(self, rid):
        p = self.local(self.path())
        self.apply(p, self.read_attrs())
        self.status(rid, SFTP_OK)

    def op_fsetstat(self, rid):
        fd = self.handles[self.string()]
        a = self.read_attrs()
        if "mtime" in a and not getattr(os, "supports_fd", set()) & {os.utime}:
            a.pop("mtime")
        self.apply(fd, a)
        self.status(rid, SFTP_OK)

    def op_opendir(self, rid):
        v = self.path()
        p = self.local(v)
        under = [x.lower() for x in v.replace("\\", "/").split("/") if x and x != "."]
        names = [".", ".."] + ([] if under and under[-1] == ".git" else
                               [x for x in os.listdir(p) if x.lower() not in SFTP_HIDE])
        self._send(102, rid, self._s(self.new_handle([p, names])))

    def op_readdir(self, rid):
        d = self.handles[self.string()]
        base, names = d
        if not names:
            return self.status(rid, SFTP_EOF, "end")
        batch, d[1] = names[:100], names[100:]
        out = b""
        n = 0
        for name in batch:
            try:
                st = os.lstat(os.path.join(base, name))
            except OSError:
                continue
            nb = name.encode("utf-8", "surrogateescape")
            out += self._s(nb) + self._s(nb) + self.attrs(st)
            n += 1
        self._send(104, rid, struct.pack(">I", n) + out)

    def op_remove(self, rid):
        os.remove(self.local(self.path(), follow=False))
        self.status(rid, SFTP_OK)

    def op_mkdir(self, rid):
        p = self.local(self.path())
        self.read_attrs()
        os.mkdir(p)
        self.status(rid, SFTP_OK)

    def op_rmdir(self, rid):
        os.rmdir(self.local(self.path()))
        self.status(rid, SFTP_OK)

    def op_realpath(self, rid):
        p = self.path()
        loc = self.local(p or "/")
        v = self.virt(loc) if os.path.exists(loc) else "/" + "/".join(x for x in p.split("/") if x and x != ".")
        st = b"\0\0\0\0"
        self._send(104, rid, struct.pack(">I", 1) + self._s(v.encode()) + self._s(v.encode()) + st)

    def op_rename(self, rid):
        a, b = self.local(self.path(), follow=False), self.local(self.path(), follow=False)
        if os.path.exists(b):
            raise FileExistsError("target exists")
        os.rename(a, b)
        self.status(rid, SFTP_OK)

    def op_readlink(self, rid):
        p = self.local(self.path(), follow=False)
        t = os.readlink(p)
        tl = os.path.realpath(os.path.join(os.path.dirname(p), t))
        if tl != self.root and not tl.startswith(self.root.rstrip(os.sep) + os.sep):
            raise PermissionError("the link leads out of the project folder")
        v = t.replace(os.sep, "/") if not os.path.isabs(t) else self.virt(tl)
        self._send(104, rid, struct.pack(">I", 1) + self._s(v.encode()) + self._s(v.encode()) + b"\0\0\0\0")

    def op_symlink(self, rid):
        target, link = self.path(), self.path()         # OpenSSH order: target first
        lp = self.local(link, follow=False)
        tl = os.path.realpath(os.path.join(os.path.dirname(lp), target)) if not target.startswith("/") else self.local(target)
        if tl != self.root and not tl.startswith(self.root.rstrip(os.sep) + os.sep):
            raise PermissionError("the link would lead out of the project folder")
        os.symlink(target if not target.startswith("/") else os.path.relpath(tl, os.path.dirname(lp)), lp)
        self.status(rid, SFTP_OK)

    def op_extended(self, rid):
        name = self.string().decode()
        if name == "posix-rename@openssh.com":
            os.replace(self.local(self.path(), follow=False), self.local(self.path(), follow=False))
        elif name == "hardlink@openssh.com":
            os.link(self.local(self.path()), self.local(self.path(), follow=False))
        elif name == "fsync@openssh.com":
            os.fsync(self.handles[self.string()])
        else:
            return self.status(rid, SFTP_UNSUP, "unsupported")
        self.status(rid, SFTP_OK)


SFTP_OPS = {3: SftpServer.op_open, 4: SftpServer.op_close, 5: SftpServer.op_read, 6: SftpServer.op_write,
            7: SftpServer.op_lstat, 8: SftpServer.op_fstat, 9: SftpServer.op_setstat, 10: SftpServer.op_fsetstat,
            11: SftpServer.op_opendir, 12: SftpServer.op_readdir, 13: SftpServer.op_remove, 14: SftpServer.op_mkdir,
            15: SftpServer.op_rmdir, 16: SftpServer.op_realpath, 17: SftpServer.op_stat, 18: SftpServer.op_rename,
            19: SftpServer.op_readlink, 20: SftpServer.op_symlink, 200: SftpServer.op_extended}


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


def ssh_cfg_value(v: str) -> str:
    """A path in ~/.ssh/config: quoted when it has a space ("/Users/simab co/.ssh/key") — unquoted, ssh
    reads "extra arguments at end of line" and refuses the WHOLE file (every ssh of the app stops)."""
    return f'"{v}"' if " " in v and not v.startswith('"') else v


def heal_ssh_config(path: Path = None) -> bool:
    """~/.ssh/config with an unquoted space-path (an older version of ours wrote one; or anyone): quote it
    in place — such a line is never valid, ssh refuses the whole file. A backup is kept next to it.
    Mutagen and the user's own ssh read this file directly, so the copy of win_ssh_config is not enough."""
    f = path or home_path(".ssh", "config")
    try:
        old = f.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return False
    new = quote_ssh_paths(old)
    if new == old:
        return False
    bak = f.with_name(f.name + ".simab-backup")
    bak.write_text(old, encoding="utf-8", errors="surrogateescape")
    f.write_text(new, encoding="utf-8", errors="surrogateescape")
    return True


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
        new = new.rstrip("\n") + ("\n" if new.strip() else "") + f"\n{RC_MARK_BEGIN}\nHost {conf.host}\n    IdentityFile {ssh_cfg_value(conf.key)}\n{RC_MARK_END}\n"
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
        argv = ["ssh", *win_ssh_config(), "-o", "StrictHostKeyChecking=accept-new", "-p", str(conf.port), conf.target,
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


def linux_menu_entry(cmd: str) -> bool:
    """The app-menu entry + icon (hicolor theme). StartupWMClass = the window's --class, so the dock shows
    our logo for the open window instead of a gear. Writes only what changed; True when something did."""
    apps = home_path(".local", "share", "applications")
    icons = home_path(".local", "share", "icons")
    files = [(icons / "hicolor" / "scalable" / "apps" / "simabworkspace.svg", STATIC / "icon.svg"),
             (icons / "hicolor" / "512x512" / "apps" / "simabworkspace.png", STATIC / "icon-512.png"),
             (icons / "hicolor" / "192x192" / "apps" / "simabworkspace.png", STATIC / "icon-192.png")]
    desk = ("[Desktop Entry]\nType=Application\nName=Simab Workspace\nComment=Projects, terminals and Claude\n"
            f"Exec={desktop_quote(cmd)} gui\nIcon=simabworkspace\nStartupWMClass=simabworkspace\nStartupNotify=true\n"
            "Terminal=false\nCategories=Development;\n")
    changed = False
    for dst, src in files:
        if src.is_file() and (not dst.is_file() or dst.read_bytes() != src.read_bytes()):
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            changed = True
    old = icons / "simabworkspace.svg"           # before 1.66.2: Icon=<this path>, no StartupWMClass
    if old.is_file():
        old.unlink()
    f = apps / "simabworkspace.desktop"
    if not f.is_file() or f.read_text(errors="replace") != desk:
        apps.mkdir(parents=True, exist_ok=True)
        f.write_text(desk)
        changed = True
    if changed:
        for tool in (["gtk-update-icon-cache", "-q", "-t", str(icons / "hicolor")], ["update-desktop-database", "-q", str(apps)]):
            if shutil.which(tool[0]):
                try:
                    subprocess.run(tool, stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
                except (OSError, subprocess.SubprocessError):
                    pass
    return changed


def linux_desktop() -> bool:
    """A Linux desktop session (not Windows, Mac or WSL)."""
    return sys.platform.startswith("linux") and not is_wsl() and bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


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
        self.alt_hosts = set()        # WSL: "<vm ip>:<port>" — Windows reaches us there when localhost forwarding is broken
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
        self._shared = {}             # project -> (time, [shared commands from .simab.json])
        self._shared_busy = set()
        self.pusher = None            # web version: Pusher of the web server (phone notifications)
        self.push_msgs = collections.deque(maxlen=30)
        self.push_seq = 0
        self.last_visible = 0.0       # a page of this workspace was on screen (GET tabs?vis=1)
        self._restored = threading.Event()    # set once the tabs of the last run are back (restore_live)
        self._restored.set()
        self._live_saved = False
        self.copies = {}              # project -> FastCopy (Add project with parallel copies)
        self._share_lock = threading.Lock()
        self._share_kick = threading.Event()     # a tab opened / closed: share now
        self._share_stop = False
        self._share_keep = {}                    # tab key -> closed here (value: Claude keeps working)
        self._share_seen = None                  # tab keys the shared set and this device agree on
        self._share_projects = []                # projects of the other devices
        self._share_gone = {}                    # session id -> closed because another device closed it
        self._layout_out = []                    # group / group colour / order changes not yet on the server
        self._layout_lock = threading.Lock()
        self._copies_checked = False
        self.pc = PcHub(self)                    # builds on this computer (onpc)
        self._pc_unlock = {}                     # web: computer id -> (MFA unlock token, until) — memory only

    @property
    def conf(self) -> ServerConf:
        return self._conf or load_server_conf()

    # projects / status
    def projects(self, max_age=3.0) -> list:
        t, cached = self._projects_cache
        if time.time() - t < max_age:
            return cached
        p = merge_projects([] if self.web else mutagen_sessions(), self.store.manual(), self.store.data["projects"])
        have = {x["name"] for x in p} | {x.get("remote") for x in p if x.get("remote")}
        hidden = set(self.store.db.get("hiddenShared", []) or [])      # shared projects removed from this device's list
        for sp in self._share_projects:
            if sp["name"] in have or sp["remote"] in have or sp["remote"] in hidden:
                continue
            have |= {sp["name"], sp["remote"]}
            own = self.store.data["projects"].get(sp["name"]) or {}      # renamed / coloured here
            p.append({"name": sp["name"], "source": "shared", "local": "", "localExists": False, "remote": sp["remote"],
                      "endpoint": {}, "sync": None, "commands": list(own.get("commands") or []),
                      "color": own.get("color") or str(sp.get("color") or "")[:20],
                      "starter": own.get("starter") or str(sp.get("starter") or "")[:2000],
                      "title": own.get("title") or str(sp.get("title") or "")[:80], "group": own.get("group") or "",
                      "sharedFrom": sp.get("sharedFrom") or ""})
        pci = (getattr(self, "_pc_cache", None) or (0, {}))[1]
        for x in p:                   # the owner of a Local only project, as the server knows it (any computer)
            own = (pci.get("pins") or {}).get(x["name"])
            if (pci.get("builds") or {}).get(x["name"]) == "local" and own:
                x["localOnly"] = own
        if self.web:                  # "local" would be the web server's own disk
            for x in p:
                x["local"], x["localExists"] = "", False
        else:
            self._copies_leftover()
            # a Local only project of ANOTHER of my computers: a folder with the same path here is not it —
            # its files, @ list and Files tab come from that computer (through the server), never this disk
            me = self.pc.cfg()["id"] if getattr(self, "pc", None) else ""
            for x in p:
                if x.get("localOnly") and x["localOnly"] != me:
                    x["local"], x["localExists"], x["elsewhere"] = "", False, True
        for x in p:
            job = self.copies.get(x["name"])
            if job and not job.done and x.get("sync"):
                x["sync"].update(state="busy", label=f"copying ×{job.streams}", paused=False, progress=job.progress())
        for x in p:
            x["commands"] = [c for c in x["commands"] if not c.get("shared")] + self.shared_commands(x)
        if self.web:
            self._device_commands(p)
        order = self.store.project_order()
        if order:
            idx = {name: i for i, name in enumerate(order)}
            p.sort(key=lambda x: idx.get(x["name"], len(order)))
        builds = ((getattr(self, "_pc_cache", None) or (0, {}))[1] or {}).get("builds") or {}
        gcol = self.store.group_colors()
        for x in p:
            x["groupColor"] = gcol.get(x.get("group") or "", "") if x.get("group") else ""
        for x in p:                   # where its builds run (Computers): run commands follow it
            x["buildsOn"] = "local" if x.get("localOnly") else builds.get(x["name"], "server")
        self._projects_cache = (time.time(), p)
        return p

    SHARED_TTL = 30

    def _shared_side(self, p: dict) -> str:
        if p.get("localExists") and not self.web:
            return "local"
        return "server" if p.get("remote") and self.conf.configured else ""

    def shared_commands(self, p: dict) -> list:
        """The project's .simab.json commands from the cache; an old cache is refreshed in the background."""
        t, cmds = self._shared.get(p["name"], (0.0, []))
        if time.time() - t > self.SHARED_TTL and p["name"] not in self._shared_busy and self._shared_side(p):
            self._shared_busy.add(p["name"])
            threading.Thread(target=self._shared_refresh, args=(dict(p),), daemon=True).start()
        return [dict(c) for c in cmds]

    def _shared_refresh(self, p: dict):
        try:
            cmds = shared_clean(self._shared_call(p, "read").get("commands"))
            old = self._shared.get(p["name"], (0.0, None))[1]
            self._shared[p["name"]] = (time.time(), cmds)
            if old != cmds:
                self.invalidate()
        except ApiError:
            self._shared[p["name"]] = (time.time(), self._shared.get(p["name"], (0.0, []))[1])
        finally:
            self._shared_busy.discard(p["name"])

    # Share on your domain: the settings live in ~/.config/simabworkspace/share.json on the server, so the
    # app and the web version show and change the same; the web version answers the requests (ShareHub).
    def share_data(self) -> dict:
        if not self.conf.configured:
            raise ApiError(400, "the server is not configured")
        r = remote_python(self.conf, SHARE_CFG_PY, ["get"], multiplex=bool(self.store.settings.get("sshMultiplex")))
        if isinstance(r, dict) and r.get("missing"):
            return {"missing": True, "domains": [], "shares": []}
        return share_clean(r)

    def share_save(self, data: dict) -> dict:
        r = remote_python(self.conf, SHARE_CFG_PY, ["put"], stdin=json.dumps(share_clean(data)).encode(),
                          multiplex=bool(self.store.settings.get("sshMultiplex")))
        return share_clean(r)

    def share_api(self, method: str, sub: str, body: dict, host: str = "") -> dict:
        hook = getattr(self, "share_hook", None)           # the web version: its tables follow at once
        if method == "GET" and sub == "/status":
            return {"status": self.share_status(self.share_data()), "web": self.web}
        if method == "GET" and sub == "":
            data = self.share_data()
        elif method == "POST" and sub == "/revoke":
            gid = str(body.get("id") or "")
            if not re.fullmatch(r"[0-9a-f]{16}", gid):
                raise ApiError(400, "bad request")
            rv = getattr(self, "share_revoke", None)
            if rv:
                rv(gid)
                data = self.share_data()
            else:                                           # the app: this computer at once, the web version within 30 s
                self.pc.share_revoke(gid)
                data = self.share_data()
                data["revoke"] = sorted(set(data.get("revoke") or []) | {gid})
                data = self.share_save(data)
        elif method == "POST" and sub == "/port":
            data = share_edit(self.share_data(), sub, body, host if self.web else "")
            if hook:
                hook(data, host, True)
            data = self.share_save(data)
        else:
            raise ApiError(404, "not found")
        if hook:
            hook(data, host, False)
        data.pop("missing", None)
        data.pop("domains", None)
        for x in data["shares"]:
            x["urls"] = [f"{data['base']}/{x['slug']}/{x['name']}"] if data.get("base") else []
        data["web"] = self.web
        live = getattr(self, "share_live", None)
        if live:
            data["live"] = live()
        else:
            try:
                r = remote_python(self.conf, SHARE_CFG_PY, ["live-get"], multiplex=bool(self.store.settings.get("sshMultiplex")))
                data["live"] = r if isinstance(r, dict) and not r.get("missing") else {}
            except (ApiError, OSError, ValueError):
                data["live"] = {}
            gone = set(data.get("revoke") or [])               # revoked here, the web version not yet done
            if gone and data["live"].get("guests"):
                data["live"]["guests"] = [g for g in data["live"]["guests"] if g.get("id") not in gone]
        return data

    def share_status(self, data: dict) -> dict:
        shares = data.get("shares") or []
        probe = getattr(self, "share_probe", None)
        if probe:                                           # the web version: through the user's computers
            return probe(shares)
        on = self.pc.cfg()["on"]
        out = {}
        for x in shares:
            out[f"{x['slug']}/{x['name']}"] = "pc-off" if not on else "on" if port_open(int(x["port"])) else "port-off"
        return out

    def tasks(self, op: dict = None) -> dict:
        """The task list on the server (see TASKS_PY); op = one change, None = just read it."""
        if not self.conf.configured:
            raise ApiError(400, "the server is not configured")
        body = clean_task_op(op) if op is not None else {"op": "list"}
        res = remote_python(self.conf, TASKS_PY, [], stdin=json.dumps(body).encode(),
                            multiplex=bool(self.store.settings.get("sshMultiplex")))
        if not isinstance(res, dict):
            raise ApiError(502, "bad answer while reading the tasks")
        if res.get("error"):
            raise ApiError(400, str(res["error"]))
        return {"tasks": res.get("tasks") or [], "rev": res.get("rev") or 0}

    def tts(self, body: dict) -> dict:
        """A Persian piece of an answer spoken on the server (see TTS_PY): {audio: base64, mime}."""
        if not self.conf.configured:
            raise ApiError(400, "the server is not configured")
        req = clean_tts(body)
        res = remote_python(self.conf, TTS_PY, [TTS_IMAGE], stdin=json.dumps(req).encode(), timeout=120,
                            multiplex=bool(self.store.settings.get("sshMultiplex")))
        if not isinstance(res, dict):
            raise ApiError(502, "bad answer from the Persian voice")
        if res.get("installing"):
            return {"installing": True}
        if res.get("error"):
            raise ApiError(502, str(res["error"]))
        return {"audio": res.get("audio") or "", "mime": res.get("mime") or "audio/wav"}

    # projects + open tabs shared with the other devices (see SHARE_PY) ------------------
    def device_id(self) -> str:
        d = self.store.db.get("deviceId", "")
        if not (isinstance(d, str) and re.fullmatch(r"[0-9a-f]{16}", d)):
            d = secrets.token_hex(8)
            self.store.db.put("deviceId", d)
        return d

    def device_name(self) -> str:
        return "Web" if self.web else (socket.gethostname() or "Computer")[:40]

    def share_on(self) -> bool:
        return bool(self.store.settings.get("shareDevices", True)) and self.conf.configured

    def _share_projects_mine(self) -> list:
        """This device's projects that live on the default server (others cannot reach the rest)."""
        c, out = self.conf, []
        pc_id = ""
        if not self.web and getattr(self, "pc", None):
            try:
                pc_id = str(self.pc.cfg().get("id") or "")
            except Exception:
                pc_id = ""
        for p in self.projects(max_age=10):
            ep = p.get("endpoint") or {}
            if p.get("source") == "shared" or not p.get("remote"):
                continue
            if ep.get("host") and (ep.get("host") != c.host or (ep.get("user") or c.user) != c.user
                                   or str(ep.get("port") or c.port) != str(c.port)):
                continue
            rec = {k: p.get(k) or "" for k in ("name", "remote", "title", "color", "starter")}
            if not self.web:              # its own run commands: the web shows them and runs them here / on the server
                rec["pc"], rec["commands"] = pc_id, share_run_commands(p)
            out.append(rec)
        return out[:300]

    def _device_commands(self, ps: list):
        """Web: the run commands my computers set for the same project folder, each marked with where it runs
        (that computer, or the server)."""
        for sp in self._share_projects:
            k = layout_key(sp.get("remote"))
            cmds = sp.get("commands") if isinstance(sp.get("commands"), list) else []
            if not k or not cmds:
                continue
            for x in ps:
                if layout_key(x.get("remote")) != k:
                    continue
                have = {c.get("id") for c in x["commands"]}
                for c in cmds:
                    if isinstance(c, dict) and c.get("id") and c["id"] not in have:
                        have.add(c["id"])
                        x["commands"].append(dict(c, shared=False, where="server" if c.get("where") == "server" else "local",
                                                  devName=str(sp.get("sharedFrom") or "your computer"),
                                                  fromDev=str(sp.get("pc") or ""), devProject=str(sp.get("name") or "")))

    def _share_tabs_mine(self) -> dict:
        out = {}
        for s in list(self.sessions.values()):
            k = share_tab_key(getattr(s, "spec", {}) or {})
            if k and k not in out:
                out[k] = s
        return out

    def share_sync(self) -> dict:
        """One round: publish my projects and tab changes, then open the tabs other devices opened
        and close the ones they closed. Answers what changed here (for tests / logs)."""
        if not self.share_on() or self._share_stop:
            return {}
        with self._share_lock:
            mine = self._share_tabs_mine()
            first = self._share_seen is None          # first round: only read (what is open / closed elsewhere)
            seen = set() if first else self._share_seen
            add = [] if first else [
                {"key": k, "title": s.title, "spec": {x: s.spec.get(x) for x in ("kind", "project", "resume", "named") if s.spec.get(x)}}
                for k, s in mine.items() if k not in seen]
            # only tabs the user closed here (quitting / a restart closes nothing elsewhere)
            keep = dict(self._share_keep)
            remove = [] if first else [{"key": k, "keep": bool(v)} for k, v in keep.items() if k not in mine]
            with self._layout_lock:
                lay_out, self._layout_out = self._layout_out, []
            lay_send = (self._layout_seed() if first else []) + lay_out
            op = {"device": self.device_id(), "name": self.device_name(), "projects": self._share_projects_mine(),
                  "add": add, "remove": remove, "layout": lay_send}
            try:
                res = remote_python(self.conf, SHARE_PY, [], stdin=json.dumps(op).encode(),
                                    multiplex=bool(self.store.settings.get("sshMultiplex")), timeout=30)
            except Exception as e:
                log_line(f"share: {e}")
                res = None
            if not isinstance(res, dict) or "tabs" not in res:
                with self._layout_lock:
                    self._layout_out = lay_out + self._layout_out      # sent again next round
                return {}
            seen =(seen | {t["key"] for t in add}) - {t["key"] for t in remove}
            for k in keep:
                self._share_keep.pop(k, None)
            if first:
                seen = {k for k in mine if k in (res.get("tabs") or {})}      # the rest is added next round
            # projects of the other devices
            me, others = self.device_id(), []
            for dev_id, dev in (res.get("devices") or {}).items():
                if dev_id == me or not isinstance(dev, dict):
                    continue
                for p in dev.get("projects") or []:
                    if isinstance(p, dict) and isinstance(p.get("name"), str) and p["name"] and isinstance(p.get("remote"), str) and p["remote"]:
                        others.append(dict(p, sharedFrom=str(dev.get("name") or "")[:40]))
            if others != self._share_projects:
                self._share_projects = others
                self._projects_cache = (0.0, [])
            with self._layout_lock:
                busy = bool(self._layout_out)       # changed here meanwhile: the next round brings it back
            if not busy:
                self._layout_apply(res.get("layout"))
            names ={p["name"] for p in self.projects(max_age=0)}
            opened, closed = [], []
            for k, c in (res.get("closed") or {}).items():
                s = mine.get(k)
                if s and isinstance(c, dict) and c.get("by") != me and not any(t["key"] == k for t in add):
                    try:
                        self.close_session(s.id, keep=bool(c.get("keep")), share=False)
                    except ApiError:
                        pass
                    self._share_gone[s.id] = time.time()
                    seen.discard(k)
                    closed.append(k)
            for k, t in (res.get("tabs") or {}).items():
                if k in mine or k in seen or not isinstance(t, dict):
                    continue
                sp = t.get("spec") or {}
                if sp.get("project") and sp["project"] not in names:
                    continue                   # that project is not here
                body = {x: sp.get(x) for x in ("kind", "project", "resume", "mode", "model", "effort", "named") if sp.get(x)}
                if body.get("kind") == "chat":
                    body["where"] = "server"
                body["title"] = str(t.get("title") or "")[:80]
                try:
                    info = self._create_session(body)
                except (ApiError, OSError) as e:
                    log_line(f"shared tab {k}: {e}")
                    continue
                sess = self.sessions.get(info.get("id"))
                if sess:
                    sess.moved = time.time()          # the open pages add it (see syncTabs)
                seen.add(k)
                opened.append(k)
            self._share_seen = seen
            if first and any(k not in seen for k in self._share_tabs_mine()):
                self._share_kick.set()
            return {"opened": opened, "closed": closed}

    # project groups, group colours and the project order: the same on every device (SHARE_PY "layout")
    def _layout_remotes(self) -> dict:
        return {x["name"]: layout_key(x.get("remote")) for x in self.projects(max_age=10) if layout_key(x.get("remote"))}

    def _layout_order(self, rm: dict) -> list:
        out = []
        for n in self.store.project_order():
            if rm.get(n) and rm[n] not in out:
                out.append(rm[n])
        return out

    def _layout_seed(self) -> list:
        """This device's list, at 0: kept only where no device has set anything yet."""
        rm, sp = self._layout_remotes(), self.store.data["projects"]
        ch = [{"k": "g", "r": r, "v": (sp.get(n) or {}).get("group"), "at": 0}
              for n, r in rm.items() if (sp.get(n) or {}).get("group")]
        ch += [{"k": "c", "g": g, "v": c, "at": 0} for g, c in self.store.group_colors().items() if c]
        o = self._layout_order(rm)
        return ch + ([{"k": "o", "v": o, "at": 0}] if o else [])

    def layout_changed(self, names=(), colors=(), order=False):
        """A group / group colour / order change made here: on the server at the next share round (now)."""
        if not self.share_on():
            return
        now, rm, ch = time.time(), self._layout_remotes(), []
        for n in names:
            if rm.get(n):
                ch.append({"k": "g", "r": rm[n], "v": (self.store.data["projects"].get(n) or {}).get("group") or "", "at": now})
        gc = self.store.group_colors()
        ch += [{"k": "c", "g": g, "v": gc.get(g, ""), "at": now} for g in colors if g]
        if order:
            ch.append({"k": "o", "v": self._layout_order(rm), "at": now})
        if ch:
            with self._layout_lock:
                self._layout_out += ch
            self._share_kick.set()

    def _layout_apply(self, lay) -> bool:
        """The shared groups / colours / order → this device's list. True = something changed."""
        if not isinstance(lay, dict):
            return False
        ps, changed = self.projects(max_age=0), False
        groups = lay.get("groups") if isinstance(lay.get("groups"), dict) else {}
        by = {}
        for x in ps:
            r = layout_key(x.get("remote"))
            if r:
                by.setdefault(r, []).append(x["name"])
        with self.store.lock:
            for r, names in by.items():
                v = groups.get(r)
                if not (isinstance(v, list) and v and isinstance(v[0], str)):
                    continue
                for n in names:
                    rec = self.store.data["projects"].setdefault(n, {})
                    if (rec.get("group") or "") != v[0][:40]:
                        rec["group"], changed = v[0][:40], True
            cols = {g: v[0] for g, v in (lay.get("colors") or {}).items()
                    if isinstance(v, list) and v and isinstance(v[0], str) and re.fullmatch(r"#[0-9a-f]{6}", v[0])}
            if isinstance(lay.get("colors"), dict) and cols != self.store.group_colors():
                self.store.db.put("groupColors", cols)
                changed = True
            o = lay.get("order")
            if isinstance(o, list) and o and isinstance(o[0], list):
                want = []
                for r in o[0]:
                    want += [n for n in by.get(r, []) if n not in want]
                cur = self.store.project_order()
                new = want + [n for n in cur if n not in want]
                if new != cur:
                    self.store.db.put("projectOrder", new[:2000])
                    changed = True
            if changed:
                self.store.save()
        if changed:
            self._projects_cache = (0.0, [])
        return changed

    def share_loop(self):
        while not self._share_stop:
            try:
                self.share_sync()
            except Exception as e:                     # never let the loop die
                log_line(f"share: {e}")
            busy = time.time() - self.last_visible < 120
            self._share_kick.wait(6 if busy else 60)
            self._share_kick.clear()

    def _shared_call(self, p: dict, op: str, stdin: bytes = None) -> dict:
        side = self._shared_side(p)
        if side == "local":
            r = subprocess.run([sys.executable, "-I", "-c", SHARED_PY, op, p["local"]], input=stdin or b"",
                               capture_output=True, timeout=20)
            try:
                res = json.loads(r.stdout.decode("utf-8", "replace") or "null")
            except ValueError:
                res = None
        elif side == "server":
            res = remote_python(self._project_conf(p), SHARED_PY, [op, p["remote"]], stdin=stdin or b"",
                                multiplex=bool(self.store.settings.get("sshMultiplex")))
        else:
            raise ApiError(400, "this project has no folder for shared commands")
        if not isinstance(res, dict):
            raise ApiError(502, "bad answer while reading .simab.json")
        if res.get("error"):
            raise ApiError(400, str(res["error"]))
        return res

    def command(self, project: str, cid: str):
        """A run command of the project: this workspace's own, or a shared one (.simab.json)."""
        c = self.store.command(project, cid)
        if c:
            return c
        t, cmds = self._shared.get(project, (0.0, []))
        c = next((dict(x) for x in cmds if x["id"] == cid), None)
        if c or not self.web:
            return c
        x = next((x for x in self.projects(max_age=10) if x["name"] == project), None)      # one of my computers'
        return next((dict(c) for c in (x or {}).get("commands") or [] if c.get("id") == cid and c.get("devName")), None)

    def set_commands(self, project: str, commands) -> list:
        """From the editor: rows marked shared go to the project's .simab.json, the rest stay here."""
        p = self._project(project)
        if not isinstance(commands, list):
            raise ApiError(400, "invalid command list")
        mine = [c for c in commands if not (isinstance(c, dict) and c.get("shared"))]
        shared = [c for c in commands if isinstance(c, dict) and c.get("shared")]
        before = self._shared.get(project, (0.0, []))[1]
        out = self.store.set_commands(project, mine)
        if shared or before:
            clean = clean_commands([dict(c, id="tmp0") for c in shared])
            plain = [{k: c[k] for k in ("name", "cmd", "where", "cwd", "url") if c.get(k)} for c in clean]
            self._shared_call(p, "write", json.dumps(plain, ensure_ascii=False).encode())
            self._shared[project] = (time.time(), shared_clean(plain))
        self.invalidate()
        return out + [dict(c) for c in self._shared.get(project, (0.0, []))[1]]

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

    def _create_session(self, body: dict, sid: str = None, ready=False) -> dict:
        if not ready:
            self._restored.wait(60)
        sid = sid or secrets.token_hex(6)
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
            pn = built["spec"]["project"]
            built["prepare"] = lambda force=False: self.pc.ensure_mount(pn, force) if pn else None
            c = ChatSession(sid, built, on_change=self._changed)
            c.log = self.activity.add
            c.remember = self.store.set_chat_prefs
            c.remember_pc = self.store.set_chat_pc
            c.on_usage = self.usage.limits
            c.on_cost = self.usage.cost
            c.on_notify = self.push_notify
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
        if body.get("kind") == "tasks":
            if not self.conf.configured:
                raise ApiError(400, "the server is not configured")
            title = body.get("title") if isinstance(body.get("title"), str) and body.get("title").strip() else "Tasks"
            s = FilesSession(sid, "", title[:80], kind="tasks")
            with self.lock:
                self.sessions[sid] = s
            self._remember()
            return s.info()
        if body.get("kind") == "changes":
            p = self._project(body.get("project") or "")
            if not (p.get("remote") or (p.get("localExists") and not self.web)):
                raise ApiError(400, "this project has no folder")
            title = body.get("title") if isinstance(body.get("title"), str) and body.get("title").strip() else ""
            s = FilesSession(sid, p["name"], title[:80], kind="changes")
            with self.lock:
                self.sessions[sid] = s
            self._remember()
            return s.info()
        body = {k: v for k, v in body.items() if k not in ("_docker", "_cmd")}
        if body.get("kind") == "run":
            body["_cmd"] = self.command(body.get("project") or "", body.get("cmdId") or "")
        if self.web:
            if body.get("kind") not in WEB_KINDS:
                raise ApiError(403, "not available in the web version")
            if body.get("kind") == "shell":
                body["where"] = "server"
            if body.get("kind") == "run":
                c = body.get("_cmd")
                if c and c.get("where") != "server":
                    raise ApiError(403, "only server commands run in the web version")
        if body.get("kind") == "docker":
            body["_docker"] = self.docker_tab(body.get("project") or "", body.get("container"), body.get("action"))
        if body.get("kind") == "pcterm" and self.web:      # the computer opens it only with a ticket of its MFA unlock
            body["_ticket"] = self.pc_ticket(str(body.get("project") or ""))
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
            cmd = self.command(sp["project"], sp["cmdId"]) or {}
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
        self._restored.wait(60)
        wins = self.live_windows()
        now = time.time()
        self._share_gone = {k: t for k, t in self._share_gone.items() if now - t < 120}
        return {"sessions": [s.info() for s in self.sessions.values()], "windows": wins, "now": now,
                "gone": list(self._share_gone)}

    def get(self, sid: str) -> Session:
        s = self.sessions.get(sid)
        if not s and not self._restored.is_set():
            self._restored.wait(60)
            s = self.sessions.get(sid)
        if not s:
            raise ApiError(404, "this tab no longer exists")
        return s

    def close_session(self, sid: str, keep=False, share=True):
        """keep: a chat tab closed while Claude works — Claude finishes on the server (it can be
        opened again from Sessions); otherwise it is stopped unless another device is on it too.
        share=False: closed because another device closed it."""
        s = self.get(sid)
        if share:
            k = share_tab_key(getattr(s, "spec", {}) or {})
            if k:
                self._share_keep[k] = keep
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
        # only folders synced on THIS computer: a project added on another device can still be downloaded here
        synced = {p.get("remote"): p["name"] for p in self.projects(max_age=5) if p.get("remote") and p.get("source") == "mutagen"}
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
        ignores = self._clean_ignores(body.get("ignores")) if body.get("ignores") is not None else default_ignores()
        if body.get("ignores") is not None:
            argv += ["--ignores", ",".join(ignores)]
        try:
            streams = max(0, min(int(body.get("streams") or 0), COPY_MAX_STREAMS))
        except (TypeError, ValueError):
            streams = 0
        if streams:
            argv += ["--paused"]
        code, out = self.run_script(argv, timeout=180)
        if code != 0:
            raise ApiError(400, self.script_error(out, "could not add the project"))
        self.invalidate()
        added = re.search(r"Project '([^']+)' added", out)
        pname = added.group(1) if added else name
        if streams and pname:
            self.start_copy(pname, mode, ignores, git == "on", streams)
        self.activity.add("add-" + mode, pname, ("downloaded from " + remote) if mode == "download" else ("uploaded " + local))
        return {"ok": True, "project": pname, "output": out[-2000:]}

    def _copy_dir(self) -> Path:
        return self.store.path.parent / "copying"

    def start_copy(self, name: str, mode: str, ignores: list, git: bool, streams: int):
        """The first copy of a just-added (paused) project over several ssh connections, then resume."""
        sess = next((x for x in mutagen_sessions() if x.get("name") == name), None)
        if not sess:
            mutagen_resume(name)
            return None
        a, b = _endpoint(sess.get("alpha")), _endpoint(sess.get("beta"))
        local, remote = (a, b) if not a["host"] else (b, a)
        if not local["path"] or not remote["path"]:
            mutagen_resume(name)
            return None
        marker = self._copy_dir() / re.sub(r"[^A-Za-z0-9._-]", "-", name)
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(name)
        except OSError:
            marker = None

        def done(job):
            self.invalidate()
            took = int(time.time() - job.started)
            note = f"copied {job.files} files over {job.streams} connections in {took // 60}:{took % 60:02d}"
            if job.failed:
                note += f" ({len(job.failed)} left for the sync)"
            self.activity.add("copy", name, note)
        job = FastCopy(name, mode, local["path"], remote["path"],
                       self.conf.with_endpoint(remote["user"], remote["host"], remote["port"]),
                       ignores, git, streams, marker=str(marker) if marker else None, on_done=done)
        self.copies[name] = job
        threading.Thread(target=job.run, daemon=True).start()
        return job

    def _copies_leftover(self):
        """A copy the app did not finish (closed meanwhile): resume its sync, Mutagen copies the rest."""
        if self._copies_checked:
            return
        self._copies_checked = True
        try:
            left = [f for f in self._copy_dir().iterdir() if f.is_file()]
        except OSError:
            return
        for f in left:
            try:
                name = f.read_text().strip()
            except OSError:
                continue
            if name and name not in self.copies:
                threading.Thread(target=mutagen_resume, args=(name,), daemon=True).start()
                try:
                    f.unlink()
                except OSError:
                    pass

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

    def remove_project(self, name: str, server: bool = False, local: bool = False) -> dict:
        """Take a project off the list; optionally delete its folder on the server and/or this computer."""
        p = next((x for x in self.projects(max_age=0) if x["name"] == name), None)
        if not p:
            raise ApiError(404, "no such project")
        if p.get("source") == "shared":           # another device's project: only hide it here; its folder is the owner's
            if server or local:
                raise ApiError(400, "a shared project's folder is deleted on the device that added it")
            hidden = [x for x in (self.store.db.get("hiddenShared", []) or []) if x != p["remote"]] + [p["remote"]]
            self.store.db.put("hiddenShared", hidden[-500:])
            self.store.set_project_order([n for n in self.store.project_order() if n != name])
            self.invalidate()
            self.activity.add("project-remove", name, "removed from this list (shared from " + (p.get("sharedFrom") or "another device") + ")")
            return {"ok": True, "server": False, "local": False, "errors": []}
        if server and (p.get("localOnly") or not p.get("remote")):
            raise ApiError(400, "this project has no folder of its own on the server")
        if local and (self.web or not p.get("local")):
            raise ApiError(400, "this project has no folder on this computer")
        if p.get("source") == "mutagen":            # first stop the sync, so a delete does not travel across
            exe = which("mutagen")
            r = subprocess.run([exe, "sync", "terminate", name], capture_output=True, text=True, timeout=60, env=dict(os.environ, PATH=tool_path()))
            if r.returncode != 0:
                raise ApiError(500, (r.stderr or r.stdout).strip()[-300:] or "could not stop the sync")
        self.pc_forget_local(name)
        self.store.remove_manual(name)
        with self.store.lock:
            self.store.data["projects"].pop(name, None)
            self.store.save()
        self.store.set_project_order([n for n in self.store.project_order() if n != name])
        self.invalidate()
        out, errors = {"ok": True, "server": False, "local": False}, []
        for where, on in (("server", server), ("local", local)):
            if not on:
                continue
            try:
                res = rm_project_dir(p["local"]) if where == "local" else remote_python(
                    self._project_conf(p), RMDIR_PY, [p["remote"]], multiplex=bool(self.store.settings.get("sshMultiplex")), timeout=300)
            except ApiError as e:
                res = {"error": e.message}
            res = res if isinstance(res, dict) else {"error": "bad answer"}
            if res.get("error") or res.get("errors"):
                errors.append(where + ": " + str(res.get("error") or res["errors"][0]))
            out[where] = bool(res.get("deleted"))
        out["errors"] = errors
        what = " + ".join(w for w in ("server", "local") if out[w]) or "list only"
        self.activity.add("project-remove", name, "removed (" + what + ")" + ("; " + errors[0] if errors else ""))
        return out

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
            linux_menu_entry(cmd)
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

    # skills -------------------------------------------------------------------
    def skill_repos(self) -> list:
        return self.store.skill_repos()

    def set_skill_repos(self, repos: list) -> list:
        clean = []
        for r in (repos or []):
            if not isinstance(r, dict):
                continue
            url = str(r.get("url") or "").strip()[:300]
            name = str(r.get("name") or "").strip()[:100]
            if url:
                clean.append({"url": url, "name": name})
        return self.store.set_skill_repos(clean[:50])

    def fetch_github_skills(self, repo_url: str) -> list:
        import re as _re
        m = _re.match(r'https?://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$', repo_url.strip())
        if not m:
            raise ApiError(400, "not a valid GitHub repo URL")
        owner, repo = m.group(1), m.group(2)
        api_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD?recursive=1"
        req = urllib.request.Request(api_url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "simab-workspace/1",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise ApiError(502, f"GitHub API error: {e.code}")
        except Exception as e:
            raise ApiError(502, f"GitHub fetch failed: {e}")
        skills = []
        for item in (data.get("tree") or []):
            if item.get("type") == "blob" and item.get("path", "").endswith(".md"):
                path = item["path"]
                skill_name = path.split("/")[-1]
                skills.append({"name": skill_name, "path": path, "repo": f"{owner}/{repo}"})
        return skills

    def fetch_skill_content(self, repo_url: str, path: str) -> str:
        import re as _re
        m = _re.match(r'https?://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$', repo_url.strip())
        if not m:
            raise ApiError(400, "not a valid GitHub repo URL")
        owner, repo = m.group(1), m.group(2)
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{path}"
        req = urllib.request.Request(raw_url, headers={"User-Agent": "simab-workspace/1"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            raise ApiError(502, f"fetch failed: {e}")

    def list_project_skills(self, project_name: str) -> list:
        p = self._project(project_name)
        if not p.get("remote"):
            raise ApiError(400, "project has no server")
        return remote_python(self._project_conf(p), SKILL_LIST_PY, [p["remote"]],
                             multiplex=bool(self.store.settings.get("sshMultiplex")))

    def install_skill(self, project_name: str, skill_name: str, content: str) -> None:
        p = self._project(project_name)
        if not p.get("remote"):
            raise ApiError(400, "project has no server")
        if not skill_name.endswith(".md") or "/" in skill_name or "\\" in skill_name:
            raise ApiError(400, "invalid skill name")
        remote_python(self._project_conf(p), SKILL_WRITE_PY, [p["remote"], skill_name],
                      multiplex=bool(self.store.settings.get("sshMultiplex")),
                      stdin=content.encode())

    def uninstall_skill(self, project_name: str, skill_name: str) -> None:
        p = self._project(project_name)
        if not p.get("remote"):
            raise ApiError(400, "project has no server")
        if not skill_name.endswith(".md") or "/" in skill_name or "\\" in skill_name:
            raise ApiError(400, "invalid skill name")
        remote_python(self._project_conf(p), SKILL_DELETE_PY, [p["remote"], skill_name],
                      multiplex=bool(self.store.settings.get("sshMultiplex")))

    # git badges ---------------------------------------------------------------
    def git_summary_one(self, p: dict, fetch=False) -> dict:
        info = git_repos(self, p)
        out = {"changes": 0, "ahead": 0, "behind": 0, "conflicts": 0, "repos": len(info["repos"]),
               "noUpstream": 0, "t": time.time(), "where": info["where"], "branch": "", "branches": 0}
        names = set()
        for r in info["repos"]:
            repo = GitRepo(self, p, r, info["where"])
            try:
                if fetch and repo.run(["fetch", "--quiet", "--prune"], timeout=60)[0] == 0:
                    repo.mark_fetched()
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
        elif p.get("remote") and p.get("localOnly"):
            # lives on one of my computers: that computer lists it (one request through the server) —
            # not a walk of the server's mount, one round trip per folder
            res = remote_python(self._project_conf(p), PC_FILES_PY, [p["remote"]],
                                multiplex=bool(self.store.settings.get("sshMultiplex")), timeout=90) or {}
            if res.get("error"):
                raise ApiError(400, str(res["error"]))
            files = [x for x in res.get("files") or [] if isinstance(x, str)][:FILE_LIMIT]
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
        self._share_kick.set()
        specs = []
        for s in list(self.sessions.values()):
            sp = dict(s.spec)
            if sp.get("kind") in ("sync-info",):
                continue
            sp["title"] = s.title
            specs.append(sp)
        self.store.set_last_tabs(specs)

    def shutdown_sessions(self):
        self._share_stop = True               # quitting closes tabs here, not on the other devices
        with self._share_lock:
            pass
        self._save_live()
        for s in list(self.sessions.values()):
            getattr(s, "shutdown", s.kill)()
        self.sessions = {}
        for t in list(self.tunnels.values()):
            t.stop()
        self.pc.stop()
        self.pc.stop_bg()                        # dev servers end with the app

    # A restart (update, the web service restarted) must not cost the open tabs: chats (Claude
    # keeps running on the server under its relay), watched chats and Files tabs come back with
    # the SAME ids, so browsers simply reconnect. Terminals cannot: their process is gone.
    LIVE_KINDS = ("chat", "files", "changes", "tasks")
    LIVE_MAX_AGE = 600              # installed app: only right after a restart, not the next morning

    def _save_live(self):
        if self._live_saved:
            return
        self._live_saved = True
        tabs = []
        for s in list(self.sessions.values()):
            sp = dict(s.spec)
            if sp.get("kind") not in self.LIVE_KINDS:
                continue
            if sp.get("kind") == "chat" and not sp.get("resume"):
                continue              # a chat with no conversation yet: nothing to come back to
            tabs.append({"id": s.id, "win": s.win or "", "title": s.title, "spec": sp})
        self.store.db.put("live", {"at": time.time(), "tabs": tabs})

    def restore_live(self, wait=False):
        """Bring back the tabs saved by the last shutdown (in a thread: chats read their history
        over SSH). Until it is done, state/tabs/new tabs/websockets wait for it."""
        rec = self.store.db.get("live", None)
        if not isinstance(rec, dict) or not rec.get("tabs"):
            return
        self.store.db.put("live", {"at": 0, "tabs": []})
        if not self.web and time.time() - float(rec.get("at") or 0) > self.LIVE_MAX_AGE:
            return
        self._restored.clear()

        def run():
            try:
                for t in rec["tabs"][:40]:
                    try:
                        self._restore_one(t)
                    except Exception as e:                # one bad tab must not stop the others
                        log_line(f"restore tab {t.get('id')}: {e}")
            finally:
                self._restored.set()
        th = threading.Thread(target=run, daemon=True)
        th.start()
        if wait:
            th.join()

    def _restore_one(self, t: dict):
        sid, sp = t.get("id"), t.get("spec") or {}
        if not (isinstance(sid, str) and re.fullmatch(r"[0-9a-f]{12}", sid)) or sid in self.sessions:
            return
        body = {"kind": sp.get("kind"), "project": sp.get("project") or "", "title": t.get("title") or ""}
        if sp.get("kind") == "chat":
            body["resume"] = sp.get("resume") or ""
            if sp.get("watch"):
                body["watch"] = True
            elif sp.get("relay"):
                body["relay"] = sp["relay"]
            for k in ("mode", "model", "effort", "named"):
                if sp.get(k):
                    body[k] = sp[k]
        info = self._create_session(body, sid=sid, ready=True)
        wid = t.get("win") or ""
        if wid and WIN_RE.fullmatch(wid):
            s = self.sessions.get(info["id"])
            if s:
                s.win, s.moved = wid, time.time()

    # phone notifications (web version) ------------------------------------------------
    PUSH_MAX = 10

    def _push_subs(self) -> list:
        v = self.store.db.get("push", [])
        return [x for x in v if isinstance(x, dict) and push_endpoint_ok(x.get("endpoint"))] if isinstance(v, list) else []

    def push_info(self) -> dict:
        if not self.pusher:
            return {"available": False}
        return {"available": True, "key": self.pusher.public(), "devices": len(self._push_subs())}

    def push_subscribe(self, b: dict) -> dict:
        if not self.pusher:
            raise ApiError(400, "notifications need the web version")
        sub = b.get("subscription") if isinstance(b, dict) else None
        ep = sub.get("endpoint") if isinstance(sub, dict) else None
        if not push_endpoint_ok(ep):
            raise ApiError(400, "this browser's push service is not supported")
        subs = [x for x in self._push_subs() if x["endpoint"] != ep]
        subs.append({"endpoint": ep, "at": time.time(), "seen": self.push_seq, "device": clean_str(b.get("device"), 120, "device")})
        self.store.db.put("push", subs[-self.PUSH_MAX:])
        return self.push_info()

    def push_unsubscribe(self, b: dict) -> dict:
        ep = b.get("endpoint") if isinstance(b, dict) else None
        self.store.db.put("push", [x for x in self._push_subs() if x["endpoint"] != ep])
        return self.push_info()

    def push_pending(self, b: dict) -> dict:
        """The service worker got a push: what to show (messages this device has not shown yet)."""
        ep = b.get("endpoint") if isinstance(b, dict) else None
        subs = self._push_subs()
        me = next((x for x in subs if x["endpoint"] == ep), None)
        seen = me.get("seen", 0) if me else self.push_seq - 1
        out = [m for m in self.push_msgs if m["seq"] > seen and time.time() - m["at"] < 3600]
        if me:
            me["seen"] = self.push_seq
            self.store.db.put("push", subs)
        return {"messages": out[-5:]}

    def push_notify(self, sess, kind: str, detail: str = ""):
        if not self.pusher or time.time() - self.last_visible < 8:
            return                        # someone is looking at the workspace: the page shows it
        subs = self._push_subs()
        if not subs:
            return
        p = (sess.spec or {}).get("project") or ""
        name = sess.title if not str(sess.title).startswith("Claude · ") else (p or "home")
        title = "Claude needs you" if kind == "ask" else "Claude finished"
        body = (name + (" · " + p if p and p != name else "")) + (" — " + detail if detail else "")
        with self.lock:
            self.push_seq += 1
            self.push_msgs.append({"seq": self.push_seq, "at": time.time(), "title": title, "body": body[:200],
                                   "tag": sess.id + ":" + kind, "tab": sess.id})
        threading.Thread(target=self._push_send, args=(subs,), daemon=True).start()

    def _push_send(self, subs):
        gone = []
        for x in subs:
            code = self.pusher.send(x["endpoint"])
            if code in (404, 410):
                gone.append(x["endpoint"])
            elif not 200 <= code < 300:
                log_line(f"push: {urllib.parse.urlsplit(x['endpoint']).hostname} answered {code}")
        if gone:
            self.store.db.put("push", [x for x in self._push_subs() if x["endpoint"] not in gone])

    def client_error(self, b: dict) -> dict:
        """A script error in a browser (phone, tablet…) → one line in the log. At most 30 a minute."""
        now = time.time()
        recent = [t for t in getattr(self, "_client_errs", []) if now - t < 60]
        if len(recent) >= 30:
            return {"ok": False}
        self._client_errs = recent + [now]

        def one(k, n):
            v = b.get(k) if isinstance(b, dict) else ""
            return " ".join(str(v if isinstance(v, (str, int, float)) else "").split())[:n]
        who = f"{self.conf.user}@{self.conf.host}" if self.web else "app"
        log_line(f"CLIENT ERROR [{who}] {one('message', 500)} @ {one('where', 200)} | {one('size', 20)} | {one('ua', 200)}"
                 + (f"\n  {one('stack', 2000)}" if one('stack', 10) else ""))
        return {"ok": True}

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

    # builds on your own computers (onpc) ---------------------------------------------
    def pc_api(self, method: str, rest: list, body: dict) -> dict:
        key = (method, rest[0] if rest else "")
        if key == ("GET", "devices"):
            return self.pc_devices(cached=rest[1:] != ["fresh"])      # the Computers dialog asks now, the sidebar may use 10 s old
        if key == ("POST", "remove"):
            did = body.get("id")
            if not isinstance(did, str) or not PC_ID_RE.fullmatch(did):
                raise ApiError(400, "invalid computer")
            if not self.web and did == self.pc.cfg()["id"]:
                self.pc.update({"on": False})
            remote_python(self.conf, PC_ADMIN_PY, ["remove", did])
            return self.pc_devices()
        if key == ("GET", "remote-asks"):
            return self.pc_remote_asks()
        if key in (("POST", "remote-run"), ("POST", "remote-log"), ("POST", "remote-stop")):
            op = key[1].split("-")[1]
            call = {"op": op, "project": str(body.get("project") or ""), "id": str(body.get("id") or ""), "name": str(body.get("name") or "")}
            dev = body.get("dev") or ""
            if dev and not (isinstance(dev, str) and PC_ID_RE.fullmatch(dev)):
                raise ApiError(400, "invalid computer")
            r = self.pc_call(dev, call)
            if not isinstance(r, dict) or r.get("error"):
                e = str((r or {}).get("error") or "no answer")
                raise ApiError(409 if "already running" in e else 403 if (r or {}).get("locked") else 404, e)
            return r
        if key == ("POST", "remote-portstop"):
            dev = body.get("dev") or ""
            if dev and not (isinstance(dev, str) and PC_ID_RE.fullmatch(dev)):
                raise ApiError(400, "invalid computer")
            r = self.pc_call(dev, {"op": "portstop", "port": clean_port(body.get("port"), "port")})
            if not isinstance(r, dict) or r.get("error"):
                raise ApiError(403 if (r or {}).get("locked") else 404, str((r or {}).get("error") or "no answer"))
            return r
        if key == ("POST", "remote-answer"):
            dev, aid, ans = body.get("dev"), body.get("id"), body.get("answer")
            if not (isinstance(dev, str) and PC_ID_RE.fullmatch(dev) and isinstance(aid, str) and re.fullmatch(r"[0-9a-f]{12}", aid)
                    and ans in PC_ANSWERS):
                raise ApiError(400, "bad request")
            r = self.pc_call(dev, {"op": "answer", "id": aid, "answer": ans})
            if isinstance(r, dict) and r.get("error"):
                raise ApiError(403 if r.get("locked") else 404, r["error"])
            return {"ok": True}
        if key == ("POST", "unlock"):           # the phone: this computer's own MFA code → no limits there for 1 h
            dev, code = body.get("dev"), str(body.get("code") or "").replace(" ", "")
            if not (isinstance(dev, str) and PC_ID_RE.fullmatch(dev)) or not re.fullmatch(r"\d{6}", code):
                raise ApiError(400, "enter the 6-digit code")
            r = self.pc_call(dev, {"op": "unlock", "code": code, "by": ua_label(str(body.get("by") or ""))[:60] or "web"})
            if not isinstance(r, dict) or not r.get("token"):
                raise ApiError(403, (r or {}).get("error") or "no answer from that computer")
            with self.lock:
                self._pc_unlock[dev] = (r["token"], float(r.get("until") or 0))
            return {"unlocked": self.pc_unlocked()}
        if key == ("POST", "lock"):
            dev = body.get("dev")
            if not (isinstance(dev, str) and PC_ID_RE.fullmatch(dev)):
                raise ApiError(400, "invalid computer")
            if dev in self.pc_unlocked():
                self.pc_call(dev, {"op": "lock"})
            with self.lock:
                self._pc_unlock.pop(dev, None)
            return {"unlocked": self.pc_unlocked()}
        if key == ("POST", "pair"):
            if not self.web:
                raise ApiError(400, "make the link in the web version (it is reachable from the new computer)")
            if not self.conf.configured:
                raise ApiError(400, "the server is not configured")
            return pair_new(self.conf)
        if key == ("POST", "builds"):
            p = self._project(body.get("project") if isinstance(body.get("project"), str) else "")
            on = body.get("on")
            if on not in ("computers", "server"):
                raise ApiError(400, "invalid choice")
            if not p.get("remote"):
                raise ApiError(400, "this project has no server folder")
            if (p.get("endpoint") or {}).get("host") not in (None, "", self.conf.host):
                raise ApiError(400, "this project is on another server")
            dev = body.get("dev") or ""
            if dev and not (isinstance(dev, str) and PC_ID_RE.fullmatch(dev)):
                raise ApiError(400, "invalid computer")
            remote_python(self.conf, PC_ADMIN_PY, ["builds", p["remote"], on] + ([dev] if dev and on == "computers" else []))
            if not self.web:
                self.pc.trust(p["name"], on == "computers")       # chosen here: its hooked commands follow the chat
            return self.pc_devices()
        if key == ("POST", "default"):         # the computer of every My computers project without its own choice
            dev = body.get("dev") or ""
            if dev and not (isinstance(dev, str) and PC_ID_RE.fullmatch(dev)):
                raise ApiError(400, "invalid computer")
            if not self.conf.configured:
                raise ApiError(400, "the server is not configured")
            remote_python(self.conf, PC_ADMIN_PY, ["default", dev])
            return self.pc_devices()
        if key == ("GET", "net") and self.web:           # each Local only chat's line, as the server saw it
            return self.web_net()
        if key == ("GET", "webruns") and len(rest) == 2 and self.web:     # a chat's commands, live
            return self.web_runs(rest[1])
        if self.web:
            raise ApiError(403, "not available in the web version")
        if key == ("POST", "local"):
            return self.pc_add_local(body)
        if key == ("POST", "mount"):
            return {"mounts": self.pc.mount()}
        if key == ("GET", ""):
            return self.pc.public()
        if key == ("POST", ""):
            return self.pc.update(body)
        if key == ("GET", "asks"):
            return self.pc.pending()
        if key == ("POST", "guest") and not self.web:         # this computer's user answers a visitor of a shared link
            return self.pc.guest_answer(body.get("id"), body.get("answer"))
        if key == ("GET", "net") and not self.web:
            return self.pc.net_info()
        if key == ("POST", "speed"):
            return ssh_speed(self.conf)
        if key == ("POST", "portstop"):
            if self.web:                    # never the server's own processes: the web asks the computer (remote-portstop)
                raise ApiError(403, "not in the web version")
            return self.pc.stop_port(clean_port(body.get("port"), "port"))
        if key == ("POST", "bgstop"):
            self.pc.stop_bg(str(body.get("name") or "") or "-")
            return {"bg": self.pc.bg_list()}
        if key == ("GET", "bglog") and len(rest) == 2:
            x = self.pc.bg.get(rest[1])
            if not x:
                raise ApiError(404, "no such dev server")
            return {"name": x["name"], "state": x["state"], "url": x["url"], "out": x["out"][-60000:]}
        if key == ("POST", "answer"):
            return self.pc.answer(body.get("id"), body.get("answer"))
        if key == ("GET", "access"):           # Phone access: who is in now + the log
            return self.pc.remote_access()
        if key == ("POST", "disconnect"):
            uid = body.get("id")
            if not (uid == "all" or (isinstance(uid, str) and re.fullmatch(r"[0-9a-f]{12}", uid))):
                raise ApiError(400, "bad request")
            return self.pc.lock_out(uid)
        if key == ("POST", "mfa"):            # phone access devices: set on THIS computer only
            if self.web:
                raise ApiError(403, "set phone access on the computer itself")
            op = body.get("op")
            if op == "setup":
                return self.pc.mfa_setup(body.get("name"))
            if op == "on":
                return self.pc.mfa_add(body.get("code"))
            if op in ("remove", "off"):
                did = "all" if op == "off" else body.get("id")
                if not (did == "all" or (isinstance(did, str) and PC_ID_RE.fullmatch(did))):
                    raise ApiError(400, "bad request")
                return self.pc.mfa_remove(did)
            raise ApiError(400, "bad request")
        if key == ("GET", "run") and len(rest) == 2:
            return self.pc.run_info(rest[1])
        raise ApiError(404, "not found")

    def setup_check(self) -> dict:
        """⚙ → Setup check: every item with ok + what to do."""
        items = []
        st = self.status(force=True)
        ok_ssh = st.get("ssh") == "ok"
        items.append({"id": "server", "ok": ok_ssh, "title": "Server connection",
                      "detail": f"{self.conf.target} port {self.conf.port}" if ok_ssh else (st.get("sshError") or "not connected"), "fix": "connection"})
        srv = {}
        if ok_ssh:
            try:
                srv = remote_python(self.conf, SERVER_CHECK_PY, []) or {}
            except ApiError as e:
                srv = {"error": e.message}
        items.append({"id": "claude", "ok": bool(srv.get("claude")), "title": "Claude Code on the server",
                      "detail": srv.get("claude") or srv.get("error") or "not installed (curl -fsSL https://claude.ai/install.sh | bash)",
                      "fix": "install-claude"})
        items.append({"id": "login", "ok": bool(srv.get("login")), "title": "Claude signed in on the server",
                      "detail": "signed in" if srv.get("login") else "not signed in yet — sign in once in a server terminal", "fix": "login"})
        if not self.web:
            pr = self.prereqs()
            miss = [x["name"] for x in pr.get("items", []) if not x["ok"]]
            items.append({"id": "tools", "ok": not miss, "title": "Tools on this computer",
                          "detail": "all there" if not miss else "missing: " + ", ".join(miss), "fix": "tools"})
            c = self.pc.cfg()
            items.append({"id": "computer", "ok": c["on"] and self.pc.state == "connected", "optional": True, "title": "This computer in Computers",
                          "detail": "connected" if c["on"] and self.pc.state == "connected" else (self.pc.error or "not connected") if c["on"] else
                          "off — connect it so Claude can run commands and Local only projects here", "fix": "computer"})
        fs = srv.get("sshfs") or ""
        items.append({"id": "sshfs", "ok": fs in ("system", "own") and bool(srv.get("fuse")), "optional": True,
                      "title": "sshfs on the server (Local only projects)",
                      "detail": {"system": "installed", "own": "ready (our own copy)", "apt": "not yet — fetched on first use, or now"}.get(fs,
                                 "missing — ask the server's admin: sudo apt install sshfs") + ("" if srv.get("fuse", True) else " · /dev/fuse missing"),
                      "fix": "sshfs" if fs == "apt" else ""})
        return {"items": items}

    def setup_fix_sshfs(self) -> dict:
        r = remote_python(self.conf, PC_MOUNT_PY, ["mount"], timeout=300, stdin=b'{"mounts": []}')
        return {"ok": True, "result": r}

    def pc_remote_asks(self) -> dict:
        """The open questions of all the user's computers, each tied to this workspace's chat tab."""
        r = remote_python(self.conf, PC_BRIDGE_PY, ["asks"])
        r = r if isinstance(r, dict) else {}
        tabs = {(x.spec or {}).get("relay"): sid for sid, x in list(self.sessions.items()) if (x.spec or {}).get("relay")}
        for a in r.get("asks") or []:
            a["session"] = tabs.get(a.get("chat") or "", "")
        return {"asks": r.get("asks") or [], "online": r.get("online") or 0, "on": r.get("on") or [], "now": time.time(), "bg": r.get("bg") or [],
                "unlocked": self.pc_unlocked()}

    def pc_unlocked(self) -> dict:
        """Web version: the computers this workspace unlocked with their MFA code → until when. The tokens
        live only in this process's memory (never on disk, never in a command line)."""
        now = time.time()
        with self.lock:
            d = self._pc_unlock
            for k in [k for k, v in d.items() if v[1] < now]:
                del d[k]
            return {k: v[1] for k, v in d.items()}

    def pc_call(self, dev: str, body: dict):
        """PC_BRIDGE_PY call with this workspace's unlock tokens (over stdin)."""
        self.pc_unlocked()
        with self.lock:
            body = dict(body, toks={k: v[0] for k, v in self._pc_unlock.items()})
        return remote_python(self.conf, PC_BRIDGE_PY, ["call", dev or ""], stdin=json.dumps(body).encode())

    def pc_ticket(self, project: str) -> str:
        """A one-time terminal ticket from an unlocked computer that has the project, else "" (onpc says why)."""
        for dev in self.pc_unlocked():
            try:
                r = self.pc_call(dev, {"op": "ticket", "project": project})
            except ApiError:
                continue
            if isinstance(r, dict) and r.get("ticket"):
                return r["ticket"]
        return ""

    def pc_watch_loop(self):
        """Web version: a question on one of the user's computers → a phone notification (when nobody
        looks at a page). Quiet while no computer is connected."""
        told = set()
        while not getattr(self, "_stopping", False):
            wait = 6
            if self.pusher and self._push_subs():
                try:
                    r = self.pc_remote_asks()
                    if not r["online"]:
                        wait = 60
                    for a in r["asks"]:
                        if a["id"] in told:
                            continue
                        told.add(a["id"])
                        sess = self.sessions.get(a["session"]) or types_ns(id=a["id"], title=a["project"], spec={"project": a["project"]})
                        self.push_notify(sess, "ask", f"run on {a.get('devName') or 'your computer'}: {a['cmd'][:80]}")
                except ApiError:
                    wait = 60
            else:
                wait = 30
            time.sleep(wait)

    def pc_add_local(self, b: dict) -> dict:
        """A Local only project: its files stay in `local` on this computer; the server mounts them
        at ~/.simab-mnt/<name> and Claude works there (every Bash command runs here)."""
        c = self.pc.cfg()
        if not c["on"]:
            raise ApiError(400, "connect this computer first: ⚙ → Computers → Connect this computer")
        local = os.path.abspath(os.path.expanduser(clean_str(b.get("local"), 1000, "folder", required=True)))
        if not os.path.isdir(local):
            raise ApiError(400, f"no such folder on this computer: {local}")
        name = clean_str(b.get("name"), 64, "name") or os.path.basename(local.rstrip("/\\"))
        if any(p["name"] == name for p in self.projects(max_age=0)):
            raise ApiError(409, f"there is already a project named {name}")
        remote = "~/.simab-mnt/" + name
        item = self.store.add_manual({"name": name, "local": local, "remote": remote})
        with self.store.lock:
            self.store.data["projects"].setdefault(name, {})["localOnly"] = c["id"]
            self.store.save()
        self.invalidate()
        remote_python(self.conf, PC_ADMIN_PY, ["builds", remote, "local", c["id"]])
        self._pc_cache = None
        mounts = self.pc.mount([name]) if self.pc.state == "connected" else {}
        return {"project": item, "mount": mounts.get(name, "")}

    def pc_forget_local(self, name: str):
        """Removing a Local only project: its mount leaves the server (the files stay on the computer)."""
        p = next((x for x in self.projects(max_age=0) if x["name"] == name and x.get("localOnly")), None)
        if not p:
            return
        self.pc.unmount([p])
        try:
            remote_python(self.conf, PC_ADMIN_PY, ["builds", p["remote"], "server"])
        except ApiError:
            pass
        with self.store.lock:
            (self.store.data["projects"].get(name) or {}).pop("localOnly", None)
            self.store.save()
        self._pc_cache = None

    def web_runs(self, chat: str) -> dict:
        """The web version / phone: the commands a chat ran on my computers, with their output so far
        (RunNote of onpc) — the Bash card shows them live, like in the app."""
        if not RELAY_KEY_RE.fullmatch(chat or ""):
            raise ApiError(400, "bad chat")
        r = remote_python(self.conf, PC_RUNS_PY, [chat], timeout=15) or {}
        return {"runs": [x for x in r.get("runs") or [] if isinstance(x, dict)]}

    def web_net(self) -> dict:
        """The web version / phone: each Local only chat's line, as onpc on the server measured it
        (Mux.note → links/<chat>.json). The computer's own app is not reachable from here."""
        c = getattr(self, "_web_net", None)
        if c and time.time() - c[0] < 2.5:
            return c[1]
        r = remote_python(self.conf, PC_LINKS_PY, [], timeout=15) or {}
        out = {"state": "connected", "web": True, "rtt": None, "bars": 0, "down": 0, "up": 0,
               "chats": r.get("chats") if isinstance(r.get("chats"), dict) else {}}
        self._web_net = (time.time(), out)
        return out

    def pc_devices(self, cached=False) -> dict:
        """The computers of this server user (from the server) + which projects build on them.
        cached: every window asks every 30 s (sidebar marks) — one ssh call per 10 s is enough."""
        c = getattr(self, "_pc_cache", None)
        if cached and c and time.time() - c[0] < 10:
            return c[1]
        if not self.conf.configured:
            raise ApiError(400, "the server is not configured")
        paths = {p["name"]: p["remote"] for p in self.projects() if p.get("remote")}
        r = remote_python(self.conf, PC_ADMIN_PY, ["list"], stdin=json.dumps({"paths": paths}).encode())
        r = r if isinstance(r, dict) else {}
        out = {"devices": r.get("devices") or [], "builds": r.get("builds") or {}, "pins": r.get("pins") or {},
               "default": r.get("default") or "",
               "self": "" if self.web else self.pc.cfg()["id"]}
        if not self.web:              # my Local only projects made before the owner was kept on the server
            for p in self.projects():
                if p.get("localOnly") == out["self"] and out["builds"].get(p["name"]) == "local" \
                        and out["pins"].get(p["name"]) != out["self"] and p.get("remote"):
                    remote_python(self.conf, PC_ADMIN_PY, ["builds", p["remote"], "local", out["self"]])
                    out["pins"][p["name"]] = out["self"]
        known = getattr(self, "_pc_cache", None)
        if not known or {k: v for k, v in known[1].get("pins", {}).items() if known[1].get("builds", {}).get(k) == "local"} \
                != {k: v for k, v in out["pins"].items() if out["builds"].get(k) == "local"}:
            self._pc_cache = (time.time(), out)
            self.invalidate()         # projects() learns which computer each Local only project lives on
        if not self.web and self.pc.cfg().get("trusted") is None:     # once, from an older version: keep what was chosen
            self.pc.trust([n for n, v in out["builds"].items() if v == "computers"], True)
        self._pc_cache = (time.time(), out)
        return out

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
    def full_remote_path(self, p: dict) -> str:
        """The server folder with ~ replaced by the real home: VS Code does not expand ~, it
        opened an empty window for "~/shop"."""
        r = p["remote"]
        if r != "~" and not r.startswith("~/"):
            return r
        homes = self.__dict__.setdefault("_server_homes", {})
        key = json.dumps(p.get("endpoint") or {}, sort_keys=True)
        if not homes.get(key):
            home = (self.server_ls("~/", p["name"]).get("home") or "").rstrip("/")
            if not home.startswith("/"):
                raise ApiError(502, "could not find the home folder on the server")
            homes[key] = home
        return homes[key] + r[1:]

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
            folder = self.full_remote_path(p)
            if is_wsl():
                wh = windows_home()
                if wh:
                    try:
                        prepare_windows_vscode(conf, wh)
                    except OSError as e:
                        raise ApiError(500, f"could not set up Windows SSH for VS Code: {e}")
            argv = editor + ["--remote", f"ssh-remote+{VSCODE_HOST_ALIAS}", folder]
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
        job = self.copies.get(name)
        if job and not job.done:
            raise ApiError(409, "the first copy is still running — the sync starts by itself when it ends")
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
        self._restored.wait(60)
        windows = self.live_windows()
        return {"version": VERSION, "windows": windows, "now": time.time(), "server": self.conf.to_dict(), "settings": self.store.settings,
                "projects": self.projects(), "sessions": [s.info() for s in self.sessions.values()],
                "lastTabs": self.store.data.get("lastTabs") or [], "wsl": False if self.web else is_wsl(),
                "platform": sys.platform, "web": self.web, "home": "" if self.web else os.path.expanduser("~"),
                "defaultIgnores": default_ignores(), "pc": {"on": False if self.web else bool(self.pc.cfg()["on"])}}

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
        lines.append(f"    IdentityFile {ssh_cfg_value(conf.key)}")
    lines.append(GUI_MARK_END)
    # Put our Host block first: ssh uses the first value it finds per option.
    new = "\n".join(lines) + "\n" + ("\n" + old.lstrip("\n") if old.strip() else "")
    f.write_text(new)
    if chmod:
        os.chmod(f, 0o600)


# ----------------------------------------------------------------------------- web push
# Phone/tablet notifications when Claude finishes or needs you, even with the page closed.
# Python has no elliptic-curve crypto in its standard library, so the P-256 maths for the
# VAPID signature (ES256) is here. Pushes carry NO payload (that would need AES-GCM): the
# service worker asks the app what happened (POST push/pending) and shows that.
P256_P = 2 ** 256 - 2 ** 224 + 2 ** 192 + 2 ** 96 - 1
P256_N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
P256_B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
P256_G = (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
          0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)
PUSH_HOSTS = ("fcm.googleapis.com", ".push.services.mozilla.com", ".notify.windows.com", "web.push.apple.com", ".push.apple.com")


def _ec_add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    p = P256_P
    if a[0] == b[0]:
        if (a[1] + b[1]) % p == 0:
            return None
        m = (3 * a[0] * a[0] - 3) * pow(2 * a[1], -1, p) % p
    else:
        m = (b[1] - a[1]) * pow(b[0] - a[0], -1, p) % p
    x = (m * m - a[0] - b[0]) % p
    return x, (m * (a[0] - x) - a[1]) % p


def _ec_mul(k: int, pt=P256_G):
    out = None
    while k:
        if k & 1:
            out = _ec_add(out, pt)
        pt = _ec_add(pt, pt)
        k >>= 1
    return out


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def p256_public(d: int) -> bytes:
    x, y = _ec_mul(d)
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def es256_sign(d: int, msg: bytes) -> bytes:
    z = int.from_bytes(hashlib.sha256(msg).digest(), "big")
    while True:
        k = secrets.randbelow(P256_N - 1) + 1
        r = _ec_mul(k)[0] % P256_N
        if not r:
            continue
        sig = pow(k, -1, P256_N) * (z + r * d) % P256_N
        if sig:
            return r.to_bytes(32, "big") + sig.to_bytes(32, "big")


def es256_verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    if len(pub) != 65 or len(sig) != 64:
        return False
    q = (int.from_bytes(pub[1:33], "big"), int.from_bytes(pub[33:], "big"))
    r, s_ = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    if not (0 < r < P256_N and 0 < s_ < P256_N):
        return False
    z = int.from_bytes(hashlib.sha256(msg).digest(), "big")
    w = pow(s_, -1, P256_N)
    pt = _ec_add(_ec_mul(z * w % P256_N), _ec_mul(r * w % P256_N, q))
    return pt is not None and pt[0] % P256_N == r


def push_endpoint_ok(url) -> bool:
    if not isinstance(url, str) or len(url) > 1000:
        return False
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or (h.startswith(".") and host.endswith(h)) for h in PUSH_HOSTS)


class Pusher:
    """The VAPID key of this web server (one file, made once) and sending pushes."""

    def __init__(self, keyfile: Path):
        self.keyfile = Path(keyfile)
        self._d = None

    def key(self) -> int:
        if self._d is None:
            try:
                self._d = int(json.loads(self.keyfile.read_text())["d"], 16)
            except (OSError, ValueError, KeyError, TypeError):
                self._d = secrets.randbelow(P256_N - 1) + 1
                tmp = self.keyfile.with_suffix(".tmp")
                tmp.write_text(json.dumps({"d": format(self._d, "064x")}))
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.keyfile)
        return self._d

    def public(self) -> str:
        return b64u(p256_public(self.key()))

    def auth(self, endpoint: str) -> str:
        u = urllib.parse.urlsplit(endpoint)
        head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600,
                                  "sub": "https://github.com/EsmailEmami/simabworkspace"}, separators=(",", ":")).encode())
        signing = (head + "." + claims).encode()
        return f"vapid t={head}.{claims}.{b64u(es256_sign(self.key(), signing))}, k={self.public()}"

    def send(self, endpoint: str) -> int:
        """HTTP status of the push service (201 = sent; 404/410 = this subscription is gone)."""
        req = urllib.request.Request(endpoint, data=b"", method="POST", headers={
            "Authorization": self.auth(endpoint), "TTL": "86400", "Urgency": "high", "Content-Length": "0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except (OSError, ValueError):
            return 0


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
               ("POST", "uninstall"), ("POST", "quit"), ("POST", "tunnels"), ("POST", "add-project")}
SSH_USER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}")
SSH_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}|\[?[0-9A-Fa-f:]+\]?")


def web_verify_password(conf: ServerConf, password: str) -> None:
    """Sign in with the password only (keys off) and, in the same call, put this workspace's key
    on the server. Raises ApiError when the password is wrong."""
    pub = Path(conf.key + ".pub").read_text().strip()
    marker = "SW-OK-" + secrets.token_hex(8)
    q = shlex.quote(pub)
    argv = ["ssh", *win_ssh_config(), "-p", str(conf.port), "-o", "StrictHostKeyChecking=accept-new", "-o", "PubkeyAuthentication=no",
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


class NeedCode(ApiError):
    """Sign-in needs (another) two-step code: the page shows the code box."""


def totp_code(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    h = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = h[-1] & 15
    return "%06d" % ((struct.unpack(">I", h[o:o + 4])[0] & 0x7fffffff) % 1000000)


def ua_label(ua: str) -> str:
    """'Chrome on Android' from a User-Agent — enough to tell your devices apart."""
    ua = ua or ""
    os_ = next((n for k, n in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("Windows", "Windows"),
                               ("Mac OS", "Mac"), ("CrOS", "ChromeOS"), ("Linux", "Linux")) if k in ua), "")
    br = next((n for k, n in (("Edg/", "Edge"), ("OPR/", "Opera"), ("SamsungBrowser", "Samsung Internet"), ("Firefox/", "Firefox"),
                              ("Chrome/", "Chrome"), ("Safari/", "Safari")) if k in ua), "")
    return (br + (" on " + os_ if os_ else "")) if br else (os_ or "unknown device")


# ----------------------------------------------------------------------------- share: your computer's ports on your own domain
# A user of the web version points a domain of theirs here (CDN origin = this web server) and names ports
# of a project: https://DOMAIN/<project>/<name> → that port on the computer the project runs on (onpc link:
# one `ssh -L` from here to the computer's socket on the server per computer, closed when idle; then
# POST /pc/proxy on the computer). Several projects on one domain must not mix their absolute paths
# (/assets/…, /api/…): the first visit installs /__simab/sw.js, which keeps each TAB on its project
# (sessionStorage + client id) and sends every request of that tab as /__simab/p/<project>/<name>/<path>.
# HTML pages get /__simab/shim.js first: WebSockets to the same project, cookies + localStorage per project.
# Private shares: a sign-in of the workspace on the main domain → one-time code → own cookie on DOMAIN.
SHARE_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?")
SHARE_USER_RE = re.compile(r"[a-z0-9_][a-z0-9._-]{0,31}")
SHARE_DOMAIN_RE = re.compile(r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
SHARE_COOKIE = "__simab_share"
SHARE_LAST = "__simab_last"         # the last project opened here: for a browser without service workers
SHARE_DAYS = 30
SHARE_CODE_TTL = 60
SHARE_IDLE = 600                    # an ssh link with no connection for 10 min is closed
SHARE_BODY_MAX = 64 << 20
SHARE_HTML_MAX = 20 << 20
SHARE_MAX = 50
SHARE_ASK_WAIT = 300                # a visitor waits at most 5 min for "Ask me"
SHARE_ANSWERS = ("hour", "day", "always", "deny")
SHARE_TTL = {"hour": 3600, "day": 86400, "always": 0}
SHARE_ACCESS = ("private", "ask", "public")
SHARE_ONLINE = 300                  # "online" = used the link in the last 5 min
SHARE_HOP = {"connection", "keep-alive", "proxy-connection", "proxy-authenticate", "proxy-authorization", "te",
             "trailer", "transfer-encoding", "upgrade"}
SHARE_DROP_IN = {"host", "content-length", "x-real-ip", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"}


def share_pipe(rfile, conn, other):
    """Bytes both ways between an HTTP connection (its buffered reader + socket) and another socket, until
    the other socket ends; then both are closed."""
    read = getattr(rfile, "read1", None) or rfile.read

    def up():
        try:
            while True:
                d = read(65536)
                if not d:
                    break
                other.sendall(d)
        except (OSError, ValueError):
            pass
        try:
            other.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    threading.Thread(target=up, daemon=True).start()
    try:
        while True:
            d = other.recv(65536)
            if not d:
                break
            conn.sendall(d)
    except OSError:
        pass
    for f in (lambda: conn.shutdown(socket.SHUT_RDWR), other.close):
        try:
            f()
        except OSError:
            pass


SHARE_CFG_PY = r'''
import os, sys, json
d = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "simabworkspace")
f = os.path.join(d, "share-live.json" if sys.argv[1].startswith("live-") else "share.json")
if sys.argv[1] in ("get", "live-get"):
    try:
        with open(f) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {"missing": True}
    print(json.dumps(data))
else:
    data = json.loads(sys.stdin.read())
    os.makedirs(d, exist_ok=True)
    tmp = f + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, f)
    print(json.dumps(data))
'''


def share_clean(data) -> dict:
    """share.json as read: only well-formed domains and ports."""
    data = data if isinstance(data, dict) else {}
    doms = [d for d in (data.get("domains") or []) if isinstance(d, str) and SHARE_DOMAIN_RE.fullmatch(d)][:10]
    shares, seen = [], set()
    for x in data.get("shares") or []:
        if not isinstance(x, dict):
            continue
        sl, nm = share_slug(x.get("slug")), share_slug(x.get("name"))
        try:
            port = int(x.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not SHARE_SLUG_RE.fullmatch(sl) or not SHARE_SLUG_RE.fullmatch(nm) or not 1 <= port <= 65535 or (sl, nm) in seen:
            continue
        seen.add((sl, nm))
        shares.append({"slug": sl, "name": nm, "project": str(x.get("project") or sl)[:200], "port": port,
                       "access": x.get("access") if x.get("access") in SHARE_ACCESS else "private", "tls": bool(x.get("tls"))})
    base = data.get("base") if isinstance(data.get("base"), str) and re.fullmatch(r"https://[a-z0-9.-]+(:\d+)?/[a-z0-9._-]+", data.get("base") or "") else ""
    revoke = [g for g in (data.get("revoke") or []) if isinstance(g, str) and re.fullmatch(r"[0-9a-f]{16}", g)][:100]
    return {"domains": doms, "shares": shares[:SHARE_MAX], "base": base, "revoke": revoke}


def share_edit(data: dict, sub: str, body: dict, host: str = "", hosts=()) -> dict:
    """One change from Settings → Share on your domain (POST share/domain or share/port)."""
    data = share_clean(data)
    if sub == "/domain":
        raise ApiError(404, "the apps address is set by the server's admin")
    if sub != "/port":
        raise ApiError(404, "not found")
    slug = share_slug(body.get("slug") or body.get("project"))
    name = share_slug(body.get("name"))
    if not SHARE_SLUG_RE.fullmatch(slug) or not SHARE_SLUG_RE.fullmatch(name) or slug == "__simab":
        raise ApiError(400, "use letters, digits and - in the address")
    old = body.get("old") if isinstance(body.get("old"), dict) else {}
    gone = {(slug, name), (share_slug(old.get("slug")), share_slug(old.get("name")))}
    rest = [x for x in data["shares"] if (x["slug"], x["name"]) not in gone]
    if body.get("on"):
        try:
            port = int(body.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not 1 <= port <= 65535:
            raise ApiError(400, "enter the port it runs on, like 3000")
        project = clean_str(body.get("project"), 200, "project", required=True)
        if len(rest) >= SHARE_MAX:
            raise ApiError(400, f"at most {SHARE_MAX} shared ports")
        access = body.get("access") if body.get("access") in SHARE_ACCESS else "public" if body.get("public") else "private"
        rest.append({"slug": slug, "name": name, "project": project, "port": port, "access": access, "tls": bool(body.get("tls"))})
    data["shares"] = sorted(rest, key=lambda x: (x["slug"], x["name"]))
    return data


def port_open(port: int, timeout: float = 0.5) -> bool:
    """Something listens on this port of this computer (127.0.0.1, else ::1)."""
    for fam, addr in ((socket.AF_INET, ("127.0.0.1", port)), (socket.AF_INET6, ("::1", port, 0, 0))):
        try:
            s = socket.socket(fam, socket.SOCK_STREAM)
        except OSError:
            continue
        s.settimeout(timeout)
        try:
            s.connect(addr)
            return True
        except OSError:
            pass
        finally:
            s.close()
    return False


def share_slug(s) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(s or "").lower())).strip("-")[:40].strip("-")


def share_domain_clean(s) -> str:
    s = str(s or "").strip().lower()
    if "//" in s:
        s = urllib.parse.urlsplit(s).netloc
    return s.split("/")[0].split(":")[0].rstrip(".")


def share_cookies_in(header: str, prefix: str) -> str:
    """Only the project's own cookies (their prefix taken off) go to its port."""
    out = []
    for part in (header or "").split(";"):
        k, eq, v = part.strip().partition("=")
        if eq and k.startswith(prefix) and len(k) > len(prefix):
            out.append(k[len(prefix):] + "=" + v)
    return "; ".join(out)


def share_cookie_out(value: str, prefix: str):
    """A Set-Cookie of the computer's app: the project's prefix on the name, Path=/ (the service worker's
    requests go to /__simab/p/…) and no Domain (it would say localhost)."""
    parts = (value or "").split(";")
    k, eq, v = parts[0].strip().partition("=")
    if not eq or not k.strip():
        return None
    attrs = [a.strip() for a in parts[1:] if a.strip() and a.split("=")[0].strip().lower() not in ("domain", "path")]
    return "; ".join([prefix + k.strip() + "=" + v] + attrs + ["Path=/"])


def share_location(loc: str, base: str) -> str:
    """A redirect of the computer's app, on this domain (base "" through the service worker, else the prefix)."""
    u = urllib.parse.urlsplit(loc or "")
    if u.scheme in ("http", "https", "ws", "wss") and (u.hostname or "") in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        loc = urllib.parse.urlunsplit(("", "", u.path or "/", u.query, u.fragment))
    if base and loc.startswith("/") and not loc.startswith("//"):
        loc = base + loc
    return loc


def share_inject(html: bytes, target: str, ports=None) -> bytes:
    """/__simab/shim.js as the page's first script (ports: {"6000": "project/api"} for localhost:6000 calls)."""
    p = json.dumps(ports or {}, separators=(",", ":")).replace("&", "&amp;").replace('"', "&quot;")
    tag = ('<script src="/__simab/shim.js" data-t="%s" data-p="%s"></script>' % (target, p)).encode()
    head = html[:65536]
    m = (re.search(rb"<head\b[^>]*>", head, re.I) or re.search(rb"<html\b[^>]*>", head, re.I)
         or re.search(rb"<!doctype[^>]*>", head, re.I))
    i = m.end() if m else 0
    return html[:i] + tag + html[i:]


SHARE_PAGE_CSS = """
:root{color-scheme:light dark;--bg:#f4f5f7;--card:#fff;--fg:#1d2330;--mute:#6b7280;--line:#e3e6eb;--acc:#3b82f6}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--card:#1d2026;--fg:#e8eaee;--mute:#9aa1ad;--line:#2c3038;--acc:#60a5fa}}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{display:flex;align-items:center;justify-content:center;padding:1.5em;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.card{width:100%;max-width:24em;background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:2em 1.75em;text-align:center;box-shadow:0 8px 30px rgba(0,0,0,.08)}
.brand{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mute);margin-bottom:1.25em}
.spin{width:28px;height:28px;margin:0 auto 1em;border:3px solid var(--line);border-top-color:var(--acc);
border-radius:50%;animation:r .8s linear infinite;display:none}
body.busy .spin{display:block}
@keyframes r{to{transform:rotate(360deg)}}
#m{margin:0;font-size:16px}
.acts{display:flex;flex-direction:column;gap:.6em;margin-top:1.5em}
.acts:empty{display:none}
button,.btn{display:block;width:100%;padding:.65em 1em;border-radius:8px;font:inherit;cursor:pointer;
text-decoration:none;border:1px solid var(--line);background:transparent;color:var(--fg)}
button{background:var(--acc);border-color:var(--acc);color:#fff}
button:hover{filter:brightness(1.08)}.btn:hover{background:var(--line)}
"""


def share_page(title: str, text: str, script: str = "", busy: bool = False) -> bytes:
    """The small pages of Share on your domain (asking, opening, not found …): one centred card."""
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + esc(title) + '</title><style>' + SHARE_PAGE_CSS + '</style></head>'
            '<body' + (' class="busy"' if busy else '') + '><div class="card"><div class="brand">Simab Workspace</div>'
            '<div class="spin"></div><p id="m">' + esc(text) + '</p>' + script + '</div></body></html>').encode()


SHARE_RETRY = '<p><a href="" onclick="location.reload();return false">Try again</a></p>'


def share_js(v) -> str:
    return json.dumps(v).replace("</", "<\\/")


# the entry /<project>/<name>/…: remember the project in this tab, install the service worker, open the page
SHARE_ENTRY_JS = """<script>(function(){
var t = @@T@@, to = @@TO@@ + location.hash, sw = navigator.serviceWorker, tries = 0;
try { sessionStorage.setItem('__simabT', t); } catch (e) {}
function direct() { location.replace('/__simab/p/' + t + to); }
if (!sw || !window.MessageChannel) return direct();
function claim() {
  var c = new MessageChannel();
  c.port1.onmessage = function (e) {
    if (e.data && e.data.ok) location.replace(to);
    else if (++tries < 25) setTimeout(claim, 200); else direct();
  };
  sw.controller.postMessage({ simabClaim: new URL(to, location.href).href.split('#')[0], t: t }, [c.port2]);
}
function go() { if (sw.controller) claim(); else if (++tries < 200) setTimeout(go, 50); else direct(); }
sw.register('/__simab/sw.js', { scope: '/' }).then(function () { return sw.ready; }).then(go, direct);
})();</script>"""

# "Ask me": the visitor waits here while the owner is asked on the phone / computer
SHARE_WAIT_JS = """<div class="acts"><button id="again" style="display:none">Ask again</button><a class="btn" href=@@IN@@>I am the owner — sign in</a></div>
<script>(function(){
var t = @@T@@, m = document.getElementById('m'), again = document.getElementById('again');
function say(x, busy) { m.textContent = x; document.body.classList.toggle('busy', !!busy); }
function poll(id) {
  fetch('/__simab/ask?id=' + id, { credentials: 'include' }).then(function (r) { return r.json(); }).then(function (d) {
    if (d.state === 'allowed') { say('Allowed — opening…', 1); location.reload(); }
    else if (d.state === 'denied') say('The owner did not let you in.');
    else if (d.state === 'waiting') { document.body.classList.add('busy'); setTimeout(function () { poll(id); }, 2000); }
    else { say('No answer from the owner.'); again.style.display = ''; }
  }, function () { setTimeout(function () { poll(id); }, 4000); });
}
function ask() {
  again.style.display = 'none';
  say('Asking the owner of this link to let you in…', 1);
  fetch('/__simab/ask', { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ t: t }) })
    .then(function (r) { return r.json(); }).then(function (d) { if (d.id) poll(d.id); else { say(d.error || 'Not possible now.'); again.style.display = ''; } },
      function () { say('Could not ask — check your connection.'); again.style.display = ''; });
}
again.onclick = ask;
ask();
})();</script>"""

# any other page opened in a tab: back through the entry of this tab's project
SHARE_LOST_JS = """<script>(function(){
var t = null; try { t = sessionStorage.getItem('__simabT'); } catch (e) {}
if (!t) t = @@LAST@@;            // a new tab (window.open, target=_blank): the last project opened in this browser
var p = location.pathname;
if (t && p.indexOf('/' + t + '/') !== 0 && p !== '/' + t && p.indexOf('/__simab/') !== 0) {
  location.replace('/' + t + p + location.search + location.hash);
} else { try { sessionStorage.removeItem('__simabT'); } catch (e) {} }
})();</script>"""


class ShareHub:
    """Domains, shared ports, sign-ins and ssh links of Share on your domain (one per WebGate)."""

    def __init__(self, gate, popen=None):
        self.gate, self.db = gate, gate.db
        self.popen = popen or subprocess.Popen
        self.lock = threading.Lock()
        self.links, self.key_locks, self.codes = {}, {}, {}
        self.reaper = None
        if gate.start_loops:
            threading.Thread(target=self.pull_loop, daemon=True).start()
        self.db.run("CREATE TABLE IF NOT EXISTS web_share_domains (domain TEXT PRIMARY KEY, ws TEXT NOT NULL, main TEXT NOT NULL)")
        self.db.run("CREATE TABLE IF NOT EXISTS web_shares (ws TEXT NOT NULL, slug TEXT NOT NULL, name TEXT NOT NULL, "
                    "project TEXT NOT NULL, port INTEGER NOT NULL, public INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (ws, slug, name))")
        for col in ("tls INTEGER NOT NULL DEFAULT 0", "ask INTEGER NOT NULL DEFAULT 0"):
            try:
                self.db.run("ALTER TABLE web_shares ADD COLUMN " + col)
            except sqlite3.Error:
                pass                # there already
        self.db.run("CREATE TABLE IF NOT EXISTS web_share_guests (hash TEXT PRIMARY KEY, ws TEXT NOT NULL, slug TEXT NOT NULL, "
                    "name TEXT NOT NULL, label TEXT NOT NULL, ip TEXT NOT NULL, created REAL NOT NULL, exp REAL NOT NULL, last REAL NOT NULL)")
        self.asks = {}              # id -> a visitor waiting for "Ask me"
        self.mains = {}             # ws -> the workspace's own address (where private links sign in)
        self.host = share_domain_clean(getattr(gate, "share_domain", "") or "")
        self.db.run("CREATE TABLE IF NOT EXISTS web_share_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        r = self.db.rows("SELECT v FROM web_share_meta WHERE k = 'main'")
        self.main_host = r[0][0] if r else ""
        self.db.run("CREATE TABLE IF NOT EXISTS web_share_tokens (hash TEXT PRIMARY KEY, ws TEXT NOT NULL, domain TEXT NOT NULL, exp REAL NOT NULL)")

    # the apps address (set by the server's admin: `web --share-domain`) and its users --------------
    def is_host(self, host: str) -> bool:
        return bool(self.host) and share_domain_clean(host) == self.host

    def base(self, ws: str) -> str:
        u = self.user_of(ws)
        return f"https://{self.host}/{u}" if self.host and u else ""

    def _users(self) -> dict:
        """user name → workspace (the newest one if the same name signs in to several servers)."""
        c = getattr(self, "_users_cache", None)
        if c and time.time() - c[0] < 30:
            return c[1]
        out, when = {}, {}
        for d in self.gate.root.iterdir() if self.gate.root.is_dir() else []:
            f = d / "workspace.json"
            try:
                meta = json.loads(f.read_text())
                t = f.stat().st_mtime
            except (OSError, ValueError):
                continue
            u = str(meta.get("user") or "").lower()
            if SHARE_USER_RE.fullmatch(u) and t >= when.get(u, 0):
                out[u], when[u] = d.name, t
        self._users_cache = (time.time(), out)
        return out

    def ws_of_user(self, user: str) -> str:
        return self._users().get((user or "").lower(), "")

    def user_of(self, ws: str) -> str:
        return next((u for u, w in self._users().items() if w == ws), "")

    def note_main(self, host: str):
        """The workspace's own address (where private links sign in): the Host of the signed-in web."""
        host = (host or "").lower()
        if host and host != self.main_host and not self.is_host(host):
            self.main_host = host
            self.db.run("INSERT OR REPLACE INTO web_share_meta (k, v) VALUES ('main', ?)", (host,))

    # settings (Settings → Share on your domain) ---------------------------------------------------
    def domain(self, host: str):
        d = share_domain_clean(host)
        r = self.db.rows("SELECT domain, ws, main FROM web_share_domains WHERE domain = ?", (d,)) if d else []
        return {"domain": r[0][0], "ws": r[0][1], "main": r[0][2]} if r else None

    def find(self, ws: str, slug: str, name: str, user: str = ""):
        sh = self._find(ws, slug, name)
        if sh:
            sh["user"] = user or self.user_of(ws)
        return sh

    def _find(self, ws: str, slug: str, name: str):
        r = self.db.rows("SELECT slug, name, project, port, public, tls, ask FROM web_shares WHERE ws = ? AND slug = ? AND name = ?", (ws, slug, name))
        if not r:
            return None
        return {"slug": r[0][0], "name": r[0][1], "project": r[0][2], "port": r[0][3], "public": bool(r[0][4]),
                "tls": bool(r[0][5]), "ask": bool(r[0][6]), "access": "public" if r[0][4] else "ask" if r[0][6] else "private"}

    def ports(self, ws: str, slug: str, signed: bool, user: str = "") -> list:
        """[{port, t}] of this workspace, this project first: a page that calls localhost:PORT (its API)
        gets that shared port. Private ones only for a signed-in browser."""
        out = []
        for sl, name, port, pub in self.db.rows("SELECT slug, name, port, public FROM web_shares WHERE ws = ? ORDER BY slug != ?, slug, name", (ws, slug)):
            if pub or signed:
                out.append({"port": port, "t": f"{user or self.user_of(ws)}/{sl}/{name}"})
        return out

    def data_of(self, ws: str) -> dict:
        """This workspace's domains + shared ports, as kept in the user's share.json on the server."""
        st = self.state(ws)
        return {"shares": [{k: x[k] for k in ("slug", "name", "project", "port", "access", "tls")}
                                                     for x in st["shares"]]}

    def main_of(self, ws: str) -> str:
        r = self.db.rows("SELECT main FROM web_share_domains WHERE ws = ? AND main != '' LIMIT 1", (ws,))
        return self.mains.get(ws) or (r[0][0] if r else "")

    def sync(self, ws: str, data: dict, host: str = "", check: bool = False):
        """share.json of the user (edited in the app or here) → the tables requests are answered from.
        check: only refuse what cannot be (a domain of another workspace, the workspace's own address)."""
        if host:
            self.note_main(host)
        if check:
            return
        self.db.run("DELETE FROM web_shares WHERE ws = ?", (ws,))
        for x in data.get("shares") or []:
            a = x.get("access") if x.get("access") in SHARE_ACCESS else "private"
            self.db.run("INSERT OR REPLACE INTO web_shares (ws, slug, name, project, port, public, tls, ask) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (ws, x["slug"], x["name"], x["project"], int(x["port"]), 1 if a == "public" else 0, 1 if x.get("tls") else 0,
                         1 if a == "ask" else 0))

    def pull(self, ws: str, app):
        """Read share.json of this workspace (the app may have changed it); the first time, write ours there."""
        try:
            data = app.share_data()
        except (ApiError, OSError, ValueError):
            return
        try:                                                # who asks / is let in / online: for the app
            remote_python(app.conf, SHARE_CFG_PY, ["live-put"], stdin=json.dumps(self.live(ws)).encode())
        except (ApiError, OSError, ValueError):
            pass
        if data.get("missing"):
            mine = self.data_of(ws)
            if mine["shares"]:
                try:
                    app.share_save(dict(mine, base=self.base(ws)))
                except (ApiError, OSError, ValueError):
                    pass
            return
        self.sync(ws, data)
        if data.get("revoke"):                              # Revoke from the app (it cannot reach these tables)
            for gid in data["revoke"]:
                self.revoke(ws, gid)
            data = dict(data, revoke=[])
            try:
                app.share_save(dict(data, base=self.base(ws)))
            except (ApiError, OSError, ValueError):
                pass
            try:
                remote_python(app.conf, SHARE_CFG_PY, ["live-put"], stdin=json.dumps(self.live(ws)).encode())
            except (ApiError, OSError, ValueError):
                pass
        if self.base(ws) != data.get("base", ""):           # the app shows the links with it
            try:
                app.share_save(dict(data, base=self.base(ws)))
            except (ApiError, OSError, ValueError):
                pass

    def pull_loop(self):
        while True:
            time.sleep(30)
            with self.gate.lock:
                apps = list(self.gate.apps.items())
            for ws, app in apps:
                self.pull(ws, app)

    def probe(self, ws: str, app, shares: list) -> dict:
        """{"slug/name": on | port-off | pc-off} — is the port of each running on its computer now?"""
        out = {}
        by_dev = {}
        for x in shares:
            try:
                by_dev.setdefault(self.device(app, x["project"]), []).append(x)
            except ApiError:
                out[f"{x['slug']}/{x['name']}"] = "pc-off"
        for dev, xs in by_dev.items():
            try:
                ln = self.link(ws, app, dev)
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(15)
                s.connect(ln["sock"])
                body = json.dumps({"ports": sorted({int(x["port"]) for x in xs})}).encode()
                s.sendall(("POST /pc/probe HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-PC: %s\r\nContent-Type: application/json\r\n"
                           "Content-Length: %d\r\n\r\n" % (ln["port"], ln["secret"], len(body))).encode() + body)
                data = b""
                while True:
                    d = s.recv(65536)
                    if not d:
                        break
                    data += d
                s.close()
                head, _, rest = data.partition(b"\r\n\r\n")
                opened = set((json.loads(rest.decode() or "{}") or {}).get("open") or []) if b" 200 " in head.split(b"\r\n")[0] + b" " else None
            except (ApiError, OSError, ValueError):
                opened = None
            for x in xs:
                out[f"{x['slug']}/{x['name']}"] = "pc-off" if opened is None else "on" if int(x["port"]) in opened else "port-off"
        return out

    def state(self, ws: str) -> dict:
        base = self.base(ws)
        shares = []
        for slug, name, project, port, pub, tls, ask in self.db.rows(
                "SELECT slug, name, project, port, public, tls, ask FROM web_shares WHERE ws = ? ORDER BY slug, name", (ws,)):
            shares.append({"slug": slug, "name": name, "project": project, "port": port, "public": bool(pub), "tls": bool(tls),
                           "access": "public" if pub else "ask" if ask else "private",
                           "urls": [f"{base}/{slug}/{name}"] if base else []})
        now = time.time()
        guests = [{"id": hh[:16], "t": f"{sl}/{nm}", "label": label, "ip": ip, "created": cr, "exp": exp, "last": last}
                  for hh, sl, nm, label, ip, cr, exp, last in self.db.rows(
                      "SELECT hash, slug, name, label, ip, created, exp, last FROM web_share_guests WHERE ws = ? AND (exp = 0 OR exp > ?) "
                      "ORDER BY created DESC", (ws, now))]
        return {"base": base, "shares": shares, "guests": guests}

    def api(self, ws: str, method: str, sub: str, body: dict, host: str) -> dict:
        if method == "GET" and sub == "":
            return self.state(ws)
        if method == "POST" and sub == "/port":
            data = share_edit(self.data_of(ws), sub, body, host, self.gate.hosts)
            self.sync(ws, data, host, check=True)
            self.sync(ws, data, host)
            return self.state(ws)
        if method == "GET" and sub == "/asks":
            return {"asks": self.open_asks(ws), "now": time.time()}
        if method == "POST" and sub == "/answer":
            return self.answer(ws, body.get("id"), body.get("answer"))
        if method == "POST" and sub == "/revoke":
            gid = str(body.get("id") or "")
            if not re.fullmatch(r"[0-9a-f]{16}", gid):
                raise ApiError(400, "bad request")
            self.revoke(ws, gid)
            return self.state(ws)
        raise ApiError(404, "not found")

    # sign-in of private shares ---------------------------------------------------------------------
    def code(self, ws: str, domain: str) -> str:
        now = time.time()
        with self.lock:
            for k in [k for k, v in self.codes.items() if v[2] < now]:
                del self.codes[k]
            c = secrets.token_urlsafe(24)
            self.codes[c] = (ws, domain, now + SHARE_CODE_TTL)
        return c

    def take_code(self, c: str, domain: str):
        with self.lock:
            v = self.codes.pop(c or "", None)
        if not v or v[2] < time.time() or v[1] != domain:
            return None
        tok = secrets.token_urlsafe(32)
        self.db.run("INSERT INTO web_share_tokens (hash, ws, domain, exp) VALUES (?, ?, ?, ?)",
                    (hashlib.sha256(tok.encode()).hexdigest(), v[0], domain, time.time() + SHARE_DAYS * 86400))
        return tok, v[0]

    def token_ok(self, tok: str, ws: str, domain: str) -> bool:
        if not tok or len(tok) > 200:
            return False
        r = self.db.rows("SELECT ws, domain, exp FROM web_share_tokens WHERE hash = ?", (hashlib.sha256(tok.encode()).hexdigest(),))
        return bool(r) and r[0][0] == ws and r[0][1] == domain and r[0][2] > time.time()

    # "Ask me": someone without a sign-in asks, the owner allows from the phone / computer ----------------
    def guest_ok(self, h, ws: str, sh: dict) -> str:
        """→ the guest's id (hash[:16]) when this browser was let in to this link, else ""."""
        tok = self._cookie(h, f"__simab_g.{sh['user']}.{sh['slug']}.{sh['name']}")
        if not tok or len(tok) > 200:
            return ""
        hh = hashlib.sha256(tok.encode()).hexdigest()
        r = self.db.rows("SELECT ws, slug, name, exp, last FROM web_share_guests WHERE hash = ?", (hh,))
        if not r or r[0][:3] != (ws, sh["slug"], sh["name"]) or (r[0][3] and r[0][3] < time.time()):
            return ""
        if time.time() - r[0][4] > 60:
            self.db.run("UPDATE web_share_guests SET last = ? WHERE hash = ?", (time.time(), hh))
        return hh[:16]

    def open_asks(self, ws: str) -> list:
        now = time.time()
        with self.lock:
            for k in [k for k, a in self.asks.items() if a["until"] < now - 60]:
                del self.asks[k]
            return [{k: v for k, v in a.items() if not k.startswith("_")} for a in self.asks.values()
                    if a["ws"] == ws and not a["answer"] and a["until"] > now]

    def new_ask(self, h, ws: str, sh: dict) -> dict:
        ip, ua = h._client_ip(), h.headers.get("User-Agent") or ""
        t = f"{sh['slug']}/{sh['name']}"            # as the owner reads it (the user is theirs)
        now = time.time()
        with self.lock:
            mine = [a for a in self.asks.values() if a["ws"] == ws and a["t"] == t and a["ip"] == ip and a["_ua"] == ua
                    and not a["answer"] and a["until"] > now]
            if mine:
                return mine[0]
            if len([a for a in self.asks.values() if a["ws"] == ws and not a["answer"] and a["until"] > now]) >= 20 or \
                    len([a for a in self.asks.values() if a["ip"] == ip and a["until"] > now]) >= 5:
                raise ApiError(429, "too many requests — try again in a few minutes")
            a = {"id": secrets.token_hex(6), "ws": ws, "t": t, "user": sh["user"], "project": sh["project"], "label": ua_label(ua), "ip": ip,
                 "at": now, "until": now + SHARE_ASK_WAIT, "answer": "", "_ua": ua, "_tok": ""}
            self.asks[a["id"]] = a
        threading.Thread(target=self._tell, args=(a,), daemon=True).start()
        return a

    def _tell(self, a: dict):
        """Phone / browsers of the workspace (push) and the user's computer (its own question)."""
        with self.gate.lock:
            app = self.gate._app(a["ws"])
        if app is None:
            return
        text = f"Someone wants to open {a['t']} — {a['label']} · {a['ip']}"
        if getattr(app, "pusher", None):
            subs = app._push_subs()
            if subs:
                with app.lock:
                    app.push_seq += 1
                    app.push_msgs.append({"seq": app.push_seq, "at": time.time(), "title": "Someone wants to open your link",
                                          "body": text[:200], "tag": "guest:" + a["id"], "tab": ""})
                threading.Thread(target=app._push_send, args=(subs,), daemon=True).start()
        try:
            ans = self.pc_guest(a["ws"], app, a["project"], {"op": "ask", "id": a["id"], "text": text, "until": a["until"]})
        except (ApiError, OSError, ValueError):
            return
        if ans in SHARE_ANSWERS:
            try:
                self.answer(a["ws"], a["id"], ans, tell_pc=False)
            except ApiError:
                pass

    def pc_guest(self, ws: str, app, project: str, call: dict) -> str:
        ln = self.link(ws, app, self.device(app, project))
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(SHARE_ASK_WAIT + 15)
        try:
            s.connect(ln["sock"])
            body = json.dumps(call).encode()
            s.sendall(("POST /pc/guest HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-PC: %s\r\nContent-Type: application/json\r\n"
                       "Content-Length: %d\r\n\r\n" % (ln["port"], ln["secret"], len(body))).encode() + body)
            data = b""
            while True:
                d = s.recv(65536)
                if not d:
                    break
                data += d
        finally:
            s.close()
        head, _, rest = data.partition(b"\r\n\r\n")
        return (json.loads(rest.decode("utf-8", "replace") or "{}") or {}).get("answer") or ""

    def answer(self, ws: str, aid, ans, tell_pc=True) -> dict:
        if ans not in SHARE_ANSWERS:
            raise ApiError(400, "invalid answer")
        with self.lock:
            a = self.asks.get(aid) if isinstance(aid, str) else None
            if not a or a["ws"] != ws:
                raise ApiError(404, "this request is no longer open")
            if a["answer"]:
                return {"ok": True, "answer": a["answer"]}
            a["answer"] = ans
            if ans != "deny":
                tok = secrets.token_urlsafe(32)
                a["_tok"] = tok
        if ans != "deny":
            now = time.time()
            sl, nm = a["t"].split("/")
            self.db.run("INSERT INTO web_share_guests (hash, ws, slug, name, label, ip, created, exp, last) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (hashlib.sha256(tok.encode()).hexdigest(), ws, sl, nm, a["label"], a["ip"], now,
                         now + SHARE_TTL[ans] if SHARE_TTL[ans] else 0, now))
        if tell_pc:                         # the computer's question is no longer needed
            def drop():
                with self.gate.lock:
                    app = self.gate._app(ws)
                try:
                    if app is not None:
                        self.pc_guest(ws, app, a["project"], {"op": "drop", "id": a["id"]})
                except (ApiError, OSError, ValueError):
                    pass
            threading.Thread(target=drop, daemon=True).start()
        return {"ok": True, "answer": ans}

    def _ask_api(self, h, method: str, ws: str, q: str):
        """The waiting page of a visitor: POST {t} → {id}; GET ?id= → {state} (+ its cookie once allowed)."""
        if method == "POST":
            n = int(h.headers.get("Content-Length") or 0)
            try:
                b = json.loads(h.rfile.read(min(n, 4096)).decode("utf-8") or "{}") if n else {}
            except ValueError:
                b = {}
            us, sl, nm, _ = self._split("/" + str(b.get("t") or "") + "/")
            ws = self.ws_of_user(us)
            sh = self.find(ws, sl, nm, us) if ws and sl else None
            if not sh or sh["access"] != "ask":
                return h._json(404, {"error": "this link does not ask"})
            return h._json(200, {"id": self.new_ask(h, ws, sh)["id"], "wait": SHARE_ASK_WAIT})
        aid = urllib.parse.parse_qs(q).get("id", [""])[0]
        with self.lock:
            a = self.asks.get(aid)
            if not a or a["ip"] != h._client_ip():
                return h._json(404, {"state": "gone"})
            state = "denied" if a["answer"] == "deny" else "allowed" if a["answer"] else "gone" if a["until"] < time.time() else "waiting"
            tok, a["_tok"] = a["_tok"], ""
        if state == "allowed" and tok:
            ttl = SHARE_TTL[a["answer"]] or 400 * 86400
            sec = "; Secure" if self.gate.secure else ""
            sl, nm = a["t"].split("/")
            body = json.dumps({"state": state}).encode()
            return h._send(200, body, "application/json", {"Set-Cookie":
                           f"__simab_g.{a['user']}.{sl}.{nm}={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age={ttl}{sec}"})
        return h._json(200, {"state": state})

    def _wait_page(self, h, sd: dict, sh: dict, to: str, sw: bool):
        t = f"{sh['user']}/{sh['slug']}/{sh['name']}"
        js = SHARE_WAIT_JS.replace("@@T@@", share_js(t)).replace("@@IN@@", share_js(self._signin_url(sd, f"/{t}{to}")))
        return h._send(200, share_page("Asking…", "Asking the owner of this link to let you in…", js, busy=True), MIME[".html"])

    # the way to the computer --------------------------------------------------------------------------
    def device(self, app, project: str) -> str:
        info = app.pc_devices(cached=True)
        on = [d for d in info.get("devices") or [] if d.get("online")]
        pin = (info.get("pins") or {}).get(project)
        for d in on:
            if d.get("id") == pin:
                return d["id"]
        if on:
            return on[0]["id"]
        raise ApiError(503, "your computer is not connected — open Simab Workspace on it")

    def link(self, ws: str, app, dev: str) -> dict:
        """The local socket of an `ssh -L` to this computer's socket on the server (started when needed)."""
        key = (ws, dev)
        with self.lock:
            kl = self.key_locks.setdefault(key, threading.Lock())
        with kl:
            with self.lock:
                ln = self.links.get(key)
                if ln and ln["proc"].poll() is None and os.path.exists(ln["sock"]):
                    ln["used"] = time.time()
                    return ln
            if ln:
                self._drop(key)
            info = remote_python(app.conf, PC_ADMIN_PY, ["link", dev], multiplex=False, timeout=20)
            info = info if isinstance(info, dict) else {}
            if not info.get("secret") or not info.get("sock"):
                raise ApiError(503, "this computer is not set up on the server")
            d = self.gate.root / "share"
            d.mkdir(mode=0o700, exist_ok=True)
            local = str(d / (hashlib.sha256(f"{ws}/{dev}".encode()).hexdigest()[:16] + ".sock"))
            try:
                os.unlink(local)
            except OSError:
                pass
            argv = ssh_base(app.conf, multiplex=False, batch=True, forwards=True) + [
                "-N", "-T", "-o", "StreamLocalBindUnlink=yes", "-L", f"{local}:{info['sock']}", app.conf.target]
            proc = self.popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
            end = time.time() + 15
            while not os.path.exists(local):
                if proc.poll() is not None or time.time() > end:
                    pc_kill(proc)
                    raise ApiError(503, "could not reach the server")
                time.sleep(0.05)
            ln = {"proc": proc, "sock": local, "secret": info["secret"], "port": int(info.get("port") or 0),
                  "used": time.time(), "active": 0}
            with self.lock:
                self.links[key] = ln
                if self.reaper is None:
                    self.reaper = threading.Thread(target=self._reap_loop, daemon=True)
                    self.reaper.start()
            return ln

    def _drop(self, key):
        with self.lock:
            ln = self.links.pop(key, None)
        if ln:
            pc_kill(ln["proc"])
            try:
                os.unlink(ln["sock"])
            except OSError:
                pass

    def reap(self, now=None):
        now = now or time.time()
        with self.lock:
            old = [k for k, v in self.links.items() if v["active"] <= 0 and now - v["used"] > SHARE_IDLE]
        for k in old:
            self._drop(k)

    def _reap_loop(self):
        while True:
            time.sleep(60)
            self.reap()

    def dial(self, ws: str, app, share: dict, guest: str = ""):
        """→ (socket to the port of the computer, link) after the computer said yes."""
        ln = self.link(ws, app, self.device(app, share["project"]))
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(PC_ASK_WAIT + 15)       # the first time, the computer asks its user
        try:
            s.connect(ln["sock"])
            body = json.dumps({"port": share["port"], "project": share["project"], "guest": guest}).encode()
            s.sendall(("POST /pc/proxy HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-PC: %s\r\nContent-Type: application/json\r\n"
                       "Content-Length: %d\r\n\r\n" % (ln["port"], ln["secret"], len(body))).encode() + body)
            head = b""
            while not head.endswith(b"\r\n\r\n") and len(head) < 16384:
                c = s.recv(1)
                if not c:
                    break
                head += c
        except OSError:
            s.close()
            raise ApiError(503, "your computer did not answer")
        first = head.split(b"\r\n", 1)[0].split()
        if len(first) >= 2 and first[1] == b"200":
            s.settimeout(None)
            return s, ln
        why = ""
        try:
            s.settimeout(3)
            why = json.loads(s.recv(65536).decode("utf-8", "replace") or "{}").get("error") or ""
        except (OSError, ValueError, AttributeError):
            pass
        s.close()
        if len(first) >= 2 and first[1] == b"404":          # an older app there: no /pc/proxy yet
            raise ApiError(502, f"update Simab Workspace on your computer to {VERSION} or newer, then open the link again")
        raise ApiError(502, why or "your computer refused the connection")

    # requests on a share domain ---------------------------------------------------------------------
    def handle(self, h, method: str):
        """A request on the apps address: https://APPS/<user>/<project>/<name>/… — nothing of the workspace
        itself is served here (no sign-in, no API, no files): only the shared ports."""
        url = urllib.parse.urlsplit(h.path)
        path, q = url.path, url.query
        hd = h.headers
        domain = self.host
        nav = hd.get("Sec-Fetch-Mode") == "navigate" or (not hd.get("Sec-Fetch-Mode") and method == "GET"
                                                         and "text/html" in (hd.get("Accept") or ""))
        try:
            if path == "/__simab/sw.js" and method == "GET":
                return h._send(200, (STATIC / "share-sw.js").read_bytes(), MIME[".js"], {"Service-Worker-Allowed": "/"})
            if path == "/__simab/shim.js" and method == "GET":
                return h._send(200, (STATIC / "share-shim.js").read_bytes(), MIME[".js"], {"Cache-Control": "max-age=300"})
            if path == "/__simab/ask" and method in ("GET", "POST"):
                return self._ask_api(h, method, "", q)
            if path == "/__simab/log" and method == "POST":           # a shared page's script error (share-shim.js)
                now = time.time()
                self._logs = [x for x in getattr(self, "_logs", []) if now - x < 60]
                if len(self._logs) < 60:
                    self._logs.append(now)
                    try:
                        b = json.loads(h.rfile.read(min(int(hd.get("Content-Length") or 0), 4096)).decode("utf-8", "replace") or "{}")
                    except ValueError:
                        b = {}
                    clean = lambda v: re.sub(r"[\x00-\x1f]", " ", str(v or ""))[:500]
                    log_line(f"share page {clean(b.get('t'))[:120]} {clean(b.get('at'))[:200]}: {clean(b.get('msg'))}")
                return h._send(204, b"", "text/plain")
            if path == "/__simab/auth" and method == "GET":
                qs = urllib.parse.parse_qs(q)
                got = self.take_code((qs.get("c") or [""])[0], domain)
                to = (qs.get("to") or ["/"])[0]
                to = to if to.startswith("/") and not to.startswith("//") else "/"
                if not got:
                    return h._send(403, share_page("Sign-in", "This sign-in link was used already or is too old — open the link again."),
                                   MIME[".html"])
                tok, ws = got
                sec = "; Secure" if self.gate.secure else ""
                return h._send(302, b"", "text/plain", {"Location": to, "Set-Cookie":
                               f"{SHARE_COOKIE}.{self.user_of(ws)}={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SHARE_DAYS * 86400}{sec}"})
            sw_req = path.startswith("/__simab/p/")
            if path == "/__simab/ports" and method == "GET":
                user, slug, _, _ = self._split("/" + urllib.parse.parse_qs(q).get("t", [""])[0] + "/")
            elif sw_req:
                user, slug, name, sub = self._split(path[len("/__simab/p"):])
            else:
                user, slug, name, sub = self._split(path)
            ws = self.ws_of_user(user) if user else ""
            if not ws:
                if nav and not sw_req:
                    return h._send(200, share_page("Not found", "Nothing is shared at this address.", self._lost_js(h)), MIME[".html"])
                last = self._cookie(h, SHARE_LAST)                  # a browser without service workers
                lu, ls, ln_, _ = self._split("/" + last + "/")
                lws = self.ws_of_user(lu) if lu else ""
                sh = self.find(lws, ls, ln_, lu) if lws else None
                if sh and not sw_req:
                    with self.gate.lock:
                        app = self.gate._app(lws)
                    if app is not None:
                        sd = {"ws": lws, "domain": domain, "user": lu}
                        return self._serve(h, method, lws, domain, sd, app, sh, path, q, f"/__simab/p/{last}", False)
                return h._send(404, share_page("Not found", "Nothing is shared at this address."), MIME[".html"])
            sd = {"ws": ws, "domain": domain, "user": user}
            if path == "/__simab/ports":
                signed = self.token_ok(self._cookie(h, f"{SHARE_COOKIE}.{user}"), ws, domain)
                return h._json(200, self.ports(ws, slug, signed, user))
            with self.gate.lock:
                app = self.gate._app(ws)
            sh = self.find(ws, slug, name, user) if app is not None and slug else None
            if sw_req:
                if not sh:
                    return h._send(404, share_page("Not found", "This link is not shared (any more)."), MIME[".html"])
                if hd.get("X-Simab-SW") == "1":
                    swnav = hd.get("X-Simab-Nav") == "1"
                    u2, s2, n2, sub2 = self._split(sub)
                    w2 = self.ws_of_user(u2) if swnav and u2 else ""
                    other = self.find(w2, s2, n2, u2) if w2 else None
                    if other:
                        return self._entry(h, w2, domain, dict(sd, ws=w2, user=u2), other, sub2, q, sw=True)
                    return self._serve(h, method, ws, domain, sd, app, sh, sub, q, "", swnav, sw=True)
                return self._serve(h, method, ws, domain, sd, app, sh, sub, q, f"/__simab/p/{user}/{slug}/{name}", nav)
            if sh:
                if nav and method == "GET":
                    return self._entry(h, ws, domain, sd, sh, sub, q)
                return self._serve(h, method, ws, domain, sd, app, sh, sub, q, f"/{user}/{slug}/{name}", False)
            if nav:
                return h._send(200, share_page("Not found", "Nothing is shared at this address.", self._lost_js(h)), MIME[".html"])
            return h._send(404, b"not found", "text/plain")
        except ApiError as e:
            if nav or hd.get("X-Simab-Nav") == "1":         # also a page the service worker fetches for the tab
                return h._send(e.status, share_page("Not available", e.message, SHARE_RETRY), MIME[".html"])
            return h._send(e.status, e.message.encode(), "text/plain; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _lost_js(self, h) -> str:
        u, sl, nm, _ = self._split("/" + self._cookie(h, SHARE_LAST) + "/")
        return SHARE_LOST_JS.replace("@@LAST@@", share_js(f"{u}/{sl}/{nm}" if u else None))

    @staticmethod
    def _split(path: str):
        """/<user>/<project>/<name>/rest → (user, project, name, "/rest"); anything else → empty."""
        parts = path.split("/", 4)
        if len(parts) < 4 or not SHARE_USER_RE.fullmatch(parts[1]) or not SHARE_SLUG_RE.fullmatch(parts[2]) \
                or not SHARE_SLUG_RE.fullmatch(parts[3]):
            return "", "", "", ""
        return parts[1], parts[2], parts[3], "/" + (parts[4] if len(parts) > 4 else "")

    @staticmethod
    def _cookie(h, name: str) -> str:
        for part in (h.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v.strip()
        return ""

    def _allowed(self, h, ws, domain, sh) -> str:
        """"" (not allowed) | you (signed in) | guest (let in with Ask me) | anyone (public link)"""
        if self.token_ok(self._cookie(h, f"{SHARE_COOKIE}.{sh['user']}"), ws, domain):
            return "you"
        gid = self.guest_ok(h, ws, sh) if sh.get("ask") else ""
        if gid:
            h.share_gid = gid
            return "guest"
        return "anyone" if sh["public"] else ""

    def saw(self, h, ws: str, sh: dict, who: str):
        """Who opens this link now (Settings / sidebar: "online")."""
        key = (ws, sh["slug"], sh["name"], h._client_ip(), ua_label(h.headers.get("User-Agent") or ""), who,
               getattr(h, "share_gid", "") if who == "guest" else "")
        now = time.time()
        with self.lock:
            seen = self.__dict__.setdefault("seen", {})
            first = seen.get(key, (now, 0))[0]
            seen[key] = (first if now - seen.get(key, (0, 0))[1] < SHARE_ONLINE else now, now)

    def live(self, ws: str) -> dict:
        """Open requests (Ask me), people let in, and who used each link in the last minutes."""
        now = time.time()
        with self.lock:
            seen = self.__dict__.setdefault("seen", {})
            for k in [k for k, v in seen.items() if now - v[1] > SHARE_ONLINE]:
                del seen[k]
            active = [{"t": f"{k[1]}/{k[2]}", "ip": k[3], "label": k[4], "who": k[5], "since": v[0], "last": v[1]}
                      for k, v in seen.items() if k[0] == ws]
        active.sort(key=lambda x: -x["last"])
        return {"asks": self.open_asks(ws), "guests": self.state(ws)["guests"], "active": active, "at": now}

    def revoke(self, ws: str, gid: str):
        """At once: no more requests, open connections (WebSockets…) closed, gone from "online"."""
        if not re.fullmatch(r"[0-9a-f]{16}", gid or ""):
            return
        self.db.run("DELETE FROM web_share_guests WHERE ws = ? AND substr(hash, 1, 16) = ?", (ws, gid))
        with self.lock:
            conns = list(self.__dict__.setdefault("conns", {}).pop(gid, set()))
            seen = self.__dict__.setdefault("seen", {})
            for k in [k for k in seen if k[0] == ws and k[6] == gid]:
                del seen[k]
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _signin_url(self, sd: dict, to: str) -> str:
        scheme = "https" if self.gate.secure else "http"
        return (f"{scheme}://{self.main_host}/__simab/share-auth?u={urllib.parse.quote(sd['user'])}"
                f"&to={urllib.parse.quote(to)}")

    def _entry(self, h, ws, domain, sd, sh, sub, q, sw=False):
        t = f"{sh['user']}/{sh['slug']}/{sh['name']}"
        to = sub + ("?" + q if q else "")
        if not self._allowed(h, ws, domain, sh):
            if sh.get("ask"):
                return self._wait_page(h, sd, sh, to, sw)
            loc = self._signin_url(sd, f"/{t}{to}")
            return h._send(200 if sw else 302, b"", "text/plain", {"X-Simab-Location" if sw else "Location": loc})
        js = SHARE_ENTRY_JS.replace("@@T@@", share_js(t)).replace("@@TO@@", share_js(to))
        sec = "; Secure" if self.gate.secure else ""
        return h._send(200, share_page("Opening…", "Opening…", js, busy=True), MIME[".html"],
                       {"Set-Cookie": f"{SHARE_LAST}={t}; Path=/; HttpOnly; SameSite=Lax{sec}"})

    def _serve(self, h, method, ws, domain, sd, app, sh, sub, q, base, nav, sw=False):
        t = f"{sh['user']}/{sh['slug']}/{sh['name']}"
        who = self._allowed(h, ws, domain, sh)
        if who:
            self.saw(h, ws, sh, who)
        if not who:
            if nav and sh.get("ask"):
                return self._wait_page(h, sd, sh, sub + ("?" + q if q else ""), sw)
            if nav:
                loc = self._signin_url(sd, f"/{t}{sub}" + ("?" + q if q else ""))
                return h._send(200 if sw else 302, b"", "text/plain", {"X-Simab-Location" if sw else "Location": loc})
            return h._send(401, b"sign in to the workspace first", "text/plain")
        hd = h.headers
        if "chunked" in (hd.get("Transfer-Encoding") or "").lower():
            return h._send(411, b"length required", "text/plain")
        n = int(hd.get("Content-Length") or 0)
        if n > SHARE_BODY_MAX:
            return h._send(413, b"too large", "text/plain")
        body = h.rfile.read(n) if n else b""
        upgrade = (hd.get("Upgrade") or "").lower() == "websocket"
        origin = ("https://" if self.gate.secure else "http://") + (hd.get("Host") or "")
        prefix = f"{sh['user']}.{sh['slug']}."      # cookies of localhost are the same on every port: one jar per project
        page = sh
        fu, fs, fn, _ = self._split("/" + (hd.get("X-Simab-From") or "") + "/")
        if fu == sh["user"] and fs and (fs, fn) != (sh["slug"], sh["name"]):
            page = self.find(ws, fs, fn, fu) or sh        # the page that calls this port (its panel)
        local = f"{'https' if page.get('tls') else 'http'}://localhost:{page['port']}"
        lines = [f"{method} {sub}{'?' + q if q else ''} HTTP/1.1", f"Host: localhost:{sh['port']}"]
        for k, v in hd.items():
            lk = k.lower()
            if lk in SHARE_HOP or lk in SHARE_DROP_IN or lk.startswith("x-simab-"):
                continue
            if lk == "cookie":
                v = share_cookies_in(v, prefix)
                if not v:
                    continue
            elif lk == "origin" and v == origin:
                v = local
            elif lk == "referer" and (v == origin or v.startswith(origin + "/")):
                v = local + v[len(origin):]
            elif lk == "accept-encoding" and nav:
                v = "identity"
            lines.append(f"{k}: {v}")
        lines += ["Connection: Upgrade", "Upgrade: websocket"] if upgrade else ["Connection: close"]
        if body or method in ("POST", "PUT", "PATCH"):
            lines.append(f"Content-Length: {len(body)}")
        lines += [f"X-Forwarded-Proto: {origin.split(':')[0]}", f"X-Forwarded-Host: {hd.get('Host') or ''}"]
        gid = getattr(h, "share_gid", "") if who == "guest" else ""
        s, ln = self.dial(ws, app, sh, gid)
        with self.lock:
            ln["active"] += 1
            if gid:                         # closed at once when revoked
                self.__dict__.setdefault("conns", {}).setdefault(gid, set()).update({s, h.connection})
        try:
            if sh.get("tls"):                # the port speaks HTTPS (its own certificate: not checked, it is localhost)
                import ssl
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                try:
                    s = ctx.wrap_socket(s, server_hostname="localhost")
                except (ssl.SSLError, OSError):
                    raise ApiError(502, "this port does not speak HTTPS — turn HTTPS off for it")
            s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8", "surrogateescape") + body)
            if upgrade:
                return self._upgrade(h, s)
            pmap = {}
            if nav:
                signed = self.token_ok(self._cookie(h, f"{SHARE_COOKIE}.{sh['user']}"), ws, domain)
                for x in self.ports(ws, sh["slug"], signed, sh["user"]):
                    pmap.setdefault(str(x["port"]), x["t"])
            return self._answer(h, method, s, t, prefix, base, nav, sw, pmap, bool(sh.get("tls")))
        finally:
            with self.lock:
                ln["active"] -= 1
                ln["used"] = time.time()
                if gid:
                    self.__dict__.setdefault("conns", {}).get(gid, set()).difference_update({s, h.connection})
            try:
                s.close()
            except OSError:
                pass

    def _upgrade(self, h, s):
        h.close_connection = True
        head = b""
        while not head.endswith(b"\r\n\r\n") and len(head) < 65536:
            c = s.recv(1)
            if not c:
                break
            head += c
        h.wfile.write(head)
        h.wfile.flush()
        share_pipe(h.rfile, h.connection, s)

    def _answer(self, h, method, s, t, prefix, base, nav, sw, pmap=None, tls=False):
        import http.client
        h.close_connection = True
        resp = http.client.HTTPResponse(s, method=method)
        try:
            resp.begin()
        except (http.client.HTTPException, OSError):
            raise ApiError(502, "this port did not answer like a web server" + (
                "" if tls else " — if it speaks https://, turn on “The port speaks HTTPS” (Edit in its Share window)"))
        status, headers = resp.status, resp.getheaders()
        ctype = resp.getheader("Content-Type") or ""
        enc = (resp.getheader("Content-Encoding") or "").lower()
        inject = (nav and ctype.lower().startswith("text/html") and method != "HEAD" and status not in (204, 304)
                  and enc in ("", "identity", "gzip"))
        out = []
        loc = None
        for k, v in headers:
            lk = k.lower()
            if lk in SHARE_HOP or lk == "content-length":
                continue
            if lk == "set-cookie":
                v = share_cookie_out(v, prefix)
                if not v:
                    continue
            elif lk == "location":
                loc = share_location(v, base)
                continue
            elif inject and (lk.startswith("content-security-policy") or lk == "content-encoding"):
                continue
            out.append((k, v))
        if loc is not None and 300 <= status < 400 and sw:      # the service worker turns it into a redirect
            h.send_response(200)
            for k, v in out:
                h.send_header(k, v)
            h.send_header("X-Simab-Location", loc)
            h.send_header("Content-Length", "0")
            h.end_headers()
            return
        if loc is not None:
            out.append(("Location", loc))
        if inject:
            data = resp.read(SHARE_HTML_MAX + 1)
            if enc == "gzip":
                import gzip
                try:
                    data = gzip.decompress(data)
                except (OSError, EOFError):
                    pass
            data = share_inject(data, t, pmap)
            h.send_response(status)
            for k, v in out:
                h.send_header(k, v)
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            h.wfile.write(data)
            return
        h.send_response(status)
        for k, v in out:
            h.send_header(k, v)
        if resp.length is not None and not resp.chunked:
            h.send_header("Content-Length", str(resp.length))
        h.end_headers()
        if method == "HEAD" or status in (204, 304) or 100 <= status < 200:
            return
        read = getattr(resp, "read1", None) or resp.read
        while True:
            d = read(65536)
            if not d:
                break
            h.wfile.write(d)
            h.wfile.flush()


class WebGate:
    SESSION_DAYS = 30
    IDLE_DAYS = 7
    FAIL_WINDOW = 900
    FAILS_PER_IP = 5
    FAILS_ALL = 30

    def __init__(self, root: Path, hosts=None, trust_proxy=False, secure=True, verify=None, start_loops=True, cert_download="",
                 share_domain=""):
        self.share_domain = share_domain        # the apps address: https://APPS/<user>/<project>/<name> (Share)
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
        self.pusher = Pusher(self.root / "vapid.json")
        self.share = ShareHub(self)
        self.db.run("CREATE TABLE IF NOT EXISTS web_totp (ws TEXT PRIMARY KEY, secret TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, last_step INTEGER NOT NULL DEFAULT 0)")
        for col in ("ua TEXT NOT NULL DEFAULT ''", "created REAL NOT NULL DEFAULT 0"):
            try:
                self.db.run("ALTER TABLE web_sessions ADD COLUMN " + col)
            except sqlite3.OperationalError:
                pass                              # already there

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

    def login(self, ip: str, body: dict, ua: str = "") -> str:
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
        if self.totp_on(wsid):
            code = str(body.get("code") or "").replace(" ", "")
            if not code:
                raise NeedCode(401, "enter the 6-digit code from your authenticator app")
            if not self.totp_check(wsid, code):
                self._failed(ip)
                raise NeedCode(401, "wrong code — check the time on your phone and try the newest code")
        (d / "workspace.json").write_text(json.dumps({"user": user, "host": host, "port": port}))
        tok = secrets.token_urlsafe(32)
        now = time.time()
        with self.db.tx() as c:
            c.execute("DELETE FROM web_sessions WHERE exp < ? OR last < ?", (now, now - self.IDLE_DAYS * 86400))
            c.execute("INSERT INTO web_sessions (hash, ws, exp, last, ip, ua, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (self._hash(tok), wsid, now + self.SESSION_DAYS * 86400, now, ip, (ua or "")[:300], now))
        log_line(f"web: {user}@{host}:{port} signed in from {ip}")
        return tok

    # ---- two-step sign-in (TOTP, RFC 6238) and the devices signed in to a workspace
    def ws_of(self, tok: str) -> str:
        r = self.db.rows("SELECT ws FROM web_sessions WHERE hash = ?", (self._hash(tok or ""),))
        return r[0][0] if r else ""

    def totp_on(self, ws: str) -> bool:
        r = self.db.rows("SELECT enabled FROM web_totp WHERE ws = ?", (ws,))
        return bool(r and r[0][0])

    def totp_check(self, ws: str, code: str, at: float = None) -> bool:
        r = self.db.rows("SELECT secret, last_step FROM web_totp WHERE ws = ?", (ws,))
        if not r or not re.fullmatch(r"\d{6}", code or ""):
            return False
        secret, last = r[0]
        step = int((at or time.time()) // 30)
        for s in (step, step - 1, step + 1):
            if s > last and hmac.compare_digest(totp_code(secret, s), code):
                self.db.run("UPDATE web_totp SET last_step = ? WHERE ws = ?", (s, ws))   # a code works once
                return True
        return False

    def totp_setup(self, ws: str, label: str) -> dict:
        if self.totp_on(ws):
            raise ApiError(409, "two-step sign-in is already on")
        secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        self.db.run("INSERT OR REPLACE INTO web_totp (ws, secret, enabled, last_step) VALUES (?, ?, 0, 0)", (ws, secret))
        uri = "otpauth://totp/" + urllib.parse.quote("Simab Workspace:" + label) + "?secret=" + secret + "&issuer=Simab%20Workspace"
        return {"secret": secret, "uri": uri}

    def totp_enable(self, ws: str, code: str, on: bool):
        if not self.totp_check(ws, str(code or "").replace(" ", "")):
            raise ApiError(400, "wrong code")
        if on:
            self.db.run("UPDATE web_totp SET enabled = 1 WHERE ws = ?", (ws,))
        else:
            self.db.run("DELETE FROM web_totp WHERE ws = ?", (ws,))

    def devices(self, ws: str, tok: str) -> list:
        me = self._hash(tok or "")
        rows = self.db.rows("SELECT hash, ip, ua, created, last FROM web_sessions WHERE ws = ? ORDER BY last DESC", (ws,))
        return [{"id": h[:16], "ip": ip, "device": ua_label(ua), "created": created, "last": last, "current": h == me}
                for h, ip, ua, created, last in rows]

    def sign_out_device(self, ws: str, tok: str, which: str):
        me = self._hash(tok or "")
        for h, in self.db.rows("SELECT hash FROM web_sessions WHERE ws = ?", (ws,)):
            if h != me and (which == "others" or (isinstance(which, str) and len(which) == 16 and h.startswith(which))):
                self.db.run("DELETE FROM web_sessions WHERE hash = ?", (h,))

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
        app.pusher = self.pusher
        app.share_hook = lambda data, host, check, ws=wsid: self.share.sync(ws, data, host, check)
        app.share_probe = lambda shares, ws=wsid, a=app: self.share.probe(ws, a, shares)
        app.share_live = lambda ws=wsid: self.share.live(ws)
        app.share_revoke = lambda gid, ws=wsid: self.share.revoke(ws, gid)
        self.apps[wsid] = app
        if self.start_loops:
            threading.Thread(target=self.share.pull, args=(wsid, app), daemon=True).start()
        app.restore_live()
        if self.start_loops:
            threading.Thread(target=app.git_watch_loop, daemon=True).start()
            threading.Thread(target=app.docker_watch_loop, daemon=True).start()
            threading.Thread(target=app.share_loop, daemon=True).start()
            threading.Thread(target=app.pc_watch_loop, daemon=True).start()
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
    ap.add_argument("--share-domain", default="", help="address for shared ports of the users' computers: https://THIS/<user>/<project>/<name> "
                                                       "(its own domain, never the workspace's: the apps must not reach the workspace)")
    a = ap.parse_args(args)
    install_crash_logging()
    root = Path(a.data or (config_dir() / "web")).expanduser()
    gate = WebGate(root, [h.strip() for h in a.hosts.split(",")], trust_proxy=a.proxy, secure=True, share_domain=a.share_domain)
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

    def _account(self, method: str, sub: str) -> dict:
        """Web sign-in settings of this workspace: two-step codes and signed-in devices."""
        gate, tok = self.gate, self._cookie()
        ws = gate.ws_of(tok)
        if not ws:
            raise ApiError(401, "sign in")
        if method == "GET" and sub == "":
            return {"totp": gate.totp_on(ws), "devices": gate.devices(ws, tok)}
        b = self._body()
        if method == "POST" and sub == "/totp/setup":
            c = self.app.conf
            return gate.totp_setup(ws, f"{c.user}@{c.host}")
        if method == "POST" and sub == "/totp":
            gate.totp_enable(ws, b.get("code"), bool(b.get("on")))
            log_line(f"web: two-step sign-in {'on' if b.get('on') else 'off'} for {self.app.conf.user}@{self.app.conf.host}")
            return {"totp": gate.totp_on(ws)}
        if sub in ("/share/domain", "/share/port"):          # kept in share.json on the server (app + web)
            return self.app.share_api("POST", sub[len("/share"):], b, self.headers.get("Host") or "")
        if sub == "/share" or sub.startswith("/share/"):
            return gate.share.api(ws, method, sub[len("/share"):], b, self.headers.get("Host") or "")
        if method == "POST" and sub == "/signout":
            gate.sign_out_device(ws, tok, b.get("id"))
            return {"devices": gate.devices(ws, tok)}
        raise ApiError(404, "not found")

    def _share_auth(self, query: dict):
        """A private shared port asks: is this browser signed in to the workspace that owns the domain?"""
        gate = self.gate
        ws = gate.ws_of(self._cookie())
        if not ws:
            return self._send(401, share_page("Sign in", "Sign in to the workspace in this browser first, then open the link again.",
                                              '<div class="acts"><a class="btn" href="/">Sign in</a></div>'), MIME[".html"])
        if not gate.share.host or gate.share.ws_of_user((query.get("u") or [""])[0]) != ws:
            return self._send(403, share_page("Not yours", "This link belongs to another user — sign in as that user."), MIME[".html"])
        to = (query.get("to") or ["/"])[0]
        to = to if to.startswith("/") and not to.startswith("//") else "/"
        scheme = "https" if gate.secure else "http"
        host = gate.share.host
        return self._send(302, b"", "text/plain", {"Location": f"{scheme}://{host}/__simab/auth?c="
                                                   f"{gate.share.code(ws, host)}&to={urllib.parse.quote(to)}"})

    def _set_cookie(self, value: str, max_age: int) -> dict:
        secure = "; Secure" if self.gate.secure else ""
        return {"Set-Cookie": f"{WEB_COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"}

    def _pair(self, method: str, code: str):
        p = pair_take(code)
        if not p:
            raise ApiError(404, "this link was used already or is older than 10 minutes — make a new one")
        c = p["conf"]
        if method == "GET":
            return self._json(200, {"user": c.user, "host": c.host, "port": c.port})
        if method != "POST":
            raise ApiError(405, "method not allowed")
        key = self._body().get("pubkey")
        if not isinstance(key, str) or not PUBKEY_RE.fullmatch(key.strip()):
            raise ApiError(400, "not an ssh public key")
        if not pair_take(code, consume=True):
            raise ApiError(404, "this link was used already")
        remote_python(c, ADD_KEY_PY, [], stdin=key.strip().encode())
        return self._json(200, {"ok": True})

    def _web_dispatch(self, method: str):
        url = urllib.parse.urlsplit(self.path)
        path, query = url.path, urllib.parse.parse_qs(url.query)
        gate = self.gate
        try:
            if gate.share.is_host(self.headers.get("Host") or ""):    # the apps address: shared ports only
                return gate.share.handle(self, method)
            if method not in ("GET", "POST", "DELETE"):
                raise ApiError(405, "method not allowed")
            if path == "/__simab/share-auth" and method == "GET":
                return self._share_auth(query)
            m = re.fullmatch(r"/pair/([A-Za-z0-9_-]{20,64})", path)
            if m:                       # a new computer with a one-time code (no sign-in: the code is the secret)
                return self._pair(method, m.group(1))
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
                try:
                    tok = gate.login(self._client_ip(), self._body(), self.headers.get("User-Agent") or "")
                except NeedCode as e:
                    return self._json(e.status, {"error": e.message, "code": True})
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
                gate.share.note_main(self.headers.get("Host") or "")
                if path == "/ws":
                    return self._websocket(query)
                if self.headers.get("X-SW") != "1":
                    raise ApiError(403, "forbidden")
                if path.startswith("/api/account"):
                    return self._json(200, self._account(method, path[len("/api/account"):]))
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
        return host in (f"127.0.0.1:{port}", f"localhost:{port}") or host in self.app.alt_hosts

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

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def _dispatch(self, method: str):
        self._cached_body = None
        if self.gate is not None:
            return self._web_dispatch(method)
        if method not in ("GET", "POST", "DELETE"):
            return self._send(405, b"method not allowed", "text/plain")
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        url = urllib.parse.urlsplit(self.path)
        path, query = url.path, urllib.parse.parse_qs(url.query)
        try:
            if method == "POST" and path == "/pc/run":         # onpc on the server, through the ssh link
                return self.app.pc.serve_run(self)
            if method == "POST" and path == "/pc/sftp":        # sshfs on the server: a Local only project's files
                return self.app.pc.serve_sftp(self)
            if method == "POST" and path == "/pc/fs":          # Claude's Grep / Glob of a Local only project
                return self.app.pc.serve_fs(self)
            if method == "POST" and path == "/pc/fsmux":       # the same, on one connection kept open
                return self.app.pc.serve_fsmux(self)
            if method == "POST" and path == "/pc/proxy":       # Share on your domain: one connection to a port here
                return self.app.pc.serve_proxy(self)
            if method == "POST" and path == "/pc/probe":       # Share on your domain: which shared ports run here
                return self.app.pc.serve_probe(self)
            if method == "POST" and path == "/pc/guest":       # Share on your domain: someone asks to open a link
                return self.app.pc.serve_guest(self)
            if method == "POST" and path == "/pc/term":        # a terminal of this computer in the web version
                return self.app.pc.serve_term(self)
            if method == "POST" and path == "/pc/remote":      # the web version / phone: questions and answers
                return self.app.pc.serve_remote(self)
            if method == "POST" and path == "/pc/bg":          # onpc --bg / --logs / --stop / --ps
                return self.app.pc.serve_bg(self)
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
            cmds = [c for c in cmds if not (isinstance(c, dict) and c.get("devName"))]     # another computer's
            for c in cmds:
                if isinstance(c, dict) and not c.get("shared"):
                    c["where"] = "server"         # shared ones keep theirs: the installed app runs the local ones
            self._cached_body = {"commands": cmds}
        if app.web and route == ("POST", "manual"):
            b = dict(self._body(), local="")
            self._cached_body = b
        if route == ("POST", "client-error"):
            return app.client_error(self._body())
        if route == ("GET", "ping"):
            return {"ok": True, "version": VERSION, "pid": os.getpid()}
        if route == ("GET", "chat-search"):
            text = (query.get("q") or [""])[0].strip()
            if len(text) < 2 or len(text) > 200:
                raise ApiError(400, "type at least 2 letters")
            only = (query.get("project") or [""])[0]
            dirs = {p["name"]: p["remote"] for p in app.projects() if p.get("remote") and (not only or p["name"] == only)}
            return remote_python(app.conf, CHAT_SEARCH_PY, [], timeout=60,
                                 stdin=json.dumps({"q": text, "dirs": dirs, "only": bool(only)}, ensure_ascii=False).encode())
        if route[1] == "share":
            sub = "/" + "/".join(parts[1:]) if len(parts) > 1 else ""
            return app.share_api(method, sub, self._body() if method == "POST" else {}, self.headers.get("Host") or "")
        if route == ("GET", "prompts"):
            return remote_python(app.conf, PROMPTS_PY, ["get"])
        if route == ("POST", "prompts"):
            b = self._body()
            r = remote_python(app.conf, PROMPTS_PY, ["put"], stdin=json.dumps(
                {"rev": int(b.get("rev") or 0), "prompts": clean_prompts(b.get("prompts"))}, ensure_ascii=False).encode())
            if isinstance(r, dict) and r.get("conflict"):
                raise ApiError(409, "changed on another device — reopen to see the new list")
            return r
        if route == ("GET", "setup-check"):
            return app.setup_check()
        if route == ("POST", "setup-check") and parts[1:] == ["sshfs"]:
            return app.setup_fix_sshfs()
        if parts[:1] == ["pc"]:
            return app.pc_api(method, parts[1:], self._body() if method == "POST" else {})
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
            return {"commands": app.set_commands(parts[1], self._body().get("commands"))}
        if route == ("GET", "suggest") and len(parts) == 2:
            p = next((x for x in app.projects() if x["name"] == parts[1]), None)
            return {"suggestions": project_suggestions(app, p)}
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
        if route == ("POST", "group") and len(parts) == 2:
            app._project(parts[1])
            g = app.store.set_group(parts[1], self._body().get("group"))
            app.invalidate()
            app.layout_changed(names=[parts[1]])
            return {"group": g}
        if route == ("POST", "groups") and parts[1:] == ["rename"]:
            b = self._body() or {}
            old = " ".join(str(b.get("from") or "").split())
            moved = [k for k, v in app.store.data["projects"].items() if isinstance(v, dict) and old and v.get("group") == old]
            n = app.store.rename_group(b.get("from"), b.get("to"))
            app.invalidate()
            app.layout_changed(names=moved, colors=[old, " ".join(str(b.get("to") or "").split())])
            return {"moved": n}
        if route == ("POST", "groups") and parts[1:] == ["color"]:
            b = self._body() or {}
            c = app.store.set_group_color(b.get("group"), b.get("color"))
            app.invalidate()
            app.layout_changed(colors=[" ".join(str(b.get("group") or "").split())])
            return {"color": c}
        if route == ("POST", "color") and len(parts) == 2:
            c = app.store.set_color(parts[1], self._body().get("color"))
            app.invalidate()
            return {"color": c}
        if route == ("POST", "manual"):
            item = app.store.add_manual(self._body())
            app.invalidate()
            return {"project": item}
        if route == ("POST", "remove-project") and len(parts) == 2:
            b = self._body()
            return app.remove_project(parts[1], server=b.get("server") is True, local=b.get("local") is True)
        if route == ("DELETE", "manual") and len(parts) == 2:
            app.pc_forget_local(parts[1])
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
            if query.get("vis") == ["1"]:
                app.last_visible = time.time()
            return app.tabs()
        if route == ("GET", "push"):
            return app.push_info()
        if route == ("POST", "push") and len(parts) == 1:
            return app.push_subscribe(self._body())
        if route == ("POST", "push") and parts[1:] == ["remove"]:
            return app.push_unsubscribe(self._body())
        if route == ("POST", "push") and parts[1:] == ["pending"]:
            return app.push_pending(self._body())
        if route == ("POST", "push") and parts[1:] == ["test"]:
            if not app.pusher:
                raise ApiError(400, "notifications need the web version")
            with app.lock:
                app.push_seq += 1
                app.push_msgs.append({"seq": app.push_seq, "at": time.time(), "title": "Simab Workspace",
                                      "body": "Notifications work on this device", "tag": "test", "tab": ""})
            subs = app._push_subs()
            codes = [app.pusher.send(x["endpoint"]) for x in subs]
            return {"sent": sum(1 for c in codes if 200 <= c < 300), "devices": len(subs)}
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
        if route == ("POST", "tts") and len(parts) == 1:
            return app.tts(self._body())
        if route == ("GET", "tasks") and len(parts) == 1:
            return app.tasks()
        if route == ("POST", "tasks") and len(parts) == 1:
            return app.tasks(self._body())
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
            if method == "POST" and act == "discard":
                repo.discard(body.get("files"))
                app.invalidate()
                app.activity.add("git-discard", p["name"], ", ".join(str(x) for x in body.get("files") or [])[:200], repo=repo.repo)
                return {"status": repo.status()}
            if method == "GET" and act == "conflict":
                return repo.conflict((query.get("path") or [""])[0])
            if method == "POST" and act in ("resolve", "merge-abort"):
                if act == "resolve":
                    side = body.get("side")
                    repo.resolve(body.get("path"), body.get("text"), side if side in ("ours", "theirs") else None)
                else:
                    repo.merge_abort()
                app.invalidate()
                app.activity.add("git-" + act, p["name"], str(body.get("path") or "")[:200], repo=repo.repo)
                return {"status": repo.status()}
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
                    patches_raw = body.get("patches")
                    patches = {str(k): str(v) for k, v in patches_raw.items()
                               if isinstance(k, str) and isinstance(v, str)} if isinstance(patches_raw, dict) else None
                    out = repo.commit(body.get("message"), body.get("files"), patches)
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
        if route == ("GET", "lan-ip"):
            return {"ips": [] if app.web else lan_ips()}       # the web version: the server's own network, useless
        if route == ("GET", "server-folders"):
            return {"folders": app.server_folders()}
        if route == ("POST", "add-project"):
            return app.add_project(self._body())
        if route == ("POST", "projects") and parts[1:] == ["order"]:
            order = (self._body() or {}).get("order")
            if not isinstance(order, list):
                raise ApiError(400, "bad request")
            app.store.set_project_order(order)
            app.invalidate()
            app.layout_changed(order=True)
            return {"ok": True}
        if route == ("GET", "git-summary"):
            return {"summary": app.git_summary((query.get("project") or [""])[0], (query.get("force") or ["0"])[0] == "1")}
        if route == ("GET", "activity"):
            lim = (query.get("limit") or ["500"])[0]
            return {"items": app.activity.list(int(lim) if lim.isdigit() else 500,
                                               (query.get("project") or [""])[0], (query.get("q") or [""])[0])}
        if route == ("POST", "activity-delete"):
            b = self._body()
            return {"deleted": app.activity.delete(b.get("ids"), str(b.get("session") or ""), bool(b.get("clear")),
                                                   str(b.get("project") or ""))}
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
        if parts and parts[0] == "skills":
            act = parts[1] if len(parts) > 1 else ""
            if method == "GET" and act == "repos":
                return {"repos": app.skill_repos()}
            if method == "PUT" and act == "repos":
                repos = (body or {}).get("repos")
                if not isinstance(repos, list):
                    raise ApiError(400, "bad request")
                return {"repos": app.set_skill_repos(repos)}
            if method == "GET" and act == "list" and len(parts) == 3:
                import base64 as _b64
                try:
                    repo_url = _b64.urlsafe_b64decode(parts[2] + "==").decode()
                except Exception:
                    raise ApiError(400, "bad repo url")
                return {"skills": app.fetch_github_skills(repo_url)}
            if method == "GET" and act == "installed" and len(parts) == 3:
                return {"skills": app.list_project_skills(parts[2])}
            if method == "POST" and act == "install" and len(parts) == 3:
                d = body or {}
                project = parts[2]
                skill_name = str(d.get("name") or "")
                repo_url = str(d.get("repo_url") or "")
                skill_path = str(d.get("path") or "")
                if not skill_name or not repo_url or not skill_path:
                    raise ApiError(400, "missing fields")
                content = app.fetch_skill_content(repo_url, skill_path)
                app.install_skill(project, skill_name, content)
                return {"ok": True}
            if method == "DELETE" and act == "installed" and len(parts) == 4:
                app.uninstall_skill(parts[2], parts[3])
                return {"ok": True}
            raise ApiError(404, "not found")
        raise ApiError(404, "not found")

    def _websocket(self, query: dict):
        if self.gate is None:
            if not self._token_ok((query.get("t") or [""])[0]):
                raise ApiError(401, "bad token")
            port = self.server.server_address[1]
            origin = self.headers.get("Origin") or ""
            if origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}") and \
                    origin[len("http://"):] not in self.app.alt_hosts:
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


def wsl_vm_ip() -> str:
    """WSL2 (NAT mode): the VM's private address Windows can always reach, even when
    WSL's localhost forwarding is broken (VPN, firewall). "" when not WSL or not private."""
    if not is_wsl():
        return ""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "eth0"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)/", out)
    if not m:
        return ""
    import ipaddress
    ip = ipaddress.ip_address(m.group(1))
    # 172.16/12 is WSL's NAT range; mirrored mode puts the LAN address here — never listen on that
    return str(ip) if ip in ipaddress.ip_network("172.16.0.0/12") else ""


def serve_wsl_alt(app: App) -> str:
    """Second listener on the WSL VM address (same port, same token)."""
    ip = wsl_vm_ip()
    if not ip:
        return ""
    try:
        alt = ThreadingHTTPServer((ip, app.port), app.httpd.RequestHandlerClass)
    except OSError as e:
        log_line(f"WSL address {ip}:{app.port} not served: {e}")
        return ""
    alt.daemon_threads = True
    app.alt_hosts.add(f"{ip}:{app.port}")
    threading.Thread(target=alt.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
    return ip


def windows_reaches(port: int) -> bool:
    """WSL: can Windows open 127.0.0.1:port? (False when localhost forwarding is broken.)"""
    ps = (f"$c=New-Object Net.Sockets.TcpClient; try {{ if ($c.ConnectAsync('127.0.0.1',{int(port)}).Wait(1500))"
          " { 'ok' } } catch {} finally { $c.Close() }")
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                             text=True, timeout=15, cwd="/mnt/c" if os.path.isdir("/mnt/c") else None).stdout
    except (OSError, subprocess.SubprocessError):
        return True       # cannot tell — keep localhost
    return "ok" in out


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
    try:                          # an IdentityFile with a space written unquoted by an older version
        if heal_ssh_config():
            log_line("~/.ssh/config: quoted a key path with a space in our block (ssh refused the file)")
    except OSError as e:
        log_line(f"~/.ssh/config not healed: {e}")
    store = Store()
    app = App(store)
    httpd = make_server(app, int(store.settings.get("port") or 0), wait=15 if os.environ.get("SIMAB_RESTART") else 0)
    if store.settings.get("port") != app.port:
        store.update_settings({"port": app.port})
    st = {"pid": os.getpid(), "port": app.port, "token": app.token, "version": VERSION,
          "code": code_fingerprint(), "path": str(HERE), "wslIp": serve_wsl_alt(app)}
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
    threading.Thread(target=app.pc.start, daemon=True).start()
    app.restore_live()
    threading.Thread(target=app.share_loop, daemon=True).start()
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
        subprocess.Popen([win_python() if IS_WIN else sys.executable, *(["-X", "utf8"] if IS_WIN else []),
                          str(Path(__file__).resolve()), "serve"],
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
    if is_wsl() and st.get("wslIp") and shutil.which("powershell.exe") and not windows_reaches(st["port"]):
        url = url.replace("127.0.0.1", st["wslIp"], 1)
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
        if IS_WIN and not sys.flags.utf8_mode:   # started by hand: open(), subprocess text in UTF-8, not cp1252
            return subprocess.call([sys.executable, "-X", "utf8", str(Path(__file__).resolve())] + argv[1:])
        serve()
    elif cmd == "open":
        if linux_desktop():                   # the logo in the app menu and on the open window (dock), not a gear
            try:
                inst = home_path(".local", "bin", "simabworkspace")
                sc = HERE.parent / "simabworkspace.sh"
                if inst.is_file() or sc.is_file():
                    linux_menu_entry(str(inst if inst.is_file() else sc))
            except OSError:
                pass
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
    elif cmd == "pair" and len(argv) > 2:      # install.sh / install.ps1 with SIMAB_PAIR
        return pair_computer(argv[2])
    elif cmd == "shortcut" and IS_WIN:        # used by install.ps1
        print("  ✓ Added to the " + make_windows_shortcut())
    else:
        print(__doc__ or "usage: server.py [open|serve|url|status|stop]")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv) or 0)
