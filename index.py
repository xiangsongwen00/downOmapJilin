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
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent


def project_path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def single_instance(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("Another Ovi index.py process is running") from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    required = {"python", "omapexepath", "target", "export_dir", "source_features", "mosaic_dir"}
    missing = required - config.keys()
    if missing:
        raise ValueError(f"Missing config fields: {sorted(missing)}")
    if config.get("zoom", 18) != 18 or config.get("format", "tif").lower() != "tif":
        raise ValueError("This workflow requires level 18 and TIF")
    if config.get("coordinate_system", "default") != "default":
        raise ValueError("Coordinate system must remain the Ovi default")
    for key in ("export_timeout_seconds", "download_timeout_seconds", "max_attempts"):
        if int(config.get(key, 3)) < 1:
            raise ValueError(f"{key} must be positive")
    for key in required - {"python"}:
        config[key] = project_path(config[key])
    config["python"] = project_path(config["python"])
    if not config["omapexepath"].is_file():
        raise FileNotFoundError(f"Ovi executable not found: {config['omapexepath']}")
    config["prepared_target"] = project_path(config.get("prepared_target", "out/ovi_import_target.geojson"))
    config["state_file"] = project_path(config.get("state_file", "out/ovi_pipeline_state.json"))
    config["lock_file"] = project_path(config.get("lock_file", "out/ovi_pipeline.lock"))
    return config


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


def run(config, check_only=False, prepare_only=False, mosaic_only=False):
    if not config["source_features"].is_file():
        raise FileNotFoundError(config["source_features"])
    from src.target_input import prepare_target

    if "plan_buffer_meters" in config:
        requested_buffer = float(config["plan_buffer_meters"])
        if requested_buffer < 0:
            raise ValueError("plan_buffer_meters must be nonnegative")
        target_path = config["target"]
        if target_path.suffix.lower() not in {".geojson", ".json"}:
            raise ValueError("Generated buffered target must be GeoJSON")
        if not target_path.exists():
            from src.export_plan import make_plan
            counts = make_plan(config["source_features"], target_path, requested_buffer)
            print(f"Generated buffered target: {target_path}; parts: {counts}", flush=True)
        generated = json.loads(target_path.read_text(encoding="utf-8"))["features"]
        source_hash = hashlib.sha256(config["source_features"].read_bytes()).hexdigest()
        if not generated or any(
            float(feature["properties"].get("buffer_meters", -1)) != requested_buffer
            or feature["properties"].get("source_sha256") != source_hash
            for feature in generated
        ):
            raise RuntimeError("Buffered target is stale; regenerate it before running")

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
    args = parser.parse_args()
    config_path = args.config.resolve()
    config = load_config(config_path)
    desired_python = config["python"]
    if not desired_python.is_file():
        raise FileNotFoundError(f"gis312 Python not found: {desired_python}")
    if Path(sys.executable).resolve() != desired_python.resolve():
        return subprocess.call([str(desired_python), str(Path(__file__).resolve()),
                                "--config", str(config_path),
                                *(["--check"] if args.check else []),
                                *(["--prepare"] if args.prepare else []),
                                *(["--mosaic-only"] if args.mosaic_only else [])], cwd=ROOT)
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
