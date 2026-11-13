import os
import shlex
import shutil
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest import mock

from scripts import install_touch_autostart
from scripts.install_touch_autostart import autostart_entry, update_autostart

SHELL = shutil.which("sh")
if SHELL is None and os.name == "nt":
    git_shell = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/usr/bin/sh.exe"
    if git_shell.is_file():
        SHELL = str(git_shell)


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
            if SHELL:
                subprocess.run([SHELL, "-n", str(path)], check=True)
            self.assertFalse(update_autostart(path, entry))
            self.assertEqual(path.read_text(), installed)
            self.assertTrue(update_autostart(path, None))
            self.assertEqual(path.read_text(), original)

    def test_no_gantry_port_means_no_gantry_environment(self):
        entry = autostart_entry(Path("/home/user/chess"), Path("/home/user/log"), "pi", 0)
        self.assertNotIn("CHESS_GANTRY_PORT", entry)
        self.assertNotIn("env ", entry)

    def test_gantry_port_is_set_in_the_command_environment(self):
        for port in ("auto", "/dev/ttyUSB0"):
            with self.subTest(port=port), tempfile.TemporaryDirectory() as directory:
                entry = autostart_entry(Path("/home/user/chess board"),
                                        Path("/home/user/state/chess log"), "pi", 0, port)
                lines = entry.splitlines()
                self.assertEqual(lines[0], "# BEGIN chess-board-pi-only touch screen")
                self.assertEqual(lines[-1], "# END chess-board-pi-only touch screen")
                self.assertIn(f"&& env CHESS_GANTRY_PORT={port} '/home/user/chess board/.venv/bin/python'", entry)
                self.assertIn("--camera pi --device 0", entry)
                path = Path(directory) / "autostart"
                path.write_text("other-app &\n")
                self.assertTrue(update_autostart(path, entry))
                if SHELL:
                    subprocess.run([SHELL, "-n", str(path)], check=True)
                plain = autostart_entry(Path("/home/user/chess board"),
                                        Path("/home/user/state/chess log"), "pi", 0)
                self.assertTrue(update_autostart(path, plain), "reinstalling without a port replaces the block")
                self.assertNotIn("CHESS_GANTRY_PORT", path.read_text())
                self.assertEqual(path.read_text().count("# BEGIN"), 1)

    @unittest.skipUnless(os.name == "posix", "This check executes the Pi's POSIX env/sh commands")
    def test_gantry_port_reaches_the_program(self):
        entry = autostart_entry(Path("/p"), Path("/l"), "pi", 0, "auto")
        command = entry.splitlines()[1].split(" && ", 1)[1].split(" >> ")[0]
        argv = shlex.split(command)
        self.assertEqual(argv[:2], ["env", "CHESS_GANTRY_PORT=auto"])
        result = subprocess.run(["env", "CHESS_GANTRY_PORT=auto", "sh", "-c", 'printf %s "$CHESS_GANTRY_PORT"'],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, "auto")

    def test_main_passes_the_gantry_port(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"XDG_CONFIG_HOME": str(Path(directory) / "config"),
                   "XDG_STATE_HOME": str(Path(directory) / "state")}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(Path, "is_file", return_value=True), \
                    mock.patch("builtins.print"):
                self.assertEqual(install_touch_autostart.main(["--gantry-port", "auto"]), 0)
            installed = (Path(directory) / "config/labwc/autostart").read_text()
            self.assertIn("env CHESS_GANTRY_PORT=auto ", installed)

    def test_incomplete_block_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "autostart"
            contents = "# BEGIN chess-board-pi-only touch screen\n"
            path.write_text(contents)
            with self.assertRaises(ValueError):
                update_autostart(path, "new entry\n")
            self.assertEqual(path.read_text(), contents)

    def test_standalone_autostart_runs_the_integrated_launcher(self):
        entry = autostart_entry(Path("/home/smart/chess"), Path("/home/smart/game.log"),
                                "pi", 0, "auto", standalone=True)
        self.assertIn("pi_standalone.py", entry)
        self.assertIn("--gantry-port auto", entry)
        self.assertNotIn("pi_touch.py", entry)


if __name__ == "__main__":
    unittest.main()
