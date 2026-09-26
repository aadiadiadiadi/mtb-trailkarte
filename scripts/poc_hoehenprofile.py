#!/usr/bin/env python3
"""Resumierbare Höhenprofil-Berechnung für klar begrenzte Schweizer Gebiete.

Der PoC verwendet bewusst den offiziellen swisstopo Height Service, weil die
Auswahl klein ist. Für den Vollbestand sind lokale swissALTI3D-Raster geplant.
Original-GeoJSON-Dateien werden ausschließlich gelesen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import statistics
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import requests


HEIGHT_URL = "https://api3.geo.admin.ch/rest/services/height"
DEFAULT_SPACING_M = 20.0
DEFAULT_REQUEST_DELAY_S = 0.05
ROUTE_NAME = "Gigeliwald Trail"
EARTH_RADIUS_M = 6_371_008.8
AREAS = {
    "guetschwald": {"center": (8.29021, 47.05012), "radius_m": 1_200.0},
    "luzern": {"center": (8.30931, 47.05017), "radius_m": 15_000.0},
    "haute-nendaz": {"center": (7.29400, 46.18300), "radius_m": 15_000.0},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=Path("."))
    parser.add_argument("--output-dir", type=Path, default=Path("poc-output"))
    parser.add_argument("--cache", type=Path, default=Path("poc-output/elevation-cache.sqlite"))
    parser.add_argument("--spacing-m", type=float, default=DEFAULT_SPACING_M)
    parser.add_argument("--request-delay-s", type=float, default=DEFAULT_REQUEST_DELAY_S)
    parser.add_argument(
        "--areas", nargs="+", choices=tuple(AREAS), default=list(AREAS),
        help="Zu verarbeitende Gebiete (Standard: alle).",
    )
    parser.add_argument("--feature-id", help="Optional nur eine OSM-Feature-ID, z. B. way/1225944161")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Bereits vorhandene Profile neu berechnen")
    return parser.parse_args()


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    dlon, dlat = lon2 - lon1, lat2 - lat1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(value)))


def point_segment_distance(point, a, b) -> float:
    """Lokale metrische Näherung für die räumliche Vorauswahl."""
    lon0, lat0 = point
    scale_x = 111_320.0 * math.cos(math.radians(lat0))
    px, py = 0.0, 0.0
    ax, ay = (a[0] - lon0) * scale_x, (a[1] - lat0) * 110_540.0
    bx, by = (b[0] - lon0) * scale_x, (b[1] - lat0) * 110_540.0
    dx, dy = bx - ax, by - ay
    denominator = dx * dx + dy * dy
    t = 0.0 if denominator == 0 else max(0.0, min(1.0, (-(ax * dx + ay * dy)) / denominator))
    return math.hypot(ax + t * dx - px, ay + t * dy - py)


def feature_distance(feature: dict[str, Any], center: tuple[float, float]) -> float:
    geometry = feature.get("geometry") or {}
    if geometry.get("type") != "LineString":
        return math.inf
    coordinates = geometry.get("coordinates") or []
    if len(coordinates) == 1:
        return haversine(center, tuple(coordinates[0]))
    return min(point_segment_distance(center, a, b) for a, b in zip(coordinates, coordinates[1:]))


def stable_id(feature: dict[str, Any]) -> str:
    properties = feature.get("properties") or {}
    value = feature.get("id") or properties.get("@id")
    if value:
        return str(value)
    geometry = json.dumps(feature.get("geometry"), sort_keys=True, separators=(",", ":"))
    return "geometry/" + hashlib.sha256(geometry.encode()).hexdigest()[:20]


def is_gigeliwald(feature: dict[str, Any]) -> bool:
    properties = feature.get("properties") or {}
    return properties.get("route_name") == ROUTE_NAME or ROUTE_NAME in (properties.get("mtb_route_names") or [])


def is_tagged_mtb_trail(feature: dict[str, Any]) -> bool:
    properties = feature.get("properties") or {}
    return any(properties.get(tag) not in (None, "") for tag in ("mtb:scale", "mtb:scale:imba", "mtb:scale:uphill"))


def matching_areas(feature: dict[str, Any], area_names: list[str]) -> list[str]:
    if not is_tagged_mtb_trail(feature):
        return []
    return [
        name for name in area_names
        if feature_distance(feature, AREAS[name]["center"]) <= AREAS[name]["radius_m"]
    ]


def load_selection(input_dir: Path, area_names: list[str], requested_id: str | None):
    selected: dict[str, tuple[dict[str, Any], str, list[str]]] = {}
    for path in sorted(input_dir.glob("trails-[0-9][0-9].geojson")):
        document = json.loads(path.read_text(encoding="utf-8"))
        for feature in document.get("features", []):
            feature_id = stable_id(feature)
            if requested_id and feature_id != requested_id:
                continue
            areas = matching_areas(feature, area_names)
            if requested_id or is_gigeliwald(feature) or areas:
                selected[feature_id] = (feature, path.name, areas or ["gigeliwald-route"])
    return selected


def read_existing_profiles(path: Path, spacing_m: float) -> dict[str, Any]:
    if not path.exists():
        return {"version": 2, "spacing_m": spacing_m, "areas": {}, "features": {}}
    document = json.loads(path.read_text(encoding="utf-8"))
    if float(document.get("spacing_m", spacing_m)) != spacing_m:
        raise SystemExit("Vorhandene Profildatei verwendet einen anderen Abtastabstand; --force und getrennte Ausgabe verwenden.")
    document["version"] = 2
    document.setdefault("areas", {})
    document.setdefault("features", {})
    return document


def write_json_atomic(path: Path, document: Any, *, indent: int | None = None) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=indent, separators=None if indent else (",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def interpolate(a, b, fraction: float):
    return (a[0] + (b[0] - a[0]) * fraction, a[1] + (b[1] - a[1]) * fraction)


def sample_line(coordinates: list[list[float]], spacing_m: float):
    lengths = [haversine(tuple(a), tuple(b)) for a, b in zip(coordinates, coordinates[1:])]
    total = sum(lengths)
    targets = [0.0]
    cursor = spacing_m
    while cursor < total:
        targets.append(cursor)
        cursor += spacing_m
    if total > 0:
        targets.append(total)
    if len(targets) == 1:
        targets.append(0.0)

    result = []
    segment_index = 0
    segment_start = 0.0
    for target in targets:
        while segment_index < len(lengths) - 1 and target > segment_start + lengths[segment_index]:
            segment_start += lengths[segment_index]
            segment_index += 1
        length = lengths[segment_index] if lengths else 0.0
        fraction = 0.0 if length == 0 else (target - segment_start) / length
        result.append((target, interpolate(coordinates[segment_index], coordinates[segment_index + 1], fraction)))
    return result


def wgs84_to_lv95(lon: float, lat: float) -> tuple[float, float]:
    """Offizielle swisstopo-Näherungsformel, Genauigkeit im Meterbereich."""
    lat_seconds = lat * 3600
    lon_seconds = lon * 3600
    lat_aux = (lat_seconds - 169_028.66) / 10_000
    lon_aux = (lon_seconds - 26_782.5) / 10_000
    east = 2_600_072.37 + 211_455.93 * lon_aux - 10_938.51 * lon_aux * lat_aux - 0.36 * lon_aux * lat_aux**2 - 44.54 * lon_aux**3
    north = 1_200_147.07 + 308_807.95 * lat_aux + 3_745.25 * lon_aux**2 + 76.63 * lat_aux**2 - 194.56 * lon_aux**2 * lat_aux + 119.79 * lat_aux**3
    return east, north


class HeightCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("CREATE TABLE IF NOT EXISTS heights (e10 INTEGER, n10 INTEGER, height REAL NOT NULL, PRIMARY KEY(e10,n10))")

    def get(self, east: float, north: float) -> float | None:
        row = self.connection.execute("SELECT height FROM heights WHERE e10=? AND n10=?", (round(east * 10), round(north * 10))).fetchone()
        return None if row is None else float(row[0])

    def put(self, east: float, north: float, height: float) -> None:
        self.connection.execute("INSERT OR REPLACE INTO heights VALUES (?,?,?)", (round(east * 10), round(north * 10), height))
        self.connection.commit()


def query_height(session: requests.Session, cache: HeightCache, point, request_delay_s: float) -> float:
    east, north = wgs84_to_lv95(*point)
    cached = cache.get(east, north)
    if cached is not None:
        return cached
    for attempt in range(5):
        try:
            response = session.get(HEIGHT_URL, params={"easting": east, "northing": north, "sr": 2056}, timeout=30)
            response.raise_for_status()
            height = float(response.json()["height"])
            if not math.isfinite(height):
                raise ValueError("Ungültige Höhe")
            cache.put(east, north, height)
            if request_delay_s:
                time.sleep(request_delay_s)
            return height
        except Exception:
            if attempt == 4:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("Höhenabfrage fehlgeschlagen")


def smooth(values: list[float]) -> list[float]:
    if len(values) < 3:
        return values[:]
    robust = values[:]
    for index in range(1, len(values) - 1):
        robust[index] = statistics.median(values[index - 1:index + 2])
    if len(values) < 5:
        return robust
    result = robust[:]
    weights = (-3, 12, 17, 12, -3)  # Savitzky-Golay, Fenster 5, Grad 2
    for index in range(2, len(values) - 2):
        result[index] = sum(weight * robust[index + offset] for offset, weight in zip(range(-2, 3), weights)) / 35
    return result


def calculate_metrics(samples, heights):
    profile = [[round(distance), round(height)] for (distance, _), height in zip(samples, heights)]
    deltas = [(heights[i] - heights[i - 1], samples[i][0] - samples[i - 1][0]) for i in range(1, len(samples))]
    significant = [(delta, distance) for delta, distance in deltas if abs(delta) >= 1.0 and distance > 0]
    # Extreme Steigungen nicht aus nur einem 20-m-Paar ableiten. Ein gleitendes
    # Fenster von mindestens 40 m ist robuster gegen lokale Rasterabweichungen.
    grade_window_m = 40.0
    grades = []
    for start in range(len(samples) - 1):
        for end in range(start + 1, len(samples)):
            distance = samples[end][0] - samples[start][0]
            if distance >= grade_window_m or end == len(samples) - 1:
                if distance > 0:
                    grades.append(100 * (heights[end] - heights[start]) / distance)
                break
    length = samples[-1][0]
    return {
        "elevation_source": "swisstopo Height Service / swissALTI3D",
        "elevation_status": "ok",
        "elevation_min_m": round(min(heights)),
        "elevation_max_m": round(max(heights)),
        "elevation_difference_m": round(max(heights) - min(heights)),
        "elevation_gain_m": round(sum(max(delta, 0) for delta, _ in significant)),
        "elevation_loss_m": round(sum(max(-delta, 0) for delta, _ in significant)),
        "average_grade_percent": round(100 * (heights[-1] - heights[0]) / length, 1) if length else 0.0,
        "max_uphill_percent": round(max((grade for grade in grades if grade > 0), default=0.0), 1),
        "max_downhill_percent": round(min((grade for grade in grades if grade < 0), default=0.0), 1),
        "grade_window_m": grade_window_m,
        "elevation_length_m": round(length),
        "elevation_profile": profile,
    }


def main() -> int:
    args = parse_args()
    if args.spacing_m <= 0 or args.request_delay_s < 0:
        raise SystemExit("Abstand muss positiv und die Abfragepause darf nicht negativ sein.")
    selected = load_selection(args.input_dir, args.areas, args.feature_id)
    print(f"Ausgewählt: {len(selected)} Features")
    area_counts = {name: sum(name in areas for _, _, areas in selected.values()) for name in args.areas}
    print("Gebiete:", json.dumps(area_counts, ensure_ascii=False))
    if len(selected) <= 250:
        for feature_id, (feature, source, areas) in selected.items():
            print(f"  {feature_id} · {source} · {','.join(areas)} · {(feature.get('properties') or {}).get('route_name') or (feature.get('properties') or {}).get('name') or 'unbenannt'}")
    if args.dry_run:
        return 0
    if not selected:
        raise SystemExit("Keine passenden Features gefunden.")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    cache = HeightCache(args.cache)
    session = requests.Session()
    session.headers.update({"User-Agent": "mtb-trailkarte-elevation-poc/1.0", "Accept": "application/json"})
    profiles_path = args.output_dir / "elevation-profiles.json"
    profiles = read_existing_profiles(profiles_path, args.spacing_m)
    profiles["areas"].update({
        name: {"center": list(AREAS[name]["center"]), "radius_m": AREAS[name]["radius_m"]}
        for name in args.areas
    })
    existing_ids = set(profiles["features"])
    pending = selected if args.force else {key: value for key, value in selected.items() if key not in existing_ids}
    print(f"Bereits vorhanden: {len(selected) - len(pending)} · Neu zu berechnen: {len(pending)}")
    output_by_id: dict[str, dict[str, Any]] = {}
    selected_path = args.output_dir / "selected-trails.geojson"
    if selected_path.exists():
        for feature in json.loads(selected_path.read_text(encoding="utf-8")).get("features", []):
            output_by_id[stable_id(feature)] = feature
    failures = []
    started = time.monotonic()

    for number, (feature_id, (feature, source, areas)) in enumerate(pending.items(), 1):
        try:
            coordinates = (feature.get("geometry") or {}).get("coordinates") or []
            samples = sample_line(coordinates, args.spacing_m)
            raw = [query_height(session, cache, point, args.request_delay_s) for _, point in samples]
            smoothed = smooth(raw)
            metrics = calculate_metrics(samples, smoothed)
            enriched = deepcopy(feature)
            enriched.setdefault("properties", {}).update({key: value for key, value in metrics.items() if key != "elevation_profile"})
            enriched["properties"]["elevation_profile_ref"] = feature_id
            enriched["properties"]["elevation_areas"] = areas
            output_by_id[feature_id] = enriched
            profiles["features"][feature_id] = {"source_file": source, "areas": areas, **metrics}
            print(f"[{number}/{len(pending)}] {feature_id}: {metrics['elevation_length_m']} m, {len(samples)} Punkte")
            if number % 25 == 0:
                write_json_atomic(selected_path, {"type": "FeatureCollection", "features": list(output_by_id.values())})
                write_json_atomic(profiles_path, profiles)
        except Exception as exc:
            failures.append({"feature_id": feature_id, "error": str(exc)})
            print(f"FEHLER {feature_id}: {exc}")

    collection = {"type": "FeatureCollection", "features": list(output_by_id.values())}
    write_json_atomic(selected_path, collection)
    write_json_atomic(profiles_path, profiles)
    summary = {
        "selected": len(selected), "already_present": len(selected) - len(pending),
        "attempted": len(pending), "completed_total": len(profiles["features"]), "failed": len(failures),
        "spacing_m": args.spacing_m, "areas": profiles["areas"], "area_counts": area_counts,
        "duration_seconds": round(time.monotonic() - started, 1), "failures": failures,
        "note": "Auswahl nach Distanz zur Liniengeometrie; verarbeitet werden nur MTB-bewertete LineStrings.",
    }
    write_json_atomic(args.output_dir / "summary.json", summary, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())