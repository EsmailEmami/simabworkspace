import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "gui/static"


class SideFilesButton(unittest.TestCase):
    """The sidebar no longer shows a file tree; a Files button under Usage opens the Files tab instead."""

    def setUp(self):
        self.html = (STATIC / "index.html").read_text()
        self.app = (STATIC / "app.js").read_text()
        self.files = (STATIC / "files.js").read_text()

    def test_tree_panel_is_gone(self):
        self.assertNotIn('id="side-files"', self.html)
        self.assertNotIn("SideFilesPanel", self.files)
        self.assertNotIn("sideFilesPanel", self.app)

    def test_button_sits_under_usage(self):
        usage = self.html.index('id="usage"')
        btn = self.html.index('id="side-files-btn"')
        self.assertLess(usage, btn)
        self.assertLess(btn, self.html.index('id="update-note"'))

    def test_button_opens_files_tab_of_active_project(self):
        self.assertIn("$('#side-files-btn')) $('#side-files-btn').addEventListener('click', function () { var p = filesBtnProject(); if (p) openFiles(p); });", self.app)
        fn = self.app[self.app.index("function filesBtnProject()"):]
        fn = fn[:fn.index("function renderFilesBtn")]
        self.assertIn("S.active.info.spec.project", fn)
        self.assertIn("hasRemote(p) || p.localExists", fn)

    def test_button_follows_active_tab(self):
        title = self.app[self.app.index("function renderTitle() {"):][:80]
        self.assertIn("renderFilesBtn();", title)


if __name__ == "__main__":
    unittest.main()
