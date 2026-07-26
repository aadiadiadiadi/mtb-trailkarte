#!/usr/bin/env python3
"""Create MTB trail GeoJSON for Switzerland and enrich missing incline values.

Data selection:
- Geofabrik Switzerland extract (polygon-based)
- Ways carrying mtb:scale, mtb:scale:imba or mtb:scale:uphill
- All way members of relations tagged type=route + route=mtb
- Geofabrik's extract keeps crossing ways reference-complete, so ways crossing
  the Swiss border remain complete.

Incline priority:
1. Existing OSM incline tag
2. Elevation profile from the official geo.admin.ch profile service
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import math
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import osmium
import requests
from pyproj import Transformer

PROFILE_URL = "https://api3.geo.admin.ch/rest/services/profile.json"
TRANSFORM = Transformer.from_crs(4326, 2056, always_xy=True)
USER_AGENT = os.environ.get("MTB_USER_AGENT", "mtb-trailkarte-github-action/2.0")
APP_VERSION = "2.0.0"
MTB_KEYS = ("mtb:scale", "mtb:scale:imba", "mtb:scale:uphill")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def file_timestamp(pbf: Path) -> str | None:
    try:
        reader = osmium.io.Reader(str(pbf))
        header = reader.header()
        value = header.get("osmosis_replication_timestamp") or header.get("timestamp")
        reader.close()
        return value or None
    except Exception:
        return None


class RouteCollector(osmium.SimpleHandler):
    def __init__(self) -> None:
        super().__init__()
        self.way_routes: dict[int, list[dict[str, str]]] = defaultdict(list)

    def relation(self, rel: osmium.osm.Relation) -> None:
        tags = dict(rel.tags)
        if tags.get("type") != "route" or tags.get("route") != "mtb":
            return
        info = {
            "id": str(rel.id),
            "name": tags.get("name", ""),
            "ref": tags.get("ref", ""),
            "network": tags.get("network", ""),
            "operator": tags.get("operator", ""),
        }
        for member in rel.members:
            if member.type == "w":
                self.way_routes[member.ref].append(info)


class TrailCollector(osmium.SimpleHandler):
    def __init__(self, way_routes: dict[int, list[dict[str, str]]]) -> None:
        super().__init__()
        self.way_routes = way_routes
        self.features: list[dict[str, Any]] = []
        self.invalid_geometry = 0

    def way(self, way: osmium.osm.Way) -> None:
        tags = dict(way.tags)
        routes = self.way_routes.get(way.id, [])
        rated = any(str(tags.get(k, "")).strip() for k in MTB_KEYS)
        if not rated and not routes:
            return
        coords: list[list[float]] = []
        try:
            for node in way.nodes:
                if not node.location.valid():
                    raise ValueError("invalid node location")
                coords.append([node.lon, node.lat])
        except Exception:
            self.invalid_geometry += 1
            return
        if len(coords) < 2:
            self.invalid_geometry += 1
            return
        props: dict[str, Any] = dict(tags)
        props["@id"] = f"way/{way.id}"
        props["osm_way_id"] = way.id
        if routes:
            props["route"] = "mtb"
            props["mtb_route"] = True
            props["mtb_route_ids"] = [x["id"] for x in routes]
            names = [x["name"] for x in routes if x["name"]]
            refs = [x["ref"] for x in routes if x["ref"]]
            if names:
                props["mtb_route_names"] = names
                props.setdefault("route_name", " / ".join(dict.fromkeys(names)))
            if refs:
                props["mtb_route_refs"] = refs
        self.features.append({
            "type": "Feature",
            "id": f"way/{way.id}",
            "properties": props,
            "geometry": {"type": "LineString", "coordinates": coords},
        })


def line_distance_m(coords: list[list[float]]) -> float:
    total = 0.0
    for a, b in zip(coords, coords[1:]):
        lat = math.radians((a[1] + b[1]) / 2)
        dx = (b[0] - a[0]) * 111_320 * math.cos(lat)
        dy = (b[1] - a[1]) * 110_540
        total += math.hypot(dx, dy)
    return total


def sample_line(coords: list[list[float]], max_vertices: int = 80) -> list[list[float]]:
    if len(coords) <= max_vertices:
        return coords
    return [coords[round(i * (len(coords) - 1) / (max_vertices - 1))] for i in range(max_vertices)]


def feature_key(feature: dict[str, Any]) -> str:
    p = feature.get("properties") or {}
    raw = str(p.get("@id", "")) + json.dumps(feature.get("geometry"), separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def parse_osm_incline(raw: Any) -> float | None:
    text = str(raw or "").strip().lower().replace(",", ".")
    if not text:
        return None
    if text in {"up", "down", "steep", "yes", "no"}:
        return None
    if text.endswith("°"):
        try:
            return round(math.tan(math.radians(float(text[:-1]))) * 100, 1)
        except ValueError:
            return None
    text = text.rstrip("%")
    try:
        return round(float(text), 1)
    except ValueError:
        return None


def osm_incline_values(props: dict[str, Any]) -> None:
    raw = props.get("incline")
    props["incline_source"] = "OSM"
    numeric = parse_osm_incline(raw)
    if numeric is not None:
        props["incline_avg_percent"] = numeric
        props["incline_max_percent"] = abs(numeric)


def dem_profile(feature: dict[str, Any]) -> dict[str, Any] | None:
    coords = (feature.get("geometry") or {}).get("coordinates") or []
    if len(coords) < 2 or line_distance_m(coords) < 3:
        return None
    sampled = sample_line(coords)
    lv95 = [list(TRANSFORM.transform(lon, lat)) for lon, lat, *_ in sampled]
    payload = {"type": "LineString", "coordinates": lv95}
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    for attempt in range(5):
        try:
            response = session.post(
                PROFILE_URL,
                data={"geom": json.dumps(payload), "sr": "2056", "nb_points": "80", "distinct_points": "true"},
                timeout=60,
            )
            if response.status_code in {429, 500, 502, 503, 504}:
                raise RuntimeError(f"temporary HTTP {response.status_code}")
            response.raise_for_status()
            points = []
            for item in response.json():
                altitude = (item.get("alts") or {}).get("COMB")
                if altitude is not None:
                    points.append((float(item.get("dist", 0)), float(altitude)))
            if len(points) < 2:
                return None
            gain = loss = 0.0
            grades: list[float] = []
            for (d1, h1), (d2, h2) in zip(points, points[1:]):
                distance = d2 - d1
                height = h2 - h1
                if distance <= 1:
                    continue
                grade = 100 * height / distance
                grades.append(grade)
                if height > 0:
                    gain += height
                else:
                    loss -= height
            total = max(points[-1][0] - points[0][0], 0.1)
            signed = 100 * (points[-1][1] - points[0][1]) / total
            # A single noisy DEM point can exaggerate a maximum. Use the 95th percentile
            # of absolute segment grades when enough samples are available.
            absolute = sorted(abs(x) for x in grades)
            if absolute:
                idx = min(len(absolute) - 1, round(0.95 * (len(absolute) - 1)))
                max_grade = absolute[idx]
            else:
                max_grade = 0.0
            return {
                "incline": f"{signed:.1f}%",
                "incline_avg_percent": round(signed, 1),
                "incline_max_percent": round(max_grade, 1),
                "elevation_gain_m": round(gain),
                "elevation_loss_m": round(loss),
                "incline_source": "DEM (swisstopo)",
            }
        except Exception as exc:
            if attempt == 4:
                print(f"DEM failed for {(feature.get('properties') or {}).get('@id')}: {exc}")
                return None
            time.sleep(min(30, 2 ** attempt))
    return None


def save_json(path: Path, value: Any, compact: bool = False) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":") if compact else None, indent=None if compact else 2),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pbf", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    parser.add_argument("--chunks", type=int, default=10)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-dem", type=int, default=500,
                        help="Maximum new DEM profiles per run; 0 means all")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    print("Collecting MTB route relations …")
    routes = RouteCollector()
    routes.apply_file(str(args.pbf))
    print(f"Route member ways: {len(routes.way_routes)}")

    print("Collecting trail ways and geometries …")
    trails = TrailCollector(routes.way_routes)
    trails.apply_file(str(args.pbf), locations=True, idx="flex_mem")
    features = trails.features
    print(f"Selected features: {len(features)}; invalid geometries: {trails.invalid_geometry}")

    cache_path = args.output_dir / "elevation-cache.json"
    try:
        cache: dict[str, Any] = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    except Exception:
        cache = {}

    missing: list[tuple[str, dict[str, Any]]] = []
    osm_count = dem_count = 0
    for feature in features:
        props = feature["properties"]
        if str(props.get("incline", "")).strip():
            osm_incline_values(props)
            osm_count += 1
            continue
        key = feature_key(feature)
        cached = cache.get(key)
        if cached:
            props.update(cached)
            dem_count += 1
        else:
            missing.append((key, feature))

    to_process = missing if args.max_dem == 0 else missing[: max(0, args.max_dem)]
    print(f"OSM inclines: {osm_count}; cached DEM: {dem_count}; new DEM requests: {len(to_process)}; pending: {len(missing)-len(to_process)}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(dem_profile, feature): (key, feature) for key, feature in to_process}
        for number, future in enumerate(concurrent.futures.as_completed(futures), 1):
            key, feature = futures[future]
            result = future.result()
            cache[key] = result
            if result:
                feature["properties"].update(result)
                dem_count += 1
            if number % 50 == 0:
                print(f"DEM completed {number}/{len(to_process)}")
                save_json(cache_path, cache, compact=True)
    save_json(cache_path, cache, compact=True)

    features.sort(key=lambda f: int((f.get("properties") or {}).get("osm_way_id", 0)))
    updated_at = utc_now()
    osm_timestamp = file_timestamp(args.pbf)
    route_count = sum(1 for f in features if (f.get("properties") or {}).get("mtb_route"))
    rated_count = sum(1 for f in features if any(str((f.get("properties") or {}).get(k, "")).strip() for k in MTB_KEYS))
    pending_count = sum(1 for f in features if not str((f.get("properties") or {}).get("incline", "")).strip())
    meta = {
        "app_version": APP_VERSION,
        "data_version": updated_at,
        "updated_at": updated_at,
        "osm_data_timestamp": osm_timestamp,
        "source": "OpenStreetMap (Geofabrik Schweiz) + swisstopo Höhenprofil",
        "trail_count": rated_count,
        "route_count": route_count,
        "feature_count": len(features),
        "incline_osm_count": osm_count,
        "incline_dem_count": dem_count,
        "incline_pending_count": pending_count,
        "coverage": "Schweiz; grenzüberschreitende Wege vollständig",
    }
    save_json(args.output_dir / "data-meta.json", meta)

    for old in args.output_dir.glob("trails-*.geojson"):
        old.unlink()
    for index in range(args.chunks):
        collection = {
            "type": "FeatureCollection",
            "metadata": {"updated_at": updated_at, "data_version": updated_at},
            "features": features[index::args.chunks],
        }
        save_json(args.output_dir / f"trails-{index+1:02d}.geojson", collection, compact=True)
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
