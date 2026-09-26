# ARCHITECTURE

GeoJSON -> Vorverarbeitung -> GitHub Pages -> Browser.

Vorverarbeitung berechnet Höhenprofile und Kennwerte. Keine Höhenberechnung im Browser.

Die zehn Trail-Dateien und die zehn `elevation-profiles-XX.json` Chunks bleiben synchron. Bestehende OSM-IDs behalten ihren Chunk; neue IDs werden deterministisch ergänzt. Die vollständigen Profilpunkte bleiben ausserhalb der grossen Trail-GeoJSONs.
