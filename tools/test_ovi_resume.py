"""Resume must avoid raster reads and UI work for completed exports."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.ovi_batch_export import checkpoint_matches, resume_exports, run, verify_tif


class ResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.plan = self.root / "plan.geojson"
        self.ledger_path = self.root / "ovi_export_ledger.json"
        self.features = [{"properties": {"name": f"part_{i}", "zoom": 18}}
                         for i in range(3)]
        self.plan.write_text(json.dumps({"features": self.features}), encoding="utf-8")
        self.ledger = {"plan_sha256": hashlib.sha256(self.plan.read_bytes()).hexdigest(),
                       "basemap_key": "test", "completed": {}}

    def completed(self, feature, legacy=False):
        name = feature["properties"]["name"]
        path = self.root / f"{name}.tif"
        path.write_bytes(b"x" * 5000)
        record = {"bytes": 5000, "coverage": 1.0}
        if not legacy:
            record["mtime_ns"] = path.stat().st_mtime_ns
        self.ledger["completed"][name] = record
        return path

    def scan(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return resume_exports(self.features, self.root, self.ledger_path, self.ledger)

    def test_1906_legacy_checkpoints_skip_without_reading_rasters(self):
        self.features = [{"properties": {"name": f"part_{i}", "zoom": 18}}
                         for i in range(1906)]
        for feature in self.features:
            self.completed(feature, legacy=True)
        with patch("tools.ovi_batch_export.verify_tif", side_effect=AssertionError("raster read")):
            self.assertEqual(self.scan(), [])
        saved = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["completed"]), 1906)
        self.assertTrue(all("mtime_ns" in r for r in saved["completed"].values()))

    def test_missing_checkpoint_is_removed_and_only_missing_part_is_pending(self):
        for feature in self.features:
            self.completed(feature)
        (self.root / "part_1.tif").unlink()
        with patch("tools.ovi_batch_export.verify_tif", side_effect=AssertionError("raster read")):
            self.assertEqual(self.scan(), [(2, self.features[1])])
        self.assertNotIn("part_1", self.ledger["completed"])

    def test_changed_timestamp_and_untracked_file_are_validated_once(self):
        for feature in self.features:
            self.completed(feature)
        path = self.root / "part_1.tif"
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        del self.ledger["completed"]["part_2"]
        def validated(path, feature):
            return {"bytes": path.stat().st_size, "coverage": 1.0,
                    "mtime_ns": path.stat().st_mtime_ns}
        with patch("tools.ovi_batch_export.verify_tif", side_effect=validated) as verify:
            self.assertEqual(self.scan(), [])
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(self.scan(), [])
            self.assertEqual(verify.call_count, 2)

    def test_invalid_file_is_checked_once_then_pending(self):
        for feature in self.features:
            self.completed(feature)
        (self.root / "part_1.tif").write_bytes(b"partial")
        with patch("tools.ovi_batch_export.verify_tif", side_effect=RuntimeError("incomplete")) as verify:
            self.assertEqual(self.scan(), [(2, self.features[1])])
            verify.assert_called_once()
        self.assertNotIn("part_1", self.ledger["completed"])

    def test_all_completed_never_launches_or_inspects_ovi(self):
        for feature in self.features:
            self.completed(feature)
        self.ledger_path.write_text(json.dumps(self.ledger), encoding="utf-8")
        args = SimpleNamespace(plan=self.plan, output=self.root, only=None, limit=None,
                               basemap=SimpleNamespace(key="test"),
                               ensure_running=lambda: self.fail("launched Ovi"))
        with (patch("tools.ovi_batch_export.main_window", side_effect=AssertionError("UI scan")),
              patch("tools.ovi_batch_export.verify_tif", side_effect=AssertionError("raster read")),
              contextlib.redirect_stdout(io.StringIO())):
            run(args)

    def test_no_coverage_record_is_not_a_completed_checkpoint(self):
        path = self.completed(self.features[0])
        self.assertFalse(checkpoint_matches(path, {"bytes": 5000}))

    def test_real_validation_records_timestamp_and_coverage(self):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin
        from shapely.geometry import box, mapping
        from shapely.ops import transform
        from pyproj import Transformer
        path = self.root / "real.tif"
        with rasterio.open(path, "w", driver="GTiff", width=64, height=64,
                           count=4, dtype="uint8", crs="EPSG:3857",
                           transform=from_origin(0, 100, 0.597, 0.597)) as raster:
            raster.write(np.full((4, 64, 64), 255, dtype="uint8"))
        geometry = transform(Transformer.from_crs(3857, 4326, always_xy=True).transform,
                             box(1, 70, 30, 99))
        result = verify_tif(path, {"geometry": mapping(geometry)})
        self.assertEqual(result["mtime_ns"], path.stat().st_mtime_ns)
        self.assertEqual(result["coverage"], 1.0)
        self.assertTrue(checkpoint_matches(path, result))

    def test_entrypoint_does_not_validate_completed_rasters_again(self):
        import index
        for feature in self.features:
            self.completed(feature)
        self.ledger_path.write_text(json.dumps(self.ledger), encoding="utf-8")
        source = self.root / "source.geojson"
        source.touch()
        config = {"source_features": source, "target": self.plan,
                  "prepared_target": self.plan, "export_dir": self.root,
                  "mosaic": False}
        with (patch("index.configure_basemap", return_value=SimpleNamespace(key="test")),
              patch("index._resolve_buffered_plan"), patch("index.report"),
              patch("src.target_input.prepare_target", return_value=self.plan),
              patch("index.ensure_ovi_running", side_effect=AssertionError("launch")),
              patch("tools.ovi_batch_export.verify_tif", side_effect=AssertionError("raster read")),
              contextlib.redirect_stdout(io.StringIO())):
            index.run(config)


if __name__ == "__main__":
    unittest.main()
