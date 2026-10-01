"""Spatial tools: results, argument validation and error handling."""

import geopandas as gpd
import pytest
from shapely.geometry import Point

from app.tools import TOOL_SCHEMAS, TOOLS, Catalog, run_tool, to_geojson


def test_list_layers(cat):
    res = run_tool(cat, "list_layers", {})
    assert res["summary"] == "3 layers available: communes, stops, schools."
    assert res["layers"]["stops"] == {"geometry": ["Point"], "count": 3, "attributes": ["name"]}
    # Some models send a placeholder argument to tools that take none.
    assert "error" not in run_tool(cat, "list_layers", {"dummy": ""})


def test_within_distance(cat):
    res = run_tool(
        cat, "within_distance", {"target": "schools", "reference": "stops", "meters": 200}
    )
    assert res["count"] == 1
    assert res["results"] == [{"name": "near"}]
    assert res["geojson"]["features"][0]["properties"]["name"] == "near"


def test_within_distance_counts_each_feature_once(cat):
    # Every stop is within 10 km of several schools but must be reported once.
    res = run_tool(
        cat, "within_distance", {"target": "stops", "reference": "schools", "meters": 10_000}
    )
    assert res["count"] == 3
    assert [r["name"] for r in res["results"]] == ["s1", "s2", "s3"]


def test_count_in_polygons(cat):
    res = run_tool(
        cat, "count_in_polygons", {"points": "stops", "polygons": "communes", "label": "name"}
    )
    assert res["counts"] == {"A": 2, "B": 1}
    # The polygons come back with their count, so the map can shade them.
    props = [f["properties"] for f in res["geojson"]["features"]]
    assert props == [{"name": "A", "count": 2}, {"name": "B", "count": 1}]
    assert res["geojson"]["features"][0]["geometry"]["type"] == "Polygon"


def test_count_in_polygons_keeps_empty_zones(cat):
    res = run_tool(
        cat, "count_in_polygons", {"points": "schools", "polygons": "communes", "label": "name"}
    )
    assert res["counts"] == {"A": 1, "B": 1}
    cat.add("empty", cat.get("stops").iloc[0:0])
    res = run_tool(
        cat, "count_in_polygons", {"points": "empty", "polygons": "communes", "label": "name"}
    )
    assert res["counts"] == {"A": 0, "B": 0}


def test_nearest(cat):
    res = run_tool(cat, "nearest", {"layer": "stops", "lon": 6.11, "lat": 49.61, "k": 1})
    assert res["results"][0]["name"] == "s1"
    assert res["results"][0]["distance_m"] < 1


def test_nearest_distances_are_metric(cat):
    # s1 -> s2 is 0.01 deg in both axes: about 1.33 km at this latitude.
    res = run_tool(cat, "nearest", {"layer": "stops", "lon": 6.11, "lat": 49.61, "k": 2})
    assert res["results"][1]["name"] == "s2"
    assert res["results"][1]["distance_m"] == pytest.approx(1326, abs=10)


def test_results_are_returned_in_wgs84(cat):
    res = run_tool(cat, "nearest", {"layer": "stops", "lon": 6.11, "lat": 49.61, "k": 1})
    lon, lat = res["geojson"]["features"][0]["geometry"]["coordinates"]
    assert (lon, lat) == pytest.approx((6.11, 49.61), abs=1e-6)


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        ("unknown_tool", {}, "Unknown tool"),
        ("within_distance", {"target": "nope", "reference": "stops", "meters": 100}, "nope"),
        ("within_distance", {"target": "schools", "reference": "stops", "meters": -5}, "meters"),
        ("within_distance", {"target": "schools", "reference": "stops"}, "meters"),
        ("within_distance", {"target": "schools", "reference": "stops", "meters": "far"}, "meters"),
        ("nearest", {"layer": "stops", "lon": 6.1, "lat": 49.6, "k": 0}, "k"),
        ("nearest", {"layer": "stops", "lon": 6.1, "lat": 149.6}, "lat"),
        ("nearest", {"layer": "stops", "lon": 6.1, "lat": 49.6, "code": "import os"}, "code"),
        ("count_in_polygons", {"points": "stops", "polygons": "communes", "label": "x"}, "'x'"),
    ],
)
def test_errors_are_returned_not_raised(cat, name, args, expected):
    res = run_tool(cat, name, args)
    assert set(res) == {"error"}
    assert expected in res["error"]


def test_schemas_match_the_whitelist():
    assert [s["function"]["name"] for s in TOOL_SCHEMAS] == list(TOOLS)
    within = TOOL_SCHEMAS[1]["function"]["parameters"]
    assert within["required"] == ["target", "reference", "meters"]
    assert within["properties"]["meters"]["type"] == "number"
    # `k` has a default, so it is optional for the LLM.
    assert TOOL_SCHEMAS[3]["function"]["parameters"]["required"] == ["layer", "lon", "lat"]


def test_geojson_handles_missing_values():
    gdf = gpd.GeoDataFrame(
        {"name": ["a", None], "capacity": [10.0, float("nan")]},
        geometry=[Point(6.1, 49.6), Point(6.2, 49.7)],
        crs="EPSG:4326",
    )
    props = [f["properties"] for f in to_geojson(gdf)["features"]]
    assert props[1] == {"name": None, "capacity": None}


def test_catalog_from_folder(tmp_path):
    gdf = gpd.GeoDataFrame({"name": ["a"]}, geometry=[Point(6.1, 49.6)], crs="EPSG:4326")
    gdf.to_file(tmp_path / "places.geojson", driver="GeoJSON")
    (tmp_path / "notes.txt").write_text("ignored")
    assert list(Catalog.from_folder(tmp_path).layers) == ["places"]
    with pytest.raises(FileNotFoundError, match="does not exist"):
        Catalog.from_folder(tmp_path / "missing")


def test_catalog_rejects_layers_without_crs():
    with pytest.raises(ValueError, match="no CRS"):
        Catalog().add("bad", gpd.GeoDataFrame({"name": ["a"]}, geometry=[Point(0, 0)]))
