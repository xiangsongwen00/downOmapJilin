"""Regression checks for Ovi favorites with scrolled or duplicate rows."""

import unittest

from tools.ovi_batch_export import grid_tree_item


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


class GridLookupTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
