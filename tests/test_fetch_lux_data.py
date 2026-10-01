"""Cleaning functions of scripts/fetch_lux_data.py, on small in-memory inputs (no download)."""

import io
import zipfile

import pandas as pd
import pytest

from scripts import fetch_lux_data


@pytest.fixture
def fetch():
    return fetch_lux_data


def _square(x: float, y: float) -> dict:
    ring = [[x, y], [x + 0.1, y], [x + 0.1, y + 0.1], [x, y + 0.1], [x, y]]
    return {"type": "MultiPolygon", "coordinates": [[ring]]}


def test_clean_boundaries(fetch):
    commune = {"COMMUNE": "Mersch", "CANTON": "Mersch", "DISTRICT": "Luxembourg", "LAU2": "0407"}
    raw = {
        "communes": {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": commune, "geometry": _square(6.1, 49.7)}
            ],
        },
        "cantons": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"CANTON": "Mersch", "DISTRICT": "Luxembourg", "LAU1": "04"},
                    "geometry": _square(6.0, 49.7),
                }
            ],
        },
    }
    layers = fetch.clean_boundaries(raw)
    assert list(layers["communes"].columns) == ["name", "canton", "district", "lau2", "geometry"]
    assert list(layers["cantons"].columns) == ["name", "district", "geometry"]
    assert layers["communes"].crs.to_epsg() == 4326
    assert layers["communes"].loc[0, "lau2"] == "0407"


def test_stops_from_gtfs(fetch):
    stops_txt = (
        "stop_id,stop_name,stop_lat,stop_lon,location_type\n"
        "2,Centre Hamilius ,49.6113,6.1263,0\n"
        "1,Gare Centrale,49.5999,6.1340,0\n"
        "1,Gare Centrale (duplicate),49.5999,6.1340,0\n"
        "3,No coordinates,,,0\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("stops.txt", stops_txt)
    stops = fetch.stops_from_gtfs(buffer.getvalue())
    assert stops["stop_id"].tolist() == ["1", "2"]
    assert stops["name"].tolist() == ["Gare Centrale", "Centre Hamilius"]
    assert (stops.geometry.x.iloc[0], stops.geometry.y.iloc[0]) == (6.1340, 49.5999)


def test_school_addresses(fetch):
    primary = pd.DataFrame(
        {
            "ECO_ID": ["511", "776"],
            "ECOLE": ["Angsber Schoul", "Schoul um Kiemel "],
            "CPO": ["L-7410", "L-9780"],
            "VILLE": ["ANGELSBERG", "WINCRANGE"],
            "RUE": ["RUE DE SCHOOS", "MAISON"],
            "NUM": ["16", None],
        }
    )
    secondary = pd.DataFrame(
        {
            "Lycée": ["Athénée de Luxembourg", None],
            "CP+Localité": ["L-1430 Luxembourg", None],
            "Adresse": ["24, bd Pierre Dupong ", None],
        }
    )
    schools = fetch.school_addresses(primary, secondary)
    assert schools.to_dict(orient="records") == [
        {
            "name": "Angsber Schoul",
            "level": "fondamental",
            "address": "16 Rue De Schoos, L-7410 Angelsberg",
        },
        {"name": "Schoul um Kiemel", "level": "fondamental", "address": "Maison, L-9780 Wincrange"},
        {
            "name": "Athénée de Luxembourg",
            "level": "lycee",
            "address": "24, bd Pierre Dupong, L-1430 Luxembourg",
        },
    ]


def test_geocode_schools_drops_imprecise_matches(fetch, monkeypatch):
    answers = {"good": (6.11, 49.60, 8), "street only": (6.12, 49.62, 6), "unknown": None}
    monkeypatch.setattr(fetch, "geocode", answers.get)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    schools = pd.DataFrame(
        {"name": ["A", "B", "C"], "level": "lycee", "address": ["good", "street only", "unknown"]}
    )
    out = fetch.geocode_schools(schools)
    assert out["name"].tolist() == ["A"]
    assert list(out.columns) == ["name", "level", "address", "geometry"]
    assert (out.geometry.x.iloc[0], out.geometry.y.iloc[0]) == (6.11, 49.60)
