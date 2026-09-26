# MTB-Trailkarte – GitHub Pages

## Enthalten

- Standortmarker mit Richtungsanzeige über Gerätekompass beziehungsweise GPS-Kurs
- Dunkelmodus: System, Hell oder Dunkel
- unbewertete Trails und MTB-Routen standardmässig ausgeschaltet
- Canvas-Rendering und reduzierte Ebenen während des Zoomens
- Manuell gestarteter differentieller OSM-Abgleich
- Schweizer Geofabrik-Auszug; grenzüberschreitende Wege bleiben vollständig
- `incline` aus OSM wird bevorzugt
- zehn chunkweise berechnete Höhenprofile aus dem swisstopo Height Service
- Datenbereich in den Einstellungen mit App-Version, Aktualisierungszeitpunkt, OSM-Datenstand, Zählwerten und Quellen
- Schaltfläche **Jetzt aktualisieren**, die bereits auf GitHub veröffentlichte Daten neu lädt

## Datenstruktur

- `trails-01.geojson` bis `trails-10.geojson`: Karten- und Traildaten
- `elevation-profiles-01.json` bis `elevation-profiles-10.json`: vollständige Höhenprofile pro Chunk
- `summary-01.json` bis `summary-10.json`: Ergebnis und Fehler des jeweiligen Laufs
- `elevation-cache.sqlite`: lokaler bzw. in Actions zwischengespeicherter Höhenpunktcache; nicht committen (durch `.gitignore` ausgeschlossen)
- `.github/workflows/update-osm-diff.yml`: differentielles OSM-Update
- `.github/workflows/schweiz-hoehenprofile.yml`: manuelle Höhenprofil-Berechnung

Die Zuordnung eines Profildatei-Chunks entspricht exakt der gleichnamigen Trail-Datei. Bestehende OSM-IDs behalten ihren bisherigen Chunk, damit bereits berechnete Profile gültig bleiben. Die Berechnung muss vor dem Deployment mindestens einmal für den jeweiligen Chunk gestartet werden.

## GitHub-Actions-Workflows

### OSM-Traildaten differentiell aktualisieren

1. **Actions → OSM-Traildaten differentiell aktualisieren → Run workflow**
2. Für einen ersten Test `dry_run` aktivieren.
3. Ohne Dry-Run werden der aktuelle Geofabrik-Auszug geladen, unveränderte Geometrien und berechnete Attribute beibehalten und nur tatsächlich geänderte Dateien committed.

### Schweiz-Höhenprofile berechnen

1. **Actions → Schweiz-Höhenprofile berechnen → Run workflow**
2. Den Chunk `01` bis `10` auswählen.
3. `dry_run` prüft nur die Zielauswahl. Für die Berechnung deaktivieren.
4. `force` ist nur für eine bewusste Neuberechnung aller Profile des gewählten Chunks nötig.

Beide schreibenden Workflows verwenden dieselbe Concurrency-Gruppe. Dadurch können OSM-Update und Profilberechnung nicht gleichzeitig in dieselben Trail-Dateien schreiben.

## Workflow-Berechtigung

Der Workflow enthält bereits `permissions: contents: write`. Falls GitHub das Schreiben dennoch blockiert:

**Settings → Actions → General → Workflow permissions → Read and write permissions → Save**

## Lokale Profilberechnung

Vom Projektverzeichnis:

```text
python scripts/berechne_schweiz_profile.py --chunk 01 --dry-run
python scripts/berechne_schweiz_profile.py --chunk 01
```

Ein einzelnes Feature kann gezielt verarbeitet werden:

```text
python scripts/berechne_schweiz_profile.py --chunk 06 --feature-id way/1225944161
```

Ein abgebrochener Lauf wird mit demselben Befehl fortgesetzt. Vorhandene Profile werden anhand eines Geometrie-Hashs übersprungen, der SQLite-Cache verhindert doppelte Höhenabfragen, und alle 25 erfolgreichen Profile werden als Checkpoint geschrieben.

Details zu Berechnung, Ausgabe, Wiederaufnahme und Browser-Integration stehen in `docs/ELEVATION_POC.md`.

## Datenaktualität in der App

Der Workflow erzeugt nach jedem Lauf `data-meta.json`. Die App zeigt daraus in den Einstellungen:

- App-Version
- letzte Aktualisierung
- OSM-Datenstand
- Anzahl MTB-Trails und MTB-Routen
- Anzahl Steigungen aus OSM und DEM
- noch fehlende Steigungen
- Abdeckung und Datenquellen

Die Schaltfläche **Jetzt aktualisieren** startet keinen GitHub-Workflow. Sie prüft, ob GitHub Pages bereits eine neuere `data-meta.json` bereitstellt, und lädt dann die neuen Trail-Dateien.
