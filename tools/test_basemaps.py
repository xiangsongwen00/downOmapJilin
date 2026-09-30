"""Map menu allowlist, yearly selection, and output separation."""

from pathlib import Path
import hashlib
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from index import configure_basemap
from src.basemaps import allowed_options, menu_title_matches, resolve_basemap
from tools.ovi_batch_export import run as export_run
from tools.ovi_basemap import _map_section


MENU = [
    {"name": "天地图", "enabled": True},
    {"name": "天地图影像", "enabled": True},
    {"name": "吉林一号卫星图(全国2025)", "enabled": True},
    {"name": "吉林一号卫星图(全国2026)", "enabled": True},
    {"name": "吉林一号卫星图(全球)", "enabled": True},
    {"name": "吉林一号季度卫星图(2026)", "enabled": True},
    {"name": "四维地球卫星影像图(2025)", "enabled": True},
    {"name": "四维地球日新图", "enabled": True},
    {"name": "世纪空间卫星影像图(2025)", "enabled": True},
    {"name": "腾讯电子地图", "enabled": True},
    {"name": "定制地图菜单", "enabled": True},
]


class BasemapTests(unittest.TestCase):
    def test_only_map_switch_section_is_recorded(self):
        class Item:
            def __init__(self, name):
                self.name = name

            def window_text(self):
                return self.name

        names = ["天地图", "天地图影像", "吉林一号季度卫星图(2026)",
                 "腾讯电子地图", "定制地图菜单", "系统(S)", "视图(V)"]
        self.assertEqual([item.window_text() for item in _map_section(
            [Item(name) for name in names])], names[:4])

    def test_only_seven_allowed_families(self):
        self.assertEqual({item.family for item in allowed_options(MENU)}, {
            "tianditu_vector", "tianditu_imagery", "jilin_national",
            "jilin_global", "four_dimensional_satellite",
            "century_satellite", "tencent_vector",
        })
        self.assertFalse(any("季度" in item.label or "日新图" in item.label
                             for item in allowed_options(MENU)))
        self.assertFalse(any("定制地图菜单" in item.label for item in allowed_options(MENU)))
        self.assertEqual(resolve_basemap("four_dimensional_satellite", MENU, 2026).year, 2025)
        self.assertNotEqual(
            resolve_basemap("four_dimensional_satellite", MENU, 2026).key,
            resolve_basemap("century_satellite", MENU, 2026).key)

    def test_previous_year_then_latest_older_year(self):
        self.assertEqual(resolve_basemap("jilin_national", MENU, 2026).year, 2025)
        self.assertEqual(resolve_basemap("jilin_national", MENU, 2025).year, 2025)
        older = [item for item in MENU if "全国2026" not in item["name"]]
        self.assertEqual(resolve_basemap("jilin_national", older, 2026).year, 2025)
        only_current = [item for item in MENU if "全国2026" in item["name"]]
        self.assertEqual(resolve_basemap("jilin_national", only_current, 2026).year, 2026)
        older_and_current = MENU + [{"name": "吉林一号卫星图(全国2024)", "enabled": True}]
        self.assertEqual(resolve_basemap("jilin_national", older_and_current, 2027).year, 2026)
        self.assertEqual(resolve_basemap("jilin_national", older_and_current, 2025).year, 2024)
        with self.assertRaisesRegex(RuntimeError, "未来年份"):
            resolve_basemap("jilin_national", only_current, 2025)

    def test_disabled_and_missing_map_report_configuration_hint(self):
        disabled = [{"name": "腾讯电子地图", "enabled": False}]
        with self.assertRaisesRegex(RuntimeError, "定制地图菜单"):
            resolve_basemap("tencent_vector", disabled)
        with self.assertRaises(ValueError):
            resolve_basemap("jilin_quarterly", MENU)

    def test_title_with_version_suffix(self):
        choice = resolve_basemap("century_satellite", MENU, 2026)
        self.assertTrue(menu_title_matches(
            "奥维互动地图(VIP) -- 世纪空间卫星影像图(2025版)[14] [user]", choice))

    def test_each_map_has_distinct_output_and_ledger_key(self):
        def configured(family):
            config = {"basemap_id": family, "basemap_menu": MENU,
                      "export_dir": Path("out/vip_parts"),
                      "mosaic_dir": Path("out/features")}
            choice = configure_basemap(config)
            return choice, config

        jilin, jilin_config = configured("jilin_national")
        century, century_config = configured("century_satellite")
        self.assertNotEqual(jilin.key, century.key)
        self.assertNotEqual(jilin_config["export_dir"], century_config["export_dir"])
        self.assertNotEqual(jilin_config["mosaic_dir"], century_config["mosaic_dir"])

    def test_target_is_imported_before_map_switch(self):
        choice = resolve_basemap("jilin_national", MENU, 2026)
        events = []
        with tempfile.TemporaryDirectory() as temporary:
            plan = Path(temporary) / "grid.geojson"
            plan.write_text(json.dumps({"features": []}), encoding="utf-8")
            args = SimpleNamespace(plan=plan, output=Path(temporary) / "parts",
                                   only=None, limit=None, basemap=choice)
            with (patch("tools.ovi_batch_export.main_window", return_value=object()),
                  patch("tools.ovi_batch_export.maximize"),
                  patch("tools.ovi_batch_export.recover_ui"),
                  patch("tools.ovi_batch_export.imported_folder", return_value=None),
                  patch("tools.ovi_batch_export.import_plan", side_effect=lambda *a: events.append("import")),
                  patch("tools.ovi_basemap.ensure_basemap", side_effect=lambda *a: events.append("switch"))):
                export_run(args)
        self.assertEqual(events, ["import", "switch"])

    def test_existing_ledger_cannot_be_reused_for_another_map(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = Path(temporary) / "grid.geojson"
            plan.write_text(json.dumps({"features": []}), encoding="utf-8")
            output = Path(temporary) / "parts"
            output.mkdir()
            (output / "ovi_export_ledger.json").write_text(json.dumps({
                "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
                "basemap_key": resolve_basemap("jilin_global", MENU).key,
                "completed": {},
            }), encoding="utf-8")
            args = SimpleNamespace(plan=plan, output=output, only=None, limit=None,
                                   basemap=resolve_basemap("tianditu_imagery", MENU))
            with self.assertRaisesRegex(RuntimeError, "basemap differs"):
                export_run(args)


if __name__ == "__main__":
    unittest.main()
