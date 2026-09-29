"""Test Ovi's tree item -> context menu -> properties path using UIA names.

Run with the registered gis312 interpreter. The script opens properties for two
different grid rows, verifies the name field, and closes each dialog.
"""

from __future__ import annotations

import json
import sys
import time

from pywinauto import Desktop
from pywinauto.keyboard import send_keys
import win32con
import win32gui


ITEMS = (
    "03_山丘地块_天台村_part_02",
    "07_feature_6_part_04",
)


def box(control):
    rect = control.rectangle()
    return [rect.left, rect.top, rect.right, rect.bottom]


def main_window():
    matches = [
        w for w in Desktop(backend="uia").windows()
        if "奥维互动地图(VIP)" in w.window_text()
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one Ovi window, got {len(matches)}")
    return matches[0]


def property_dialog(window):
    matches = [
        c for c in window.descendants(control_type="Window")
        if c.window_text() == "图形设置"
    ]
    if len(matches) > 1:
        raise RuntimeError(f"Multiple 图形设置 dialogs: {len(matches)}")
    return matches[0] if matches else None


def close_property_dialog(window):
    dialog = property_dialog(window)
    if dialog:
        close_buttons = [
            c for c in dialog.descendants(control_type="Button")
            if c.window_text() == "关闭"
        ]
        if len(close_buttons) != 1:
            raise RuntimeError("Cannot locate dialog title-bar close button")
        close_buttons[0].click_input()
        time.sleep(0.3)


def test_item(name):
    window = main_window()
    close_property_dialog(window)
    if window.is_minimized():
        window.restore()
    win32gui.ShowWindow(window.handle, win32con.SW_MAXIMIZE)
    window.set_focus()
    matches = [
        c for c in window.descendants(control_type="TreeItem")
        if c.window_text() == name
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one tree item {name!r}, got {len(matches)}")
    target = matches[0]
    target.iface_scroll_item.ScrollIntoView()
    time.sleep(0.2)
    target_box = box(target)
    choices = []
    seen = []
    for attempt in range(3):
        target.right_click_input()
        time.sleep(0.4)
        menus = [
            w for w in Desktop(backend="uia").windows()
            if w.element_info.class_name == "#32768" and w.is_visible()
        ]
        choices = [
            (menu, item)
            for menu in menus
            for item in menu.descendants(control_type="MenuItem")
            if item.window_text().startswith("属性(")
        ]
        if len(choices) == 1:
            break
        seen = [
            (box(menu), [c.window_text() for c in menu.descendants(control_type="MenuItem")])
            for menu in menus
        ]
        send_keys("{ESC}")
        time.sleep(0.3)
        window.set_focus()
        target.iface_scroll_item.ScrollIntoView()
        time.sleep(0.3)
    if len(choices) != 1:
        raise RuntimeError(f"Expected one 属性 menu item, got {len(choices)}; target={target_box!r}; menus={seen!r}")
    menu, choice = choices[0]
    menu_box = box(menu)
    choice_box = box(choice)
    choice.iface_invoke.Invoke()
    time.sleep(0.4)

    dialog = property_dialog(window)
    if dialog is None:
        raise RuntimeError("属性 command did not open 图形设置")
    edits = dialog.descendants(control_type="Edit")
    values = [edit.iface_value.CurrentValue for edit in edits]
    if name not in values:
        raise RuntimeError(f"Dialog does not show selected grid name: {values!r}")
    result = {
        "target": name,
        "target_box": target_box,
        "menu_box": menu_box,
        "property_box": choice_box,
        "dialog": dialog.window_text(),
        "name_verified": name in values,
        "menu_attempts": attempt + 1,
    }
    close_property_dialog(window)
    return result


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps([test_item(name) for name in ITEMS], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
