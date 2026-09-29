"""Mosaic Ovi TIFs by their fourth-band transparency and clip to features."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
from contextlib import ExitStack

import geopandas as gpd
import numpy as np
import pyproj
from pyproj import Transformer
from shapely import force_2d
from shapely.geometry import box, mapping, shape
from shapely.ops import transform as transform_geom

# The host may point PROJ_LIB at PostgreSQL's incompatible proj.db.
os.environ["PROJ_LIB"] = pyproj.datadir.get_data_dir()
os.environ["PROJ_DATA"] = pyproj.datadir.get_data_dir()

import rasterio
from affine import Affine
from rasterio.enums import ColorInterp, Resampling
from rasterio.features import geometry_mask
from rasterio.warp import reproject
from rasterio.windows import Window

from .geo import PIXEL_SIZE


BLOCK = 512
OVI_VALID_ALPHA = 128


def _feature_name(properties, ordinal):
    parts = [f"{ordinal:02d}"]
    parts.extend(str(properties[field]) for field in ("layer", "village") if properties.get(field))
    if len(parts) == 1:
        parts.append(f"feature_{ordinal - 1}")
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", "_".join(parts))[:150]


def _load_features(path):
    if path.suffix.lower() in (".geojson", ".json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("type") != "FeatureCollection":
            raise ValueError("Expected FeatureCollection")
        for item in data["features"]:
            yield item.get("properties") or {}, force_2d(shape(item["geometry"]))
    else:
        frame = gpd.read_file(path)
        if frame.crs is None:
            raise ValueError(f"Missing CRS: {path}")
        frame = frame.to_crs(4326)
        for _, row in frame.iterrows():
            yield row.to_dict(), force_2d(row.geometry)


def _output_grid(geometry):
    west, south, east, north = geometry.bounds
    west = math.floor(west / PIXEL_SIZE) * PIXEL_SIZE
    east = math.ceil(east / PIXEL_SIZE) * PIXEL_SIZE
    south = math.floor(south / PIXEL_SIZE) * PIXEL_SIZE
    north = math.ceil(north / PIXEL_SIZE) * PIXEL_SIZE
    width = round((east - west) / PIXEL_SIZE)
    height = round((north - south) / PIXEL_SIZE)
    return width, height, Affine.translation(west, north) @ Affine.scale(PIXEL_SIZE, -PIXEL_SIZE)


def _copy_block(sources, window, transform, geometry):
    height, width = int(window.height), int(window.width)
    inside = geometry_mask([mapping(geometry)], out_shape=(height, width),
                           transform=transform, invert=True)
    rgb = np.zeros((3, height, width), dtype=np.uint8)
    alpha = np.zeros((height, width), dtype=np.uint8)
    # Build bounds directly from this block's transform. All source rasters are EPSG:3857.
    block_box = box(transform.c, transform.f - height * PIXEL_SIZE,
                    transform.c + width * PIXEL_SIZE, transform.f)
    for source in sources:
        source_box = box(*source.bounds)
        if not source_box.intersects(block_box):
            continue
        src_alpha = np.zeros((height, width), dtype=np.uint8)
        reproject(rasterio.band(source, 4), src_alpha,
                  src_transform=source.transform, src_crs=source.crs,
                  dst_transform=transform, dst_crs="EPSG:3857",
                  resampling=Resampling.nearest)
        # Ovi encodes its near-white no-data background as RGB 254/254/254
        # with alpha=1. A plain alpha>0 test lets that background overwrite
        # real pixels from neighboring export regions.
        valid = src_alpha >= OVI_VALID_ALPHA
        if not np.any(valid):
            continue
        for band in range(1, 4):
            pixels = np.zeros((height, width), dtype=np.uint8)
            reproject(rasterio.band(source, band), pixels,
                      src_transform=source.transform, src_crs=source.crs,
                      dst_transform=transform, dst_crs="EPSG:3857",
                      resampling=Resampling.nearest)
            rgb[band - 1, valid] = pixels[valid]
        alpha[valid] = 255
    alpha[~inside] = 0
    rgb[:, ~inside] = 0
    return rgb, alpha, int(np.count_nonzero(inside)), int(np.count_nonzero(inside & (alpha > 0)))


def export_raster_features(rasters: list[Path], features: Path, output: Path,
                           only_index: int | None = None):
    output.mkdir(parents=True, exist_ok=True)
    to_mercator = Transformer.from_crs(4326, 3857, always_xy=True).transform
    summary = []
    with ExitStack() as stack:
        sources = [stack.enter_context(rasterio.open(path)) for path in rasters]
        for path, source in zip(rasters, sources):
            if source.crs is None or source.crs.to_epsg() != 3857:
                raise RuntimeError(f"Raster is not EPSG:3857: {path}")
            if source.count != 4:
                raise RuntimeError(f"Expected four Ovi bands including transparency: {path}")
        for ordinal, (properties, original) in enumerate(_load_features(features), 1):
            if only_index is not None and ordinal != only_index:
                continue
            if original.is_empty:
                summary.append((ordinal, 0, "空几何"))
                continue
            projected = transform_geom(to_mercator, original)
            width, height, transform = _output_grid(projected)
            name = _feature_name(properties, ordinal)
            target = output / f"{name}_z18.tif"
            temp = output / f"{name}_z18.partial.tif"
            profile = {
                "driver": "GTiff", "width": width, "height": height, "count": 4,
                "dtype": "uint8", "crs": "EPSG:3857", "transform": transform,
                "tiled": True, "blockxsize": BLOCK, "blockysize": BLOCK,
                "compress": "DEFLATE", "predictor": 2, "BIGTIFF": "IF_SAFER",
            }
            expected = present = 0
            with rasterio.open(temp, "w", **profile) as destination:
                destination.colorinterp = (ColorInterp.red, ColorInterp.green,
                                           ColorInterp.blue, ColorInterp.alpha)
                for _, window in destination.block_windows(1):
                    local_transform = transform @ Affine.translation(window.col_off, window.row_off)
                    block_box = box(local_transform.c,
                                    local_transform.f - window.height * PIXEL_SIZE,
                                    local_transform.c + window.width * PIXEL_SIZE,
                                    local_transform.f)
                    if not projected.intersects(block_box):
                        continue
                    rgb, alpha, block_expected, block_present = _copy_block(
                        sources, window, local_transform, projected)
                    destination.write(rgb, window=window, indexes=[1, 2, 3])
                    destination.write(alpha, 4, window=window)
                    expected += block_expected
                    present += block_present
            if expected == 0 or present == 0:
                temp.unlink(missing_ok=True)
                summary.append((ordinal, 0, "源影像未覆盖要素"))
            elif present / expected < 0.995:
                incomplete = output / f"{name}_z18.incomplete.tif"
                temp.replace(incomplete)
                summary.append((ordinal, 0, f"仅覆盖 {present / expected:.3%}；待补齐影像：{incomplete}"))
            else:
                temp.replace(target)
                summary.append((ordinal, 1, str(target)))
    return summary
