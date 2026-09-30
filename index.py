"""Configuration-driven Ovi export and feature mosaic entry point."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace


PACKAGED = "__compiled__" in globals()
EXECUTABLE = Path(sys.argv[0]).resolve() if PACKAGED else Path(sys.executable).resolve()
ROOT = EXECUTABLE.parent if PACKAGED else Path(__file__).resolve().parent
DLL_HANDLES = []
if PACKAGED and os.name == "nt":
    dll_directories = [ROOT / name for name in ("pyogrio.libs", "rasterio.libs")]
    dll_directories = [path for path in dll_directories if path.is_dir()]
    os.environ["PATH"] = os.pathsep.join(
        [*(str(path) for path in dll_directories), os.environ.get("PATH", "")])
    DLL_HANDLES = [os.add_dll_directory(str(path)) for path in dll_directories]
    proj_directory = ROOT / "pyproj" / "proj_dir" / "share" / "proj"
    if proj_directory.is_dir():
        os.environ["PROJ_LIB"] = str(proj_directory)
        os.environ["PROJ_DATA"] = str(proj_directory)


def project_path(value, base=ROOT):
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def single_instance(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    owner_path = Path(str(path) + ".owner.json")
    with path.open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            owner = _lock_owner(owner_path)
            detail = f" (PID {owner['pid']})" if owner and owner.get("pid") else ""
            raise RuntimeError(
                f"Another Ovi export process{detail} is still running. "
                "Wait for it to finish or stop it before resuming. "
                f"Lock: {path}") from exc
        try:
            atomic_json(owner_path, {"pid": os.getpid(), "executable": str(EXECUTABLE),
                                     "started_at": datetime.now().astimezone().isoformat()})
            yield
        finally:
            if (_lock_owner(owner_path) or {}).get("pid") == os.getpid():
                owner_path.unlink(missing_ok=True)
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _lock_owner(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def active_instance(path):
    """Return metadata only while the OS lock is held by another process."""
    if not path.is_file():
        return None
    with path.open("r+b") as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return _lock_owner(Path(str(path) + ".owner.json")) or {"pid": None}
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return None


def resolve_config(config, base=ROOT):
    config = dict(config)
    required = {"omapexepath", "target", "export_dir", "source_features", "mosaic_dir"}
    missing = required - config.keys()
    if missing:
        raise ValueError(f"Missing config fields: {sorted(missing)}")
    if config.get("zoom", 18) != 18 or config.get("format", "tif").lower() != "tif":
        raise ValueError("This workflow requires level 18 and TIF")
    if config.get("coordinate_system", "default") != "default":
        raise ValueError("Coordinate system must remain the Ovi default")
    for key in ("startup_timeout_seconds", "export_timeout_seconds",
                "download_timeout_seconds", "max_attempts"):
        if int(config.get(key, 3)) < 1:
            raise ValueError(f"{key} must be positive")
    for key in required:
        config[key] = project_path(config[key], base)
    if config.get("python") and not PACKAGED:
        config["python"] = project_path(config["python"], base)
    if not config["omapexepath"].is_file():
        raise FileNotFoundError(f"Ovi executable not found: {config['omapexepath']}")
    config["prepared_target"] = project_path(config.get("prepared_target", "out/ovi_import_target.geojson"), base)
    config["state_file"] = project_path(config.get("state_file", "out/ovi_pipeline_state.json"), base)
    config["lock_file"] = project_path(config.get("lock_file", "out/ovi_pipeline.lock"), base)
    return config


def load_config(path):
    return resolve_config(json.loads(path.read_text(encoding="utf-8-sig")), path.resolve().parent)


def ensure_ovi_running(config):
    from tools.ovi_batch_export import main_window, maximize

    try:
        window = main_window()
    except RuntimeError:
        executable = config["omapexepath"]
        if not executable.is_file():
            raise FileNotFoundError(f"Ovi executable not found: {executable}")
        print(f"Starting Ovi: {executable}", flush=True)
        process = subprocess.Popen([str(executable)], cwd=executable.parent)
        deadline = time.monotonic() + int(config.get("startup_timeout_seconds", 120))
        while time.monotonic() < deadline:
            if process.poll() not in (None, 0):
                raise RuntimeError(f"Ovi exited during startup: {process.returncode}")
            try:
                window = main_window()
                break
            except RuntimeError:
                time.sleep(1)
        else:
            raise RuntimeError("Ovi VIP main window did not appear before startup timeout")
    maximize(window)
    return window


def report(config, stage, **details):
    state = {"updated_at": datetime.now().astimezone().isoformat(),
             "stage": stage, **details}
    atomic_json(config["state_file"], state)
    print(f"[{stage}] {details}", flush=True)


def validate_plan(plan):
    data = json.loads(plan.read_text(encoding="utf-8-sig"))
    features = data["features"]
    names = [item["properties"]["name"] for item in features]
    if not names or len(set(names)) != len(names):
        raise ValueError("Target names are empty or repeated")
    if any(item["properties"].get("zoom") != 18 for item in features):
        raise ValueError("Target has a level other than 18")
    return features


def _plan_is_current(path, source_hash, buffer_meters):
    if not path.is_file():
        return False
    features = json.loads(path.read_text(encoding="utf-8-sig")).get("features", [])
    return bool(features) and all(
        float(item.get("properties", {}).get("buffer_meters", -1)) == buffer_meters
        and item["properties"].get("source_sha256") == source_hash
        for item in features
    )


def _seed_compatible_exports(current_plan, output_dir, previous_plans):
    """Reuse only verified TIFs whose target geometry and metadata are unchanged."""
    from shapely.geometry import shape
    from tools.ovi_batch_export import verify_tif

    current_features = json.loads(current_plan.read_text(encoding="utf-8"))["features"]
    current = {item["properties"]["name"]: item for item in current_features}
    plan_hash = hashlib.sha256(current_plan.read_bytes()).hexdigest()
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = output_dir / "ovi_export_ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
    if ledger and ledger.get("plan_sha256") != plan_hash:
        raise RuntimeError(f"New export directory belongs to another target: {output_dir}")
    completed = dict(ledger.get("completed", {}))
    for previous_plan, previous_dir in previous_plans:
        if previous_plan == current_plan or not previous_plan.is_file() or not previous_dir.is_dir():
            continue
        old = json.loads(previous_plan.read_text(encoding="utf-8"))["features"]
        for item in old:
            name = item["properties"]["name"]
            if name not in current or name in completed:
                continue
            new = current[name]
            properties = lambda feature: {key: value for key, value in feature["properties"].items()
                                          if key != "source_sha256"}
            if properties(item) != properties(new) or not shape(item["geometry"]).equals_exact(
                    shape(new["geometry"]), tolerance=1e-12):
                continue
            source_tif = previous_dir / f"{name}.tif"
            if not source_tif.is_file():
                continue
            try:
                result = verify_tif(source_tif, new)
            except (OSError, RuntimeError):
                continue
            destination = output_dir / source_tif.name
            if destination.exists():
                try:
                    result = verify_tif(destination, new)
                except (OSError, RuntimeError):
                    continue
            else:
                temporary = destination.with_suffix(".tif.tmp")
                shutil.copy2(source_tif, temporary)
                os.replace(temporary, destination)
            completed[name] = result
            print(f"Reused verified TIF: {name}", flush=True)
    if completed:
        atomic_json(ledger_path, {"plan_sha256": plan_hash, "completed": completed})
    return len(completed)


def _resolve_buffered_plan(config):
    from src.export_plan import make_plan

    buffer_meters = float(config["plan_buffer_meters"])
    if buffer_meters < 0:
        raise ValueError("plan_buffer_meters must be nonnegative")
    base = config["target"]
    if base.suffix.lower() not in {".geojson", ".json"}:
        raise ValueError("Generated buffered target must be GeoJSON")
    source_hash = hashlib.sha256(config["source_features"].read_bytes()).hexdigest()
    if _plan_is_current(base, source_hash, buffer_meters):
        return
    if not base.exists():
        counts = make_plan(config["source_features"], base, buffer_meters)
        print(f"Generated buffered target: {base}; parts: {counts}", flush=True)
        return

    # Preserve the old plan, ledger, and Ovi folder. A distinct filename gives
    # the changed geometry a distinct imported tree even if its part count is unchanged.
    signature = hashlib.sha256(f"{source_hash}:{buffer_meters}".encode()).hexdigest()[:12]
    suffix = f"_src{signature}"
    current = base.with_name(base.stem + suffix + base.suffix)
    if not current.exists():
        counts = make_plan(config["source_features"], current, buffer_meters)
        print(f"Source changed; generated versioned target: {current}; parts: {counts}", flush=True)
    if not _plan_is_current(current, source_hash, buffer_meters):
        raise RuntimeError(f"Versioned target is inconsistent: {current}")
    previous = [(base, config["export_dir"])]
    for candidate in sorted(base.parent.glob(base.stem + "_src*" + base.suffix)):
        old_suffix = candidate.stem[len(base.stem):]
        previous.append((candidate, config["export_dir"].with_name(config["export_dir"].name + old_suffix)))
    config["target"] = current
    config["export_dir"] = config["export_dir"].with_name(config["export_dir"].name + suffix)
    config["mosaic_dir"] = config["mosaic_dir"].with_name(config["mosaic_dir"].name + suffix)
    reused = _seed_compatible_exports(current, config["export_dir"], previous)
    print(f"Versioned export directory: {config['export_dir']}; reused TIFs: {reused}", flush=True)


def run(config, check_only=False, prepare_only=False, mosaic_only=False):
    if not config["source_features"].is_file():
        raise FileNotFoundError(config["source_features"])
    from src.target_input import prepare_target

    if "plan_buffer_meters" in config:
        _resolve_buffered_plan(config)

    target = prepare_target(config["target"], config["prepared_target"])
    features = validate_plan(target)
    names = [item["properties"]["name"] for item in features]
    plan_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    export_dir = config["export_dir"]
    ledger_file = export_dir / "ovi_export_ledger.json"
    ledger = json.loads(ledger_file.read_text(encoding="utf-8")) if ledger_file.exists() else {}
    if ledger and ledger.get("plan_sha256") != plan_hash:
        raise RuntimeError("Target differs from existing export ledger; use a new export_dir")
    completed = len(set(names) & set(ledger.get("completed", {})))
    print(f"Target: {target}; polygons: {len(features)}; ledger: {completed}/{len(features)}", flush=True)
    if check_only or prepare_only:
        return

    if not mosaic_only:
        ensure_ovi_running(config)
        from tools.ovi_batch_export import run as export_all
        report(config, "exporting", target=str(target), completed=completed, total=len(features))
        export_all(SimpleNamespace(
            plan=target, output=export_dir, only=None, limit=None,
            timeout=int(config.get("export_timeout_seconds", 900)),
            download_timeout=int(config.get("download_timeout_seconds", 900)),
            max_attempts=int(config.get("max_attempts", 3)),
        ))
    from tools.ovi_batch_export import verify_tif
    rasters = []
    for feature in features:
        path = export_dir / f"{feature['properties']['name']}.tif"
        verify_tif(path, feature)
        rasters.append(path)
    report(config, "exports_complete", count=len(rasters))

    if config.get("mosaic", True):
        from src.raster_features import export_raster_features
        from src.raster_features import _load_features
        signature_data = ["ovi-alpha>=128-v2", plan_hash,
                          hashlib.sha256(config["source_features"].read_bytes()).hexdigest()]
        signature_data.extend(f"{p.name}:{p.stat().st_size}:{p.stat().st_mtime_ns}" for p in rasters)
        signature = hashlib.sha256("|".join(signature_data).encode("utf-8")).hexdigest()
        mosaic_dir = config["mosaic_dir"]
        ledger_path = mosaic_dir / "mosaic_ledger.json"
        mosaic_ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
        if mosaic_ledger.get("input_signature") != signature:
            mosaic_ledger = {"input_signature": signature, "completed": {}}
        feature_count = sum(1 for _ in _load_features(config["source_features"]))
        report(config, "mosaicking", source_count=len(rasters), features=feature_count)
        for ordinal in range(1, feature_count + 1):
            prior = mosaic_ledger["completed"].get(str(ordinal))
            if prior and Path(prior).is_file() and Path(prior).stat().st_size > 4096:
                import rasterio
                with rasterio.open(prior) as mosaic:
                    valid = (mosaic.count == 4 and mosaic.crs is not None
                             and mosaic.crs.to_epsg() == 3857
                             and mosaic.width > 0 and mosaic.height > 0)
                if valid:
                    print(f"Mosaic {ordinal}/{feature_count}: skip verified checkpoint", flush=True)
                    continue
            summary = export_raster_features(rasters, config["source_features"], mosaic_dir, ordinal)
            if len(summary) != 1 or not summary[0][1]:
                raise RuntimeError(f"Incomplete feature mosaic: {summary}")
            mosaic_ledger["completed"][str(ordinal)] = summary[0][2]
            atomic_json(ledger_path, mosaic_ledger)
            print(f"Mosaic {ordinal}/{feature_count}: {summary[0][2]}", flush=True)
        report(config, "complete", exports=len(rasters), mosaics=feature_count)
    else:
        report(config, "complete", exports=len(rasters), mosaics=0)


def main():
    parser = argparse.ArgumentParser(description="Resume configured Ovi level-18 TIF exports")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="Validate configuration and current ledger without UI")
    action.add_argument("--prepare", action="store_true", help="Prepare importable target without UI")
    action.add_argument("--mosaic-only", action="store_true", help="Rebuild final feature TIFs from existing exports without Ovi")
    action.add_argument("--run", action="store_true", help="Run the configured export without opening the settings window")
    args = parser.parse_args()
    config_path = args.config.resolve()
    if not (args.check or args.prepare or args.mosaic_only or args.run):
        from src.config_gui import launch_gui
        launch_gui(config_path)
        return 0
    config = load_config(config_path)
    desired_python = config.get("python") if not PACKAGED else None
    if desired_python is not None and not desired_python.is_file():
        raise FileNotFoundError(f"Configured Python not found: {desired_python}")
    if desired_python is not None and Path(sys.executable).resolve() != desired_python.resolve():
        return subprocess.call([str(desired_python), str(Path(__file__).resolve()),
                                "--config", str(config_path),
                                *(["--check"] if args.check else []),
                                *(["--prepare"] if args.prepare else []),
                                *(["--mosaic-only"] if args.mosaic_only else []),
                                *(["--run"] if args.run else [])], cwd=ROOT)
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8")
    with single_instance(config["lock_file"]):
        try:
            run(config, check_only=args.check, prepare_only=args.prepare,
                mosaic_only=args.mosaic_only)
        except Exception as exc:
            report(config, "failed", error=str(exc), traceback=traceback.format_exc())
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
