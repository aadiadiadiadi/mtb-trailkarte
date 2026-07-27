#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

import geopandas as gpd
import requests
from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString, Point, shape
from shapely.ops import linemerge, unary_union

HEIGHT_URL = "https://api3.geo.admin.ch/rest/services/height"
STAC_ITEMS_URL = (
    "https://data.geo.admin.ch/api/stac/v0.9/collections/"
    "ch.swisstopo.swissboundaries3d/items"
)

WGS84_TO_LV95 = Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True)

session = requests.Session()
session.headers.update({
    "User-Agent": "mtb-trailkarte-height-enrichment/1.0",
    "Accept": "application/json",
})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--selection",
        default="alle",
        choices=["alle", "01", "02", "03", "04", "05", "06", "07", "08", "09", "10"],
    )
    return parser.parse_args()


def trail_files(selection: str) -> list[Path]:
    if selection == "alle":
        files = sorted(Path(".").glob("trails-[0-9][0-9].geojson"))
    else:
        files = [Path(f"trails-{selection}.geojson")]

    missing = [str(path) for path in files if not path.exists()]
    if missing:
        raise SystemExit("Nicht gefunden: " + ", ".join(missing))
    if not files:
        raise SystemExit("Keine trails-XX.geojson-Dateien gefunden.")
    return files


def discover_boundary_asset() -> str:
    response = session.get(STAC_ITEMS_URL, params={"limit": 100}, timeout=60)
    response.raise_for_status()
    items = response.json().get("features", [])
    if not items:
        raise RuntimeError("Keine swissBOUNDARIES3D-STAC-Objekte gefunden.")

    candidates: list[tuple[str, str]] = []
    for item in items:
        dt = (
            item.get("properties", {}).get("datetime")
            or item.get("properties", {}).get("start_datetime")
            or item.get("id", "")
        )
        for asset in item.get("assets", {}).values():
            href = asset.get("href", "")
            title = (asset.get("title") or "").lower()
            media = (asset.get("type") or "").lower()
            haystack = f"{href} {title} {media}".lower()
            if href.endswith(".zip") and (
                "shp" in haystack or "shape" in haystack
            ):
                candidates.append((str(dt), href))

    if not candidates:
        raise RuntimeError(
            "Keine Shapefile-ZIP in swissBOUNDARIES3D gefunden."
        )

    candidates.sort(reverse=True)
    return candidates[0][1]


def download(url: str, target: Path) -> None:
    print(f"Lade Landesgrenze: {url}")
    with session.get(url, stream=True, timeout=180) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    handle.write(chunk)


def load_switzerland_polygon():
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "swissboundaries3d.zip"
        download(discover_boundary_asset(), zip_path)

        import fiona
        layers = list(fiona.listlayers(f"zip://{zip_path}"))
        preferred = [
            layer for layer in layers
            if "landesgebiet" in layer.lower()
            or "country" in layer.lower()
            or "land_flaeche" in layer.lower()
        ]
        ordered = preferred + [x for x in layers if x not in preferred]

        selected_layer = None
        for layer in ordered:
            try:
                sample = gpd.read_file(f"zip://{zip_path}", layer=layer, rows=5)
            except Exception:
                continue
            if not sample.empty and any(
                typ in {"Polygon", "MultiPolygon"}
                for typ in sample.geometry.geom_type
            ):
                selected_layer = layer
                break

        if selected_layer is None:
            raise RuntimeError("Keine geeignete Grenzebene gefunden.")

        print(f"Verwende Grenzebene: {selected_layer}")
        data = gpd.read_file(f"zip://{zip_path}", layer=selected_layer)
        data = data[data.geometry.notna()].copy()
        if data.crs is None:
            raise RuntimeError("Grenzebene hat kein Koordinatensystem.")
        data = data.to_crs("EPSG:4326")

        selected = None
        for col in data.columns:
            if col == data.geometry.name:
                continue
            vals = data[col].astype(str).str.lower()
            mask = vals.str.contains(
                r"schweiz|suisse|svizzera|svizra|switzerland|\bch\b|\bche\b",
                regex=True,
                na=False,
            )
            if mask.any():
                subset = data[mask]
                if not subset.empty:
                    selected = subset
                    break

        if selected is None:
            # swissBOUNDARIES3D enthält zusätzlich Liechtenstein.
            # Diese Fallback-Selektion schliesst dessen Geometrien über die
            # Lage des Schwerpunktes aus.
            centers = data.geometry.centroid
            selected = data[centers.x < 9.53]

        polygon = unary_union(selected.geometry)
        if polygon.is_empty:
            raise RuntimeError("Schweizer Landesfläche ist leer.")
        return polygon


def line_parts(geom) -> Iterable[LineString]:
    if geom is None or geom.is_empty:
        return
    if isinstance(geom, LineString):
        if len(geom.coords) >= 2:
            yield geom
    elif isinstance(geom, MultiLineString):
        for part in geom.geoms:
            if len(part.coords) >= 2:
                yield part
    elif geom.geom_type == "GeometryCollection":
        for child in geom.geoms:
            yield from line_parts(child)


def main_line(geom):
    if isinstance(geom, LineString):
        return geom
    parts = list(line_parts(geom))
    if not parts:
        return None
    try:
        merged = linemerge(parts)
        if isinstance(merged, LineString):
            return merged
    except Exception:
        pass
    return max(parts, key=lambda x: x.length)


def swiss_endpoints(geom, country):
    ref = main_line(geom)
    if ref is None or ref.is_empty:
        return None

    clipped = ref.intersection(country)
    parts = list(line_parts(clipped))
    if not parts:
        return None

    points: list[Point] = []
    for part in parts:
        points.append(Point(part.coords[0]))
        points.append(Point(part.coords[-1]))

    positioned = sorted((ref.project(p), p) for p in points)
    a = positioned[0][1]
    b = positioned[-1][1]
    if a.equals(b):
        return None
    return a, b


def point_key(point: Point) -> tuple[int, int]:
    east, north = WGS84_TO_LV95.transform(point.x, point.y)
    return round(east * 10), round(north * 10)


def query_height(key: tuple[int, int]) -> tuple[tuple[int, int], float | None]:
    east = key[0] / 10
    north = key[1] / 10

    for attempt in range(5):
        try:
            r = session.get(
                HEIGHT_URL,
                params={"easting": east, "northing": north, "sr": 2056},
                timeout=30,
            )
            r.raise_for_status()
            value = r.json().get("height")
            if value is None:
                return key, None
            value = float(value)
            return key, value if math.isfinite(value) else None
        except Exception as exc:
            if attempt == 4:
                print(f"WARNUNG: Höhe fehlgeschlagen {east},{north}: {exc}")
                return key, None
            time.sleep(2 ** attempt)
    return key, None


def process_file(path: Path, country) -> None:
    print(f"\nVerarbeite {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    features = document.get("features", [])

    records = []
    keys: set[tuple[int, int]] = set()

    for feature in features:
        props = feature.setdefault("properties", {})
        for name in (
            "elevation_higher_m",
            "elevation_lower_m",
            "elevation_difference_m",
            "elevation_source",
            "elevation_status",
        ):
            props.pop(name, None)

        geom_data = feature.get("geometry")
        if not geom_data:
            props["elevation_status"] = "no_geometry"
            records.append((feature, None))
            continue

        try:
            endpoints = swiss_endpoints(shape(geom_data), country)
        except Exception:
            props["elevation_status"] = "geometry_error"
            records.append((feature, None))
            continue

        if endpoints is None:
            props["elevation_status"] = "outside_switzerland"
            records.append((feature, None))
            continue

        pair = (point_key(endpoints[0]), point_key(endpoints[1]))
        keys.update(pair)
        records.append((feature, pair))

    print(f"Eindeutige Höhenpunkte: {len(keys)}")
    heights: dict[tuple[int, int], float | None] = {}

    # Moderate Parallelität: schnell genug, ohne den Dienst unnötig zu belasten.
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(query_height, key) for key in keys]
        for i, future in enumerate(as_completed(futures), start=1):
            key, value = future.result()
            heights[key] = value
            if i % 500 == 0 or i == len(futures):
                print(f"Höhenabfragen: {i}/{len(futures)}")

    ok = 0
    failed = 0
    outside = 0

    for feature, pair in records:
        props = feature["properties"]
        if pair is None:
            if props.get("elevation_status") == "outside_switzerland":
                outside += 1
            continue

        h1 = heights.get(pair[0])
        h2 = heights.get(pair[1])
        if h1 is None or h2 is None:
            props["elevation_status"] = "height_unavailable"
            failed += 1
            continue

        low = round(min(h1, h2))
        high = round(max(h1, h2))
        props["elevation_lower_m"] = low
        props["elevation_higher_m"] = high
        props["elevation_difference_m"] = abs(high - low)
        props["elevation_source"] = "swisstopo Height Service"
        props["elevation_status"] = "ok"
        ok += 1

    tmp = path.with_suffix(".geojson.tmp")
    tmp.write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    tmp.replace(path)

    print(
        f"Fertig: {ok} angereichert, {failed} ohne Höhe, "
        f"{outside} ausserhalb der Schweiz."
    )


def main() -> int:
    args = parse_args()
    files = trail_files(args.selection)
    country = load_switzerland_polygon()

    for path in files:
        process_file(path, country)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
