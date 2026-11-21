"""Files of a Local only project whose computer is off: one short line, never the whole ssh/python command."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gui"))
import server as sw  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class Conf:
    configured, target = True, "u@127.0.0.1"


def timeout(*a, **k):
    raise subprocess.TimeoutExpired(a[0], 40)


class OfflineFiles(unittest.TestCase):
    def test_timeout_message_is_short(self):
        with mock.patch.object(sw, "ssh_base", return_value=["ssh"]), mock.patch.object(sw.subprocess, "run", timeout):
            with self.assertRaises(sw.ApiError) as c:
                sw.remote_python(Conf(), sw.FS_PY, ["list", "/x", ""], timeout=40)
        self.assertEqual(c.exception.message, "cannot reach the server: no answer in 40s")
        self.assertNotIn("FS_SKIP", c.exception.message)

    def test_local_only_says_computer_off(self):
        err = sw.ApiError(504, "cannot reach the server: no answer in 40s")
        with mock.patch.object(sw, "remote_python", side_effect=err):
            with self.assertRaises(sw.ApiError) as c:
                sw.fs_remote({"localOnly": "pc1"}, Conf(), ["list", "/x", ""])
            self.assertIn("not connected", c.exception.message)
            with self.assertRaises(sw.ApiError) as c:
                sw.fs_remote({}, Conf(), ["list", "/x", ""])
            self.assertIn("no answer", c.exception.message)

    def test_sidebar_hatches_offline_local_project(self):
        js = (ROOT / "gui/static/app.js").read_text()
        self.assertIn("' pc-off'", js)
        self.assertIn("fresh: function () { return infoFresh; }", (ROOT / "gui/static/computers.js").read_text())
        self.assertIn(".proj.pc-off {", (ROOT / "gui/static/app.css").read_text())


if __name__ == "__main__":
    unittest.main()
