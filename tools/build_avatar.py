#!/usr/bin/env python3
"""Laedt einen Roblox-Avatar und baut daraus eine Blender-Szene (Manifest + OBJs).

Beispiel:
    python3 tools/build_avatar.py --username Builderman --out build
    python3 tools/build_avatar.py --dry-run --out build   # ohne Netzwerk

Ergebnis (im Ausgabeordner):
    manifest.json    Beschreibung der Szene (Parts, Farben, Texturen)
    parts/*.obj      Geometrie (Koerper + echte Roblox-Accessoire-Meshes)
    textures/*       heruntergeladene Texturen (PNG/JPG)
    avatar.json      Rohdaten der Avatar-API (zum Debuggen)
    summary.md       Kurzbericht (fuer GitHub Step Summary)

Accessoires und die getragenen Koerperteile liefert Roblox als
RBXM-Modelle aus; die Mesh-/Textur-IDs werden mit tools/rbxm.py aus dem
Modell gelesen und danach wie bisher geladen. Das Skript braucht nur die
Standardbibliothek; fuer komprimierte RBXM-Chunks kommen optional die
Module `lz4` und `zstandard` dazu (im Workflow per pip installiert).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import avatar_rig  # noqa: E402
import rbxm  # noqa: E402
import roblox_api as api  # noqa: E402
import roblox_mesh  # noqa: E402

# Diese Koerperteile sind rund und werden weich schattiert, der Rest bleibt flach
# (glatte Normalen auf Quadern wirken sonst wie "Plattenpanzer").
SMOOTH_BODY_PARTS = {"kopf", "kopf_mesh"}

# Klassische Kleidung -> Vorlagen-Art. Der Renderer schneidet daraus das
# passende Feld der Roblox-Vorlage (585x559) heraus, statt das ganze Bild
# auf jede Flaeche zu legen.
CLOTHING_TEXTURE_KINDS = {
    2: "tshirt",   # TShirt (512x512, nur Vorderseite)
    11: "shirt",   # Shirt (585x559)
    12: "pants",   # Pants (585x559)
    64: "shirt",
    65: "shirt",
    66: "pants",
    67: "shirt",
    68: "shirt",
    69: "pants",
    70: "pants",
    71: "pants",
    72: "pants",
}

CLOTHING_TARGETS = {
    2: ["torso"],                                  # TShirt
    11: ["torso"],                                 # Shirt
    12: ["links_bein", "rechts_bein"],             # Pants
    18: ["kopf", "kopf_mesh"],                     # Face
    64: ["torso"],                                 # TShirtAccessory
    65: ["torso"],                                 # ShirtAccessory
    66: ["links_bein", "rechts_bein"],             # PantsAccessory
    67: ["torso"],                                 # JacketAccessory
    68: ["torso"],                                 # SweaterAccessory
    69: ["links_bein", "rechts_bein"],             # ShortsAccessory
    70: ["links_bein"],                            # LeftShoeAccessory
    71: ["rechts_bein"],                           # RightShoeAccessory
    72: ["torso"],                                 # DressSkirtAccessory
}

# AssetTypeId der getragenen Koerperteile -> Rig-Teilname. Diese Assets
# kommen als RBXM und ersetzen das jeweilige Rig-Teil durch das echte Mesh.
BODY_PART_TYPES = {
    27: "torso",        # Torso
    28: "rechts_arm",   # RightArm
    29: "links_arm",    # LeftArm
    30: "links_bein",   # LeftLeg
    31: "rechts_bein",  # RightLeg
}
HEAD_ASSET_TYPE = 17
DYNAMIC_HEAD_TYPE = 79
# Ein DynamicHead mit weniger Faces ist praktisch unsichtbar ("Headless")
HEADLESS_MAX_FACES = 20
# ... oder mit winziger Part-Groesse im RBXM (echte Koepfe: > 0,8 Studs)
HEADLESS_MAX_SIZE = 0.25
# ... oder winziger Datei-Geometrie
HEADLESS_MAX_MESH_SIZE = 0.05
# Obergrenze fuer Downloads, die erst durch RBXM-Inhalte bekannt werden
MAX_FOLLOWUP_DOWNLOADS = 40
# Sanity-Check: plausible Groesse eines einzelnen Teils in Studs
SANITY_MAX_AXIS = 12.0
SANITY_MIN_AXIS = 0.05


def log(msg: str) -> None:
    print(msg, flush=True)


def fmt_de(value: Any, nd: int = 2) -> str:
    """Zahl in deutscher Schreibweise: 5.496 -> "5,50" (Komma statt Punkt)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:.{nd}f}".replace(".", ",")


def _trimmed_size(mesh) -> tuple:
    """Robuste Ausdehnung: 1%- bis 99%-Perzentil je Achse.

    Erkennt "Speck"-Geometrie: fast alle Vertices an einem Punkt, nur
    wenige Ausreisser spannen eine grosse Bounding-Box auf (so
    verstecken manche "Headless"-Meshes ihre Unsichtbarkeit).
    """
    if not mesh.positions:
        return (0.0, 0.0, 0.0)
    out = []
    for axis in range(3):
        values = sorted(p[axis] for p in mesh.positions)
        n = len(values)
        lo_i = min(n - 1, int(n * 0.01))
        hi_i = max(0, int(n * 0.99) - 1)
        out.append(values[hi_i] - values[lo_i] if hi_i >= lo_i else 0.0)
    return tuple(out)


def head_geometry_info(mesh, rbxm_size=None):
    """Kennzahlen eines Kopf-Meshes + Headless-Verdikt.

    "Headless"-DynamicHeads (z. B. Asset 'Headless Head') verstecken
    den Kopf auf mehrere Arten - alle werden geprueft:
      * kaum Geometrie (< HEADLESS_MAX_FACES Faces)
      * winzige Part-Groesse im RBXM (< 0,25 Studs; echte Koepfe > 0,8)
      * winzige Datei-Geometrie (< 0,05 Einheiten)
      * "Speck": getrimmte Ausdehnung winzig gegenueber der BBox
      * Geometrie weit weg vom Ursprung (im Koerper versteckt)

    Rueckgabe: (ist_headless: bool, begruendung: str, kennzahlen: str)
    """
    size = mesh.size()
    center = mesh.center()
    trimmed = _trimmed_size(mesh)
    max_dim = max(size) if size else 0.0
    size_txt = "x".join(fmt_de(v, 3) for v in size)
    center_txt = "/".join(fmt_de(v, 2) for v in center)
    rbxm_txt = ("x".join(fmt_de(v, 3) for v in rbxm_size)
                if rbxm_size else "unbekannt")
    info = (f"{len(mesh.faces)} Faces, Mesh-BBox {size_txt}, "
            f"Zentrum {center_txt}, RBXM-Groesse {rbxm_txt} Studs")

    if len(mesh.faces) < HEADLESS_MAX_FACES:
        return True, "kaum Geometrie", info
    if rbxm_size and max(rbxm_size) < HEADLESS_MAX_SIZE:
        return True, "winzige Part-Groesse im RBXM", info
    if max_dim < HEADLESS_MAX_MESH_SIZE:
        return True, "winzige Datei-Geometrie", info
    if max_dim > 0 and max(trimmed) < 0.02 * max_dim:
        return True, "Punkt-Geometrie in grosser BBox versteckt", info
    if max_dim > 0 and max(abs(v) for v in center) > 3.0 * max_dim:
        return True, "Geometrie weit ausserhalb des Ursprungs versteckt", info
    return False, "", info


# Erwartete R15-Segmentnamen je Rig-Koerperteil. Bundle-RBXMs enthalten
# oft mehrere Varianten (R6/R15/LOD) - nur diese Segmente gehoeren zum
# sichtbaren R15-Koerperteil.
R15_SEGMENT_NAMES = {
    "torso": ["UpperTorso", "LowerTorso"],
    "links_arm": ["LeftUpperArm", "LeftLowerArm", "LeftHand"],
    "rechts_arm": ["RightUpperArm", "RightLowerArm", "RightHand"],
    "links_bein": ["LeftUpperLeg", "LeftLowerLeg", "LeftFoot"],
    "rechts_bein": ["RightUpperLeg", "RightLowerLeg", "RightFoot"],
}


def _normalize_name(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _sort_segments_top_down(group: list) -> list:
    """Fallback-Reihenfolge fuer Segmente ohne passende Instanznamen.

    Von oben nach unten nach der Bind-Pose-Y sortieren; doppelte Segmente
    desselben Mesh-Assets (Bundles liefern oft zwei Varianten) werden
    dedupliziert, sonst mischt der Stapel beide Varianten.
    """
    seen = set()
    unique = []
    for mp in group:
        key = mp.get("mesh_asset_id") or id(mp.get("mesh"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(mp)
    if all(mp.get("rbxm_position") for mp in unique):
        return sorted(unique, key=lambda mp: float(mp["rbxm_position"][1]),
                      reverse=True)
    return unique


def _pick_r15_segments(group: list, expected_names: list) -> list:
    """Aus einer Mesh-Gruppe die R15-Segmente per Instanzname auswaehlen.

    Je erwartetem Segmentnamen wird der erste passende Eintrag
    genommen (Namensgleichheit nach Normalisierung, z. B.
    'LeftUpperArm' ~ 'leftupperarm'). Liefert [] wenn nichts passt.
    """
    picked: list = []
    for expected in expected_names:
        wanted = _normalize_name(expected)
        for mesh_part in group:
            if _normalize_name(mesh_part.get("label") or "") == wanted:
                picked.append(mesh_part)
                break
    return picked


def stacked_part_size(items: list, gap: float = 0.012) -> Optional[tuple]:
    """Groesse eines Koerperteils aus den RBXM-Part-Groessen (in Studs).

    Roblox-Bundles liefern die Part-Groesse in echten Studs
    (``MeshPart.Size``), die Datei-Geometrie dagegen in willkuerlichen
    Mesh-Einheiten. Fuer das Rig zaehlen die Stud-Masse - sonst wird ein
    2x2x1-Torso aus einem Mesh mit Einheit 3x1x0,5 ploetzlich 3 Studs breit
    ("zu gross / gedehnt"). Fehlt eine plausible Part-Groesse, dient die
    Geometrie als Rueckfall.
    """
    sizes = []
    for mesh_part in items:
        size = mesh_part.get("rbxm_size")
        if not size or len(size) != 3 or min(size) <= 1e-6:
            size = mesh_part["mesh"].size()
        sizes.append(tuple(float(v) for v in size))
    if not sizes:
        return None
    return (max(s[0] for s in sizes),
            sum(s[1] for s in sizes) + gap * (len(sizes) - 1),
            max(s[2] for s in sizes))


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def asset_type_id(asset: Dict[str, Any], details: Dict[str, Any]) -> int:
    """AssetTypeId aus der Avatar-API oder aus den Asset-Details."""
    at = asset.get("assetType") or {}
    if isinstance(at, dict) and at.get("id"):
        return int(at["id"])
    if details.get("assetTypeId"):
        return int(details["assetTypeId"])
    return 0


def texture_filename(asset_id: int, data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return f"{asset_id}.png"
    if data[:3] == b"\xff\xd8\xff":
        return f"{asset_id}.jpg"
    return f"{asset_id}.bin"


def write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def effective_scales(raw: Any) -> Dict[str, float]:
    """Normalisiert die Scales (fehlende Werte = 1.0 / 0.0)."""
    scales = dict(api.DEFAULT_SCALES)
    if isinstance(raw, dict):
        for key in ("height", "width", "head", "depth", "proportion", "bodyType"):
            if raw.get(key) is not None:
                try:
                    scales[key] = float(raw[key])
                except (TypeError, ValueError):
                    pass
    return scales


def effective_body_colors(raw: Any) -> Dict[str, Any]:
    colors = dict(api.DEFAULT_BODY_COLORS)
    if isinstance(raw, dict):
        for key in list(colors.keys()):
            if raw.get(key) is not None:
                colors[key] = raw[key]
    return colors


def sanity_check(mesh: roblox_mesh.MeshData, target_max: float, label: str,
                 warnings: List[str]) -> roblox_mesh.MeshData:
    """Prueft ein platziertes Teil auf plausible Groesse und skaliert sonst neu.

    Mesh-Einheiten aus RBXM-Modellen sind nicht immer Studs - ein Teil mit
    200 Studs wuerde die ganze Szene sprengen. Teile groesser als
    SANITY_MAX_AXIS oder kleiner als SANITY_MIN_AXIS werden deshalb um
    ihre Mitte auf die Zielgroesse des Ankers skaliert (mit Warnung).
    """
    size = mesh.size()
    max_axis = max(size) if size else 0.0
    if SANITY_MIN_AXIS <= max_axis <= SANITY_MAX_AXIS:
        return mesh
    if max_axis <= 0.0 or target_max <= 0.0:
        return mesh
    center = mesh.center()
    factor = target_max / max_axis
    rescaled = (
        mesh.transform(1.0, (-center[0], -center[1], -center[2]))
        .transform(factor)
        .transform(1.0, center)
    )
    warnings.append(
        f"Sanity-Check: Teil '{label}' war {fmt_de(max_axis)} Studs gross und "
        f"wurde auf {fmt_de(target_max)} Studs skaliert "
        "(Mesh-Einheiten sind nicht immer Studs)."
    )
    return rescaled


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------


def build(username_raw: str, out_dir: str, dry_run: bool = False,
          max_assets: int = 80, verbose: bool = True) -> Dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    parts_dir = os.path.join(out_dir, "parts")
    textures_dir = os.path.join(out_dir, "textures")
    os.makedirs(parts_dir, exist_ok=True)
    os.makedirs(textures_dir, exist_ok=True)

    warnings: List[str] = []
    parts: List[Dict[str, Any]] = []
    textures: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    ignored: List[Dict[str, Any]] = []
    avatar_raw: Dict[str, Any] = {}
    mesh_parts: List[Dict[str, Any]] = []
    headless = {"active": False, "label": "", "reason": "", "info": ""}

    def set_headless(label: str, reason: str = "", info: str = "") -> None:
        if not headless["active"]:
            headless["active"] = True
            headless["label"] = label
            headless["reason"] = reason
            headless["info"] = info

    # ---- Avatar-Daten -----------------------------------------------------
    if dry_run:
        username = api.normalize_username(username_raw) if username_raw else "Builderman"
        user_id = 0
        avatar_raw = {
            "_source": "dry-run",
            "playerAvatarType": "R15",
            "scales": dict(api.DEFAULT_SCALES),
            "bodyColors": dict(api.DEFAULT_BODY_COLORS),
            "assets": [],
        }
        log("[1/4] Dry-Run: keine Netzwerk-Anfragen, Koerper mit Standardfarben")
    else:
        username = api.normalize_username(username_raw)
        client = api.HttpClient(verbose=verbose)
        if not client.api_key:
            warnings.append(
                "Kein Secret ROBLOX_OPEN_CLOUD_API_KEY gesetzt - Asset-Downloads "
                "laufen nur ueber die oeffentlichen Fallback-Endpunkte und koennen "
                "fehlschlagen (401)."
            )
            log("  ! ROBLOX_OPEN_CLOUD_API_KEY ist nicht gesetzt (Fallback aktiv)")
        else:
            log("  i Open-Cloud-Key gefunden (x-api-key wird mitgeschickt)")

        log(f"[1/4] Roblox-User-ID fuer '{username}' ermitteln ...")
        user_id, username = api.resolve_user_id(client, username)
        log(f"      -> UserId {user_id} ({username})")

        log("[2/4] Avatar-Definition laden ...")
        avatar_raw = api.fetch_avatar(client, user_id)
        if avatar_raw.get("_degraded"):
            warnings.append(
                "Nur 'currently-wearing' verfuegbar - Farben/Scales sind Standardwerte."
            )

        # -- Helfer fuer Nachlade-Downloads (IDs aus RBXM-Modellen) ---------
        followup_cache: Dict[int, Optional[bytes]] = {}
        followup_state = {"attempts": 0, "warned": False}
        texture_files: Dict[int, Optional[str]] = {}

        def download_followup(asset_id: int) -> Optional[bytes]:
            """Laedt eine ID, die erst in einem RBXM stand (Cache + Limit)."""
            if asset_id in followup_cache:
                return followup_cache[asset_id]
            if followup_state["attempts"] >= MAX_FOLLOWUP_DOWNLOADS:
                if not followup_state["warned"]:
                    followup_state["warned"] = True
                    warnings.append(
                        f"RBXM-Nachlade-Limit ({MAX_FOLLOWUP_DOWNLOADS}) erreicht - "
                        "weitere Meshes/Texturen werden nicht geladen."
                    )
                followup_cache[asset_id] = None
                return None
            followup_state["attempts"] += 1
            result = api.download_asset(client, asset_id)
            followup_cache[asset_id] = result.data
            return result.data

        def register_texture(asset_id: int, data: bytes, type_name: str,
                             label: str, type_id: int) -> Optional[str]:
            fname = texture_filename(asset_id, data)
            with open(os.path.join(textures_dir, fname), "wb") as fh:
                fh.write(data)
            rel = f"textures/{fname}"
            textures.append({"asset_id": asset_id, "name": label,
                             "asset_type": type_name, "file": rel,
                             "type_id": type_id})
            texture_files[asset_id] = rel
            return rel

        def save_texture_by_id(asset_id: int, type_name: str, label: str,
                               type_id: int) -> Optional[str]:
            if asset_id in texture_files:
                return texture_files[asset_id]
            raw = download_followup(asset_id)
            rel = None
            if raw is not None and api.classify_asset(raw, 0) == "texture":
                rel = register_texture(asset_id, raw, type_name, label, type_id)
            texture_files[asset_id] = rel
            return rel

        def add_mesh(raw: bytes, parent_asset_id: int, mesh_asset_id: int,
                     type_id: int, type_name: str, label: str,
                     rbxm_size: Optional[Any], texture_file: Optional[str],
                     rbxm_position: Optional[Any] = None,
                     rbxm_rotation: Optional[Any] = None) -> None:
            """Parst ein FileMesh und reiht es in mesh_parts ein."""
            try:
                mesh = roblox_mesh.parse(raw)
            except roblox_mesh.MeshParseError as exc:
                skipped.append({"asset_id": mesh_asset_id, "name": label,
                                "asset_type": type_name,
                                "reason": f"Mesh nicht lesbar: {exc}"})
                return
            head_info = ""
            if type_id == DYNAMIC_HEAD_TYPE:
                is_headless, reason, head_info = head_geometry_info(
                    mesh, rbxm_size)
                if mesh.is_empty or is_headless:
                    # DynamicHead ohne sichtbare Geometrie = "Headless"
                    set_headless(label or type_name, reason, head_info)
                    return
            if mesh.is_empty:
                skipped.append({"asset_id": mesh_asset_id, "name": label,
                                "asset_type": type_name,
                                "reason": "Mesh ohne Faces"})
                return
            mesh_parts.append({
                "asset_id": parent_asset_id,
                "mesh_asset_id": mesh_asset_id,
                "type_id": type_id,
                "type_name": type_name,
                "label": label,
                "mesh": mesh,
                "rbxm_size": rbxm_size,
                "rbxm_position": rbxm_position,
                "rbxm_rotation": rbxm_rotation,
                "texture_file": texture_file,
                "head_info": head_info,
            })

        def process_rbxm(data: bytes, parent_asset_id: int, type_id: int,
                         type_name: str, label: str, depth: int = 1) -> None:
            """RBXM-Modell auswerten: Mesh-/Textur-IDs nachladen."""
            try:
                entries = rbxm.extract_assets(data)
            except rbxm.RbxmError as exc:
                skipped.append({"asset_id": parent_asset_id, "name": label,
                                "asset_type": type_name,
                                "reason": f"RBXM nicht lesbar: {exc}"})
                return
            if not entries:
                if type_id == DYNAMIC_HEAD_TYPE:
                    set_headless(label or type_name)
                else:
                    skipped.append({"asset_id": parent_asset_id, "name": label,
                                    "asset_type": type_name,
                                    "reason": "RBXM enthielt keine Mesh-/Textur-IDs"})
                return

            parts_before, skips_before = len(mesh_parts), len(skipped)
            textures_before, headless_before = len(textures), headless["active"]
            for entry in entries:
                entry_label = entry.get("name") or label
                texture_file = None
                if entry.get("texture_id"):
                    texture_file = save_texture_by_id(
                        int(entry["texture_id"]), type_name, entry_label, type_id)
                mesh_id = entry.get("mesh_id")
                if not mesh_id:
                    continue
                mesh_id = int(mesh_id)
                raw = download_followup(mesh_id)
                if raw is None:
                    skipped.append({"asset_id": mesh_id, "name": entry_label,
                                    "asset_type": type_name,
                                    "reason": "Mesh-Download aus RBXM fehlgeschlagen "
                                              f"(Parent-Asset {parent_asset_id})"})
                    continue
                sub_kind = api.classify_asset(raw, 4)
                if sub_kind == "rbxm" and depth < 2:
                    process_rbxm(raw, mesh_id, type_id, type_name, entry_label, depth + 1)
                    continue
                if sub_kind == "texture":
                    # Die ID war doch eine Textur (kann beim Fallback-Scan
                    # in rbxm.py passieren) - als Textur ablegen.
                    if mesh_id not in texture_files:
                        register_texture(mesh_id, raw, type_name, entry_label, type_id)
                    continue
                if sub_kind != "mesh":
                    skipped.append({"asset_id": mesh_id, "name": entry_label,
                                    "asset_type": type_name,
                                    "reason": f"Unerwarteter Dateityp '{sub_kind}' "
                                              "fuer Mesh-ID aus RBXM"})
                    continue
                log(f"      + Mesh {mesh_id} aus RBXM ({entry.get('class', '?')}"
                    f"{', mit Textur' if texture_file else ''})")
                add_mesh(raw, parent_asset_id, mesh_id, type_id, type_name,
                         entry_label, entry.get("size"), texture_file,
                         entry.get("position"), entry.get("rotation"))

            if (len(mesh_parts) == parts_before and len(skipped) == skips_before
                    and len(textures) == textures_before
                    and headless["active"] == headless_before):
                skipped.append({"asset_id": parent_asset_id, "name": label,
                                "asset_type": type_name,
                                "reason": "RBXM: keine verwertbare Geometrie gefunden"})

        # ---- Assets ---------------------------------------------------------
        assets = avatar_raw.get("assets") or []
        log(f"[3/4] {len(assets)} Assets verarbeiten ...")

        for index, asset in enumerate(assets[:max_assets]):
            try:
                aid = int(asset.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if not aid:
                continue
            label = asset.get("name") or ""
            declared = asset_type_id(asset, {})
            if declared in api.IGNORED_ASSET_TYPES:
                # Animationen/Audio: gar nicht herunterladen, nur zaehlen
                ignored.append({
                    "asset_id": aid, "name": label,
                    "asset_type": api.ASSET_TYPE_NAMES.get(declared, "Animation"),
                })
                continue

            details = api.fetch_asset_details(client, aid)
            type_id = asset_type_id(asset, details)
            type_name = api.ASSET_TYPE_NAMES.get(type_id, "Unbekannt")
            if not label:
                label = details.get("name") or type_name
            if type_id in api.IGNORED_ASSET_TYPES:
                ignored.append({"asset_id": aid, "name": label,
                                "asset_type": type_name})
                continue

            dl = api.download_asset(client, aid)
            if dl.data is None:
                skipped.append({
                    "asset_id": aid,
                    "name": label,
                    "asset_type": type_name,
                    "reason": "Download fehlgeschlagen",
                    "detail": dl.note[:400],
                })
                log(f"      - {aid} {type_name}: Download fehlgeschlagen ({dl.note[:120]})")
                continue

            # Manche Endpunkte liefern RBXM-Modelle gzip-gepackt aus -
            # erst entpacken, sonst erkennt classify_asset sie nicht.
            dl.data = api.maybe_decompress(dl.data)
            kind = api.classify_asset(dl.data, type_id)
            log(f"      - {aid} {type_name:<22} {kind:<8} {len(dl.data)/1024:8.1f} KB via {dl.source}")

            if kind == "mesh":
                add_mesh(dl.data, aid, aid, type_id, type_name, label, None, None)
            elif kind == "texture":
                fname = texture_filename(aid, dl.data)
                with open(os.path.join(textures_dir, fname), "wb") as fh:
                    fh.write(dl.data)
                textures.append({"asset_id": aid, "name": label, "asset_type": type_name,
                                 "file": f"textures/{fname}", "type_id": type_id})
            elif kind == "rbxm":
                # Accessoires und Koerperteile kommen als Modell - die
                # eigentlichen Mesh-/Textur-IDs stecken darin.
                process_rbxm(dl.data, aid, type_id, type_name, label)
            else:
                skipped.append({"asset_id": aid, "name": label, "asset_type": type_name,
                                "reason": f"Dateityp '{kind}' wird nicht unterstuetzt"})

    # ---- Koerper bauen ----------------------------------------------------
    log("[4/4] Rig bauen und OBJ-Dateien schreiben ...")
    scales = effective_scales(avatar_raw.get("scales"))
    body_colors = effective_body_colors(avatar_raw.get("bodyColors"))
    body_parts, metrics = avatar_rig.build_body(body_colors, scales)

    # Texturen den Kleidungs-Parts zuordnen. Zusaetzlich wird vermerkt,
    # ob es klassische Kleidung ist: deren Bilder sind Vorlagen mit
    # getrennten Feldern fuer Vorder-/ Rueckseite und Aermel. Der Renderer
    # schneidet daraus das passende Feld heraus, sonst liegt die komplette
    # Vorlage (mit Aermeln und transparenten Luecken) auf jeder Flaeche.
    texture_for_part: Dict[str, str] = {}
    clothing_kind_by_file: Dict[str, str] = {}
    for tex in textures:
        type_id = int(tex["type_id"])
        kind = CLOTHING_TEXTURE_KINDS.get(type_id)
        if kind:
            clothing_kind_by_file[tex["file"]] = kind
        for target in CLOTHING_TARGETS.get(type_id, []):
            texture_for_part.setdefault(target, tex["file"])

    # Echte Koerperteil-Meshes (Typ 27-31) den Rig-Teilen zuordnen.
    # R15-Bundles liefern pro Asset mehrere Segmente (z. B. LeftArm =
    # UpperArm + LowerArm + Hand, teils in mehreren Varianten). Die
    # R15-Segmente werden anhand ihrer Instanznamen ausgewaehlt und
    # **senkrecht gestapelt** (Stand-Pose). Die RBXM-Positionen
    # beschreiben dagegen die Bind-Pose des Pakets (A-Pose, Arme schraeg
    # nach aussen) - deren Uebernahme war die Ursache fuer falsche
    # Position und Rotation der Rig-Teile beim Pressen in die senkrechte
    # Rig-Box.
    body_groups: Dict[str, list] = {}
    for mesh_part in mesh_parts:
        rig_name = BODY_PART_TYPES.get(int(mesh_part["type_id"]))
        if rig_name is not None:
            body_groups.setdefault(rig_name, []).append(mesh_part)

    body_replacements: Dict[str, Dict[str, Any]] = {}
    for rig_name, group in body_groups.items():
        if len(group) == 1:
            body_replacements[rig_name] = group[0]
            continue
        segments = _pick_r15_segments(group, R15_SEGMENT_NAMES.get(rig_name, []))
        mergeable = segments if len(segments) >= 2 else _sort_segments_top_down(group)
        mergeable = [mp for mp in mergeable
                     if mp.get("mesh") is not None and not mp["mesh"].is_empty]
        if 2 <= len(mergeable) <= 6:
            stacked = roblox_mesh.stack_segments(
                [(mp["mesh"], mp.get("rbxm_size") or mp["mesh"].size())
                 for mp in mergeable],
                source_version="stacked-rbxm")
            pseudo = dict(mergeable[0])
            pseudo["mesh"] = stacked
            pseudo["texture_file"] = next(
                (mp["texture_file"] for mp in mergeable
                 if mp.get("texture_file")), None)
            names = ", ".join(mp.get("label") or "?" for mp in mergeable)
            pseudo["note_suffix"] = (
                f", {len(mergeable)} R15-Segmente senkrecht gestapelt "
                f"(Stand-Pose statt Bind-Pose: {names})")
            pseudo["stacked_size"] = stacked_part_size(mergeable)
            body_replacements[rig_name] = pseudo
        else:
            largest = max(group, key=lambda mp: max(mp["mesh"].size()))
            body_replacements[rig_name] = largest
            warnings.append(
                f"Koerperteil '{rig_name}': {len(group)} Meshes gefunden, "
                f"aber kein Stapel aus 2-6 Segmenten moeglich - "
                f"nur das groesste Segment verwendet "
                f"(Asset {largest['asset_id']}).")

    # Kopf: echtes Head-/DynamicHead-Mesh suchen (DynamicHead hat Vorrang).
    # Degenerierte DynamicHead-Meshes (< HEADLESS_MAX_FACES) sind bereits
    # in add_mesh() als Headless aussortiert und stehen nicht in mesh_parts.
    head_mesh: Optional[Dict[str, Any]] = None
    for wanted_type in (DYNAMIC_HEAD_TYPE, HEAD_ASSET_TYPE):
        for mesh_part in mesh_parts:
            if int(mesh_part["type_id"]) == wanted_type:
                head_mesh = mesh_part
                break
        if head_mesh is not None:
            break

    # Rig-Boxen an die echten Mesh-Groessen anpassen: Vorher wurden die
    # Bundle-Meshes achsweise in Schema-F-Boxen gepresst (Arme/Beine/Torso
    # "gedehnt", "zu gross"). Jetzt liefert das Mesh die Proportionen, die
    # Boxen werden daraus neu zusammengesetzt (Beine auf dem Boden, Torso
    # darauf, Arme an der Schulter, Kopf auf dem Hals).
    mesh_sizes: Dict[str, tuple] = {}
    for rig_name, replacement in body_replacements.items():
        size = replacement.get("stacked_size")
        if not size:
            size = replacement.get("rbxm_size") or replacement["mesh"].size()
        if max(size) > 1e-6:
            mesh_sizes[rig_name] = tuple(float(v) for v in size)
    head_natural = None
    if head_mesh is not None and not headless["active"]:
        head_size = head_mesh.get("rbxm_size") or head_mesh["mesh"].size()
        if max(head_size) > 1e-6:
            head_natural = tuple(float(v) for v in head_size)
    if mesh_sizes:
        body_parts, metrics = avatar_rig.relayout_body(
            body_parts, metrics, mesh_sizes, scales, head_natural,
            headless=headless["active"])
        if head_natural is not None:
            warnings.append(
                "Rig aus den echten Mesh-Groessen neu aufgebaut "
                f"(Kopf {fmt_de(metrics.head_radius * 2)} Studs, "
                f"Torso {fmt_de(metrics.torso_width)} breit) - "
                "keine achsweise Streckung der Koerperteile mehr.")

    body_parts_from_mesh = 0
    for part in body_parts:
        if part.name == "kopf":
            if headless["active"]:
                continue  # Headless-Avatar: gar kein Kopf
            if head_mesh is not None:
                size = head_mesh["mesh"].size()
                max_dim = max(size) or 1.0
                # Kopf-Mesh gleichmaessig auf den Kopfdurchmesser bringen
                # (vorher 2,15x Radius -> Kopf zu gross).
                target = metrics.head_radius * 2.0
                placed = head_mesh["mesh"].transform(target / max_dim)
                c = placed.center()
                placed = placed.transform(1.0, (-c[0], metrics.head_center_y - c[1], -c[2]))
                placed = sanity_check(placed, target, "Kopf", warnings)
                fname = "kopf_mesh.obj"
                placed.write_obj(os.path.join(parts_dir, fname))
                head_texture = (head_mesh.get("texture_file")
                                or texture_for_part.get("kopf_mesh"))
                parts.append({
                    "name": "kopf_mesh",
                    "obj": f"parts/{fname}",
                    "color": part.color,
                    "kind": "body",
                    "smooth": True,
                    "texture": head_texture,
                    "texture_kind": clothing_kind_by_file.get(head_texture or ""),
                    "asset_id": head_mesh["asset_id"],
                    "note": ("Echtes Kopf-Mesh des Avatars"
                             + (f" ({head_mesh['head_info']})"
                                if head_mesh.get("head_info") else "")),
                })
                warnings.append("Kopf wurde durch das echte Head-Mesh des Avatars ersetzt.")
                continue

        replacement = body_replacements.get(part.name)
        if replacement is not None:
            lo, hi = part.mesh.bbox()
            # Box stammt jetzt aus dem Mesh selbst -> achsweise ist hier
            # nur noch die Avatar-Skalierung (width/height/depth), keine
            # Verzerrung mehr.
            placed = avatar_rig.fit_mesh_to_part(replacement["mesh"], lo, hi,
                                                 uniform=False)
            placed = sanity_check(placed, max(hi[i] - lo[i] for i in range(3)),
                                  part.name, warnings)
            fname = f"{part.name}.obj"
            placed.write_obj(os.path.join(parts_dir, fname))
            body_texture = (replacement.get("texture_file")
                            or texture_for_part.get(part.name))
            parts.append({
                "name": part.name,
                "obj": f"parts/{fname}",
                "color": part.color,
                "kind": "body",
                "smooth": True,
                "texture": body_texture,
                "texture_kind": clothing_kind_by_file.get(body_texture or ""),
                "asset_id": replacement["asset_id"],
                "note": (f"Echtes Mesh aus RBXM (Mesh-Asset "
                         f"{replacement['mesh_asset_id']})"
                         + replacement.get("note_suffix", "")),
            })
            body_parts_from_mesh += 1
            continue

        fname = f"{part.name}.obj"
        part.mesh.write_obj(os.path.join(parts_dir, fname))
        parts.append({
            "name": part.name,
            "obj": f"parts/{fname}",
            "color": part.color,
            "kind": "body",
            "smooth": part.name in SMOOTH_BODY_PARTS,
            "texture": texture_for_part.get(part.name),
            "texture_kind": clothing_kind_by_file.get(
                texture_for_part.get(part.name) or ""),
        })

    if headless["active"]:
        detail = ""
        if headless["reason"]:
            detail += f" Grund: {headless['reason']}."
        if headless["info"]:
            detail += f" Kennzahlen: {headless['info']}."
        warnings.append(
            "Headless/DynamicHead erkannt: Das Kopf-Asset "
            f"'{headless['label']}' enthaelt keine sichtbare Geometrie - "
            f"der Rig-Kopf wird nicht gerendert.{detail}"
        )

    # Accessoires platzieren
    consumed = {id(mp) for mp in body_replacements.values()}
    if head_mesh is not None:
        consumed.add(id(head_mesh))
    used_filenames = set()
    accessory_assets = 0
    for mesh_part in mesh_parts:
        if id(mesh_part) in consumed:
            continue
        if int(mesh_part["type_id"]) in BODY_PART_TYPES:
            continue  # Duplikat (Warnung steht oben)
        if int(mesh_part["type_id"]) == HEAD_ASSET_TYPE:
            continue  # nur ein Kopf wird verwendet
        placed, note = avatar_rig.place_accessory(
            mesh_part["mesh"], int(mesh_part["type_id"]), metrics,
            size_hint=mesh_part.get("rbxm_size"),
            rotation=mesh_part.get("rbxm_rotation"))
        if "unplausibel" in note:
            # Diagnose: die Part-Groesse im RBXM passt nicht zur
            # Accessoire-Art (z. B. Platzhalter 1x1x1 bei einem 3 Studs
            # breiten Hut) - es gilt die Zielgroesse der Accessoire-Art.
            warnings.append(
                f"Accessoire '{mesh_part['label'] or mesh_part['type_name']}': "
                "Groesse aus dem RBXM war unplausibel und wurde auf die "
                "Zielgroesse der Accessoire-Art korrigiert.")
        placed = sanity_check(
            placed, avatar_rig.accessory_target_size(int(mesh_part["type_id"]), metrics),
            mesh_part["label"] or mesh_part["type_name"], warnings)
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                       for ch in (mesh_part["label"] or mesh_part["type_name"]))[:28]
        file_id = mesh_part.get("mesh_asset_id") or mesh_part["asset_id"]
        fname = f"accessoire_{file_id}_{safe or 'teil'}.obj"
        counter = 2
        while fname in used_filenames:
            fname = f"accessoire_{file_id}_{safe or 'teil'}_{counter}.obj"
            counter += 1
        used_filenames.add(fname)
        placed.write_obj(os.path.join(parts_dir, fname))
        parts.append({
            "name": f"{safe or mesh_part['type_name']} ({mesh_part['asset_id']})",
            "obj": f"parts/{fname}",
            "color": "#dff4ff",
            "kind": "accessory",
            "smooth": True,
            "asset_id": mesh_part["asset_id"],
            "asset_type": mesh_part["type_name"],
            "note": note,
            "texture": mesh_part.get("texture_file"),
        })
        accessory_assets += 1

    # ---- Manifest ---------------------------------------------------------
    bounds_lo = [1e9, 1e9, 1e9]
    bounds_hi = [-1e9, -1e9, -1e9]
    for part in parts:
        path = os.path.join(out_dir, part["obj"])
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("v "):
                        _, x, y, z = line.split()
                        for i, val in enumerate((float(x), float(y), float(z))):
                            bounds_lo[i] = min(bounds_lo[i], val)
                            bounds_hi[i] = max(bounds_hi[i], val)
        except OSError:
            continue
    if bounds_lo[0] > bounds_hi[0]:
        bounds_lo, bounds_hi = [-1, 0, -1], [1, 5, 1]

    manifest = {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "username": username,
        "user_id": user_id,
        "avatar_type": avatar_raw.get("playerAvatarType") or "R15",
        "avatar_source": avatar_raw.get("_source", ""),
        "dry_run": dry_run,
        "scales": scales,
        "body_colors": body_colors,
        "metrics": {
            "height": round(metrics.height, 3),
            "height_de": fmt_de(metrics.height),
            "torso_width": round(metrics.torso_width, 3),
            "head_radius": round(metrics.head_radius, 3),
            "headless": bool(headless["active"]),
            # Groessen der Koerperteile (Studs) - zur Kontrolle der
            # Proportionen im Manifest / in der Step Summary.
            "parts": {p.name: [round(v, 3) for v in p.mesh.size()]
                      for p in body_parts},
        },
        "bbox": {"min": [round(v, 3) for v in bounds_lo],
                 "max": [round(v, 3) for v in bounds_hi]},
        "parts": parts,
        "textures": textures,
        "skipped": skipped,
        "ignored": ignored,
        "warnings": warnings,
        "stats": {
            "parts": len(parts),
            "body_parts": len(body_parts),
            "body_parts_from_mesh": body_parts_from_mesh,
            "accessories": accessory_assets,
            "textures": len(textures),
            "skipped": len(skipped),
            # Alles in "skipped" ist ein echter Fehlschlag (Download/Parse);
            # Animationen stehen getrennt unter ignored_animations.
            "failed": len(skipped),
            "ignored_animations": len(ignored),
            "assets_seen": len(avatar_raw.get("assets") or []),
        },
    }
    write_json(os.path.join(out_dir, "manifest.json"), manifest)
    write_json(os.path.join(out_dir, "avatar.json"), avatar_raw)
    write_summary(os.path.join(out_dir, "summary.md"), manifest)

    log("")
    log(f"Fertig: {manifest['stats']['parts']} Teile "
        f"({accessory_assets} Accessoires, {body_parts_from_mesh} Koerperteile "
        f"aus echten Meshes, {len(textures)} Texturen), "
        f"{len(skipped)} Assets fehlgeschlagen, "
        f"{len(ignored)} Animationen ignoriert")
    log(f"Höhe: {fmt_de(manifest['metrics']['height'])} Studs (typisch 5–6), "
        f"Bounding-Box: {manifest['bbox']['min']} .. {manifest['bbox']['max']}")
    if warnings:
        for w in warnings:
            log(f"  ! {w}")
    return manifest


def write_summary(path: str, manifest: Dict[str, Any]) -> None:
    s = manifest["stats"]
    height_de = manifest["metrics"].get("height_de") or fmt_de(manifest["metrics"]["height"])
    lines = [
        f"# Avatar-Render: `{manifest['username']}`",
        "",
        f"* UserId: **{manifest['user_id']}**",
        f"* Avatar-Typ: **{manifest['avatar_type']}**",
        f"* Quelle: `{manifest['avatar_source']}`",
        f"* Teile: **{s['parts']}** (Accessoires: {s['accessories']}, "
        f"Körperteile aus echten Meshes: {s.get('body_parts_from_mesh', 0)}, "
        f"Texturen: {s['textures']}, fehlgeschlagen: {s.get('failed', s['skipped'])}, "
        f"Animationen ignoriert: {s.get('ignored_animations', 0)})",
        f"* Höhe: {height_de} Studs (typisch 5–6)",
        "",
        "## Teile",
        "",
        "| Teil | Typ | Farbe | Notiz |",
        "| --- | --- | --- | --- |",
    ]
    for part in manifest["parts"]:
        note = (part.get("note") or "").replace("|", "/")
        lines.append(f"| {part['name']} | {part['kind']} | `{part['color']}` | {note} |")
    if manifest["skipped"]:
        lines += ["", "## Fehlgeschlagen / übersprungen", "",
                  "| Asset | Typ | Grund |", "| --- | --- | --- |"]
        for item in manifest["skipped"]:
            lines.append(f"| {item.get('asset_id')} | {item.get('asset_type', '')} | "
                         f"{item.get('reason', '')} |")
    if manifest.get("ignored"):
        names = ", ".join(
            f"{item.get('name') or item.get('asset_id')} ({item.get('asset_type', '')})"
            for item in manifest["ignored"]
        )
        lines += ["", "## Ignoriert (Animationen/Audio, werden nicht gerendert)", "", names]
    if manifest["warnings"]:
        lines += ["", "## Hinweise", ""]
        lines += [f"* {w}" for w in manifest["warnings"]]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def read_username_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Roblox-Avatar -> Blender-Szene")
    parser.add_argument("--username", default=None,
                        help="Roblox-Username (sonst username.txt)")
    parser.add_argument("--username-file", default="username.txt")
    parser.add_argument("--out", default="build", help="Ausgabeordner (Default: build)")
    parser.add_argument("--dry-run", action="store_true",
                        help="ohne Netzwerk: nur Koerper mit Standardfarben bauen")
    parser.add_argument("--max-assets", type=int, default=80)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    username = args.username or read_username_file(args.username_file)
    if not username and not args.dry_run:
        print("FEHLER: kein Username uebergeben und username.txt ist leer.", file=sys.stderr)
        return 2

    try:
        build(username, args.out, dry_run=args.dry_run, max_assets=args.max_assets,
              verbose=not args.quiet)
    except api.RobloxError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
