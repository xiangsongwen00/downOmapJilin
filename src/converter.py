from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys

qgis_root = Path(sys.executable).resolve().parents[2]
proj_data = qgis_root / "share" / "proj"
if (proj_data / "proj.db").is_file():
    os.environ["PROJ_DATA"] = str(proj_data)

from osgeo import gdal, osr

from .geo import TILE_SIZE, geotransform
from .tiles import TileSource

gdal.UseExceptions()


def _read_tile(item: tuple[int, int], source: TileSource):
    x, y = item
    return x, y, source.get(x, y)


def _decode(data: bytes):
    name = "/vsimem/ovi_tile"
    # Unique name is only needed during decode; decoding runs on the main thread.
    gdal.FileFromMemBuffer(name, data)
    try:
        dataset = gdal.Open(name)
        if dataset is None or dataset.RasterXSize != TILE_SIZE or dataset.RasterYSize != TILE_SIZE:
            raise ValueError("瓦片不是 256×256 图片")
        if dataset.RasterCount < 3:
            raise ValueError("瓦片不是 RGB 影像")
        return [dataset.GetRasterBand(i).ReadRaster() for i in (1, 2, 3)]
    finally:
        gdal.Unlink(name)


def convert(
    source: TileSource,
    bounds: tuple[int, int, int, int],
    output: Path,
    chunk_tiles: int = 16,
    workers: int = 12,
) -> tuple[int, int]:
    min_x, min_y, max_x, max_y = bounds
    output.mkdir(parents=True, exist_ok=True)
    driver = gdal.GetDriverByName("GTiff")
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    tile_count = file_count = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for top_y in range(min_y, max_y + 1, chunk_tiles):
            for left_x in range(min_x, max_x + 1, chunk_tiles):
                right_x = min(left_x + chunk_tiles - 1, max_x)
                bottom_y = min(top_y + chunk_tiles - 1, max_y)
                coords = ((x, y) for y in range(top_y, bottom_y + 1) for x in range(left_x, right_x + 1))
                target = output / f"map_{source.map_id}_z18_x{left_x}_y{top_y}.tif"
                dataset = None
                try:
                    for x, y, data in pool.map(lambda item: _read_tile(item, source), coords):
                        if data is None:
                            continue
                        bands = _decode(data)
                        if dataset is None:
                            width = (right_x - left_x + 1) * TILE_SIZE
                            height = (bottom_y - top_y + 1) * TILE_SIZE
                            dataset = driver.Create(
                                str(target), width, height, 4, gdal.GDT_Byte,
                                options=["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=2", "BIGTIFF=IF_SAFER", "SPARSE_OK=YES"],
                            )
                            if dataset is None:
                                raise RuntimeError(f"不能创建 {target}")
                            dataset.SetGeoTransform(geotransform(left_x, top_y))
                            dataset.SetProjection(srs.ExportToWkt())
                            for band_number in (1, 2, 3):
                                dataset.GetRasterBand(band_number).SetColorInterpretation(
                                    (gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand)[band_number - 1]
                                )
                            dataset.GetRasterBand(4).SetColorInterpretation(gdal.GCI_AlphaBand)
                        offset_x = (x - left_x) * TILE_SIZE
                        offset_y = (y - top_y) * TILE_SIZE
                        for band_number, pixels in enumerate(bands, 1):
                            dataset.GetRasterBand(band_number).WriteRaster(offset_x, offset_y, TILE_SIZE, TILE_SIZE, pixels)
                        dataset.GetRasterBand(4).WriteRaster(offset_x, offset_y, TILE_SIZE, TILE_SIZE, bytes([255]) * (TILE_SIZE * TILE_SIZE))
                        tile_count += 1
                finally:
                    if dataset is not None:
                        dataset.FlushCache()
                        dataset = None
                        file_count += 1
                print(f"已扫描 x={left_x}..{right_x}, y={top_y}..{bottom_y}; 累计 {tile_count} 张瓦片、{file_count} 个 TIFF", flush=True)
    return tile_count, file_count
