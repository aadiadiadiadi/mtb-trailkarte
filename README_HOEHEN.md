# MTB Trailkarte 2.2.0 – Höhen Variante A

## Was berechnet wird

Für jeden Trail werden die beiden Endpunkte des innerhalb der Schweiz
liegenden Linienabschnitts ausgewertet.

Im Infofenster erscheinen danach:

- Höherer Punkt
- Tieferer Punkt
- Höhendifferenz, immer als positiver Wert
- Höhenquelle

Bei einem grenzüberschreitenden Trail wird nur der Schweizer Abschnitt
berücksichtigt.

Die Werte sind kein vollständiges Höhenprofil und entsprechen nicht den
kumulierten Höhenmetern. Es handelt sich um die Höhendifferenz zwischen den
beiden äussersten Punkten des Schweizer Trailabschnitts.

## Dateien

- `index.html`: zeigt die neuen Höhenfelder an
- `service-worker.js`: Cache-Version 2.2.0
- `data-meta.json`: Metadaten zur Höhenmethode
- `hoehen_variante_a.py`: manuelles Aufbereitungsskript
- `requirements.txt`: benötigte Python-Bibliotheken

## Ausführen

Lege das Skript in einen beliebigen Ordner und starte:

```text
python hoehen_variante_a.py
```

Danach wählst du den Ordner mit den `trails-*.geojson`-Dateien aus.

Alternativ über die Kommandozeile:

```text
python hoehen_variante_a.py --input C:\Pfad\zum\Repository
```

Die Originaldateien werden nicht verändert. Die angereicherten Dateien werden
standardmässig in einem neuen Ordner mit dem Zusatz `_mit_hoehen` gespeichert.

## Fortsetzen nach einem Abbruch

Das Skript speichert bereits abgefragte Höhen in:

```text
.elevation_work\height_cache.sqlite
```

Nach einem Abbruch kann es erneut gestartet werden. Bereits vorhandene
Höhenwerte werden nicht nochmals abgefragt.

## Grenz- und Höhendaten

Das Skript lädt die aktuelle konfigurierte swissBOUNDARIES3D-Shapefile-ZIP
automatisch herunter. Falls der Downloadlink später geändert wird, kannst du
eine manuell heruntergeladene ZIP angeben:

```text
python hoehen_variante_a.py ^
  --input C:\Pfad\zum\Repository ^
  --boundary-zip C:\Downloads\swissboundaries3d_....shp.zip
```

Die Höhen werden über den GeoAdmin Height Service abgefragt.

## GitHub

Nach erfolgreicher Verarbeitung ersetzt du im Repository:

1. `index.html`
2. `service-worker.js`
3. `data-meta.json`
4. alle erzeugten `trails-*.geojson`

Die automatische OSM-Aktualisierung wird dadurch nicht wieder eingeführt.
