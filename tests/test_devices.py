"""Projects and open tabs shared between devices (installed app ↔ web) through devices.json on the server."""
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
