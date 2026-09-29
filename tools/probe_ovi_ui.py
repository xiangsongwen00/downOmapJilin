"""Inspect Ovi UI controls without assuming popup screen coordinates."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pywinauto import Desktop


def describe(control):
    info = control.element_info
    try:
        rect = control.rectangle()
        box = [rect.left, rect.top, rect.right, rect.bottom]
    except Exception:
        box = None
    return {
        "name": info.name,
        "type": info.control_type,
        "class": info.class_name,
        "handle": info.handle,
        "rectangle": box,
        "visible": control.is_visible(),
        "enabled": control.is_enabled(),
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--match", default="grid_3km_z18")
    parser.add_argument("--backend", default="uia", choices=["uia", "win32"])
    parser.add_argument("--right-click", help="Exact tree item name to open its context menu")
    parser.add_argument("--open-property", action="store_true")
    args = parser.parse_args()
    desk = Desktop(backend=args.backend)
    windows = [w for w in desk.windows() if "奥维互动地图" in w.window_text()]
    print("WINDOWS", json.dumps([describe(w) for w in windows], ensure_ascii=False))
    for window in windows:
        if args.right_click:
            if window.is_minimized():
                window.restore()
            window.set_focus()
        matches = []
        for control in window.descendants():
            name = control.window_text()
            if args.match in name:
                matches.append(describe(control))
        print("MATCHES", json.dumps(matches[:100], ensure_ascii=False))
        if args.right_click:
            target = next(
                c for c in window.descendants(control_type="TreeItem")
                if c.window_text() == args.right_click
            )
            print("TARGET_BEFORE", json.dumps(describe(target), ensure_ascii=False))
            target.iface_scroll_item.ScrollIntoView()
            time.sleep(0.3)
            print("TARGET_AFTER", json.dumps(describe(target), ensure_ascii=False))
            target.right_click_input()
            time.sleep(0.4)
            for backend in ("uia", "win32"):
                popup_desktop = Desktop(backend=backend)
                popup_windows = [
                    w for w in popup_desktop.windows()
                    if w.is_visible() and (w.element_info.class_name == "#32768" or "属性" in w.window_text())
                ]
                print("POPUPS", backend, json.dumps([describe(w) for w in popup_windows], ensure_ascii=False))
                for popup in popup_windows:
                    children = [describe(c) for c in popup.descendants()]
                    print("POPUP_CHILDREN", backend, json.dumps(children[:100], ensure_ascii=False))
            if args.open_property:
                menus = [
                    w for w in Desktop(backend="uia").windows()
                    if w.element_info.class_name == "#32768" and w.is_visible()
                ]
                properties = [
                    c for menu in menus for c in menu.descendants(control_type="MenuItem")
                    if c.window_text().startswith("属性(")
                ]
                if len(properties) != 1:
                    raise RuntimeError(f"Expected one 属性 menu item, found {len(properties)}")
                properties[0].click_input()
                time.sleep(0.5)
                dialogs = [
                    w for w in Desktop(backend="uia").windows()
                    if w.is_visible() and ("图形设置" in w.window_text() or "属性" in w.window_text())
                ]
                print("DIALOGS", json.dumps([describe(w) for w in dialogs], ensure_ascii=False))
                for dialog in dialogs:
                    print("DIALOG_CHILDREN", json.dumps([describe(c) for c in dialog.descendants()][:100], ensure_ascii=False))


if __name__ == "__main__":
    main()
