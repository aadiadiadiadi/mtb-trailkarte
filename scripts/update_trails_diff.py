#!/usr/bin/env python3
"""Differentieller OSM-Traildaten-Aktualisierer für die Schweizer MTB-Trailkarte.

Dieses Skript vergleicht den aktuellen Geofabrik-OSM-Auszug (switzerland-latest.osm.pbf)
mit dem bestehenden GeoJSON-Bestand (trails-01.geojson bis trails-10.geojson).

Vorteile des differentiellen Ansatzes:
1. Trails ohne Änderungen werden 1:1 beibehalten.
2. Reine Tag-Änderungen (z.B. Name, Surface, MTB-Grade) aktualisieren nur die Attribute.
   Bereits berechnete Höhenangaben/Höhenprofile bleiben vollständig erhalten!
3. Nur Trails mit veränderter Geometrie oder neu hinzugekommene Trails werden
   für eine Neuberechnung der Höhen profile markiert/abgefragt.
4. In OSM entfernte Trails (oder entfallene MTB-Auszeichnung) werden bereinigt.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    import osmium
except ImportError:
    osmium = None

MTB_KEYS = ("mtb:scale", "mtb:scale:imba", "mtb:scale:uphill")
APP_VERSION = "2.4.2"

# Attribute, die durch Vorverarbeitung/Berechnung hinzukommen und bei Tag-Updates
# nicht von OSM überschrieben werden dürfen
ENRICHED_PROP_KEYS = {
        "incline_avg_percent",
    "incline_max_percent",
    "incline_source",
    "elevation_gain_m",
    "elevation_loss_m",
    "elevation_lower_m",
    "elevation_higher_m",
    "elevation_difference_m",
    "elevation_source",
    "elevation_status",
    "elevation_start_m",
    "elevation_end_m",
    "elevation_min_m",
    "elevation_max_m",
    "average_grade_percent",
    "max_uphill_percent",
    "max_downhill_percent",
    "grade_window_m",
    "elevation_length_m",
    "elevation_profile_ref",
    "elevation_profile_chunk",
    "elevation_profile_version",
    "elevation_profile_hash",
}


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def geometry_hash(coordinates: list[list[float]]) -> str:
    """Erzeugt einen deterministischen Hash der Koordinatenkette (auf 6 Nachkommastellen gerundet)."""
    rounded = [[round(pt[0], 6), round(pt[1], 6)] for pt in coordinates]
    raw = json.dumps(rounded, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def parse_osm_incline(raw: Any) -> float | None:
    text = str(raw or "").strip().lower().replace(",", ".")
    if not text or text in {"up", "down", "steep", "yes", "no"}:
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


def load_existing_trails(directory: Path) -> dict[str, dict[str, Any]]:
    """Lädt alle existierenden Trails aus trails-*.geojson und indiziert sie nach Way-ID."""
    existing: dict[str, dict[str, Any]] = {}
    for p in sorted(directory.glob("trails-[0-9][0-9].geojson")):
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
            for feat in data.get("features", []):
                fid = feat.get("id") or feat.get("properties", {}).get("@id")
                if fid:
                    existing[fid] = feat
        except Exception as exc:
            print(f"Warnung beim Laden von {p.name}: {exc}", file=sys.stderr)
    return existing


def load_existing_chunk_map(directory: Path, chunk_count: int) -> dict[str, int]:
    """Merkt sich die bisherige Chunk-Zuordnung bestehender OSM-IDs.

    Die Chunk-Dateien dürfen nicht alle nachfolgenden IDs verschieben, wenn ein
    neuer Trail hinzukommt.
    Neue IDs werden nach ``((osm_way_id - 1) % chunk_count) + 1`` verteilt;
    bestehende IDs behalten ihre Datei. So bleiben vorhandene Profil-Dateien
    auch nach einem differentiellen OSM-Update gültig.
    """
    chunk_map: dict[str, int] = {}
    for index in range(1, chunk_count + 1):
        path = directory / f"trails-{index:02d}.geojson"
        if not path.exists():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            for feature in document.get("features", []):
                fid = feature.get("id") or (feature.get("properties") or {}).get("@id")
                if fid:
                    chunk_map[str(fid)] = index
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Warnung beim Lesen der Chunk-Zuordnung aus {path.name}: {exc}", file=sys.stderr)
    return chunk_map


if osmium is not None:

    class OsmRouteCollector(osmium.SimpleHandler):
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

    class OsmTrailCollector(osmium.SimpleHandler):
        def __init__(self, way_routes: dict[int, list[dict[str, str]]]) -> None:
            super().__init__()
            self.way_routes = way_routes
            self.features: list[dict[str, Any]] = []
            self.invalid_geometry = 0

        def way(self, way: osmium.osm.Way) -> None:
            tags = dict(way.tags)
            routes = self.way_routes.get(way.id, [])
            rated = any(
                tags.get(k) is not None and str(tags.get(k, "")).strip()
                for k in MTB_KEYS
            )
            if not rated and not routes:
                return
            coords: list[list[float]] = []
            try:
                for node in way.nodes:
                    if not node.location.valid():
                        raise ValueError("ungültige Node-Location")
                    coords.append([node.lon, node.lat])
            except Exception:
                self.invalid_geometry += 1
                return
            if len(coords) < 2:
                self.invalid_geometry += 1
                return

            props: dict[str, Any] = dict(tags)
            fid = f"way/{way.id}"
            props["@id"] = fid
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
                "id": fid,
                "properties": props,
                "geometry": {"type": "LineString", "coordinates": coords},
            })


def save_json(path: Path, value: Any, compact: bool = True) -> None:
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        if compact:
            json.dump(value, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
    tmp.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aktualisiert Schweizer MTB-Traildaten differentiell gegen OSM-PBF."
    )
    parser.add_argument(
        "pbf",
        type=Path,
        help="Pfad zum Geofabrik OSM PBF-Auszug (z.B. switzerland-latest.osm.pbf)",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path("."),
        help="Arbeitsverzeichnis mit den GeoJSON-Dateien (Standard: aktuelles Verzeichnis)",
    )
    parser.add_argument(
        "--chunks",
        type=int,
        default=10,
        help="Anzahl der Ausgabedateien (Standard: 10)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Nur analysieren und Differenzen ausgeben, keine Dateien überschreiben",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.chunks <= 0:
        raise SystemExit("Die Chunk-Anzahl muss grösser als 0 sein.")

    if osmium is None:
        print(
            "Fehler: Das Python-Paket 'osmium' ist nicht installiert.\n"
            "Bitte installiere es mit: pip install osmium",
            file=sys.stderr,
        )
        sys.exit(1)

    if not args.pbf.exists():
        print(f"Fehler: PBF-Datei nicht gefunden: {args.pbf}", file=sys.stderr)
        sys.exit(1)

    work_dir = args.dir.resolve()
    print(f"Lade bestehenden Datenbestand aus {work_dir}...")
    existing_trails = load_existing_trails(work_dir)
    print(f"-> {len(existing_trails)} bestehende Trails geladen.")
    existing_chunk_map = load_existing_chunk_map(work_dir, args.chunks)
    print(f"-> {len(existing_chunk_map)} bestehende Chunk-Zuordnungen geladen.")

    # Vorbereiten des Geometrie-Hash-Lookups für den Bestand
    existing_geom_hashes: dict[str, str] = {}
    for fid, feat in existing_trails.items():
        coords = feat.get("geometry", {}).get("coordinates") or []
        if coords:
            existing_geom_hashes[fid] = geometry_hash(coords)

    # 1. PBF parsen: Relationen sammeln
    print(f"Lese MTB-Routen aus {args.pbf.name}...")
    route_collector = OsmRouteCollector()
    route_collector.apply_file(str(args.pbf))
    print(f"-> {len(route_collector.way_routes)} Wegebeteiligungen in Relationen gefunden.")

    # 2. PBF parsen: Trails extrahieren
    print("Lese Trail-Geometrien und Tags...")
    trail_collector = OsmTrailCollector(route_collector.way_routes)
    trail_collector.apply_file(str(args.pbf), locations=True, idx="flex_mem")
    new_features = trail_collector.features
    print(f"-> {len(new_features)} Trails aus aktuellem OSM extrahiert.")
    if trail_collector.invalid_geometry:
        print(f"-> {trail_collector.invalid_geometry} ungültige Geometrien übersprungen.")

    # 3. Differentieller Vergleich
    print("\nFühre differentiellen Abgleich durch...")
    stats = {
        "identical": 0,
        "tags_updated": 0,
        "geometry_changed": 0,
        "new_trails": 0,
        "deleted_trails": 0,
    }

    final_features: list[dict[str, Any]] = []
    new_fids = set()

    for feat in new_features:
        fid = feat["id"]
        new_fids.add(fid)
        coords = feat["geometry"]["coordinates"]
        ghash = geometry_hash(coords)

        if fid not in existing_trails:
            # Komplett neuer Trail
            stats["new_trails"] += 1
            # Prüfen ob OSM-incline Tag vorliegt
            raw_inc = feat["properties"].get("incline")
            parsed_inc = parse_osm_incline(raw_inc)
            if parsed_inc is not None:
                feat["properties"]["incline_source"] = "OSM"
                feat["properties"]["incline_avg_percent"] = parsed_inc
                feat["properties"]["incline_max_percent"] = abs(parsed_inc)
            final_features.append(feat)
        else:
            old_feat = existing_trails[fid]
            old_ghash = existing_geom_hashes.get(fid)

            if old_ghash == ghash:
                # Geometrie identisch: OSM-relevante Tags vergleichen.
                old_props = old_feat.get("properties", {})
                new_props = feat["properties"]

                # Berechnete Attribute werden beim Vergleich ignoriert.
                tags_changed = False
                for k, v in new_props.items():
                    if k not in ENRICHED_PROP_KEYS and old_props.get(k) != v:
                        tags_changed = True
                        break
                if not tags_changed:
                    for k in old_props:
                        if k not in ENRICHED_PROP_KEYS and k not in new_props:
                            tags_changed = True
                            break

                # Bestehende angereicherte Höhendaten beibehalten!
                merged_props = dict(new_props)
                for k in ENRICHED_PROP_KEYS:
                    if k in old_props:
                        merged_props[k] = old_props[k]
                # Ein berechneter DEM-Incline bleibt erhalten, wenn OSM den
                # Rohwert entfernt; ein neuer OSM-Wert hat dagegen Vorrang.
                if "incline" not in new_props and "incline" in old_props:
                    merged_props["incline"] = old_props["incline"]

                # Falls OSM-incline neu gesetzt wurde, hat OSM Vorrang
                raw_inc = new_props.get("incline")
                if raw_inc and old_props.get("incline_source") != "OSM":
                    parsed_inc = parse_osm_incline(raw_inc)
                    if parsed_inc is not None:
                        merged_props["incline_source"] = "OSM"
                        merged_props["incline_avg_percent"] = parsed_inc
                        merged_props["incline_max_percent"] = abs(parsed_inc)

                if tags_changed:
                    stats["tags_updated"] += 1
                else:
                    stats["identical"] += 1

                final_features.append({
                    "type": "Feature",
                    "id": fid,
                    "properties": merged_props,
                    "geometry": old_feat["geometry"],
                })
            else:
                # Geometrie hat sich geändert!
                stats["geometry_changed"] += 1
                # Neue Geometrie übernehmen. Alte Höhendaten und Profilreferenzen
                # verwerfen, damit das nächste Profil-Lauf sie neu berechnet.
                raw_inc = feat["properties"].get("incline")
                parsed_inc = parse_osm_incline(raw_inc)
                if parsed_inc is not None:
                    feat["properties"]["incline_source"] = "OSM"
                    feat["properties"]["incline_avg_percent"] = parsed_inc
                    feat["properties"]["incline_max_percent"] = abs(parsed_inc)
                final_features.append(feat)

    # Gelöschte Trails zählen
    deleted_fids = set(existing_trails.keys()) - new_fids
    stats["deleted_trails"] = len(deleted_fids)

    print("\n" + "=" * 50)
    print("DIFF-ERGEBNIS:")
    print(f"  • Unverändert (100% Cache):        {stats['identical']:>6}")
    print(f"  • Nur Tags aktualisiert:           {stats['tags_updated']:>6} (Höhendaten behalten!)")
    print(f"  • Geometrie geändert:              {stats['geometry_changed']:>6} (Höhendaten zurückgesetzt)")
    print(f"  • Neu hinzugekommen:               {stats['new_trails']:>6}")
    print(f"  • In OSM entfernt / kein MTB mehr: {stats['deleted_trails']:>6}")
    print(f"  Total neue Trail-Anzahl:           {len(final_features):>6}")
    print("=" * 50 + "\n")

    if args.dry_run:
        print("Dry-Run aktiviert. Es wurden keine Dateien verändert.")
        return

    # Nach osm_way_id sortieren für konsistentes Chunkergebnis
    final_features.sort(key=lambda f: int(f.get("properties", {}).get("osm_way_id", 0)))

    now_iso = utc_now()
    route_count = sum(1 for f in final_features if f.get("properties", {}).get("mtb_route"))
    rated_count = sum(
        1
        for f in final_features
        if any(
            f.get("properties", {}).get(k) is not None
            and str(f.get("properties", {}).get(k, "")).strip()
            for k in MTB_KEYS
        )
    )
    osm_inc_count = sum(
        1 for f in final_features if f.get("properties", {}).get("incline_source") == "OSM"
    )
    dem_inc_count = sum(
        1
        for f in final_features
        if f.get("properties", {}).get("incline_source") == "DEM (swisstopo)"
    )
    pending_count = sum(
        1 for f in final_features if not str(f.get("properties", {}).get("incline", "")).strip()
    )

    meta_path = work_dir / "data-meta.json"
    meta = {
        "app_version": APP_VERSION,
        "data_version": now_iso,
        "updated_at": now_iso,
        "osm_data_timestamp": None,
        "source": "OpenStreetMap (Geofabrik Schweiz) + swisstopo Höhenprofil",
        "trail_count": rated_count,
        "route_count": route_count,
        "feature_count": len(final_features),
        "incline_osm_count": osm_inc_count,
        "incline_dem_count": dem_inc_count,
        "incline_pending_count": pending_count,
        "coverage": "Schweiz; grenzüberschreitende Wege vollständig",
        "elevation_method": "20-m-Höhenprofil mit 40-m-Fenster für Extremsteigungen",
        "elevation_source": "swisstopo Height Service / swissALTI3D",
    }
    save_json(meta_path, meta, compact=False)
    print(f"data-meta.json aktualisiert.")

    # Aufteilen in Chunks. Bestehende IDs behalten ihren Chunk, damit bereits
    # berechnete Profile nicht verschoben werden. Neue IDs werden stabil nach
    # ((osm_way_id - 1) % Chunkanzahl) + 1 verteilt.
    print(f"Schreibe {args.chunks} GeoJSON-Chunk-Dateien...")
    chunk_features: list[list[dict[str, Any]]] = [[] for _ in range(args.chunks)]
    for feature in final_features:
        fid = str(feature.get("id") or (feature.get("properties") or {}).get("@id") or "")
        osm_way_id = int((feature.get("properties") or {}).get("osm_way_id", 0))
        chunk_index = existing_chunk_map.get(fid)
        if chunk_index is None:
            chunk_index = ((osm_way_id - 1) % args.chunks) + 1
        if not 1 <= chunk_index <= args.chunks:
            raise ValueError(f"Ungültige Chunk-Zuordnung für {fid}: {chunk_index}")
        chunk_features[chunk_index - 1].append(feature)

    for index in range(args.chunks):
        chunk_feats = chunk_features[index]
        chunk_data = {
            "type": "FeatureCollection",
            "metadata": {"updated_at": now_iso, "data_version": now_iso},
            "features": chunk_feats,
        }
        out_file = work_dir / f"trails-{index + 1:02d}.geojson"
        save_json(out_file, chunk_data, compact=True)

    print(f"Erfolgreich {args.chunks} GeoJSON-Dateien aktualisiert.")


if __name__ == "__main__":
    main()