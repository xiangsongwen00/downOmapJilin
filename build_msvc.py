"""Run the MSVC/Conda Nuitka build from Python when preferred."""

from pathlib import Path
import subprocess
import sys


def main():
    if sys.platform != "win32":
        raise SystemExit("This build requires Windows and Visual Studio BuildTools")
    root = Path(__file__).resolve().parent
    return subprocess.call(["cmd", "/d", "/c", str(root / "build_msvc.bat")], cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
