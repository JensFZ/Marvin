# H265-Konverter

Kleines Windows-Tool, das einen Ordner nach Videodateien durchsucht, den Codec jeder Datei
anzeigt und ausgewählte Dateien per ffmpeg nach H.265/HEVC umkodiert. Nach erfolgreicher
Konvertierung wird das Original weggeräumt.

Eine einzige Datei, kein Build-Schritt: [`h265gui.py`](h265gui.py).

## Voraussetzungen

| | |
|---|---|
| Python | 3.9 oder neuer, mit tkinter (bei den üblichen Windows-Installern dabei) |
| ffmpeg | `ffmpeg` **und** `ffprobe` müssen im `PATH` liegen |
| send2trash | optional, für den Papierkorb — ohne das Paket wird endgültig gelöscht |

```bash
winget install Gyan.FFmpeg
```

```bash
pip install send2trash
```

Fehlt ffmpeg, meldet das Programm das beim Start. Fehlt `send2trash`, erscheint ein Hinweis
im Log und Originale werden hart gelöscht statt in den Papierkorb verschoben.

Für GPU-Kodierung (`hevc_nvenc`) wird eine NVIDIA-Karte mit NVENC-Unterstützung gebraucht.

## Starten

```bash
python h265gui.py
```

## Bedienung

1. **Ordner wählen** — der Ordner wird rekursiv durchsucht. Erkannt werden `.mkv`, `.mp4`,
   `.m4v`, `.avi`, `.mov`, `.wmv`, `.flv`, `.webm`, `.mpg`, `.mpeg`, `.ts`, `.m2ts`.
2. Die Liste zeigt Datei, Codec, Größe und Laufzeit. Dateien, die **bereits HEVC** sind,
   erscheinen grau und werden von der Konvertierung ausgenommen.
3. **Auswählen** per Strg- bzw. Shift-Klick, oder über *Alle nicht-HEVC wählen*.
4. **Encoder und Qualität** einstellen (siehe unten).
5. **Konvertieren** — der obere Balken zeigt die laufende Datei, der untere den Gesamtfortschritt.
   *Abbrechen* stoppt den laufenden ffmpeg-Prozess.

Farben in der Liste: grau = schon HEVC, grün = konvertiert, rot = fehlgeschlagen.
Fehlermeldungen landen im Log unten.

## Encoder und Qualität

| Encoder | Wann |
|---|---|
| `hevc_nvenc` | Standard. Läuft auf der NVIDIA-GPU, um ein Vielfaches schneller. |
| `libx265` | CPU. Deutlich langsamer, dafür bessere Kompression bei gleicher Qualität. |

Der Wert **Qualität** ist der CQ- (NVENC) bzw. CRF-Wert (x265), Standard 24. Kleiner heißt
bessere Qualität und größere Datei, größer heißt umgekehrt. Sinnvoller Bereich ist etwa 20–28.

Audio-, Untertitel- und Anhangsspuren werden unverändert übernommen (`-c:a copy`), nur das
Video wird neu kodiert.

## Was mit den Dateien passiert

Die konvertierte Datei bekommt `.h265` vor die Endung:

```
Film.mkv  ->  Film.h265.mkv
```

Container, die kein HEVC transportieren können (`avi`, `wmv`, `flv`, `mpg`, `webm`), werden
nach **mkv** geschrieben — `Alt.avi` wird zu `Alt.h265.mkv`. AVI würde den Stream sonst
klaglos als `rawvideo` ablegen.

Das Original wird **nur dann** entfernt, wenn alle drei Prüfungen bestehen:

1. ffmpeg endet mit Rückgabewert 0
2. die Zieldatei existiert und ist nicht leer
3. ein erneutes `ffprobe` meldet `hevc` und eine Laufzeit, die um weniger als 1 % abweicht

Schlägt etwas fehl, wird die unfertige Zieldatei gelöscht und das Original bleibt unberührt.

### Lokale Laufwerke

Das Original wandert in den **Papierkorb** (wenn `send2trash` installiert ist).

### Netzlaufwerke

Auf Netzlaufwerken und UNC-Pfaden gibt es keinen Papierkorb — Windows löscht dort endgültig.
Deshalb wird das Original stattdessen in einen Unterordner **`_alt`** verschoben, jeweils
neben der Datei:

```
Serie\Staffel1\E01.mp4        ->  Serie\Staffel1\E01.h265.mp4
                                  Serie\Staffel1\_alt\E01.mp4
```

`_alt` wird bei späteren Scans übersprungen, die weggeräumten Originale tauchen also nicht
wieder in der Liste auf. Aufräumen ist deine Entscheidung: `_alt` prüfen, dann löschen.

Die Erkennung läuft über `GetDriveTypeW`, erfasst also auch verbundene Laufwerke wie `Z:`,
nicht nur `\\server\freigabe`. Beim Ordnerwählen erscheint ein Hinweis im Log, wenn ein
Netzlaufwerk erkannt wurde.

## Tests

```bash
python h265gui.py --selftest
```

Der Selftest erzeugt sich sein Testmaterial selbst und prüft die ganze Kette: Codec-Erkennung,
Konvertierung mit Fortschrittsmeldungen, Übernahme der Audiospur, Abbruch mitsamt Aufräumen
der Teildatei, kaputte Eingabedateien, die Laufzeitprüfung, die avi-nach-mkv-Zuordnung sowie
das Verschieben nach `_alt` inklusive Namenskollisionen.

## Grenzen

- Kein Pause/Resume und keine Warteschlange über einen Programmstart hinaus — nach einem
  Abbruch fängt die betroffene Datei von vorn an.
- Kein 2-Pass und kein Bitraten-Ziel, nur CQ/CRF.
- Es wird sequenziell kodiert, eine Datei nach der anderen.
- Vom Quellvideo wird nur die erste Videospur übernommen; eingebettete Cover-Bilder gehen
  verloren.
- HEVC-Dateien werden nie erneut kodiert, auch nicht auf einen anderen Qualitätswert.

## Hinweis

Fang mit ein paar Dateien an und sieh dir die Größen an, bevor du eine ganze Sammlung
durchlaufen lässt. Wie viel HEVC tatsächlich spart, hängt stark vom Material ab — bei bereits
gut komprimierten Quellen kann die Datei auch größer werden.
