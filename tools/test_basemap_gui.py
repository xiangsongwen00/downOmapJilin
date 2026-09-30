"""The first save discovers maps; later saves reuse the stored menu."""

from datetime import date
import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from src.config_gui import ConfigWindow
from tools.test_basemaps import MENU


class BasemapGuiTests(unittest.TestCase):
    def test_first_save_discovers_then_later_save_reuses_menu(self):
        project = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            raw = json.loads((project / "config.json").read_text(encoding="utf-8"))
            raw.update(target=str(Path(temporary) / "grid.geojson"),
                       export_dir=str(Path(temporary) / "parts"),
                       mosaic_dir=str(Path(temporary) / "features"),
                       prepared_target=str(Path(temporary) / "prepared.geojson"),
                       state_file=str(Path(temporary) / "state.json"),
                       lock_file=str(Path(temporary) / "pipeline.lock"))
            raw.pop("basemap_menu", None)
            raw.pop("basemap_id", None)
            config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            root = tk.Tk()
            root.withdraw()
            try:
                window = ConfigWindow(root, config_path)
                with patch.object(window, "read_basemap_menu", return_value=MENU) as read:
                    self.assertTrue(window.save())
                    self.assertEqual(read.call_count, 1)
                    first = json.loads(config_path.read_text(encoding="utf-8"))
                    self.assertEqual(first["basemap_checked_year"], date.today().year)
                    self.assertNotIn("basemap_id", first)
                    self.assertEqual(len(window.map_tree.get_children()), len(MENU))
                    states = [window.map_tree.item(row, "values")[1]
                              for row in window.map_tree.get_children()]
                    self.assertIn("可下载", states)
                    self.assertIn("禁止下载", states)
                    self.assertIn("菜单功能", states)
                    self.assertIn("菜单项：11", window.map_summary.get())
                    self.assertTrue(any(family == "four_dimensional_satellite"
                                        for family in window.basemap_options.values()))
                    display = next(label for label, family in window.basemap_options.items()
                                   if family == "jilin_national")
                    window.basemap_value.set(display)
                    self.assertTrue(window.save())
                    self.assertEqual(read.call_count, 1)
                    second = json.loads(config_path.read_text(encoding="utf-8"))
                    self.assertEqual(second["basemap_id"], "jilin_national")
            finally:
                root.destroy()


if __name__ == "__main__":
    unittest.main()
