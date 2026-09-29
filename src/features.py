"""Export one clipped level-18 GeoTIFF per polygon feature."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re

from osgeo import gdal, ogr, osr

from .converter import _decode
from .geo import ORIGIN, PIXEL_SIZE, TILE_SIZE, WORLD_TILES, geotransform
from .tiles import TileSource

ogr.UseExceptions()


def _tile_range(geom):
    west, east, south, north = geom.GetEnvelope()
    scale = TILE_SIZE * PIXEL_SIZE
    return (
        max(0, int((west + ORIGIN) // scale)),
        max(0, int((ORIGIN - north) // scale)),
        min(WORLD_TILES - 1, int((east + ORIGIN) // scale)),
        min(WORLD_TILES - 1, int((ORIGIN - south) // scale)),
    )


def _feature_name(feature, ordinal):
    parts = [f"{ordinal:02d}"]
    for field in ("layer", "village"):
        value = feature.GetField(field) if feature.GetFieldIndex(field) >= 0 else None
        if value:
            parts.append(str(value))
    if len(parts) == 1:
        parts.append(f"feature_{feature.GetFID()}")
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", "_".join(parts))[:150]


def _mask_for_tile(x, y, layer, srs):
    mem = gdal.GetDriverByName("MEM").Create("", TILE_SIZE, TILE_SIZE, 1, gdal.GDT_Byte)
    mem.SetGeoTransform(geotransform(x, y))
    mem.SetProjection(srs.ExportToWkt())
    gdal.RasterizeLayer(mem, [1], layer, burn_values=[255])
    pixels = mem.GetRasterBand(1).ReadRaster()
    mem = None
    return pixels if any(pixels) else None


def _fetch(item, source):
    x, y, mask = item
    return x, y, mask, source.get(x, y)


def export_features(source: TileSource, input_path: Path, output: Path, workers: int = 12, only_index: int | None = None):
    vector = ogr.Open(str(input_path))
    if vector is None:
        raise RuntimeError(f"无法打开矢量文件：{input_path}")
    input_layer = vector.GetLayer()
    input_srs = input_layer.GetSpatialRef()
    if input_srs is None:
        raise RuntimeError("矢量文件没有坐标系")
    input_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    target_srs = osr.SpatialReference()
    target_srs.ImportFromEPSG(3857)
    target_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(input_srs, target_srs)
    driver = gdal.GetDriverByName("GTiff")
    output.mkdir(parents=True, exist_ok=True)
    summary = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for ordinal, feature in enumerate(input_layer, 1):
            if only_index is not None and ordinal != only_index:
                continue
            geom = feature.GetGeometryRef()
            if geom is None or geom.IsEmpty():
                summary.append((ordinal, 0, "空几何"))
                continue
            projected = geom.Clone()
            projected.Transform(transform)
            min_x, min_y, max_x, max_y = _tile_range(projected)
            if min_x > max_x or min_y > max_y:
                summary.append((ordinal, 0, "范围无效"))
                continue
            memory_vector = ogr.GetDriverByName("Memory").CreateDataSource("")
            mask_layer = memory_vector.CreateLayer("polygon", target_srs, ogr.wkbUnknown)
            mask_feature = ogr.Feature(mask_layer.GetLayerDefn())
            mask_feature.SetGeometry(projected)
            mask_layer.CreateFeature(mask_feature)
            name = _feature_name(feature, ordinal)
            path = output / f"{name}_z18.tif"
            temp_path = output / f"{name}_z18.partial.tif"
            dataset = None
            count = 0
            try:
                # Limit both memory use and queued HTTP requests.
                batch = []

                def flush_batch():
                    nonlocal dataset, count, batch
                    for x, y, mask, data in pool.map(lambda item: _fetch(item, source), batch):
                        if data is None:
                            continue
                        bands = _decode(data)
                        if dataset is None:
                            dataset = driver.Create(
                                str(temp_path),
                                (max_x - min_x + 1) * TILE_SIZE,
                                (max_y - min_y + 1) * TILE_SIZE,
                                4,
                                gdal.GDT_Byte,
                                options=["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=2", "BIGTIFF=IF_SAFER", "SPARSE_OK=YES"],
                            )
                            if dataset is None:
                                raise RuntimeError(f"无法创建 {temp_path}")
                            dataset.SetGeoTransform(geotransform(min_x, min_y))
                            dataset.SetProjection(target_srs.ExportToWkt())
                            for i, color in enumerate((gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand, gdal.GCI_AlphaBand), 1):
                                dataset.GetRasterBand(i).SetColorInterpretation(color)
                        off_x = (x - min_x) * TILE_SIZE
                        off_y = (y - min_y) * TILE_SIZE
                        for i, pixels in enumerate(bands, 1):
                            dataset.GetRasterBand(i).WriteRaster(off_x, off_y, TILE_SIZE, TILE_SIZE, pixels)
                        dataset.GetRasterBand(4).WriteRaster(off_x, off_y, TILE_SIZE, TILE_SIZE, mask)
                        count += 1
                    batch = []

                for y in range(min_y, max_y + 1):
                    for x in range(min_x, max_x + 1):
                        mask = _mask_for_tile(x, y, mask_layer, target_srs)
                        if mask is None:
                            continue
                        batch.append((x, y, mask))
                        if len(batch) >= 64:
                            flush_batch()
                    print(f"要素 {ordinal}: 扫描 y={y}/{max_y}，已取得 {count} 张瓦片", flush=True)
                if batch:
                    flush_batch()
            finally:
                if dataset is not None:
                    dataset.FlushCache()
                    dataset = None
            if count:
                temp_path.replace(path)
                summary.append((ordinal, count, str(path)))
            else:
                summary.append((ordinal, 0, "范围内没有可读取的 18 级瓦片"))
    vector = None
    return summary
