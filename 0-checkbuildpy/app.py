"""Desktop interface for the static Nuitka dependency auditor."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))
from checkbuildpy import audit, render_html, render_text  # noqa: E402


class AuditApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Nuitka 依赖与打包审计")
        self.root.geometry("1120x760")
        self.result = None
        self.build = tk.StringVar()
        self.entry = tk.StringVar()
        self.project = tk.StringVar()
        self.interpreter = tk.StringVar()
        self.report = tk.StringVar()
        self.status = tk.StringVar(value="选择构建脚本与入口文件，然后开始分析。不会执行构建脚本。")
        self.settings_path = HERE / "app_settings.json"
        self._load_settings()
        self._layout()

    def _load_settings(self):
        if self.settings_path.is_file():
            try:
                saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
                for key in ("build", "entry", "project", "interpreter", "report"):
                    getattr(self, key).set(saved.get(key, ""))
                return
            except (OSError, ValueError):
                pass
        root = HERE.parent
        if (root / "build_msvc.bat").is_file():
            self.build.set(str(root / "build_msvc.bat"))
            self.project.set(str(root))
            if (root / "index.py").is_file():
                self.entry.set(str(root / "index.py"))

    def _save_settings(self):
        values = {key: getattr(self, key).get() for key in
                  ("build", "entry", "project", "interpreter", "report")}
        self.settings_path.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")

    def _layout(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        for row, (label, variable, kind) in enumerate((
            ("构建脚本 (.bat/.cmd/.py)", self.build, "build"),
            ("入口文件 (index.py/app.py/main.py)", self.entry, "entry"),
            ("项目根目录", self.project, "directory"),
            ("构建 Python（可选）", self.interpreter, "python"),
            ("编译报告 XML（可选）", self.report, "report"),
        )):
            ttk.Label(outer, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(outer, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8)
            ttk.Button(outer, text="浏览…", command=lambda v=variable, k=kind: self._browse(v, k)).grid(row=row, column=2)
        outer.columnconfigure(1, weight=1)
        buttons = ttk.Frame(outer)
        buttons.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(12, 6))
        ttk.Button(buttons, text="分析依赖", command=self.analyze).pack(side="left")
        ttk.Button(buttons, text="保存 JSON + HTML 报告", command=self.save).pack(side="left", padx=8)
        ttk.Button(buttons, text="复制建议参数", command=self.copy_suggestions).pack(side="left")
        ttk.Label(outer, textvariable=self.status).grid(row=6, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Label(outer, text="分析依赖：读取入口引用、构建参数和指定 Python 的模块；不会执行构建。发现页只列问题，依赖对照页列全部第三方模块。编译报告可核对模块收集；下载、DLL 与数据文件仍需运行验证。",
                  wraplength=1050).grid(row=7, column=0, columnspan=3, sticky="w", pady=(0, 6))
        notebook = ttk.Notebook(outer)
        notebook.grid(row=8, column=0, columnspan=3, sticky="nsew")
        outer.rowconfigure(8, weight=1)

        frame = ttk.Frame(notebook, padding=5)
        notebook.add(frame, text="发现")
        table_frame = ttk.Frame(frame)
        table_frame.pack(fill="both", expand=True)
        self.findings = ttk.Treeview(table_frame, columns=("level", "subject", "reason", "flag"), show="headings")
        for name, label, width in (("level", "等级", 75), ("subject", "对象", 140),
                                   ("reason", "说明", 590), ("flag", "建议参数", 240)):
            self.findings.heading(name, text=label)
            self.findings.column(name, width=width, stretch=(name == "reason"))
        self.findings.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.findings.yview)
        scroll.pack(side="right", fill="y")
        self.findings.configure(yscrollcommand=scroll.set)
        ttk.Label(frame, text="选中一项查看完整说明：").pack(anchor="w", pady=(6, 2))
        self.finding_detail = tk.Text(frame, height=4, wrap="word")
        self.finding_detail.pack(fill="x")
        self.finding_detail.configure(state="disabled")
        self.findings.bind("<<TreeviewSelect>>", self._show_finding)

        coverage_frame = ttk.Frame(notebook, padding=5)
        notebook.add(coverage_frame, text="依赖对照")
        self.coverage = ttk.Treeview(coverage_frame, columns=("module", "inclusion", "environment", "report", "sources"), show="headings")
        for name, label, width in (("module", "模块", 130), ("inclusion", "构建方式", 150),
                                   ("environment", "Python 环境", 100), ("report", "编译报告", 90),
                                   ("sources", "引用文件", 530)):
            self.coverage.heading(name, text=label)
            self.coverage.column(name, width=width, stretch=(name == "sources"))
        self.coverage.pack(side="left", fill="both", expand=True)
        coverage_scroll = ttk.Scrollbar(coverage_frame, orient="vertical", command=self.coverage.yview)
        coverage_scroll.pack(side="right", fill="y")
        self.coverage.configure(yscrollcommand=coverage_scroll.set)

        self.details = tk.Text(notebook, wrap="word", font=("Consolas", 10))
        notebook.add(self.details, text="完整参数与引用")
        self.details.configure(state="disabled")

    def _browse(self, variable: tk.StringVar, kind: str):
        if kind == "directory":
            selected = filedialog.askdirectory(title="选择项目根目录")
        else:
            selected = filedialog.askopenfilename(title="选择构建脚本" if kind == "build" else "选择入口文件",
                filetypes=[("Python / Batch", "*.py *.bat *.cmd *.exe"), ("所有文件", "*.*")])
        if selected:
            variable.set(selected)
            if kind == "build" and not self.project.get():
                self.project.set(str(Path(selected).parent))
            if kind == "entry" and not self.project.get():
                self.project.set(str(Path(selected).parent))

    def _show_finding(self, _event=None):
        selected = self.findings.selection()
        if not selected:
            return
        values = self.findings.item(selected[0], "values")
        message = f"{values[1]}：{values[2]}"
        if values[3]:
            message += f"\n建议参数：{values[3]}"
        self.finding_detail.configure(state="normal")
        self.finding_detail.delete("1.0", "end")
        self.finding_detail.insert("end", message)
        self.finding_detail.configure(state="disabled")

    def analyze(self):
        try:
            if not self.build.get().strip():
                raise ValueError("请选择构建脚本")
            project = Path(self.project.get()).resolve() if self.project.get().strip() else None
            entry = Path(self.entry.get()).resolve() if self.entry.get().strip() else None
            interpreter = Path(self.interpreter.get()) if self.interpreter.get().strip() else None
            report = Path(self.report.get()) if self.report.get().strip() else None
            self.result = audit(Path(self.build.get()), entry, project, interpreter, report)
        except (OSError, ValueError, SyntaxError) as exc:
            messagebox.showerror("分析失败", str(exc))
            return
        try:
            self._save_settings()
        except OSError:
            pass
        self.findings.delete(*self.findings.get_children())
        self.finding_detail.configure(state="normal")
        self.finding_detail.delete("1.0", "end")
        self.finding_detail.configure(state="disabled")
        self.coverage.delete(*self.coverage.get_children())
        labels = {"error": "错误", "review": "待核对", "info": "提示"}
        for item in self.result["findings"]:
            self.findings.insert("", "end", values=(labels.get(item["level"], item["level"]), item["subject"],
                                                   item["message"], item["suggestion"]))
        for item in self.result["coverage"]:
            self.coverage.insert("", "end", values=(item["module"], item["inclusion"],
                                                  item["environment"], item["report"],
                                                  ", ".join(item["sources"])))
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("end", render_text(self.result))
        self.details.configure(state="disabled")
        summary = self.result["summary"]
        self.status.set(f"完成：入口可达 {summary['reachable_files']} 个文件，第三方模块 {summary['external_packages']} 个，待处理 {summary['findings']} 项；编译报告：{self.result['report_status']}。")

    def save(self):
        if self.result is None:
            self.analyze()
        if self.result is None:
            return
        filename = filedialog.asksaveasfilename(title="保存报告", defaultextension=".json",
                                               initialfile="nuitka_audit.json",
                                               filetypes=[("JSON 报告", "*.json")])
        if not filename:
            return
        stem = Path(filename).with_suffix("")
        stem.with_suffix(".json").write_text(json.dumps(self.result, ensure_ascii=False, indent=2), encoding="utf-8")
        stem.with_suffix(".html").write_text(render_html(self.result), encoding="utf-8")
        self.status.set(f"已保存 {stem.with_suffix('.json')} 和 {stem.with_suffix('.html')}")

    def copy_suggestions(self):
        if self.result is None:
            self.analyze()
        if self.result is None:
            return
        flags = list(dict.fromkeys(item["suggestion"] for item in self.result["findings"]
                                  if item["suggestion"] and item["level"] != "info"))
        if not flags:
            self.status.set(f"没有需要添加的参数；编译报告状态：{self.result['report_status']}。请查看“发现”与“依赖对照”。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(flags))
        self.status.set(f"已复制 {len(flags)} 条待审查参数；编译报告状态：{self.result['report_status']}。")


if __name__ == "__main__":
    window = tk.Tk()
    AuditApp(window)
    window.mainloop()
