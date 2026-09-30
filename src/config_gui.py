"""Small Tk settings and run console for the Ovi export pipeline."""

from __future__ import annotations

import json
from datetime import date
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import win32api
import win32con
import win32gui
import win32process


PATH_FIELDS = (
    ("target", "下载目标", "file"),
    ("source_features", "原始要素", "file"),
    ("omapexepath", "奥维程序", "file"),
    ("python", "gis312 Python", "file"),
    ("export_dir", "分块 TIF 目录", "directory"),
    ("mosaic_dir", "最终 TIF 目录", "directory"),
    ("prepared_target", "格式转换目标", "file"),
    ("state_file", "运行状态文件", "file"),
    ("lock_file", "单实例锁文件", "file"),
)
NUMBER_FIELDS = (
    ("startup_timeout_seconds", "奥维启动等待（秒）", 120),
    ("export_timeout_seconds", "单块导出等待（秒）", 900),
    ("download_timeout_seconds", "缺图下载等待（秒）", 900),
    ("max_attempts", "单块最多重试", 3),
)


def _export_process(pid: int, executable: Path):
    """Open only a process running this exact executable."""
    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ |
            win32con.PROCESS_TERMINATE, False, pid)
        image = Path(win32process.GetModuleFileNameEx(handle, 0)).resolve()
        if os.path.normcase(str(image)) == os.path.normcase(str(executable.resolve())):
            return handle
        handle.Close()
    except OSError:
        pass
    return None


def _has_visible_window(pid: int):
    found = []

    def inspect(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == pid:
            found.append(hwnd)

    win32gui.EnumWindows(inspect, None)
    return bool(found)


def _legacy_worker_pid(executable: Path):
    """Find one headless worker from an older build without owner metadata."""
    matches = []
    for pid in win32process.EnumProcesses():
        if pid == os.getpid():
            continue
        handle = _export_process(pid, executable)
        if handle is not None:
            handle.Close()
            if not _has_visible_window(pid):
                matches.append(pid)
    return matches[0] if len(matches) == 1 else None


def _clear_lock_or_report(path: Path, parent=None):
    from index import cleanup_lock_files

    if cleanup_lock_files(path):
        return True
    messagebox.showerror("锁清理失败", f"运行锁仍被占用或无法删除：{path}", parent=parent)
    return False


class ConfigWindow:
    def __init__(self, root: tk.Tk, config_path: Path):
        from index import PACKAGED, ROOT

        self.root = root
        self.config_path = config_path
        self.raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
        self.path_fields = tuple(field for field in PATH_FIELDS if field[0] != "python" or not PACKAGED)
        self.values: dict[str, tk.StringVar] = {}
        self.messages: queue.Queue[tuple[subprocess.Popen, str | None]] = queue.Queue()
        self.process: subprocess.Popen | None = None
        self.reader_thread: threading.Thread | None = None
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        root.title("奥维 18 级 TIF 自动导出")
        icon = ROOT / "src" / "logo" / "logo.ico"
        image = ROOT / "src" / "logo" / "logo.png"
        if icon.is_file():
            root.iconbitmap(str(icon))
        if image.is_file():
            self.logo_image = tk.PhotoImage(file=str(image))
            root.iconphoto(True, self.logo_image)
        root.geometry("1040x760")
        root.minsize(880, 660)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TNotebook.Tab", padding=(14, 7))
        style.map("TNotebook.Tab",
                  background=[("selected", "#dceeff"), ("!selected", "#f2f4f7")],
                  foreground=[("selected", "#15395c"), ("!selected", "#333333")])
        outer = ttk.Frame(root, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=f"配置文件：{config_path}").pack(anchor="w", pady=(0, 8))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="x")
        paths = ttk.Frame(notebook, padding=12)
        options = ttk.Frame(notebook, padding=12)
        map_status = ttk.Frame(notebook, padding=12)
        notebook.add(paths, text="路径")
        notebook.add(options, text="参数")
        notebook.add(map_status, text="底图状态")
        self.map_summary = tk.StringVar()
        ttk.Label(map_status, textvariable=self.map_summary).pack(anchor="w", pady=(0, 8))
        columns = ("name", "state")
        self.map_tree = ttk.Treeview(map_status, columns=columns, show="headings", height=14)
        self.map_tree.heading("name", text="奥维菜单项")
        self.map_tree.heading("state", text="下载状态")
        self.map_tree.column("name", width=480, anchor="w")
        self.map_tree.column("state", width=220, anchor="w")
        self.map_tree.pack(fill="both", expand=True)
        paths.columnconfigure(1, weight=1)
        for row, (key, label, kind) in enumerate(self.path_fields):
            ttk.Label(paths, text=label, width=18).grid(row=row, column=0, sticky="w", pady=4)
            var = tk.StringVar(value=str(self.raw.get(key, "")))
            self.values[key] = var
            ttk.Entry(paths, textvariable=var).grid(row=row, column=1, sticky="ew", padx=6)
            ttk.Button(paths, text="浏览…", command=lambda k=key, t=kind: self.browse(k, t)).grid(
                row=row, column=2, padx=(2, 0))

        options.columnconfigure(1, weight=1)
        for row, (key, label, default) in enumerate(NUMBER_FIELDS):
            ttk.Label(options, text=label, width=22).grid(row=row, column=0, sticky="w", pady=5)
            var = tk.StringVar(value=str(self.raw.get(key, default)))
            self.values[key] = var
            ttk.Entry(options, textvariable=var, width=16).grid(row=row, column=1, sticky="w")
        row = len(NUMBER_FIELDS)
        ttk.Label(options, text="下载底图", width=22).grid(row=row, column=0, sticky="w", pady=5)
        self.basemap_value = tk.StringVar()
        self.basemap_options: dict[str, str] = {}
        self.basemap_combo = ttk.Combobox(options, textvariable=self.basemap_value,
                                         state="readonly", width=54)
        self.basemap_combo.grid(row=row, column=1, sticky="ew")
        self.basemap_value.trace_add("write", lambda *_: self.render_basemap_status())
        ttk.Button(options, text="重新读取底图", command=self.refresh_basemaps).grid(
            row=row, column=2, padx=6)
        self.set_basemap_options(self.raw.get("basemap_menu", []), self.raw.get("basemap_id"))
        row += 1
        self.generated = tk.BooleanVar(value="plan_buffer_meters" in self.raw)
        ttk.Checkbutton(options, text="由原始要素生成缓冲网格", variable=self.generated).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=6)
        row += 1
        ttk.Label(options, text="网格缓冲（米）").grid(row=row, column=0, sticky="w", pady=5)
        self.values["plan_buffer_meters"] = tk.StringVar(
            value=str(self.raw.get("plan_buffer_meters", 20)))
        ttk.Entry(options, textvariable=self.values["plan_buffer_meters"], width=16).grid(
            row=row, column=1, sticky="w")
        row += 1
        self.mosaic = tk.BooleanVar(value=bool(self.raw.get("mosaic", True)))
        ttk.Checkbutton(options, text="导出完成后生成最终要素 TIF", variable=self.mosaic).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=6)
        row += 1
        ttk.Label(options, text="固定设置：18 级、TIF、奥维默认坐标系").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=6)

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=10)
        ttk.Button(actions, text="确定并保存", command=self.save).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="保存并运行", command=lambda: self.start("--run")).pack(side="left", padx=8)
        ttk.Button(actions, text="仅重新拼接", command=lambda: self.start("--mosaic-only")).pack(side="left", padx=8)
        ttk.Button(actions, text="停止任务", command=self.stop).pack(side="right")

        self.status = tk.StringVar(value="就绪")
        ttk.Label(outer, textvariable=self.status).pack(anchor="w")
        self.log = scrolledtext.ScrolledText(outer, height=18, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, pady=(6, 0))
        root.after(200, self.drain_messages)

    def browse(self, key: str, kind: str):
        current = self.values[key].get()
        initial = Path(current).expanduser()
        if not initial.is_absolute():
            initial = self.config_path.parent / initial
        if kind == "directory":
            selected = filedialog.askdirectory(initialdir=str(initial if initial.is_dir() else initial.parent))
        else:
            selected = filedialog.askopenfilename(initialdir=str(initial.parent), filetypes=[
                ("目标与要素", "*.geojson *.json *.shp *.gpkg"),
                ("程序", "*.exe"), ("所有文件", "*.*")])
        if selected:
            self.values[key].set(selected)
            if key == "target" and Path(selected).suffix.lower() in {".shp", ".gpkg"}:
                self.generated.set(False)

    def set_basemap_options(self, menu, selected=None):
        from src.basemaps import FAMILIES, resolve_basemap

        self.basemap_options = {}
        for family, title in FAMILIES.items():
            try:
                choice = resolve_basemap(family, menu)
            except RuntimeError:
                continue
            self.basemap_options[f"{title} · {choice.label}"] = family
        self.basemap_combo["values"] = list(self.basemap_options)
        display = next((label for label, family in self.basemap_options.items()
                        if family == selected), "")
        self.basemap_value.set(display)
        self.render_basemap_status(menu)

    def render_basemap_status(self, menu=None):
        from src.basemaps import classify

        if menu is None:
            menu = self.raw.get("basemap_menu", [])
        for row in self.map_tree.get_children():
            self.map_tree.delete(row)
        counts = {"可下载": 0, "当前禁用": 0, "禁止下载": 0, "菜单功能": 0}
        for entry in menu:
            name = entry if isinstance(entry, str) else entry.get("name", "")
            enabled = True if isinstance(entry, str) else entry.get("enabled", False)
            choice = classify(name)
            if name in {"定制地图菜单", "自定义地图菜单"}:
                state = "菜单功能"
            else:
                state = "可下载" if choice and enabled else ("当前禁用" if choice else "禁止下载")
            counts[state] += 1
            self.map_tree.insert("", "end", values=(name, state))
        selected = self.basemap_value.get() or "未选择"
        checked = self.raw.get("basemap_checked_year", "未读取")
        self.map_summary.set(
            f"读取年份：{checked}  |  菜单项：{len(menu)}  |  "
            f"可下载：{counts['可下载']}  当前禁用：{counts['当前禁用']}  "
            f"禁止下载：{counts['禁止下载']}  菜单功能：{counts['菜单功能']}  "
            f"|  当前选择：{selected}")

    def read_basemap_menu(self, executable):
        from index import ensure_ovi_running
        from src.basemaps import allowed_options
        from tools.ovi_basemap import read_map_menu

        self.status.set("正在启动或连接奥维，并最大化窗口…")
        self.root.update_idletasks()
        window = ensure_ovi_running({"omapexepath": executable,
                                     "startup_timeout_seconds": 120})
        self.status.set(f"已连接奥维：{window.window_text()}；正在读取地图切换菜单…")
        self.root.update_idletasks()
        menu = read_map_menu(window)
        if not allowed_options(menu):
            raise RuntimeError("奥维地图切换菜单中没有可用的允许下载底图")
        return menu

    def refresh_basemaps(self):
        from index import active_instance, atomic_json, project_path

        if self.process is not None and self.process.poll() is None:
            messagebox.showwarning("任务正在运行", "请先停止当前导出任务，再重新读取底图。", parent=self.root)
            return False
        lock_path = project_path(self.values["lock_file"].get().strip(),
                                 self.config_path.resolve().parent)
        if active_instance(lock_path) is not None:
            messagebox.showwarning("任务正在运行", "请先停止正在使用奥维的导出任务，再重新读取底图。", parent=self.root)
            return False
        executable = project_path(self.values["omapexepath"].get().strip(),
                                  self.config_path.resolve().parent)
        if not executable.is_file():
            messagebox.showerror("奥维路径错误", f"奥维 EXE 不存在：{executable}", parent=self.root)
            return False
        try:
            self.status.set("正在读取奥维地图切换菜单…")
            self.root.update_idletasks()
            menu = self.read_basemap_menu(executable)
            raw = dict(self.raw)
            selected = self.basemap_options.get(self.basemap_value.get()) or raw.get("basemap_id")
            raw.update(omapexepath=self.values["omapexepath"].get().strip(),
                       basemap_menu=menu, basemap_checked_year=date.today().year)
            self.set_basemap_options(menu, selected)
            if not self.basemap_value.get():
                raw.pop("basemap_id", None)
                raw.pop("basemap_resolved_label", None)
            atomic_json(self.config_path, raw)
            self.raw = raw
            self.render_basemap_status(menu)
            self.status.set(f"已读取 {len(self.basemap_options)} 种可下载底图；请选择后保存")
            return True
        except Exception as exc:
            self.status.set(f"读取底图失败：{exc}")
            messagebox.showerror("读取底图失败", str(exc), parent=self.root)
            return False

    def save(self) -> bool:
        if self.process is not None and self.process.poll() is None:
            messagebox.showerror("任务正在运行", "请等待当前任务结束后再修改配置。", parent=self.root)
            return False
        from index import PACKAGED, active_instance, atomic_json, cleanup_lock_files, resolve_config

        data = dict(self.raw)
        try:
            for key, _, _ in self.path_fields:
                value = self.values[key].get().strip()
                if not value and key != "python":
                    raise ValueError(f"{key} 不能为空")
                if value:
                    data[key] = value
                else:
                    data.pop(key, None)
            if PACKAGED:
                data.pop("python", None)
            for key, _, _ in NUMBER_FIELDS:
                data[key] = int(self.values[key].get().strip())
            if self.generated.get():
                data["plan_buffer_meters"] = float(self.values["plan_buffer_meters"].get().strip())
            else:
                data.pop("plan_buffer_meters", None)
            data["mosaic"] = self.mosaic.get()
            data.update(zoom=18, format="tif", coordinate_system="default")
            resolved = resolve_config(data, self.config_path.resolve().parent)
            owner = active_instance(resolved["lock_file"])
            if owner is not None:
                pid = f" PID {owner['pid']}" if owner.get("pid") else ""
                messagebox.showwarning("已有任务", f"导出任务{pid}仍在运行。请先点击“停止任务”，再保存配置。", parent=self.root)
                return False
            if not cleanup_lock_files(resolved["lock_file"]):
                raise OSError(f"无法清理运行锁：{resolved['lock_file']}")
            if not PACKAGED and "python" in resolved and not resolved["python"].is_file():
                raise FileNotFoundError(f"Python 不存在：{resolved['python']}")
            if not resolved["source_features"].is_file():
                raise FileNotFoundError(f"原始要素不存在：{resolved['source_features']}")
            if "plan_buffer_meters" not in data and not resolved["target"].is_file():
                raise FileNotFoundError(f"下载目标不存在：{resolved['target']}")
            if "plan_buffer_meters" in data and resolved["target"].suffix.lower() not in (".geojson", ".json"):
                raise ValueError("自动生成缓冲网格时，目标路径必须是 GeoJSON")
            if not data.get("basemap_menu") or data.get("basemap_checked_year") != date.today().year:
                selected = self.basemap_options.get(self.basemap_value.get()) or data.get("basemap_id")
                menu = self.read_basemap_menu(resolved["omapexepath"])
                data.update(basemap_menu=menu, basemap_checked_year=date.today().year)
                self.set_basemap_options(menu, selected)
                self.render_basemap_status(menu)
            family = self.basemap_options.get(self.basemap_value.get())
            if family:
                from src.basemaps import resolve_basemap

                data["basemap_id"] = family
                data["basemap_resolved_label"] = resolve_basemap(family, data["basemap_menu"]).label
            else:
                data.pop("basemap_id", None)
                data.pop("basemap_resolved_label", None)
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc), parent=self.root)
            return False
        atomic_json(self.config_path, data)
        self.raw = data
        self.render_basemap_status()
        self.status.set(f"已保存：{self.config_path}")
        return True

    def start(self, action: str):
        if not self.stop():
            return
        if not self.save():
            return
        if not self.raw.get("basemap_id"):
            messagebox.showwarning("请选择下载底图", "已读取奥维底图菜单；请先选择允许下载的底图，再保存并运行。", parent=self.root)
            return
        from index import EXECUTABLE, PACKAGED, ROOT

        command = ([str(EXECUTABLE)] if PACKAGED else [sys.executable, str(ROOT / "index.py")])
        command += ["--config", str(self.config_path), action]
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            self.process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True,
                                            encoding="utf-8", errors="replace",
                                            creationflags=flags)
        except OSError as exc:
            self.status.set("启动失败")
            self.append_log(f"\n启动失败：{exc}\n")
            messagebox.showerror("启动失败", str(exc), parent=self.root)
            return
        self.status.set("运行中")
        self.append_log("\n" + " ".join(command) + "\n")
        self.reader_thread = threading.Thread(
            target=self.read_output, args=(self.process,), daemon=True)
        self.reader_thread.start()

    def read_output(self, process: subprocess.Popen):
        assert process.stdout is not None
        with process.stdout:
            for line in process.stdout:
                self.messages.put((process, line))
        code = process.wait()
        self.messages.put((process, f"\n任务结束，退出码：{code}\n"))
        self.messages.put((process, None))

    def drain_messages(self):
        try:
            while True:
                process, message = self.messages.get_nowait()
                if process is not self.process:
                    continue
                if message is None:
                    code = process.returncode
                    self.status.set("已完成" if code == 0 else f"失败（退出码 {code}）")
                else:
                    self.append_log(message)
        except queue.Empty:
            pass
        self.root.after(200, self.drain_messages)

    def append_log(self, message: str):
        self.log.configure(state="normal")
        self.log.insert("end", message)
        self.log.see("end")
        self.log.configure(state="disabled")

    def stop(self):
        from index import EXECUTABLE, PACKAGED, active_instance, project_path
        parent = getattr(self, "root", None)

        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.status.set("正在停止")
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                messagebox.showerror("停止失败", "当前任务未能在 10 秒内退出。", parent=parent)
                return False
            if self.reader_thread is not None:
                self.reader_thread.join(timeout=5)
        lock_path = project_path(self.values["lock_file"].get().strip(), self.config_path.resolve().parent)
        owner = active_instance(lock_path)
        if owner is None:
            return _clear_lock_or_report(lock_path, parent)
        pid = owner.get("pid")
        if pid is None and PACKAGED:
            pid = _legacy_worker_pid(EXECUTABLE)
        if not pid:
            messagebox.showerror("无法确认遗留进程", "运行锁仍被占用，但无法唯一确认导出进程。请在任务管理器检查后重试。", parent=parent)
            return False
        handle = _export_process(int(pid), EXECUTABLE)
        if handle is None:
            if active_instance(lock_path) is None:
                return _clear_lock_or_report(lock_path, parent)
            messagebox.showerror("无法确认遗留进程", f"PID {pid} 持有运行锁，但程序路径与当前 EXE 不一致，已停止启动。", parent=parent)
            return False
        try:
            if active_instance(lock_path) is None:
                return _clear_lock_or_report(lock_path, parent)
            win32api.TerminateProcess(handle, 1)
        except OSError as exc:
            messagebox.showerror("停止失败", str(exc), parent=parent)
            return False
        finally:
            handle.Close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if active_instance(lock_path) is None:
                if not _clear_lock_or_report(lock_path, parent):
                    return False
                self.status.set(f"已停止旧任务 PID {pid}；可以续跑")
                return True
            time.sleep(0.2)
        messagebox.showerror("停止失败", f"已结束 PID {pid}，但运行锁仍未释放。", parent=parent)
        return False

    def on_close(self):
        if self.stop():
            self.root.destroy()


def launch_gui(config_path: Path):
    root = tk.Tk()
    ConfigWindow(root, config_path)
    root.mainloop()
