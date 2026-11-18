"""Tasks: the to-do list kept on the server (~/.config/simabworkspace/tasks.json)."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gui"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as sw  # noqa: E402
import test_gui_server as base  # noqa: E402


class FakeServer:
    """remote_python, but the script runs here with HOME = a temp folder (the "server")."""
    def __init__(self, home):
        self.home = home
        self.calls = []

    def __call__(self, conf, code, args, multiplex=True, timeout=30, stdin=None):
        self.calls.append(json.loads(stdin or b"{}"))
        r = subprocess.run([sys.executable, "-I", "-c", code] + list(args), input=stdin or b"",
                           capture_output=True, timeout=timeout, env={"HOME": self.home, "PATH": os.environ.get("PATH", "")})
        if r.returncode:
            raise sw.ApiError(502, "server read failed: " + r.stderr.decode()[-300:])
        return json.loads(r.stdout.decode() or "null")


class Tasks(base.TmpHome):
    def setUp(self):
        super().setUp()
        self.server_home = tempfile.TemporaryDirectory()
        self.fake = FakeServer(self.server_home.name)
        self.rp = mock.patch.object(sw, "remote_python", self.fake)
        self.rp.start()
        self.mut = mock.patch.object(sw, "mutagen_sessions", return_value=[])
        self.mut.start()
        self.app = sw.App(self.store, sw.ServerConf("esi", "h"))

    def tearDown(self):
        self.rp.stop()
        self.mut.stop()
        self.app.shutdown_sessions()
        self.server_home.cleanup()
        super().tearDown()

    def texts(self, r):
        return [t["text"] for t in r["tasks"]]

    def test_add_tick_edit_move_delete(self):
        self.assertEqual(self.app.tasks(), {"tasks": [], "rev": 0})
        a = self.app.tasks({"op": "add", "text": "  fix login  ", "project": "shop", "prio": 1})
        t = a["tasks"][0]
        self.assertEqual((t["text"], t["project"], t["prio"], t["done"], a["rev"]), ("fix login", "shop", 1, False, 1))
        self.app.tasks({"op": "add", "text": "صفحه پرداخت"})
        r = self.app.tasks({"op": "add", "text": "deploy", "project": "shop"})
        self.assertEqual(self.texts(r), ["fix login", "صفحه پرداخت", "deploy"], "new ones at the end")
        ids = [x["id"] for x in r["tasks"]]
        r = self.app.tasks({"op": "update", "id": ids[0], "done": True})
        self.assertTrue(r["tasks"][0]["done"])
        self.assertGreater(r["tasks"][0]["doneAt"], 0)
        r = self.app.tasks({"op": "update", "id": ids[0], "done": False, "text": "fix login page", "prio": 3})
        self.assertEqual((r["tasks"][0]["done"], r["tasks"][0]["doneAt"], r["tasks"][0]["text"], r["tasks"][0]["prio"]),
                         (False, 0, "fix login page", 3))
        r = self.app.tasks({"op": "move", "id": ids[2], "before": ids[0]})
        self.assertEqual(self.texts(r), ["deploy", "fix login page", "صفحه پرداخت"])
        r = self.app.tasks({"op": "move", "id": ids[2], "after": ids[1]})
        self.assertEqual(self.texts(r), ["fix login page", "صفحه پرداخت", "deploy"])
        r = self.app.tasks({"op": "order", "ids": [ids[2], ids[0]]})        # only these two swap places
        self.assertEqual(self.texts(r), ["deploy", "صفحه پرداخت", "fix login page"])
        self.assertIn("صفحه", Path(self.server_home.name, ".config", "simabworkspace", "tasks.json").read_text(encoding="utf-8"))
        r = self.app.tasks({"op": "delete", "id": ids[1]})
        self.assertEqual(self.texts(r), ["deploy", "fix login page"])
        f = Path(self.server_home.name, ".config", "simabworkspace", "tasks.json")
        self.assertEqual(json.loads(f.read_text(encoding="utf-8"))["rev"], r["rev"])

    def test_board_columns(self):
        r = self.app.tasks({"op": "add", "text": "idea", "status": "backlog"})
        self.assertEqual((r["tasks"][0]["status"], r["tasks"][0]["done"]), ("backlog", False))
        r = self.app.tasks({"op": "add", "text": "next"})
        self.assertEqual(r["tasks"][1]["status"], "todo", "To do by default")
        a, b = (x["id"] for x in r["tasks"])
        # dragged into In progress, before nothing (end of the column)
        r = self.app.tasks({"op": "move", "id": a, "status": "doing"})
        self.assertEqual([(x["text"], x["status"]) for x in r["tasks"]], [("next", "todo"), ("idea", "doing")])
        # dragged into Done, above another card
        r = self.app.tasks({"op": "move", "id": b, "status": "done", "before": a})
        self.assertEqual([(x["text"], x["status"], x["done"]) for x in r["tasks"]], [("next", "done", True), ("idea", "doing", False)])
        self.assertGreater(r["tasks"][0]["doneAt"], 0)
        r = self.app.tasks({"op": "update", "id": b, "status": "todo"})
        self.assertEqual((r["tasks"][0]["done"], r["tasks"][0]["doneAt"]), (False, 0))
        r = self.app.tasks({"op": "update", "id": b, "done": True})            # 1.41.0 clients
        self.assertEqual(r["tasks"][0]["status"], "done")
        with self.assertRaises(sw.ApiError):
            self.app.tasks({"op": "move", "id": a, "status": "later"})

    def test_lists_from_1_41_0_get_a_status(self):
        d = Path(self.server_home.name, ".config", "simabworkspace")
        d.mkdir(parents=True)
        (d / "tasks.json").write_text(json.dumps({"rev": 3, "tasks": [
            {"id": "a" * 12, "text": "x", "done": True}, {"id": "b" * 12, "text": "y", "done": False}]}))
        self.assertEqual([t["status"] for t in self.app.tasks()["tasks"]], ["done", "todo"])

    def test_clear_done_of_one_project(self):
        ids = [self.app.tasks({"op": "add", "text": n, "project": p})["tasks"][-1]["id"]
               for n, p in (("a", "shop"), ("b", "cheshmeh"), ("c", ""), ("d", "shop"))]
        for i in ids[:3]:
            self.app.tasks({"op": "update", "id": i, "done": True})
        self.assertEqual(self.texts(self.app.tasks({"op": "clear", "project": "shop"})), ["b", "c", "d"])
        self.assertEqual(self.texts(self.app.tasks({"op": "clear", "project": ""})), ["b", "d"])
        self.assertEqual(self.texts(self.app.tasks({"op": "clear"})), ["d"])

    def test_bad_operations_are_refused_here(self):
        for bad in ({"op": "drop"}, {"op": "add", "text": "  "}, {"op": "add", "text": "x" * 1001},
                    {"op": "update", "text": "x"}, {"op": "update", "id": "../../etc", "text": "x"},
                    {"op": "add", "text": "x", "prio": 7}, {"op": "move", "id": "a" * 12, "before": "nope"},
                    {"op": "order", "ids": ["a" * 12, "a" * 12]}, {"op": "add", "text": ["x"]}, "x"):
            with self.assertRaises(sw.ApiError, msg=bad):
                self.app.tasks(bad)
        self.assertEqual(self.fake.calls, [], "nothing reached the server")
        # unknown fields are dropped, an unknown id changes nothing
        self.app.tasks({"op": "add", "text": "x", "evil": 1, "done": True})
        self.assertNotIn("evil", self.fake.calls[-1])
        r = self.app.tasks({"op": "update", "id": "b" * 12, "text": "y"})
        self.assertEqual(self.texts(r), ["x"])

    def test_two_devices_at_once_lose_nothing(self):
        errs = []

        def add(n):
            try:
                self.app.tasks({"op": "add", "text": "t%d" % n})
            except Exception as e:          # noqa: BLE001
                errs.append(e)
        th = [threading.Thread(target=add, args=(i,)) for i in range(12)]
        [t.start() for t in th]
        [t.join() for t in th]
        self.assertEqual(errs, [])
        r = self.app.tasks()
        self.assertEqual(sorted(self.texts(r)), sorted("t%d" % i for i in range(12)))
        self.assertEqual(r["rev"], 12)

    def test_a_broken_file_starts_empty(self):
        d = Path(self.server_home.name, ".config", "simabworkspace")
        d.mkdir(parents=True)
        (d / "tasks.json").write_text("{not json")
        self.assertEqual(self.app.tasks()["tasks"], [])
        self.assertEqual(self.texts(self.app.tasks({"op": "add", "text": "again"})), ["again"])

    def test_needs_a_server(self):
        app = sw.App(self.store, sw.ServerConf("", ""))
        with self.assertRaises(sw.ApiError):
            app.tasks()
        with self.assertRaises(sw.ApiError):
            app.create_session({"kind": "tasks"})

    def test_tasks_tab_is_a_tab_that_survives_a_restart(self):
        info = self.app.create_session({"kind": "tasks"})
        self.assertEqual((info["title"], info["spec"]["kind"], info["spec"]["project"]), ("Tasks", "tasks", ""))
        with mock.patch.object(sw.ChatSession, "shutdown", lambda self: None):
            self.app.shutdown_sessions()
        self.assertEqual([t["id"] for t in self.store.db.get("live")["tabs"]], [info["id"]])
        app2 = sw.App(self.store, sw.ServerConf("esi", "h"))
        app2.restore_live(wait=True)
        self.assertEqual(app2.get(info["id"]).spec["kind"], "tasks")
        app2.shutdown_sessions()


class TasksHttp(base.HttpServer):
    def test_routes_and_web(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.app._conf = sw.ServerConf("esi", "h")
        with mock.patch.object(sw, "remote_python", FakeServer(home.name)):
            st, d = self.req("POST", "/api/tasks", {"op": "add", "text": "write tests", "project": "p", "prio": 2})
            self.assertEqual((st, [t["text"] for t in d["tasks"]]), (200, ["write tests"]))
            st, d = self.req("GET", "/api/tasks")
            self.assertEqual((st, d["rev"]), (200, 1))
            st, d = self.req("POST", "/api/tasks", {"op": "nope"})
            self.assertEqual(st, 400)
            st, d = self.req("POST", "/api/sessions", {"kind": "tasks"})
            self.assertEqual((st, d["spec"]["kind"]), (200, "tasks"))
        self.assertNotIn(("GET", "tasks"), sw.WEB_BLOCKED)
        self.assertNotIn(("POST", "tasks"), sw.WEB_BLOCKED)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(Tasks))
    suite.addTest(TasksHttp("test_routes_and_web"))       # not the HttpServer tests again
    return suite


if __name__ == "__main__":
    unittest.main()
