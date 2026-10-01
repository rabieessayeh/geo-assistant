"""Spatial tools exposed to the LLM.

Every tool is a plain, deterministic Python function working on GeoPandas
layers. The LLM never writes code: it only chooses a tool from the `TOOLS`
whitelist and its arguments, which are validated against a Pydantic model
before anything runs. This keeps answers traceable and reproducible.

Distances are computed in EPSG:2169 (LUREF / Luxembourg TM), a metric
projection for Luxembourg. Results are returned in EPSG:4326 for web maps.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from shapely.geometry import Point

logger = logging.getLogger(__name__)

METRIC_CRS = "EPSG:2169"  # LUREF / Luxembourg TM (metres)
WEB_CRS = "EPSG:4326"
MAX_RESULTS = 50  # attribute rows sent to the LLM per tool call
MAX_MAP_FEATURES = 2000  # features returned as GeoJSON per tool call
LAYER_SUFFIXES = {".geojson", ".gpkg", ".shp"}


class ToolError(ValueError):
    """A tool call that cannot be executed; the message is safe to show the LLM."""


@dataclass
class Catalog:
    """In-memory catalogue of named layers (kept in a metric CRS)."""

    layers: dict[str, gpd.GeoDataFrame] = field(default_factory=dict)

    def add(self, name: str, gdf: gpd.GeoDataFrame) -> None:
        """Register a layer, reprojecting it to the metric CRS."""
        if gdf.crs is None:
            raise ValueError(f"Layer '{name}' has no CRS")
        self.layers[name] = gdf.to_crs(METRIC_CRS).reset_index(drop=True)
        logger.info("Loaded layer '%s' (%d features)", name, len(gdf))

    def get(self, name: str) -> gpd.GeoDataFrame:
        """Return a layer by name, or raise a `ToolError` listing valid names."""
        if name not in self.layers:
            raise ToolError(f"Unknown layer '{name}'. Available: {sorted(self.layers)}")
        return self.layers[name]

    @classmethod
    def from_folder(cls, folder: str | Path) -> Catalog:
        """Load every .geojson / .gpkg / .shp file of a folder as a layer."""
        folder = Path(folder)
        if not folder.is_dir():
            raise FileNotFoundError(
                f"Data folder '{folder}' does not exist. Run scripts/make_sample_data.py "
                "or set DATA_DIR."
            )
        cat = cls()
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() in LAYER_SUFFIXES:
                cat.add(path.stem, gpd.read_file(path))
        if not cat.layers:
            logger.warning("No layer found in '%s'", folder)
        return cat


def to_geojson(gdf: gpd.GeoDataFrame, limit: int = MAX_MAP_FEATURES) -> dict[str, Any]:
    """Serialise up to `limit` features as a WGS84 GeoJSON FeatureCollection.

    Goes through `GeoDataFrame.to_json` so NaN and dates become valid JSON.
    """
    return json.loads(gdf.head(limit).to_crs(WEB_CRS).to_json(na="null", drop_id=True))


def _records(df: pd.DataFrame, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    """Attribute rows (no geometry) as JSON-safe dicts, for the LLM."""
    attributes = df.drop(columns="geometry", errors="ignore").head(limit)
    return json.loads(attributes.to_json(orient="records", date_format="iso"))


# ------------------------------------------------------------ arguments


class _Args(BaseModel):
    """Base class of tool arguments: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class ListLayersArgs(_Args):
    """`list_layers` takes no argument; stray ones sent by some models are ignored."""

    model_config = ConfigDict(extra="ignore")


class WithinDistanceArgs(_Args):
    """Arguments of `within_distance`."""

    target: str = Field(description="Layer whose features are returned.")
    reference: str = Field(description="Layer the distance is measured to.")
    meters: float = Field(gt=0, le=100_000, description="Distance in metres (> 0).")


class CountInPolygonsArgs(_Args):
    """Arguments of `count_in_polygons`."""

    points: str = Field(description="Layer of features to count.")
    polygons: str = Field(description="Polygon layer defining the zones.")
    label: str = Field(description="Attribute of the polygon layer used as zone name.")


class NearestArgs(_Args):
    """Arguments of `nearest`."""

    layer: str = Field(description="Layer to search.")
    lon: float = Field(ge=-180, le=180, description="Longitude of the point (WGS84).")
    lat: float = Field(ge=-90, le=90, description="Latitude of the point (WGS84).")
    k: int = Field(default=3, ge=1, le=MAX_RESULTS, description="Number of features (default 3).")


# ---------------------------------------------------------------- tools


def list_layers(cat: Catalog, args: ListLayersArgs | None = None) -> dict[str, Any]:
    """Describe the available layers: geometry type, feature count, attributes."""
    layers = {
        name: {
            "geometry": sorted(gdf.geom_type.dropna().unique().tolist()),
            "count": int(len(gdf)),
            "attributes": [c for c in gdf.columns if c != "geometry"],
        }
        for name, gdf in cat.layers.items()
    }
    return {"summary": f"{len(layers)} layers available: {', '.join(layers)}.", "layers": layers}


def within_distance(cat: Catalog, args: WithinDistanceArgs) -> dict[str, Any]:
    """Features of `target` located within `meters` of any feature of `reference`."""
    tgt, ref = cat.get(args.target), cat.get(args.reference)
    pairs = gpd.sjoin(
        tgt[["geometry"]], ref[["geometry"]], predicate="dwithin", distance=args.meters
    )
    hits = tgt.loc[pairs.index.unique().sort_values()]
    return {
        "summary": f"{len(hits)} of {len(tgt)} '{args.target}' features are within "
        f"{args.meters:g} m of '{args.reference}'.",
        "count": int(len(hits)),
        # Attributes of the hits, so the LLM can name them (it never sees the GeoJSON).
        "results": _records(hits),
        "results_truncated": len(hits) > MAX_RESULTS,
        "geojson": to_geojson(hits),
    }


def count_in_polygons(cat: Catalog, args: CountInPolygonsArgs) -> dict[str, Any]:
    """Count the features of `points` falling inside each polygon of `polygons`."""
    pts, polys = cat.get(args.points), cat.get(args.polygons)
    if args.label not in polys.columns or args.label == "geometry":
        attributes = [c for c in polys.columns if c != "geometry"]
        raise ToolError(
            f"'{args.label}' is not an attribute of '{args.polygons}'. Available: {attributes}"
        )
    joined = gpd.sjoin(pts[["geometry"]], polys[["geometry"]], predicate="within")
    per_polygon = joined.groupby("index_right").size().reindex(polys.index, fill_value=0)
    zones = polys[[args.label, "geometry"]].assign(count=per_polygon.astype(int))
    zones = zones.sort_values("count", ascending=False, kind="stable")
    # Polygons sharing a label (multi-part zones) are summed under that label.
    counts = zones.groupby(args.label, sort=False)["count"].sum()
    return {
        "summary": f"Number of '{args.points}' per '{args.polygons}' ({args.label}).",
        "counts": {str(k): int(v) for k, v in counts.items()},
        "geojson": to_geojson(zones),
    }


def nearest(cat: Catalog, args: NearestArgs) -> dict[str, Any]:
    """The `k` features of `layer` closest to a WGS84 point (lon, lat)."""
    gdf = cat.get(args.layer)
    origin = gpd.GeoSeries([Point(args.lon, args.lat)], crs=WEB_CRS).to_crs(METRIC_CRS).iloc[0]
    out = gdf.assign(distance_m=gdf.geometry.distance(origin).round(1))
    out = out.nsmallest(args.k, "distance_m")
    return {
        "summary": f"{len(out)} nearest '{args.layer}' features to ({args.lon}, {args.lat}).",
        "results": _records(out),
        "geojson": to_geojson(out),
    }


# ------------------------------------------------------------- registry


@dataclass(frozen=True)
class Tool:
    """A whitelisted tool: its function, argument model and LLM-facing description."""

    func: Callable[[Catalog, Any], dict[str, Any]]
    args_model: type[_Args]
    description: str


TOOLS: dict[str, Tool] = {
    "list_layers": Tool(
        list_layers,
        ListLayersArgs,
        "List available spatial layers with geometry type, count and attributes.",
    ),
    "within_distance": Tool(
        within_distance,
        WithinDistanceArgs,
        "Find features of a target layer within a distance (metres) of a reference layer.",
    ),
    "count_in_polygons": Tool(
        count_in_polygons,
        CountInPolygonsArgs,
        "Count point features inside each polygon (e.g. stops per municipality).",
    ),
    "nearest": Tool(
        nearest,
        NearestArgs,
        "Return the k nearest features of a layer to a WGS84 point.",
    ),
}

# Keys of a JSON schema kept for the LLM. Providers differ in what they accept
# (titles, defaults, numeric bounds...), so only the common subset is sent;
# the bounds are still enforced by the Pydantic models.
_SCHEMA_KEYS = {"type", "description", "properties", "required"}


def _llm_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Reduce a Pydantic JSON schema to the subset every provider understands."""
    schema = model.model_json_schema()
    properties = {
        name: {k: v for k, v in prop.items() if k in _SCHEMA_KEYS}
        for name, prop in schema.get("properties", {}).items()
    }
    out: dict[str, Any] = {"type": "object", "properties": properties}
    if schema.get("required"):
        out["required"] = schema["required"]
    return out


# Tool definitions in the OpenAI tool-calling format, generated from the registry.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": tool.description,
            "parameters": _llm_schema(tool.args_model),
        },
    }
    for name, tool in TOOLS.items()
]


def _validation_message(exc: ValidationError) -> str:
    """One short line per invalid argument, readable by the LLM."""
    problems = [
        f"{'.'.join(str(p) for p in err['loc']) or 'arguments'}: {err['msg']}"
        for err in exc.errors()
    ]
    return "Invalid arguments: " + "; ".join(problems)


def run_tool(cat: Catalog, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute a whitelisted tool; errors are returned as `{"error": ...}`, not raised."""
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"Unknown tool '{name}'. Available: {sorted(TOOLS)}"}
    try:
        return tool.func(cat, tool.args_model.model_validate(args))
    except ValidationError as exc:
        return {"error": _validation_message(exc)}
    except ToolError as exc:
        return {"error": str(exc)}
    except Exception:
        logger.exception("Tool '%s' failed with args %s", name, args)
        return {"error": f"Tool '{name}' failed unexpectedly."}
