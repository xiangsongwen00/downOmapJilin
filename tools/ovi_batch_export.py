r"""Export an imported GeoJSON grid through the maximized Ovi desktop UI.

Run with D:\App\python_env\gis312\python.exe. UIA identifies controls by name;
click_input uses each control's current screen bounds. No feature-row or popup
screen coordinates are hard-coded.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

import pyproj

# A machine-wide PostgreSQL PROJ_LIB points at an incompatible database.
os.environ["PROJ_LIB"] = pyproj.datadir.get_data_dir()
os.environ["PROJ_DATA"] = pyproj.datadir.get_data_dir()

import rasterio
from affine import Affine
from rasterio.features import geometry_mask
from shapely.geometry import mapping, shape
from shapely.ops import transform as transform_geom
from pywinauto import Desktop
from pywinauto.keyboard import send_keys
import win32con
import win32gui


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLAN = ROOT / "out" / "grid_3km_z18.geojson"
DEFAULT_OUTPUT = ROOT / "out" / "vip_parts"


def wait_until(check, timeout=15, delay=0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(delay)
    raise TimeoutError(f"UI state not reached within {timeout}s")


def one(controls, description):
    controls = list(controls)
    if len(controls) != 1:
        raise RuntimeError(f"Expected one {description}; found {len(controls)}")
    return controls[0]


def main_window():
    windows = [w for w in Desktop(backend="uia").windows()
               if "奥维互动地图(VIP)" in w.window_text()]
    if not windows:
        handles = []

        def find_omap(handle, _):
            if (win32gui.GetClassName(handle) == "omapWin"
                    and "奥维互动地图(VIP)" in win32gui.GetWindowText(handle)):
                handles.append(handle)

        win32gui.EnumWindows(find_omap, None)
        windows = [Desktop(backend="uia").window(handle=handle) for handle in handles]
    return one(windows, "Ovi main window")


def maximize(window):
    if window.is_minimized():
        win32gui.ShowWindow(window.handle, win32con.SW_RESTORE)
    win32gui.ShowWindow(window.handle, win32con.SW_MAXIMIZE)
    try:
        win32gui.SetForegroundWindow(window.handle)
    except Exception:
        # Windows can refuse foreground activation after focus switches. The
        # current control's click_input still activates Ovi for the next step.
        pass
    wait_until(lambda: win32gui.GetWindowPlacement(window.handle)[1] == win32con.SW_MAXIMIZE)


def child_dialog(window, title):
    return next((c for c in window.descendants(control_type="Window")
                 if c.window_text() == title), None)


def dialog_starting(window, prefix):
    return next((c for c in window.descendants(control_type="Window")
                 if c.window_text().startswith(prefix)), None)


def button(dialog, name):
    return one((c for c in dialog.descendants(control_type="Button")
                if c.window_text() == name), f"{name} button")


def imported_folder(window, folder_name, expected_names):
    folders = [c for c in window.descendants(control_type="TreeItem")
               if c.window_text() == folder_name]
    if not folders:
        return None
    for folder in folders:
        if folder.iface_expand_collapse.CurrentExpandCollapseState == 0:
            folder.iface_expand_collapse.Expand()
    def ready_folder():
        for folder in window.descendants(control_type="TreeItem"):
            if folder.window_text() != folder_name:
                continue
            children = {item.window_text() for item in folder.descendants(control_type="TreeItem")}
            if expected_names.issubset(children):
                return folder
        return None
    try:
        return wait_until(ready_folder)
    except TimeoutError as exc:
        raise RuntimeError(f"Ovi already has {folder_name}, but its target items are incomplete") from exc


def import_plan(window, path, folder_name, expected_names):
    maximize(window)
    send_keys("{ESC}")
    system = one((c for c in window.descendants(control_type="MenuItem")
                  if c.window_text() == "系统(S)"), "系统(S) menu")
    system.click_input()
    import_item = wait_until(lambda: next((c for c in window.descendants(control_type="MenuItem")
                                            if c.window_text() == "导入对象" and c.rectangle().width() > 0), None))
    import_item.click_input()
    opened = wait_until(lambda: child_dialog(window, "打开"))
    filename = one((c for c in opened.descendants(control_type="Edit")
                    if c.window_text() == "文件名(N):"), "open filename field")
    filename.set_edit_text(str(path.resolve()))
    button(opened, "打开(O)").click_input()
    dialog = wait_until(lambda: dialog_starting(window, "导入对象 -- 对象:"))
    parsed_folder = one((c for c in dialog.descendants(control_type="TreeItem")
                         if c.window_text() == folder_name), "import preview folder")
    if parsed_folder is None:
        raise RuntimeError("GeoJSON import preview did not match the plan")
    button(dialog, "导入").click_input()
    success = wait_until(lambda: child_dialog(window, "omap"))
    if not any(c.window_text() == "导入成功" for c in success.descendants()):
        raise RuntimeError("Ovi did not report import success")
    button(success, "确定").click_input()
    wait_until(lambda: child_dialog(window, "omap") is None)
    imported_folder(window, folder_name, expected_names)


def property_menu_item():
    menus = [w for w in Desktop(backend="uia").windows()
             if w.element_info.class_name == "#32768" and w.is_visible()]
    found = [(menu, item) for menu in menus
             for item in menu.descendants(control_type="MenuItem")
             if item.window_text().startswith("属性(")]
    return found[0][1] if len(found) == 1 else None


def grid_tree_item(window, folder_name, name, timeout=10):
    """Locate a grid by its ancestry, even when the folder row is scrolled away."""
    deadline = time.monotonic() + timeout
    visible_names = visible_folders = 0
    while time.monotonic() < deadline:
        items = window.descendants(control_type="TreeItem")
        candidates = [item for item in items if item.window_text() == name]
        visible_names = len(candidates)
        matches = []
        for candidate in candidates:
            ancestor = candidate.parent()
            for _ in range(20):
                if ancestor is None:
                    break
                if ancestor.window_text() == folder_name:
                    matches.append(candidate)
                    break
                ancestor = ancestor.parent()
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError(f"Multiple {name} rows belong to {folder_name}")
        folders = [item for item in items if item.window_text() == folder_name]
        visible_folders = len(folders)
        for folder in folders:
            if folder.iface_expand_collapse.CurrentExpandCollapseState == 0:
                folder.iface_expand_collapse.Expand()
        time.sleep(0.25)
    raise RuntimeError(
        f"Cannot locate {name} under {folder_name} in Ovi favorites "
        f"(visible names: {visible_names}, visible folders: {visible_folders})")


def open_property(window, name, folder_name):
    maximize(window)
    for _ in range(3):
        send_keys("{ESC}")
        target = grid_tree_item(window, folder_name, name)
        target.iface_scroll_item.ScrollIntoView()
        time.sleep(0.3)
        target = grid_tree_item(window, folder_name, name)
        tree = target.parent().parent().parent()
        bounds, viewport = target.rectangle(), tree.rectangle()
        if (bounds.width() < 1 or bounds.height() < 1 or
                not (viewport.left <= bounds.mid_point().x <= viewport.right and
                     viewport.top <= bounds.mid_point().y <= viewport.bottom)):
            continue
        target.right_click_input()
        try:
            item = wait_until(property_menu_item, timeout=2)
        except TimeoutError:
            item = None
        if item:
            item.iface_invoke.Invoke()
            break
        send_keys("{ESC}")
        window.set_focus()
    else:
        raise RuntimeError(f"No 属性 context menu item for {name}")
    dialog = wait_until(lambda: child_dialog(window, "图形设置"))
    values = [c.iface_value.CurrentValue for c in dialog.descendants(control_type="Edit")]
    if name not in values:
        raise RuntimeError(f"Properties opened for another object: {values!r}")
    return dialog


def popup_item(label):
    menus = [w for w in Desktop(backend="uia").windows()
             if w.element_info.class_name == "#32768" and w.is_visible()]
    found = [c for menu in menus for c in menu.descendants(control_type="MenuItem")
             if c.window_text() == label]
    return found[0] if len(found) == 1 else None


def invoke_save_menu(window):
    for _ in range(3):
        item = popup_item("保存成图片")
        if item is None:
            export = dialog_starting(window, "导出成图片")
            if export is None:
                raise RuntimeError("Image export dialog disappeared")
            export.set_focus()
            button(export, "保存成图片").click_input()
            try:
                item = wait_until(lambda: popup_item("保存成图片"), timeout=2)
            except TimeoutError:
                continue
        item.iface_invoke.Invoke()
        return
    raise RuntimeError("Cannot open 保存成图片 menu after three attempts")


def recover_ui(window):
    """Return to the grid tree after a failed attempt, leaving the plan untouched."""
    for menu in (w for w in Desktop(backend="uia").windows()
                 if w.element_info.class_name == "#32768" and w.is_visible()):
        send_keys("{ESC}")
        break
    for _ in range(5):
        for title, action in (
            ("温馨提示", "我已知晓"), ("另存为", "取消"),
            ("工程坐标系设置", "取消"), ("导出图片", "退出"),
        ):
            dialog = child_dialog(window, title)
            if dialog is not None:
                dialog.set_focus()
                button(dialog, action).click_input()
                time.sleep(0.2)
    message = child_dialog(window, "omap")
    if message is not None:
        controls = {c.window_text(): c for c in message.descendants(control_type="Button")}
        for choice in ("否(N)", "确定", "关闭"):
            if choice in controls:
                controls[choice].click_input()
                time.sleep(0.2)
                break
    download = dialog_starting(window, "下载地图")
    if download is not None:
        status = " ".join(c.window_text() for c in download.descendants(control_type="Text"))
        if "正在下载地图" in status:
            raise RuntimeError("A map download is still running; UI recovery stopped")
        button(download, "隐藏").click_input()
    export = dialog_starting(window, "导出成图片")
    if export is not None:
        export.set_focus()
        button(export, "取消").click_input()
    advanced = child_dialog(window, "图形高级功能")
    if advanced is not None:
        one((c for c in advanced.descendants(control_type="Button")
             if c.window_text() == "关闭" and c.rectangle().width() > 100),
            "advanced close").click_input()
    props = child_dialog(window, "图形设置")
    if props is not None:
        button(props, "关闭").click_input()
    maximize(window)


def ensure_check(dialog, label, state):
    def current():
        return one((c for c in dialog.descendants(control_type="CheckBox")
                    if c.window_text() == label), f"{label} checkbox")

    check = current()
    if check.get_toggle_state() != state:
        check.toggle()
    wait_until(lambda: current().get_toggle_state() == state, timeout=3)


def save_dialog(window, destination):
    dialog = wait_until(lambda: child_dialog(window, "另存为"))
    format_choice = one((c for c in dialog.descendants(control_type="ComboBox")
                         if c.window_text() == "保存类型:"), "save format")
    format_choice.select("TIF格式 (*.tif)")
    if format_choice.iface_value.CurrentValue != "TIF格式 (*.tif)":
        raise RuntimeError("TIF format was not selected")
    filename = one((c for c in dialog.descendants(control_type="Edit")
                    if c.window_text() == "文件名:"), "save filename")
    filename.set_edit_text(str(destination.resolve()))
    button(dialog, "保存(S)").click_input()


def download_missing(window, feature_name, timeout):
    dialog = wait_until(lambda: dialog_starting(window, "下载地图"))
    levels = [c.iface_value.CurrentValue for c in dialog.descendants(control_type="Edit")
              if c.iface_value.CurrentValue.isdigit()]
    if levels != ["18", "18"]:
        raise RuntimeError(f"Download dialog level range is not 18–18: {levels!r}")
    regions = [c.iface_value.CurrentValue for c in dialog.descendants(control_type="Edit")]
    if not any(feature_name in region for region in regions):
        raise RuntimeError(f"Download region does not show {feature_name}")
    button(dialog, "开始下载").click_input()
    started = time.monotonic()
    full_at = None
    print(f"  download started: {feature_name}", flush=True)

    def finished():
        nonlocal full_at
        active = dialog_starting(window, "下载地图")
        if active is None:
            return "window closed"
        status = " ".join(c.window_text() for c in active.descendants(control_type="Text"))
        if "下载完成" in status:
            return status
        count = re.search(r"正在下载地图.*已下载\[(\d+)/(\d+)\]", status)
        if count and int(count.group(2)) > 0 and count.group(1) == count.group(2):
            full_at = full_at or time.monotonic()
            if time.monotonic() - full_at >= 7:
                return status
        else:
            full_at = None
        return None

    result = wait_until(finished, timeout=timeout, delay=1)
    completion = child_dialog(window, "omap")
    if completion is not None:
        message = " ".join(c.window_text() for c in completion.descendants())
        if "下载完成" in message:
            button(completion, "确定").click_input()
            wait_until(lambda: child_dialog(window, "omap") is None)
    active = dialog_starting(window, "下载地图")
    if active is not None:
        button(active, "隐藏").click_input()
        wait_until(lambda: dialog_starting(window, "下载地图") is None)
    time.sleep(max(0, 7 - (time.monotonic() - started)))
    wait_until(lambda: any(c.window_text() == "导出成图片 -- 获取图片结束"
                           for c in window.descendants(control_type="Window")),
               timeout=300, delay=1)
    print(f"  download finished: {result}", flush=True)


def open_save_as(window, export, feature_name, download_timeout):
    downloads = 0
    busy_retries = 0
    for attempt in range(3):
        if child_dialog(window, "另存为") is not None:
            return
        invoke_save_menu(window)
        while True:
            current = wait_until(lambda: child_dialog(window, "温馨提示")
                                 or child_dialog(window, "另存为")
                                 or child_dialog(window, "omap"))
            title = current.window_text()
            if title == "温馨提示":
                button(current, "我已知晓").click_input()
                wait_until(lambda: child_dialog(window, "温馨提示") is None)
                continue
            if title == "另存为":
                return
            message = " ".join(c.window_text() for c in current.descendants())
            if "系统正在下载地图" in message:
                if busy_retries >= 2:
                    raise RuntimeError(f"Ovi download did not settle: {message}")
                button(current, "否(N)").click_input()
                wait_until(lambda: child_dialog(window, "omap") is None)
                busy_retries += 1
                time.sleep(7)
                continue
            if "未下载图片" not in message or "是否先下载地图" not in message:
                raise RuntimeError(f"Ovi rejected image save: {message}")
            if downloads >= 2:
                raise RuntimeError(f"Ovi still reports missing images after two downloads: {message}")
            button(current, "是(Y)").click_input()
            download_missing(window, feature_name, download_timeout)
            downloads += 1
            break
    raise RuntimeError(f"Save dialog did not appear after {attempt + 1} attempts")


def archive_existing(path):
    if not path.exists():
        return
    archive = path.parent / "_previous"
    archive.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for source in (path, Path(str(path) + ".aux.xml")):
        if source.exists():
            target = archive / f"{source.stem}_{stamp}{source.suffix}"
            shutil.move(str(source), str(target))


def write_ledger(path, ledger):
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def verify_tif(path, feature=None):
    if not path.is_file() or path.stat().st_size < 4096:
        raise RuntimeError(f"TIF is missing or too small: {path}")
    with rasterio.open(path) as raster:
        if raster.crs is None or raster.crs.to_epsg() != 3857:
            raise RuntimeError(f"Unexpected TIF CRS: {raster.crs}")
        if raster.count < 3 or raster.width < 1 or raster.height < 1:
            raise RuntimeError("TIF has invalid dimensions or bands")
        if not (0.55 < raster.transform.a < 0.65 and
                -0.65 < raster.transform.e < -0.55):
            raise RuntimeError(f"Unexpected pixel size: {raster.transform}")
        result = {"bytes": path.stat().st_size, "width": raster.width,
                  "height": raster.height, "bands": raster.count}
        if feature is not None:
            geometry = transform_geom(
                pyproj.Transformer.from_crs(4326, 3857, always_xy=True).transform,
                shape(feature["geometry"]),
            )
            expected = present = 0
            for _, window in raster.block_windows(4):
                inside = geometry_mask(
                    [mapping(geometry)],
                    out_shape=(int(window.height), int(window.width)),
                    transform=raster.transform @ Affine.translation(window.col_off, window.row_off),
                    invert=True,
                )
                if not inside.any():
                    continue
                alpha = raster.read(4, window=window) >= 128
                expected += int(inside.sum())
                present += int((inside & alpha).sum())
            coverage = present / expected if expected else 0
            if coverage < 0.995:
                raise RuntimeError(f"Incomplete TIF coverage {coverage:.2%}: {path}")
            result["coverage"] = round(coverage, 6)
        return result


def export_one(window, feature, destination, timeout, download_timeout, folder_name):
    name = feature["properties"]["name"]
    props = open_property(window, name, folder_name)
    button(props, "高级").click_input()
    advanced = wait_until(lambda: child_dialog(window, "图形高级功能"))
    button(advanced, "导出成图片").click_input()
    fetch_started = time.monotonic()
    export = wait_until(lambda: dialog_starting(window, "导出成图片"))
    print(f"  image fetch started: {export.window_text()}", flush=True)
    zoom = one((c for c in export.descendants(control_type="ComboBox")
                if c.window_text() == "地图级别"), "map level")
    zoom.select("18")
    if zoom.iface_value.CurrentValue != "18":
        raise RuntimeError("Map level is not 18")
    ensure_check(export, "仅显示区域内的图片", 1)
    ensure_check(export, "在图片上显示奥维对象", 0)
    ensure_check(export, "显示当前图形", 0)
    pixel_label = next((c.window_text() for c in export.descendants(control_type="Text")
                        if "当前图片总共" in c.window_text()), "")
    match = re.search(r"当前图片总共(\d+)像素", pixel_label)
    if not match or int(match.group(1)) > 100_000_000:
        raise RuntimeError(f"Unexpected pixel estimate: {pixel_label!r}")
    time.sleep(max(0, 7 - (time.monotonic() - fetch_started)))
    wait_until(lambda: any(c.window_text() == "导出成图片 -- 获取图片结束"
                           for c in window.descendants(control_type="Window")),
               timeout=300, delay=1)
    print("  image fetch complete; saving TIF", flush=True)
    open_save_as(window, export, name, download_timeout)
    save_dialog(window, destination)
    coordinate = wait_until(lambda: child_dialog(window, "工程坐标系设置"))
    current = one((c for c in coordinate.descendants(control_type="Edit")
                   if c.window_text() == "当前坐标系"), "current coordinate system")
    if current.iface_value.CurrentValue != "WGS 84 / Pseudo-Mercator":
        raise RuntimeError(f"Unexpected default coordinate system: {current.iface_value.CurrentValue}")
    button(coordinate, "确定").click_input()
    progress = wait_until(lambda: child_dialog(window, "导出图片"))
    wait_until(lambda: any(c.window_text() == "导出图片完成"
                           for c in progress.descendants(control_type="Text")), timeout=timeout, delay=1)
    button(progress, "退出").click_input()
    result = verify_tif(destination, feature)
    button(wait_until(lambda: dialog_starting(window, "导出成图片")), "取消").click_input()
    advanced = wait_until(lambda: child_dialog(window, "图形高级功能"))
    close = one((c for c in advanced.descendants(control_type="Button")
                 if c.window_text() == "关闭" and c.rectangle().width() > 100),
                "advanced close")
    close.click_input()
    props = wait_until(lambda: child_dialog(window, "图形设置"))
    button(props, "关闭").click_input()
    return result


def run(args):
    plan_bytes = args.plan.read_bytes()
    plan_hash = hashlib.sha256(plan_bytes).hexdigest()
    features = json.loads(plan_bytes)["features"]
    names = [f["properties"]["name"] for f in features]
    if len(names) != len(set(names)):
        raise RuntimeError("Grid names are not unique")
    if not all(f["properties"]["zoom"] == 18 for f in features):
        raise RuntimeError("Plan includes a level other than 18")
    if args.only:
        features = [f for f in features if f["properties"]["name"] == args.only]
        if not features:
            raise RuntimeError(f"Grid not found in plan: {args.only}")
    if args.limit:
        features = features[:args.limit]
    args.output.mkdir(parents=True, exist_ok=True)
    ledger_path = args.output / "ovi_export_ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
    if ledger and ledger.get("plan_sha256") != plan_hash:
        raise RuntimeError("Target geometry changed; archive the old ledger explicitly before a new run")
    if not ledger:
        ledger = {"plan_sha256": plan_hash, "completed": {}}
    window = main_window()
    maximize(window)
    recover_ui(window)
    folder_name = f"{args.plan.stem}[{len(names)}]"
    if imported_folder(window, folder_name, set(names)) is None:
        print(f"Target tree absent: {folder_name}; importing", flush=True)
        import_plan(window, args.plan, folder_name, set(names))
    else:
        print(f"Target tree already exists: {folder_name}; reusing", flush=True)
    print(f"Grid import ready: {folder_name}; planned exports: {len(features)}", flush=True)
    for index, feature in enumerate(features, 1):
        name = feature["properties"]["name"]
        output = args.output / f"{name}.tif"
        if name in ledger["completed"] and output.is_file():
            try:
                verify_tif(output, feature)
            except Exception as exc:
                print(f"[{index}/{len(features)}] existing TIF invalid: {exc}", flush=True)
            else:
                print(f"[{index}/{len(features)}] skip verified {name}", flush=True)
                continue
        if output.is_file():
            try:
                existing = verify_tif(output, feature)
            except RuntimeError:
                pass
            else:
                ledger["completed"][name] = existing
                write_ledger(ledger_path, ledger)
                print(f"[{index}/{len(features)}] adopt verified {name}", flush=True)
                continue
        result = None
        for attempt in range(1, getattr(args, "max_attempts", 3) + 1):
            try:
                recover_ui(window)
                archive_existing(output)
                print(f"[{index}/{len(features)}] exporting {name}; attempt {attempt}", flush=True)
                result = export_one(window, feature, output, args.timeout,
                                    args.download_timeout, folder_name)
                break
            except Exception as exc:
                print(f"[{index}/{len(features)}] attempt {attempt} failed: {exc}", flush=True)
                try:
                    recover_ui(window)
                except Exception as recovery_exc:
                    raise RuntimeError(f"Cannot recover Ovi UI: {recovery_exc}") from exc
                if attempt == getattr(args, "max_attempts", 3):
                    raise
        ledger["completed"][name] = result
        write_ledger(ledger_path, ledger)
        print(f"[{index}/{len(features)}] complete {name}: {result}", flush=True)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Export all level-18 GeoJSON grid regions through Ovi UI")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--only", help="Export one exact grid name")
    parser.add_argument("--limit", type=int, help="Export only the first N grid features")
    parser.add_argument("--timeout", type=int, default=900, help="Seconds to wait for each TIF")
    parser.add_argument("--download-timeout", type=int, default=900,
                        help="Seconds to wait for each missing-map download")
    args = parser.parse_args()
    try:
        run(args)
    except Exception:
        from PIL import ImageGrab
        failure = ROOT / "out" / f"ovi_failure_{datetime.now():%Y%m%d_%H%M%S}.png"
        ImageGrab.grab().save(failure)
        print(f"Stopped; UI screenshot: {failure}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
