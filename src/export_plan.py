"""Build level-18 Ovi export regions from 3 km grid intersections."""

from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path
import re

import geopandas as gpd
from pyproj import Transformer
from shapely import force_2d
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box, mapping, shape
from shapely.ops import transform, unary_union


GRID_METERS = 3000
MIN_FRAGMENT_M2 = 700_000
MAX_EXPORT_PIXELS = 100_000_000
PIXEL_SIZE_Z18 = 156543.03392804097 / (2**18)


def _polygonal(geometry):
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return geometry
    if isinstance(geometry, GeometryCollection):
        parts = [g for g in geometry.geoms if isinstance(g, (Polygon, MultiPolygon)) and not g.is_empty]
        return unary_union(parts) if parts else Polygon()
    return Polygon()


def _name(properties: dict, ordinal: int) -> str:
    parts = [f"{ordinal:02d}"]
    parts.extend(str(properties[field]) for field in ("layer", "village") if properties.get(field))
    if len(parts) == 1:
        parts.append(f"feature_{ordinal - 1}")
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", "_".join(parts))[:150]


def _pixel_count(projected_geometry, to_mercator) -> int:
    west, south, east, north = transform(to_mercator, projected_geometry).bounds
    return (int((east - west) / PIXEL_SIZE_Z18 + 2)
            * int((north - south) / PIXEL_SIZE_Z18 + 2))


def _merge_small_regions(regions: list[dict], to_mercator) -> list[dict]:
    """Merge each small region into a touching neighbor of minimum area.

    Shared edge takes precedence over a single-point touch. A disconnected
    island has no touching neighbor and is retained as its own export region.
    """
    while len(regions) > 1:
        small = [r for r in regions if r["geometry"].area < MIN_FRAGMENT_M2]
        if not small:
            break
        selected = None
        for fragment in sorted(small, key=lambda r: (r["geometry"].area, r["cell_order"])):
            options = []
            for neighbor in regions:
                if neighbor is fragment or not fragment["geometry"].intersects(neighbor["geometry"]):
                    continue
                combined = unary_union([fragment["geometry"], neighbor["geometry"]])
                if _pixel_count(combined, to_mercator) > MAX_EXPORT_PIXELS:
                    continue
                shared_edge = fragment["geometry"].boundary.intersection(neighbor["geometry"].boundary).length
                options.append((0 if shared_edge > 0.001 else 1,
                                neighbor["geometry"].area, neighbor["cell_order"], neighbor, combined))
            if options:
                selected = fragment, min(options, key=lambda option: option[:3])
                break
        if selected is None:
            break
        fragment, (_, _, _, neighbor, combined) = selected
        neighbor["geometry"] = combined
        neighbor["cells"].extend(fragment["cells"])
        regions.remove(fragment)
    return regions


def make_plan(vector_path: Path, output_path: Path, buffer_meters: float = 0):
    if buffer_meters < 0:
        raise ValueError("buffer_meters must be nonnegative")
    source_bytes = vector_path.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    source = json.loads(source_bytes)
    if source.get("type") != "FeatureCollection":
        raise ValueError(f"Expected GeoJSON FeatureCollection: {vector_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    features = []
    counts = {}
    for ordinal, item in enumerate(source["features"], 1):
        original = force_2d(_polygonal(shape(item["geometry"])))
        if original.is_empty:
            counts[ordinal] = 0
            continue
        west, _, east, _ = original.bounds
        zone = min(60, max(1, int(((west + east) / 2 + 180) // 6) + 1))
        to_utm = Transformer.from_crs(4326, 32600 + zone, always_xy=True).transform
        to_wgs84 = Transformer.from_crs(32600 + zone, 4326, always_xy=True).transform
        to_mercator = Transformer.from_crs(32600 + zone, 3857, always_xy=True).transform
        projected = transform(to_utm, original)
        west, south, east, north = projected.bounds
        x0 = math.floor(west / GRID_METERS) * GRID_METERS
        y0 = math.floor(south / GRID_METERS) * GRID_METERS
        x1 = math.floor(east / GRID_METERS) * GRID_METERS
        y1 = math.floor(north / GRID_METERS) * GRID_METERS
        regions = []
        for cell_y in range(y0, y1 + 1, GRID_METERS):
            for cell_x in range(x0, x1 + 1, GRID_METERS):
                clipped = _polygonal(projected.intersection(box(
                    cell_x, cell_y, cell_x + GRID_METERS, cell_y + GRID_METERS)))
                if clipped.is_empty or clipped.area <= 0:
                    continue
                regions.append({"geometry": clipped, "cell_order": len(regions),
                                "cells": [[cell_x, cell_y]]})
        regions = _merge_small_regions(regions, to_mercator)
        regions.sort(key=lambda r: r["cell_order"])
        base_name = _name(item.get("properties") or {}, ordinal)
        buffer_tag = f"_buf{str(buffer_meters).replace('.', 'p')}m" if buffer_meters else ""
        for part, region in enumerate(regions, 1):
            # Buffer each finished region separately. Neighboring exports now
            # overlap across their shared grid line; the final mosaic is still
            # clipped to the unbuffered source feature.
            geom = _polygonal(region["geometry"].buffer(buffer_meters)) if buffer_meters else region["geometry"]
            pixels = _pixel_count(geom, to_mercator)
            if pixels > MAX_EXPORT_PIXELS:
                raise ValueError(f"{base_name} part {part} exceeds Ovi pixel limit: {pixels}")
            features.append({
                "type": "Feature",
                "properties": {
                    "name": f"{base_name}_part_{part:02d}{buffer_tag}",
                    "feature_index": ordinal,
                    "part": part,
                    "zoom": 18,
                    "utm_zone": zone,
                    "grid_meters": GRID_METERS,
                    "buffer_meters": buffer_meters,
                    "source_sha256": source_hash,
                    "estimated_pixels": pixels,
                    "area_km2": round(region["geometry"].area / 1_000_000, 6),
                    "export_area_km2": round(geom.area / 1_000_000, 6),
                    "source_cells": len(region["cells"]),
                },
                "geometry": mapping(transform(to_wgs84, geom)),
            })
        counts[ordinal] = len(regions)
    plan = {"type": "FeatureCollection", "features": features}
    output_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    shp_path = output_path.with_suffix(".shp")
    frame = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
    frame = frame.rename(columns={
        "feature_index": "feat_idx", "grid_meters": "grid_m",
        "buffer_meters": "buffer_m", "source_sha256": "src_sha",
        "estimated_pixels": "est_pixels", "export_area_km2": "exp_km2",
        "source_cells": "src_cells",
    })
    frame.to_file(shp_path, driver="ESRI Shapefile", engine="pyogrio", encoding="UTF-8")
    return counts
