"""Baut aus Avatar-Daten einen R15-artigen Rig in Standard-Stand-Pose.

Roblox liefert ueber die Web-APIs **keine** fertige, geriggte OBJ-Datei und
auch keine Attachment-Offsets fuer Accessoires. Deshalb:

* Der Koerper wird aus Quadern/Kugel erzeugt (Groesse folgt den `scales`
  des Avatars, Farben den `bodyColors`). Die Quader bekommen die UVs des
  klassischen Kleidungs-Atlasses (siehe `avatar_uv.py`), damit Shirt und
  Hose dort landen, wo sie hingehoeren.
* Traegt der Avatar echte Koerperteil-Meshes (R15-Bundles), werden deren
  **RBXM-Masse** fuer den Aufbau benutzt: Arme, Beine, Torso und Kopf
  werden gestapelt, statt in feste R6-Quader gepresst. So bleiben die
  Proportionen erhalten und die Teile sitzen ohne Versatz aneinander.
* Accessoires (Hut, Haare, Gesichts-Accessoire, ...) kommen als echte
  Roblox-Meshes aus der Asset Delivery API. Sie werden zentriert, auf eine
  sinnvolle Groesse normiert und an die passende Koerperstelle gesetzt
  (Kopf oben, Gesicht vorne, Brust, Ruecken, Hals, Taille, Hand).

Das Ergebnis ist eine *Naeherung* an den echten Avatar - gut genug fuer ein
kuenstlerisches Glas-Rendering, aber keine 1:1-Kopie der Roblox-Engine.
Details siehe ANLEITUNG.md, Abschnitt "Einschraenkungen".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import avatar_uv
from roblox_mesh import MeshData, make_box, make_sphere
from roblox_api import brick_color_hex, hex_to_rgb

# AssetTypeId -> Layout fuer Accessoires
ACCESSORY_LAYOUT: Dict[int, Dict[str, Any]] = {
    8: {"label": "Hut", "target": 1.75, "anchor": "head_top", "sink": 0.38},
    41: {"label": "Haare", "target": 1.85, "anchor": "head_top", "sink": 0.04},
    42: {"label": "Gesichts-Accessoire", "target": 1.35, "anchor": "face_front",
         "sink": 0.55},
    43: {"label": "Hals-Accessoire", "target": 2.2, "anchor": "neck", "sink": 0.35},
    44: {"label": "Schulter-Accessoire", "target": 2.9, "anchor": "shoulders",
         "sink": 0.25},
    45: {"label": "Brust-Accessoire", "target": 1.6, "anchor": "chest_front",
         "sink": 0.45},
    46: {"label": "Ruecken-Accessoire", "target": 2.2, "anchor": "back", "sink": 0.45},
    47: {"label": "Hueft-Accessoire", "target": 2.0, "anchor": "waist", "sink": 0.0},
    19: {"label": "Gear", "target": 1.3, "anchor": "right_hand", "sink": 0.2},
    76: {"label": "Augenbrauen", "target": 1.2, "anchor": "face_front", "sink": 0.6},
    77: {"label": "Wimpern", "target": 1.2, "anchor": "face_front", "sink": 0.6},
    # Layered-Kleidung (Shirt/Jacke/Pullover/Hose/Rock/Schuhe) sind
    # Hüllen um den Koerper, keine Ansteck-Accessoires. Sie werden auf
    # Torso bzw. Huefte/Bein gezogen statt vor die Brust gesetzt.
    64: {"label": "T-Shirt (Layered)", "target": 1.6, "anchor": "torso_shell"},
    65: {"label": "Shirt (Layered)", "target": 1.6, "anchor": "torso_shell"},
    66: {"label": "Hose (Layered)", "target": 1.6, "anchor": "hip_shell"},
    67: {"label": "Jacke (Layered)", "target": 1.8, "anchor": "torso_shell"},
    68: {"label": "Pullover (Layered)", "target": 1.8, "anchor": "torso_shell"},
    69: {"label": "Shorts (Layered)", "target": 1.6, "anchor": "hip_shell"},
    70: {"label": "Linker Schuh", "target": 1.0, "anchor": "foot_left"},
    71: {"label": "Rechter Schuh", "target": 1.0, "anchor": "foot_right"},
    72: {"label": "Rock (Layered)", "target": 1.8, "anchor": "hip_shell"},
    # DynamicHead gehoert an den Kopf, nicht an die Brust (Fallback-Anker).
    # Die eigentliche Behandlung (Kopf-Ersatz bzw. Headless) macht
    # build_avatar.py; das Layout greift nur fuer Zusatzteile.
    79: {"label": "Dynamic Head", "target": 1.45, "anchor": "head_center",
         "sink": 0.0},
}

# Standardmasse (Studs) des nachgebauten R6-Koerpers. Sie gelten nur, wenn
# der Avatar fuer ein Teil kein echtes Mesh mitbringt.
DEFAULT_PART_SIZES: Dict[str, Tuple[float, float, float]] = {
    "torso": (2.0, 2.0, 1.0),
    "rechts_arm": (1.0, 2.0, 1.0),
    "links_arm": (1.0, 2.0, 1.0),
    "rechts_bein": (1.0, 2.0, 1.0),
    "links_bein": (1.0, 2.0, 1.0),
}

# Roblox-Kopf: Durchmesser der Standardkugel in Studs.
DEFAULT_HEAD_DIAMETER = 1.36

# Kleine Ueberlappung an den Gelenken: Flaechen, die exakt aufeinander
# liegen, erzeugen im Glasrender dunkle Nahtstellen ("abgerissene" Teile).
JOINT_OVERLAP = 0.03


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
    torso_height: float = 2.0
    torso_bottom: float = 2.0
    shoulder_y: float = 4.0
    neck_y: float = 4.15
    head_center_y: float = 4.62
    head_radius: float = 0.68
    hand_y: float = 2.15
    hand_x: float = 1.45
    waist_y: float = 2.5
    arm_height: float = 2.0
    arm_width: float = 1.0
    arm_bottom: float = 2.0
    leg_height: float = 2.0
    leg_width: float = 1.0
    parts: Dict[str, Tuple[float, float, float]] = field(default_factory=dict)

    def anchor(self, name: str) -> Tuple[float, float, float]:
        if name == "head_top":
            return (0.0, self.head_center_y + self.head_radius * 0.95, 0.0)
        if name == "head_center":
            return (0.0, self.head_center_y, 0.0)
        if name == "face_front":
            return (0.0, self.head_center_y, -(self.head_radius * 0.95))
        if name == "neck":
            return (0.0, self.neck_y, 0.0)
        if name == "shoulders":
            return (0.0, self.shoulder_y - 0.1, 0.0)
        if name == "chest_front":
            return (0.0, self.torso_bottom + self.torso_height * 0.62,
                    -(self.torso_depth * 0.55))
        if name == "back":
            return (0.0, self.torso_bottom + self.torso_height * 0.62,
                    self.torso_depth * 0.55)
        if name == "waist":
            return (0.0, self.waist_y, 0.0)
        if name == "right_hand":
            return (self.hand_x, self.hand_y, 0.0)
        if name == "torso_center":
            return (0.0, self.torso_bottom + self.torso_height / 2.0, 0.0)
        return (0.0, self.height * 0.5, 0.0)

    @property
    def torso_center_y(self) -> float:
        return self.torso_bottom + self.torso_height / 2.0


def part_box_size(part_name: str, part_sizes: Optional[Dict[str, Sequence[float]]],
                  scales: Dict[str, Any]) -> Tuple[float, float, float]:
    """Groesse eines Koerperteil-Quad ers in Studs (inkl. Avatar-Scales)."""
    w = _clamp(scales.get("width"), 0.75, 1.30)
    h = _clamp(scales.get("height"), 0.75, 1.30)
    d = _clamp(scales.get("depth"), 0.75, 1.30)
    base = DEFAULT_PART_SIZES.get(part_name, (1.0, 2.0, 1.0))
    size = base
    if part_sizes:
        given = part_sizes.get(part_name)
        if given and len(given) == 3 and all(float(v) > 0.05 for v in given):
            size = tuple(float(v) for v in given)
    return (size[0] * w, size[1] * h, size[2] * d)


def head_diameter(scales: Dict[str, Any],
                  part_sizes: Optional[Dict[str, Sequence[float]]] = None) -> float:
    hs = _clamp(scales.get("head"), 0.75, 1.40)
    diameter = DEFAULT_HEAD_DIAMETER
    if part_sizes:
        given = part_sizes.get("kopf")
        if given and len(given) == 3 and all(float(v) > 0.05 for v in given):
            diameter = max(float(v) for v in given)
    return diameter * hs


def build_body(body_colors: Dict[str, Any], scales: Dict[str, Any],
               part_sizes: Optional[Dict[str, Sequence[float]]] = None
               ) -> Tuple[List[Part], BodyMetrics]:
    """Erzeugt den Koerper in Standard-Stand-Pose.

    ``part_sizes`` enthaelt optionale echte Masse (Studs) je Teil, z. B.
    aus den RBXM-Modellen der getragenen Koerperteile. Fehlt ein Teil,
    gelten die klassischen R6-Masse. Die Teile werden lueckenlos
    gestapelt (Beine -> Torso -> Hals -> Kopf, Arme an den Schultern),
    damit nichts "verschoben" wirkt.

    Bewusst wenige, geschlossene Volumen (Kopf, Torso, 2 Arme, 2 Beine):
    gestapelte Einzel-Quader erzeugen im Cycles-Glasrender dunkle
    Nahtstellen, weil dort Flaechen exakt aufeinander liegen.
    """
    colors = {
        "head": brick_color_hex(body_colors.get("headColorId", 226)),
        "torso": brick_color_hex(body_colors.get("torsoColorId", 23)),
        "left_arm": brick_color_hex(body_colors.get("leftArmColorId", 226)),
        "right_arm": brick_color_hex(body_colors.get("rightArmColorId", 226)),
        "left_leg": brick_color_hex(body_colors.get("leftLegColorId", 9)),
        "right_leg": brick_color_hex(body_colors.get("rightLegColorId", 9)),
    }

    sizes = {name: part_box_size(name, part_sizes, scales)
             for name in ("torso", "rechts_arm", "links_arm",
                          "rechts_bein", "links_bein")}

    torso_w, torso_h, torso_d = sizes["torso"]
    leg_w, leg_h, leg_d = sizes["rechts_bein"]
    arm_w, arm_h, arm_d = sizes["rechts_arm"]
    head_r = head_diameter(scales, part_sizes) / 2.0

    parts: List[Part] = []

    def box(part_name: str, size: Tuple[float, float, float],
            center: Tuple[float, float, float], color: str) -> Part:
        mesh = make_box(size[0], size[1], size[2], center)
        # Klassische Kleidungs-UVs (Shirt/Pants) nur auf den bekleideten
        # Teilen; Hals/Kopf nutzen die normalen Quader-UVs.
        mesh = avatar_uv.apply_box_uv(mesh, part_name)
        return Part(part_name, mesh, color, "body")

    # --- Beine: stehen auf y = 0, beruehren sich in der Mitte -------------
    leg_x = leg_w / 2.0 - JOINT_OVERLAP * 0.5
    parts.append(box("rechts_bein", (leg_w, leg_h, leg_d),
                     (leg_x, leg_h / 2.0, 0.0), colors["right_leg"]))
    parts.append(box("links_bein", (leg_w, leg_h, leg_d),
                     (-leg_x, leg_h / 2.0, 0.0), colors["left_leg"]))

    # --- Torso: sitzt auf den Beinen auf ---------------------------------
    torso_bottom = leg_h - JOINT_OVERLAP
    torso_center_y = torso_bottom + torso_h / 2.0
    parts.append(box("torso", (torso_w, torso_h, torso_d),
                     (0.0, torso_center_y, 0.0), colors["torso"]))
    shoulder_y = torso_bottom + torso_h

    # --- Arme: oben an der Schulter, haengen seitlich herab --------------
    arm_x = torso_w / 2.0 + arm_w / 2.0 - JOINT_OVERLAP
    arm_top = shoulder_y + JOINT_OVERLAP
    arm_center_y = arm_top - arm_h / 2.0
    parts.append(box("rechts_arm", (arm_w, arm_h, arm_d),
                     (arm_x, arm_center_y, 0.0), colors["right_arm"]))
    parts.append(box("links_arm", (arm_w, arm_h, arm_d),
                     (-arm_x, arm_center_y, 0.0), colors["left_arm"]))

    # --- Hals: kurz, verschwindet in Torso und Kopf ----------------------
    neck_h = min(0.30 * (head_r * 2.0), 0.35 * torso_h)
    neck_w = max(0.55, min(torso_w * 0.42, head_r * 0.85))
    neck_d = max(0.5, min(torso_d * 0.55, head_r * 0.85))
    neck_bottom = shoulder_y - 0.05 - JOINT_OVERLAP
    neck_center_y = neck_bottom + neck_h / 2.0
    parts.append(Part("hals", make_box(neck_w, neck_h, neck_d,
                                      (0.0, neck_center_y, 0.0)),
                      colors["head"], "body"))

    # --- Kopf: sitzt auf dem Hals, leicht nach vorne ---------------------
    head_center_y = neck_bottom + neck_h + head_r * 0.80
    parts.append(Part("kopf", make_sphere(head_r, 32, 22,
                                         (0.0, head_center_y, -0.02)),
                      colors["head"], "body"))

    metrics = BodyMetrics(
        height=head_center_y + head_r,
        torso_width=torso_w,
        torso_depth=torso_d,
        torso_height=torso_h,
        torso_bottom=torso_bottom,
        shoulder_y=shoulder_y,
        neck_y=neck_bottom + neck_h * 0.5,
        head_center_y=head_center_y,
        head_radius=head_r,
        hand_y=arm_center_y - arm_h / 2.0 + 0.18 * arm_h,
        hand_x=arm_x,
        waist_y=torso_bottom + torso_h * 0.22,
        arm_height=arm_h,
        arm_width=arm_w,
        arm_bottom=arm_center_y - arm_h / 2.0,
        leg_height=leg_h,
        leg_width=leg_w,
        parts=dict(sizes),
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


def upright_limb(mesh: MeshData, part_name: str) -> Tuple[MeshData, str]:
    """Dreht ein in T-Pose modelliertes Glied senkrecht nach unten.

    R15-Bundles liefern Arme/Beine teils liegend (Arm zeigt nach +X bzw.
    -X statt nach unten). Wird das Mesh einfach in einen senkrechten
    Quader gestreckt, entstehen die "verrenkten" Arme aus dem Render.
    Hier wird die Hauptachse geprueft und das Mesh um die Z-Achse
    gedreht, sodass die Schulter oben und die Hand unten liegt.

    Rueckgabe: (Mesh, Hinweis) - Hinweis ist leer, wenn nichts gedreht
    wurde.
    """
    size = mesh.size()
    if max(size) <= 0.0 or size[2] >= max(size[0], size[1]):
        return mesh, ""  # flach in Z: nichts zu drehen
    if size[1] >= size[0] * 1.15:
        return mesh, ""  # bereits senkrecht
    if size[0] < size[1] * 1.15:
        return mesh, ""
    # Glied liegt waagerecht. Rechte Koerperhaelfte zeigt in T-Pose nach
    # +X, linke nach -X; die Schulter liegt jeweils innen.
    angle = -90.0 if part_name.startswith("rechts") else 90.0
    rotated = mesh.rotate_z(angle)
    note = (f"T-Pose erkannt ({_de(size[0])}x{_de(size[1])}x{_de(size[2])} "
            f"Studs) und um {_de(abs(angle), 0)} Grad aufgerichtet")
    return rotated, note


def _shell_box(anchor: str, metrics: BodyMetrics):
    """Ziel-Box fuer Kleidungs-Huellen (Layered-Kleidung).

    Rueckgabe ``None``, wenn der Anker keine Huelle ist.
    """
    cx = 0.0
    if anchor == "torso_shell":
        center = (cx, metrics.torso_center_y, 0.0)
        size = (metrics.torso_width * 1.08,
                metrics.torso_height * 1.02,
                metrics.torso_depth * 1.45)
    elif anchor == "hip_shell":
        hip_h = max(metrics.leg_height * 0.55, metrics.torso_height * 0.5)
        center = (cx, metrics.waist_y + hip_h * 0.15, 0.0)
        size = (metrics.torso_width * 1.10, hip_h,
                metrics.torso_depth * 1.55)
    elif anchor in ("foot_left", "foot_right"):
        side = 1.0 if anchor == "foot_right" else -1.0
        foot_w = metrics.leg_width * 1.15
        foot_h = max(metrics.leg_width * 0.55, 0.35)
        center = (side * (metrics.leg_width / 2.0),
                  foot_h * 0.5, -metrics.leg_width * 0.1)
        size = (foot_w, foot_h, metrics.leg_width * 1.55)
    else:
        return None
    half = tuple(v / 2.0 for v in size)
    lo = tuple(center[i] - half[i] for i in range(3))
    hi = tuple(center[i] + half[i] for i in range(3))
    return lo, hi


def place_accessory(mesh: MeshData, asset_type_id: int,
                    metrics: BodyMetrics,
                    size_hint=None) -> Tuple[MeshData, str]:
    """Zentriert, skaliert (relativ zur Kopf-/Koerpergroesse) und platziert
    ein Accessoire-Mesh.

    Mit `size_hint` (Groesse aus dem RBXM-Modell, in Studs) wird das Mesh
    auf diese echte Groesse skaliert statt auf die heuristische
    Zielgroesse - der Sanity-Check in build_avatar.py faengt danach
    unplausible Werte ab (Mesh-Einheiten sind nicht immer Studs).

    Rueckgabe: (transformiertes Mesh, Hinweistext)
    """
    layout = ACCESSORY_LAYOUT.get(asset_type_id)
    if layout is None:
        layout = {"label": "Accessoire", "target": 1.8, "anchor": "chest_front",
                  "sink": 0.45}

    shell = _shell_box(layout["anchor"], metrics)
    if shell is not None:
        # Kleidungs-Huelle: achsweise auf den Koerper ziehen (wie ein
        # Kleidungsstueck, das ueber dem Koerper liegt).
        lo, hi = shell
        placed = fit_mesh_to_part(mesh, lo, hi)
        size = placed.size()
        note = (f"{layout['label']}: als Huelle um den Koerper gezogen "
                f"({_de(size[0])}x{_de(size[1])}x{_de(size[2])} Studs), "
                f"Anker '{layout['anchor']}'")
        return placed, note

    target = accessory_target_size(asset_type_id, metrics)

    size = mesh.size()
    max_dim = max(size) or 1.0
    if size_hint is not None and len(size_hint) == 3 and all(v > 0 for v in size_hint):
        # Form erhalten: gleichmaessig auf die groesste RBXM-Achse
        # skalieren. Pro-Achsen-Skalierung auf die Part-Size wuerde z. B.
        # eine flache Krone (im RBXM oft Size 1x1x1) zur Kugel verzerren.
        scale = max(size_hint) / max_dim
        placed = mesh.transform(scale)
        scale_note = ("RBXM-Groesse " + "x".join(_de(v, 2) for v in size_hint)
                      + " Studs, form-erhaltend skaliert")
    else:
        scale = target / max_dim
        placed = mesh.transform(scale)
        scale_note = f"skaliert x{scale:.2f}"
    c = placed.center()
    placed = placed.transform(1.0, (-c[0], -c[1], -c[2]))  # um Ursprung zentrieren

    half = placed.size()
    hx, hy, hz = half[0] / 2.0, half[1] / 2.0, half[2] / 2.0
    ao = metrics.anchor(layout["anchor"])  # Basis-Anker (ohne Accessoire-Masse)
    anchor = layout["anchor"]
    sink = float(layout.get("sink", 0.0))

    # Accessoire *auf* den Anker setzen statt mittig durch ihn hindurch.
    # `sink` bestimmt, wie weit es in den Koerper eintaucht (Hut sitzt auf
    # dem Kopf, Haare umschliessen die obere Kopfhaelfte).
    if anchor == "head_top":
        bottom = metrics.head_center_y + metrics.head_radius * sink
        offset = (0.0, bottom + hy - ao[1], 0.0)
    elif anchor == "head_center":
        offset = (0.0, 0.0, 0.0)
    elif anchor == "face_front":
        offset = (0.0, 0.0, -hz * sink)
    elif anchor == "neck":
        offset = (0.0, hy * sink, 0.0)
    elif anchor == "shoulders":
        offset = (0.0, hy * sink, 0.0)
    elif anchor == "chest_front":
        offset = (0.0, 0.0, -hz * sink)
    elif anchor == "back":
        offset = (0.0, 0.0, hz * sink)
    elif anchor == "waist":
        offset = (0.0, 0.0, 0.0)
    elif anchor == "right_hand":
        offset = (hx * sink, 0.0, 0.0)
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
