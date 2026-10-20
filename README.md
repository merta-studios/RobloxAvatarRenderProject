# Roblox Avatar Render Pipeline

Usernamen in [`username.txt`](username.txt) eintragen, pushen, warten – und nach
wenigen Minuten liegt ein gläserner Avatar-Render als GitHub-Actions-Artifact bereit.

```
username.txt  ──►  UserId  ──►  Avatar-Definition  ──►  Assets
                                                           │
                                        RBXM-Modelle parsen (Mesh-/Textur-IDs)
                                                           │
                             render_avatar.png  ◄──  Blender Cycles (CPU)
                                Artifact + Branch "renders" + Release
```

Accessoires und getragene Körperteile liefert Roblox als RBXM-**Modelle**
aus; die FileMesh-ID steckt erst darin. Ein eigener Parser
(`tools/rbxm.py`) extrahiert Mesh-/Textur-IDs, Größe und Position – erst
danach werden die echten Meshes geladen.

![Vorschau des Rigs](docs/preview_rig.png)

*Vorschau aus `tools/preview_scene.py` (Software-Render ohne Blender,
Test-Rig mit beschriftetem Test-Texturatlas): Der Avatar steht von vorne
leicht schräg im Bild, Torso vorne/hinten und die Arm- und Beinpanels kommen
aus dem richtigen Bereich des Kleidungs-Atlasses. Den finalen Glas-Render
macht Blender (`blender/render_glass.py`).*

## Schnellstart

1. **Secret hinterlegen:** Settings → Secrets and variables → Actions →
   `ROBLOX_OPEN_CLOUD_API_KEY` (Details und nötige Berechtigungen: **[ANLEITUNG.md](ANLEITUNG.md)**).
2. `username.txt` öffnen, Roblox-Usernamen eintragen, committen, pushen.
3. Tab **Actions** → Lauf *Avatar-Render (Glas)* abwarten, bis der Punkt grün ist.
4. Ergebnis ansehen: Inline-Bild in der **Step Summary** des Laufs (geht auch
   auf dem Handy), im Branch [`renders`](../../blob/renders/latest.png)
   (`latest.png`) oder als Release
   [`avatar-render-latest`](../../releases/tag/avatar-render-latest).
   Alternativ unter **Artifacts** → `avatar-render` herunterladen
   (ZIP mit `render_avatar.png`; funktioniert nur im Browser, nicht in der
   iOS-App – Artifacts sind session-gebunden).

Ohne Secret läuft die Pipeline weiter, aber die Mesh-/Textur-Downloads schlagen
vermutlich fehl (401) – es wird dann ein Platzhalter-Körper gerendert.

## Was passiert im Workflow

| Schritt | Datei | Aufgabe |
| --- | --- | --- |
| 1 | `.github/workflows/avatar-render.yml` | Trigger, Secret, Blender-Installation, Artifact |
| 2 | `tools/build_avatar.py` | Orchestrierung: Username → UserId → Avatar → Rig |
| 3 | `tools/roblox_api.py` | Web-APIs + Asset Delivery (Open Cloud & Fallbacks) |
| 4 | `tools/rbxm.py` | RBXM-/RBXMX-Parser: Mesh-/Textur-IDs aus Modellen |
| 5 | `tools/roblox_mesh.py` | Roblox-`.mesh`-Parser (v1.00–v7.00) → OBJ |
| 6 | `tools/avatar_rig.py` | Stand-Pose-Rig, Accessoire-Platzierung |
| 6b | `tools/avatar_uv.py` | Kleidungs-UV-Atlas (Shirt/Pants, 585×559) |
| 7 | `blender/render_glass.py` | Cycles-Glasmaterial, Kamera, Licht, Render |
| 8 | `.github/workflows/avatar-render.yml` | Veröffentlichen: Artifact + Branch `renders` + Release |
| – | `tools/diagnose.py` | prüft Endpunkte/Key-Rechte (Workflow *Roblox-Diagnose*) |

Ergebnis im Build-Ordner: `manifest.json`, `parts/*.obj`, `textures/*`,
`summary.md` (landet in der GitHub-Step-Summary).

## Lokal ausprobieren

```bash
# nur den Körper bauen, ohne Netzwerk
python3 tools/build_avatar.py --dry-run --out build

# Rig/UVs/Kamera ohne Blender prüfen (braucht numpy + Pillow)
python3 tools/preview_scene.py --manifest build/manifest.json --out preview.png

# echten Avatar laden (Key optional, aber empfohlen)
export ROBLOX_OPEN_CLOUD_API_KEY="..."
python3 tools/build_avatar.py --username Builderman --out build

# rendern (Blender 4.2 muss installiert sein)
blender -b --factory-startup --python blender/render_glass.py -- \
  --manifest build/manifest.json --out render_avatar.png \
  --resolution 1280x720 --samples 64
```

Verbindungs- und Key-Test (zeigt HTTP-Status aller Roblox-Endpunkte):

```bash
python3 tools/diagnose.py --username Builderman
```

Tests (ohne Netzwerk, ohne Blender):

```bash
pip install -r requirements-dev.txt
python3 -m pytest tools/tests -q
```

## Kamera, Kleidung und Texturen

* **Blickrichtung:** Der Avatar wird von **vorne, leicht schräg** gerendert
  (Azimut 24°, Höhe 9°). Der OBJ-Import dreht Roblox-`-Z` auf Blender-`+Y`,
  deshalb steht die Kamera auf der +Y-Seite. Anpassbar mit
  `--camera-azimuth` / `--camera-elevation`; die Mathematik steckt in
  `tools/scene_camera.py`.
* **Klassische Kleidung** (Shirt/Pants) liegt im echten Roblox-UV-Atlas
  (585×559): `tools/avatar_uv.py` vergibt pro Fläche das richtige Panel –
  vorne, hinten, Seiten, oben, unten, und für Arme/Beine die
  Gliedmaßen-Blöcke. Echte R15-Meshes bekommen die Panels per Projektion,
  weil ihre eigenen UVs nicht zu Kleidungs-Atlanten passen. T-Shirt-Grafik
  und Gesicht liegen als Dekals vor Brust bzw. Gesicht.
* **Farben:** Das Manifest speichert die Durchschnittsfarbe jeder Textur,
  damit bekleidete Teile im Glas in ihrer echten Farbe leuchten; unbekleidete
  Teile bleiben klares Glas. Damit Texturen überhaupt sichtbar sind, mischt
  das Material einen deckenden Textur-Shader mit Glas (`--texture-strength`,
  Standard 0,85 – reines Glas zeigt von einer Textur fast nichts); dunkle
  Kleidung wird per Gamma aufgehellt und bleibt matt, damit das Muster
  lesbar ist.
* **Körperteile:** R15-Bundles liefern echte Maße – der Rig stapelt Beine,
  Torso, Hals und Kopf lückenlos und richtet in T-Pose gelieferte Arme auf.

## Grenzen

* GitHub-Runner haben **keine GPU** → Cycles rendert auf der CPU, 2–10 Minuten
  bei 720p/64 Samples.
* Roblox liefert **keinen** fertig geriggten Avatar als OBJ/GLB. Der Körper wird
  aus den Avatar-Farben/-Proportionen nachgebaut, Accessoires kommen als echte
  Meshes dazu und werden heuristisch an Kopf/Brust/Rücken/Händen platziert.
  Das Ergebnis ist eine künstlerische Näherung, kein 1:1-Abbild.
* Automatisierte Downloads fremder Avatare können gegen Roblox' Nutzungs-
  bedingungen verstoßen – bitte vorher prüfen (siehe ANLEITUNG.md).

Ausführlich: **[ANLEITUNG.md](ANLEITUNG.md)**
