"""Regression checks for vector input formats in buffered export plans."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import geopandas as gpd
from shapely.geometry import box, shape

from src.export_plan import make_plan
from index import _resolve_buffered_plan, resolve_config


class ExportPlanTests(unittest.TestCase):
    def test_managed_grid_rebuilds_and_preserves_checkpoint_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.geojson"
            gpd.GeoDataFrame(geometry=[box(120, 30, 120.005, 30.005)],
                             crs=4326).to_file(source, driver="GeoJSON")
            executable = root / "omap.exe"
            executable.touch()
            raw = dict(omapexepath=str(executable), source_features=str(source),
                       export_dir="parts", mosaic_dir="mosaics")
            config = resolve_config(raw, root)
            self.assertEqual(config["target"], root / "out/auto_download_grid.geojson")
            legacy = resolve_config(dict(raw, target="old.shp", prepared_target="old.json"), root)
            self.assertEqual(legacy["target"], config["target"])
            with patch("src.export_plan.make_plan", wraps=make_plan) as generate:
                _resolve_buffered_plan(config)
                first = config["target"].read_bytes()
                _resolve_buffered_plan(config)
                self.assertEqual(generate.call_count, 2)
                self.assertEqual(config["target"].read_bytes(), first)
                self.assertEqual(config["export_dir"], root / "parts")
            changed = resolve_config(dict(raw, plan_buffer_meters=30), root)
            _resolve_buffered_plan(changed)
            self.assertNotEqual(changed["target"], config["target"])
            self.assertEqual(config["target"].read_bytes(), first)
            again = resolve_config(dict(raw, plan_buffer_meters=30), root)
            _resolve_buffered_plan(again)
            self.assertEqual(again["target"], changed["target"])
            self.assertEqual(again["export_dir"], changed["export_dir"])

    def test_projected_shapefile_matches_wgs84_geojson(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame = gpd.GeoDataFrame(
                {"village": ["alpha", "beta"]},
                geometry=[box(120, 30, 120.005, 30.005),
                          box(120.01, 30, 120.015, 30.005)], crs=4326)
            geojson = root / "source.geojson"
            shapefile = root / "source.shp"
            frame.to_file(geojson, driver="GeoJSON")
            frame.to_crs(32651).to_file(shapefile)
            expected = root / "expected.geojson"
            actual = root / "actual.geojson"
            self.assertEqual(make_plan(geojson, expected, 20),
                             make_plan(shapefile, actual, 20))
            original = json.loads(expected.read_text(encoding="utf-8"))["features"]
            converted = json.loads(actual.read_text(encoding="utf-8"))["features"]
            self.assertEqual(len(original), 2)
            for first, second in zip(original, converted):
                for key in ("name", "feature_index", "part", "buffer_meters"):
                    self.assertEqual(first["properties"][key], second["properties"][key])
                self.assertLess(shape(first["geometry"]).hausdorff_distance(
                    shape(second["geometry"])), 1e-9)
            self.assertTrue(actual.with_suffix(".shp").is_file())

    def test_shapefile_without_crs_reports_source_problem(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.shp"
            gpd.GeoDataFrame(geometry=[box(120, 30, 120.005, 30.005)],
                             crs=4326).to_file(source)
            source.with_suffix(".prj").unlink()
            with self.assertRaisesRegex(ValueError, "Missing CRS"):
                make_plan(source, root / "plan.geojson", 20)


if __name__ == "__main__":
    unittest.main()
