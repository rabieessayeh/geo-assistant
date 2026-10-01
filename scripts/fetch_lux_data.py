"""Download and clean open datasets for Luxembourg from data.public.lu.

Layers written to `data/lux/` (GeoJSON, WGS84, useful columns only):

- communes, cantons : administrative boundaries (ACT)
- stops             : public transport stops, from the national GTFS feed
- schools           : public school addresses (2021), geocoded with the
                      national geocoder because the source has no coordinates

Resource URLs on data.public.lu change at every update, so each dataset is
resolved through the portal API from its stable identifier (see `DATASETS`).
A `SOURCES.json` file records what was downloaded, from where and when.

Usage:  python scripts/fetch_lux_data.py [--out data/lux] [--only communes stops]
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd

logger = logging.getLogger("fetch_lux_data")

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "lux"
WGS84 = "EPSG:4326"
USER_AGENT = "geo-assistant (https://github.com/rabieessayeh/geo-assistant)"
TIMEOUT_S = 180

# data.public.lu API (udata): GET {PORTAL_API}/datasets/{slug}/ lists the resources.
PORTAL_API = "https://data.public.lu/api/1"
# National geocoder of the Administration du cadastre et de la topographie (ACT).
GEOCODER_URL = "https://apiv4.geoportail.lu/geocode/search"
GEOCODER_PAUSE_S = 0.2  # be polite: about 220 requests in total
# Geocoder accuracy 8 = matched on the house number; lower values are street- or
# locality-level guesses, which are dropped rather than plotted in the wrong place.
MIN_GEOCODE_ACCURACY = 8


@dataclass(frozen=True)
class Dataset:
    """A dataset of data.public.lu, identified by its stable slug."""

    slug: str
    publisher: str
    licence: str

    @property
    def page(self) -> str:
        """Human-readable page of the dataset."""
        return f"https://data.public.lu/en/datasets/{self.slug}/"


# All three slugs were checked against the live portal on 2026-10-01.
DATASETS = {
    "boundaries": Dataset(
        slug="limites-administratives-du-grand-duche-de-luxembourg",
        publisher="Administration du cadastre et de la topographie",
        licence="CC0",
    ),
    "gtfs": Dataset(
        slug="horaires-et-arrets-des-transport-publics-gtfs",
        publisher="Administration des transports publics",
        licence="CC BY",
    ),
    "schools": Dataset(
        slug="adresses-des-batiments-scolaires-2021",
        publisher="Ministère de l'Éducation nationale, de l'Enfance et de la Jeunesse",
        licence="CC0",
    ),
}

# Resource file names inside each dataset.
BOUNDARIES_FILE = "limadmin.geojson"
GTFS_PREFIX = "gtfs-"
PRIMARY_SCHOOLS_FILE = "adr-ecoles-fondamentales-2021.xlsx"
SECONDARY_SCHOOLS_FILE = "adr-lycees-2021.xlsx"


# ------------------------------------------------------------- download


def http_get(url: str) -> bytes:
    """Download a URL, raising `RuntimeError` with a readable message on failure."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Download failed for {url}: {exc}") from exc


def find_resource(dataset: Dataset, *, name: str | None = None, prefix: str | None = None) -> str:
    """Return the URL of a dataset resource, by exact file name or latest by prefix."""
    meta = json.loads(http_get(f"{PORTAL_API}/datasets/{dataset.slug}/"))
    matches = [
        r
        for r in meta["resources"]
        if (name and r["title"] == name) or (prefix and r["title"].startswith(prefix))
    ]
    if not matches:
        wanted = name or f"{prefix}*"
        raise RuntimeError(f"No resource '{wanted}' in dataset {dataset.page}")
    latest = max(matches, key=lambda r: r.get("last_modified") or "")
    logger.info("Resolved %s -> %s", name or f"{prefix}*", latest["url"])
    return latest["url"]


# ------------------------------------------------------------- cleaning


def clean_boundaries(raw: dict[str, Any]) -> dict[str, gpd.GeoDataFrame]:
    """Extract the commune and canton layers from the ACT boundaries file.

    The file is a JSON object holding one FeatureCollection per administrative
    level, with WGS84 coordinates.
    """
    communes = gpd.GeoDataFrame.from_features(raw["communes"]["features"], crs=WGS84)
    communes = communes.rename(
        columns={"COMMUNE": "name", "CANTON": "canton", "DISTRICT": "district", "LAU2": "lau2"}
    )[["name", "canton", "district", "lau2", "geometry"]]
    cantons = gpd.GeoDataFrame.from_features(raw["cantons"]["features"], crs=WGS84)
    cantons = cantons.rename(columns={"CANTON": "name", "DISTRICT": "district"})
    cantons = cantons[["name", "district", "geometry"]]
    return {
        "communes": communes.sort_values("name", ignore_index=True),
        "cantons": cantons.sort_values("name", ignore_index=True),
    }


def stops_from_gtfs(archive: bytes) -> gpd.GeoDataFrame:
    """Build the stops layer from the `stops.txt` table of a GTFS archive."""
    with zipfile.ZipFile(io.BytesIO(archive)) as gtfs, gtfs.open("stops.txt") as table:
        stops = pd.read_csv(table, dtype=str)
    stops["stop_lon"] = pd.to_numeric(stops["stop_lon"], errors="coerce")
    stops["stop_lat"] = pd.to_numeric(stops["stop_lat"], errors="coerce")
    stops = stops.dropna(subset=["stop_lon", "stop_lat"]).drop_duplicates("stop_id")
    gdf = gpd.GeoDataFrame(
        {"stop_id": stops["stop_id"], "name": stops["stop_name"].str.strip()},
        geometry=gpd.points_from_xy(stops["stop_lon"], stops["stop_lat"]),
        crs=WGS84,
    )
    return gdf.sort_values("stop_id", ignore_index=True)


def school_addresses(primary: pd.DataFrame, secondary: pd.DataFrame) -> pd.DataFrame:
    """Merge the two school address tables into `name`, `level`, `address` rows."""
    primary = primary.dropna(subset=["ECOLE", "RUE", "VILLE"]).fillna("")
    street = (primary["NUM"] + " " + primary["RUE"].str.title()).str.strip()
    locality = primary["CPO"] + " " + primary["VILLE"].str.title()
    fondamental = pd.DataFrame(
        {"name": primary["ECOLE"], "level": "fondamental", "address": street + ", " + locality}
    )
    secondary = secondary.dropna(subset=["Lycée", "Adresse", "CP+Localité"])
    lycee = pd.DataFrame(
        {
            "name": secondary["Lycée"],
            "level": "lycee",
            "address": secondary["Adresse"].str.strip() + ", " + secondary["CP+Localité"],
        }
    )
    schools = pd.concat([fondamental, lycee], ignore_index=True)
    schools["name"] = schools["name"].str.strip()
    return schools


def geocode(address: str) -> tuple[float, float, int] | None:
    """Geocode an address in Luxembourg; return (lon, lat, accuracy) or None."""
    query = urllib.parse.urlencode({"queryString": address})
    results = json.loads(http_get(f"{GEOCODER_URL}?{query}")).get("results", [])
    if not results or "geomlonlat" not in results[0]:
        return None
    lon, lat = results[0]["geomlonlat"]["coordinates"]
    return lon, lat, int(results[0].get("accuracy", 0))


def geocode_schools(schools: pd.DataFrame) -> gpd.GeoDataFrame:
    """Add a point geometry to each school; imprecise matches are dropped and logged."""
    rows, points = [], []
    for row in schools.itertuples(index=False):
        match = geocode(row.address)
        time.sleep(GEOCODER_PAUSE_S)
        if match is None or match[2] < MIN_GEOCODE_ACCURACY:
            reason = "no match" if match is None else f"accuracy {match[2]}"
            logger.warning("Dropped '%s' (%s): %s", row.name, row.address, reason)
            continue
        rows.append(row._asdict())
        points.append(match[:2])
    logger.info("Geocoded %d of %d schools", len(rows), len(schools))
    lons, lats = zip(*points, strict=True) if points else ((), ())
    return gpd.GeoDataFrame(rows, geometry=gpd.points_from_xy(lons, lats), crs=WGS84)


# ---------------------------------------------------------------- steps


def fetch_boundaries() -> tuple[dict[str, gpd.GeoDataFrame], list[str]]:
    """Download and clean the administrative boundaries."""
    url = find_resource(DATASETS["boundaries"], name=BOUNDARIES_FILE)
    return clean_boundaries(json.loads(http_get(url))), [url]


def fetch_stops() -> tuple[dict[str, gpd.GeoDataFrame], list[str]]:
    """Download the latest GTFS feed and extract its stops."""
    url = find_resource(DATASETS["gtfs"], prefix=GTFS_PREFIX)
    return {"stops": stops_from_gtfs(http_get(url))}, [url]


def fetch_schools() -> tuple[dict[str, gpd.GeoDataFrame], list[str]]:
    """Download the school address lists and geocode them."""
    urls = [
        find_resource(DATASETS["schools"], name=PRIMARY_SCHOOLS_FILE),
        find_resource(DATASETS["schools"], name=SECONDARY_SCHOOLS_FILE),
    ]
    primary, secondary = (pd.read_excel(io.BytesIO(http_get(u)), dtype=str) for u in urls)
    schools = geocode_schools(school_addresses(primary, secondary))
    return {"schools": schools}, [*urls, GEOCODER_URL]


STEPS = {
    "communes": ("boundaries", fetch_boundaries),
    "stops": ("gtfs", fetch_stops),
    "schools": ("schools", fetch_schools),
}


def main() -> int:
    """Run the selected steps; return a non-zero exit code if any of them failed."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder")
    parser.add_argument("--only", nargs="+", choices=sorted(STEPS), help="steps to run")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("pyogrio").setLevel(logging.WARNING)
    args.out.mkdir(parents=True, exist_ok=True)
    sources_path = args.out / "SOURCES.json"
    sources = json.loads(sources_path.read_text()) if sources_path.exists() else {}

    failed = []
    for step in args.only or STEPS:
        dataset_key, fetch = STEPS[step]
        dataset = DATASETS[dataset_key]
        try:
            layers, urls = fetch()
        except (RuntimeError, KeyError, ValueError, zipfile.BadZipFile) as exc:
            logger.error("Step '%s' failed: %s", step, exc)
            failed.append(step)
            continue
        for name, gdf in layers.items():
            path = args.out / f"{name}.geojson"
            gdf.to_file(path, driver="GeoJSON")
            logger.info("Wrote %s (%d features)", path, len(gdf))
            sources[name] = {
                "dataset": dataset.page,
                "publisher": dataset.publisher,
                "licence": dataset.licence,
                "resources": urls,
                "downloaded": datetime.now(UTC).isoformat(timespec="seconds"),
                "features": len(gdf),
            }
    sources_path.write_text(json.dumps(sources, indent=2, ensure_ascii=False) + "\n")

    if failed:
        logger.error("Failed steps: %s. Check the dataset pages listed in DATASETS.", failed)
        return 1
    logger.info("Done. Start the API with DATA_DIR=%s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
