"""Read Ovi's map menu and switch only to a configured, allowed map."""

from __future__ import annotations

import time

from pywinauto import Desktop
from pywinauto.keyboard import send_keys

from src.basemaps import classify, menu_title_matches


def _name(item):
    return item.window_text().strip()


def _visible(item):
    try:
        return bool(item.is_visible())
    except Exception:
        return False


def _menu_controls(window):
    """Prefer the open Windows popup over hidden main-window menu descendants."""
    popups = [item for item in Desktop(backend="uia").windows()
              if item.element_info.class_name == "#32768" and _visible(item)]
    for popup in popups:
        items = popup.descendants(control_type="MenuItem")
        if any(_name(item) in {"天地图", "天地图影像"} for item in items):
            return items
    return [item for item in window.descendants(control_type="MenuItem")
            if _visible(item)]


def _map_section(items):
    names = [_name(item) for item in items]
    start = next((index for index, name in enumerate(names)
                  if name in {"天地图", "天地图影像"}), None)
    if start is None:
        return [item for item in items if classify(_name(item))]
    end = next((index for index in range(start, len(names))
                if names[index] in {"定制地图菜单", "自定义地图菜单"}), None)
    return items[start:end] if end is not None else [
        item for item in items[start:] if classify(_name(item))]


def _open_map_menu(window):
    last_error = None
    for attempt in range(3):
        send_keys("{ESC}")
        try:
            candidates = [item for item in window.descendants(control_type="MenuItem")
                          if _name(item).startswith("地图切换")]
            candidates = [item for item in candidates if _visible(item)] or candidates
            if not candidates:
                raise RuntimeError("无法定位奥维“地图切换”菜单")
            candidates[0].click_input()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                items = _map_section(_menu_controls(window))
                if any(_name(item) in {"天地图", "天地图影像"} for item in items):
                    return items
                time.sleep(0.25)
            raise RuntimeError("打开“地图切换”后未读到完整底图列表")
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.5)
    raise RuntimeError(
        f"读取奥维底图菜单失败（重试 3 次）：{last_error}；"
        "请检查奥维是否最大化、菜单是否被其他窗口遮挡") from last_error


def read_map_menu(window):
    """Return every entry in the visible map section, including forbidden entries."""
    try:
        items = _open_map_menu(window)
        found = {}
        for item in items:
            name = _name(item)
            if not name or name.startswith("地图切换"):
                continue
            enabled = bool(item.is_enabled())
            choice = classify(name)
            if choice and menu_title_matches(window.window_text(), choice):
                enabled = True
            found[name] = {"name": name, "enabled": enabled}
        return list(found.values())
    finally:
        send_keys("{ESC}")


def ensure_basemap(window, choice):
    if menu_title_matches(window.window_text(), choice):
        return
    for attempt in range(2):
        try:
            items = _open_map_menu(window)
            matches = [item for item in items if _name(item) == choice.label]
            if not matches:
                names = ", ".join(_name(item) for item in items)
                raise RuntimeError(
                    f"当前奥维菜单中没有“{choice.label}”；已看到：{names}。"
                    "请重新读取底图菜单，并检查版本或定制地图菜单")
            visible = [item for item in matches if _visible(item)]
            candidates = visible or matches
            enabled = [item for item in candidates if item.is_enabled()]
            item = (enabled or candidates)[0]
            uia_enabled = bool(enabled)
            try:
                item.click_input()
            except Exception:
                item.iface_invoke.Invoke()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if menu_title_matches(window.window_text(), choice):
                    return
                time.sleep(0.5)
            raise RuntimeError(
                f"已点击“{choice.label}”，但奥维窗口标题未确认切换；"
                f"UIA 可用状态：{uia_enabled}；当前标题：{window.window_text()}")
        except RuntimeError as exc:
            send_keys("{ESC}")
            if attempt == 1 or "当前奥维菜单中没有" in str(exc):
                raise
            time.sleep(0.5)
        except Exception:
            send_keys("{ESC}")
            if attempt == 1:
                raise
            time.sleep(0.5)
