# Anleitung: Roblox-Avatar als Glossy-Render (Blender 5.0 EEVEE)

Diese Pipeline rendert einen Roblox-Avatar mit Blender 5.0 EEVEE als hochglänzenden
(Glossy) Charakter – automatisch, ausgelöst durch einen Push. Du musst nur einen Namen
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

**Variante 1 – über `Configs.toml` (der normale Weg)**

1. Datei [`Configs.toml`](Configs.toml) öffnen.
2. Oben den Roblox-Usernamen eintragen, z. B. `username = "Builderman"`.
   Groß-/Kleinschreibung ist egal (`builderman` funktioniert genauso).
   Eine Profil-URL wie `https://www.roblox.com/users/12345/profile` wird
   ebenfalls akzeptiert.
3. Optional die weiteren Werte anpassen (Drehung, Pose, Bildausschnitt,
   Hintergrund) – alle sind in der Datei ausführlich kommentiert.
4. Committen und pushen (auf `main`). Ist `render_bei_aenderung = true`,
   startet der Render direkt; der Workflow setzt den Schalter danach wieder
   auf `false`.
5. Tab **Actions** → Workflow **Avatar-Render (Glas)**.
6. Warten, bis der gelbe Punkt grün wird (typisch 3–12 Minuten).
7. Das Ergebnis steht als Bild in der **Step Summary**, im Branch `renders`
   (`latest.png`) und im Release `avatar-render-latest`. Zusätzlich gibt es
   das Artifact **`avatar-render`** (ZIP mit `render_avatar.png`).

**Variante 2 – manuell**

Actions → *Avatar-Render (Glas)* → **Run workflow**. Dort kannst du optional
einen Usernamen, die Auflösung (z. B. `960x540`) und die Sample-Zahl
(z. B. `32`) überschreiben – praktisch für schnelle Testläufe.

**Wo finde ich was?**

* **Step Summary** der Lauf-Seite: das fertige Bild als **Inline-Vorschau**
  plus Links zum Bild und zum Download (keine Analysedaten mehr).
* **Branch `renders`** im Repository: `latest.png` (bzw. `<username>.png`) –
  wird bei jedem Lauf ersetzt und ist ohne Login-Download direkt anschaubar.
* **Release `avatar-render-latest`**: `render_avatar.png` unter einer
  stabilen Download-URL
  (`/releases/download/avatar-render-latest/render_avatar.png`).
* **Artifact `avatar-render`** (ZIP mit `render_avatar.png`, 7 Tage gültig).
* **Artifact `render-debug`** (nur bei Fehlern): `manifest.json`,
  `summary.md`, Blender-Log – reine Diagnose, nicht Teil des Ergebnisses.

> **iPhone-Hinweis (about:blank → App → 404):** GitHub-Artifacts sind an
> die Browser-Session gebunden. Die GitHub-App für iOS kann sie deshalb
> nicht herunterladen – das ist kein Fehler der Pipeline. Auf dem Handy
> einfach das Inline-Bild in der Step Summary, den Branch `renders`
> (`latest.png`) oder das Release `avatar-render-latest` benutzen. Das
> Artifact selbst funktioniert in einem Desktop-Browser (eingeloggt).

---

## d) `Configs.toml` und `Posen.toml`

Alle Render-Einstellungen stehen in [`Configs.toml`](Configs.toml), die Posen
in [`Posen.toml`](Posen.toml). Beide Dateien sind komplett kommentiert; die
Werte werden vor dem Render geprüft (`tools/render_config.py`,
`tools/avatar_pose.py`) und bei Fehlern mit einer klaren Meldung abgelehnt.

| Wert in `Configs.toml` | Bedeutung |
| --- | --- |
| `username` | Roblox-Username (früher `username.txt`) |
| `drehung_grad` | Seitliche Drehung: `0` = frontal, `90` = rechte Seite, `-90` = linke Seite. Nur −90 … 90 ist erlaubt. |
| `pose` | `None` = Stand-Pose, sonst ein Name aus `Posen.toml` (z. B. `Winken`) |
| *(Bildausschnitt)* | Steht **nicht** mehr in der Config: Jede Pose in `Posen.toml` definiert selbst, bis zu welchen Positionen man sehen kann (`sichtbar_oben_prozent`, `sichtbar_unten_prozent`, `kamera_abstand_studs`, `avatar_hoch_studs`). |
| `render_bei_aenderung` | `true` = nach diesem Push sofort rendern. Der Workflow setzt den Wert nach erfolgreichem Render automatisch auf `false`. |
| `hintergrund_url` | URL (oder lokaler Pfad) zu einem Hintergrundbild, `"hdri"` für die HDRI-Umgebung (Standard) oder `"transparent"` für einen durchsichtigen Hintergrund. |
| `hintergrund_glossy` | `true` = Hintergrund glänzt wie der Avatar, `false` = matt. |

Das Hintergrundbild wird in Blender auf eine Fläche gelegt, die exakt hinter
dem Avatar sitzt und bis zum Bildrand reicht.

**Posen:** In `Posen.toml` bekommt jede Pose einen eigenen Abschnitt mit
eigenem Bildausschnitt/Kamera-Werten und sprechenden Delta-Werten pro
Körperteil – keine rohen Achsen-Tripel mehr. Beispiel:

```toml
[Winken]
sichtbar_oben_prozent = 8        # bis 8 % über dem Kopf sieht man
sichtbar_unten_prozent = 5       # bis 5 % unter den Füßen
kamera_abstand_studs = 0.5       # zusätzlich 0,5 Studs weiter weg
avatar_hoch_studs = 0            # Avatar weder angehoben noch gesenkt

[Winken.rechter_oberarm]
vor_hoch_schwingen_grad = 158    # Arm schwingt nach vorn-oben
vom_koerper_weg_grad = 22        # und 22° vom Körper weg
```

Die Werte sind Deltas zur Stand-Pose und drehen um die exakten Gelenk-Pivots
(Schulter, Ellbogen, Hüfte, Knie … aus den RigAttachments); überblendet wird
volumen-erhaltend, damit nichts an den Gelenken einschnürt. Verfügbare
Körperteile: oberer/unterer Torso, Kopf, linke/rechte Ober- und Unterarme,
linke/rechte Hand, linke/rechte Ober- und Unterschenkel sowie linker/rechter
Fuß. Nicht genannte Teile/Felder bleiben neutral. Accessoires der Kategorien
Kopf, Hals, Gesicht, Schultern, Vorderseite, Rücken und Taille folgen
automatisch ihrem Körperteil; **Ausrüstung (Gear) wird als einzige Kategorie
gar nicht erst heruntergeladen.** Enthalten sind u. a. `Helden_Pose`,
`Siegerfaust`, `Kampfstellung`, `Superhelden_Landung`, `Sprung_Action`,
`Arme_Verschraenkt`, `Denker`, `Peace_Pose`, `Tanz_Freeze`, `Flieger` und
`Winken`.

---

## e) Einschränkungen – bitte lesen

* **Keine physische GPU.** GitHub-gehostete Runner sind reine CPU-Maschinen. Blender 5.0
  rendert EEVEE daher über Mesa Software-OpenGL/Vulkan (`llvmpipe` / `lavapipe`)
  standardmäßig in `2000x2000` mit `16` Samples. Der Job-Timeout steht auf 45 Minuten.
  Abweichende Auflösungen oder Samples lassen sich jederzeit über *Run workflow* setzen.
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
* **2D-Kleidung** (T-Shirts, Hosen) wird als Textur auf die Körperteile gelegt,
  sofern sie herunterladbar ist. Bei den klassischen 585×559-Vorlagen schneidet
  der Renderer das passende Feld heraus (Torso 128×128, Ärmel 64×128 …), damit
  das Motiv groß und lesbar bleibt. 3D-Kleidungs-Bundles werden wie Accessoires
  behandelt. Beides kann farblich vom Original abweichen.
* **Proportionen kommen aus den Meshes:** Die getragenen Bundle-Teile (Torso,
  Arme, Beine, Kopf) bestimmen Größe und Position im Rig; es wird nichts mehr
  in Schema-F-Boxen gepresst. Unplausible RBXM-Größen (z. B. Platzhalter
  1×1×1 bei einem großen Hut) werden verworfen und im Manifest als Hinweis
  vermerkt.
* **Material- & World-Look (Blender 5.0):** Als World Environment Texture wird
  [`sunset_jhbcentral_4k.exr`](sunset_jhbcentral_4k.exr) direkt geladen (im Hintergrund
  sichtbar und für Beleuchtung + Spiegelungen aktiv, ohne Boden und ohne zusätzliche
  Studio-Lichter). Jedes Teil erhält ein Principled BSDF mit Image Texture auf
  `Base Color` sowie exakt `Roughness = 0`, `Metallic = 0.2` und `Specular IOR Level = 1.0`
  (`Transmission = 0`, deckend hochglänzend). Optionale Regler per CLI:
  `--roughness`, `--metallic`, `--specular-level`, `--env-texture`, `--env-strength`,
  `--transparent`, `--with-floor`, `--studio-lights`, `--engine cycles`.
* **RoProxy o. Ä. wird nicht verwendet.** Alle Anfragen gehen direkt an
  Roblox-Domains.

---

## Wie Accessoires und Körperteile geladen werden (RBXM)

Roblox liefert Accessoires (Hüte, Haare, Badges …) **und** die getragenen
Körperteile (Torso, Arme, Beine aus Bundles wie „Roblox 2.0“ oder
„Korblox Deathspeaker“) über die Asset Delivery **nicht** als
`.mesh`-Datei aus, sondern als RBXM-Modell (binär) bzw. RBXMX (XML).
Die eigentliche FileMesh-ID steckt erst im Modell (`MeshPart.MeshId`,
`SpecialMesh.MeshId`), dazu Texturen (`TextureID`, `SurfaceAppearance`),
die Part-Größe (`size`) und der CFrame (Position **und** Rotation –
inkl. der 24 Sonder-Rotationen der Binärspec, z. B. 90° um Y).

Deshalb läuft der Download zweistufig:

1. RBXM herunterladen, mit dem eigenen Parser
   ([`tools/rbxm.py`](tools/rbxm.py)) Mesh-/Textur-IDs, Größe, Position
   und Rotation extrahieren (Chunks sind oft LZ4-/ZSTD-komprimiert – der
   Workflow installiert dafür `zstandard` und `lz4` per pip).
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
  Die Pipeline wählt die R15-Segmente per Name aus und **stapelt sie
  senkrecht** in der Stand-Pose (Obersegment oben, mit Mindestluft
  zwischen den Stücken). Die RBXM-Positionen beschreiben dagegen die
  *Bind-Pose* des Pakets (A-Pose, Arme schräg nach außen) – deren
  Übernahme und das Pressen in die senkrechte Rig-Box war die Ursache
  für verstellte/verrenkte Rig-Teile mit falscher Position und Rotation.
* **CFrame-Rotationen:** Werden vollständig gelesen (explizite Matrix
  *und* die 24 Sonder-Rotationen der Binärspec) und bei Accessoires
  angewendet – ein um 90° gedrehtes Bruststück steht sonst seitlich
  statt in Blickrichtung, Hüte/Haar schief.
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

## Auto-Merge: jeder Arbeitsbranch landet automatisch in `main`

Der Workflow [`auto-merge.yml`](.github/workflows/auto-merge.yml) merged
**jeden Push auf einen Arbeitsbranch** automatisch nach `main` – ohne
Pull Request. Es gibt keinen fest verdrahteten Quell-Branch: Gemerged wird
immer genau der Branch, auf den gepusht wurde (`agent-changes`,
`arena/<session-id>-robloxavatarrenderproject`, …):

```bash
git checkout -b arena/<session-id>-robloxavatarrenderproject
# ... Änderungen ...
git commit -am "Beschreibung"
git push origin HEAD              # löst den Auto-Merge aus
```

Ablauf: `main` auschecken → Quell-Branch fetchen → mergen →
`git push origin HEAD:main`. `main` und `renders` sind als Quelle
ausgeschlossen (kein Selbst-Merge, keine Endlosschleife). Bei einem
parallelen Push auf `main` wird automatisch neu gemerged und erneut
gepusht.

**Es wird nie ein Pull Request erstellt.** Scheitert der direkte Push
(Branch-Schutz/Rules auf `main` oder fehlende Schreibrechte des Tokens),
bricht der Lauf mit einem Issue ab; `main` bleibt unverändert. Bei einem
Merge-Konflikt wird der Merge abgebrochen und ebenfalls ein Issue mit der
Lauf-URL erstellt.

**Optional – damit Folgeläufe automatisch starten:** Pushes, die mit dem
Standard-`GITHUB_TOKEN` gemacht werden, lösen **keine** neuen Workflows aus.
Wenn nach dem Merge automatisch ein Avatar-Render starten soll, hinterlege ein
Fine-grained PAT mit *Contents: Read and write* als Secret
**`AUTO_MERGE_TOKEN`**. Der Workflow nutzt es automatisch, sobald es existiert.
Dasselbe Secret ist nötig, wenn `main` später durch Branch Rules geschützt
wird (PAT dort als Bypass eintragen).

---

## Aufbau des Repositories

```
Configs.toml                     Username + Drehung, Pose, Bildausschnitt, Hintergrund
Posen.toml                       Posen (Grad pro Körperteil + leichte Verschiebungen)
ANLEITUNG.md                     diese Anleitung
.github/workflows/
  avatar-render.yml              Push/Manuell → Avatar rendern → Artifact
  auto-merge.yml                 jeder Arbeitsbranch → main (ohne Pull Request)
  tests.yml                      Tests bei jedem Push
blender/render_glass.py          Blender-Script (Glas+Textur-Material, Kamera, Licht)
tools/
  build_avatar.py                Orchestrierung, schreibt manifest.json + OBJs
  roblox_api.py                  Web-APIs, Asset Delivery, BrickColor-Tabelle
  roblox_mesh.py                 FileMesh-Parser (v1–v7) → OBJ
  avatar_rig.py                  Stand-Pose-Rig + Accessoire-Platzierung
  avatar_pose.py                 Posen laden/validieren und auf das Rig anwenden
  render_config.py               Configs.toml lesen/prüfen (Grenzen, Schalter)
  brick_colors.json              208 Roblox-BrickColors (ID → RGB)
  tests/test_pipeline.py         Tests ohne Netzwerk/Blender
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
| `'username' ist leer` | In `Configs.toml` fehlt der Benutzername |
| `Username wurde nicht gefunden` | Tippfehler oder Account gelöscht; exakten Namen prüfen |
| Render grün, aber Avatar „nackt“ | Downloads fehlgeschlagen → Lauf-Log und `skipped` im Manifest prüfen |
| `0 Accessoires`, viele Assets „übersprungen“ | Accessoires/Körperteile kommen als RBXM-Modell → Abschnitt „RBXM“. Steht im Manifest „RBXM nicht lesbar“, fehlen vermutlich die Module `lz4`/`zstandard` (Workflow-Schritt 3 prüft das) |
| Animationen werden „übersprungen“ gezählt | Alte Version – Animationen (Typen 24, 48–56, 61, 78) werden nicht heruntergeladen und stehen nur unter `ignored_animations` |
| „Avatar 5000 Studs hoch“ | Lesefehler: Höhen werden mit Komma ausgegeben (z. B. `5,40 Studs`); ein normaler Avatar ist 5–6 Studs hoch → Abschnitt „Zahlenformat“ |
| Artifact-Download auf dem iPhone endet auf about:blank/404 | Bekanntes Verhalten der iOS-App (Artifacts sind session-gebunden). Inline-Bild in der Step Summary, Branch `renders` (`latest.png`) oder Release `avatar-render-latest` nutzen – oder Artifact im Desktop-Browser laden |
| Lauf bricht nach 45 min ab | Auflösung/Samples reduzieren (Run workflow) |

Fragen oder Wünsche? Einfach ein Issue im Repository aufmachen.
