"""Create a small synthetic dataset so the project runs offline, out of the box.

The layers are fictional (three rectangular "communes" around Luxembourg City)
and are used by the demo, the tests and the evaluation set. For real open data
see `scripts/fetch_lux_data.py`.

Usage:  python scripts/make_sample_data.py [--out data/sample]
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point, box

logger = logging.getLogger("make_sample_data")

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "sample"
CRS = "EPSG:4326"


def build_layers() -> dict[str, gpd.GeoDataFrame]:
    """Return the synthetic layers, keyed by layer name."""
    communes = gpd.GeoDataFrame(
        {"name": ["Nord", "Centre", "Sud"]},
        geometry=[
            box(6.10, 49.63, 6.16, 49.66),
            box(6.10, 49.60, 6.16, 49.63),
            box(6.10, 49.57, 6.16, 49.60),
        ],
        crs=CRS,
    )
    stops = gpd.GeoDataFrame(
        {"name": [f"Stop {i}" for i in range(1, 7)]},
        geometry=[
            Point(6.12, 49.64),
            Point(6.14, 49.65),
            Point(6.13, 49.61),
            Point(6.15, 49.62),
            Point(6.11, 49.615),
            Point(6.13, 49.58),
        ],
        crs=CRS,
    )
    schools = gpd.GeoDataFrame(
        {"name": ["School A", "School B", "School C"]},
        geometry=[Point(6.121, 49.641), Point(6.15, 49.59), Point(6.135, 49.612)],
        crs=CRS,
    )
    return {"communes": communes, "stops": stops, "schools": schools}


def main() -> None:
    """Write each synthetic layer as GeoJSON."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args.out.mkdir(parents=True, exist_ok=True)
    for name, gdf in build_layers().items():
        path = args.out / f"{name}.geojson"
        gdf.to_file(path, driver="GeoJSON")
        logger.info("Wrote %s (%d features)", path, len(gdf))


if __name__ == "__main__":
    main()
