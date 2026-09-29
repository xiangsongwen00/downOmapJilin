"""Verify Ovi alpha=1 near-white background cannot cover valid imagery."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pyproj

os.environ["PROJ_LIB"] = pyproj.datadir.get_data_dir()
os.environ["PROJ_DATA"] = pyproj.datadir.get_data_dir()

import numpy as np
import rasterio
from affine import Affine
from rasterio.windows import Window
from shapely.geometry import box

from src.raster_features import _copy_block


def make(path: Path, values: np.ndarray, transform):
    with rasterio.open(path, "w", driver="GTiff", width=4, height=4,
                       count=4, dtype="uint8", crs="EPSG:3857",
                       transform=transform) as dst:
        dst.write(values)


def main():
    transform = Affine.translation(0, 4) @ Affine.scale(1, -1)
    red = np.zeros((4, 4, 4), dtype=np.uint8)
    red[0] = 200
    red[3] = 255
    overlay = np.full((4, 4, 4), 254, dtype=np.uint8)
    overlay[3] = 1
    overlay[0, 1:3, 1:3] = 0
    overlay[1, 1:3, 1:3] = 0
    overlay[2, 1:3, 1:3] = 200
    overlay[3, 1:3, 1:3] = 255
    transparent = np.full((4, 4, 4), 255, dtype=np.uint8)
    transparent[3] = 0
    with TemporaryDirectory() as temp:
        first, second, third = (Path(temp) / name for name in
                                ("red.tif", "overlay.tif", "transparent.tif"))
        make(first, red, transform)
        make(second, overlay, transform)
        make(third, transparent, transform)
        with rasterio.open(first) as src1, rasterio.open(second) as src2, rasterio.open(third) as src3:
            rgb, alpha, expected, present = _copy_block(
                [src1, src2, src3], Window(0, 0, 4, 4), transform, box(0, 0, 4, 4))
    assert (expected, present) == (16, 16)
    assert rgb[:, 0, 0].tolist() == [200, 0, 0]  # alpha=1/0 backgrounds did not cover red
    assert rgb[:, 1, 1].tolist() == [0, 0, 200]
    assert alpha[0, 0] == alpha[1, 1] == 255
    print("Alpha mosaic check passed")


if __name__ == "__main__":
    main()
