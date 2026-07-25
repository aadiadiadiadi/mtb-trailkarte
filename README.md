# MTB Trailkarte

Statische, installierbare MTB-Web-App für GitHub Pages.

## Dateien

Alle Dateien müssen im Hauptverzeichnis des GitHub-Repositories liegen:

- index.html
- trails-01.geojson bis trails-10.geojson
- manifest.webmanifest
- service-worker.js
- icon-192.svg
- icon-512.svg
- .nojekyll

Die zehn Trail-Dateien gehören zusammen. Keine davon weglassen oder umbenennen.

## GitHub Pages aktivieren

1. Repository auf GitHub erstellen.
2. Alle Dateien aus diesem Ordner hochladen.
3. Repository öffnen: Settings > Pages.
4. Unter Build and deployment:
   - Source: Deploy from a branch
   - Branch: main
   - Folder: /(root)
5. Save wählen.
6. Nach kurzer Wartezeit erscheint die öffentliche Adresse.

## Aktualisierung

Bei Änderungen alle geänderten Dateien erneut hochladen. Nach einer Aktualisierung
kann ein vollständiges Neuladen der Seite oder das Löschen des App-Caches nötig sein.


Wichtig: Nicht die ZIP-Datei selbst hochladen, sondern zuerst entpacken und nur deren Inhalt hochladen.
