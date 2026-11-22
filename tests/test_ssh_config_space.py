"""A key path with a space (macOS user "simab co"): ~/.ssh/config must quote it, or ssh refuses the file."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gui"))
import server as sw  # noqa: E402


class SpaceInKeyPath(unittest.TestCase):
    KEY = "/Users/simab co/.ssh/simab_key"

    def test_value_quoted_only_with_a_space(self):
        self.assertEqual(sw.ssh_cfg_value(self.KEY), '"/Users/simab co/.ssh/simab_key"')
        self.assertEqual(sw.ssh_cfg_value("/k"), "/k")
        self.assertEqual(sw.ssh_cfg_value('"/a b"'), '"/a b"')

    def test_heal_quotes_only_inside_our_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "config"
            f.write_text("Host mine\n    IdentityFile /Users/simab co/own key\n\n"
                         f"{sw.RC_MARK_BEGIN}\nHost h\n    IdentityFile {self.KEY}\n{sw.RC_MARK_END}\n"
                         f"{sw.GUI_MARK_BEGIN}\nHost x\n    IdentityFile \"{self.KEY}\"\n{sw.GUI_MARK_END}\n")
            self.assertTrue(sw.heal_ssh_config(f))
            t = f.read_text()
            self.assertIn(f'    IdentityFile "{self.KEY}"\n{sw.RC_MARK_END}', t)
            self.assertIn("    IdentityFile /Users/simab co/own key\n", t)          # the user's own line: untouched
            self.assertEqual(t.count(f'"{self.KEY}"'), 2)
            self.assertFalse(sw.heal_ssh_config(f))                                # nothing left to do

    @unittest.skipUnless(sw.shutil.which("ssh"), "no ssh")
    def test_ssh_accepts_what_we_write(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "config"
            f.write_text(f"{sw.RC_MARK_BEGIN}\nHost h\n    IdentityFile {sw.ssh_cfg_value(self.KEY)}\n{sw.RC_MARK_END}\n")
            r = subprocess.run(["ssh", "-F", str(f), "-G", "h"], capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("identityfile /users/simab co/.ssh/simab_key", r.stdout.lower())

    def test_server_conf_writes_it_quoted(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}), \
                mock.patch.object(sw, "home_path", side_effect=lambda *a: Path(d, *a)):
            sw.write_server_conf(sw.ServerConf("u", "h", "22", self.KEY))
            self.assertIn(f'IdentityFile "{self.KEY}"', Path(d, ".ssh", "config").read_text())

    def test_bash_script_quotes_it(self):
        self.assertIn('case "$_k" in *" "*) _k="\\"$_k\\"" ;; esac', (ROOT / "simabworkspace.sh").read_text())


if __name__ == "__main__":
    unittest.main()
