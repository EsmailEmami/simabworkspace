"""Projects and open tabs shared between devices (installed app ↔ web) through devices.json on the server."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gui"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as sw  # noqa: E402
import test_gui_server as base  # noqa: E402
from test_tasks import FakeServer  # noqa: E402

CONV = "0b5e4c2a-1111-4222-8333-444455556666"


class Devices(base.TmpHome):
    def setUp(self):
        super().setUp()
        self.server_home = tempfile.TemporaryDirectory()
        self.fake = FakeServer(self.server_home.name)
        self.rp = mock.patch.object(sw, "remote_python", self.fake)
        self.rp.start()
        self.mut = mock.patch.object(sw, "mutagen_sessions", return_value=[])
        self.mut.start()
        conf = sw.ServerConf("esi", "h")
        self.laptop = sw.App(self.store, conf)
        self.store.add_manual({"name": "shop", "remote": "~/shop"})
        self.store.add_manual({"name": "far", "remote": "/x"})
        d = Path(self.tmp.name) / "web"
        d.mkdir()
        self.web = sw.App(sw.Store(d / "simab.db"), conf, web=True)
        self.web.store.add_manual({"name": "blog", "remote": "~/blog"})

    def tearDown(self):
        for a in (self.laptop, self.web):
            a.shutdown_sessions()
        self.rp.stop()
        self.mut.stop()
        self.server_home.cleanup()
        super().tearDown()

    def names(self, app):
        return sorted(p["name"] for p in app.projects(max_age=0))

    def kinds(self, app):
        return sorted(sw.share_tab_key(s.spec) for s in app.sessions.values())

    def test_projects_of_the_other_device_show_up(self):
        self.laptop.share_sync()
        self.web.share_sync()
        self.laptop.share_sync()
        self.assertEqual(self.names(self.web), ["blog", "far", "shop"])
        self.assertEqual(self.names(self.laptop), ["blog", "far", "shop"])
        shop = next(p for p in self.web.projects(max_age=0) if p["name"] == "shop")
        self.assertEqual((shop["source"], shop["remote"], shop["local"]), ("shared", "~/shop", ""))
        self.assertTrue(shop["sharedFrom"])
        # a project on another server is not shared (the web could not reach it)
        real = self.laptop.projects
        with mock.patch.object(self.laptop, "projects", lambda max_age=3.0: [
                dict(p, endpoint={"user": "esi", "host": "elsewhere", "port": "22"}) if p["name"] == "far" else p for p in real(max_age)]):
            self.assertEqual(sorted(p["name"] for p in self.laptop._share_projects_mine()), ["shop"])
        # turned off: nothing is shared
        self.web.store.update_settings({"shareDevices": False})
        self.assertEqual(self.web.share_sync(), {})

    def test_web_shows_my_computers_run_commands_and_where_they_run(self):
        loc = Path(self.tmp.name) / "shopsrc"
        loc.mkdir()
        self.store.add_manual({"name": "shop", "remote": "~/shop", "local": str(loc)})
        self.store.set_commands("shop", [{"name": "dev", "cmd": "npm run dev", "where": "local"},
                                         {"name": "deploy", "cmd": "./deploy.sh", "where": "server"}])
        self.laptop._projects_cache = (0.0, [])
        self.laptop.share_sync()
        self.web.share_sync()
        shop = next(p for p in self.web.projects(max_age=0) if p["name"] == "shop")
        by = {c["name"]: c for c in shop["commands"]}
        self.assertEqual((by["dev"]["where"], by["deploy"]["where"]), ("local", "server"))
        self.assertTrue(by["dev"]["devName"])                       # the web says which computer runs it
        self.assertEqual(by["dev"]["devProject"], "shop")
        self.assertEqual(by["dev"]["fromDev"], str(self.laptop.pc.cfg().get("id") or ""))
        # a server one runs from the web on the server; a computer's one only through that computer
        self.assertEqual(self.web.command("shop", by["deploy"]["id"])["cmd"], "./deploy.sh")
        with self.assertRaises(sw.ApiError):
            self.web.create_session({"kind": "run", "project": "shop", "cmdId": by["dev"]["id"]})
        # not on this computer (no folder): its local command is not offered
        self.assertEqual([c["name"] for c in sw.share_run_commands(dict(shop, localExists=False, buildsOn="server",
                                                                         commands=[dict(by["dev"], devName=None)]))], [])
        src = (Path(sw.__file__).parent / "static" / "app.js").read_text()
        self.assertIn("'⚒ ' + pcName + ' · off'", src)              # computer off: shown, greyed out
        self.assertIn("!c.devName", src)                            # editor leaves another computer's alone

    def test_history_of_the_other_computers_and_the_web_shows_up_with_where(self):
        self.laptop.activity.add("prompt", "shop", "fix the cart", session="s1")
        self.web.activity.add("run", "blog", "build", cmd="make")
        self.laptop.history_push()
        rows = self.web.history_all()
        by = {r["text"]: r for r in rows}
        self.assertEqual(set(by), {"fix the cart", "build"})
        self.assertEqual(by["build"]["from"], {"here": True, "name": "Web", "web": True})
        far = by["fix the cart"]["from"]
        self.assertEqual((far["name"], far["web"]), (self.laptop.device_name(), False))
        self.assertTrue(by["fix the cart"]["id"].startswith(self.laptop.device_id() + ":"))
        # the laptop sees the web's row (the web pushed while reading); nothing new → nothing sent again
        laptop_rows = {r["text"]: r["from"] for r in self.laptop.history_all()}
        self.assertEqual(laptop_rows["build"]["name"], "Web")
        self.assertTrue(laptop_rows["build"]["web"])
        self.fake.calls.clear()
        self.assertEqual(self.laptop.history_push(), {})
        self.assertEqual(self.fake.calls, [])
        # removed on the laptop → gone on the web too; another device's id is not removed here
        self.laptop.activity.delete(session="s1")
        self.laptop.history_push()
        self.assertEqual([r["text"] for r in self.web.history_all()], ["build"])
        self.assertEqual(self.web.activity.delete([by["fix the cart"]["id"]]), 0)
        # sharing off: only mine
        self.web.store.update_settings({"shareDevices": False})
        self.assertEqual([r["text"] for r in self.web.history_all()], ["build"])
        src = (Path(sw.__file__).parent / "static" / "app.js").read_text()
        self.assertIn("'activity?all=1&limit=2000'", src)
        self.assertIn("histFrom(it)", src)

    def test_sessions_of_every_computers_own_copy_show_and_open_here(self):
        home = Path(self.server_home.name)
        conv = {"": "11111111-1111-4111-8111-111111111111", "desk": "22222222-2222-4222-8222-222222222222",
                "shared": "33333333-3333-4333-8333-333333333333"}
        for folder, where in (("shop@laptop", ""), ("shop@desk", "desk"), ("shop", "shared")):
            (home / folder).mkdir()
            d = home / ".claude" / "projects" / sw.claude_project_dir(str(home / folder))
            d.mkdir(parents=True)
            (d / (conv[where] + ".jsonl")).write_text(json.dumps({"type": "user", "message": {"content": "hi from " + (where or "here")}}, separators=(",", ":")) + "\n")
        (home / "shopping").mkdir()                       # another project that starts the same: not counted
        self.store.add_manual({"name": "mine", "remote": str(home / "shop@laptop")})
        self.laptop._projects_cache = (0.0, [])
        got = {c["id"]: c for c in self.laptop.claude_sessions("mine")}
        self.assertEqual({c["where"] for c in got.values()}, {"", "desk", "shared"})
        desk = got[conv["desk"]]
        self.assertEqual((desk["first"], desk["dir"]), ("hi from desk", sw.claude_project_dir(str(home / "shop@desk"))))
        # opening the desk's conversation here copies it into this folder's Claude folder (the original stays)
        out = self.fake(None, sw.ADOPT_PY, [sw.claude_project_dir(str(home / "shop@laptop")), conv["desk"], desk["dir"]])
        self.assertEqual(out, {"copied": True})
        here = home / ".claude" / "projects" / sw.claude_project_dir(str(home / "shop@laptop"))
        self.assertTrue((here / (conv["desk"] + ".jsonl")).exists())
        self.assertTrue((home / ".claude" / "projects" / desk["dir"] / (conv["desk"] + ".jsonl")).exists())
        self.assertEqual(self.fake(None, sw.ADOPT_PY, [sw.claude_project_dir(str(home / "shop@laptop")), conv["desk"], desk["dir"]]),
                         {"copied": False})        # already here: left alone
        src = (Path(sw.__file__).parent / "static" / "app.js").read_text()
        self.assertIn("c.where ? { adopt: c.dir } : {}", src)

    def test_sync_window_lists_each_computers_copy_with_its_git_state(self):
        import subprocess
        home = Path(self.server_home.name)
        env = dict(os.environ, HOME=str(home), GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        def git(d, *a):
            subprocess.run(["git", "-C", str(d)] + list(a), check=True, capture_output=True, env=env)
        (home / "shop").mkdir()
        git(home / "shop", "init", "-q", "-b", "main")
        (home / "shop" / "a.txt").write_text("1")
        git(home / "shop", "add", ".")
        git(home / "shop", "commit", "-qm", "first")
        subprocess.run(["cp", "-a", str(home / "shop"), str(home / "shop@desk")], check=True)
        (home / "shop@desk" / "a.txt").write_text("2")              # desk: one commit more + a file not committed
        git(home / "shop@desk", "commit", "-qam", "desk work")
        (home / "shop@desk" / "a.txt").write_text("3")
        (home / "shopping").mkdir()                                  # another project: not a copy
        d = Path(self.tmp.name) / "desk"
        d.mkdir()
        desk = sw.App(sw.Store(d / "simab.db"), sw.ServerConf("esi", "h"))
        try:
            desk.store.add_manual({"name": "shop", "remote": "~/shop@desk"})
            desk.share_sync()
            self.laptop.share_sync()
            self.laptop.share_sync()
            r = self.laptop.sync_computers("shop")
        finally:
            desk.shutdown_sessions()
        self.assertEqual([f["folder"] for f in r["folders"]], ["~/shop", "~/shop@desk"])
        mine, dk = r["folders"]
        self.assertTrue(mine["mine"])
        self.assertTrue(any(w.get("here") for w in mine["who"]))
        self.assertEqual([w["dev"] for w in dk["who"]], [desk.device_id()])
        self.assertTrue(dk["who"][0]["online"])
        self.assertEqual((dk["repos"][0]["repo"], dk["repos"][0]["dirty"], dk["repos"][0]["subject"]), (".", 1, "desk work"))
        self.assertNotEqual(dk["repos"][0]["head"], mine["repos"][0]["head"])
        self.assertEqual(mine["repos"][0]["dirty"], 0)
        # bring: the desk has files not committed → refused; committed → pushed there, pulled here (all on the server)
        origin = home / "origin.git"
        git(home, "init", "-q", "--bare", str(origin))
        for f in ("shop", "shop@desk"):
            git(home / f, "remote", "add", "origin", str(origin))
        git(home / "shop", "push", "-q", "-u", "origin", "main")
        git(home / "shop@desk", "fetch", "-q", "origin")
        git(home / "shop@desk", "branch", "-q", "-u", "origin/main")
        res = self.laptop.sync_bring("shop", "desk", "")
        self.assertFalse(res["repos"][0]["ok"])
        self.assertIn("not committed", res["repos"][0]["why"])
        git(home / "shop@desk", "commit", "-qam", "more desk work")
        res = self.laptop.sync_bring("shop", "desk", "")
        self.assertEqual((res["repos"][0]["ok"], res["repos"][0]["pushed"]), (True, 2))
        self.assertEqual((home / "shop" / "a.txt").read_text(), "3")
        with self.assertRaises(sw.ApiError):
            self.laptop.sync_bring("shop", "../x", "")

    def test_a_shared_project_can_be_removed_from_this_list(self):
        self.laptop.share_sync()
        self.web.share_sync()
        with self.assertRaises(sw.ApiError):                 # its folder belongs to the device that added it
            self.web.remove_project("shop", server=True)
        self.assertTrue(self.web.remove_project("shop")["ok"])
        self.assertEqual(self.names(self.web), ["blog", "far"])
        self.web.share_sync()                                # the next round does not bring it back
        self.assertEqual(self.names(self.web), ["blog", "far"])
        self.assertIn("shop", self.names(self.laptop))      # the owner keeps it

    def test_tabs_open_and_close_on_every_device(self):
        self.laptop.share_sync()
        self.web.share_sync()
        self.laptop.create_session({"kind": "tasks"})
        files = self.laptop.create_session({"kind": "files", "project": "shop"})
        self.laptop.share_sync()
        r = self.web.share_sync()
        self.assertEqual(sorted(r["opened"]), ["files:shop", "tasks"])
        self.assertEqual(self.kinds(self.web), ["files:shop", "tasks"])
        moved = [s.moved for s in self.web.sessions.values()]
        self.assertTrue(all(moved), "open pages add the new tabs")
        # nothing opens twice
        self.assertEqual(self.web.share_sync()["opened"], [])
        # closed on the web → closed on the laptop, and the laptop's pages hear about it
        wid = next(s.id for s in self.web.sessions.values() if s.spec["kind"] == "files")
        self.web.close_session(wid)
        self.web.share_sync()
        r = self.laptop.share_sync()
        self.assertEqual(r["closed"], ["files:shop"])
        self.assertEqual(self.kinds(self.laptop), ["tasks"])
        self.assertIn(files["id"], self.laptop.tabs()["gone"])
        # opened again on the laptop → back on the web
        self.laptop.create_session({"kind": "files", "project": "shop"})
        self.laptop.share_sync()
        self.assertEqual(self.web.share_sync()["opened"], ["files:shop"])

    def test_quitting_or_restarting_closes_nothing_elsewhere(self):
        self.laptop.share_sync()
        self.laptop.create_session({"kind": "tasks"})
        self.laptop.share_sync()
        self.laptop.share_sync()
        self.web.share_sync()
        self.assertEqual(self.kinds(self.web), ["tasks"])
        self.laptop.shutdown_sessions()
        # the next morning: a new app with no tabs open
        laptop2 = sw.App(self.store, sw.ServerConf("esi", "h"))
        self.assertEqual(laptop2.share_sync()["opened"], ["tasks"], "it gets the web's tabs instead")
        self.assertEqual(self.web.share_sync()["closed"], [])
        self.assertEqual(self.kinds(self.web), ["tasks"])
        laptop2.shutdown_sessions()

    def test_a_tab_whose_project_is_missing_is_not_opened(self):
        self.fake(None, sw.SHARE_PY, [], stdin=b'{"device": "x", "add": [{"key": "files:nope", "spec": {"kind": "files", "project": "nope"}}]}')
        self.assertEqual(self.web.share_sync()["opened"], [])
        self.assertEqual(self.web.sessions, {})

    def test_a_chat_of_another_computer_does_not_open_here_only_on_the_web(self):
        """Two computers on: a chat opened on one does not open (and work) in the other's app (user 2026-11-27)."""
        chat = {"kind": "chat", "project": "shop", "resume": "c1"}
        self.fake(None, sw.SHARE_PY, [], stdin=json.dumps({"device": "desk", "name": "desk", "add": [{"key": "chat:c1", "spec": chat}]}).encode())
        self.fake(None, sw.SHARE_PY, [], stdin=json.dumps({"device": "webdev", "name": "Web", "add": [{"key": "chat:c2", "spec": dict(chat, resume="c2")}]}).encode())
        self.fake(None, sw.SHARE_PY, [], stdin=json.dumps({"device": "desk", "name": "desk", "add": [{"key": "files:shop", "spec": {"kind": "files", "project": "shop"}}]}).encode())
        made = []
        for app in (self.laptop, self.web):
            app.store.add_manual({"name": "shop", "remote": "~/shop"}) if "shop" not in self.names(app) else None
            app._create_session = lambda body, app=app: made.append((app.web, body.get("resume") or body["kind"])) or {}
            app.share_sync()
            app.share_sync()
        self.assertEqual(sorted(x for w, x in made if not w), ["c2", "files"], "the desk's chat stays on the desk")
        self.assertEqual(sorted(x for w, x in made if w), ["c1", "c2", "files"], "the web follows every chat")

    def share(self, dev, name, projects=(), add=()):
        self.fake(None, sw.SHARE_PY, [], stdin=json.dumps({"device": dev, "name": name, "projects": list(projects), "add": list(add)}).encode())

    def git_repo(self, d):
        import subprocess
        os.makedirs(d)
        g = ["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", d], check=True)
        Path(d, "a.txt").write_text("1")
        subprocess.run(g + ["add", "."], check=True)
        subprocess.run(g + ["commit", "-qm", "one"], check=True)
        return g

    def test_own_copy_names(self):
        self.assertEqual(sw.copy_base("~/shop@esipc"), "~/shop")
        self.assertEqual(sw.copy_base("/home/esi/shop"), "")
        self.assertEqual(sw.layout_key("/home/esi/shop@desk"), "shop", "a copy shares the layout of its folder")
        self.assertEqual(sw.copy_suffix("ESI-PC.local"), "esi-pc-local")
        self.assertEqual(sw.home_rel("/home/esi/shop@desk"), "shop@desk")

    def test_own_copy_script_makes_and_checks_the_copy(self):
        import subprocess
        g = self.git_repo(os.path.join(self.server_home.name, "shop"))
        self.assertEqual(self.fake(None, sw.OWN_COPY_PY, ["make", "~/shop", "~/shop@desk"]), {"ok": True, "skipped": []})
        copy = os.path.join(self.server_home.name, "shop@desk")
        self.assertEqual(Path(copy, "a.txt").read_text(), "1")
        self.assertIn("already exists", self.fake(None, sw.OWN_COPY_PY, ["make", "~/shop", "~/shop@desk"])["error"])
        self.assertTrue(self.fake(None, sw.OWN_COPY_PY, ["check", "~/shop", "~/shop@desk"])["ok"])
        Path(copy, "a.txt").write_text("2")
        r = self.fake(None, sw.OWN_COPY_PY, ["check", "~/shop", "~/shop@desk"])
        self.assertFalse(r["ok"])
        self.assertIn("uncommitted", r["why"][0])
        gc = ["git", "-C", copy] + g[3:]
        subprocess.run(gc + ["commit", "-qam", "two"], check=True)
        self.assertIn("same commit", self.fake(None, sw.OWN_COPY_PY, ["check", "~/shop", "~/shop@desk"])["why"][0])

    def test_a_project_another_computer_syncs_gets_its_own_server_copy(self):
        """User 2026-11-27: each computer keeps syncing with the server, but gets the other's changes only via git."""
        self.git_repo(os.path.join(self.server_home.name, "shop"))
        self.share("lap", "laptop", [{"name": "shop", "remote": "~/shop", "here": True}])
        desk = self.laptop
        desk.device_name = lambda: "desk"
        desk.share_sync()
        runs = []
        desk.run_script = lambda argv, **kw: runs.append(argv) or (0, "Project 'shop' added")
        desk.add_project({"mode": "download", "remote": "~/shop", "name": "shop"})
        self.assertEqual(runs[-1][:2], ["add-server", "~/shop@desk"])
        self.assertTrue(os.path.isdir(os.path.join(self.server_home.name, "shop@desk")))
        desk.add_project({"mode": "download", "remote": "~/shop", "name": "shop"})
        self.assertEqual(runs[-1][1], "~/shop@desk", "the copy made before is used again")
        desk.store.settings["ownCopy"] = False
        desk.add_project({"mode": "download", "remote": "~/shop", "name": "shop"})
        self.assertEqual(runs[-1][1], "~/shop", "setting off (old True/False): the shared folder")
        # auto (default): only while the laptop is on; it has not been seen for an hour → the shared folder
        desk.store.settings["ownCopy"] = "auto"
        desk._share_devs["lap"]["at"] -= 3600
        self.assertFalse(desk._copy_of_other("~/shop", online_only=True))
        self.assertTrue(desk._copy_of_other("~/shop"))
        import shutil
        shutil.rmtree(os.path.join(self.server_home.name, "shop@desk"))
        desk.add_project({"mode": "download", "remote": "~/shop", "name": "shop"})
        self.assertEqual(runs[-1][1], "~/shop", "auto, the other computer off: one shared folder")
        desk.store.settings["ownCopy"] = "always"
        desk.add_project({"mode": "download", "remote": "~/shop", "name": "shop"})
        self.assertEqual(runs[-1][1], "~/shop@desk", "always: own copy even when it is off")
        self.assertEqual(sw.App.own_copy_mode(desk, {"ownCopy": True}), "auto")
        with self.assertRaises(sw.ApiError):
            desk.store.update_settings({"ownCopy": "sometimes"})
        desk.add_project({"mode": "download", "remote": "~/blog", "name": "blog"})
        self.assertEqual(runs[-1][1], "~/blog")

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs a non-root unix user")
    def test_own_copy_skips_an_unreadable_database_folder_and_never_syncs_it(self):
        """User 2026-11-28: cheshmeh's docker/data/main-db (owned by the container) broke the copy: Permission denied."""
        self.git_repo(os.path.join(self.server_home.name, "shop"))
        db = Path(self.server_home.name, "shop", "docker", "data", "main-db")
        db.mkdir(parents=True)
        Path(db, "x").write_text("x")
        Path(self.server_home.name, "shop", "node_modules").mkdir()
        os.chmod(db, 0)
        try:
            self.share("lap", "laptop", [{"name": "shop", "remote": "~/shop", "here": True}])
            desk = self.laptop
            desk.device_name = lambda: "desk"
            desk.share_sync()
            runs = []
            desk.run_script = lambda argv, **kw: runs.append(argv) or (0, "Project 'shop' added")
            desk.add_project({"mode": "download", "remote": "~/shop", "name": "shop"})
            copy = Path(self.server_home.name, "shop@desk")
            self.assertTrue(Path(copy, "a.txt").is_file())
            self.assertFalse(Path(copy, "docker", "data", "main-db").exists())
            self.assertFalse(Path(copy, "node_modules").exists(), "Not synced paths are not copied")
            ign = runs[-1][runs[-1].index("--ignores") + 1].split(",")
            self.assertIn("/docker/data/main-db", ign)
            self.assertIn("node_modules", ign)
        finally:
            os.chmod(db, 0o755)

    def test_own_copy_and_back_to_the_shared_folder(self):
        import subprocess
        self.git_repo(os.path.join(self.server_home.name, "shop"))
        desk = self.laptop
        desk.device_name = lambda: "desk"
        p = {"name": "shop", "remote": "~/shop", "source": "mutagen"}
        desk._synced = lambda name: p
        runs = []
        desk.run_script = lambda argv, **kw: runs.append(argv) or (0, "")
        self.assertEqual(desk.own_copy("shop", True)["remote"], "~/shop@desk")
        self.assertEqual(runs[-1], ["move", "shop", "--remote", "~/shop@desk", "--yes", "--no-wait", "--merge-server"])
        p["remote"] = "~/shop@desk"
        with self.assertRaises(sw.ApiError):
            desk.own_copy("shop", True)
        Path(self.server_home.name, "shop@desk", "a.txt").write_text("changed")
        with self.assertRaises(sw.ApiError) as e:
            desk.own_copy("shop", False)
        self.assertIn("uncommitted", e.exception.message if hasattr(e.exception, "message") else str(e.exception))
        subprocess.run(["git", "-C", os.path.join(self.server_home.name, "shop@desk"), "checkout", "-q", "a.txt"], check=True)
        r = desk.own_copy("shop", False)
        self.assertEqual((r["remote"], r["left"]), ("~/shop", "~/shop@desk"))
        self.assertEqual(runs[-1][3], "~/shop")
        self.assertTrue(os.path.isdir(os.path.join(self.server_home.name, "shop@desk")), "the copy stays")
        import shutil
        shutil.rmtree(os.path.join(self.server_home.name, "shop@desk"))
        p["remote"] = "~/shop"
        desk.run_script = lambda argv, **kw: (1, "boom")
        with self.assertRaises(sw.ApiError):
            desk.own_copy("shop", True)
        self.assertFalse(os.path.isdir(os.path.join(self.server_home.name, "shop@desk")), "the sync could not switch: the new copy is removed")

    def test_web_lists_a_computers_own_copy_and_opens_its_chats_there(self):
        self.share("lap", "laptop", [{"name": "shop", "remote": "~/shop", "here": True}])
        self.share("desk", "desk", [{"name": "shop", "remote": "~/shop@desk", "here": True}],
                   [{"key": "chat:c9", "spec": {"kind": "chat", "project": "shop", "resume": "c9", "remote": "~/shop@desk"}}])
        made = []
        self.web._create_session = lambda body: made.append(body["project"]) or {}
        self.web.share_sync()
        self.web.share_sync()
        self.assertIn("shop@desk", self.names(self.web))
        self.assertEqual(next(p for p in self.web.projects(max_age=0) if p["name"] == "shop@desk")["remote"], "~/shop@desk")
        self.assertEqual(made, ["shop@desk"], "the desk's chat opens in the desk's copy, not in the laptop's folder")
        self.laptop.share_sync()
        self.assertNotIn("shop@desk", self.names(self.laptop), "the installed app does not list another computer's copy")

    def test_auto_pull_only_when_new_commits_and_nothing_changed_here(self):
        calls = []

        class Repo:
            def __init__(self, app, p, r, where):
                self.r = r

            def status(self):
                return {"behind": 2, "ahead": 0, "files": [] if self.r != "dirty" else ["x"]}

            def run(self, args, timeout=60):
                calls.append((self.r, args[0]))
                return 0, ""

        p = {"name": "shop"}
        with mock.patch.object(sw, "git_repos", return_value={"repos": ["", "dirty"], "where": "server"}), mock.patch.object(sw, "GitRepo", Repo):
            self.assertTrue(self.laptop._auto_pull(p, {"behind": 0}, {"behind": 2}))
            self.assertEqual(calls, [("", "pull")], "a repo with changes is left alone")
            calls.clear()
            self.assertFalse(self.laptop._auto_pull(p, {"behind": 2}, {"behind": 2}), "nothing new")
            self.assertFalse(self.laptop._auto_pull(p, {"behind": 0}, {"behind": 2, "changes": 1}))
            self.assertFalse(self.laptop._auto_pull(p, {"behind": 0}, {"behind": 2, "ahead": 1}))
            self.assertEqual(calls, [])

    def proj(self, app, name):
        return next(p for p in app.projects(max_age=0) if p["name"] == name)

    def test_groups_colours_and_order_are_the_same_on_every_device(self):
        self.laptop.store.set_group("shop", "Work")           # set before sharing: the first round seeds it
        self.laptop.share_sync()
        self.web.share_sync()
        self.assertEqual(self.proj(self.web, "shop")["group"], "Work")
        # changed on the web: the laptop follows
        self.web.store.set_group("blog", "Work")
        self.web.store.set_group_color("Work", "#112233")
        self.web.layout_changed(names=["blog"], colors=["Work"])
        self.web.store.set_project_order(["far", "blog", "shop"])
        self.web.layout_changed(order=True)
        self.web.share_sync()
        self.laptop.share_sync()
        self.assertEqual(self.proj(self.laptop, "blog")["group"], "Work")
        self.assertEqual(self.proj(self.laptop, "shop")["groupColor"], "#112233")
        self.assertEqual([p["name"] for p in self.laptop.projects(max_age=0)], ["far", "blog", "shop"])
        # ungrouped on the laptop: the web follows, and the web's older first list does not undo it
        self.laptop.store.set_group("shop", "")
        self.laptop.layout_changed(names=["shop"])
        self.laptop.share_sync()
        self.web.share_sync()
        self.assertEqual(self.proj(self.web, "shop")["group"], "")

    def test_starter_title_colour_stars_and_chat_defaults_are_the_same_on_every_device(self):
        self.laptop.store.set_starter("shop", "Read memory first.")      # set before the new computer joined
        self.laptop.store.set_title("shop", "Shop")
        self.laptop.store.set_color("shop", "#aa3300")
        self.laptop.store.update_settings({"starred": ["cmd:Git", "proj:Files"], "chatEffort": "high"})
        self.web.store.add_manual({"name": "shop", "remote": "~/shop"})   # the same project, added there too
        self.laptop.share_sync()
        self.web.share_sync()
        p = self.proj(self.web, "shop")
        self.assertEqual((p["starter"], p["title"], p["color"]), ("Read memory first.", "Shop", "#aa3300"))
        self.assertEqual(self.web.store.settings["starred"], ["cmd:Git", "proj:Files"])
        self.assertEqual(self.web.store.settings["chatEffort"], "high")
        # changed / removed on the new one: the old one follows, its own first list does not undo it
        self.web.store.set_starter("shop", "")
        self.web.layout_changed(fields=[("shop", "starter")])
        self.web.store.update_settings({"starred": ["cmd:Git"], "chatMode": "plan"})
        self.web.layout_changed(settings=["starred", "chatMode"])
        self.web.share_sync()
        self.laptop.share_sync()
        self.assertEqual(self.proj(self.laptop, "shop")["starter"], "")
        self.assertEqual(self.laptop.store.settings["starred"], ["cmd:Git"])
        self.assertEqual(self.laptop.store.settings["chatMode"], "plan")

    def test_a_new_computer_copies_starter_names_colours_and_run_commands_from_another(self):
        """Settings → From your other computers → Copy from another computer…"""
        self.laptop.store.set_starter("shop", "Read memory first.")
        self.laptop.store.set_color("shop", "#aa3300")
        self.laptop.store.set_commands("shop", [{"name": "api", "cmd": "make api", "where": "server"},
                                                {"name": "web", "cmd": "npm start", "where": "server"}])
        d = Path(self.tmp.name) / "pc2"
        d.mkdir()
        pc2 = sw.App(sw.Store(d / "simab.db"), sw.ServerConf("esi", "h"))
        try:
            pc2.store.add_manual({"name": "my-shop", "remote": "/home/esi/shop"})     # same folder, another name
            pc2.store.set_commands("my-shop", [{"name": "web", "cmd": "npm start", "where": "server"}])
            self.laptop.share_sync()
            pc2.share_sync()
            pc2.store.set_starter("my-shop", "my own")          # changed here: the copy puts the laptop's back
            src = [x for x in pc2.copy_sources() if x["name"] == self.laptop.device_name()]
            self.assertEqual(len(src), 1)
            shop = next(p for p in src[0]["projects"] if p["name"] == "shop")
            self.assertEqual((shop["here"], shop["starter"], shop["commands"]), ("my-shop", True, 2))
            done = pc2.copy_from(src[0]["id"], ["starter", "commands"], ["~/shop"])
            self.assertEqual(done, {"starter": 1, "commands": 1}, "npm start was there already: only make api is added")
            p = next(x for x in pc2.projects(max_age=0) if x["name"] == "my-shop")
            self.assertEqual(p["starter"], "Read memory first.")
            self.assertEqual(sorted(c["cmd"] for c in p["commands"]), ["make api", "npm start"])
            self.assertEqual(pc2.copy_from(src[0]["id"], ["commands"], ["~/shop"]), {"commands": 0}, "twice: nothing doubled")
            with self.assertRaises(sw.ApiError):
                pc2.copy_from("nope", ["starter"], ["~/shop"])
        finally:
            pc2.shutdown_sessions()

    def test_a_new_computer_gets_the_not_synced_list_of_the_folder(self):
        self.laptop.share_sync()
        self.laptop.layout_changed(ignores=("shop", ["node_modules", "/dist"]))
        self.laptop.share_sync()
        self.web.share_sync()
        self.assertEqual(self.web._shared_ignores.get(sw.layout_key("/home/esi/shop")), ["node_modules", "/dist"])

    def test_the_same_folder_under_another_name_shares_its_group(self):
        self.web.store.add_manual({"name": "shop-web", "remote": "/home/esi/shop/"})
        self.web.share_sync()
        self.web.store.set_group("shop-web", "Shops")
        self.web.layout_changed(names=["shop-web"])
        self.web.share_sync()
        self.laptop.share_sync()
        self.assertEqual(self.proj(self.laptop, "shop")["group"], "Shops")
        self.assertEqual(sw.layout_key("~/shop"), sw.layout_key("/home/esi/shop/"))

    def test_lan_address(self):
        for ip in sw.lan_ips():
            self.assertFalse(ip.startswith("127."))
        self.assertEqual(self.web._layout_apply(None), False)

    def test_which_tabs_are_shared(self):
        k = sw.share_tab_key
        self.assertEqual(k({"kind": "chat", "where": "server", "resume": CONV, "project": "a"}), "chat:" + CONV)
        self.assertEqual(k({"kind": "chat", "where": "server", "project": "a"}), "", "no conversation yet")
        self.assertEqual(k({"kind": "chat", "where": "server", "resume": CONV, "watch": True}), "")
        self.assertEqual(k({"kind": "shell", "where": "server", "project": "a"}), "")
        self.assertEqual(k({"kind": "changes", "project": "a"}), "changes:a")
        self.assertEqual(k({"kind": "files", "project": ""}), "")


if __name__ == "__main__":
    unittest.main()
