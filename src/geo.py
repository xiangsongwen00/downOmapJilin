import math

ZOOM = 18
TILE_SIZE = 256
ORIGIN = 20037508.342789244
WORLD_TILES = 1 << ZOOM
PIXEL_SIZE = 2 * ORIGIN / (WORLD_TILES * TILE_SIZE)


def lonlat_to_tile(lon: float, lat: float) -> tuple[int, int]:
    if not (-180 <= lon <= 180 and -85.05112878 <= lat <= 85.05112878):
        raise ValueError("经纬度超出 Web Mercator 的有效范围")
    x = min(WORLD_TILES - 1, max(0, math.floor((lon + 180) / 360 * WORLD_TILES)))
    latitude = math.radians(lat)
    y = min(WORLD_TILES - 1, max(0, math.floor((1 - math.asinh(math.tan(latitude)) / math.pi) / 2 * WORLD_TILES)))
    return x, y


def tile_bounds_from_bbox(bbox: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    west, south, east, north = bbox
    if west >= east or south >= north:
        raise ValueError("--bbox 应为 西经度 南纬度 东经度 北纬度，且西<东、南<北")
    min_x, min_y = lonlat_to_tile(west, north)
    max_x, max_y = lonlat_to_tile(east, south)
    return min_x, min_y, max_x, max_y


def geotransform(x: int, y: int) -> tuple[float, float, float, float, float, float]:
    return (
        -ORIGIN + x * TILE_SIZE * PIXEL_SIZE,
        PIXEL_SIZE,
        0.0,
        ORIGIN - y * TILE_SIZE * PIXEL_SIZE,
        0.0,
        -PIXEL_SIZE,
    )
