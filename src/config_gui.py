"""Small Tk settings and run console for the Ovi export pipeline."""

from __future__ import annotations

import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk


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


class ConfigWindow:
    def __init__(self, root: tk.Tk, config_path: Path):
        from index import PACKAGED

        self.root = root
        self.config_path = config_path
        self.raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
        self.path_fields = tuple(field for field in PATH_FIELDS if field[0] != "python" or not PACKAGED)
        self.values: dict[str, tk.StringVar] = {}
        self.messages: queue.Queue[str | None] = queue.Queue()
        self.process: subprocess.Popen | None = None

        root.title("奥维 18 级 TIF 自动导出")
        root.geometry("1040x760")
        root.minsize(880, 660)
        outer = ttk.Frame(root, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=f"配置文件：{config_path}").pack(anchor="w", pady=(0, 8))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="x")
        paths = ttk.Frame(notebook, padding=12)
        options = ttk.Frame(notebook, padding=12)
        notebook.add(paths, text="路径")
        notebook.add(options, text="参数")
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

    def save(self) -> bool:
        if self.process is not None and self.process.poll() is None:
            messagebox.showerror("任务正在运行", "请等待当前任务结束后再修改配置。")
            return False
        from index import PACKAGED, atomic_json, resolve_config

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
            if not PACKAGED and "python" in resolved and not resolved["python"].is_file():
                raise FileNotFoundError(f"Python 不存在：{resolved['python']}")
            if not resolved["source_features"].is_file():
                raise FileNotFoundError(f"原始要素不存在：{resolved['source_features']}")
            if "plan_buffer_meters" not in data and not resolved["target"].is_file():
                raise FileNotFoundError(f"下载目标不存在：{resolved['target']}")
            if "plan_buffer_meters" in data and resolved["target"].suffix.lower() not in (".geojson", ".json"):
                raise ValueError("自动生成缓冲网格时，目标路径必须是 GeoJSON")
        except (ValueError, OSError, TypeError) as exc:
            messagebox.showerror("配置错误", str(exc))
            return False
        atomic_json(self.config_path, data)
        self.raw = data
        self.status.set(f"已保存：{self.config_path}")
        return True

    def start(self, action: str):
        if not self.save():
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
            messagebox.showerror("启动失败", str(exc))
            return
        self.status.set("运行中")
        self.append_log("\n" + " ".join(command) + "\n")
        threading.Thread(target=self.read_output, args=(self.process,), daemon=True).start()

    def read_output(self, process: subprocess.Popen):
        assert process.stdout is not None
        for line in process.stdout:
            self.messages.put(line)
        code = process.wait()
        self.messages.put(f"\n任务结束，退出码：{code}\n")
        self.messages.put(None)

    def drain_messages(self):
        try:
            while True:
                message = self.messages.get_nowait()
                if message is None:
                    code = self.process.returncode if self.process is not None else None
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
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.status.set("正在停止")


def launch_gui(config_path: Path):
    root = tk.Tk()
    ConfigWindow(root, config_path)
    root.mainloop()
