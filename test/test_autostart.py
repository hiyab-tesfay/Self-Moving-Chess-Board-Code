import tempfile
import unittest
import subprocess
from pathlib import Path

from scripts.install_touch_autostart import autostart_entry, update_autostart


class AutostartTest(unittest.TestCase):
    def test_preserves_other_entries_and_is_reversible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "labwc/autostart"
            path.parent.mkdir()
            original = "# existing setting\nsome-other-app &\n"
            path.write_text(original)
            entry = autostart_entry(Path("/home/user/chess board"),
                                    Path("/home/user/state/chess log"), "usb", 1)
            self.assertTrue(update_autostart(path, entry))
            installed = path.read_text()
            self.assertIn(original, installed)
            self.assertIn("--camera usb --device 1", installed)
            self.assertIn("'/home/user/chess board'", installed)
            subprocess.run(["sh", "-n", str(path)], check=True)
            self.assertFalse(update_autostart(path, entry))
            self.assertEqual(path.read_text(), installed)
            self.assertTrue(update_autostart(path, None))
            self.assertEqual(path.read_text(), original)

    def test_incomplete_block_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "autostart"
            contents = "# BEGIN chess-board-pi-only touch screen\n"
            path.write_text(contents)
            with self.assertRaises(ValueError):
                update_autostart(path, "new entry\n")
            self.assertEqual(path.read_text(), contents)


if __name__ == "__main__":
    unittest.main()
