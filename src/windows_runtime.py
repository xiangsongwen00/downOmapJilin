"""Process-local compatibility setup for portable Windows builds."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import platform
import sys
import types


_DLL_HANDLES = []
_COM_READY = False


def configure_environment(root: Path, packaged: bool):
    if not packaged:
        return
    # Do not load another GIS installation's plugins or data (ArcGIS/QGIS/GDAL).
    os.environ["GDAL_DRIVER_PATH"] = "disable"
    os.environ["OGR_DRIVER_PATH"] = "disable"
    for name in ("GDAL_DATA", "GDAL_PYTHON_DRIVER_PATH", "PROJ_LIB", "PROJ_DATA",
                 "PROJ_AUX_DB", "GEOTIFF_CSV"):
        os.environ.pop(name, None)
    for candidate in (root / "pyogrio/gdal_data", root / "rasterio/gdal_data"):
        if candidate.is_dir():
            os.environ["GDAL_DATA"] = str(candidate)
            break
    proj = root / "pyproj/proj_dir/share/proj"
    if proj.is_dir():
        os.environ["PROJ_LIB"] = os.environ["PROJ_DATA"] = str(proj)
    directories = [root / name for name in ("pyogrio.libs", "rasterio.libs", "pyproj.libs")]
    directories = [path for path in directories if path.is_dir()]
    os.environ["PATH"] = os.pathsep.join([*(str(path) for path in directories),
                                           os.environ.get("PATH", "")])
    if os.name == "nt":
        _DLL_HANDLES.extend(os.add_dll_directory(str(path)) for path in directories)


def configure_console():
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def configure_dpi():
    """Set awareness before Tk/pywinauto create windows or read coordinates."""
    if os.name != "nt":
        return "not-windows"
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_int
        if set_context(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
        if ctypes.get_last_error() == 5:
            return "already-configured"
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except (AttributeError, OSError):
        return "unavailable"


def prepare_comtypes(packaged=None):
    """Generate target-machine COM wrappers in memory, never in the EXE folder.

    Build flags must exclude comtypes.gen.* so Nuitka cannot import wrappers
    compiled on the build machine. Keep comtypes' own version checks enabled.
    """
    global _COM_READY
    if packaged is None:
        packaged = "__compiled__" in globals()
    if not packaged or _COM_READY:
        return
    import comtypes
    if "comtypes.client" in sys.modules:
        raise RuntimeError("COM compatibility setup must run before importing comtypes.client")
    gen = types.ModuleType("comtypes.gen")
    # _generate._create_module needs one path even in its memory-only branch.
    gen.__path__ = [str(Path(comtypes.__file__).parent / "gen")]
    gen.__package__ = "comtypes.gen"
    sys.modules["comtypes.gen"] = gen
    comtypes.gen = gen
    import comtypes.client
    comtypes.client.gen_dir = None
    _COM_READY = True


def diagnose(root: Path, packaged: bool):
    """Exercise bundled GIS and COM imports without touching Ovi or user output."""
    result = {"windows": platform.platform(), "architecture": platform.machine(),
              "python": platform.python_version(), "packaged": packaged,
              "root": str(root), "checks": {}}
    checks = result["checks"]
    for name in ("GDAL_DRIVER_PATH", "GDAL_DATA", "PROJ_DATA"):
        result[name] = os.environ.get(name)
    try:
        import pyproj
        import pyogrio
        import rasterio
        import numpy as np
        from rasterio.io import MemoryFile
        from rasterio.transform import Affine
        pyproj.Transformer.from_crs(4326, 3857, always_xy=True).transform(120, 30, errcheck=True)
        drivers = pyogrio.list_drivers()
        if not {"GPKG", "GeoJSON", "ESRI Shapefile"}.issubset(drivers):
            raise RuntimeError("Required vector drivers are missing")
        with MemoryFile() as memory:
            with memory.open(driver="GTiff", width=2, height=2, count=4, dtype="uint8",
                             crs="EPSG:3857", transform=Affine(0.597, 0, 10, 0, -0.597, 10)) as raster:
                raster.write(np.full((4, 2, 2), 255, dtype="uint8"))
            with memory.open() as raster:
                assert raster.read(4).min() == 255
        checks["gis"] = {"ok": True, "gdal": rasterio.__gdal_version__}
    except Exception as exc:
        checks["gis"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        prepare_comtypes(packaged)
        import comtypes.client
        module = comtypes.client.GetModule("UIAutomationCore.dll")
        if not hasattr(module, "IUIAutomation"):
            raise RuntimeError("IUIAutomation interface missing")
        # Also verify pywin32 native extensions and pywinauto's import chain.
        import pywinauto
        checks["automation"] = {"ok": True, "comtypes": comtypes.__version__,
                                 "memory_wrappers": comtypes.client.gen_dir is None}
    except Exception as exc:
        checks["automation"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if all(check["ok"] for check in checks.values()) else 1
