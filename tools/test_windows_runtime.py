"""Portable startup must not inherit another computer's GIS/COM caches."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from src import windows_runtime as runtime


class WindowsRuntimeTests(unittest.TestCase):
    def test_packaged_environment_uses_only_bundled_gis_data(self):
        with tempfile.TemporaryDirectory(prefix="中文 兼容 ") as temporary:
            root = Path(temporary)
            for name in ("pyogrio/gdal_data", "pyproj/proj_dir/share/proj", "pyogrio.libs"):
                (root / name).mkdir(parents=True)
            external = {name: r"C:\Program Files\GDAL\old" for name in (
                "GDAL_DRIVER_PATH", "OGR_DRIVER_PATH", "GDAL_DATA", "GDAL_PYTHON_DRIVER_PATH",
                "PROJ_LIB", "PROJ_DATA", "PROJ_AUX_DB", "GEOTIFF_CSV")}
            with patch.dict(os.environ, external), patch("os.add_dll_directory") as add_dll:
                runtime.configure_environment(root, True)
                self.assertEqual(os.environ["GDAL_DRIVER_PATH"], "disable")
                self.assertEqual(os.environ["OGR_DRIVER_PATH"], "disable")
                self.assertEqual(os.environ["GDAL_DATA"], str(root / "pyogrio/gdal_data"))
                self.assertEqual(os.environ["PROJ_DATA"], str(root / "pyproj/proj_dir/share/proj"))
                self.assertNotIn("PROJ_AUX_DB", os.environ)
                self.assertNotIn("GDAL_PYTHON_DRIVER_PATH", os.environ)
                add_dll.assert_called_once_with(str(root / "pyogrio.libs"))

    def test_source_execution_preserves_existing_environment(self):
        with patch.dict(os.environ, {"GDAL_DATA": "existing-data"}):
            runtime.configure_environment(Path("unused"), False)
            self.assertEqual(os.environ["GDAL_DATA"], "existing-data")

    def test_memory_com_generation_without_access_to_bundled_wrappers(self):
        code = r'''
import importlib.abc, json, sys
from src.windows_runtime import prepare_comtypes
class NoCachedWrappers(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('comtypes.gen.'):
            raise ImportError('Simulated incompatible build-machine wrapper')
sys.meta_path.insert(0, NoCachedWrappers())
prepare_comtypes(True)
prepare_comtypes(True)
import comtypes.client
module = comtypes.client.GetModule('UIAutomationCore.dll')
import pywinauto
print(json.dumps({'memory': comtypes.client.gen_dir is None,
                  'interface': hasattr(module, 'IUIAutomation'),
                  'wrappers': sorted(n for n in sys.modules if n.startswith('comtypes.gen.'))}))
'''
        process = subprocess.run([sys.executable, "-B", "-c", code],
                                 cwd=Path(__file__).resolve().parent.parent,
                                 text=True, encoding="utf-8", capture_output=True, timeout=60)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertTrue(result["memory"])
        self.assertTrue(result["interface"])
        self.assertIn("comtypes.gen.UIAutomationClient", result["wrappers"])
        self.assertTrue(any("00020430" in name for name in result["wrappers"]))

    def test_console_handles_absent_and_redirected_streams(self):
        stderr = Mock()
        with patch("sys.stdout", None), patch("sys.stderr", stderr):
            runtime.configure_console()
        stderr.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")

    def test_dpi_already_set_is_not_an_error(self):
        user32 = Mock()
        user32.SetProcessDpiAwarenessContext.return_value = 0
        with patch("ctypes.WinDLL", return_value=user32), patch("ctypes.get_last_error", return_value=5):
            self.assertEqual(runtime.configure_dpi(), "already-configured")

    def test_diagnostic_needs_no_config_or_ovi_path(self):
        process = subprocess.run([sys.executable, "-B", "index.py", "--diagnose"],
                                 cwd=Path(__file__).resolve().parent.parent,
                                 text=True, encoding="utf-8", capture_output=True, timeout=60)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        result = json.loads(process.stdout)
        self.assertTrue(result["checks"]["gis"]["ok"])
        self.assertTrue(result["checks"]["automation"]["ok"])


if __name__ == "__main__":
    unittest.main()
