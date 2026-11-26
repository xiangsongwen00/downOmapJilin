"""A fresh installation must include usable initial configuration."""

import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from tools.package_release import package


class PackageTests(unittest.TestCase):
    def test_complete_package_includes_defaults_without_private_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "jinlinOmap.exe").write_bytes(b"exe")
            (source / "config.json").write_text('{"source_features":"private-path"}')
            (source / "out2").mkdir()
            (source / "out2/private.tif").write_bytes(b"download")
            archive = package(source, root / "完整安装.zip")
            with zipfile.ZipFile(archive) as z:
                config = json.loads(z.read("config.json"))
                self.assertEqual(config["source_features"], "")
                self.assertIn("使用说明.txt", z.namelist())
                self.assertNotIn("out2/private.tif", z.namelist())
                z.extractall(root / "unpacked")
            import tkinter as tk
            from src.config_gui import ConfigWindow
            window = tk.Tk()
            window.withdraw()
            try:
                gui = ConfigWindow(window, root / "unpacked/config.json")
                self.assertEqual(gui.status.get(), "就绪")
                self.assertEqual(gui.values["source_features"].get(), "")
            finally:
                window.destroy()


if __name__ == "__main__":
    unittest.main()
