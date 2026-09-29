"""Static Nuitka build audit. Never executes the build script or project code."""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict, deque
import html
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any
import xml.etree.ElementTree as ET


SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "build", "dist", "release", "node_modules"}
GIS_PACKAGES = {"rasterio", "pyogrio", "fiona", "pyproj", "geopandas", "shapely", "osgeo"}
# These are review rules, not claims that Nuitka always requires an explicit flag.
DYNAMIC_RULES = {
    "rasterio": ("rasterio.serde", "Rasterio loads internal modules dynamically"),
    "pyogrio": ("pyogrio", "GeoPandas may select the Pyogrio engine at runtime"),
    "fiona": ("fiona", "GeoPandas may select the Fiona engine at runtime"),
    "comtypes": ("comtypes.stream", "pywinauto UIA loads COM modules dynamically"),
    "pywinauto": ("pywinauto", "UIA backends select modules at runtime"),
}
IMPORT_ALIASES = {"win32con": "pywin32", "win32gui": "pywin32", "win32api": "pywin32",
                  "pythoncom": "pywin32", "PIL": "Pillow", "cv2": "opencv-python", "osgeo": "GDAL"}
VALUE_OPTIONS = {"--include-package", "--include-module", "--include-package-data",
                 "--include-data-files", "--include-data-dir", "--user-package-configuration-file",
                 "--output-dir", "--output-filename", "--report", "--enable-plugin",
                 "--windows-console-mode", "--jobs", "--msvc", "--mode", "--module-parameter",
                 "--nofollow-import-to", "--python-flag"}


def _read(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            pass
    return path.read_text(encoding="utf-8", errors="replace")


def _simple_value(node: ast.AST, names: dict[str, str]) -> str:
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.Name):
        return names.get(node.id, "{" + node.id + "}")
    if isinstance(node, ast.JoinedStr):
        return "".join(_simple_value(part, names) for part in node.values)
    if isinstance(node, ast.FormattedValue):
        return _simple_value(node.value, names)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _simple_value(node.left, names).rstrip("/\\") + "/" + _simple_value(node.right, names).lstrip("/\\")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _simple_value(node.left, names) + _simple_value(node.right, names)
    if isinstance(node, ast.Call) and len(node.args) == 1 and isinstance(node.func, ast.Name) and node.func.id in {"str", "Path"}:
        return _simple_value(node.args[0], names)
    return "{" + ast.unparse(node) + "}"


def _py_commands(path: Path) -> list[list[str]]:
    tree = ast.parse(_read(path), filename=str(path))
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, (ast.Constant, ast.JoinedStr, ast.BinOp)):
                names[node.targets[0].id] = _simple_value(node.value, names)
    commands = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        args = [_simple_value(item, names) for item in node.elts]
        if any(arg.lower() == "nuitka" for arg in args) and any(arg.startswith("--") for arg in args):
            commands.append(args)
    return commands


def _bat_commands(path: Path) -> list[list[str]]:
    lines = _read(path).splitlines()
    logical: list[str] = []
    current = ""
    for line in lines:
        part = line.strip()
        if not part or part.lower().startswith(("rem ", "::", "@echo")):
            continue
        continuing = part.rstrip().endswith("^")
        current += part.rstrip().rstrip("^").strip() + " "
        if not continuing:
            logical.append(current.strip())
            current = ""
    if current:
        logical.append(current.strip())
    commands = []
    for line in logical:
        if re.search(r"(?:^|\s)-m\s+nuitka(?:\s|$)", line, re.I) or re.search(r"(?:^|\s)nuitka(?:\.exe)?\s+--", line, re.I):
            # shlex with posix=False keeps Windows backslashes intact.
            commands.append([word.strip('"') for word in shlex.split(line, posix=False)])
    return commands


def parse_build(path: Path, seen: set[Path] | None = None) -> dict[str, Any]:
    path = path.resolve()
    if seen is None:
        seen = set()
    if path in seen:
        raise ValueError(f"Build wrapper cycle: {path}")
    seen.add(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    commands = _bat_commands(path) if path.suffix.lower() in {".bat", ".cmd"} else _py_commands(path)
    if not commands and path.suffix.lower() == ".py":
        # A small Python wrapper may invoke an adjacent .bat file.
        tree = ast.parse(_read(path), filename=str(path))
        candidates = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.lower().endswith((".bat", ".cmd")):
                candidates.append(path.parent / node.value)
        for candidate in candidates:
            if candidate.is_file():
                result = parse_build(candidate, seen)
                result["wrapper"] = str(path)
                return result
    if not commands:
        raise ValueError(f"No Nuitka argument list found in {path}")
    if len(commands) > 1:
        # Prefer the longest command; retain ambiguity in the report.
        commands.sort(key=len, reverse=True)
    args = commands[0]
    opts: dict[str, list[str]] = defaultdict(list)
    entry = ""
    consumed = set()
    for i, token in enumerate(args):
        if i in consumed:
            continue
        if token.startswith("--"):
            name, separator, value = token.partition("=")
            if not separator and name in VALUE_OPTIONS and i + 1 < len(args) and not args[i + 1].startswith("-"):
                value = args[i + 1]
                consumed.add(i + 1)
            opts[name].append(value)
        elif token.lower().endswith(".py") or re.search(r"\.py[}'\"]*$", token.lower()):
            entry = token
    return {"script": str(path), "arguments": args, "options": dict(opts),
            "entry": entry, "command_count": len(commands)}


def _entry_path(raw: str, root: Path) -> Path | None:
    if not raw:
        return None
    normalized = raw.replace("\\", "/")
    match = re.search(r"([\w./-]+\.py)", normalized)
    if not match:
        return None
    candidate = Path(match.group(1))
    if candidate.is_absolute() and candidate.is_file():
        return candidate.resolve()
    for choice in (root / candidate, root / candidate.name):
        if choice.is_file():
            return choice.resolve()
    return None


def _local_module(root: Path, dotted: str) -> Path | None:
    if not dotted:
        return None
    parts = dotted.split(".")
    if not all(re.fullmatch(r"[A-Za-z_]\w*", p) for p in parts):
        return None
    rel = Path(*parts)
    for candidate in (root / rel.with_suffix(".py"), root / rel / "__init__.py"):
        if candidate.is_file():
            return candidate.resolve()
    return None


def _local_namespace(root: Path, dotted: str) -> bool:
    directory = root.joinpath(*dotted.split("."))
    return directory.is_dir() and any(directory.glob("*.py"))


def scan_imports(root: Path, entry: Path | list[Path]) -> dict[str, Any]:
    queue = deque([p.resolve() for p in entry] if isinstance(entry, list) else [entry.resolve()])
    seen: set[Path] = set()
    external: dict[str, set[str]] = defaultdict(set)
    dynamic: dict[str, set[str]] = defaultdict(set)
    local_edges: dict[str, set[str]] = defaultdict(set)
    parse_errors = []
    while queue:
        file = queue.popleft()
        if file in seen or not file.is_file() or not file.is_relative_to(root):
            continue
        if any(part in SKIP_DIRS for part in file.relative_to(root).parts[:-1]):
            continue
        seen.add(file)
        rel = file.relative_to(root).as_posix()
        try:
            tree = ast.parse(_read(file), filename=str(file))
        except SyntaxError as exc:
            parse_errors.append(f"{rel}:{exc.lineno}: {exc.msg}")
            continue
        package = list(file.relative_to(root).with_suffix("").parts)
        if package[-1] != "__init__":
            package.pop()
        else:
            package.pop()
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    prefix = package[:max(0, len(package) - node.level + 1)]
                    base = ".".join([*prefix, *([node.module] if node.module else [])])
                else:
                    base = node.module or ""
                targets = [base]
                targets += [base + "." + alias.name for alias in node.names if alias.name != "*"]
            elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                func = ast.unparse(node.func)
                if func in {"importlib.import_module", "__import__"}:
                    targets = [node.args[0].value]
                    dynamic[targets[0].split(".")[0]].add(rel)
            for target in targets:
                if not target:
                    continue
                local = _local_module(root, target)
                if local:
                    queue.append(local)
                    local_edges[rel].add(local.relative_to(root).as_posix())
                else:
                    top = target.split(".")[0]
                    if _local_module(root, top) or _local_namespace(root, top):
                        continue
                    external[top].add(rel)
    return {"files": sorted(str(p.relative_to(root)).replace("\\", "/") for p in seen),
            "external": {k: sorted(v) for k, v in sorted(external.items())},
            "dynamic": {k: sorted(v) for k, v in sorted(dynamic.items())},
            "local_edges": {k: sorted(v) for k, v in sorted(local_edges.items())},
            "parse_errors": parse_errors}


def _included(options: dict[str, list[str]], name: str) -> set[str]:
    return {value.split(":", 1)[0] for value in options.get(name, []) if value}


def _probe_interpreter(interpreter: Path, packages: set[str]) -> dict[str, bool]:
    """Query installed top-level modules without importing the target project."""
    script = ("import importlib.util,json,sys; "
              "print(json.dumps({name: importlib.util.find_spec(name) is not None "
              "for name in json.loads(sys.argv[1])}))")
    result = subprocess.run([str(interpreter), "-c", script, json.dumps(sorted(packages))],
                            capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def _report_modules(path: Path) -> set[str]:
    modules = set()
    for _, element in ET.iterparse(path, events=("end",)):
        if element.tag == "module" and element.get("name"):
            modules.add(element.get("name"))
        element.clear()
    return modules


def _project_files(root: Path) -> list[Path]:
    files = []
    for directory, subdirs, names in os.walk(root):
        subdirs[:] = [name for name in subdirs if name not in SKIP_DIRS and name != "0-checkbuildpy"]
        files.extend(Path(directory) / name for name in names if name.endswith(".py"))
    return files


def audit(build: Path, entry: Path | None = None, root: Path | None = None,
          interpreter: Path | None = None, report_path: Path | None = None) -> dict[str, Any]:
    parsed = parse_build(build)
    root = (root or build.parent).resolve()
    entry = (entry or _entry_path(parsed["entry"], root))
    if entry is None or not entry.is_file():
        raise FileNotFoundError("Cannot resolve entry point; pass --entry index.py/app.py/main.py")
    entry = entry.resolve()
    if not entry.is_relative_to(root):
        raise ValueError("Entry point must be inside project root")
    project_files = _project_files(root)
    opts = parsed["options"]
    packages = _included(opts, "--include-package")
    modules = _included(opts, "--include-module")
    data = _included(opts, "--include-package-data")
    entry_imports = scan_imports(root, entry)
    entry_seen = set(entry_imports["external"])
    explicit_local = [p for p in project_files if any(
        p.relative_to(root).as_posix().startswith(package.replace(".", "/") + "/")
        for package in packages if _local_namespace(root, package) or _local_module(root, package))]
    imports = scan_imports(root, [entry, *explicit_local])
    project_inventory = scan_imports(root, project_files)
    seen = set(imports["external"])
    findings = []

    def add(level: str, subject: str, message: str, suggestion: str = "", files: list[str] | None = None):
        findings.append({"level": level, "subject": subject, "message": message,
                         "suggestion": suggestion, "files": files or []})

    if not ("--standalone" in opts or "--onefile" in opts or "--mode" in opts):
        add("error", "Nuitka mode", "No standalone or onefile option found.", "--standalone")
    if parsed["command_count"] > 1:
        add("review", "Multiple Nuitka commands", f"Found {parsed['command_count']} candidate commands; audited the longest one.")
    if parsed["entry"] and Path(parsed["entry"].replace("\\", "/")).name != entry.name:
        add("error", "Entry mismatch", f"Build command names {parsed['entry']}, but selected entry is {entry.name}.")
    for option in ("--include-data-files", "--include-data-dir", "--user-package-configuration-file"):
        for value in opts.get(option, []):
            source = value.split("=", 1)[0]
            if not source or any(marker in source for marker in ("{", "}", "%", "*", "?")):
                continue
            candidate = Path(source)
            if not candidate.is_absolute():
                candidate = root / candidate
            if not candidate.exists():
                add("error", source, f"Input for {option} does not exist: {candidate}")
    if "--report" not in opts:
        add("review", "编译报告", "构建命令未配置 --report，无法核对 EXE 实际收集的模块。",
            "--report=compilation-report.xml")
    for package in sorted(seen):
        if package in sys.stdlib_module_names:
            continue
        covered = any(package == item or item.startswith(package + ".") or package.startswith(item + ".")
                      for item in packages | modules)
        files = imports["external"][package]
        if package == "geopandas" and "pyogrio" not in packages and "pyogrio" not in modules:
            add("review", "pyogrio", "GeoPandas may load the Pyogrio engine without a direct import.",
                "--include-package=pyogrio", files)
        if package == "pywinauto" and "comtypes" not in packages and "comtypes.stream" not in modules:
            add("review", "comtypes", "pywinauto UIA may load comtypes.stream dynamically.",
                "--include-package=comtypes", files)
        if package in DYNAMIC_RULES and not covered:
            module, reason = DYNAMIC_RULES[package]
            flag = f"--include-module={module}" if "." in module else f"--include-package={package}"
            add("review", package, reason + "; confirm with a packaged runtime test.", flag, files)
    for package in sorted(packages):
        if _local_namespace(root, package) or _local_module(root, package):
            extra = sorted(set(imports["files"]) - set(entry_imports["files"]))
            extra = [file for file in extra if file.startswith(package.replace(".", "/") + "/")]
            add("review", package, f"Whole local package adds {len(extra)} files beyond the entry import graph; they are included in dependency analysis.",
                files=extra)
            continue
        if package == "pyogrio" and "geopandas" in seen:
            continue
        if package == "comtypes" and "pywinauto" in seen:
            continue
        if package not in seen and package not in imports["dynamic"]:
            # A package may be imported by a third-party package or chosen dynamically.
            add("review", package, "No direct import found in reachable project files. Check the compilation report and runtime use before removing this flag.",
                files=[])
        elif package not in DYNAMIC_RULES and package not in GIS_PACKAGES:
            add("review", package, "Direct imports are normally followed in standalone mode. This broad include may add unused modules; verify before removal.")
    if "geopandas" in seen and "pyogrio" not in packages and "fiona" not in packages:
        add("review", "GIS engine", "Neither GeoPandas file IO engine is explicitly included. Verify the selected engine in the build environment.",
            "--include-package=pyogrio")
    if imports["parse_errors"]:
        add("error", "Source parsing", "Some reachable source files could not be parsed: " + "; ".join(imports["parse_errors"]))
    dormant = sorted(set(project_inventory["external"]) - seen - sys.stdlib_module_names)
    availability = None
    if interpreter is not None:
        interpreter = interpreter.resolve()
        if not interpreter.is_file():
            raise FileNotFoundError(interpreter)
        requested = {p for p in seen | packages if p not in sys.stdlib_module_names and
                     not _local_namespace(root, p) and not _local_module(root, p)}
        requested |= {"pyogrio"} if "geopandas" in seen else set()
        requested |= {"comtypes"} if "pywinauto" in seen else set()
        try:
            availability = _probe_interpreter(interpreter, requested)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            add("error", "Python environment", f"Could not inspect {interpreter}: {exc}")
        else:
            for package, found in sorted(availability.items()):
                if not found:
                    findings[:] = [item for item in findings if not (
                        item["level"] == "info" and item["subject"] == package)]
                    if package in entry_seen:
                        add("error", package, f"Module is imported by entry-reachable code but absent from selected build interpreter: {interpreter}",
                            files=entry_imports["external"][package])
                    elif package in seen:
                        add("review", package, f"Module is absent from selected build interpreter: {interpreter}. "
                            "It is referenced only by files brought in through broad inclusion; Nuitka may still build, but importing those files can fail.",
                            files=imports["external"][package])
                    else:
                        add("review", package, f"Module is absent from selected build interpreter: {interpreter}")
    report_modules = None
    report_status = "未配置"
    if report_path is None and opts.get("--report"):
        configured = opts["--report"][0]
        if configured and not any(char in configured for char in ("{", "}", "%")):
            configured_path = Path(configured)
            report_path = configured_path if configured_path.is_absolute() else root / configured_path
        else:
            report_status = "报告路径包含未解析表达式"
            add("review", "编译报告", "构建参数中的报告路径含动态表达式，需手动指定实际 XML 文件。")
    if report_path is not None:
        report_path = report_path.resolve()
        if not report_path.is_file():
            report_status = "等待构建"
            add("review", "编译报告", f"尚未生成 {report_path}；目前只能做静态预检，不能证明打包内容完整。")
        else:
            latest_input = max((p.stat().st_mtime for p in [Path(parsed["script"]), entry,
                                 *[root / f for f in imports["files"]]]
                                if p.is_file()), default=0)
            if report_path.stat().st_mtime < latest_input:
                report_status = "已过期"
                add("review", "编译报告", "报告早于构建脚本或项目源码；需重新打包后再核对。")
            else:
                report_status = "已核对"
                report_modules = _report_modules(report_path)
    if report_modules is not None:
        if not report_modules:
            report_status = "无法解析"
            add("error", "编译报告", "XML 中没有可识别的 module 条目，无法确认打包内容。")
        else:
            for name in sorted(seen - sys.stdlib_module_names):
                if name not in report_modules and not any(module.startswith(name + ".") for module in report_modules):
                    add("error", name, "Direct dependency is absent from the Nuitka compilation report.", files=imports["external"][name])
            for name, trigger in (("rasterio.serde", "rasterio"), ("comtypes.stream", "pywinauto")):
                if trigger in seen and name not in report_modules:
                    add("error", name, "Known runtime-loaded module is absent from the Nuitka compilation report.",
                        f"--include-module={name}")
    coverage = []
    inferred = set()
    if "geopandas" in seen:
        inferred.add("pyogrio")
    if "pywinauto" in seen:
        inferred.add("comtypes")
    for name in sorted((seen - sys.stdlib_module_names) | (packages & inferred)):
        explicitly_included = any(name == value or name.startswith(value + ".")
                                  for value in packages | modules)
        coverage.append({"module": name,
                         "sources": imports["external"].get(name, []),
                         "inclusion": "显式收集" if explicitly_included else "静态导入自动跟随" if name in seen else "间接依赖",
                         "environment": "已安装" if availability and availability.get(name) else
                                        "缺失" if availability and availability.get(name) is False else "未检查",
                         "report": "已收集" if report_modules is not None and
                                   any(module == name or module.startswith(name + ".") for module in report_modules)
                                   else "未收集" if report_modules is not None else "未核对"})
    findings.sort(key=lambda item: {"error": 0, "review": 1, "info": 2}.get(item["level"], 3))
    return {"build": parsed, "project_root": str(root), "entry": str(entry),
            "imports": imports, "entry_imports": entry_imports, "findings": findings,
            "coverage": coverage, "report_status": report_status,
            "interpreter": str(interpreter) if interpreter else None,
            "availability": availability, "report": str(report_path) if report_path else None,
            "report_module_count": len(report_modules) if report_modules is not None else None,
            "project_inventory": {"files": len(project_inventory["files"]),
                                  "external": project_inventory["external"], "unreachable": dormant},
            "summary": {"reachable_files": len(entry_imports["files"]),
                        "packaged_files": len(imports["files"]),
                        "external_packages": len(seen - sys.stdlib_module_names),
                        "explicit_packages": len(packages), "findings": len(findings)}}


def render_text(result: dict[str, Any]) -> str:
    build = result["build"]
    lines = [f"Build: {build['script']}", f"Entry: {result['entry']}",
             f"Entry-reachable files: {result['summary']['reachable_files']}",
             f"Project files selected for build: {result['summary']['packaged_files']}",
             f"Project Python files: {result['project_inventory']['files']}",
             "\nNuitka arguments:", "  " + " ".join(build["arguments"]),
             "\nExternal imports:"]
    lines += [f"  {name}: {', '.join(files)}" for name, files in result["imports"]["external"].items()
              if name not in sys.stdlib_module_names]
    lines.append("\nImports outside build graph:")
    lines += [f"  {name}: {', '.join(result['project_inventory']['external'][name])}"
              for name in result["project_inventory"]["unreachable"]]
    lines.append("\nFindings:")
    for item in result["findings"]:
        lines.append(f"  [{item['level'].upper()}] {item['subject']}: {item['message']}")
        if item["suggestion"]:
            lines.append(f"    Suggested: {item['suggestion']}")
    return "\n".join(lines) + "\n"


def render_html(result: dict[str, Any]) -> str:
    rows = "".join("<tr><td>" + html.escape(f["level"]) + "</td><td>" + html.escape(f["subject"])
                   + "</td><td>" + html.escape(f["message"]) + "</td><td><code>"
                   + html.escape(f["suggestion"]) + "</code></td></tr>" for f in result["findings"])
    return ("<!doctype html><meta charset='utf-8'><title>Nuitka audit</title>"
            "<style>body{font:15px system-ui;max-width:1100px;margin:40px auto;color:#243042}"
            "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd3dc;padding:8px;text-align:left}"
            "th{background:#e9eef6}pre{white-space:pre-wrap;background:#f4f6f8;padding:15px}</style>"
            "<h1>Nuitka dependency audit</h1><p>Static review; verify findings against a packaged runtime test.</p>"
            "<h2>Build command</h2><pre>" + html.escape(" ".join(result["build"]["arguments"])) + "</pre>"
            "<h2>Findings</h2><table><tr><th>Level</th><th>Package</th><th>Reason</th><th>Suggestion</th></tr>"
            + rows + "</table><h2>External imports</h2><pre>" +
            html.escape("\n".join(f"{k}: {', '.join(v)}" for k, v in result["imports"]["external"].items()
                                  if k not in sys.stdlib_module_names)) + "</pre><h2>Outside build graph</h2><pre>" +
            html.escape("\n".join(f"{k}: {', '.join(result['project_inventory']['external'][k])}"
                                  for k in result["project_inventory"]["unreachable"])) + "</pre>")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit a Nuitka .bat/.cmd/.py build without executing it")
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--entry", type=Path, help="Explicit index.py/app.py/main.py")
    parser.add_argument("--root", type=Path, help="Project source root (default: build script directory)")
    parser.add_argument("--python", type=Path, help="Build Python interpreter; check external modules installed there")
    parser.add_argument("--report", type=Path, help="Existing Nuitka compilation-report.xml for actual inclusion checks")
    parser.add_argument("--output", type=Path, help="Save JSON and HTML beside this path stem")
    args = parser.parse_args(argv)
    result = audit(args.build, args.entry, args.root, args.python, args.report)
    print(render_text(result))
    if args.output:
        stem = args.output.with_suffix("")
        stem.parent.mkdir(parents=True, exist_ok=True)
        stem.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        stem.with_suffix(".html").write_text(render_html(result), encoding="utf-8")
        print(f"Saved: {stem.with_suffix('.json')} and {stem.with_suffix('.html')}")
    return 1 if any(item["level"] == "error" for item in result["findings"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
