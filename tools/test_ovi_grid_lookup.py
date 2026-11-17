"""Regression checks for Ovi favorites with scrolled or duplicate rows."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.ovi_batch_export import (
    _NativeTreeItem, _native_folders, grid_tree_item, imported_folder,
    open_property,
)


class Item:
    def __init__(self, name, parent=None):
        self.name = name
        self.owner = parent

    def window_text(self):
        return self.name

    def parent(self):
        return self.owner


class Window:
    def __init__(self, items):
        self.items = items

    def descendants(self, control_type):
        assert control_type == "TreeItem"
        return self.items


class NativeItem:
    def __init__(self, name, children=()):
        self.name = name
        self.nodes = list(children)

    def text(self):
        return self.name

    def children(self):
        return self.nodes

    def sub_elements(self):
        return [item for node in self.nodes for item in [node, *node.sub_elements()]]


class GridLookupTests(unittest.TestCase):
    def setUp(self):
        # These tests never inspect or operate a real desktop window.
        desktop = patch("tools.ovi_batch_export.Desktop")
        self.desktop = desktop.start()
        self.addCleanup(desktop.stop)
        self.desktop.return_value.window.return_value.descendants.return_value = []

    def test_finds_target_when_folder_is_not_enumerated(self):
        folder = Item("current[43]")
        target = Item("part_01", folder)
        self.assertIs(grid_tree_item(Window([target]), "current[43]", "part_01"), target)

    def test_ignores_same_named_row_in_old_favorites(self):
        old = Item("old[40]")
        current = Item("current[43]")
        old_target = Item("part_01", old)
        current_target = Item("part_01", current)
        window = Window([old_target, current_target])
        self.assertIs(grid_tree_item(window, "current[43]", "part_01"), current_target)

    def native_tree(self, *roots, visible=True):
        tree = Mock()
        tree.roots.return_value = roots
        tree.is_visible.return_value = visible
        self.desktop.return_value.window.return_value.descendants.return_value = [tree]
        return tree

    def test_native_lookup_reads_offscreen_folders(self):
        target = NativeItem("part_01")
        folder = NativeItem("current[5]", [target])
        self.native_tree(NativeItem("favorites", [folder]))
        window = SimpleNamespace(handle=123)
        self.assertEqual(_native_folders(window, "current[5]"), [folder])

    def test_native_lookup_ignores_hidden_import_preview(self):
        self.native_tree(NativeItem("current[5]"), visible=False)
        self.assertEqual(_native_folders(SimpleNamespace(handle=123), "current[5]"), [])

    def test_native_fallback_when_uia_has_names_but_no_folder_ancestry(self):
        target = NativeItem("part_01")
        old = NativeItem("old[5]", [NativeItem("part_01")])
        current = NativeItem("current[5]", [target])
        self.native_tree(NativeItem("favorites", [old, current]))
        window = Window([Item("part_01") for _ in range(3)])
        window.handle = 123
        found = grid_tree_item(window, "current[5]", "part_01")
        self.assertIsInstance(found, _NativeTreeItem)
        self.assertIs(found.item, target)

    def test_native_fallback_rejects_ambiguous_rows(self):
        self.native_tree(NativeItem("current[5]", [
            NativeItem("part_01"), NativeItem("part_01")]))
        window = Window([])
        window.handle = 123
        with self.assertRaisesRegex(RuntimeError, "Multiple part_01"):
            grid_tree_item(window, "current[5]", "part_01")

    def test_reuses_complete_native_folder_when_uia_omits_it(self):
        folder = NativeItem("current[5]", [NativeItem("part_01"), NativeItem("part_02")])
        self.native_tree(folder)
        window = Window([])
        window.handle = 123
        self.assertIs(imported_folder(window, "current[5]", {"part_01", "part_02"}), folder)

    def test_incomplete_native_folder_is_not_treated_as_absent(self):
        self.native_tree(NativeItem("current[5]", [NativeItem("part_01")]))
        window = Window([])
        window.handle = 123
        with self.assertRaisesRegex(RuntimeError, "import is not ready"):
            imported_folder(window, "current[5]", {"part_01", "part_02"}, timeout=0.01)

    def test_import_waits_for_folder_to_appear(self):
        folder = NativeItem("current[5]", [NativeItem("part_01")])
        with patch("tools.ovi_batch_export._native_folders", side_effect=[[], [], [folder]]):
            self.assertIs(imported_folder(Window([]), "current[5]", {"part_01"},
                                          wait_for_absent=True), folder)

    def test_missing_folder_after_import_cannot_report_ready(self):
        with self.assertRaisesRegex(RuntimeError, "import is not ready"):
            imported_folder(Window([]), "current[5]", {"part_01"},
                            timeout=0.01, wait_for_absent=True)

    def test_native_property_path_scrolls_clicks_and_verifies_name(self):
        item = Mock()
        bounds = SimpleNamespace(left=10, top=10, right=100, bottom=30,
                                 width=lambda: 90, height=lambda: 20,
                                 mid_point=lambda: SimpleNamespace(x=55, y=20))
        item.client_rect.return_value = bounds
        item.tree_ctrl.client_rect.return_value = bounds
        dialog = Mock()
        dialog.descendants.return_value = [SimpleNamespace(
            iface_value=SimpleNamespace(CurrentValue="part_01"))]
        menu_item = Mock()
        with (patch("tools.ovi_batch_export.maximize"),
              patch("tools.ovi_batch_export.send_keys"),
              patch("tools.ovi_batch_export.time.sleep"),
              patch("tools.ovi_batch_export.grid_tree_item", return_value=_NativeTreeItem(item)),
              patch("tools.ovi_batch_export.property_menu_item", return_value=menu_item),
              patch("tools.ovi_batch_export.child_dialog", return_value=dialog)):
            self.assertIs(open_property(Window([]), "part_01", "current[5]"), dialog)
        item.ensure_visible.assert_called_once_with()
        item.click_input.assert_called_once_with(button="right")
        menu_item.iface_invoke.Invoke.assert_called_once_with()

    def test_uia_property_path_finds_tree_without_fixed_parent_depth(self):
        target = Mock()
        tree = Mock()
        tree.element_info.control_type = "Tree"
        target.parent.return_value = tree
        bounds = SimpleNamespace(left=10, top=10, right=100, bottom=30,
                                 width=lambda: 90, height=lambda: 20,
                                 mid_point=lambda: SimpleNamespace(x=55, y=20))
        target.rectangle.return_value = bounds
        tree.rectangle.return_value = bounds
        dialog = Mock()
        dialog.descendants.return_value = [SimpleNamespace(
            iface_value=SimpleNamespace(CurrentValue="part_01"))]
        with (patch("tools.ovi_batch_export.maximize"),
              patch("tools.ovi_batch_export.send_keys"),
              patch("tools.ovi_batch_export.time.sleep"),
              patch("tools.ovi_batch_export.grid_tree_item", return_value=target),
              patch("tools.ovi_batch_export.property_menu_item", return_value=Mock()),
              patch("tools.ovi_batch_export.child_dialog", return_value=dialog)):
            self.assertIs(open_property(Window([]), "part_01", "current[5]"), dialog)
        target.iface_scroll_item.ScrollIntoView.assert_called_once_with()
        target.right_click_input.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
