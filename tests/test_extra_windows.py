"""Tabs moved out to extra windows (a second monitor): which window shows which tab."""
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gui"))
sys.path.insert(0, str(ROOT / "tests"))
import server as sw  # noqa: E402
import test_gui_server as base  # noqa: E402


class ExtraWindows(base.TmpHome):
    def setUp(self):
        super().setUp()
        self.app = sw.App(self.store, sw.ServerConf("", "h"))
        self.mut = mock.patch.object(sw, "mutagen_sessions", return_value=[])
        self.mut.start()
        self.store.add_manual({"name": "p", "local": self.tmp.name})
        self.app.invalidate()

    def tearDown(self):
        self.mut.stop()
        self.app.shutdown_sessions()
        super().tearDown()

    def files_tab(self, **kw):
        return self.app.create_session(dict({"kind": "files", "project": "p"}, **kw))

    def wins(self):
        return {s["id"]: s["win"] for s in self.app.tabs()["sessions"]}

    def test_move_to_a_window_and_back(self):
        a, b = self.files_tab(), self.files_tab()
        self.assertEqual((a["win"], a["moved"]), ("", 0.0))
        info = self.app.set_window(a["id"], "w2abc")
        self.assertEqual(info["win"], "w2abc")
        self.assertGreater(info["moved"], 0)
        self.assertEqual(self.wins(), {a["id"]: "w2abc", b["id"]: ""})
        self.assertEqual(self.app.tabs()["windows"], [{"id": "w2abc", "n": 2}])
        self.assertEqual(self.app.state()["windows"], [{"id": "w2abc", "n": 2}])
        self.app.set_window(a["id"], "")
        self.assertEqual(self.wins()[a["id"]], "")

    def test_new_tab_opened_in_an_extra_window_stays_there(self):
        t = self.files_tab(win="zz99")
        self.assertEqual(t["win"], "zz99")
        self.assertEqual(self.wins()[t["id"]], "zz99")

    def test_windows_are_numbered_from_2_and_numbers_are_reused(self):
        for w in ("aaaa", "bbbb", "cccc"):
            self.app.window_seen(w)
        self.assertEqual([x["n"] for x in self.app.live_windows()], [2, 3, 4])
        self.app.windows["bbbb"]["seen"] = time.time() - sw.WIN_TTL - 1
        self.assertEqual([x["id"] for x in self.app.live_windows()], ["aaaa", "cccc"])
        self.app.window_seen("dddd")
        self.assertEqual({x["id"]: x["n"] for x in self.app.live_windows()}, {"aaaa": 2, "cccc": 4, "dddd": 3})

    def test_closed_window_gives_its_tabs_back_unless_it_comes_back(self):
        t = self.files_tab(win="abcd")
        self.app.window_closed("abcd")
        self.assertEqual(self.wins()[t["id"]], "abcd", "still its own during the grace time")
        # a reload: the window says hello again → keeps its tabs
        self.app.window_seen("abcd")
        with mock.patch.object(sw.time, "time", return_value=time.time() + sw.WIN_GRACE + 1):
            self.assertEqual(self.wins()[t["id"]], "abcd")
        # really closed: after the grace time the tab is the main window's again, marked as moved
        self.app.window_closed("abcd")
        later = time.time() + sw.WIN_GRACE + 1
        with mock.patch.object(sw.time, "time", return_value=later):
            d = self.app.tabs()
        s = next(x for x in d["sessions"] if x["id"] == t["id"])
        self.assertEqual((s["win"], s["moved"]), ("", later))
        self.assertEqual(d["windows"], [])

    def test_silent_window_expires(self):
        t = self.files_tab(win="abcd")
        self.app.windows["abcd"]["seen"] = time.time() - sw.WIN_TTL - 1
        self.assertEqual(self.wins()[t["id"]], "")

    def test_opening_an_open_conversation_brings_its_tab_here(self):
        sid = "11111111-2222-3333-4444-555555555555"
        built = {"make_argv": None, "cwd": self.tmp.name, "title": "Claude · p", "resume": sid, "mode": "default",
                 "model": "", "relay": "", "spec": {"kind": "chat", "project": "p", "where": "server", "resume": sid}}
        c = sw.ChatSession("chat1", built)
        c.session_id = sid
        self.app.sessions["chat1"] = c
        info = self.app.create_session({"kind": "chat", "project": "p", "resume": sid, "win": "abcd"})
        self.assertEqual((info["id"], info["win"]), ("chat1", "abcd"))
        self.assertEqual(len(self.app.sessions), 1, "no second chat, no second Claude")
        c.closed = True
        del self.app.sessions["chat1"]

    def test_bad_window_ids(self):
        t = self.files_tab()
        for bad in ("../x", "A" * 3, "x" * 40, 5, "UPPER"):
            with self.assertRaises(sw.ApiError, msg=repr(bad)):
                self.app.set_window(t["id"], bad)
        with self.assertRaises(sw.ApiError):
            self.files_tab(win="no way")
        with self.assertRaises(sw.ApiError):
            self.app.set_window("nope", "abcd")


class ExtraWindowsHttp(base.HttpServer):
    """Run only this test of the HttpServer machinery."""
    pass


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(ExtraWindows))
    suite.addTest(ExtraWindowsHttp("test_windows_routes"))
    return suite


def test_windows_routes(self):
    os.makedirs(os.path.join(self.tmp.name, "q"))
    st, _ = self.req("POST", "/api/manual", {"name": "q", "local": os.path.join(self.tmp.name, "q")})
    self.assertEqual(st, 200)
    st, t = self.req("POST", "/api/sessions", {"kind": "files", "project": "q", "win": "k2k2"})
    self.assertEqual((st, t["win"]), (200, "k2k2"))
    st, d = self.req("GET", "/api/tabs")
    self.assertEqual(d["windows"], [{"id": "k2k2", "n": 2}])
    st, info = self.req("POST", f"/api/sessions/{t['id']}/win", {"win": ""})
    self.assertEqual(info["win"], "")
    self.assertEqual(self.req("POST", "/api/windows/k3k3")[0], 200)
    self.assertEqual(self.req("DELETE", "/api/windows/k3k3")[0], 200)
    self.assertEqual(self.req("POST", "/api/windows/..")[0], 400)
    self.assertEqual(self.req("POST", f"/api/sessions/{t['id']}/win", {"win": "BAD!"})[0], 400)


ExtraWindowsHttp.test_windows_routes = test_windows_routes

if __name__ == "__main__":
    unittest.main()


class TabsSurviveARestart(base.TmpHome):
    """An update / a restart of the web service: chats and Files tabs come back with the same ids."""
    def setUp(self):
        super().setUp()
        self.mut = mock.patch.object(sw, "mutagen_sessions", return_value=[])
        self.mut.start()
        self.store.add_manual({"name": "p", "local": self.tmp.name, "remote": "/srv/p"})

    def tearDown(self):
        self.mut.stop()
        super().tearDown()

    def chat(self, sid, conv):
        c = sw.ChatSession(sid, {"make_argv": lambda *a: ["/bin/sh", "-c", "cat >/dev/null"], "cwd": "/tmp", "title": "Fix login",
                                 "resume": conv, "relay": "abcdef0123456789",
                                 "spec": {"kind": "chat", "project": "p", "where": "server", "resume": conv}})
        return c

    def test_restart_brings_back_chats_and_files_with_the_same_ids(self):
        conv = "11111111-2222-3333-4444-555555555555"
        app = sw.App(self.store, sw.ServerConf("esi", "h"))
        app.invalidate()
        f = app.create_session({"kind": "files", "project": "p"})
        app.set_window(f["id"], "w2abc")
        app.sessions["aaaaaaaaaaaa"] = self.chat("aaaaaaaaaaaa", conv)
        app.sessions["bbbbbbbbbbbb"] = self.chat("bbbbbbbbbbbb", "")          # no conversation yet: not kept
        with mock.patch.object(sw.ChatSession, "shutdown", lambda self: None):
            app.shutdown_sessions()
            app.shutdown_sessions()                                        # called twice on the way out: kept once
        saved = self.store.db.get("live")
        self.assertEqual([t["id"] for t in saved["tabs"]], [f["id"], "aaaaaaaaaaaa"])

        made = []

        def fake_chat(self2, body, sid=None, ready=False):
            if body.get("kind") != "chat":
                return orig(self2, body, sid=sid, ready=ready)
            made.append((sid, body))
            self2.sessions[sid] = sw.FilesSession(sid, "p", body["title"])   # stands in for the chat
            return self2.sessions[sid].info()
        orig = sw.App._create_session
        app2 = sw.App(self.store, sw.ServerConf("esi", "h"))
        with mock.patch.object(sw.App, "_create_session", fake_chat):
            app2.restore_live(wait=True)
        self.assertEqual(list(app2.sessions), [f["id"], "aaaaaaaaaaaa"])
        self.assertEqual(app2.sessions[f["id"]].win, "w2abc")
        self.assertEqual(made[0][1]["resume"], conv)
        self.assertEqual(made[0][1]["relay"], "abcdef0123456789")
        self.assertEqual(made[0][1]["title"], "Fix login")
        # used once: the next start does not bring them back again
        app3 = sw.App(self.store, sw.ServerConf("esi", "h"))
        app3.restore_live(wait=True)
        self.assertEqual(app3.sessions, {})
        app2.sessions.clear()

    def test_installed_app_restores_only_right_after_a_restart(self):
        self.store.db.put("live", {"at": time.time() - 3600, "tabs": [{"id": "cccccccccccc", "win": "", "title": "Files · p",
                                                                    "spec": {"kind": "files", "project": "p"}}]})
        app = sw.App(self.store, sw.ServerConf("esi", "h"))
        app.restore_live(wait=True)
        self.assertEqual(app.sessions, {})
        self.store.db.put("live", {"at": time.time() - 3600, "tabs": [{"id": "cccccccccccc", "win": "", "title": "Files · p",
                                                                    "spec": {"kind": "files", "project": "p"}}]})
        web = sw.App(self.store, sw.ServerConf("esi", "h"), web=True)
        web.restore_live(wait=True)
        self.assertEqual(list(web.sessions), ["cccccccccccc"])
