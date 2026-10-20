# Anleitung: Roblox-Avatar als Glas-Render

Diese Pipeline rendert einen Roblox-Avatar mit Blender Cycles als gläsernen
Charakter – automatisch, ausgelöst durch einen Push. Du musst nur einen Namen
in eine Textdatei schreiben.

---

## a) Open-Cloud-API-Key hinterlegen

1. Öffne das Repository auf GitHub.
2. **Settings** → **Secrets and variables** → **Actions**.
3. Button **New repository secret**.
4. **Name:** `ROBLOX_OPEN_CLOUD_API_KEY`
5. **Secret:** dein Open-Cloud-API-Key aus dem Roblox Creator Dashboard
   (<https://create.roblox.com/dashboard/credentials> → *Open Cloud* → *API Keys*).
6. **Add secret**.

Der Workflow liest das Secret als Umgebungsvariable und schickt es als Header
`x-api-key` an die Asset-Delivery-API. Fehlt das Secret, bricht der Workflow
nicht ab: Er versucht die öffentlichen Fallback-Endpunkte und rendert sonst
einen Platzhalter-Körper.

> Das Secret wird nie geloggt. In den Logs steht nur, **ob** ein Key gefunden
> wurde – nicht welcher.

---

## b) Erforderliche Berechtigungen für den API-Key

Beim Anlegen des Keys im Creator Dashboard:

| Einstellung | Wert |
| --- | --- |
| Access Permissions | **Assets** → *Read* |
| Zusätzlich nötig | **Legacy Assets** (Legacy-Berechtigung) → `legacy-asset:manage` |
| IP-Beschränkung | optional; ohne Beschränkung funktionieren die GitHub-Runner (dynamische IPs) |

**Wichtig – die häufigste Fehlerquelle (HTTP 403):** Der Endpunkt
`https://apis.roblox.com/asset-delivery-api/v1/assetId/{assetId}` verlangt den
Legacy-Scope `legacy-asset:manage`. Der moderne Scope `asset:read` allein
berechtigt nur zur **Metadaten**-Abfrage (`/assets/v1/assets/{id}`), nicht zum
**Download der Binärdateien**. Keys mit reinem `asset:read` liefern deshalb
403 – nicht weil der Key falsch ist, sondern weil der Scope fehlt.

Weitere Hinweise:

* Ein Key eines **persönlichen Accounts** genügt für öffentliche Assets.
  `legacy-asset:manage` ist für **Gruppen-Keys** derzeit nicht verfügbar.
* **Kein Key nötig** für: Username → UserId (`users.roblox.com`),
  Avatar-Definition (`avatar.roblox.com`), Asset-Details (`economy.roblox.com`).
  Diese öffentlichen Web-Endpunkte werden ohne Authentifizierung abgefragt.
* Der Key wird nur für den Download von Meshes und Texturen gebraucht.
* Rate-Limits: Die Pipeline drosselt sich selbst (Retries mit Backoff,
  maximal 80 Assets pro Lauf). Bei sehr großen Avataren kann trotzdem mal ein
  Asset fehlen – dann steht es im Manifest unter `skipped`.

---

## c) So startest du den Workflow

**Variante 1 – über `username.txt` (der normale Weg)**

1. Datei [`username.txt`](username.txt) öffnen.
2. Usernamen hineinschreiben, z. B. `Builderman`.
   Groß-/Kleinschreibung ist egal (`builderman` funktioniert genauso).
   Eine Profil-URL wie `https://www.roblox.com/users/12345/profile` wird
   ebenfalls akzeptiert.
3. Committen und pushen (auf `main`).
4. Tab **Actions** → Workflow **Avatar-Render (Glas)**.
5. Warten, bis der gelbe Punkt grün wird (typisch 3–12 Minuten).
6. Im Lauf nach unten scrollen zu **Artifacts** → **`avatar-render`** anklicken.
   Die ZIP-Datei enthält `render_avatar.png`.

**Variante 2 – manuell**

Actions → *Avatar-Render (Glas)* → **Run workflow**. Dort kannst du optional
einen Usernamen, die Auflösung (z. B. `960x540`) und die Sample-Zahl
(z. B. `32`) überschreiben – praktisch für schnelle Testläufe.

**Wo finde ich was?**

* **Step Summary** der Lauf-Seite: das fertige Bild als **Inline-Vorschau**,
  Username, UserId, Anzahl Teile/Accessoires, fehlgeschlagene Assets,
  ignorierte Animationen, Renderdauer.
* **Branch `renders`** im Repository: `latest.png` (bzw. `<username>.png`) –
  wird bei jedem Lauf ersetzt und ist ohne Login-Download direkt anschaubar.
* **Release `avatar-render-latest`**: `render_avatar.png` unter einer
  stabilen Download-URL
  (`/releases/download/avatar-render-latest/render_avatar.png`).
* **Artifact `avatar-render`** (ZIP mit `render_avatar.png`, 7 Tage gültig).
* **Artifact `render-debug`** (nur bei Fehlern): `manifest.json`,
  `summary.md`, Blender-Log.

> **iPhone-Hinweis (about:blank → App → 404):** GitHub-Artifacts sind an
> die Browser-Session gebunden. Die GitHub-App für iOS kann sie deshalb
> nicht herunterladen – das ist kein Fehler der Pipeline. Auf dem Handy
> einfach das Inline-Bild in der Step Summary, den Branch `renders`
> (`latest.png`) oder das Release `avatar-render-latest` benutzen. Das
> Artifact selbst funktioniert in einem Desktop-Browser (eingeloggt).

---

## d) Einschränkungen – bitte lesen

* **Keine GPU.** GitHub-gehostete Runner sind reine CPU-Maschinen. Blender
  rendert daher mit Cycles auf der CPU. Bei 1280×720 und 64 Samples dauert das
  grob **2–10 Minuten**; komplexe Avatare mit vielen Meshes/Texturen länger.
  Der Job-Timeout steht auf 45 Minuten. Schneller geht es über
  *Run workflow* mit z. B. `960x540` und `32` Samples.
* **Es gibt keinen einzelnen Open-Cloud-Endpunkt, der einen kompletten,
  geriggten Avatar als OBJ liefert.** Roblox gibt Meshes im proprietären
  FileMesh-Format aus (Versionen 1.00–7.00, teils mit LODs und Skin-Daten);
  dieses Repository enthält dafür einen eigenen Parser
  (`tools/roblox_mesh.py`). Der Körper wird aus den Avatar-Farben und
  -Proportionen als Stand-Pose-Rig aufgebaut; Accessoires werden als echte
  Roblox-Meshes geladen, relativ zur Kopfgröße skaliert und heuristisch an
  Kopf, Gesicht, Brust, Rücken, Hals, Taille oder Händen platziert.
  **Das Ergebnis ist eine künstlerische Näherung, kein pixelgenaues Abbild
  der Roblox-Engine.** Anatomisch korrekte Gelenk-Rigs (R15-Bones, Animationen)
  sind bewusst nicht Teil der Pipeline.
* **Rechtliches:** Das automatisierte Herunterladen fremder Avatare und
  Asset-Daten kann gegen die Roblox-Nutzungsbedingungen verstoßen. Prüfe das
  bitte, bevor du die Pipeline auf Accounts anwendest, die dir nicht gehören,
  und verwende sie nicht für kommerzielle Weiterverbreitung fremder Assets.
  Für eigene Accounts/Assets ist das Risiko gering.
* **Moderation/Archivierung:** Einzelne Assets können gesperrt oder archiviert
  sein. Dann fehlt das Teil im Render – im Manifest steht der Grund unter
  `skipped`, der Rest wird trotzdem gerendert.
* **2D-Kleidung** (klassische Shirts und Hosen) wird über den echten
  Roblox-UV-Atlas auf Torso, Arme und Beine gelegt: Jede Fläche (vorne,
  hinten, links, rechts, oben, unten) bekommt ihr eigenes Panel aus dem
  585×559-Template – nicht das komplette Bild auf den ganzen Körper. Das
  T-Shirt (eigene Grafik) und die Gesichts-Textur liegen als flache Dekals
  vor Brust bzw. Gesicht. Layered-Kleidung (Jacke, Pullover, Hose als
  3D-Asset) wird als Hülle um den Körper gezogen. Farben können vom
  Original abweichen, weil das Glasmaterial die Textur einfärbt.
* **RoProxy o. Ä. wird nicht verwendet.** Alle Anfragen gehen direkt an
  Roblox-Domains.

---

## Wie der Render aussieht (Kamera, Kleidung, Texturen)

**Kamera.** Der OBJ-Import (`forward_axis="NEGATIVE_Z"`, `up_axis="Y"`)
dreht Roblox-Koordinaten `(x, y, z)` nach Blender `(x, -z, y)`. Die
Roblox-Front (-Z) landet damit auf **+Y** – die Kamera steht deshalb auf
der +Y-Seite und schaut den Avatar von vorne an. Leicht schräg und leicht
von oben:

```bash
blender -b --factory-startup --python blender/render_glass.py -- \
  --manifest build/manifest.json --out render_avatar.png \
  --camera-azimuth 24 --camera-elevation 9
```

`--camera-azimuth 0` ist exakt frontal, negative Werte drehen zur anderen
Körperseite. Die Mathematik dazu liegt in `tools/scene_camera.py` und ist
ohne Blender testbar (`tools/tests/test_render_fixes.py`).

**Kleidung.** Klassische Shirts und Hosen sind kein freies Bild, sondern ein
UV-Atlas (585×559 px). `tools/avatar_uv.py` kennt die Panels (Pixel-genau,
Torso vorne/hinten/links/rechts/oben/unten, Arm- und Bein-Blöcke) und vergibt
die UVs:

* Rig-Quader (Avatar ohne eigene Körper-Meshes): pro Fläche das passende Panel,
* echte R15-Meshes: zylindrische Projektion der Panels auf das Mesh –
  die mitgelieferten Mesh-UVs passen nicht zu Kleidungs-Atlanten.

Wird eine Textur fälschlich auf einen ganzen Körper gelegt, erscheint der
komplette Atlas als Raster auf Brust und Beinen – genau das war der alte
Render. Zusätzlich speichert das Manifest als `color` die
Durchschnittsfarbe der Textur, damit das Glas in der Kleidungsfarbe und
nicht in Hautfarbe leuchtet.

**Texturen im Glas.** Reines Glas (Transmission 1,0) lässt die Umgebung
durch und zeigt von einer Textur fast nichts – genau deshalb wirkte der
Avatar grau/blau. Deshalb wird das Material aus zwei Shadern gemischt:
einem Glas-Shader (Transmission, Coat, IOR) für den Glaseindruck und einem
deckenden Shader mit der Textur. `--texture-strength` steuert den Anteil
(Standard `0.7` = 70 % Textur, `0` = reines Glas, `1` = Textur pur).
Transparente Bereiche des Kleidungs-Atlasses werden mit der Körperfarbe
gemischt, sonst rendert Blender sie schwarz. `--mono-glass` ignoriert
Texturen ganz.

**Körperteile.** Bringt der Avatar echte R15-Körper-Meshes mit (Bundles),
werden deren RBXM-Maße für Torso, Arme, Beine und Kopf verwendet und die
Teile lückenlos gestapelt (Beine → Torso → Hals → Kopf, Arme an den
Schultern). Liegen Arme in T-Pose vor, werden sie automatisch um 90° nach
unten gedreht. Unplausible Größen (Mesh-Einheiten sind nicht immer Studs)
werden begrenzt; das steht als Hinweis im Manifest.

---

## Wie Accessoires und Körperteile geladen werden (RBXM)

Roblox liefert Accessoires (Hüte, Haare, Badges …) **und** die getragenen
Körperteile (Torso, Arme, Beine aus Bundles wie „Roblox 2.0“ oder
„Korblox Deathspeaker“) über die Asset Delivery **nicht** als
`.mesh`-Datei aus, sondern als RBXM-Modell (binär) bzw. RBXMX (XML).
Die eigentliche FileMesh-ID steckt erst im Modell (`MeshPart.MeshId`,
`SpecialMesh.MeshId`), dazu Texturen (`TextureID`, `SurfaceAppearance`),
die Part-Größe (`size`) und die Position.

Deshalb läuft der Download zweistufig:

1. RBXM herunterladen, mit dem eigenen Parser
   ([`tools/rbxm.py`](tools/rbxm.py)) Mesh-/Textur-IDs, Größe und Position
   extrahieren (Chunks sind oft LZ4-/ZSTD-komprimiert – der Workflow
   installiert dafür `zstandard` und `lz4` per pip).
2. Die so gefundenen Meshes/Texturen wie bisher laden und platzieren:
   Körperteil-Meshes (AssetTypen 27–31) **ersetzen** die Rig-Quader in
   der jeweiligen BodyColor, Accessoires kommen an ihre Anker
   (Kopf, Gesicht, Brust, Rücken …).

Besonderheiten:

* **Animationen** (AssetTypen 24, 48–56, 61, 78) werden gar nicht erst
  heruntergeladen, nur gezählt (im Manifest unter
  `stats.ignored_animations`) – sie tauchen **nicht** als „übersprungen“ auf.
* **R15-Segmente:** Körperteil-Bundles (z. B. Korblox) liefern pro Asset
  mehrere Segmente (`RightUpperArm`, `RightLowerLeg`, `RightFoot` …).
  Die Pipeline wählt die R15-Segmente per Name aus und führt sie über
  ihre RBXM-Positionen zu einem zusammenhängenden Körperteil zusammen.
  Drehungen der Segmente werden ignoriert (aufrechte Standardpose).
* **Moderne Meshes (FileMesh 6.00/7.00):** Neuere Assets nutzen ein
  Chunk-Format; Version 7.00 ist zusätzlich **Draco-komprimiert**.
  Der Parser liest beides; für 7.00 installiert der Workflow das Paket
  `DracoPy` (ohne das Paket werden nur diese Meshes übersprungen, der
  Grund steht dann im Manifest).
* **Headless:** Ein `DynamicHead`-Asset ohne sichtbare Geometrie
  (z. B. „Headless Head“ – erkannt an kaum vorhandener Geometrie,
  winziger Part-Größe im RBXM oder versteckter Punkt-Geometrie)
  bedeutet: Der Avatar trägt keinen
  Kopf. Die Pipeline lässt den Rig-Kopf dann weg und vermerkt das als
  „Headless/DynamicHead erkannt“ in Manifest und Step Summary.
* **Sanity-Check:** Mesh-Einheiten aus RBXM-Dateien sind nicht immer
  Studs. Platzierte Teile größer als 12 Studs oder kleiner als
  0,05 Studs werden deshalb auf die Zielgröße ihres Ankers skaliert
  (Warnung im Manifest unter `warnings`).

## Zahlenformat: Studs mit Komma

Größen werden in der Roblox-Einheit **Studs** ausgegeben – in Log,
`summary.md` und Manifest (`metrics.height_de`) in deutscher
Schreibweise mit Komma, z. B. `Höhe: 5,40 Studs (typisch 5–6)`.
Früher stand dort der Rohwert mit Punkt (`5.398`), was man leicht als
„5 398 Studs“ (fünftausend…) liest. Ein normaler Avatar ist etwa
5–6 Studs hoch.

---

## Auto-Merge: direkt auf `main` arbeiten

Der Workflow [`auto-merge.yml`](.github/workflows/auto-merge.yml) merged
Pushes auf den Branch **`agent-changes`** automatisch nach `main` – ohne
manuellen Pull Request:

```bash
git checkout agent-changes
# ... Änderungen ...
git commit -am "Beschreibung"
git push origin agent-changes     # löst den Auto-Merge aus
```

Ablauf: auschecken → `main` fetchen → `agent-changes` nach `main` mergen →
`git push origin main`. Bei Konflikten wird der Merge abgebrochen (`main`
bleibt unverändert) und ein Issue mit der Lauf-URL erstellt.

Ist `main` durch Branch Rules geschützt, scheitert der direkte Push – der
Workflow legt dann automatisch einen Pull Request an und aktiviert Auto-Merge.

**Optional – damit Folgeläufe automatisch starten:** Pushes, die mit dem
Standard-`GITHUB_TOKEN` gemacht werden, lösen **keine** neuen Workflows aus.
Wenn nach dem Merge automatisch ein Avatar-Render starten soll, hinterlege ein
Fine-grained PAT mit *Contents: Read and write* als Secret
**`AUTO_MERGE_TOKEN`**. Der Workflow nutzt es automatisch, sobald es existiert.

---

## Aufbau des Repositories

```
username.txt                     Roblox-Username (Trigger für den Render)
ANLEITUNG.md                     diese Anleitung
.github/workflows/
  avatar-render.yml              Push/Manuell → Avatar rendern → Artifact
  auto-merge.yml                 agent-changes → main
  tests.yml                      Tests bei Pull Requests
blender/render_glass.py          Blender-Script (Glasmaterial, Kamera, Licht)
tools/
  build_avatar.py                Orchestrierung, schreibt manifest.json + OBJs
  roblox_api.py                  Web-APIs, Asset Delivery, BrickColor-Tabelle
  roblox_mesh.py                 FileMesh-Parser (v1–v7) → OBJ
  avatar_rig.py                  Stand-Pose-Rig + Accessoire-Platzierung
  avatar_uv.py                   UV-Panels des klassischen Kleidungs-Atlasses
  scene_camera.py                Kamera-Mathematik (vorne = +Y, testbar)
  brick_colors.json              208 Roblox-BrickColors (ID → RGB)
  tests/test_pipeline.py         Tests ohne Netzwerk/Blender
  tests/test_render_fixes.py     Tests für Rig, Kamera und Texturen
```

## Erster Testlauf (Referenz)

Der Workflow wurde direkt nach dem Einrichten einmal auf GitHub Actions
ausgeführt: Der komplette Job **„Avatar rendern“** war nach **1 min 47 s**
grün (Checkout, Username lesen, Avatar laden, Blender 4.2 installieren,
Cycles-Render, Artifact-Upload). Alle Schritte erfolgreich.

Zum Vergleich: Nur der Render-Schritt (1280×720, 64 Samples, Test-Rig) dauert
auf einem CPU-Runner ca. 30–60 s; der Rest ist Download/Installation von
Blender und das Laden der Assets.

## Key/Endpunkte prüfen (Diagnose)

Actions → Workflow **Roblox-Diagnose** → **Run workflow**. Er testet in wenigen
Sekunden:

* `users.roblox.com` – Username → UserId
* `avatar.roblox.com` – Avatar-Definition (Anzahl Assets)
* `economy.roblox.com` – Asset-Details
* `apis.roblox.com/asset-delivery-api/...` – Download **mit** deinem Key
* `assetdelivery.roblox.com/v2/...` – Download ohne Key (Fallback)

Ergebnis: Tabelle in der Step-Summary plus Hinweise/Annotations. So siehst du
sofort, ob der Key fehlt (401) oder der Scope nicht reicht (403).

## Fehlersuche

| Symptom | Ursache / Lösung |
| --- | --- |
| `HTTP 403` bei Asset-Downloads | Key hat `legacy-asset:manage` nicht → Abschnitt b) |
| `HTTP 401` bei Asset-Downloads | Secret fehlt oder ist leer → Abschnitt a) |
| `username.txt ist leer` | Datei enthält nur Leerzeichen/Newline |
| `Username wurde nicht gefunden` | Tippfehler oder Account gelöscht; exakten Namen prüfen |
| Render grün, aber Avatar „nackt“ | Downloads fehlgeschlagen → Lauf-Log und `skipped` im Manifest prüfen |
| `0 Accessoires`, viele Assets „übersprungen“ | Accessoires/Körperteile kommen als RBXM-Modell → Abschnitt „RBXM“. Steht im Manifest „RBXM nicht lesbar“, fehlen vermutlich die Module `lz4`/`zstandard` (Workflow-Schritt 3 prüft das) |
| Animationen werden „übersprungen“ gezählt | Alte Version – Animationen (Typen 24, 48–56, 61, 78) werden nicht heruntergeladen und stehen nur unter `ignored_animations` |
| „Avatar 5000 Studs hoch“ | Lesefehler: Höhen werden mit Komma ausgegeben (z. B. `5,40 Studs`); ein normaler Avatar ist 5–6 Studs hoch → Abschnitt „Zahlenformat“ |
| Artifact-Download auf dem iPhone endet auf about:blank/404 | Bekanntes Verhalten der iOS-App (Artifacts sind session-gebunden). Inline-Bild in der Step Summary, Branch `renders` (`latest.png`) oder Release `avatar-render-latest` nutzen – oder Artifact im Desktop-Browser laden |
| Lauf bricht nach 45 min ab | Auflösung/Samples reduzieren (Run workflow) |

Fragen oder Wünsche? Einfach ein Issue im Repository aufmachen.
