import argparse
from pathlib import Path
import socket
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FEATURES = Path(r"D:\0-liaoliao\0-郎酒新项目\样本区域\0-合并.geojson")


def _args():
    parser = argparse.ArgumentParser(description="将奥维 18 级瓦片转换为 EPSG:3857 GeoTIFF")
    parser.add_argument("--bbox", nargs=4, type=float, metavar=("WEST", "SOUTH", "EAST", "NORTH"), help="WGS84 经纬度范围")
    parser.add_argument("--tiles", nargs=4, type=int, metavar=("MIN_X", "MIN_Y", "MAX_X", "MAX_Y"), help="18 级 XYZ 瓦片范围")
    parser.add_argument("--tile-dir", type=Path, help="已导出的 XYZ 文件目录：18/x/y.jpg")
    parser.add_argument("--source-raster", type=Path, action="append", default=[], help="奥维导出的带坐标 TIF；可重复指定多个")
    parser.add_argument("--source-dir", type=Path, help="奥维导出的带坐标 TIF 所在目录；读取目录下所有 tif/tiff")
    parser.add_argument("--http", action="store_true", help="从奥维 HTTP 瓦片服务取图；内置地图 140 不支持")
    parser.add_argument("--features", type=Path, default=DEFAULT_FEATURES, help="每个多边形单独导出；默认使用 0-合并.geojson")
    parser.add_argument("--feature-index", type=int, help="只处理第 N 个要素（从 1 开始）")
    parser.add_argument("--make-plan", action="store_true", help="按 3 km × 3 km 网格拆分要素并生成导出区域")
    parser.add_argument("--url", default="http://127.0.0.1:9999", help="奥维 HTTP 瓦片服务地址")
    parser.add_argument("--map-id", type=int, default=140)
    parser.add_argument("--out", type=Path, default=ROOT / "out")
    parser.add_argument("--chunk-tiles", type=int, default=16, help="单个 TIFF 的瓦片边长")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=10)
    return parser.parse_args()


def main() -> int:
    args = _args()
    if args.bbox and args.tiles:
        raise SystemExit("--bbox 和 --tiles 只能指定一个")
    if args.chunk_tiles < 1 or args.workers < 1:
        raise SystemExit("--chunk-tiles 和 --workers 必须大于 0")
    if args.make_plan:
        from .export_plan import make_plan

        plan = args.out / "grid_3km_z18.geojson"
        counts = make_plan(args.features, plan)
        print(f"已生成 {plan} 和 {plan.with_suffix('.shp')}；各要素分块数：{counts}")
        return 0
    if args.source_dir:
        if not args.source_dir.is_dir():
            raise SystemExit(f"影像目录不存在：{args.source_dir}")
        args.source_raster.extend(sorted(p for p in args.source_dir.rglob("*") if p.suffix.lower() in (".tif", ".tiff")))
        if not args.source_raster:
            raise SystemExit(f"影像目录内没有 tif/tiff：{args.source_dir}")
    if args.source_raster:
        from .raster_features import export_raster_features

        if args.bbox or args.tiles or args.tile_dir or args.http:
            raise SystemExit("--source-raster 不能与 --bbox、--tiles、--tile-dir 或 --http 并用")
        summary = export_raster_features(args.source_raster, args.features, args.out, args.feature_index)
        for ordinal, count, result in summary:
            print(f"要素 {ordinal}: {result}")
        return 0 if summary and all(count > 0 for _, count, _ in summary) else 2
    if not args.tile_dir and not args.http:
        raise SystemExit(
            "地图 140 是奥维内置地图，HTTP 瓦块服务不发布此类数据。"
            "请先在奥维导出带坐标的 18 级 TIF，再用 --source-raster 指定；"
            "或提供已导出的 18/x/y.jpg 目录并使用 --tile-dir。"
        )
    if args.http and args.map_id == 140:
        raise SystemExit("奥维官方说明内置地图不支持发布 HTTP 瓦片；地图 140 的请求会返回 404。请使用 --source-raster 或 --tile-dir。")
    from .geo import WORLD_TILES, tile_bounds_from_bbox

    bounds = tile_bounds_from_bbox(tuple(args.bbox)) if args.bbox else tuple(args.tiles) if args.tiles else None
    if bounds:
        min_x, min_y, max_x, max_y = bounds
        if not (0 <= min_x <= max_x < WORLD_TILES and 0 <= min_y <= max_y < WORLD_TILES):
            raise SystemExit("瓦片坐标必须在 18 级有效范围内，且最小值不大于最大值")
    elif not args.features.is_file():
        raise SystemExit(f"矢量文件不存在：{args.features}")
    if not args.tile_dir:
        parsed = urlsplit(args.url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise SystemExit("--url 必须是有效的 http(s) 地址")
        try:
            with socket.create_connection((parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)), timeout=2):
                pass
        except OSError as exc:
            raise SystemExit(
                "奥维 HTTP 瓦片服务未开启。请在奥维：系统 → 系统设置 → 高级 → 第三方接口 → Web接口，"
                "启用 WebSocket 与 HTTP 瓦块服务，端口设为 9999。"
            ) from exc
    from .tiles import TileSource

    source = TileSource(args.tile_dir, args.url, args.map_id, args.timeout)
    if bounds:
        from .converter import convert

        count, files = convert(source, bounds, args.out, args.chunk_tiles, args.workers)
        print(f"完成：18 级瓦片 {count} 张，GeoTIFF {files} 个，输出目录 {args.out}")
        return 0 if count else 2
    from .features import export_features

    summary = export_features(source, args.features, args.out, args.workers, args.feature_index)
    for ordinal, count, result in summary:
        print(f"要素 {ordinal}: {count} 张瓦片；{result}")
    return 0 if summary and all(count > 0 for _, count, _ in summary) else 2
