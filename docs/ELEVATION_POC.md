# Vorberechnete Höhenprofile für die Schweiz

## Umfang

Die neue Berechnung verarbeitet alle MTB-bewerteten OSM-LineStrings in den zehn vorhandenen Trail-Dateien. Verarbeitet werden `mtb:scale`, `mtb:scale:imba` und `mtb:scale:uphill`; der Gigeliwald Trail bleibt als Kompatibilitätsfall erhalten. Die Auswahl wird nach stabiler OSM-ID dedupliziert.

Die Trail-Dateien werden nicht nach `osm_way_id % 10` verteilt, sondern vom Differential-Update nach ihrer bisherigen Chunk-Zuordnung stabil gehalten. Neue IDs werden nach `((osm_way_id - 1) % 10) + 1` ergänzt. Deshalb enthält `elevation-profiles-01.json` ausschließlich die Profile aus `trails-01.geojson` usw.

## Berechnung

- regelmäßige Abtastung alle 20 m
- Start- und Endpunkt immer enthalten
- Höhen vom offiziellen swisstopo Height Service
- persistenter SQLite-Punktcache
- Medianfilter gegen einzelne Ausreißer
- Savitzky-Golay-Glättung mit fünf Punkten und Polynomgrad 2
- kumulierter Auf- und Abstieg mit 1-m-Totzone
- Extremsteigungen über ein gleitendes Fenster von mindestens 40 m

Der Online-Dienst wird chunkweise mit moderater Verzögerung verwendet. Für den vollständigen Schweizer Datenbestand können später lokale, gekachelte swissALTI3D-Raster die Online-Abfragen ersetzen.

## Sichere Ausgabe

Die zehn Original-GeoJSON-Dateien werden gelesen und mit kompakten Kennwerten angereichert; die vollständigen Profilpunkte bleiben getrennt:

- `elevation-profiles-01.json` bis `elevation-profiles-10.json`: Profile und Kennwerte nach OSM-ID
- `summary-01.json` bis `summary-10.json`: Laufzusammenfassung und Fehler
- `elevation-cache.sqlite`: wiederverwendbarer Höhenpunktcache (nicht für GitHub Pages)

In `trails-XX.geojson` werden nur Kennwerte wie `elevation_profile_ref`, `elevation_profile_chunk`, `elevation_profile_version`, `elevation_profile_hash`, Start-/Endhöhe, Höhendifferenz, Auf-/Abstieg und maximale Steigungen gespeichert. Dadurch wachsen die grossen Kartendateien nur marginal und enthalten niemals die Profil Arrays. `elevation_profile_version` ist ein Fingerprint des gesamten Profildatei-Chunks und dient zugleich als Browser-Cache-Key.

## Ausgabe und Wiederaufnahme

- Vorhandene Profile und der SQLite-Punktcache werden bei Folgeläufen wiederverwendet.
- Bereits berechnete OSM-IDs werden übersprungen, sofern `--force` nicht gesetzt ist.
- Profil- und Trail-Dateien werden alle 25 Trails atomar als Checkpoint gespeichert.
- Eine kurze konfigurierbare Pause nach echten Online-Antworten begrenzt die Abfragerate.

## Grenzen

- Grenzüberschreitende Wege können einzelne Abtastpunkte ausserhalb der Schweiz enthalten. Ist dafür keine Höhe verfügbar, wird das Feature als Fehler protokolliert und beim nächsten Lauf erneut versucht.
- Die Wegsegmente werden einzeln ausgewertet. Eine Route wird noch nicht zu einem durchgehenden, topologisch sortierten Gesamtprofil zusammengesetzt.
- swissALTI3D beschreibt das Gelände. Brücken, Tunnel und künstliche Bauwerke können davon abweichen.

## Integration in der App

Beim App-Start werden weiterhin nur die zehn bestehenden Trail-GeoJSON-Dateien geladen. Beim ersten Klick auf einen vorbereiteten Trail wird anhand von `elevation_profile_chunk` genau eine der zehn Profildateien geladen. Die Zuordnung erfolgt ohne räumliche Gebietsprüfung.

- Normale Trails behalten das bestehende Detailfenster unverändert.
- Ein Profil-Chunk wird erst beim ersten Klick auf einen Trail mit `elevation_profile_ref` geladen.
- Die gelesenen Chunk-Daten bleiben für die Laufzeit der Seite im Speicher; parallele oder spätere Klicks im selben Chunk erzeugen keinen zweiten Abruf.
- Es gibt im Browser keine Höhen-API und keinen API-Fallback.
- Kennwerte werden nach dem lokalen Dateiladen in das weiterhin geöffnete Detailfenster eingesetzt.
- Die vorberechnete Länge des ausgewerteten Trailsegments wird in den allgemeinen Trailinformationen direkt unter der Steigungsangabe angezeigt.
- Im Höhenblock werden die Höhen des ersten und letzten Profilpunkts als Start- und Endpunkt angezeigt; Minimum und Maximum bleiben intern für Diagramm und Höhendifferenz erhalten.
- Das SVG-Höhenprofil ist zunächst eingeklappt und wird erst beim Antippen von `▶ Höhenprofil` erzeugt.
- Im aufgeklappten Diagramm zeigt das Ende der Distanzachse die Gesamtlänge; auf eine redundante zusätzliche Beschriftung wird verzichtet.
- Der Service Worker cached die Profildatei erst nach ihrem ersten tatsächlichen Abruf, nicht bei Installation oder App-Start. Die Chunk-Revision im Query-String verhindert, dass ein veralteter Gerätecache eine neue Profilversion ausblendet; die App prüft zusätzlich Chunk-Revision und Geometrie-Hash.
- Die produktiven Trail-GeoJSON-Dateien werden network-first geladen, damit aktualisierte Höhenkennwerte nicht von einem älteren Gerätecache verdeckt werden; offline bleibt der letzte erfolgreiche Stand verfügbar.
- Ist eine Profildatei für einen bereits markierten Trail noch nicht veröffentlicht, zeigt die App „–“ statt einer erfundenen Länge.

## GitHub Actions

Der manuelle Workflow `.github/workflows/schweiz-hoehenprofile.yml` bietet die Chunks `01` bis `10`, eine Option `force` und einen Dry-Run. Der SQLite-Cache wird zwischen den Läufen wiederverwendet. Auch ein teilweise fehlgeschlagener Lauf übernimmt seine atomaren Checkpoints, Profildatei, Zusammenfassung und Artefakte, bevor der Workflow am Ende als fehlgeschlagen markiert wird. Commit und Cache-Schutz verwenden dieselbe Concurrency-Gruppe wie der differentielle OSM-Lauf. Bei einem fehlgeschlagenen Cache-Upload bleibt der Commit-Status ebenfalls überprüft; ein Cache-Fehler wird separat als Workflow-Fehler gemeldet.

## Ausführung

Aus dem Projektverzeichnis:

```text
python scripts/berechne_schweiz_profile.py --chunk 01 --dry-run
python scripts/berechne_schweiz_profile.py --chunk 01
```

Ein einzelnes Feature kann so getestet werden:

```text
python scripts/berechne_schweiz_profile.py --chunk 06 --feature-id way/1225944161
```

Ein unterbrochener Chunklauf wird mit demselben Befehl fortgesetzt. Bereits gespeicherte Profile und Höhenpunkte werden dabei nicht erneut abgefragt.