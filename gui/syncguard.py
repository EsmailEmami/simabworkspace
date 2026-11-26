"""Sync guard: keeps Mutagen from spreading a wrong mass delete (an unmounted disk, an empty or replaced folder).

Stdlib only. Used two ways:
  * imported by server.py on each computer (marker check, file-count watch, decisions);
  * run on the server as a script (python3 syncguard.py ...): markers, counts, snapshots, restore.
Everything is fail-safe: when unsure, the caller pauses the sync and deletes nothing.
"""
import fnmatch
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time

MARKER = ".simab-root"
ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
SNAP_BASE = "~/.simab-snapshots"
INSTALL_PATH = "~/.simabworkspace/syncguard.py"
SNAP_EXCLUDES = ("node_modules", "dist", "build", ".next", ".nuxt", ".svelte-kit", ".output", "target", "__pycache__",
                 ".venv", "venv", ".cache", ".gradle", ".turbo", "coverage", ".parcel-cache", ".dev-run", "*.pyc")
DEFAULTS = {"on": True, "dropPct": 20, "dropMin": 20, "dropFiles": 500, "emptyFiles": 3, "fullFiles": 30,
            "topDirs": True, "snapEvery": 30, "snapKeep": 12, "snapDays": 7}
WINDOW = 180          # seconds: a drop is measured against the most files seen in this window


# ----------------------------------------------------------------------------- markers
def read_marker(root: str) -> dict:
    """{"state": ok | missing | noroot | bad, "id": ...}"""
    if not root or not os.path.isdir(root):
        return {"state": "noroot", "id": ""}
    try:
        with open(os.path.join(root, MARKER), encoding="utf-8", errors="replace") as f:
            raw = f.read(4096)
    except FileNotFoundError:
        return {"state": "missing", "id": ""}
    except OSError:
        return {"state": "bad", "id": ""}
    pid = ""
    try:
        d = json.loads(raw)
        pid = str(d.get("id") or "") if isinstance(d, dict) else ""
    except ValueError:
        pid = (raw.strip().splitlines() or [""])[0].strip()
    return {"state": "ok", "id": pid} if ID_RE.fullmatch(pid) else {"state": "bad", "id": ""}


def new_id() -> str:
    return secrets.token_hex(8)


def write_marker(root: str, pid: str, name: str = "") -> bool:
    """Writes the marker unless the folder is missing or already has another id. Never overwrites a different id."""
    if not ID_RE.fullmatch(pid or "") or not os.path.isdir(root):
        return False
    cur = read_marker(root)
    if cur["state"] == "ok":
        return cur["id"] == pid
    if cur["state"] == "bad":
        return False
    path = os.path.join(root, MARKER)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"id": pid, "project": str(name)[:80],
                       "note": "Simab Workspace sync guard: the sync pauses when this file is missing on one side."}, f)
            f.write("\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    git_exclude(root)
    return True


def git_exclude(root: str):
    """Keeps the marker out of `git status` (.git/info/exclude, only appended)."""
    info = os.path.join(root, ".git", "info")
    if not os.path.isdir(os.path.join(root, ".git")):
        return
    f = os.path.join(info, "exclude")
    try:
        old = open(f, encoding="utf-8", errors="replace").read() if os.path.isfile(f) else ""
        if MARKER in old.split():
            return
        os.makedirs(info, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(("" if not old or old.endswith("\n") else "\n") + "/" + MARKER + "\n")
    except OSError:
        pass


def marker_verdict(local: dict, remote: dict):
    """None = both sides match; "adopt" = neither side has a marker yet; else why the sync must stay paused."""
    ls, rs = local.get("state"), remote.get("state")
    if ls == "noroot":
        return "The folder on this computer is missing (disk not mounted?)"
    if rs == "noroot":
        return "The folder on the server is missing"
    if ls == "ok" and rs == "ok":
        return None if local.get("id") == remote.get("id") else \
            "The two folders belong to different projects (their .simab-root files differ)"
    if ls == "missing" and rs == "missing":
        return "adopt"
    if ls == "bad" or rs == "bad":
        return "Cannot read .simab-root on " + ("this computer" if ls == "bad" else "the server")
    if ls == "missing":
        return "This computer's folder has no .simab-root but the server's has: the disk may not be mounted or the folder was replaced"
    return "The server's folder has no .simab-root but this computer's has: the server folder may have been replaced"


# ----------------------------------------------------------------------------- disks / mounts (Linux)
MOUNTINFO = "/proc/self/mountinfo"
DISK_DIRS = ("/media/", "/run/media/", "/mnt/")       # where removable / data disks are mounted


def _unesc(s: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def mounts(info: str = MOUNTINFO) -> list:
    """[{mnt, src, fs}] from /proc/self/mountinfo ([] when not Linux)."""
    out = []
    try:
        lines = open(info, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return out
    for ln in lines:
        a, _, b = ln.partition(" - ")
        f, g = a.split(), b.split()
        if len(f) >= 5 and len(g) >= 2:
            out.append({"mnt": _unesc(f[4]), "src": _unesc(g[1]), "fs": g[0]})
    return out


def _disk_ids(kind: str) -> dict:
    """/dev/sdb1 -> UUID (kind "uuid") or label ("label")."""
    d = "/dev/disk/by-" + kind
    out = {}
    try:
        for n in os.listdir(d):
            out[os.path.realpath(os.path.join(d, n))] = \
                re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), n) if kind == "label" else n
    except OSError:
        pass
    return out


def _existing(path: str) -> str:
    """realpath of the part that exists + the rest as written (a missing folder still names its mount)."""
    p, tail = os.path.abspath(os.path.expanduser(path)), []
    while p != "/" and not os.path.lexists(p):
        p, t = os.path.dirname(p), os.path.basename(p)
        tail.insert(0, t)
    return os.path.join(os.path.realpath(p), *tail)


def mount_of(path: str, info: str = MOUNTINFO, ids=None) -> dict:
    """The mount that holds path (or its nearest existing parent): {mnt, src, fs, uuid, label}; {} when unknown."""
    real = _existing(path)
    best = None
    for m in mounts(info):
        mp = m["mnt"].rstrip("/") or "/"
        if (real == mp or real.startswith(mp.rstrip("/") + "/") or mp == "/") and (best is None or len(mp) >= len(best["mnt"])):
            best = dict(m, mnt=mp)
    if not best:
        return {}
    uu, lb = ids if ids is not None else (_disk_ids("uuid"), _disk_ids("label"))
    src = os.path.realpath(best["src"]) if best["src"].startswith("/dev/") else best["src"]
    best.update(uuid=uu.get(src, ""), label=lb.get(src, ""))
    return best


def where_mounted(uuid: str, info: str = MOUNTINFO, ids=None) -> str:
    """Where the disk with that UUID is mounted now ('' = not mounted)."""
    uu = (ids if ids is not None else (_disk_ids("uuid"), {}))[0]
    devs = {d for d, u in uu.items() if u == uuid}
    for m in mounts(info):
        src = os.path.realpath(m["src"]) if m["src"].startswith("/dev/") else m["src"]
        if src in devs:
            return m["mnt"]
    return ""


def mount_verdict(path: str, rec: dict, cur: dict, info: str = MOUNTINFO, ids=None):
    """None when the folder is on the disk it was on before (and not an empty disk folder on the system disk)."""
    real = _existing(path)
    if rec and rec.get("uuid") and cur.get("uuid") != rec["uuid"]:
        name = rec.get("label") or rec["uuid"]
        now = where_mounted(rec["uuid"], info, ids)
        if now:
            new = os.path.join(now, os.path.relpath(os.path.abspath(path), rec["mnt"])) if rec.get("mnt") else now
            return (f"Disk {name} is mounted at {now} instead of {rec.get('mnt')}: the folder {path} is not on it. "
                    f"The project is probably at {new}. Mount the disk at {rec.get('mnt')} (and remove the empty "
                    f"{rec.get('mnt')} folder something created), or move the project there")
        return f"Disk {name} (that held {path}) is not mounted"
    if not rec and cur and cur.get("mnt") == "/" and any(real.startswith(d) or (real + "/").startswith(d) for d in DISK_DIRS):
        return f"{path} is under {next(d for d in DISK_DIRS if (real + '/').startswith(d))} but on the system disk: its disk is probably not mounted"
    return None


def foreign_owner(path: str):
    """A sync folder made by another user (docker / sudo create a missing bind-mount folder as root)."""
    if not hasattr(os, "getuid"):
        return None
    try:
        uid = os.stat(path).st_uid
    except OSError:
        return None
    if uid != os.getuid():
        try:
            import pwd
            who = pwd.getpwuid(uid).pw_name
        except (ImportError, KeyError):
            who = str(uid)
        return (f"{path} belongs to {who}, not to you: it was probably created by docker (a container's folder "
                f"while the disk was not mounted) or sudo, not by the sync")
    return None


# ----------------------------------------------------------------------------- counts
def config(raw) -> dict:
    c = dict(DEFAULTS)
    for k, d in DEFAULTS.items():
        v = (raw or {}).get(k) if isinstance(raw, dict) else None
        if isinstance(d, bool):
            if isinstance(v, bool):
                c[k] = v
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            c[k] = max(0, int(v))
    c["dropPct"] = min(100, c["dropPct"])
    return c


def lopsided(alpha_files, beta_files, cfg: dict):
    """One side (nearly) empty, the other full: never sync that."""
    if not isinstance(alpha_files, int) or not isinstance(beta_files, int):
        return None
    e, f = cfg["emptyFiles"], max(cfg["fullFiles"], cfg["emptyFiles"] + 1)
    if alpha_files <= e and beta_files >= f:
        return f"This computer's folder is (nearly) empty ({alpha_files} files) but the server has {beta_files}"
    if beta_files <= e and alpha_files >= f:
        return f"The server's folder is (nearly) empty ({beta_files} files) but this computer has {alpha_files}"
    return None


class DropWatch:
    """File counts of one side over time; a sudden drop (percent or count) is an alarm."""

    def __init__(self):
        self.hist = []        # [(time, files)]

    def reset(self):
        self.hist = []

    def check(self, files, cfg: dict, now: float = None):
        now = time.time() if now is None else now
        if not isinstance(files, int) or files < 0:
            return None
        self.hist = [(t, n) for t, n in self.hist if now - t <= WINDOW]
        base = max([n for _, n in self.hist] or [files])
        self.hist.append((now, files))
        drop = base - files
        if drop <= 0:
            return None
        if (cfg["dropFiles"] and drop >= cfg["dropFiles"]) or \
                (drop >= max(1, cfg["dropMin"]) and drop * 100 >= cfg["dropPct"] * base):
            return f"{drop} of {base} files disappeared at once"
        return None


def _ignored(name: str, ignores) -> bool:
    for pat in ignores or ():
        p = str(pat).strip().strip("/")
        if p and not p.startswith("!") and fnmatch.fnmatch(name, p):
            return True
    return False


def top_dirs(root: str, ignores=()) -> set:
    try:
        return {e.name for e in os.scandir(root) if e.is_dir(follow_symlinks=False) and e.name != ".git"
                and not _ignored(e.name, ignores)}
    except OSError:
        return set()


def count_files(root: str, ignores=(), cap: int = 500000) -> int:
    """Files under root (ignored names skipped), -1 when the folder cannot be read. Stops counting at cap."""
    if not os.path.isdir(root):
        return -1
    n, stack = 0, [root]
    while stack and n < cap:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if _ignored(e.name, ignores):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    else:
                        n += 1
        except OSError:
            if d == root:
                return -1
    return n


def stat_root(root: str, ignores=()) -> dict:
    root = os.path.expanduser(root)
    return {"marker": read_marker(root), "files": count_files(root, ignores), "top": sorted(top_dirs(root, ignores))}


# ----------------------------------------------------------------------------- snapshots (server side)
def snap_dir(pid: str) -> str:
    return os.path.join(os.path.expanduser(SNAP_BASE), pid)


def list_snaps(pid: str) -> list:
    d = snap_dir(pid)
    try:
        return sorted(n for n in os.listdir(d) if re.fullmatch(r"\d{8}-\d{6}(-\d+)?", n))
    except OSError:
        return []


def _copy_linked(src: str, dst: str, prev: str, excludes):
    """Python fallback for rsync --link-dest: unchanged files are hard links to the previous snapshot."""
    for cur, dirs, files in os.walk(src):
        rel = os.path.relpath(cur, src)
        dirs[:] = [x for x in dirs if not _ignored(x, excludes)]
        out = os.path.normpath(os.path.join(dst, rel))
        os.makedirs(out, exist_ok=True)
        for fn in files:
            if _ignored(fn, excludes):
                continue
            s, t = os.path.join(cur, fn), os.path.join(out, fn)
            try:
                st = os.lstat(s)
                if os.path.islink(s):
                    os.symlink(os.readlink(s), t)
                    continue
                p = os.path.normpath(os.path.join(prev, rel, fn)) if prev else ""
                if p and os.path.isfile(p) and not os.path.islink(p):
                    ps = os.lstat(p)
                    if ps.st_size == st.st_size and int(ps.st_mtime) == int(st.st_mtime):
                        os.link(p, t)
                        continue
                shutil.copy2(s, t)
            except OSError:
                pass           # a file that vanished while copying


def snapshot(root: str, pid: str, every_min: int = 0, keep: int = 12, days: int = 7, excludes=(), force=False) -> dict:
    """A light copy of root in ~/.simab-snapshots/<id>/<UTC time> (unchanged files hard-linked to the last one)."""
    root = os.path.realpath(os.path.expanduser(root))
    if not ID_RE.fullmatch(pid or ""):
        return {"ok": False, "error": "bad id"}
    m = read_marker(root)
    if m["state"] != "ok" or m["id"] != pid:
        return {"ok": False, "error": "the folder's .simab-root does not match: no snapshot taken"}
    base = snap_dir(pid)
    snaps = list_snaps(pid)
    if snaps and not force and every_min > 0:
        try:
            age = time.time() - os.stat(os.path.join(base, snaps[-1])).st_mtime
        except OSError:
            age = 1e9
        if age < every_min * 60:
            return {"ok": True, "skipped": True, "name": snaps[-1]}
    os.makedirs(base, exist_ok=True)
    try:
        with open(os.path.join(base, "root.txt"), "w") as f:
            f.write(root + "\n")
    except OSError:
        pass
    name = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    i = 1
    while os.path.exists(os.path.join(base, name)):
        name = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{i}"
        i += 1
    part = os.path.join(base, "." + name + ".part")
    shutil.rmtree(part, ignore_errors=True)
    prev = os.path.join(base, snaps[-1]) if snaps else ""
    excl = list(SNAP_EXCLUDES) + [str(x) for x in excludes or () if str(x).strip() and not str(x).startswith("!")]
    rsync = shutil.which("rsync")
    if rsync:
        argv = [rsync, "-a", "--delete"] + (["--link-dest=" + prev] if prev else []) + \
               ["--exclude=" + x for x in excl] + [root + "/", part + "/"]
        r = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=3600)
        if r.returncode not in (0, 23, 24):       # 23/24: files vanished or unreadable while copying
            shutil.rmtree(part, ignore_errors=True)
            return {"ok": False, "error": "rsync failed: " + r.stderr.decode("utf-8", "replace").strip()[-200:]}
    else:
        _copy_linked(root, part, prev, excl)
    os.rename(part, os.path.join(base, name))
    os.utime(os.path.join(base, name))
    removed = prune(pid, keep, days)
    return {"ok": True, "skipped": False, "name": name, "path": os.path.join(base, name), "removed": removed}


def prune(pid: str, keep: int = 12, days: int = 7) -> list:
    """Keeps the newest `keep` snapshots and the first one of each of the last `days` days."""
    snaps = list_snaps(pid)
    keepset = set(snaps[-max(1, keep):])
    today = time.strftime("%Y%m%d", time.gmtime())
    first = {}
    for n in snaps:
        first.setdefault(n[:8], n)
    for day, n in first.items():
        try:
            age = (time.mktime(time.strptime(today, "%Y%m%d")) - time.mktime(time.strptime(day, "%Y%m%d"))) / 86400
        except ValueError:
            continue
        if age < days:
            keepset.add(n)
    gone = []
    for n in snaps:
        if n not in keepset:
            shutil.rmtree(os.path.join(snap_dir(pid), n), ignore_errors=True)
            gone.append(n)
    return gone


def restore(root: str, pid: str = "", name: str = "", overwrite: bool = False, dry: bool = False) -> dict:
    """Copies a snapshot back into root. Default: only files that are missing now; --overwrite also replaces changed ones.
    Never deletes anything in root."""
    root = os.path.realpath(os.path.expanduser(root))
    pid = pid or read_marker(root).get("id") or ""
    if not ID_RE.fullmatch(pid):
        return {"ok": False, "error": "no .simab-root in the folder: pass --id (see: list)"}
    snaps = list_snaps(pid)
    if not snaps:
        return {"ok": False, "error": "no snapshots for " + pid}
    name = name or snaps[-1]
    if name not in snaps:
        return {"ok": False, "error": "no snapshot " + name + " (have: " + ", ".join(snaps[-5:]) + ")"}
    src = os.path.join(snap_dir(pid), name)
    os.makedirs(root, exist_ok=True)
    done = []
    for cur, dirs, files in os.walk(src):
        rel = os.path.relpath(cur, src)
        out = os.path.normpath(os.path.join(root, rel))
        for fn in files:
            s, t = os.path.join(cur, fn), os.path.join(out, fn)
            if os.path.lexists(t) and not overwrite:
                continue
            if os.path.lexists(t) and os.path.isfile(t) and os.path.getsize(t) == os.path.getsize(s) \
                    and int(os.path.getmtime(t)) == int(os.path.getmtime(s)):
                continue
            done.append(os.path.normpath(os.path.join(rel, fn)))
            if dry:
                continue
            os.makedirs(out, exist_ok=True)
            if os.path.islink(s):
                if os.path.lexists(t):
                    os.unlink(t)
                os.symlink(os.readlink(s), t)
            else:
                shutil.copy2(s, t)          # a new file: the snapshot's hard links stay untouched
    return {"ok": True, "snapshot": name, "files": len(done), "list": done[:50], "dry": dry}


def all_snaps() -> list:
    base = os.path.expanduser(SNAP_BASE)
    out = []
    try:
        ids = sorted(os.listdir(base))
    except OSError:
        return out
    for pid in ids:
        if not ID_RE.fullmatch(pid):
            continue
        try:
            root = open(os.path.join(base, pid, "root.txt")).read().strip()
        except OSError:
            root = ""
        out.append({"id": pid, "root": root, "snaps": list_snaps(pid)})
    return out


# ----------------------------------------------------------------------------- command line (on the server)
USAGE = """Simab Workspace sync guard — snapshots of synced folders on the server.
  python3 ~/.simabworkspace/syncguard.py list                    projects with snapshots
  python3 ~/.simabworkspace/syncguard.py list FOLDER             snapshots of one folder
  python3 ~/.simabworkspace/syncguard.py snapshot FOLDER          take one now
  python3 ~/.simabworkspace/syncguard.py restore FOLDER [--snap NAME] [--id ID] [--overwrite] [--dry-run]
      brings back the files missing now (newest snapshot unless --snap); --overwrite also replaces changed
      files; nothing is ever deleted. Pause the sync on your computers first."""


def main(argv: list) -> int:
    op = argv[0] if argv else ""
    a = argv[1:]
    if op == "marker-read":
        print(json.dumps(stat_root(a[0], a[1:]) if a else {}))
    elif op == "marker-write":
        root = os.path.expanduser(a[0])
        print(json.dumps({"ok": write_marker(root, a[1], a[2] if len(a) > 2 else ""), "marker": read_marker(root)}))
    elif op == "snap":                     # snap FOLDER ID EVERY KEEP DAYS FORCE [EXCLUDES...]
        print(json.dumps(snapshot(a[0], a[1], int(a[2]), int(a[3]), int(a[4]), a[6:], force=a[5] == "1")))
    elif op == "install":                  # the source on stdin → ~/.simabworkspace/syncguard.py (restore by hand)
        src = sys.stdin.read()
        p = os.path.expanduser(INSTALL_PATH)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "w") as f:
            f.write(src)
        os.replace(p + ".tmp", p)
        print(json.dumps({"ok": True, "path": p}))
    elif op == "list":
        if a:
            pid = read_marker(os.path.expanduser(a[0])).get("id") or (a[1] if len(a) > 1 else "")
            for n in list_snaps(pid):
                print(n)
            if not pid:
                print("no .simab-root in that folder; run `list` alone to see every id", file=sys.stderr)
        else:
            for x in all_snaps():
                print(f"{x['id']}  {x['root'] or '?'}  {len(x['snaps'])} snapshots, newest {x['snaps'][-1] if x['snaps'] else '-'}")
    elif op == "snapshot" and a:
        root = os.path.expanduser(a[0])
        r = snapshot(root, read_marker(root).get("id") or "", force=True, keep=DEFAULTS["snapKeep"], days=DEFAULTS["snapDays"])
        print(r.get("path") or r.get("error"))
        return 0 if r.get("ok") else 1
    elif op == "restore" and a:
        opt = {"--snap": "", "--id": ""}
        rest = list(a[1:])
        for k in opt:
            if k in rest:
                i = rest.index(k)
                opt[k] = rest[i + 1] if i + 1 < len(rest) else ""
        r = restore(a[0], opt["--id"], opt["--snap"], "--overwrite" in rest, "--dry-run" in rest)
        if not r.get("ok"):
            print(r.get("error"), file=sys.stderr)
            return 1
        print(("Would restore" if r["dry"] else "Restored") + f" {r['files']} files from {r['snapshot']}")
        for x in r["list"]:
            print("  " + x)
    else:
        print(USAGE)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
