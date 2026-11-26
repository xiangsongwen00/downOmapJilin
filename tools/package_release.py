"""Create a complete first-install archive, including portable initial settings."""

import argparse
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent.parent


def package(source, destination):
    if not (source / "jinlinOmap.exe").is_file():
        raise FileNotFoundError(source / "jinlinOmap.exe")
    defaults = (ROOT / "config.default.json").read_text(encoding="utf-8")
    json.loads(defaults)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for file in sorted(source.rglob("*")):
            if not file.is_file():
                continue
            relative = file.relative_to(source)
            if relative == Path("config.json") or relative.parts[0] in {"out", "out2", "__pycache__"}:
                continue
            archive.write(file, relative.as_posix())
        archive.writestr("config.json", defaults)
        archive.writestr("使用说明.txt", "完整安装包：解压后双击 jinlinOmap.exe 打开配置窗口。\r\n"
                         "首次使用请填写奥维程序、原始要素等路径并选择底图。\r\n"
                         "升级已有安装时请先保留原 config.json、out 和 out2，再覆盖程序文件。\r\n"
                         "--diagnose 仅检查 GIS/COM 组件，不能代替正常界面启动验证。\r\n")
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Release archive CRC validation failed")
        assert "config.json" in archive.namelist()
        assert "jinlinOmap.exe" in archive.namelist()
    temporary.replace(destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "build/msvc/staging/index.dist")
    parser.add_argument("--output", type=Path, default=ROOT / "build/msvc/jinlinOmap-Win11-x64-full.zip")
    args = parser.parse_args()
    print(package(args.source.resolve(), args.output.resolve()))
