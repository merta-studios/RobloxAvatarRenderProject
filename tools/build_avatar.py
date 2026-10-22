#!/usr/bin/env python3
"""Laedt einen Roblox-Avatar und baut daraus eine Blender-Szene (Manifest + OBJs).

Beispiel:
    python3 tools/build_avatar.py --username Builderman --out build
    python3 tools/build_avatar.py --dry-run --out build   # ohne Netzwerk

Ergebnis (im Ausgabeordner):
    manifest.json    Beschreibung der Szene (Parts, Farben, Texturen, Render)
    parts/*.obj      Geometrie (Koerper + echte Roblox-Accessoire-Meshes)
    textures/*       heruntergeladene Texturen (PNG/JPG)
    avatar.json      Rohdaten der Avatar-API (zum Debuggen)
    summary.md       Kurzbericht (nur fuer Debug-Artefakte)
    hintergrund.*    Hintergrundbild aus der Configs.toml (optional)

Alle Render-Einstellungen (Username, Drehung, Pose, Bildausschnitt,
Hintergrund) stehen in ``Configs.toml``; die Posen in ``Posen.toml``.

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
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import avatar_pose  # noqa: E402
import avatar_rig  # noqa: E402
import avatar_uv  # noqa: E402
import rbxm  # noqa: E402
import render_config  # noqa: E402
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

    Bevorzugt `R15ArtistIntent` vor `R15Fixed`, damit nicht versehentlich
    Segmente aus unterschiedlichen Bundle-Unterordnern gemischt werden.
    """
    folder_rank = {"r15artistintent": 0, "r15fixed": 1, "": 2}
    ordered = sorted(
        group,
        key=lambda mp: folder_rank.get((mp.get("folder") or "").lower(), 3),
    )
    picked: list = []
    for expected in expected_names:
        wanted = _normalize_name(expected)
        for mesh_part in ordered:
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
# Hintergrundbild aus der Configs.toml
# ---------------------------------------------------------------------------

MAX_BACKGROUND_BYTES = 30 * 1024 * 1024
BACKGROUND_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"BM", ".bmp"),
    (b"RIFF", ".webp"),
)


def _background_extension(url: str, data: bytes) -> str:
    """Dateiendung aus Signatur bzw. URL bestimmen (Blender braucht sie)."""
    for signature, suffix in BACKGROUND_SIGNATURES:
        if data.startswith(signature):
            if suffix == ".webp" and data[8:12] != b"WEBP":
                continue
            return suffix
    lower = (url or "").lower().split("?")[0]
    for suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        if lower.endswith(suffix):
            return ".jpg" if suffix == ".jpeg" else suffix
    return ".png"


def fetch_background(config: Dict[str, Any], out_dir: str,
                     warnings: List[str], dry_run: bool = False) -> Dict[str, Any]:
    """Laedt das Hintergrundbild und legt es neben das Manifest.

    ``hintergrund_url`` ist entweder eine URL/ein Dateipfad, ``"transparent"``
    (Film wird durchsichtig) oder ``"hdri"`` (World-HDRI wie frueher).
    Schlaegt der Download fehl, wird weiter das HDRI benutzt - der Render
    bricht dadurch nicht ab.
    """
    url = str(config.get("hintergrund_url") or "").strip()
    glossy = bool(config.get("hintergrund_glossy"))
    info: Dict[str, Any] = {"url": url, "datei": "", "transparent": False,
                            "glossy": glossy}
    lowered = url.strip().lower()
    if lowered == "transparent":
        # Ausnahme: durchsichtiger Hintergrund (PNG mit Alpha).
        info["transparent"] = True
        return info
    if not lowered or lowered in ("hdri", "hdr", "environment", "env"):
        # Wie frueher: das World-HDRI bleibt als Hintergrund sichtbar.
        return info

    data: Optional[bytes] = None
    try:
        if url.lower().startswith(("http://", "https://")):
            if dry_run:
                warnings.append(
                    "Dry-Run: Hintergrundbild wird nicht heruntergeladen, "
                    "es wird das HDRI benutzt.")
                return info
            import urllib.request

            request = urllib.request.Request(
                url,
                headers={"User-Agent": "RobloxAvatarRenderPipeline/1.0"},
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(MAX_BACKGROUND_BYTES + 1)
        else:
            with open(url, "rb") as fh:
                data = fh.read(MAX_BACKGROUND_BYTES + 1)
    except Exception as exc:  # Netzwerk, 404, Rechte, kaputte Datei ...
        warnings.append(
            f"Hintergrundbild '{url}' konnte nicht geladen werden ({exc}) - "
            "es wird das HDRI als Hintergrund benutzt.")
        return info

    if data is None or not data:
        warnings.append(
            f"Hintergrundbild '{url}' ist leer - es wird das HDRI benutzt.")
        return info
    if len(data) > MAX_BACKGROUND_BYTES:
        warnings.append(
            f"Hintergrundbild '{url}' ist groesser als "
            f"{MAX_BACKGROUND_BYTES // (1024 * 1024)} MB - es wird das HDRI benutzt.")
        return info

    suffix = _background_extension(url, data)
    fname = f"hintergrund{suffix}"
    with open(os.path.join(out_dir, fname), "wb") as fh:
        fh.write(data)
    info["datei"] = fname
    log(f"      -> Hintergrundbild geladen: {url} ({fmt_de(len(data) / 1024, 0)} KB)")
    return info


# ---------------------------------------------------------------------------
# Pose
# ---------------------------------------------------------------------------


def setup_pose(config: Dict[str, Any],
               body_parts: List[avatar_rig.Part],
               metrics: avatar_rig.BodyMetrics,
               rig_avatar: Optional[avatar_rig.RiggedAvatar],
               use_r15_rig: bool,
               warnings: List[str]) -> Tuple[Optional[avatar_pose.PoseContext], Dict[str, Any]]:
    """Baut den Pose-Kontext (None = Stand-Pose) aus der Configs.toml.

    Die Pose wird bewusst **nach** dem fertigen Rig angewendet: Avatar laden,
    RBXM parsen und Gelenke setzen bleiben unveraendert, erst danach werden
    die Vertices um die Gelenkpunkte gedreht.
    """
    pose_name = str((config or {}).get("pose") or "None").strip()
    info: Dict[str, Any] = {"name": pose_name or "None", "aktiv": False,
                            "details": ""}
    if not pose_name or pose_name.lower() == "none":
        return None, info

    poses_path = (config or {}).get("_posen_pfad") or os.path.join(
        os.path.dirname(os.path.abspath((config or {}).get("_pfad", "Configs.toml"))),
        render_config.POSES_FILENAME,
    )
    try:
        poses = avatar_pose.load_poses(poses_path)
    except avatar_pose.PoseError as exc:
        warnings.append(
            f"Pose '{pose_name}' konnte nicht geladen werden ({exc}) - "
            "es wird die Stand-Pose gerendert.")
        return None, info

    entry = poses.get(pose_name)
    if entry is None:
        warnings.append(
            f"Pose '{pose_name}' steht nicht in {poses_path} - es wird die "
            "Stand-Pose gerendert. Verfuegbar: " + ", ".join(sorted(poses)))
        return None, info

    part_meshes = {part.name: part.mesh for part in body_parts}
    joints = avatar_pose.joints_from_parts(part_meshes, metrics)
    if use_r15_rig and rig_avatar is not None:
        # Exakte R15-Gelenke aus den *RigAttachment-Daten haben Vorrang.
        joints.update(rig_avatar.joint_positions)

    context = avatar_pose.PoseContext(entry["teile"], joints)
    info["aktiv"] = context.active
    info["details"] = avatar_pose.pose_summary(context.pose)
    log(f"      Pose '{pose_name}': {info['details']}")
    return context, info


def apply_body_pose(context: Optional[avatar_pose.PoseContext], name: str,
                    mesh: roblox_mesh.MeshData) -> roblox_mesh.MeshData:
    if context is None or not context.active:
        return mesh
    return context.apply_body(name, mesh)


def apply_accessory_pose(context: Optional[avatar_pose.PoseContext],
                         mesh: roblox_mesh.MeshData,
                         asset_type_id: int) -> roblox_mesh.MeshData:
    if context is None or not context.active:
        return mesh
    anchor = ""
    layout = avatar_rig.ACCESSORY_LAYOUT.get(int(asset_type_id))
    if layout:
        anchor = str(layout.get("anchor") or "")
    return context.apply_accessory(mesh, anchor)


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------


def build(username_raw: str, out_dir: str, dry_run: bool = False,
          max_assets: int = 80, verbose: bool = True,
          config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
    texture_bytes_by_rel: Dict[str, bytes] = {}
    headless = {"active": False, "label": "", "reason": "", "info": ""}

    # Render-Einstellungen (Configs.toml). Ohne Config gelten die Defaults -
    # so bleiben direkte Aufrufe/Tests ohne Config-Datei lauffaehig.
    render_cfg: Dict[str, Any] = dict(render_config.DEFAULTS)
    if config:
        for key in render_config.KNOWN_KEYS:
            if key in config:
                render_cfg[key] = config[key]
        warnings.extend(config.get("_warnungen") or [])

    def config_flag(name: str, default: bool = False) -> bool:
        value = render_cfg.get(name, default)
        return bool(value) if isinstance(value, bool) else default

    def config_number(name: str, default: float) -> float:
        try:
            return float(render_cfg.get(name, default))
        except (TypeError, ValueError):
            return default

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
            texture_bytes_by_rel[rel] = data
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
                     rbxm_rotation: Optional[Any] = None,
                     rbxm_meta: Optional[Dict[str, Any]] = None) -> None:
            """Parst ein FileMesh und reiht es in mesh_parts ein."""
            try:
                mesh = roblox_mesh.parse(raw)
            except roblox_mesh.MeshParseError as exc:
                skipped.append({"asset_id": mesh_asset_id, "name": label,
                                "asset_type": type_name,
                                "reason": f"Mesh nicht lesbar: {exc}"})
                return
            meta = rbxm_meta or {}
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
                "rbxm_class": meta.get("class"),
                "folder": meta.get("folder"),
                "transparency": meta.get("transparency", 0.0),
                "mesh_scale": meta.get("mesh_scale"),
                "mesh_offset": meta.get("mesh_offset"),
                "attachments": meta.get("attachments") or {},
                "wrap_layer": meta.get("wrap_layer"),
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
                         entry.get("position"), entry.get("rotation"),
                         rbxm_meta=entry)

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
                rel = f"textures/{fname}"
                texture_bytes_by_rel[rel] = dl.data
                textures.append({"asset_id": aid, "name": label, "asset_type": type_name,
                                 "file": rel, "type_id": type_id})
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
    color_hex_by_part = {p.name: p.color for p in body_parts}

    def save_baked_texture(stem: str, png_bytes: Optional[bytes]) -> Optional[str]:
        if not png_bytes:
            return None
        fname = f"_comp_{stem}.png"
        with open(os.path.join(textures_dir, fname), "wb") as fh:
            fh.write(png_bytes)
        return f"textures/{fname}"

    # Roh-Texturen klassischer Kleidung / Gesichter sammeln + Fallback-Map pflegen
    texture_for_part: Dict[str, str] = {}
    clothing_kind_by_file: Dict[str, str] = {}
    shirt_raw: Optional[bytes] = None
    pants_raw: Optional[bytes] = None
    tshirt_raw: Optional[bytes] = None
    face_raw: Optional[bytes] = None

    for tex in textures:
        type_id = int(tex["type_id"])
        rel = tex["file"]
        raw = texture_bytes_by_rel.get(rel)
        kind = CLOTHING_TEXTURE_KINDS.get(type_id)
        if kind:
            clothing_kind_by_file[rel] = kind
        if type_id == 11 and shirt_raw is None:
            shirt_raw = raw
        elif type_id == 12 and pants_raw is None:
            pants_raw = raw
        elif type_id == 2 and tshirt_raw is None:
            tshirt_raw = raw
        elif type_id == 18 and face_raw is None:
            face_raw = raw
        for target in CLOTHING_TARGETS.get(type_id, []):
            texture_for_part.setdefault(target, rel)

    # Offizieller R15-Humanoid-Kompositor fuer echte 585x559-Kleidungsvorlagen:
    # Torso (388x272) aus Pants -> Shirt -> TShirt, Arme (264x284) aus Shirt,
    # Beine (264x284) aus Pants. Synthetische 1x1-Testtexturen bleiben unberuehrt.
    torso_comp = avatar_uv.composite_torso_texture(
        avatar_rig.rgb01(color_hex_by_part.get("torso", "#c7ac78")),
        pants_bytes=pants_raw,
        shirt_bytes=shirt_raw,
        tshirt_bytes=tshirt_raw,
    )
    if torso_comp is not None:
        rel_comp = save_baked_texture("torso", torso_comp)
        if rel_comp:
            texture_for_part["torso"] = rel_comp
            clothing_kind_by_file[rel_comp] = "composited"

    for arm_name, side in (("rechts_arm", "right"), ("links_arm", "left")):
        arm_comp = avatar_uv.composite_limb_texture(
            avatar_rig.rgb01(color_hex_by_part.get(arm_name, "#c7ac78")),
            shirt_raw,
            side=side,
        )
        if arm_comp is not None:
            rel_comp = save_baked_texture(arm_name, arm_comp)
            if rel_comp:
                texture_for_part[arm_name] = rel_comp
                clothing_kind_by_file[rel_comp] = "composited"

    for leg_name, side in (("rechts_bein", "right"), ("links_bein", "left")):
        leg_comp = avatar_uv.composite_limb_texture(
            avatar_rig.rgb01(color_hex_by_part.get(leg_name, "#c7ac78")),
            pants_raw,
            side=side,
        )
        if leg_comp is not None:
            rel_comp = save_baked_texture(leg_name, leg_comp)
            if rel_comp:
                texture_for_part[leg_name] = rel_comp
                clothing_kind_by_file[rel_comp] = "composited"

    # Echte Koerperteil-Meshes (Typ 27-31) den Rig-Teilen zuordnen
    body_groups: Dict[str, list] = {}
    for mesh_part in mesh_parts:
        rig_name = BODY_PART_TYPES.get(int(mesh_part["type_id"]))
        if rig_name is not None:
            body_groups.setdefault(rig_name, []).append(mesh_part)

    r15_segments_by_part: Dict[str, List[Dict[str, Any]]] = {}
    body_replacements: Dict[str, Dict[str, Any]] = {}
    for rig_name, group in body_groups.items():
        if len(group) == 1:
            mp0 = group[0]
            r15_segments_by_part[rig_name] = [mp0]
            body_replacements[rig_name] = mp0
            continue
        segments = _pick_r15_segments(group, R15_SEGMENT_NAMES.get(rig_name, []))
        mergeable = segments if len(segments) >= 2 else _sort_segments_top_down(group)
        mergeable = [mp for mp in mergeable
                     if mp.get("mesh") is not None and not mp["mesh"].is_empty]
        if 2 <= len(mergeable) <= 6:
            # Falls die Segmente unterschiedliche eigene Texturen tragen,
            # in einen gemeinsamen Atlas packen, damit kein Segment seine Textur verliert.
            remapped_meshes, multi_atlas = avatar_uv.build_multi_segment_atlas(
                [(mp["mesh"], texture_bytes_by_rel.get(mp.get("texture_file") or ""))
                 for mp in mergeable]
            )
            seg_entries: List[Dict[str, Any]] = []
            for mp_orig, m_remap in zip(mergeable, remapped_meshes):
                mp_copy = dict(mp_orig)
                mp_copy["mesh"] = m_remap
                seg_entries.append(mp_copy)
            r15_segments_by_part[rig_name] = seg_entries

            stacked = roblox_mesh.stack_segments(
                [(mp["mesh"], mp.get("rbxm_size") or mp["mesh"].size())
                 for mp in seg_entries
                 if float(mp.get("transparency") or 0.0) < 0.99]
                or [(mp["mesh"], mp.get("rbxm_size") or mp["mesh"].size()) for mp in seg_entries],
                source_version="stacked-rbxm",
            )
            custom_tex = next(
                (mp["texture_file"] for mp in seg_entries if mp.get("texture_file")),
                None,
            )
            if multi_atlas is not None and len({mp.get("texture_file") for mp in seg_entries if mp.get("texture_file")}) > 1:
                atlas_rel = save_baked_texture(f"atlas_{rig_name}", avatar_uv.prepare_opaque_mesh_texture(multi_atlas))
                if atlas_rel:
                    custom_tex = atlas_rel
                    clothing_kind_by_file[atlas_rel] = "mesh_surface"
            elif custom_tex and custom_tex in texture_bytes_by_rel:
                prepped = avatar_uv.prepare_opaque_mesh_texture(texture_bytes_by_rel[custom_tex])
                if prepped and prepped != texture_bytes_by_rel[custom_tex]:
                    prepped_rel = save_baked_texture(f"mesh_{rig_name}", prepped)
                    if prepped_rel:
                        custom_tex = prepped_rel
                        clothing_kind_by_file[prepped_rel] = "mesh_surface"
                else:
                    clothing_kind_by_file.setdefault(custom_tex, "mesh_surface")

            pseudo = dict(seg_entries[0])
            pseudo["mesh"] = stacked
            pseudo["texture_file"] = custom_tex
            names = ", ".join(mp.get("label") or "?" for mp in seg_entries)
            pseudo["note_suffix"] = (
                f", {len(seg_entries)} R15-Segmente senkrecht gestapelt "
                f"(Stand-Pose statt Bind-Pose: {names})"
            )
            pseudo["stacked_size"] = stacked_part_size(seg_entries)
            body_replacements[rig_name] = pseudo
        else:
            largest = max(group, key=lambda mp: max(mp["mesh"].size()))
            r15_segments_by_part[rig_name] = [largest]
            body_replacements[rig_name] = largest
            warnings.append(
                f"Koerperteil '{rig_name}': {len(group)} Meshes gefunden, "
                f"aber kein Stapel aus 2-6 Segmenten moeglich - "
                f"nur das groesste Segment verwendet "
                f"(Asset {largest['asset_id']}).")

    # Einzel-Mesh-Koerperteile mit eigener Bundle-Textur ebenfalls aufbereiten
    for rig_name, rep in body_replacements.items():
        ctex = rep.get("texture_file")
        if ctex and ctex in texture_bytes_by_rel:
            prepped = avatar_uv.prepare_opaque_mesh_texture(texture_bytes_by_rel[ctex])
            if prepped and prepped != texture_bytes_by_rel[ctex]:
                prepped_rel = save_baked_texture(f"mesh_{rig_name}", prepped)
                if prepped_rel:
                    rep["texture_file"] = prepped_rel
                    clothing_kind_by_file[prepped_rel] = "mesh_surface"
            else:
                clothing_kind_by_file.setdefault(ctex, "mesh_surface")

    # Kopf: echtes Head-/DynamicHead-Mesh suchen (DynamicHead hat Vorrang)
    head_mesh: Optional[Dict[str, Any]] = None
    for wanted_type in (DYNAMIC_HEAD_TYPE, HEAD_ASSET_TYPE):
        for mesh_part in mesh_parts:
            if int(mesh_part["type_id"]) == wanted_type:
                head_mesh = mesh_part
                break
        if head_mesh is not None:
            break

    # Gesichts-Textur auf Kopf-Hautfarbe vorbacken (Augen/Mund scharf, Rest Haut-Glas)
    head_tex_src = (
        (head_mesh.get("texture_file") if head_mesh else None)
        or texture_for_part.get("kopf_mesh")
        or texture_for_part.get("kopf")
    )
    if head_tex_src and head_tex_src in texture_bytes_by_rel:
        head_comp = avatar_uv.composite_face_texture(
            avatar_rig.rgb01(color_hex_by_part.get("kopf", "#c7ac78")),
            texture_bytes_by_rel[head_tex_src],
        )
        if head_comp is not None:
            rel_head = save_baked_texture("head", head_comp)
            if rel_head:
                if head_mesh and head_mesh.get("texture_file") == head_tex_src:
                    head_mesh["texture_file"] = rel_head
                texture_for_part["kopf_mesh"] = rel_head
                texture_for_part["kopf"] = rel_head
                clothing_kind_by_file[rel_head] = "composited"

    # Exakten R15-Gelenk-Rig berechnen (liefert Welt-Attachments + Skinning-Gelenke
    # sowie bei echten RBXM-RigAttachments die 1:1-Gelenkgeometrie aller Koerperteile)
    use_r15_rig = avatar_rig.has_rig_attachments(r15_segments_by_part, head_mesh)
    rig_avatar = avatar_rig.assemble_r15_rig(
        r15_segments_by_part,
        head_mesh,
        body_colors,
        scales,
        headless=headless["active"],
    )

    if use_r15_rig:
        metrics = rig_avatar.metrics
        body_parts = [
            avatar_rig.Part(
                name=pname,
                mesh=rig_avatar.body_meshes[pname],
                color=color_hex_by_part.get(pname, color_hex_by_part.get("torso", "#c7ac78")),
                kind="body",
            )
            for pname in ("rechts_bein", "links_bein", "torso", "rechts_arm", "links_arm", "hals")
            if pname in rig_avatar.body_meshes
        ]
        if not headless["active"] and rig_avatar.head_mesh is not None:
            body_parts.append(
                avatar_rig.Part(
                    name="kopf",
                    mesh=rig_avatar.head_mesh,
                    color=color_hex_by_part.get("kopf", "#c7ac78"),
                    kind="body",
                )
            )
        if head_mesh is not None and not headless["active"]:
            warnings.append(
                "Rig aus den echten Mesh-Groessen neu aufgebaut "
                f"(Kopf {fmt_de(metrics.head_radius * 2)} Studs, "
                f"Torso {fmt_de(metrics.torso_width)} breit) - "
                "keine achsweise Streckung der Koerperteile mehr."
            )
    else:
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

    # ---- Pose -------------------------------------------------------------
    # Erst nach dem fertigen Rig: die exakten R15-Gelenke (bzw. die aus den
    # Mesh-Massen abgeleiteten Gelenke) sind die Drehpunkte. Avatar-Laden,
    # RBXM-Parsing und Rig-Aufbau bleiben davon komplett unberuehrt.
    pose_ctx, pose_info = setup_pose(config or {}, body_parts, metrics,
                                     rig_avatar, use_r15_rig, warnings)

    # Oberkante des Kopfes in Weltkoordinaten (nach der Pose). Wichtig fuer
    # Headless-Avatare: dort gibt es kein Kopf-Mesh, aber der Bildausschnitt
    # soll sich trotzdem am Kopf (nicht an Hut/Krone) orientieren.
    head_joint = (rig_avatar.joint_positions.get("Head") if rig_avatar else None)
    head_center = head_joint or (0.0, metrics.head_center_y, 0.0)
    if headless["active"] and metrics.hat_base_y is not None:
        head_center = (head_center[0], metrics.hat_base_y, head_center[2])
        head_radius = 0.0
    else:
        head_center = (head_center[0], metrics.head_center_y, head_center[2])
        head_radius = metrics.head_radius
    if pose_ctx is not None and pose_ctx.active:
        head_center = avatar_pose._mat4_point(
            pose_ctx.transforms["kopf"], head_center)
    head_top_world = round(head_center[1] + head_radius, 3)

    body_parts_from_mesh = 0
    for part in body_parts:
        if part.name == "kopf":
            if headless["active"]:
                continue
            if head_mesh is not None:
                if use_r15_rig and rig_avatar.head_mesh is not None:
                    placed = sanity_check(rig_avatar.head_mesh, metrics.head_radius * 2.0, "Kopf", warnings)
                else:
                    size = head_mesh["mesh"].size()
                    max_dim = max(size) or 1.0
                    target = metrics.head_radius * 2.0
                    placed = head_mesh["mesh"].transform(target / max_dim)
                    c = placed.center()
                    placed = placed.transform(1.0, (-c[0], metrics.head_center_y - c[1], -c[2]))
                    placed = sanity_check(placed, target, "Kopf", warnings)
                placed = apply_body_pose(pose_ctx, "kopf", placed)
                fname = "kopf_mesh.obj"
                placed.write_obj(os.path.join(parts_dir, fname))
                head_texture = (head_mesh.get("texture_file")
                                or texture_for_part.get("kopf_mesh"))
                parts.append({
                    "name": "kopf_mesh",
                    "obj": f"parts/{fname}",
                    "color": part.color,
                    "kind": "body",
                    "mesh_source": "mesh-asset",
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
            if use_r15_rig and part.name in rig_avatar.body_meshes:
                placed = sanity_check(
                    rig_avatar.body_meshes[part.name],
                    max(part.mesh.size()),
                    part.name,
                    warnings,
                )
            else:
                lo, hi = part.mesh.bbox()
                placed = avatar_rig.fit_mesh_to_part(replacement["mesh"], lo, hi,
                                                     uniform=False)
                placed = sanity_check(placed, max(hi[i] - lo[i] for i in range(3)),
                                      part.name, warnings)
            placed = apply_body_pose(pose_ctx, part.name, placed)
            fname = f"{part.name}.obj"
            placed.write_obj(os.path.join(parts_dir, fname))
            body_texture = (replacement.get("texture_file")
                            or texture_for_part.get(part.name))
            parts.append({
                "name": part.name,
                "obj": f"parts/{fname}",
                "color": part.color,
                "kind": "body",
                "mesh_source": "mesh-asset",
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

        placed = apply_body_pose(pose_ctx, part.name, part.mesh)
        fname = f"{part.name}.obj"
        placed.write_obj(os.path.join(parts_dir, fname))
        parts.append({
            "name": part.name,
            "obj": f"parts/{fname}",
            "color": part.color,
            "kind": "body",
            "mesh_source": "generated-sphere" if part.name == "kopf" else "generated-box",
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

    # Accessoires & Layered Clothing platzieren
    consumed = set()
    for seg_list in r15_segments_by_part.values():
        for mp_c in seg_list:
            consumed.add(id(mp_c))
    for mp_c in body_replacements.values():
        consumed.add(id(mp_c))
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

        placed_acc: Optional[roblox_mesh.MeshData] = None
        note = ""
        if mesh_part["mesh"].skinning is not None:
            skinned = roblox_mesh.skin_mesh_to_rig(
                mesh_part["mesh"],
                rig_avatar.joint_positions,
            )
            if skinned is not None and not skinned.is_empty:
                placed_acc = skinned
                sz = placed_acc.size()
                note = (
                    f"{mesh_part['type_name']}: per Linear-Blend-Skinning "
                    f"an R15-Gelenke angepasst "
                    f"({fmt_de(sz[0])}x{fmt_de(sz[1])}x{fmt_de(sz[2])} Studs)"
                )

        if placed_acc is None:
            welded = avatar_rig.weld_accessory(mesh_part, rig_avatar)
            if welded is not None:
                placed_acc, note = welded

        if placed_acc is None:
            placed_acc, note = avatar_rig.place_accessory(
                mesh_part["mesh"], int(mesh_part["type_id"]), metrics,
                size_hint=mesh_part.get("rbxm_size"),
                rotation=mesh_part.get("rbxm_rotation"))
            if "unplausibel" in note:
                warnings.append(
                    f"Accessoire '{mesh_part['label'] or mesh_part['type_name']}': "
                    "Groesse aus dem RBXM war unplausibel und wurde auf die "
                    "Zielgroesse der Accessoire-Art korrigiert.")
        placed = sanity_check(
            placed_acc,
            avatar_rig.accessory_target_size(int(mesh_part["type_id"]), metrics),
            mesh_part["label"] or mesh_part["type_name"],
            warnings,
        )
        # Accessoires/Kleidung werden mitgedreht und mitverschoben: starre
        # Zuordnung zum passenden Knochen (Hut -> Kopf, Gear -> rechte Hand,
        # Rucksack -> oberer Torso ...).
        placed = apply_accessory_pose(pose_ctx, placed, int(mesh_part["type_id"]))

        acc_tex = mesh_part.get("texture_file")
        acc_tex_kind = None
        if acc_tex and acc_tex in texture_bytes_by_rel:
            prepped = avatar_uv.prepare_opaque_mesh_texture(texture_bytes_by_rel[acc_tex])
            if prepped and prepped != texture_bytes_by_rel[acc_tex]:
                prepped_rel = save_baked_texture(
                    f"acc_{mesh_part.get('mesh_asset_id') or mesh_part['asset_id']}",
                    prepped,
                )
                if prepped_rel:
                    acc_tex = prepped_rel
                    acc_tex_kind = "mesh_surface"
            else:
                acc_tex_kind = "mesh_surface"

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
            "mesh_source": "mesh-asset",
            "smooth": True,
            "asset_id": mesh_part["asset_id"],
            "asset_type": mesh_part["type_name"],
            "note": note,
            "texture": acc_tex,
            "texture_kind": acc_tex_kind,
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

    # Hintergrundbild (Configs.toml) neben das Manifest legen, damit der
    # Blender-Schritt es ohne Netzwerk laden kann.
    background_info = fetch_background(render_cfg, out_dir, warnings, dry_run=dry_run)

    manifest = {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "username": username,
        "user_id": user_id,
        "avatar_type": avatar_raw.get("playerAvatarType") or "R15",
        "avatar_source": avatar_raw.get("_source", ""),
        "dry_run": dry_run,
        "scales": scales,
        # Alles, was der Blender-Schritt fuer Bildausschnitt/Hintergrund braucht.
        "render": {
            "drehung_grad": config_number("drehung_grad", 0.0),
            "pose": pose_info["name"],
            "pose_aktiv": bool(pose_info["aktiv"]),
            "pose_details": pose_info["details"],
            # Oberkante des Kopfes (ohne Accessoires) - Bezug fuer den
            # freien Abstand oben, funktioniert auch bei Headless-Avataren.
            "kopf_oberkante": head_top_world,
            "freiraum_oben_prozent": config_number("freiraum_oben_prozent", 6.0),
            "freiraum_unten_prozent": config_number("freiraum_unten_prozent", 6.0),
            "render_bei_aenderung": config_flag("render_bei_aenderung", False),
            "hintergrund": background_info,
        },
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
        f"* Pose: **{manifest.get('render', {}).get('pose', 'None')}**"
        + (f" ({manifest['render'].get('pose_details', '')})"
           if manifest.get("render", {}).get("pose_aktiv") else ""),
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


def load_config_or_defaults(path: str,
                            username_override: Optional[str] = None,
                            dry_run: bool = False) -> Tuple[Optional[Dict[str, Any]], str, int]:
    """Laedt die Configs.toml. Rueckgabe: (config, username, Exitcode)."""
    config: Dict[str, Any] = dict(render_config.DEFAULTS)
    config["_pfad"] = os.path.abspath(path)
    config["_posen_pfad"] = os.path.join(
        os.path.dirname(config["_pfad"]), render_config.POSES_FILENAME)
    config["_warnungen"] = []
    config["_transparent"] = True

    if os.path.isfile(path):
        try:
            config = render_config.load_config(path, require_username=False)
        except render_config.ConfigError as exc:
            print(f"FEHLER: {exc}", file=sys.stderr)
            return None, "", 2
    elif not username_override and not dry_run:
        print(f"FEHLER: {path} fehlt. Bitte die Config-Datei im "
              "Repository-Root anlegen.", file=sys.stderr)
        return None, "", 2

    username = (username_override or config.get("username") or "").strip()
    if not username and not dry_run:
        print(f"FEHLER: kein Username uebergeben und '{path}' enthaelt keinen "
              "Benutzernamen.", file=sys.stderr)
        return None, "", 2
    return config, username, 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Roblox-Avatar -> Blender-Szene")
    parser.add_argument("--config", default=render_config.CONFIG_FILENAME,
                        help=f"Pfad zur Config-Datei (Default: {render_config.CONFIG_FILENAME})")
    parser.add_argument("--username", default=None,
                        help="Roblox-Username (ueberschreibt den Wert aus der Config)")
    parser.add_argument("--out", default="build", help="Ausgabeordner (Default: build)")
    parser.add_argument("--dry-run", action="store_true",
                        help="ohne Netzwerk: nur Koerper mit Standardfarben bauen")
    parser.add_argument("--max-assets", type=int, default=80)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    config, username, code = load_config_or_defaults(
        args.config, args.username, args.dry_run)
    if config is None:
        return code

    try:
        build(username, args.out, dry_run=args.dry_run, max_assets=args.max_assets,
              verbose=not args.quiet, config=config)
    except api.RobloxError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
