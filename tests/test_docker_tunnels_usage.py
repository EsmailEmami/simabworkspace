"""Docker panel, port tunnels and Claude usage — run with:  python3 -m unittest discover -s tests -v"""
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gui"))
import server as sw  # noqa: E402


def wait_for(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


class TmpHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        self.env.start()
        self.store = sw.Store(Path(self.tmp.name) / "gui.json")
        self.projects = [{"name": "cheshmeh", "remote": "~/cheshmeh", "local": "", "localExists": False,
                          "source": "manual", "endpoint": {}, "commands": [], "sync": None},
                         {"name": "shop", "remote": "/srv/shop", "local": "", "localExists": False,
                          "source": "manual", "endpoint": {"port": "2222"}, "commands": [], "sync": None}]
        self.app = sw.App(self.store, sw.ServerConf("esi", "h", "225", ""))
        self.app.projects = lambda max_age=3.0: self.projects

    def tearDown(self):
        self.app.shutdown_sessions()
        self.env.stop()
        self.tmp.cleanup()


def container(**kw):
    c = {"id": "a1", "name": "cheshmeh-api-1", "image": "api", "state": "running", "health": "", "exitCode": 0,
         "startedAt": "", "finishedAt": "", "restarts": 0, "compose": "cheshmeh", "service": "api",
         "workdir": "/home/esi/cheshmeh/docker/local", "configFiles": "/home/esi/cheshmeh/docker/local/docker-compose.yml",
         "ports": [], "proj": "cheshmeh"}
    c.update(kw)
    return c


# --------------------------------------------------------------------------- docker
FAKE_DOCKER = r'''#!/usr/bin/env python3
import sys, json, os
a = sys.argv[1:]
root = os.environ["ROOT"]
if a[:1] == ["ps"]:
    print("aaa111\nbbb222\nccc333")
elif a[:1] == ["inspect"]:
    def c(i, name, wd, svc, state="running", code=0, ports=None, health=None):
        st = {"Status": state, "ExitCode": code, "StartedAt": "2026-11-14T10:00:00Z", "FinishedAt": ""}
        if health:
            st["Health"] = {"Status": health}
        return {"Id": i + "0" * 52, "Name": "/" + name, "RestartCount": 2,
                "Config": {"Image": "img:" + svc, "Labels": {
                    "com.docker.compose.project": "cheshmeh", "com.docker.compose.service": svc,
                    "com.docker.compose.project.working_dir": wd,
                    "com.docker.compose.project.config_files": wd + "/a.yml," + wd + "/b.yml"}},
                "State": st, "NetworkSettings": {"Ports": ports or {}}}
    print(json.dumps([
        c("aaa111", "cheshmeh-api-1", root + "/cheshmeh/docker/local", "api",
          ports={"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18080"}, {"HostIp": "::", "HostPort": "18080"}],
                 "9000/tcp": None}),
        c("bbb222", "cheshmeh-db-1", root + "/cheshmeh/docker/local", "db", health="unhealthy"),
        c("ccc333", "other-1", root + "/elsewhere", "x", state="exited", code=1)]))
'''


class DockerScript(unittest.TestCase):
    """DOCKER_PY is what runs on the server: feed it a fake docker."""

    def test_lists_only_containers_of_the_projects(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.realpath(d)
            os.makedirs(root + "/cheshmeh/docker/local")
            exe = Path(root, "bin", "docker")
            exe.parent.mkdir()
            exe.write_text(FAKE_DOCKER)
            exe.chmod(0o755)
            env = dict(os.environ, PATH=str(exe.parent) + ":" + os.environ["PATH"], ROOT=root)
            r = subprocess.run([sys.executable, "-c", sw.DOCKER_PY, f"cheshmeh={root}/cheshmeh", f"top={root}"],
                               env=env, capture_output=True, text=True, timeout=20)
            out = json.loads(r.stdout)
        by = {c["name"]: c for c in out["containers"]}
        self.assertEqual(by["cheshmeh-api-1"]["proj"], "cheshmeh")       # the longest folder wins
        self.assertEqual(by["other-1"]["proj"], "top")
        api = by["cheshmeh-api-1"]
        self.assertEqual(api["id"], "aaa111000000")
        self.assertEqual(api["ports"], [{"host": 18080, "port": 8080, "proto": "tcp", "ip": "0.0.0.0"}])   # no duplicate for ::
        self.assertEqual(api["service"], "api")
        self.assertTrue(api["configFiles"].endswith("/a.yml," + root + "/cheshmeh/docker/local/b.yml"))
        self.assertEqual(by["cheshmeh-db-1"]["health"], "unhealthy")
        self.assertEqual((by["other-1"]["state"], by["other-1"]["exitCode"]), ("exited", 1))

    def test_no_docker(self):
        env = dict(os.environ, PATH="/nonexistent")
        r = subprocess.run([sys.executable, "-c", sw.DOCKER_PY, "p=/tmp"], env=env, capture_output=True, text=True, timeout=20)
        self.assertIn("not installed", json.loads(r.stdout)["error"])


class DockerLogic(TmpHome):
    def test_crash_rules(self):
        run = container()
        self.assertEqual(sw.docker_crash(run, container(state="exited", exitCode=1)), "stopped with exit code 1")
        self.assertEqual(sw.docker_crash(run, container(state="exited", exitCode=0)), "")          # a normal stop
        self.assertEqual(sw.docker_crash(run, container(state="exited", exitCode=137)), "")        # docker stop
        self.assertEqual(sw.docker_crash(run, container(state="restarting")), "keeps restarting")
        self.assertEqual(sw.docker_crash(container(state="restarting"), container(state="restarting")), "")  # said once
        self.assertEqual(sw.docker_crash(run, container(health="unhealthy")), "unhealthy")
        self.assertEqual(sw.docker_crash(container(state="exited", exitCode=1), container(state="exited", exitCode=1)), "")

    def test_tab_commands_are_quoted(self):
        c = container(name="x-1", workdir="/srv/my app", configFiles="/srv/my app/a.yml,/srv/my app/b.yml")
        self.assertEqual(sw.docker_tab_inner(c, "logs"), "docker logs -f --tail 300 x-1")
        self.assertIn("docker exec -it x-1 sh -c", sw.docker_tab_inner(c, "shell"))
        rb = sw.docker_tab_inner(c, "rebuild")
        self.assertTrue(rb.startswith("cd '/srv/my app' && docker compose -p cheshmeh -f '/srv/my app/a.yml' -f '/srv/my app/b.yml' up -d --build api"))
        self.assertEqual(shlex.split(rb)[1], "/srv/my app")
        with self.assertRaises(sw.ApiError):
            sw.docker_tab_inner(container(compose=""), "rebuild")
        self.assertIn("usermod -aG docker", sw.docker_error("Got permission denied while trying to connect to the Docker daemon socket"))

    def test_one_call_per_server_and_events(self):
        calls = []

        def fake(conf, code, args, multiplex=True, timeout=30):
            calls.append((conf.port, args))
            if conf.port == "2222":
                return {"containers": [container(id="s1", name="shop-web-1", proj="shop")]}
            return {"containers": [container(), container(id="b2", name="cheshmeh-db-1", service="db")], "error": ""}
        with mock.patch.object(sw, "remote_python", side_effect=fake):
            d = self.app.docker_list()
            self.assertEqual(sorted(calls), [("2222", ["shop=/srv/shop"]), ("225", ["cheshmeh=~/cheshmeh"])])
            self.app._docker_update(d)
            self.assertEqual(self.app.docker_summary()["events"], [])          # the first look never alarms
            s = self.app.docker_summary()["summary"]
            self.assertEqual(s["cheshmeh"], {"total": 2, "running": 2, "bad": 0})
            # the api crashes; the db is stopped by us (quiet)
            self.app._docker_quiet["b2"] = time.time()
            self.app._docker_update({"containers": [container(state="exited", exitCode=2),
                                                    container(id="b2", name="cheshmeh-db-1", state="exited", exitCode=1),
                                                    container(id="s1", name="shop-web-1", proj="shop")]})
            ev = self.app.docker_summary()["events"]
            self.assertEqual([(e["name"], e["why"]) for e in ev], [("cheshmeh-api-1", "stopped with exit code 2")])
            self.assertEqual(self.app.docker_summary(since=ev[-1]["seq"])["events"], [])
            self.assertEqual(self.app.docker_summary()["summary"]["cheshmeh"], {"total": 2, "running": 0, "bad": 2})
            self.assertTrue(any(a["kind"] == "docker-crash" for a in self.app.activity.list()))

    def test_update_of_one_project_keeps_the_others(self):
        self.app._docker_update({"containers": [container(), container(id="s1", name="shop-web-1", proj="shop")]})
        self.app._docker_update({"containers": []}, only="cheshmeh")
        self.assertEqual(list(self.app.docker_summary()["summary"]), ["shop"])

    def test_actions_check_the_container(self):
        calls = []

        def fake(conf, code, args, multiplex=True, timeout=30):
            calls.append(args)
            if code == sw.DOCKER_ACT_PY:
                return {"code": 0, "err": ""}
            return {"containers": [container()]}
        with mock.patch.object(sw, "remote_python", side_effect=fake):
            self.app.docker_action("cheshmeh", "cheshmeh-api-1", "restart")
            self.assertIn(["restart", "cheshmeh-api-1"], calls)
            self.assertIn("a1", self.app._docker_quiet)
            for bad in ("rm", "exec"):
                with self.assertRaises(sw.ApiError):
                    self.app.docker_action("cheshmeh", "cheshmeh-api-1", bad)
            with self.assertRaises(sw.ApiError) as cm:
                self.app.docker_action("cheshmeh", "other-container", "stop")
            self.assertEqual(cm.exception.status, 404)
            with self.assertRaises(sw.ApiError):
                self.app.docker_action("cheshmeh", "x; rm -rf /", "stop")

    def test_docker_tab_is_built_by_the_app_only(self):
        with mock.patch.object(sw, "remote_python", return_value={"containers": [container()]}), \
                mock.patch.object(sw.Session, "start", lambda self: None):
            info = self.app.create_session({"kind": "docker", "project": "cheshmeh", "container": "cheshmeh-api-1", "action": "logs",
                                            "_docker": {"inner": "evil", "title": "x", "container": "x", "action": "logs"}})
        s = self.app.sessions[info["id"]]
        self.assertEqual(info["title"], "logs · api")
        self.assertEqual((s.spec["kind"], s.spec["where"], s.spec["container"], s.spec["action"]), ("docker", "server", "cheshmeh-api-1", "logs"))
        argv = s.built["argv"]
        self.assertIn("docker logs -f --tail 300 cheshmeh-api-1", argv[-1])
        self.assertNotIn("evil", argv[-1])
        with self.assertRaises(sw.ApiError):          # a client cannot bring its own command
            sw.build_spec({"kind": "docker", "project": "cheshmeh"}, self.app.conf, self.store, self.projects)


# --------------------------------------------------------------------------- tunnels
class FakeProc:
    def __init__(self, exit_after=None, err=b""):
        self.exit_after = exit_after
        self.t0 = time.time()
        self.code = None
        self.stderr = __import__("io").BytesIO(err)

    def poll(self):
        if self.code is None and self.exit_after is not None and time.time() - self.t0 > self.exit_after:
            self.code = 255
        return self.code

    def terminate(self):
        self.code = -15


class Tunnels(TmpHome):
    def test_clean(self):
        t = sw.clean_tunnels([{"name": "", "remotePort": "5432"}, {"name": "web", "remotePort": 4200, "localPort": 18200, "http": True, "on": True}])
        self.assertEqual((t[0]["name"], t[0]["localPort"], t[0]["remoteHost"], t[0]["on"]), ("port 5432", 5432, "127.0.0.1", False))
        self.assertEqual((t[1]["localPort"], t[1]["http"], t[1]["on"]), (18200, True, True))
        for bad in ([{"remotePort": 0}], [{"remotePort": 70000}], [{"remotePort": "x"}],
                    [{"remotePort": 1, "remoteHost": "a b"}], [{"remotePort": 1, "remoteHost": "-oProxyCommand=x"}],
                    [{"remotePort": 1}, {"remotePort": 1}]):
            with self.assertRaises(sw.ApiError, msg=bad):
                sw.clean_tunnels(bad)

    def test_argv_binds_to_localhost_only(self):
        a = sw.tunnel_argv(sw.ServerConf("esi", "h", "225", ""), {"localPort": 15432, "remoteHost": "db", "remotePort": 5432})
        self.assertIn("127.0.0.1:15432:db:5432", a)
        self.assertEqual(a[a.index("127.0.0.1:15432:db:5432") - 1], "-L")
        self.assertIn("ExitOnForwardFailure=yes", a)
        self.assertIn("BatchMode=yes", a)
        self.assertNotIn("ControlMaster=auto", a)           # its own connection
        self.assertEqual(a[-1], "esi@h")

    def test_up_then_drop_then_retry(self):
        procs = []

        def popen(argv):
            procs.append(FakeProc(exit_after=3 if not procs else None, err=b"Warning: Permanently added\nConnection reset by peer\n"))
            return procs[-1]
        t = sw.Tunnel("p", {"localPort": 1, "remoteHost": "127.0.0.1", "remotePort": 2}, lambda: sw.ServerConf("u", "h"),
                      popen=popen, free=lambda p: True)
        t.start()
        self.assertTrue(wait_for(lambda: t.state == "up", 5))
        self.assertTrue(wait_for(lambda: t.state == "error", 5))
        self.assertIn("Connection reset by peer", t.error)
        self.assertIn("retrying", t.error)
        self.assertTrue(wait_for(lambda: len(procs) == 2, 6))
        t.stop()
        self.assertEqual(procs[-1].code, -15)
        self.assertEqual(t.info()["state"], "off")

    def test_port_in_use_stops(self):
        t = sw.Tunnel("p", {"localPort": 5432, "remoteHost": "127.0.0.1", "remotePort": 5432}, lambda: sw.ServerConf("u", "h"),
                      popen=lambda a: self.fail("must not start ssh"), free=lambda p: False)
        t.start()
        self.assertTrue(wait_for(lambda: t.state == "error"))
        self.assertIn("already used on this machine", t.error)
        self.assertFalse(t.info()["on"])

    def test_low_ports_get_a_usable_local_port(self):
        t = sw.clean_tunnels([{"remotePort": 501}, {"remotePort": 80, "localPort": 8080}])
        self.assertEqual([x["localPort"] for x in t], [10501, 8080])
        if os.geteuid() != 0:
            self.assertIn("use a local port like 10501", sw.port_problem(501))
        self.assertIn("already used", sw.port_problem(15432))

    def test_local_port_free(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        try:
            self.assertFalse(sw.local_port_free(s.getsockname()[1]))
        finally:
            s.close()

    def test_app_saves_starts_and_toggles(self):
        started, stopped = [], []
        with mock.patch.object(sw.Tunnel, "start", lambda self: (started.append(self.spec["localPort"]), setattr(self, "wanted", True))), \
                mock.patch.object(sw.Tunnel, "stop", lambda self: (stopped.append(self.spec["localPort"]), setattr(self, "wanted", False))), \
                mock.patch.object(sw, "local_port_free", return_value=True):
            out = self.app.set_tunnels("cheshmeh", [{"name": "db", "remotePort": 5432, "on": True}, {"name": "web", "remotePort": 4200}])
            self.assertEqual(started, [5432])
            self.assertEqual([t["name"] for t in out], ["db", "web"])
            ids = [t["id"] for t in out]
            self.assertEqual(json.loads(self.store.path.read_text())["projects"]["cheshmeh"]["tunnels"][0]["on"], True)
            self.app.toggle_tunnel("cheshmeh", ids[1], True)
            self.assertEqual(started, [5432, 4200])
            self.app.toggle_tunnel("cheshmeh", ids[0], False)
            self.assertEqual(stopped, [5432])
            # a different server port → the tunnel is replaced
            self.app.set_tunnels("cheshmeh", [{"id": ids[1], "name": "web", "remotePort": 4300, "localPort": 4200, "on": True}])
            self.assertEqual(stopped[-1], 4200)
            self.assertEqual(started[-1], 4200)
            self.assertEqual(len(self.app.tunnels), 1)
            with self.assertRaises(sw.ApiError):          # local port of another project
                self.app.set_tunnels("shop", [{"remotePort": 80, "localPort": 4200}])
            with self.assertRaises(sw.ApiError):
                self.app.toggle_tunnel("cheshmeh", "nope", True)
            # after a restart, tunnels that were on come back by themselves
            app2 = sw.App(sw.Store(self.store.path), sw.ServerConf("esi", "h", "225", ""))
            app2.start_tunnels()
            self.assertEqual(sorted(k[0] for k in app2.tunnels), ["cheshmeh"])
            self.assertEqual(started[-1], 4200)

    def test_toggle_refuses_a_busy_port(self):
        with mock.patch.object(sw.Tunnel, "start", lambda self: None):
            ids = [t["id"] for t in self.app.set_tunnels("cheshmeh", [{"remotePort": 5432}])]
            with mock.patch.object(sw, "local_port_free", return_value=False):
                with self.assertRaises(sw.ApiError) as cm:
                    self.app.toggle_tunnel("cheshmeh", ids[0], True)
        self.assertEqual(cm.exception.status, 409)


# --------------------------------------------------------------------------- usage
class ClaudeUsage(TmpHome):
    def test_merge_limits(self):
        now = 1000.0
        info = {"status": "allowed_warning", "rateLimitType": "five_hour", "resetsAt": 5000, "utilization": 0.81,
                "unifiedWindows": {"five_hour": {"utilization": 0.81, "resetsAt": 5000, "status": "allowed_warning"},
                                   "seven_day": {"utilization": 0.2, "resetsAt": 90000}}}
        m = sw.merge_limits({}, info, now)
        self.assertEqual(m["windows"]["five_hour"], {"used": 0.81, "resetsAt": 5000, "status": "allowed_warning"})
        self.assertEqual(m["windows"]["seven_day"]["used"], 0.2)
        self.assertEqual(m["status"], "allowed_warning")
        # an event with only the top-level fields updates that window, keeps the other
        m2 = sw.merge_limits(m, {"status": "allowed", "rateLimitType": "seven_day", "utilization": 0.3, "resetsAt": 90000}, now)
        self.assertEqual((m2["windows"]["five_hour"]["used"], m2["windows"]["seven_day"]["used"]), (0.81, 0.3))
        # past its reset a window is dropped
        self.assertNotIn("five_hour", sw.merge_limits(m, None, 6000)["windows"])

    def test_cost_per_day_and_project(self):
        u = sw.Usage(Path(self.tmp.name) / "usage.json")
        today = time.strftime("%Y-%m-%d")
        u.cost("cheshmeh", 1.25)
        u.cost("cheshmeh", 0.5)
        u.cost("", 0.25)
        u.cost("shop", -3)                                  # ignored
        u.cost("shop", 2.0, day="2000-01-01")
        s = u.summary()
        self.assertEqual(s["today"], 2.0)
        self.assertEqual(s["days"][0], {"day": today, "projects": {"cheshmeh": 1.75, "home": 0.25}, "total": 2.0})
        self.assertEqual(s["week"], 2.0)
        u.limits({"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.4, "resetsAt": int(time.time()) + 600}}})
        u._save(force=True)
        again = sw.Usage(Path(self.tmp.name) / "usage.json").summary()
        self.assertEqual(again["limits"]["windows"]["five_hour"]["used"], 0.4)
        self.assertEqual(again["today"], 2.0)

    def test_chat_reports_limits_and_the_cost_of_each_answer(self):
        built = {"make_argv": None, "cwd": "/tmp", "title": "Claude · p", "resume": "", "mode": "default",
                 "model": "", "spec": {"kind": "chat", "project": "cheshmeh", "where": "server"}}
        s = sw.ChatSession("c1", built)
        costs, limits = [], []
        s.on_cost = lambda p, d: costs.append((p, round(d, 4)))
        s.on_usage = limits.append
        s._handle_event({"type": "result", "subtype": "success", "total_cost_usd": 0.4})
        s._handle_event({"type": "result", "subtype": "success", "total_cost_usd": 1.0})     # the process total
        s._handle_event({"type": "result", "subtype": "success", "total_cost_usd": 1.0})     # nothing new
        s._cost_seen = 0.0                                                                   # restarted process
        s._handle_event({"type": "result", "subtype": "success", "total_cost_usd": 0.2})
        self.assertEqual(costs, [("cheshmeh", 0.4), ("cheshmeh", 0.6), ("cheshmeh", 0.2)])
        s._handle_event({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "utilization": 0.5}})
        self.assertEqual(limits, [{"status": "allowed", "utilization": 0.5}])
        self.assertNotIn("rate_limit_event", [e.get("type") for e in s.events])

    def test_app_wires_usage(self):
        self.app.usage.cost("cheshmeh", 1.0)
        self.assertEqual(self.app.usage.summary()["today"], 1.0)


class Routes(TmpHome):
    def test_new_routes(self):
        import http.client
        httpd = sw.make_server(self.app, 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)

        def req(method, path, body=None):
            c = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=5)
            h = {"Host": f"127.0.0.1:{self.app.port}", "X-Token": self.app.token, "Content-Type": "application/json"}
            c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
            r = c.getresponse()
            out = (r.status, json.loads(r.read()))
            c.close()
            return out
        self.assertEqual(req("GET", "/api/usage")[1]["today"], 0)
        self.assertEqual(req("GET", "/api/tunnels")[1], {"tunnels": []})
        self.assertEqual(req("GET", "/api/docker-summary?since=0")[1]["events"], [])
        with mock.patch.object(sw.Tunnel, "start", lambda self: None):
            st, d = req("POST", "/api/tunnels/cheshmeh", {"tunnels": [{"name": "db", "remotePort": 5432}]})
        self.assertEqual((st, d["tunnels"][0]["localPort"]), (200, 5432))
        self.assertEqual(req("POST", "/api/tunnels/nope", {"tunnels": []})[0], 404)
        self.assertEqual(req("POST", "/api/docker/cheshmeh", {"container": "x", "action": "rm"})[0], 400)
        with mock.patch.object(sw, "remote_python", return_value={"containers": [container()], "error": ""}):
            st, d = req("GET", "/api/docker/cheshmeh")
        self.assertEqual((st, d["containers"][0]["name"]), (200, "cheshmeh-api-1"))


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- web version
FAKE_SSH = r'''#!/usr/bin/env python3
import sys, os, subprocess, getpass
pw = getpass.getpass(sys.argv[-2] + "'s password: ")
if pw != "right":
    print("Permission denied, please try again."); sys.exit(255)
sys.exit(subprocess.call(["sh", "-c", sys.argv[-1]]))
'''


class WebVersion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "web"
        self.checked = []

        def verify(conf, pw):
            self.checked.append((conf.user, conf.host, conf.port, conf.key))
            if pw != "right":
                raise sw.ApiError(401, "wrong user or password")
        self.verify = verify
        self.gate = sw.WebGate(self.root, ["127.0.0.1:22"], verify=verify, start_loops=False)
        self.keygen = mock.patch.object(sw, "ensure_ssh_key", lambda p: None)
        self.keygen.start()

    def tearDown(self):
        self.keygen.stop()
        self.gate.shutdown()
        self.tmp.cleanup()

    def login(self, pw="right", ip="1.2.3.4", **kw):
        body = dict({"host": "127.0.0.1", "port": "22", "user": "esi", "password": pw}, **kw)
        return self.gate.login(ip, body)

    def test_sign_in_makes_a_workspace(self):
        tok = self.login()
        app = self.gate.app_for(tok)
        self.assertTrue(app.web)
        self.assertEqual((app.conf.user, app.conf.host, app.conf.port), ("esi", "127.0.0.1", "22"))
        self.assertTrue(app.conf.key.startswith(str(self.root)))
        self.assertIsNone(self.gate.app_for("forged"))
        self.assertIs(self.gate.app_for(self.login()), app)                       # same user@server, same workspace
        self.assertNotIn(tok, (self.root / "sessions.json").read_text())          # only hashes are stored
        # sessions survive a restart; signing out ends one
        gate2 = sw.WebGate(self.root, ["127.0.0.1:22"], verify=self.verify, start_loops=False)
        self.assertIsNotNone(gate2.app_for(tok))
        gate2.logout(tok)
        self.assertIsNone(gate2.app_for(tok))

    def test_expired_and_idle_sessions(self):
        tok = self.login()
        rec = next(iter(self.gate.sessions.values()))
        rec["last"] = time.time() - 8 * 86400
        self.assertIsNone(self.gate.app_for(tok))

    def test_only_allowed_servers_and_valid_input(self):
        with self.assertRaises(sw.ApiError) as cm:
            self.login(host="10.0.0.9")
        self.assertEqual(cm.exception.status, 403)
        for bad in ({"user": "x;rm"}, {"user": ""}, {"port": "x"}, {"host": "a b"}, {"password": ""}, {"password": "a\nb"}):
            with self.assertRaises(sw.ApiError, msg=bad):
                self.login(**bad)
        self.assertEqual(self.checked, [])                                        # nothing reached ssh
        open_gate = sw.WebGate(self.root, [], verify=self.verify, start_loops=False)
        self.assertTrue(open_gate.host_allowed("anything", "22"))

    def test_brute_force_is_slowed_down(self):
        for _ in range(5):
            with self.assertRaises(sw.ApiError):
                self.login("wrong")
        with self.assertRaises(sw.ApiError) as cm:
            self.login("right")                                                   # even the right one now
        self.assertEqual(cm.exception.status, 429)
        self.assertTrue(self.login("right", ip="5.6.7.8"))                        # another address still can

    def test_verify_password_with_a_fake_ssh(self):
        self.keygen.stop()
        home = Path(self.tmp.name) / "home"
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir()
        (bindir / "ssh").write_text(FAKE_SSH)
        (bindir / "ssh").chmod(0o755)
        key = Path(self.tmp.name) / "k" / "id_ed25519"
        if not sw.which("ssh-keygen"):
            self.skipTest("ssh-keygen missing")
        sw.ensure_ssh_key(key)
        conf = sw.ServerConf("esi", "127.0.0.1", "22", str(key))
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(home)}):
            with self.assertRaises(sw.ApiError) as cm:
                sw.web_verify_password(conf, "wrong")
            self.assertEqual(cm.exception.status, 401)
            sw.web_verify_password(conf, "right")
            sw.web_verify_password(conf, "right")
        keys = (home / ".ssh" / "authorized_keys").read_text().splitlines()
        self.assertEqual(keys, [Path(str(key) + ".pub").read_text().strip()])     # added once
        self.keygen.start()

    def test_web_workspace_has_no_local_side(self):
        app = self.gate.app_for(self.login())
        app.store.add_manual({"name": "p", "remote": "/srv/p", "local": "/"})
        self.assertEqual([(p["local"], p["localExists"]) for p in app.projects(max_age=0)], [("", False)])
        for body in ({"kind": "add-local", "local": "/"}, {"kind": "sync-info", "project": "p"}, {"kind": "run", "project": "p"}):
            with self.assertRaises(sw.ApiError):
                app.create_session(body)
        app.store.set_commands("p", [{"name": "x", "cmd": "ls", "where": "local"}])
        cid = app.store.commands("p")[0]["id"]
        with self.assertRaises(sw.ApiError) as cm:
            app.create_session({"kind": "run", "project": "p", "cmdId": cid})
        self.assertEqual(cm.exception.status, 403)
        with mock.patch.object(sw.Session, "start", lambda self: None):
            info = app.create_session({"kind": "shell", "project": "p", "where": "local"})
        self.assertEqual(info["spec"]["where"], "server")


class BrowseServerFolders(TmpHome):
    def test_browse_script(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.realpath(d)
            for sub in ("b", "a/.git", ".hidden", "Z"):
                os.makedirs(os.path.join(root, sub))
            Path(root, "file.txt").write_text("x")
            r = subprocess.run([sys.executable, "-c", sw.BROWSE_PY, root], capture_output=True, text=True, timeout=20)
            out = json.loads(r.stdout)
            self.assertEqual(out["path"], root)
            self.assertEqual(out["parent"], os.path.dirname(root))
            self.assertEqual([(x["name"], x["git"], x["hidden"]) for x in out["dirs"]],
                             [("a", "repo", False), ("b", "", False), ("Z", "", False), (".hidden", "", True)])
            r = subprocess.run([sys.executable, "-c", sw.BROWSE_PY, root + "/file.txt"], capture_output=True, text=True, timeout=20)
            self.assertIn("not a folder", json.loads(r.stdout)["error"])

    def test_marks_folders_that_are_projects(self):
        ans = {"path": "/home/esi", "parent": "/home", "home": "/home/esi", "git": False,
               "dirs": [{"name": "cheshmeh", "git": "", "hidden": False}, {"name": "x", "git": "", "hidden": False}]}
        with mock.patch.object(sw, "remote_python", return_value=ans) as rp:
            out = self.app.browse("~")
        self.assertEqual(rp.call_args[0][2], ["~"])
        self.assertEqual([d["project"] for d in out["dirs"]], ["cheshmeh", ""])       # ~/cheshmeh is a project
        with mock.patch.object(sw, "remote_python", return_value={"error": "cannot open /root: Permission denied"}):
            with self.assertRaises(sw.ApiError):
                self.app.browse("/root")


class WebHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keygen = mock.patch.object(sw, "ensure_ssh_key", lambda p: None)
        self.keygen.start()

        def verify(conf, pw):
            if pw != "right":
                raise sw.ApiError(401, "wrong user or password")
        self.gate = sw.WebGate(Path(self.tmp.name) / "web", ["127.0.0.1"], verify=verify, start_loops=False)

    def tearDown(self):
        self.keygen.stop()
        self.gate.shutdown()
        self.tmp.cleanup()

    def serve(self, cert="", key=""):
        httpd = sw.make_web_server(self.gate, "127.0.0.1", 0, cert, key)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    def req(self, port, method, path, body=None, cookie="", origin=True, xsw=True, tls=False):
        import http.client
        import ssl
        c = http.client.HTTPSConnection("127.0.0.1", port, timeout=5, context=ssl._create_unverified_context()) if tls \
            else http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        h = {"Host": f"tablet.example:{port}"}
        if origin:
            h["Origin"] = f"https://tablet.example:{port}"
        if xsw:
            h["X-SW"] = "1"
        if cookie:
            h["Cookie"] = f"other=1; sw_web={cookie}"
        if body is not None:
            h["Content-Type"] = "application/json"
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw
        return r.status, data, r

    def test_sign_in_flow_and_blocked_routes(self):
        port = self.serve()
        st, d, r = self.req(port, "GET", "/")
        self.assertEqual(st, 200)
        self.assertIn("wss://tablet.example", r.getheader("Content-Security-Policy"))
        self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))
        self.assertEqual(self.req(port, "GET", "/api/web-info")[1]["host"], "127.0.0.1")
        st, d, _ = self.req(port, "GET", "/api/state")
        self.assertEqual((st, d.get("login")), (401, True))
        creds = {"host": "127.0.0.1", "user": "esi", "password": "right"}
        self.assertEqual(self.req(port, "POST", "/api/login", creds, origin=False)[0], 403)   # another site
        self.assertEqual(self.req(port, "POST", "/api/login", creds, xsw=False)[0], 403)
        self.assertEqual(self.req(port, "POST", "/api/login", dict(creds, password="no"))[0], 401)
        st, d, r = self.req(port, "POST", "/api/login", creds)
        self.assertEqual(st, 200)
        ck = r.getheader("Set-Cookie")
        for part in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/"):
            self.assertIn(part, ck)
        tok = ck.split(";")[0].split("=", 1)[1]
        st, d, _ = self.req(port, "GET", "/api/state", cookie=tok)
        self.assertEqual((st, d["web"], d["server"]["user"]), (200, True, "esi"))
        self.assertEqual(self.req(port, "GET", "/api/state", cookie=tok, xsw=False)[0], 403)
        for method, path in (("POST", "/api/quit"), ("POST", "/api/uninstall"), ("POST", "/api/editor"), ("GET", "/api/connection"),
                             ("POST", "/api/ssh-key/create"), ("POST", "/api/update"), ("POST", "/api/tunnels/x"), ("POST", "/api/add-project")):
            self.assertEqual(self.req(port, method, path, {}, cookie=tok)[0], 403, path)
        self.assertEqual(self.req(port, "POST", "/api/manual", {"name": "p", "remote": "/srv/p", "local": "/etc"}, cookie=tok)[0], 200)
        st, d, _ = self.req(port, "POST", "/api/commands/p", {"commands": [{"name": "x", "cmd": "ls", "where": "local"}]}, cookie=tok)
        self.assertEqual(d["commands"][0]["where"], "server")
        app = next(iter(self.gate.apps.values()))
        self.assertEqual(app.store.manual()[0]["local"], "")
        # the websocket needs the sign-in too, and the same origin
        self.assertEqual(self.req(port, "GET", "/ws?id=x", origin=True)[0], 401)
        self.assertEqual(self.req(port, "GET", "/ws?id=x", cookie=tok, origin=False)[0], 403)
        st, _, r = self.req(port, "POST", "/api/logout", {}, cookie=tok)
        self.assertIn("Max-Age=0", r.getheader("Set-Cookie"))
        self.assertEqual(self.req(port, "GET", "/api/state", cookie=tok)[0], 401)

    def test_https(self):
        if not sw.which("openssl"):
            self.skipTest("openssl missing")
        cert, key = sw.self_signed_cert(Path(self.tmp.name), "127.0.0.1")
        port = self.serve(cert, key)
        st, d, _ = self.req(port, "GET", "/api/web-info", tls=True)
        self.assertEqual((st, d["web"]), (200, True))
