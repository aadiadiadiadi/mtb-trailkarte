# PROJECT_CONTEXT

- Keine automatische OSM-Aktualisierung; ein manueller differentieller Workflow ist vorgesehen.
- Höhenprofil ersetzt die frühere Entscheidung 'nur Höhendifferenz'.
- Höhen werden ausserhalb der App berechnet.
- Höhenprofile werden pro Trail-Segment berechnet; grenzüberschreitende Abtastpunkte ohne Höhenwert bleiben als Fehler sichtbar.
- App startet im Gütschwald und wechselt nach erfolgreicher Ortung automatisch zum Benutzer.
- Dokumentation plattformneutral halten; keine PowerShell- oder IDE-Abhängigkeiten.
