"""Normalize vector download targets into Ovi-importable WGS84 GeoJSON."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

import geopandas as gpd
from shapely import force_2d
from shapely.geometry import mapping, shape


def _polygon(geometry):
    if geometry is None or geometry.is_empty:
        return False
    return geometry.geom_type in {"Polygon", "MultiPolygon"}


def prepare_target(source: Path, destination: Path) -> Path:
    """Use every polygon in SHP/GeoJSON, and the first polygon in a GPKG.

    A prepared level-18 GeoJSON plan is reused byte-for-byte, preserving its
    existing export ledger and its matching folder in Ovi.
    """
    suffix = source.suffix.lower()
    if suffix not in {".shp", ".gpkg", ".geojson", ".json"}:
        raise ValueError(f"Unsupported target format: {source.suffix}")
    if not source.is_file():
        raise FileNotFoundError(source)
    if suffix in {".geojson", ".json"}:
        data = json.loads(source.read_text(encoding="utf-8-sig"))
        if data.get("type") != "FeatureCollection":
            raise ValueError("GeoJSON target must be a FeatureCollection")
        items = data.get("features", [])
        if items and all(
            item.get("properties", {}).get("name")
            and item.get("properties", {}).get("zoom") == 18
            and _polygon(shape(item["geometry"])) for item in items
        ):
            return source
        frame = gpd.read_file(source)
    elif suffix == ".gpkg":
        layers = gpd.list_layers(source)
        frame = None
        for layer in layers.itertuples():
            if layer.geometry_type is None:
                continue
            candidate = gpd.read_file(source, layer=layer.name)
            candidate = candidate[candidate.geometry.apply(_polygon)]
            if not candidate.empty:
                frame = candidate.iloc[:1]
                break
        if frame is None:
            raise ValueError(f"GeoPackage has no polygon: {source}")
    else:
        frame = gpd.read_file(source)
    if frame.crs is None:
        raise ValueError(f"Target CRS is missing: {source}")
    frame = frame.to_crs(4326)
    features = []
    used = set()
    for ordinal, (_, row) in enumerate(frame.iterrows(), 1):
        geom = force_2d(row.geometry)
        if not _polygon(geom):
            continue
        raw = row.get("name") or f"{source.stem}_part_{ordinal:02d}"
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(raw))[:150]
        if name in used:
            name = f"{name}_{ordinal:02d}"
        used.add(name)
        features.append({"type": "Feature", "properties": {
            "name": name, "zoom": 18, "feature_index": ordinal,
        }, "geometry": mapping(geom)})
    if not features:
        raise ValueError(f"No polygon target found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"type": "FeatureCollection", "features": features},
                         ensure_ascii=False, separators=(",", ":"))
    if not destination.exists() or destination.read_text(encoding="utf-8") != payload:
        temporary = destination.with_suffix(".geojson.tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, destination)
    return destination
