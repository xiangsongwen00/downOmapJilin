# Nuitka 依赖与打包审计

独立工具，读取 `.bat`、`.cmd`、`.py` 构建脚本和 Python 入口文件，**不执行构建脚本，也不执行目标项目代码**。可在图形界面查看发现，或通过命令行生成 JSON、HTML 报告。

## 启动

在装有 Python 3.10+ 的环境中运行：

```powershell
python 0-checkbuildpy/app.py
```

界面中选择构建脚本、入口文件与项目根目录。可选填构建所用 Python（检查该环境是否安装引用模块）和已有的 `compilation-report.xml`（对照 Nuitka 实际收集的模块）。点击“分析依赖”，在“发现”与“完整参数与引用”中查看结果。路径设置保存在 `app_settings.json`。点击“保存 JSON + HTML 报告”可留档。

命令行示例：

```powershell
python 0-checkbuildpy/src/checkbuildpy.py --build build_msvc.bat --entry index.py --root . --python D:\App\python_env\gis312\python.exe --output 0-checkbuildpy\audit
python 0-checkbuildpy/src/checkbuildpy.py --build D:\seg_app\runbuild_Nuitka_msvc.py --entry D:\seg_app\app.py --root D:\seg_app
```

`--build` 支持 BAT/CMD 的 `^` 续行、Python 中的 Nuitka 参数列表、常量与常见 f-string，以及调用相邻 BAT 的 Python 包装脚本。Python 表达式无法安全求值时保留 `{表达式}`，不会执行代码。多个 Nuitka 命令时选择最长参数列表，并在 JSON 中记录候选数量。

## 判断方式

- 从入口文件跟踪静态 import 和字面量动态 import，再将 `--include-package` 指定的本地包全部计入构建依赖；区分入口可达、参数额外收集、项目其余脚本。
- 比对 `--include-package`、`--include-module`、`--include-package-data` 等参数。常规静态导入在 Nuitka standalone 模式下通常自动跟随；“可能冗余”只表示值得审查，**不要未经运行验证直接删除**。
- 对 Rasterio、Pyogrio、GeoPandas、Fiona、pywinauto/comtypes 等动态加载场景给出风险提示。包代码、数据文件、原生 DLL 是不同问题；`--include-package-data` 不包含 DLL。
- 提供编译报告时，检查静态依赖和 `rasterio.serde`、`comtypes.stream` 是否真实收集。没有编译报告时只能作静态预判，不能保证运行时完整性。

建议每次构建都加 `--report=.../compilation-report.xml`，构建后把报告载入工具，再运行一次打包程序的关键功能。

## 测试

```powershell
python -m unittest discover -s 0-checkbuildpy/tests -v
```
