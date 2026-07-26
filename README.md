# MTB-Trailkarte 2.0 – GitHub Pages

## Enthalten

- Standortmarker mit Richtungsanzeige über Gerätekompass beziehungsweise GPS-Kurs
- Dunkelmodus: System, Hell oder Dunkel
- unbewertete Trails und MTB-Routen standardmässig ausgeschaltet
- Canvas-Rendering und reduzierte Ebenen während des Zoomens
- OSM-Update jeden Sonntag um 20:30 Uhr in `Europe/Zurich`
- Schweizer Geofabrik-Auszug; grenzüberschreitende Wege bleiben vollständig
- `incline` aus OSM wird bevorzugt
- fehlende Steigungen werden schrittweise über den swisstopo-Höhenprofildienst ergänzt
- Datenbereich in den Einstellungen mit App-Version, Aktualisierungszeitpunkt, OSM-Datenstand, Zählwerten und Quellen
- Schaltfläche **Jetzt aktualisieren**, die bereits auf GitHub veröffentlichte Daten neu lädt

## Wichtige Struktur

Der Workflow muss exakt an diesem Ort liegen:

```text
.github/workflows/update-osm.yml
```

Da Ordner mit einem Punkt unter Windows teilweise ausgeblendet werden, ist er in der ZIP-Datei möglicherweise nur sichtbar, wenn **Ausgeblendete Elemente** aktiviert sind. Beim Hochladen über GitHub empfiehlt es sich, den Ordner notfalls direkt über die GitHub-Weboberfläche anzulegen; siehe unten.

## Bestehendes Repository aktualisieren

1. ZIP-Datei entpacken.
2. Alle sichtbaren Dateien und Ordner in das Hauptverzeichnis des Repositorys hochladen und vorhandene Dateien ersetzen.
3. Wegen der Dateigrösse die Trail-Dateien bei Bedarf in zwei Uploads aufteilen:
   - `trails-01.geojson` bis `trails-05.geojson`
   - `trails-06.geojson` bis `trails-10.geojson`
4. Kontrollieren, dass im Repository zusätzlich vorhanden sind:
   - `scripts/update_trails.py`
   - `requirements-update.txt`
   - `data-meta.json`
   - `elevation-cache.json`
   - `.github/workflows/update-osm.yml`

## Versteckten Workflow zuverlässig hochladen

Falls `.github` beim Ziehen in den Browser nicht mitkommt:

1. Im Repository **Add file → Create new file** wählen.
2. Als Dateinamen vollständig eingeben:

```text
.github/workflows/update-osm.yml
```

3. Den Inhalt der gleichnamigen Datei aus dem entpackten Paket einfügen.
4. **Commit changes** wählen.

Sobald die Datei korrekt gespeichert ist, erscheint im Reiter **Actions** links der Workflow **OSM-Traildaten aktualisieren**.

## Workflow-Berechtigung

Der Workflow enthält bereits `permissions: contents: write`. Falls GitHub das Schreiben dennoch blockiert:

**Settings → Actions → General → Workflow permissions → Read and write permissions → Save**

## Erster Test

1. **Actions → OSM-Traildaten aktualisieren** öffnen.
2. **Run workflow** wählen.
3. Für einen kurzen Test `max_dem = 20` verwenden.
4. Nach erfolgreichem Test erneut mit `max_dem = 500` starten.

`max_dem = 0` berechnet alle noch fehlenden Höhenprofile in einem Lauf. Das kann sehr lange dauern und den externen Dienst stark belasten. Der reguläre Sonntagslauf ergänzt deshalb höchstens 500 neue DEM-Profile. Bereits berechnete Werte bleiben in `elevation-cache.json` erhalten. So wird die Abdeckung Woche für Woche vollständig.

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
