import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from checkbuildpy import audit, parse_build


class BuildAuditTests(unittest.TestCase):
    def test_bat_wrapper_and_dynamic_gis_warning(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "main.py").write_text("import geopandas\nimport rasterio\n", encoding="utf-8")
            (root / "build.bat").write_text(
                "python -m nuitka ^\n --standalone ^\n --include-package=rasterio ^\n main.py\n", encoding="utf-8")
            (root / "wrapper.py").write_text("from pathlib import Path\nscript = Path(__file__).parent / 'build.bat'\n", encoding="utf-8")
            parsed = parse_build(root / "wrapper.py")
            self.assertEqual(parsed["entry"], "main.py")
            self.assertEqual(parsed["options"]["--include-package"], ["rasterio"])
            result = audit(root / "wrapper.py")
            suggestions = {item["suggestion"] for item in result["findings"]}
            self.assertIn("--include-package=pyogrio", suggestions)
            self.assertNotIn("--include-package=rasterio", suggestions)

    def test_python_list_fstrings_and_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "app.py").write_text("from pkg import worker\n", encoding="utf-8")
            (root / "pkg").mkdir()
            (root / "pkg/worker.py").write_text("import rasterio\n", encoding="utf-8")
            (root / "pkg/extra.py").write_text("from osgeo import gdal\n", encoding="utf-8")
            (root / "build.py").write_text(
                "from pathlib import Path\nAPP_NAME='Demo'\n"
                "args = ['-m','nuitka','--standalone', f'--output-filename={APP_NAME}.exe',"
                "'--include-package=pkg',str(Path('app.py'))]\n", encoding="utf-8")
            report = root / "report.xml"
            report.write_text('<nuitka-compilation-report><module name="app"/>'
                              '<module name="pkg.worker"/><module name="rasterio"/>'
                              '</nuitka-compilation-report>', encoding="utf-8")
            result = audit(root / "build.py", report_path=report)
            self.assertIn("pkg/worker.py", result["imports"]["files"])
            self.assertIn("pkg/extra.py", result["imports"]["files"])
            self.assertIn("osgeo", result["imports"]["external"])
            self.assertNotIn("osgeo", result["project_inventory"]["unreachable"])
            self.assertEqual(result["build"]["options"]["--output-filename"], ["Demo.exe"])
            self.assertTrue(any(item["subject"] == "rasterio.serde" and item["level"] == "error"
                                for item in result["findings"]))
            self.assertEqual(result["report_module_count"], 3)

    def test_build_script_is_not_executed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            marker = root / "executed"
            (root / "main.py").write_text("import json\n", encoding="utf-8")
            (root / "build.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n"
                "args=['-m','nuitka','--standalone','main.py']\n", encoding="utf-8")
            audit(root / "build.py")
            self.assertFalse(marker.exists())

    def test_missing_broad_only_dependency_is_review_not_build_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "main.py").write_text("import json\n", encoding="utf-8")
            (root / "pkg").mkdir()
            (root / "pkg/unused.py").write_text("import definitely_absent_for_audit_test\n", encoding="utf-8")
            (root / "build.bat").write_text(
                "python -m nuitka --standalone --include-package=pkg main.py\n", encoding="utf-8")
            result = audit(root / "build.bat", interpreter=Path(sys.executable))
            matches = [item for item in result["findings"]
                       if item["subject"] == "definitely_absent_for_audit_test" and
                       "absent from selected build interpreter" in item["message"]]
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["level"], "review")


if __name__ == "__main__":
    unittest.main()
