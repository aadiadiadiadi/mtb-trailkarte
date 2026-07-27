#!/usr/bin/env python3
"""
Ergänzt Trail-GeoJSON-Dateien um:

    elevation_higher_m
    elevation_lower_m
    elevation_difference_m
    elevation_source
    elevation_status

Bei grenzüberschreitenden Trails wird nur der Linienabschnitt innerhalb
der Schweiz berücksichtigt.

Datenquellen:
- Landesgrenze: swissBOUNDARIES3D von swisstopo
- Höhen: GeoAdmin Height Service / swissALTI3D

Das Skript verändert die Originaldateien nicht. Die angereicherten Dateien
werden mit denselben Dateinamen in einen separaten Ausgabeordner geschrieben.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import threading
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

try:
    import geopandas as gpd
    import requests
    from pyproj import Transformer
    from shapely.geometry import LineString, MultiLineString, Point, shape
    from shapely.ops import linemerge, transform, unary_union
except ImportError as exc:
    print(
        "\nFehlende Python-Bibliothek:\n"
        f"  {exc}\n\n"
        "Benötigt werden: geopandas, shapely, pyproj und requests.\n"
        "Siehe requirements.txt.",
        file=sys.stderr,
    )
    raise SystemExit(2)

APP_NAME = "MTB Trail Höhenanreicherung"
BOUNDARY_DOWNLOAD_URL = (
    "https://data.geo.admin.ch/ch.swisstopo.swissboundaries3d/"
    "swissboundaries3d_2026-01_2056_5728.shp.zip"
)
HEIGHT_URL = "https://api3.geo.admin.ch/rest/services/height"

WGS84_TO_LV95 = Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True)
LV95_TO_WGS84 = Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)

_thread_local = threading.local()


def get_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "MTB-Trailkarte-Hoehenanreicherung/1.0",
                "Accept": "application/json",
            }
        )
        _thread_local.session = session
    return session


def choose_input_folder() -> Path:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        raise SystemExit("Bitte den Eingabeordner mit --input angeben.")

    root = tk.Tk()
    root.withdraw()
    selected = filedialog.askdirectory(
        title="Ordner mit trails-*.geojson auswählen"
    )
    root.destroy()

    if not selected:
        raise SystemExit("Kein Ordner ausgewählt.")
    return Path(selected)


def download_boundary_zip(target: Path) -> Path:
    if target.exists() and target.stat().st_size > 0:
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    print("Lade swissBOUNDARIES3D herunter …")
    print(f"  {BOUNDARY_DOWNLOAD_URL}")

    request = urllib.request.Request(
        BOUNDARY_DOWNLOAD_URL,
        headers={"User-Agent": "MTB-Trailkarte-Hoehenanreicherung/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            with target.open("wb") as output:
                shutil_copyfileobj(response, output)
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise RuntimeError(
            "Die Schweizer Landesgrenze konnte nicht automatisch geladen werden.\n"
            "Lade die aktuelle swissBOUNDARIES3D-Shapefile-ZIP manuell herunter "
            "und übergib sie mit --boundary-zip.\n"
            f"Technischer Fehler: {exc}"
        ) from exc

    return target


def shutil_copyfileobj(source: Any, target: Any, length: int = 1024 * 1024) -> None:
    while True:
        block = source.read(length)
        if not block:
            break
        target.write(block)


def find_country_layer(zip_path: Path) -> tuple[str, list[str]]:
    import fiona

    layers = list(fiona.listlayers(f"zip://{zip_path}"))
    preferred = [
        layer
        for layer in layers
        if "landesgebiet" in layer.lower()
        or "country" in layer.lower()
        or "land_flaeche" in layer.lower()
    ]
    candidates = preferred + [layer for layer in layers if layer not in preferred]

    for layer in candidates:
        try:
            sample = gpd.read_file(f"zip://{zip_path}", layer=layer, rows=3)
        except Exception:
            continue

        if sample.empty or not any(
            geom_type in {"Polygon", "MultiPolygon"}
            for geom_type in sample.geometry.geom_type
        ):
            continue

        return layer, layers

    raise RuntimeError(
        "In der swissBOUNDARIES3D-ZIP wurde keine geeignete Polygon-Ebene gefunden.\n"
        f"Gefundene Ebenen: {', '.join(layers)}"
    )


def select_switzerland(rows: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if rows.empty:
        raise RuntimeError("Die Grenzebene enthält keine Objekte.")

    columns = {column.lower(): column for column in rows.columns}

    # Häufige swissBOUNDARIES3D-Felder.
    for key in ("icc", "country", "iso2", "iso_a2"):
        column = columns.get(key)
        if column:
            selected = rows[
                rows[column].astype(str).str.upper().str.strip().isin({"CH", "CHE"})
            ]
            if not selected.empty:
                return selected

    for key in ("name", "name_de", "land", "country_name"):
        column = columns.get(key)
        if column:
            values = rows[column].astype(str).str.lower()
            selected = rows[
                values.str.contains(
                    r"schweiz|suisse|svizzera|svizra|switzerland",
                    regex=True,
                    na=False,
                )
            ]
            if not selected.empty:
                return selected

    # Falls die Ebene bereits nur die Schweiz enthält, darf sie direkt verwendet
    # werden. Liechtenstein wird über den Schwerpunkt grob ausgeschlossen.
    rows_wgs84 = rows.to_crs("EPSG:4326")
    selected = rows_wgs84[
        rows_wgs84.geometry.centroid.x < 9.53
    ]
    if not selected.empty:
        return selected.to_crs(rows.crs)

    raise RuntimeError(
        "Die Schweiz konnte in der Grenzebene nicht eindeutig erkannt werden."
    )


def load_switzerland_polygon(zip_path: Path):
    layer, all_layers = find_country_layer(zip_path)
    print(f"Verwende Grenzebene: {layer}")

    countries = gpd.read_file(f"zip://{zip_path}", layer=layer)
    countries = countries[countries.geometry.notna()].copy()
    switzerland = select_switzerland(countries)

    if switzerland.crs is None:
        raise RuntimeError("Die Grenzebene besitzt kein Koordinatensystem.")

    switzerland = switzerland.to_crs("EPSG:4326")
    polygon = unary_union(switzerland.geometry)

    if polygon.is_empty:
        raise RuntimeError("Die Schweizer Landesgrenze ist leer.")

    return polygon


def iter_line_parts(geometry) -> Iterable[LineString]:
    if geometry is None or geometry.is_empty:
        return

    if isinstance(geometry, LineString):
        if len(geometry.coords) >= 2:
            yield geometry
        return

    if isinstance(geometry, MultiLineString):
        for part in geometry.geoms:
            if len(part.coords) >= 2:
                yield part
        return

    if geometry.geom_type == "GeometryCollection":
        for child in geometry.geoms:
            yield from iter_line_parts(child)


def main_reference_line(geometry):
    if isinstance(geometry, LineString):
        return geometry

    parts = list(iter_line_parts(geometry))
    if not parts:
        return None

    try:
        merged = linemerge(parts)
        if isinstance(merged, LineString):
            return merged
    except Exception:
        pass

    return max(parts, key=lambda line: line.length)


def swiss_endpoint_pair(geometry, swiss_polygon):
    """
    Liefert die beiden äussersten Punkte des innerhalb der Schweiz liegenden
    Linienabschnitts, gemessen entlang der ursprünglichen Linienrichtung.
    """
    reference = main_reference_line(geometry)
    if reference is None or reference.is_empty:
        return None

    clipped = reference.intersection(swiss_polygon)
    parts = list(iter_line_parts(clipped))
    if not parts:
        return None

    candidates: list[Point] = []
    for part in parts:
        candidates.append(Point(part.coords[0]))
        candidates.append(Point(part.coords[-1]))

    positioned = sorted(
        ((reference.project(point), point) for point in candidates),
        key=lambda item: item[0],
    )

    point_a = positioned[0][1]
    point_b = positioned[-1][1]

    if point_a.equals(point_b):
        return None

    return point_a, point_b


def endpoint_key(point: Point) -> tuple[int, int]:
    easting, northing = WGS84_TO_LV95.transform(point.x, point.y)
    # Dezimeter-Auflösung für Cache und API-Abfrage.
    return round(easting * 10), round(northing * 10)


def key_to_lv95(key: tuple[int, int]) -> tuple[float, float]:
    return key[0] / 10.0, key[1] / 10.0


def open_cache(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS heights (
            easting_dm INTEGER NOT NULL,
            northing_dm INTEGER NOT NULL,
            height_m REAL,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (easting_dm, northing_dm)
        )
        """
    )
    connection.commit()
    return connection


def load_cached_heights(
    connection: sqlite3.Connection,
) -> dict[tuple[int, int], float | None]:
    result: dict[tuple[int, int], float | None] = {}
    for easting, northing, height, status in connection.execute(
        "SELECT easting_dm, northing_dm, height_m, status FROM heights"
    ):
        result[(easting, northing)] = float(height) if status == "ok" else None
    return result


def fetch_height(
    key: tuple[int, int],
    delay: float,
    retries: int = 4,
) -> tuple[tuple[int, int], float | None, str]:
    easting, northing = key_to_lv95(key)

    if delay > 0:
        time.sleep(delay)

    last_error = ""
    for attempt in range(retries):
        try:
            response = get_session().get(
                HEIGHT_URL,
                params={
                    "easting": f"{easting:.1f}",
                    "northing": f"{northing:.1f}",
                    "sr": "2056",
                },
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            raw_height = payload.get("height")

            if raw_height is None:
                return key, None, "no_height"

            height = float(raw_height)
            if not math.isfinite(height):
                return key, None, "no_height"

            return key, height, "ok"
        except Exception as exc:
            last_error = str(exc)
            time.sleep(min(2 ** attempt, 8))

    return key, None, f"error: {last_error[:180]}"


def collect_files(input_dir: Path, pattern: str) -> list[Path]:
    files = sorted(input_dir.glob(pattern))
    if not files and pattern == "trails-*.geojson":
        files = sorted(input_dir.glob("*.geojson"))
    return files


def prepare_features(files: list[Path], swiss_polygon):
    prepared: list[tuple[Path, dict[str, Any], list[dict[str, Any]]]] = []
    all_keys: set[tuple[int, int]] = set()
    total_features = 0
    evaluable = 0

    for file_path in files:
        print(f"Lese {file_path.name} …")
        with file_path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)

        features = document.get("features", [])
        feature_records: list[dict[str, Any]] = []

        for feature in features:
            total_features += 1
            record: dict[str, Any] = {
                "feature": feature,
                "keys": None,
            }

            geometry_data = feature.get("geometry")
            if not geometry_data:
                feature.setdefault("properties", {})[
                    "elevation_status"
                ] = "no_geometry"
                feature_records.append(record)
                continue

            try:
                geometry = shape(geometry_data)
                endpoints = swiss_endpoint_pair(geometry, swiss_polygon)
            except Exception:
                feature.setdefault("properties", {})[
                    "elevation_status"
                ] = "geometry_error"
                feature_records.append(record)
                continue

            if endpoints is None:
                feature.setdefault("properties", {})[
                    "elevation_status"
                ] = "outside_switzerland"
                feature_records.append(record)
                continue

            key_a = endpoint_key(endpoints[0])
            key_b = endpoint_key(endpoints[1])
            record["keys"] = (key_a, key_b)
            all_keys.add(key_a)
            all_keys.add(key_b)
            evaluable += 1
            feature_records.append(record)

        prepared.append((file_path, document, feature_records))

    return prepared, all_keys, total_features, evaluable


def enrich_and_write(
    prepared,
    heights: dict[tuple[int, int], float | None],
    output_dir: Path,
):
    successful = 0
    failed = 0
    outside = 0

    output_dir.mkdir(parents=True, exist_ok=True)

    for source_path, document, feature_records in prepared:
        for record in feature_records:
            feature = record["feature"]
            properties = feature.setdefault("properties", {})
            keys = record["keys"]

            for old_key in (
                "elevation_higher_m",
                "elevation_lower_m",
                "elevation_difference_m",
                "elevation_source",
            ):
                properties.pop(old_key, None)

            if keys is None:
                if properties.get("elevation_status") == "outside_switzerland":
                    outside += 1
                continue

            height_a = heights.get(keys[0])
            height_b = heights.get(keys[1])

            if height_a is None or height_b is None:
                properties["elevation_status"] = "height_unavailable"
                failed += 1
                continue

            lower = round(min(height_a, height_b))
            higher = round(max(height_a, height_b))
            difference = abs(higher - lower)

            properties["elevation_lower_m"] = lower
            properties["elevation_higher_m"] = higher
            properties["elevation_difference_m"] = difference
            properties["elevation_source"] = (
                "swisstopo Height Service / swissALTI3D"
            )
            properties["elevation_status"] = "ok"
            successful += 1

        target = output_dir / source_path.name
        with target.open("w", encoding="utf-8") as handle:
            json.dump(
                document,
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        print(f"Geschrieben: {target}")

    return successful, failed, outside


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Berechnet höheren Punkt, tieferen Punkt und absolute "
            "Höhendifferenz der Schweizer Trailabschnitte."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Ordner mit trails-*.geojson",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Ausgabeordner; Standard: <Eingabe>_mit_hoehen",
    )
    parser.add_argument(
        "--pattern",
        default="trails-*.geojson",
        help="Dateimuster; Standard: trails-*.geojson",
    )
    parser.add_argument(
        "--boundary-zip",
        type=Path,
        help="Lokale swissBOUNDARIES3D-Shapefile-ZIP",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Parallele Höhenabfragen; Standard: 2",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.10,
        help="Pause je Höhenabfrage in Sekunden; Standard: 0.10",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    input_dir = (args.input or choose_input_folder()).resolve()
    if not input_dir.is_dir():
        raise SystemExit(f"Eingabeordner nicht gefunden: {input_dir}")

    output_dir = (
        args.output.resolve()
        if args.output
        else input_dir.with_name(input_dir.name + "_mit_hoehen")
    )

    files = collect_files(input_dir, args.pattern)
    if not files:
        raise SystemExit(
            f"Keine GeoJSON-Dateien in {input_dir} gefunden."
        )

    work_dir = input_dir / ".elevation_work"
    work_dir.mkdir(parents=True, exist_ok=True)

    boundary_zip = (
        args.boundary_zip.resolve()
        if args.boundary_zip
        else work_dir / "swissboundaries3d_latest.shp.zip"
    )
    if not boundary_zip.exists():
        download_boundary_zip(boundary_zip)

    swiss_polygon = load_switzerland_polygon(boundary_zip)

    prepared, endpoint_keys, total, evaluable = prepare_features(
        files, swiss_polygon
    )

    cache_path = work_dir / "height_cache.sqlite"
    connection = open_cache(cache_path)
    cached = load_cached_heights(connection)

    missing = sorted(key for key in endpoint_keys if key not in cached)

    print()
    print(f"GeoJSON-Dateien: {len(files)}")
    print(f"Objekte insgesamt: {total}")
    print(f"Auswertbare Schweizer Trailabschnitte: {evaluable}")
    print(f"Eindeutige Höhenpunkte: {len(endpoint_keys)}")
    print(f"Bereits im Cache: {len(endpoint_keys) - len(missing)}")
    print(f"Noch abzufragen: {len(missing)}")
    print()

    if missing:
        completed = 0
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(fetch_height, key, max(0.0, args.delay)): key
                for key in missing
            }

            for future in as_completed(futures):
                key, height, status = future.result()
                cached[key] = height

                connection.execute(
                    """
                    INSERT INTO heights
                        (easting_dm, northing_dm, height_m, status)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(easting_dm, northing_dm)
                    DO UPDATE SET
                        height_m=excluded.height_m,
                        status=excluded.status,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (key[0], key[1], height, status),
                )
                completed += 1

                if completed % 100 == 0 or completed == len(missing):
                    connection.commit()
                    print(
                        f"Höhenabfragen: {completed}/{len(missing)}",
                        flush=True,
                    )

        connection.commit()

    successful, failed, outside = enrich_and_write(
        prepared, cached, output_dir
    )
    connection.close()

    print()
    print("Fertig.")
    print(f"Erfolgreich angereichert: {successful}")
    print(f"Höhenabfrage fehlgeschlagen: {failed}")
    print(f"Ausserhalb der Schweiz: {outside}")
    print(f"Ausgabeordner: {output_dir}")
    print()
    print(
        "Lade danach die erzeugten trails-*.geojson-Dateien "
        "mit denselben Dateinamen in dein GitHub-Repository."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
