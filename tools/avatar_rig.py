"""Baut aus Avatar-Daten einen R15-artigen Rig in Standard-Stand-Pose.

Roblox liefert ueber die Web-APIs **keine** fertige, geriggte OBJ-Datei und
auch keine Attachment-Offsets fuer Accessoires. Deshalb:

* Der Koerper wird aus Quadern/Kugel erzeugt (klassische Roblox-Proportionen,
  Groesse folgt den `scales` des Avatars, Farben den `bodyColors`).
* Accessoires (Hut, Haare, Gesichts-Accessoire, ...) kommen als echte
  Roblox-Meshes aus der Asset Delivery API. Sie werden zentriert, auf eine
  sinnvolle Groesse normiert und an die passende Koerperstelle gesetzt
  (Kopf oben, Gesicht vorne, Brust, Ruecken, Hals, Taille, Schultern, Hand).

Das Ergebnis ist eine *Naeherung* an den echten Avatar - gut genug fuer ein
kuenstlerisches Glas-Rendering, aber keine 1:1-Kopie der Roblox-Engine.
Details siehe ANLEITUNG.md, Abschnitt "Einschraenkungen".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from roblox_mesh import MeshData, make_box, make_sphere
from roblox_api import brick_color_hex, hex_to_rgb

# AssetTypeId -> Layout fuer Accessoires
ACCESSORY_LAYOUT: Dict[int, Dict[str, Any]] = {
    8: {"label": "Hut", "target": 1.75, "anchor": "head_top"},
    41: {"label": "Haare", "target": 1.85, "anchor": "head_top"},
    42: {"label": "Gesichts-Accessoire", "target": 1.35, "anchor": "face_front"},
    43: {"label": "Hals-Accessoire", "target": 2.2, "anchor": "neck"},
    44: {"label": "Schulter-Accessoire", "target": 2.9, "anchor": "shoulders"},
    45: {"label": "Brust-Accessoire", "target": 1.6, "anchor": "chest_front"},
    46: {"label": "Ruecken-Accessoire", "target": 2.2, "anchor": "back"},
    47: {"label": "Hueft-Accessoire", "target": 2.0, "anchor": "waist"},
    19: {"label": "Gear", "target": 1.3, "anchor": "right_hand"},
    76: {"label": "Augenbrauen", "target": 1.2, "anchor": "face_front"},
    77: {"label": "Wimpern", "target": 1.2, "anchor": "face_front"},
    # DynamicHead gehoert an den Kopf, nicht an die Brust (Fallback-Anker).
    # Die eigentliche Behandlung (Kopf-Ersatz bzw. Headless) macht
    # build_avatar.py; das Layout greift nur fuer Zusatzteile.
    79: {"label": "Dynamic Head", "target": 1.45, "anchor": "head_center"},
}


def _de(value: float, nd: int = 2) -> str:
    """Zahl mit Komma statt Punkt (deutsche Schreibweise fuer Notizen)."""
    return f"{value:.{nd}f}".replace(".", ",")


def _clamp(value: Any, low: float, high: float, default: float = 1.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(low, min(high, v))


@dataclass
class Part:
    """Ein Bauteil des Rigs."""

    name: str
    mesh: MeshData
    color: str
    kind: str  # "body" | "accessory"
    asset_id: Optional[int] = None
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "color": self.color, "kind": self.kind,
                "asset_id": self.asset_id, "note": self.note}


@dataclass
class BodyMetrics:
    """Masse des erzeugten Koerpers (in Studs), fuer Accessoire-Anker."""

    height: float = 5.3
    torso_width: float = 2.0
    torso_depth: float = 1.0
    shoulder_y: float = 4.0
    head_center_y: float = 4.62
    head_radius: float = 0.68
    hand_y: float = 2.15
    hand_x: float = 1.45
    waist_y: float = 2.5

    def anchor(self, name: str) -> Tuple[float, float, float]:
        if name == "head_top":
            return (0.0, self.head_center_y + self.head_radius * 0.95, 0.0)
        if name == "head_center":
            return (0.0, self.head_center_y, 0.0)
        if name == "face_front":
            return (0.0, self.head_center_y, -(self.head_radius * 0.80))
        if name == "neck":
            return (0.0, self.shoulder_y + 0.1, 0.0)
        if name == "shoulders":
            return (0.0, self.shoulder_y - 0.1, 0.0)
        if name == "chest_front":
            return (0.0, self.shoulder_y - 0.55, -(self.torso_depth * 0.62))
        if name == "back":
            return (0.0, self.shoulder_y - 0.55, self.torso_depth * 0.62)
        if name == "waist":
            return (0.0, self.waist_y, 0.0)
        if name == "right_hand":
            return (self.hand_x, self.hand_y, 0.0)
        return (0.0, self.height * 0.5, 0.0)


def build_body(body_colors: Dict[str, Any], scales: Dict[str, Any]) -> Tuple[List[Part], BodyMetrics]:
    """Erzeugt den Koerper in Standard-Stand-Pose (klassischer Roblox-Look).

    Bewusst wenige, geschlossene Volumen (Kopf, Torso, 2 Arme, 2 Beine):
    gestapelte Einzel-Quader erzeugen im Cycles-Glasrender dunkle Nahtstellen,
    weil dort Flaechen exakt aufeinander liegen.
    """
    h = _clamp(scales.get("height"), 0.75, 1.30)
    w = _clamp(scales.get("width"), 0.75, 1.30)
    d = _clamp(scales.get("depth"), 0.75, 1.30)
    hs = _clamp(scales.get("head"), 0.75, 1.40)

    colors = {
        "head": brick_color_hex(body_colors.get("headColorId", 226)),
        "torso": brick_color_hex(body_colors.get("torsoColorId", 23)),
        "left_arm": brick_color_hex(body_colors.get("leftArmColorId", 226)),
        "right_arm": brick_color_hex(body_colors.get("rightArmColorId", 226)),
        "left_leg": brick_color_hex(body_colors.get("leftLegColorId", 9)),
        "right_leg": brick_color_hex(body_colors.get("rightLegColorId", 9)),
    }

    torso_w, torso_h, torso_d = 2.0 * w, 2.0 * h, 1.0 * d
    arm_w, arm_h, arm_d = 1.0 * w, 2.0 * h, 1.0 * d
    leg_w, leg_h, leg_d = 1.0 * w, 2.0 * h, 1.0 * d

    parts: List[Part] = []
    gap = 0.012  # minimale Luft, damit keine Flaechen exakt aufeinander liegen

    # Beine (stehen auf y=0)
    leg_x = leg_w / 2 + gap * 0.5
    parts.append(Part("rechts_bein", make_box(leg_w, leg_h, leg_d,
                                              (leg_x, leg_h / 2, 0.0)),
                      colors["right_leg"], "body"))
    parts.append(Part("links_bein", make_box(leg_w, leg_h, leg_d,
                                             (-leg_x, leg_h / 2, 0.0)),
                      colors["left_leg"], "body"))

    # Torso (mit minimalem Abstand zu den Beinen - sonst dunkle Naht im Glas)
    torso_y = leg_h + gap * 2 + torso_h / 2
    parts.append(Part("torso", make_box(torso_w, torso_h, torso_d, (0.0, torso_y, 0.0)),
                      colors["torso"], "body"))

    # Arme (haengen seitlich herab)
    arm_x = torso_w / 2 + arm_w / 2 + gap
    arm_y = torso_y
    parts.append(Part("rechts_arm", make_box(arm_w, arm_h, arm_d, (arm_x, arm_y, 0.0)),
                      colors["right_arm"], "body"))
    parts.append(Part("links_arm", make_box(arm_w, arm_h, arm_d, (-arm_x, arm_y, 0.0)),
                      colors["left_arm"], "body"))

    # Hals (klein, versinkt im Torso/Kopf)
    shoulder_y = leg_h + gap * 2 + torso_h
    neck_h = 0.40 * h
    neck_y = shoulder_y + neck_h / 2 - 0.04
    parts.append(Part("hals", make_box(0.7 * w, neck_h, 0.7 * d, (0.0, neck_y, 0.0)),
                      colors["head"], "body"))

    # Kopf (Kugel, leicht nach vorne gesetzt wie bei Roblox)
    head_r = 0.68 * hs * h
    head_center_y = shoulder_y + head_r * 1.02   # Kopf sitzt auf dem Hals, nicht im Torso
    parts.append(Part("kopf", make_sphere(head_r, 32, 22, (0.0, head_center_y, -0.02 * d)),
                      colors["head"], "body"))

    metrics = BodyMetrics(
        height=head_center_y + head_r,
        torso_width=torso_w,
        torso_depth=torso_d,
        shoulder_y=shoulder_y,
        head_center_y=head_center_y,
        head_radius=head_r,
        hand_y=arm_y - arm_h / 2 + 0.15,
        hand_x=arm_x,
        waist_y=leg_h + torso_h * 0.25,
    )
    return parts, metrics


def accessory_target_size(asset_type_id: int, metrics: BodyMetrics) -> float:
    """Heuristische Zielgroesse (max. Achse in Studs) fuer ein Accessoire.

    Accessoire-Groessen sind in Studs angegeben und skalieren mit dem Kopf
    (grosse Koepfe tragen groessere Huete). Wird auch vom Sanity-Check in
    build_avatar.py als Rueckfallgroesse benutzt.
    """
    layout = ACCESSORY_LAYOUT.get(asset_type_id)
    base = float(layout["target"]) if layout else 1.8
    head_scale = metrics.head_radius / 0.68
    return base * max(0.55, min(1.6, head_scale))


def fit_mesh_to_part(mesh: MeshData, target_lo, target_hi) -> MeshData:
    """Skaliert ein Mesh achsweise in eine Ziel-Box und zentriert es darin.

    Funktioniert wie das MeshPart-Verhalten in Roblox selbst: Die
    Bounding-Box des FileMesh wird auf die Part-Groesse gestreckt. Damit
    ersetzen echte Koerperteil-Meshes (aus RBXM) die Rig-Quader lueckenlos.
    """
    size = mesh.size()
    target = tuple(float(target_hi[i]) - float(target_lo[i]) for i in range(3))
    factors = []
    for i in range(3):
        factors.append(target[i] / size[i] if size[i] > 1e-9 else 1.0)
    scaled = mesh.transform(tuple(factors))
    center = scaled.center()
    target_center = tuple((float(target_lo[i]) + float(target_hi[i])) / 2.0 for i in range(3))
    return scaled.transform(1.0, tuple(target_center[i] - center[i] for i in range(3)))


def _rotation_is_identity(matrix) -> bool:
    if matrix is None:
        return True
    try:
        flat = [float(v) for row in matrix for v in row]
    except (TypeError, ValueError):
        return True
    if len(flat) != 9:
        return True
    expected = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    return all(abs(a - b) < 1e-6 for a, b in zip(flat, expected))


def place_accessory(mesh: MeshData, asset_type_id: int,
                    metrics: BodyMetrics,
                    size_hint=None,
                    rotation=None) -> Tuple[MeshData, str]:
    """Zentriert, skaliert (relativ zur Kopf-/Koerpergroesse) und platziert
    ein Accessoire-Mesh.

    Mit `size_hint` (Groesse aus dem RBXM-Modell, in Studs) wird das Mesh
    auf diese echte Groesse skaliert statt auf die heuristische
    Zielgroesse - der Sanity-Check in build_avatar.py faengt danach
    unplausible Werte ab (Mesh-Einheiten sind nicht immer Studs).

    `rotation` ist die CFrame-Rotation des Teils aus dem RBXM-Modell
    (3x3-Matrix als drei Zeilen). Sie beschreibt die Ausrichtung des
    Stuecks selbst (z. B. ein Brust-Stueck, das im Modell um 90 Grad
    gedreht liegt) und wird VOR dem Zentrieren angewendet - sonst steht
    das Accessoire seitlich statt in Blickrichtung.

    Rueckgabe: (transformiertes Mesh, Hinweistext)
    """
    layout = ACCESSORY_LAYOUT.get(asset_type_id)
    if layout is None:
        layout = {"label": "Accessoire", "target": 1.8, "anchor": "chest_front"}

    target = accessory_target_size(asset_type_id, metrics)

    size = mesh.size()
    max_dim = max(size) or 1.0
    if size_hint is not None and len(size_hint) == 3 and all(v > 0 for v in size_hint):
        # Form erhalten: gleichmaessig auf die groesste RBXM-Achse
        # skalieren. Pro-Achsen-Skalierung auf die Part-Size wuerde z. B.
        # eine flache Krone (im RBXM oft Size 1x1x1) zur Kugel verzerren.
        scale = max(size_hint) / max_dim
        placed = mesh.transform(scale)
        scale_note = (("RBXM-Groesse " + "x".join(_de(v, 2) for v in size_hint)
                       + " Studs, form-erhaltend skaliert"))
    else:
        scale = target / max_dim
        placed = mesh.transform(scale)
        scale_note = f"skaliert x{scale:.2f}"
    if not _rotation_is_identity(rotation):
        # Uniforme Skalierung kommutiert mit der Drehung - Reihenfolge egal,
        # hier vor dem Zentrieren, damit um den Part-Ursprung gedreht wird.
        placed = placed.rotate(rotation)
        scale_note += ", CFrame-Rotation uebernommen"
    c = placed.center()
    placed = placed.transform(1.0, (-c[0], -c[1], -c[2]))  # um Ursprung zentrieren

    half = placed.size()
    hx, hy, hz = half[0] / 2.0, half[1] / 2.0, half[2] / 2.0
    ao = metrics.anchor(layout["anchor"])  # Basis-Anker (ohne Accessoire-Masse)
    anchor = layout["anchor"]

    # Accessoire *auf* den Anker setzen statt mittig durch ihn hindurch
    if anchor == "head_top":
        # Bottom des Accessoires liegt knapp IN der Kopfsphaere -> sitzt auf
        bottom = metrics.head_center_y + metrics.head_radius * 0.30
        center_y = bottom + hy * 0.88
        offset = (0.0, center_y - ao[1], 0.0)
    elif anchor == "head_center":
        offset = (0.0, 0.0, 0.0)
    elif anchor == "face_front":
        offset = (0.0, 0.0, -hz * 0.55)
    elif anchor == "neck":
        offset = (0.0, hy * 0.35, 0.0)
    elif anchor == "shoulders":
        offset = (0.0, hy * 0.25, 0.0)
    elif anchor == "chest_front":
        offset = (0.0, 0.0, -hz * 0.45)
    elif anchor == "back":
        offset = (0.0, 0.0, hz * 0.45)
    elif anchor == "waist":
        offset = (0.0, 0.0, 0.0)
    elif anchor == "right_hand":
        offset = (hx * 0.2, 0.0, 0.0)
    else:
        offset = (0.0, 0.0, 0.0)

    placed = placed.transform(1.0, (ao[0] + offset[0], ao[1] + offset[1], ao[2] + offset[2]))

    note = (f"{layout['label']}: {scale_note} (Originalgroesse "
            + "x".join(_de(v, 1) for v in size) + f" Studs), Anker '{anchor}'")
    return placed, note


def body_part_colors(body_colors: Dict[str, Any]) -> Dict[str, str]:
    return {
        "head": brick_color_hex(body_colors.get("headColorId", 226)),
        "torso": brick_color_hex(body_colors.get("torsoColorId", 23)),
        "leftArm": brick_color_hex(body_colors.get("leftArmColorId", 226)),
        "rightArm": brick_color_hex(body_colors.get("rightArmColorId", 226)),
        "leftLeg": brick_color_hex(body_colors.get("leftLegColorId", 9)),
        "rightLeg": brick_color_hex(body_colors.get("rightLegColorId", 9)),
    }


def rgb01(hex_color: str) -> Tuple[float, float, float]:
    return hex_to_rgb(hex_color)
