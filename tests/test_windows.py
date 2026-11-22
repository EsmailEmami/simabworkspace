"""Native Windows support (Python for Windows + Git for Windows, no WSL), tested on Linux
by switching server.IS_WIN on and faking the Windows pieces."""
import json
import os
import re
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gui"))
import server as sw  # noqa: E402


def make_exe(path: Path, body="#!/bin/sh\nexit 0\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class WinOn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        p = mock.patch.object(sw, "IS_WIN", True)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)


class ImportsAndPaths(WinOn):
    def test_unix_only_modules_are_optional(self):
        src = (ROOT / "gui" / "server.py").read_text()
        for mod in ("fcntl", "termios"):
            self.assertIsNone(re.search(r"^import %s$" % mod, src, re.M), mod)
            self.assertRegex(src, r"if not IS_WIN:\n    import fcntl\n    import termios")
        src = src.replace(sw.CHAT_RELAY_PY, "")         # runs on the (Linux) server, not here
        bad = [l for l in src.splitlines() if re.search(r"signal\.SIG(KILL|HUP)\b", l) and "hasattr" not in l]
        self.assertEqual([l for l in bad if not l.strip().startswith("signal.signal(signal.SIGHUP")], [])

    def test_msys_paths_become_windows_paths(self):
        self.assertEqual(sw.msys_to_win("/c/Users/me/.ssh/id_ed25519"), "C:/Users/me/.ssh/id_ed25519")
        self.assertEqual(sw.msys_to_win("/d"), "D:/")
        self.assertEqual(sw.msys_to_win("C:/Users/me/k"), "C:/Users/me/k")
        self.assertEqual(sw.msys_to_win("/home/me/k"), "/home/me/k")
        with mock.patch.object(sw, "IS_WIN", False):
            self.assertEqual(sw.msys_to_win("/c/x"), "/c/x")

    def test_config_keys_from_git_bash_and_from_the_window(self):
        cfg = self.root / ".config"
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(cfg), "HOME": str(self.root)}):
            (cfg / "simabworkspace").mkdir(parents=True)
            (cfg / "simabworkspace" / "config").write_text("SSH_USER=me\nSSH_HOST=h\nSSH_PORT=22\nSSH_KEY=/c/Users/me/.ssh/id_x\n")
            self.assertEqual(sw.load_server_conf().key, "C:/Users/me/.ssh/id_x")
            sw.write_server_conf(sw.ServerConf("me", "h", "22", "C:\\Users\\me\\.ssh\\id_x"))
            text = (cfg / "simabworkspace" / "config").read_text()
            self.assertIn("SSH_KEY=C:/Users/me/.ssh/id_x", text)
            self.assertIn("IdentityFile C:/Users/me/.ssh/id_x", (self.root / ".ssh" / "config").read_text())


class GitBash(WinOn):
    def test_bash_is_git_bash_never_wsl(self):
        git_root = self.root / "Program Files" / "Git"
        make_exe(git_root / "cmd" / "git")
        bash = make_exe(git_root / "bin" / "bash.exe")
        wsl_bash = make_exe(self.root / "System32" / "bash")        # WSL's bash.exe on PATH
        env = {"PATH": os.pathsep.join([str(wsl_bash.parent), str(git_root / "cmd")])}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(sw.bash_exe(), str(bash))
        with mock.patch.dict(os.environ, {"PATH": str(wsl_bash.parent), "ProgramFiles": str(self.root / "Program Files")}, clear=True):
            self.assertEqual(sw.bash_exe(), str(bash))           # found in Program Files without git on PATH
        with mock.patch.dict(os.environ, {"PATH": str(wsl_bash.parent)}, clear=True):
            with self.assertRaises(sw.ApiError) as e:
                sw.bash_exe()
            self.assertIn("Git for Windows", e.exception.message if hasattr(e.exception, "message") else str(e.exception))

    def test_script_is_the_bash_file_not_the_cmd_launcher(self):
        b = self.root / ".local" / "bin"
        make_exe(b / "simabworkspace.cmd")
        with mock.patch.object(sw, "bin_dir", return_value=b), mock.patch.dict(os.environ, {"PATH": str(b)}):
            self.assertIsNone(sw.installed_script())
            make_exe(b / "simabworkspace")
            self.assertEqual(sw.installed_script(), str(b / "simabworkspace"))

    def test_git_tools_come_after_windows_own(self):
        git_root = self.root / "Git"
        make_exe(git_root / "cmd" / "git")
        (git_root / "usr" / "bin").mkdir(parents=True)
        make_exe(git_root / "bin" / "bash.exe")
        with mock.patch.dict(os.environ, {"PATH": str(git_root / "cmd"), "HOME": str(self.root)}, clear=True):
            parts = sw.tool_path().split(os.pathsep)
        self.assertEqual(parts[-1], str(git_root / "usr" / "bin"))
        self.assertLess(parts.index(str(git_root / "cmd")), parts.index(str(git_root / "usr" / "bin")))


class LocalShell(WinOn):
    def test_powershell_cmd_and_bash_argv(self):
        self.assertEqual(sw.local_shell_argv("powershell.exe"), ["powershell.exe", "-NoLogo"])
        self.assertEqual(sw.local_shell_argv("pwsh", "npm start"), ["pwsh", "-NoLogo", "-Command", "npm start"])
        self.assertEqual(sw.local_shell_argv('"C:\\Program Files\\PowerShell\\7\\pwsh.exe"', "x"),
                         ["C:\\Program Files\\PowerShell\\7\\pwsh.exe", "-NoLogo", "-Command", "x"])
        self.assertEqual(sw.local_shell_argv("cmd.exe", "dir"), ["cmd.exe", "/c", "dir"])
        self.assertEqual(sw.local_shell_argv("C:/Git/bin/bash.exe", "make"), ["C:/Git/bin/bash.exe", "-lic", "make"])
        with mock.patch.object(sw, "IS_WIN", False):
            self.assertEqual(sw.local_shell_argv("/bin/zsh"), ["/bin/zsh", "-l"])

    def test_default_is_powershell_not_SHELL(self):
        found = {"pwsh": None, "powershell": "C:/W/powershell.exe"}
        with mock.patch.object(sw, "which", side_effect=lambda n: found.get(n)), \
                mock.patch.dict(os.environ, {"SHELL": "/usr/bin/bash"}):
            self.assertEqual(sw.local_shell({}), "C:/W/powershell.exe")

    def test_no_ssh_multiplexing(self):
        a = sw.ssh_base(sw.ServerConf("u", "h", "22"), multiplex=True)
        self.assertFalse(any("ControlMaster" in x for x in a))
        with mock.patch.object(sw, "IS_WIN", False):
            self.assertTrue(any("ControlMaster" in x for x in sw.ssh_base(sw.ServerConf("u", "h", "22"), multiplex=True)))


class ConPtyHelpers(WinOn):
    def test_env_block(self):
        b = sw.win_env_block({"path": "x", "A": "1", "Zed": "z", "=C:": "skip-me-not"})
        self.assertTrue(b.endswith("\0\0"))
        keys = [kv.split("=", 1)[0] for kv in b.rstrip("\0").split("\0")]
        self.assertEqual(keys, sorted(keys, key=str.upper))
        self.assertIn("A=1\0", b)

    def test_command_line_quoting(self):
        with mock.patch.object(sw.shutil, "which", return_value="C:\\Windows\\System32\\OpenSSH\\ssh.exe"):
            line = sw.win_command_line(["ssh", "-t", "u@h", 'cd "$HOME"; exec bash -l'])
        self.assertEqual(line, 'C:\\Windows\\System32\\OpenSSH\\ssh.exe -t u@h "cd \\"$HOME\\"; exec bash -l"')

    def test_password_prompt_through_escape_codes(self):
        self.assertTrue(sw.asks_password(b"\x1b[?25l\x1b[2J\x1b[Hu@h's password: \x1b[?25h"))
        self.assertTrue(sw.asks_password(b"u@h's password:"))
        self.assertFalse(sw.asks_password(b"u@h's password:\r\nWelcome\r\n"))
        self.assertFalse(sw.asks_password(b"\x1b]0;title\x07Last login"))

    def test_signals_end_the_process(self):
        killed = []

        class P:
            pid = 7

            def kill(self):
                killed.append("kill")

            def poll(self):
                return None
        wp = sw.WinProc.__new__(sw.WinProc)
        wp.kill = lambda: killed.append("winproc")
        sw.Session._signal(wp, sw.SIGHUP)
        with mock.patch.object(sw.subprocess, "run") as run:
            sw.Session._signal(P(), sw.signal.SIGTERM)
        self.assertEqual(run.call_args[0][0], ["taskkill", "/T", "/F", "/PID", "7"])
        self.assertEqual(killed, ["winproc", "kill"])

    def test_background_processes_get_a_hidden_console(self):
        self.assertEqual(sw.detached_kwargs(), {"creationflags": sw.CREATE_NO_WINDOW | sw.CREATE_NEW_PROCESS_GROUP})
        with mock.patch.object(sw, "IS_WIN", False):
            self.assertEqual(sw.detached_kwargs(), {"start_new_session": True})


class Utf8Mode(WinOn):
    def test_serve_restarts_itself_in_utf8_mode(self):
        # cp1252 open() broke the app on Windows: "'charmap' codec can't decode byte 0x90"
        with mock.patch.object(sw.sys, "flags", mock.Mock(utf8_mode=0)), \
             mock.patch.object(sw.subprocess, "call", return_value=0) as call, \
             mock.patch.object(sw, "serve") as serve:
            self.assertEqual(sw.main(["server.py", "serve"]), 0)
        serve.assert_not_called()
        self.assertEqual(call.call_args[0][0][1:3], ["-X", "utf8"])
        self.assertEqual(call.call_args[0][0][-1], "serve")

    def test_utf8_mode_serve_runs_directly(self):
        with mock.patch.object(sw.sys, "flags", mock.Mock(utf8_mode=1)), \
             mock.patch.object(sw.subprocess, "call") as call, mock.patch.object(sw, "serve") as serve:
            sw.main(["server.py", "serve"])
        serve.assert_called_once()
        call.assert_not_called()


class StartMenuAndBrowser(WinOn):
    def test_ico_wraps_the_png(self):
        png = (ROOT / "gui" / "static" / "icon-192.png").read_bytes()
        ico = sw.png_to_ico(png)
        self.assertEqual(struct.unpack("<HHH", ico[:6]), (0, 1, 1))
        w, h, _, _, planes, bpp, size, off = struct.unpack("<BBBBHHII", ico[6:22])
        self.assertEqual((w, h, size, off), (192, 192, len(png), 22))
        self.assertEqual(ico[22:], png)

    def test_shortcut_script_quotes(self):
        ps = sw.windows_shortcut_script("C:\\Users\\o'neil\\S.lnk", "C:\\Py\\pythonw.exe", '"C:\\a b\\server.py" open', "C:\\Users\\o'neil", "C:\\i.ico")
        self.assertIn("CreateShortcut('C:\\Users\\o''neil\\S.lnk')", ps)
        self.assertIn("$s.Arguments='\"C:\\a b\\server.py\" open'", ps)

    def test_make_shortcut_runs_powershell(self):
        with mock.patch.dict(os.environ, {"APPDATA": str(self.root / "Roaming"), "XDG_CONFIG_HOME": str(self.root / "cfg")}), \
                mock.patch.object(sw.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"", b"")) as run:
            self.assertEqual(sw.make_windows_shortcut(), "Windows Start menu")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:4], ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertIn("Simab Workspace.lnk", argv[4])
        self.assertIn("server.py", argv[4])
        self.assertTrue((self.root / "cfg" / "simabworkspace" / "simabworkspace.ico").is_file())

    def test_browser_edge_app_window_then_default(self):
        edge = make_exe(self.root / "x86" / "Microsoft" / "Edge" / "Application" / "msedge.exe")
        with mock.patch.dict(os.environ, {"ProgramFiles(x86)": str(self.root / "x86")}, clear=True):
            c = sw.browser_candidates("http://127.0.0.1:1/#t=abc", "P")
        self.assertEqual(c[0][0], str(edge))
        self.assertIn("--app=http://127.0.0.1:1/#t=abc", c[0])
        self.assertEqual(c[-1], [sw.WEBBROWSER, "http://127.0.0.1:1/#t=abc"])
        self.assertFalse(any(x[0] in ("cmd.exe", "wsl.exe") for x in c))

    def test_autostart_in_startup_folder(self):
        with mock.patch.dict(os.environ, {"APPDATA": str(self.root)}), mock.patch.object(sw, "which", return_value="C:/b/mutagen.exe"):
            self.assertFalse(sw.autostart_enabled())
            sw.set_autostart(True)
            self.assertTrue(sw.autostart_enabled())
            self.assertIn('"C:/b/mutagen.exe" daemon start', sw.windows_startup_file().read_text())
            sw.set_autostart(False)
            self.assertFalse(sw.autostart_enabled())


class FoldersAndEditor(WinOn):
    def app_with_project(self, local):
        app = sw.App.__new__(sw.App)
        app.web = False
        app.store = mock.Mock(settings={"editorCmd": "code"})
        app.activity = mock.Mock()
        app._project = lambda name: {"name": name, "local": local, "localExists": True}
        app.projects = lambda max_age=0: [{"name": "shop", "local": local, "localExists": True, "remote": "/srv/shop"}]
        return app

    def test_vscode_is_started_by_its_full_path(self):
        # `code` on Windows is code.cmd: CreateProcess only finds it by the full path (WinError 2 otherwise)
        app = self.app_with_project(str(self.root))
        with mock.patch.object(sw, "which", return_value="C:\\VS Code\\bin\\code.cmd"), \
                mock.patch.object(sw.subprocess, "Popen") as popen:
            app.open_editor({"project": "shop", "where": "local"})
        self.assertEqual(popen.call_args[0][0], ["C:\\VS Code\\bin\\code.cmd", str(self.root)])

    def test_open_folder_in_explorer(self):
        app = self.app_with_project("C:\\Users\\me\\shop")
        with mock.patch.object(sw.os, "startfile", create=True) as sf:
            self.assertEqual(app.open_folder("shop")["path"], "C:\\Users\\me\\shop")
        sf.assert_called_once_with("C:\\Users\\me\\shop")
        self.assertIn(("POST", "open-folder"), sw.WEB_BLOCKED)
        self.assertIn(("POST", "pick-folder"), sw.WEB_BLOCKED)

    def test_folder_dialog(self):
        argv = sw.folder_dialog_argv("C:\\Users\\me")
        self.assertEqual(argv[:5], ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command"])
        self.assertIn("FolderBrowserDialog", argv[5])
        with mock.patch.object(sw.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "C:\\Users\\me\\work".encode(), b"")) as run:
            self.assertEqual(sw.pick_folder_dialog("C:\\Users\\me"), "C:\\Users\\me\\work")
        self.assertEqual(run.call_args[1]["env"]["SW_START"], "C:\\Users\\me")
        with mock.patch.object(sw.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"")):
            self.assertEqual(sw.pick_folder_dialog("C:\\"), "")             # cancelled
        with mock.patch.object(sw.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"D:\\", b"")):
            self.assertEqual(sw.pick_folder_dialog("C:\\"), "D:\\")       # a drive keeps its slash

    def test_pick_starts_at_the_nearest_existing_folder(self):
        app = self.app_with_project("")
        with mock.patch.object(sw, "pick_folder_dialog", side_effect=lambda st: st) as d:
            app.pick_folder(str(self.root / "not" / "yet"))
        d.assert_called_once_with(str(self.root))


class BashOnWindows(unittest.TestCase):
    """simabworkspace.sh in Git Bash: faked with a uname that says MINGW64 and a cygpath."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        fake = Path(self.tmp.name) / "fakebin"
        make_exe(fake / "uname", '#!/bin/sh\n[ "$1" = "-m" ] && echo x86_64 || echo MINGW64_NT-10.0-22631\n')
        make_exe(fake / "cygpath", "#!/bin/sh\ncase \"$2\" in /) printf '%s\\n' 'C:\\Program Files\\Git\\' ;; "
                                   "*) printf 'C:%s\\n' \"$2\" ;; esac\n")
        self.env = {"HOME": str(self.home), "PATH": f"{fake}:/usr/bin:/bin", "XDG_CONFIG_HOME": str(self.home / ".config"),
                    "SW_NONINTERACTIVE": "1"}

    def sh(self, *args):
        return subprocess.run(["bash", str(ROOT / "simabworkspace.sh")] + list(args), capture_output=True, text=True,
                              env=self.env, stdin=subprocess.DEVNULL, timeout=60)

    def funcs(self, *names):
        script = (ROOT / "simabworkspace.sh").read_text()
        return "\n".join(re.search(r"^%s\(\) \{.*?^\}" % n, script, re.S | re.M).group(0) for n in names)

    def test_self_install_writes_a_cmd_launcher(self):
        r = self.sh("self-install")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        launcher = self.home / ".local" / "bin" / "simabworkspace.cmd"
        self.assertEqual(launcher.read_bytes(), b'@"C:\\Program Files\\Git\\bin\\bash.exe" "%~dp0simabworkspace" %*\r\n')
        self.assertTrue((self.home / ".local" / "share" / "simabworkspace" / "gui" / "server.py").is_file())
        self.assertEqual(self.sh("version").stdout.strip(), "simabworkspace " + sw.VERSION)

    def test_window_calls_still_refuse_the_wizard(self):
        r = self.sh("add-server", "x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("open Settings in the window", r.stdout + r.stderr)

    def test_local_paths_and_mutagen_paths_match(self):
        prog = ("IS_WIN=1\nwinpath() { printf 'C:%s' \"$1\"; }\n"
                + self.funcs("normalize_local", "sessions_json") + "\n"
                "mutagen() { printf '%s' '[{\"name\":\"a\",\"alpha\":{\"path\":\"C:\\\\home\\\\x\"},\"beta\":{\"host\":\"h\",\"path\":\"/srv/a\"}}]'; }\n"
                'normalize_local /home/x/; echo; normalize_local "~/p"; echo; sessions_json')
        r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=dict(self.env, HOME="/home/me"))
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], "C:/home/x")
        self.assertEqual(lines[1], "C:/home/me/p")
        self.assertEqual(json.loads(lines[2])[0]["alpha"]["path"], "C:/home/x")
        self.assertEqual(json.loads(lines[2])[0]["beta"]["path"], "/srv/a")

    def test_jq_is_downloaded_not_winget(self):
        prog = ("IS_WIN=1\ninstall_jq() { echo JQ; }\npkg_install() { echo PKG \"$@\"; }\n"
                "title() { :; }; ok() { echo OK \"$*\"; }; warn() { echo WARN \"$*\"; }; die() { echo DIE \"$*\"; exit 1; }\n"
                "BIN_DIR=/nonexistent; IS_WSL=0; OS=MINGW64; ensure_path() { :; }\n"
                "mutagen() { echo v; }\n"
                + self.funcs("check_prereqs") + "\nPATH=/nonexistent-only; check_prereqs")
        r = subprocess.run(["/bin/bash", "-c", prog], capture_output=True, text=True)
        self.assertIn("JQ", r.stdout)
        self.assertIn("PKG curl tar gzip ssh", r.stdout)
        self.assertNotIn("PKG curl tar gzip ssh jq", r.stdout)


class Installer(unittest.TestCase):
    def test_install_ps1_is_native(self):
        ps = (ROOT / "install.ps1").read_text()
        self.assertNotRegex(ps, r"(?i)\bwsl(\.exe)?\s+(--install|-e|-d)")
        self.assertNotIn("wsl.exe", ps)
        self.assertIn("Python.Python.3", ps)
        self.assertIn("Git.Git", ps)
        self.assertIn("--no-open", ps)
        self.assertIn("shortcut", ps)
        self.assertIn("System32\\bash.exe (that is WSL)", ps)
        self.assertEqual(ps.count("{"), ps.count("}"))
        # finding Python: the registry first; the exit code is read right after the call, never
        # through a pipeline (that left winget's exit code and rejected the new Python)
        self.assertIn("Software\\Python\\PythonCore", ps)
        self.assertNotIn("Select-Object -First 1)", ps)
        self.assertRegex(ps, r"\$out = @\(& \$exe @pre -c .*\)\n\s+\$code = \$LASTEXITCODE")
        self.assertEqual(ps.count("("), ps.count(")"))


class WinSshConfig(unittest.TestCase):
    """ssh on Windows refuses ~/.ssh/config with a 'bad' owner/ACL: we pass a copy with -F."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for p in (mock.patch.object(sw, "IS_WIN", True),
                  mock.patch.object(sw, "home_path", lambda *a: self.root.joinpath("home", *a)),
                  mock.patch.object(sw, "config_dir", lambda: self.root / "cfg")):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_copy_of_user_config_given_with_F(self):
        src = self.root / "home" / ".ssh" / "config"
        src.parent.mkdir(parents=True)
        src.write_text("Host x\n  User y\n")
        a = sw.win_ssh_config()
        self.assertEqual(a, ["-F", str(self.root / "cfg" / "ssh_config")])
        self.assertEqual(Path(a[1]).read_text(), "Host x\n  User y\n")
        src.write_text("Host z\n")                          # follows the user's changes
        self.assertEqual(Path(sw.win_ssh_config()[1]).read_text(), "Host z\n")

    def test_no_user_config_gives_empty_file(self):
        a = sw.win_ssh_config()
        self.assertEqual(Path(a[1]).read_bytes(), b"")

    def test_in_every_ssh_command(self):
        conf = sw.ServerConf("u", "h")
        argv = sw.ssh_base(conf)
        self.assertEqual(argv[1], "-F")
        self.assertEqual(sw.pc_link_argv(conf, "/tmp/s.sock", 15550)[1], "-F")   # Computers link
        with mock.patch.object(sw, "IS_WIN", False):
            self.assertNotIn("-F", sw.ssh_base(conf))
        self.assertEqual(sw.Path(sw.__file__).read_text(encoding="utf-8").count('["ssh", *win_ssh_config()'), 3)


if __name__ == "__main__":
    unittest.main()
