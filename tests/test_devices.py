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
