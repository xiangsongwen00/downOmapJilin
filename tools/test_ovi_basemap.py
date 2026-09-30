"""Menu switching tolerates Ovi's unreliable UIA enabled state."""

from unittest import TestCase
from unittest.mock import patch

from src.basemaps import Basemap
from tools.ovi_basemap import ensure_basemap


class Item:
    def __init__(self, name, window, enabled, visible=True):
        self.name = name
        self.window = window
        self.enabled = enabled
        self.visible = visible
        self.clicked = False

    def window_text(self):
        return self.name

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def click_input(self):
        self.clicked = True
        self.window.title = f"奥维互动地图(VIP) -- {self.name}[14]"


class Window:
    def __init__(self):
        self.title = "奥维互动地图(VIP) -- 天地图[14]"

    def window_text(self):
        return self.title


class MenuTests(TestCase):
    def test_false_disabled_state_can_still_switch(self):
        window = Window()
        choice = Basemap("tencent_vector", "腾讯电子地图", None)
        item = Item(choice.label, window, enabled=False)
        with patch("tools.ovi_basemap._open_map_menu", return_value=[item]):
            ensure_basemap(window, choice)
        self.assertTrue(item.clicked)

    def test_duplicate_hidden_item_is_ignored(self):
        window = Window()
        choice = Basemap("tencent_vector", "腾讯电子地图", None)
        hidden = Item(choice.label, window, enabled=True, visible=False)
        visible = Item(choice.label, window, enabled=True)
        with patch("tools.ovi_basemap._open_map_menu", return_value=[hidden, visible]):
            ensure_basemap(window, choice)
        self.assertFalse(hidden.clicked)
        self.assertTrue(visible.clicked)
