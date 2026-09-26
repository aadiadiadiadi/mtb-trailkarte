#!/usr/bin/env python3
"""Berechnet resumierbare Höhenprofile für die Schweizer MTB-Trails.

Die Trails sind auf ``trails-01.geojson`` bis ``trails-10.geojson`` verteilt.
Der Chunk wird daher aus dem Quelldateinamen abgeleitet; eine Zuordnung über
``osm_way_id % 10`` wäre bei der aktuellen Sortierung nicht korrekt.

Je Chunk entsteht ``elevation-profiles-XX.json``. Nur kompakte Kennwerte werden
in die Trail-GeoJSON-Dateien geschrieben; die Profilpunkte bleiben getrennt.
Der swisstopo Height Service wird über einen SQLite-Punktcache wiederverwendet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import requests


HEIGHT_URL = "https://api3.geo.admin.ch/rest/services/height"
DEFAULT_SPACING_M = 20.0
DEFAULT_REQUEST_DELAY_S = 0.05
EARTH_RADIUS_M = 6_371_008.8
PROFILE_VERSION = 1
CHUNK_IDS = tuple(f"{index:02d}" for index in range(1, 11))
CHECKPOINT_EVERY = 25
PROFILE_SUMMARY_KEYS = (
    "elevation_status",
    "elevation_source",
    "elevation_start_m",
    "elevation_end_m",
    "elevation_min_m",
    "elevation_max_m",
    "elevation_higher_m",
    "elevation_lower_m",
    "elevation_difference_m",
    "elevation_gain_m",
    "elevation_loss_m",
    "average_grade_percent",
    "max_uphill_percent",
    "max_downhill_percent",
    "grade_window_m",
    "elevation_length_m",
    "elevation_profile_ref",
    "elevation_profile_chunk",
    "elevation_profile_version",
    "elevation_profile_hash",
    "elevation_profile",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chunk",
        required=True,
        choices=(*CHUNK_IDS, "alle"),
        help="Chunk-Nummer (01 bis 10) oder 'alle'",
    )
    parser.add_argument("--data-dir", type=Path, default=Path("."), help="Verzeichnis mit trails-XX.geojson")
    parser.add_argument("--output-dir", type=Path, default=Path("."), help="Verzeichnis für Profil-Dateien")
    parser.add_argument("--cache", type=Path, default=Path("elevation-cache.sqlite"), help="SQLite-Höhenpunktcache")
    parser.add_argument("--spacing-m", type=float, default=DEFAULT_SPACING_M)
    parser.add_argument("--request-delay-s", type=float, default=DEFAULT_REQUEST_DELAY_S)
    parser.add_argument("--feature-id", help="Optional nur eine OSM-Feature-ID bearbeiten")
    parser.add_argument("--dry-run", action="store_true", help="Nur Auswahl zählen, keine API-/Dateiaufrufe")
    parser.add_argument("--force", action="store_true", help="Bereits vorhandene Profile neu berechnen")
    return parser.parse_args()


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    dlon, dlat = lon2 - lon1, lat2 - lat1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(value)))


def stable_id(feature: dict[str, Any]) -> str:
    properties = feature.get("properties") or {}
    value = feature.get("id") or properties.get("@id")
    if value:
        return str(value)
    geometry = json.dumps(feature.get("geometry"), sort_keys=True, separators=(",", ":"))
    return "geometry/" + hashlib.sha256(geometry.encode("utf-8")).hexdigest()[:20]


def geometry_hash(coordinates: list[list[float]]) -> str:
    """Stabiler Fingerprint, damit geänderte Geometrien neu berechnet werden."""
    normalized: list[list[float]] = []
    for point in coordinates:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise ValueError("Ungültiger Koordinatenpunkt")
        lon, lat = float(point[0]), float(point[1])
        if not math.isfinite(lon) or not math.isfinite(lat):
            raise ValueError("Ungültiger Koordinatenpunkt")
        normalized.append([round(lon, 6), round(lat, 6)])
    payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_target_trail(feature: dict[str, Any]) -> bool:
    properties = feature.get("properties") or {}
    is_rated = any(
        properties.get(tag) is not None and str(properties.get(tag, "")).strip()
        for tag in ("mtb:scale", "mtb:scale:imba", "mtb:scale:uphill")
    )
    is_legacy_gigeliwald = properties.get("route_name") == "Gigeliwald Trail" or "Gigeliwald Trail" in (
        properties.get("mtb_route_names") or []
    )
    geometry = feature.get("geometry") or {}
    is_line = geometry.get("type") == "LineString" and len(geometry.get("coordinates") or []) >= 2
    return is_line and (is_rated or is_legacy_gigeliwald)


def interpolate(a: list[float], b: list[float], fraction: float) -> tuple[float, float]:
    return (a[0] + (b[0] - a[0]) * fraction, a[1] + (b[1] - a[1]) * fraction)


def sample_line(coordinates: list[list[float]], spacing_m: float) -> list[tuple[float, tuple[float, float]]]:
    if len(coordinates) < 2:
        raise ValueError("Mindestens zwei Koordinaten sind erforderlich")
    geometry_hash(coordinates)
    lengths = [haversine(tuple(a), tuple(b)) for a, b in zip(coordinates, coordinates[1:])]
    total = sum(lengths)
    targets = [0.0]
    cursor = spacing_m
    while cursor < total:
        targets.append(cursor)
        cursor += spacing_m
    targets.append(total)

    result: list[tuple[float, tuple[float, float]]] = []
    segment_index = 0
    segment_start = 0.0
    for target in targets:
        while segment_index < len(lengths) - 1 and target > segment_start + lengths[segment_index]:
            segment_start += lengths[segment_index]
            segment_index += 1
        length = lengths[segment_index]
        fraction = 0.0 if length == 0 else (target - segment_start) / length
        result.append((target, interpolate(coordinates[segment_index], coordinates[segment_index + 1], fraction)))
    return result


def wgs84_to_lv95(lon: float, lat: float) -> tuple[float, float]:
    """Offizielle swisstopo-Näherungsformel WGS84 -> LV95."""
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
        self.connection.commit()
        self.pending = 0

    def get(self, east: float, north: float) -> float | None:
        row = self.connection.execute("SELECT height FROM heights WHERE e10=? AND n10=?", (round(east * 10), round(north * 10))).fetchone()
        return None if row is None else float(row[0])

    def put(self, east: float, north: float, height: float) -> None:
        self.connection.execute("INSERT OR REPLACE INTO heights VALUES (?,?,?)", (round(east * 10), round(north * 10), height))
        self.pending += 1
        if self.pending >= 100:
            self.flush()

    def flush(self) -> None:
        self.connection.commit()
        self.pending = 0

    def close(self) -> None:
        self.flush()
        self.connection.close()


def query_height(session: requests.Session, cache: HeightCache, point: tuple[float, float], request_delay_s: float) -> float | None:
    east, north = wgs84_to_lv95(*point)
    cached = cache.get(east, north)
    if cached is not None:
        return cached
    for attempt in range(5):
        try:
            response = session.get(HEIGHT_URL, params={"easting": east, "northing": north, "sr": 2056}, timeout=30)
            if getattr(response, "status_code", 200) in {400, 404}:
                return None
            response.raise_for_status()
            payload = response.json()
            raw_height = payload.get("height") if isinstance(payload, dict) else None
            if raw_height is None:
                return None
            height = float(raw_height)
            if not math.isfinite(height):
                return None
            cache.put(east, north, height)
            if request_delay_s:
                time.sleep(request_delay_s)
            return height
        except (requests.RequestException, ValueError, TypeError, KeyError):
            if attempt == 4:
                raise
            time.sleep(2**attempt)
    return None


def get_heights(
    session: requests.Session,
    cache: HeightCache,
    samples: list[tuple[float, tuple[float, float]]],
    request_delay_s: float,
) -> list[float]:
    heights: list[float] = []
    for _, point in samples:
        height = query_height(session, cache, point, request_delay_s)
        if height is None:
            raise RuntimeError(f"Keine Höhe für {point[0]:.6f},{point[1]:.6f} verfügbar")
        heights.append(height)
    return heights


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
        result[index] = sum(
            weight * robust[index + offset]
            for offset, weight in zip(range(-2, 3), weights)
        ) / 35
    return result


def calculate_metrics(samples: list[tuple[float, tuple[float, float]]], heights: list[float]) -> dict[str, Any]:
    if not samples or len(samples) != len(heights):
        raise ValueError("Samples und Höhen müssen gleich lang und nicht leer sein")
    profile = [[round(distance), round(height)] for (distance, _), height in zip(samples, heights)]
    deltas = [
        (
            heights[index] - heights[index - 1],
            samples[index][0] - samples[index - 1][0],
        )
        for index in range(1, len(samples))
    ]
    significant = [
        (delta, distance)
        for delta, distance in deltas
        if abs(delta) >= 1.0 and distance > 0
    ]
    grade_window_m = 40.0
    grades: list[float] = []
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
        "elevation_start_m": round(heights[0]),
        "elevation_end_m": round(heights[-1]),
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


def write_json_atomic(path: Path, document: Any, *, indent: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            document,
            ensure_ascii=False,
            indent=indent,
            separators=None if indent else (",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def profile_document(chunk_num: str, spacing_m: float) -> dict[str, Any]:
    return {
        "version": PROFILE_VERSION,
        "chunk": chunk_num,
        "source_file": f"trails-{chunk_num}.geojson",
        "spacing_m": spacing_m,
        "revision": chunk_revision({}),
        "features": {},
    }


def chunk_revision(existing_features: dict[str, Any]) -> str:
    """Kurzer, stabiler Fingerprint des gesamten Chunk-Inhalts."""
    payload = json.dumps(
        existing_features,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def load_existing_profiles(path: Path, chunk_num: str, spacing_m: float) -> dict[str, Any]:
    if not path.exists():
        return profile_document(chunk_num, spacing_m)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Profildatei kann nicht gelesen werden: {path}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("features"), dict):
        raise RuntimeError(f"Ungültige Profildatei: {path}")
    if document.get("version") not in (None, PROFILE_VERSION):
        raise RuntimeError(f"Nicht unterstütztes Profilformat in {path}")
    if not all(
        isinstance(record, dict) and isinstance(record.get("geometry_hash"), str)
        for record in document["features"].values()
    ):
        raise RuntimeError(f"Profildatei enthält ungültige Feature-Einträge: {path}")
    if (
        document.get("chunk") not in (None, chunk_num)
        or document.get("source_file") not in (None, f"trails-{chunk_num}.geojson")
    ):
        raise RuntimeError(f"Chunk-Metadaten passen nicht zu {path}")
    if "spacing_m" in document and float(document["spacing_m"]) != float(spacing_m):
        raise RuntimeError(f"Anderer Abtastabstand in {path}; bitte Ausgabe mit --force und leerer Datei ersetzen")
    document.update(
        {
            "version": PROFILE_VERSION,
            "chunk": chunk_num,
            "source_file": f"trails-{chunk_num}.geojson",
            "spacing_m": spacing_m,
        }
    )
    expected_revision = chunk_revision(document["features"])
    if document.get("revision") not in (None, expected_revision):
        raise RuntimeError(f"Interner Chunk-Fingerprint passt nicht zu {path}")
    document["revision"] = expected_revision
    return document


def is_current_profile(record: Any, feature_hash: str) -> bool:
    return (
        isinstance(record, dict)
        and record.get("geometry_hash") == feature_hash
        and record.get("elevation_status") == "ok"
        and isinstance(record.get("elevation_profile"), list)
        and len(record["elevation_profile"]) >= 2
    )


def summary_properties(
    metrics: dict[str, Any],
    feature_id: str,
    chunk_num: str,
    revision: str,
) -> dict[str, Any]:
    excluded = {
        "elevation_profile_ref",
        "elevation_profile_chunk",
        "elevation_profile_version",
        "elevation_profile_hash",
        "elevation_profile",
    }
    summary = {
        key: metrics[key]
        for key in PROFILE_SUMMARY_KEYS
        if key in metrics and key not in excluded
    }
    summary.update(
        {
            "elevation_lower_m": metrics["elevation_min_m"],
            "elevation_higher_m": metrics["elevation_max_m"],
            "elevation_profile_ref": feature_id,
            "elevation_profile_chunk": chunk_num,
            "elevation_profile_version": revision,
            "elevation_profile_hash": metrics["geometry_hash"],
        }
    )
    return summary


def clear_profile_properties(properties: dict[str, Any]) -> None:
    for key in PROFILE_SUMMARY_KEYS:
        properties.pop(key, None)
    properties["elevation_status"] = "pending"


def apply_profile_summary(
    properties: dict[str, Any],
    record: dict[str, Any],
    feature_id: str,
    chunk_num: str,
    revision: str,
) -> None:
    properties.update(summary_properties(record, feature_id, chunk_num, revision))
    properties.pop("elevation_profile", None)


def refresh_profile_summaries(
    trail_document: dict[str, Any],
    existing_features: dict[str, Any],
    chunk_num: str,
    revision: str,
    clear_ids: set[str],
) -> None:
    """Synchronisiert alle Trail-Kennwerte eines Chunks mit einem Lauf."""
    for feature in trail_document.get("features", []):
        if not isinstance(feature, dict):
            continue
        fid = stable_id(feature)
        record = existing_features.get(fid)
        properties = feature.setdefault("properties", {})
        if not record:
            if fid in clear_ids and any(key in properties for key in PROFILE_SUMMARY_KEYS):
                clear_profile_properties(properties)
            continue
        try:
            feature_hash = geometry_hash(feature["geometry"]["coordinates"])
        except (KeyError, TypeError, ValueError):
            existing_features.pop(fid, None)
            clear_profile_properties(properties)
            continue
        if is_current_profile(record, feature_hash):
            apply_profile_summary(properties, record, fid, chunk_num, revision)
        else:
            existing_features.pop(fid, None)
            clear_profile_properties(properties)


def sync_profile_summaries(
    trail_document: dict[str, Any],
    existing_features: dict[str, Any],
    profile_doc: dict[str, Any],
    chunk_num: str,
    clear_ids: set[str],
) -> str:
    """Entfernt ungültige Einträge und schreibt konsistente Chunk-Kennwerte."""
    refresh_profile_summaries(
        trail_document,
        existing_features,
        chunk_num,
        "pending",
        clear_ids,
    )
    revision = chunk_revision(existing_features)
    profile_doc["revision"] = revision
    refresh_profile_summaries(
        trail_document,
        existing_features,
        chunk_num,
        revision,
        clear_ids,
    )
    return revision


def collect_candidates(trail_file: Path, feature_id: str | None) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any], str]]]:
    try:
        document = json.loads(trail_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Trail-Datei kann nicht gelesen werden: {trail_file}: {exc}") from exc
    features = document.get("features")
    if not isinstance(features, list):
        raise RuntimeError(f"Ungültige Trail-Datei: {trail_file}")
    seen: set[str] = set()
    candidates: list[tuple[str, dict[str, Any], str]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        fid = stable_id(feature)
        if fid in seen:
            raise RuntimeError(f"Doppelte OSM-ID {fid} in {trail_file.name}")
        seen.add(fid)
        if feature_id and fid != feature_id:
            continue
        if is_target_trail(feature):
            candidates.append((fid, feature, geometry_hash(feature["geometry"]["coordinates"])))
    return document, candidates


def process_chunk(chunk_num: str, args: argparse.Namespace, session: requests.Session, cache: HeightCache) -> dict[str, Any]:
    trail_file = args.data_dir / f"trails-{chunk_num}.geojson"
    if not trail_file.exists():
        raise RuntimeError(f"Datei nicht gefunden: {trail_file}")
    trail_document, candidates = collect_candidates(trail_file, args.feature_id)
    if args.feature_id and not candidates:
        raise RuntimeError(f"Feature {args.feature_id} ist in {trail_file.name} nicht als Zieltrail vorhanden")

    out_file = args.output_dir / f"elevation-profiles-{chunk_num}.json"
    profile_doc = load_existing_profiles(out_file, chunk_num, args.spacing_m)
    existing_features: dict[str, Any] = profile_doc["features"]
    pending: list[tuple[str, dict[str, Any], str]] = []
    initial_revision = chunk_revision(existing_features)
    for fid, feature, feature_hash in candidates:
        record = existing_features.get(fid)
        current = is_current_profile(record, feature_hash)
        if current and not args.force:
            apply_profile_summary(
                feature.setdefault("properties", {}),
                record,
                fid,
                chunk_num,
                initial_revision,
            )
        elif current and args.force:
            # Bei --force bleibt der letzte gültige Profilstand als Rückfallebene
            # erhalten, falls die neue Berechnung abbricht.
            pending.append((fid, feature, feature_hash))
        else:
            clear_profile_properties(feature.setdefault("properties", {}))
            existing_features.pop(fid, None)
            pending.append((fid, feature, feature_hash))

    if not args.feature_id:
        candidate_ids = {fid for fid, _, _ in candidates}
        stale_ids = set(existing_features) - candidate_ids
        for stale_id in stale_ids:
            del existing_features[stale_id]
        clear_ids = candidate_ids | stale_ids
    else:
        stale_ids = set()
        clear_ids = {fid for fid, _, _ in candidates}

    already_present = len(candidates) - len(pending)
    print(f"Chunk {chunk_num}: {len(candidates)} Zieltrails, {already_present} vorhanden, {len(pending)} ausstehend")
    if not pending:
        sync_profile_summaries(
            trail_document,
            existing_features,
            profile_doc,
            chunk_num,
            clear_ids,
        )
        write_json_atomic(out_file, profile_doc)
        write_json_atomic(trail_file, trail_document)
        return {
            "selected": len(candidates),
            "already_present": already_present,
            "attempted": 0,
            "calculated": 0,
            "failed": 0,
            "completed_total": len(existing_features),
            "stale_removed": len(stale_ids),
            "failures": [],
        }

    calculated = 0
    failures: list[dict[str, str]] = []
    for index, (fid, feature, feature_hash) in enumerate(pending, 1):
        try:
            samples = sample_line(feature["geometry"]["coordinates"], args.spacing_m)
            metrics = calculate_metrics(samples, smooth(get_heights(session, cache, samples, args.request_delay_s)))
            record = {"source_file": f"trails-{chunk_num}.geojson", "geometry_hash": feature_hash, **metrics}
            existing_features[fid] = record
            calculated += 1
            if index % CHECKPOINT_EVERY == 0 or index == len(pending):
                sync_profile_summaries(
                    trail_document,
                    existing_features,
                    profile_doc,
                    chunk_num,
                    clear_ids,
                )
                write_json_atomic(out_file, profile_doc)
                write_json_atomic(trail_file, trail_document)
                print(f"  [{index}/{len(pending)}] {fid}: {metrics['elevation_length_m']} m, {len(samples)} Punkte")
        except Exception as exc:
            failures.append({"feature_id": fid, "error": str(exc)})
            print(f"  FEHLER {fid}: {exc}", file=sys.stderr)

    sync_profile_summaries(
        trail_document,
        existing_features,
        profile_doc,
        chunk_num,
        clear_ids,
    )
    write_json_atomic(out_file, profile_doc)
    write_json_atomic(trail_file, trail_document)
    return {
        "selected": len(candidates),
        "already_present": already_present,
        "attempted": len(pending),
        "calculated": calculated,
        "failed": len(failures),
        "completed_total": len(existing_features),
        "stale_removed": len(stale_ids),
        "failures": failures,
    }


def main() -> int:
    args = parse_args()
    if args.spacing_m <= 0 or args.request_delay_s < 0:
        raise SystemExit("Abstand muss positiv und die Abfragepause darf nicht negativ sein.")
    chunks = list(CHUNK_IDS) if args.chunk == "alle" else [args.chunk]

    if args.dry_run:
        for chunk_num in chunks:
            trail_file = args.data_dir / f"trails-{chunk_num}.geojson"
            if not trail_file.exists():
                raise SystemExit(f"Datei nicht gefunden: {trail_file}")
            _, candidates = collect_candidates(trail_file, args.feature_id)
            if args.feature_id and not candidates:
                raise SystemExit(f"Feature {args.feature_id} ist in {trail_file.name} nicht als Zieltrail vorhanden")
            print(f"Chunk {chunk_num}: {len(candidates)} Zieltrails")
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    cache = HeightCache(args.cache)
    session = requests.Session()
    session.headers.update({"User-Agent": "mtb-trailkarte-switzerland-profile-calculator/1.0", "Accept": "application/json"})
    totals = {"selected": 0, "already_present": 0, "attempted": 0, "calculated": 0, "failed": 0}
    fatal_error: str | None = None
    try:
        for chunk_num in chunks:
            try:
                result = process_chunk(chunk_num, args, session, cache)
            except Exception as exc:
                fatal_error = str(exc)
                print(f"FEHLER Chunk {chunk_num}: {exc}", file=sys.stderr)
                break
            for key in totals:
                totals[key] += result[key]
            write_json_atomic(
                args.output_dir / f"summary-{chunk_num}.json",
                {"version": PROFILE_VERSION, "chunk": chunk_num, "source_file": f"trails-{chunk_num}.geojson", "spacing_m": args.spacing_m, **result},
                indent=2,
            )
    finally:
        cache.close()
        session.close()

    print(f"Fertig: {totals['calculated']} Profile berechnet, {totals['failed']} Fehler, {totals['selected']} Zieltrails ausgewählt")
    if fatal_error:
        print(f"Abbruch: {fatal_error}", file=sys.stderr)
        return 1
    return 1 if totals["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())