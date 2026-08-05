# Höhenprofil-Proof-of-Concept

## Umfang

Der PoC verarbeitet ausschließlich:

- alle mit einem MTB-Schwierigkeits-Tag versehenen OSM-Wegsegmente in einem Radius von 1'200 m um den App-Startpunkt im Gütschwald (`8.29021, 47.05012`)
- alle in den vorhandenen GeoJSON-Dateien als `Gigeliwald Trail` referenzierten Wegsegmente

Die Auswahl wird nach stabiler OSM-ID dedupliziert. Im aktuellen Datenbestand sind dies 114 Wegsegmente. Der Gigeliwald Trail ist als Routenname eines Segments `way/1225944161` gespeichert, nicht als normaler Trailname.

## Berechnung

- regelmäßige Abtastung alle 20 m
- Start- und Endpunkt immer enthalten
- Höhen vom offiziellen swisstopo Height Service
- persistenter SQLite-Punktcache
- Medianfilter gegen einzelne Ausreißer
- Savitzky-Golay-Glättung mit fünf Punkten und Polynomgrad 2
- kumulierter Auf- und Abstieg mit 1-m-Totzone
- Extremsteigungen über ein gleitendes Fenster von mindestens 40 m

Der Online-Dienst ist nur für diesen kleinen PoC vorgesehen. Für den vollständigen Datenbestand sollen lokale, gekachelte swissALTI3D-Raster verwendet werden.

## Sichere Ausgabe

Die zehn Original-GeoJSON-Dateien werden nur gelesen. Ergebnisse liegen getrennt unter `poc-output/`:

- `selected-trails.geojson`: ausgewählte Geometrien und kompakte Kennwerte
- `elevation-profiles.json`: vorberechnete Profile und Kennwerte nach OSM-ID
- `elevation-cache.sqlite`: wiederverwendbarer Höhenpunktcache
- `summary.json`: Laufzusammenfassung und Fehler

## Aktuelles Ergebnis

- 114 erfolgreich berechnete Features
- 1'023 Profilpunkte
- 929 eindeutige Höhenpunkte im Cache
- Profildatei rund 53 KB unkomprimiert
- ausgewählte Geometrien und Kennwerte rund 103 KB unkomprimiert

## Grenzen dieses PoC

- Das Testgebiet liegt vollständig innerhalb der Schweiz. Die Architektur für Grenz-Clipping wurde analysiert, aber in diesem PoC nicht benötigt und daher noch nicht implementiert.
- Die Wegsegmente werden einzeln ausgewertet. Eine Route wird noch nicht zu einem durchgehenden, topologisch sortierten Gesamtprofil zusammengesetzt.
- swissALTI3D beschreibt das Gelände. Brücken, Tunnel und künstliche Bauwerke können davon abweichen.
- Der Gütschwald ist in den Daten nicht als eindeutiges Trailobjekt gekennzeichnet; der 1'200-m-Radius ist deshalb eine ausdrücklich dokumentierte PoC-Auswahlregel.

## PoC-Integration in der App

Die App erkennt vorberechnete PoC-Trails über eine kleine statische Menge ihrer 114 stabilen OSM-IDs. Diese Menge entspricht exakt den Schlüsseln in `elevation-profiles.json`. Dadurch entstehen beim App-Start keine zusätzliche Datei-Anfrage und keine zusätzlichen Kartenlayer; zugleich ist die Zuordnung unabhängig von Laufzeit-Geometrieberechnungen.

- Normale Trails behalten das bestehende Detailfenster unverändert.
- Erst beim ersten Klick auf einen PoC-Trail wird `poc-output/elevation-profiles.json` geladen.
- `poc-output/elevation-profiles.json` ist eine zwingende Deployment-Datei und muss zusammen mit der PoC-App auf GitHub Pages vorhanden sein.
- Die Lade-Promise und die gelesenen Daten bleiben danach für die Laufzeit der Seite im Speicher; parallele oder spätere Klicks erzeugen keinen zweiten Abruf.
- Es gibt im Browser keine Höhen-API und keinen API-Fallback.
- Kennwerte werden nach dem lokalen Dateiladen in das weiterhin geöffnete Detailfenster eingesetzt.
- Die vorberechnete Länge des ausgewerteten Trailsegments wird in den Trailinformationen angezeigt.
- Das SVG-Höhenprofil ist zunächst eingeklappt und wird erst beim Antippen von `▶ Höhenprofil` erzeugt.
- Im aufgeklappten Diagramm zeigt das Ende der Distanzachse die Gesamtlänge; auf eine redundante zusätzliche Beschriftung wird verzichtet.
- Der Service Worker cached die Profildatei erst nach ihrem ersten tatsächlichen Abruf, nicht bei Installation oder App-Start.
- Die produktiven Trail-GeoJSON-Dateien werden network-first geladen, damit aktualisierte Höhenkennwerte nicht von einem älteren Gerätecache verdeckt werden; offline bleibt der letzte erfolgreiche Stand verfügbar.

## Ausführung

Aus dem Projektverzeichnis:

```text
python scripts/poc_hoehenprofile.py --dry-run
python scripts/poc_hoehenprofile.py
```

Ein einzelnes Feature kann so getestet werden:

```text
python scripts/poc_hoehenprofile.py --feature-id way/1225944161
```