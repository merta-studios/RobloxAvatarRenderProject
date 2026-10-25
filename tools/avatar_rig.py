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

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from avatar_uv import (apply_limb_box_uvs, apply_limb_uvs_projected,
                       apply_torso_box_uvs)
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


#: Kanonische R15-Proportionen (Roblox "Rig and Attachments"), bezogen auf die
#: Torso-Hoehe. Sie dienen (a) dazu, fehlende Koerperteile als R15-Quader zu
#: erzeugen, und (b) als Sicherheitsnetz fuer inkonsistente Bundles. Roblox
#: selbst skaliert beim Avatar-Aufbau nichts: Segmente behalten ihre
#: Mesh-Groesse, Gelenke sitzen auf den Attachments. Skaliert wird hier nur,
#: wenn sonst etwas sichtbar kaputt waere - und dann die *ganze* Extremitaet
#: mit EINEM Faktor auf allen drei Achsen (Silhouette bleibt, nichts wird
#: plattgedrueckt).
R15_ARM_LENGTH_PER_TORSO_HEIGHT = 0.869   # Schulter -> Handende
R15_WRIST_FRACTION_OF_ARM = 0.856         # Schulter -> Handgelenk
#: Kanonische Beinlaenge (Huefte->Sohle) in Torso-Hoehen - nur fuer Platzhalter.
#: Huefte -> Fussgelenk (kanonisch 1,604 Studs bei 2,0 Studs Torso).
R15_ANKLE_FRACTION_OF_LEG = 0.80
#: Huefte -> Sohle inklusive Fuss (kanonisch 2,02 Studs bei 2,0 Torso).
R15_SOLE_FRACTION_OF_LEG = 1.0
R15_LIMB_SCALE_MIN = 0.60                 # Sicherheitsnetz gegen kaputte Daten
R15_LIMB_SCALE_MAX = 1.60
#: Darunter bleibt das Bundle exakt so, wie es ist (Authentizitaet vor
#: Normierung) - erst bei groesserer Abweichung wird skaliert.
R15_LIMB_TOLERANCE = 0.15
#: Ab dieser Abweichung werden linkes und rechtes Bein angeglichen (sonst
#: steht der Avatar auf einem Bein in der Luft).
R15_LEG_SYMMETRY_TOLERANCE = 1.05
#: R15-Schulterlinie: 0,5 Studs unter der Oberkante bei 2 Studs Torso-Hoehe -
#: also ein Viertel der Torso-Hoehe. Autoren-Attachments in diesem Bereich
#: bleiben unangetastet.
R15_SHOULDER_FRACTION_OF_TORSO = 0.25
#: Erlaubtes Band fuer die Oberkante des Oberarms, in Torso-Hoehen unter der
#: Torso-Oberkante. Zwei Bug-Reports stecken die Grenzen ab: ein Arm, dessen
#: Oberkante ueber der Torso-Oberkante liegt, wirkt "zu hoch"; einer, der ein
#: Zehntel darunter beginnt, wirkt "viel zu niedrig". Deshalb: hoechstens 2 %
#: darueber (praktisch buendig) und hoechstens 6 % darunter. Liegt der Arm
#: ausserhalb, wird er starr auf die Mitte des Bandes (4 %) verschoben.
R15_ARM_TOP_BAND = (0.02, 0.06)
R15_ARM_TOP_TARGET = 0.04
#: Ueberlappung der Standard-Avatar-Segmente (Faktor auf die Segmenthoehe).
#: Die R15-Block-Meshes sind an den Enden abgerundet; ohne Ueberlappung
#: entstehen sichtbare Luecken zwischen Ober- und Unterglied.
R15_STANDARD_SEGMENT_OVERLAP = 1.12

#: Meshes des **Standard-Avatars** (R15-Block-Koerper). Roblox baut den
#: Standard-Koerper in der Engine aus genau diesen FileMesh-Assets; sie haben
#: keine Katalog-/Bundle-ID, sind aber als Mesh-Assets (AssetTypeId 4) ladbar.
#: Fehlt ein Koerperteil im Inventar, wird es damit ersetzt - nicht durch
#: scharfe Quader und nicht durch Teile des getragenen Pakets.
STANDARD_BODY_MESH_IDS: Dict[str, int] = {
    "LowerTorso": 387941715,
    "UpperTorso": 387941468,
    "Head": 387941905,
    "LeftUpperArm": 387938468,
    "LeftLowerArm": 387938971,
    "LeftHand": 387939233,
    "RightUpperArm": 387940113,
    "RightLowerArm": 387940356,
    "RightHand": 387940548,
    "LeftUpperLeg": 387939645,
    "LeftLowerLeg": 387939489,
    "LeftFoot": 387939912,
    "RightUpperLeg": 387940976,
    "RightLowerLeg": 387940802,
    "RightFoot": 387941196,
}

#: Welche Segmente ein fehlendes Koerperteil im Standard-Avatar umfasst.
STANDARD_SEGMENTS: Dict[str, Tuple[str, ...]] = {
    "LeftArm": ("LeftUpperArm", "LeftLowerArm", "LeftHand"),
    "RightArm": ("RightUpperArm", "RightLowerArm", "RightHand"),
    "LeftLeg": ("LeftUpperLeg", "LeftLowerLeg", "LeftFoot"),
    "RightLeg": ("RightUpperLeg", "RightLowerLeg", "RightFoot"),
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
    # Unterkante fuer Kopfbedeckungen. None = aus Kopfmitte/-radius ableiten.
    # Bei Headless-Avataren (kein Kopf im Mesh) wird sie auf den Kragen
    # gesetzt, sonst schweben Huete meterweit ueber dem Koerper.
    hat_base_y: Optional[float] = None

    def anchor(self, name: str) -> Tuple[float, float, float]:
        if name == "head_top":
            if self.hat_base_y is not None:
                return (0.0, self.hat_base_y, 0.0)
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
    arm_gap = 0.055  # etwas mehr Luft zwischen Torso und Armen (sonst wirken Arme „im Koerper“)

    # Beine (stehen auf y=0)
    leg_x = leg_w / 2 + gap * 0.5
    parts.append(Part("rechts_bein", apply_limb_box_uvs(
        make_box(leg_w, leg_h, leg_d, (leg_x, leg_h / 2, 0.0))),
        colors["right_leg"], "body"))
    parts.append(Part("links_bein", apply_limb_box_uvs(
        make_box(leg_w, leg_h, leg_d, (-leg_x, leg_h / 2, 0.0))),
        colors["left_leg"], "body"))

    # Torso (mit minimalem Abstand zu den Beinen - sonst dunkle Naht im Glas)
    torso_y = leg_h + gap * 2 + torso_h / 2
    parts.append(Part("torso", apply_torso_box_uvs(
        make_box(torso_w, torso_h, torso_d, (0.0, torso_y, 0.0))),
        colors["torso"], "body"))

    # Arme (haengen seitlich herab) - mit arm_gap statt gap, damit sie nicht in den Torso ragen
    arm_x = torso_w / 2 + arm_w / 2 + arm_gap
    arm_y = torso_y
    parts.append(Part("rechts_arm", apply_limb_box_uvs(
        make_box(arm_w, arm_h, arm_d, (arm_x, arm_y, 0.0))),
        colors["right_arm"], "body"))
    parts.append(Part("links_arm", apply_limb_box_uvs(
        make_box(arm_w, arm_h, arm_d, (-arm_x, arm_y, 0.0))),
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


LEG_PARTS = ("rechts_bein", "links_bein")
ARM_PARTS = ("rechts_arm", "links_arm")


def relayout_body(default_parts: List[Part], metrics: BodyMetrics,
                  mesh_sizes: Dict[str, Tuple[float, float, float]],
                  scales: Dict[str, Any],
                  head_size: Optional[Tuple[float, float, float]] = None,
                  headless: bool = False
                  ) -> Tuple[List[Part], BodyMetrics]:
    """Baut den Rig aus den **echten** Mesh-Groessen neu zusammen.

    Warum: Die Standard-Boxen von :func:`build_body` (Arm, Bein, Torso alle
    nach Schema F) passen nicht zu den Meshes, die Roblox fuer ein Bundle
    liefert. Beim Einpressen der Meshes in diese Boxen wurde jedes Teil
    achsweise gestreckt (Korblox-Arm, Rthro-Torso, schlanke Beine) - genau
    das "gedehnt / falsch skaliert / zu gross" aus dem Bug-Report.

    Hier werden Groesse und Position aus den gestapelten Bundle-Segmenten
    abgeleitet: Beine stehen auf dem Boden, der Torso sitzt darauf, die
    Arme haengen an der Schulter, der Kopf sitzt auf dem Hals. Alles
    bleibt unverzerrt (nur die Avatar-Skalen width/height/depth werden
    angewandt, so wie Roblox sie auch anwendet).

    ``mesh_sizes``: natuerliche Groessen (Studs) der Mesh-Teile, z. B.
    ``{"torso": (2.0, 2.1, 1.0), "rechts_arm": (1.0, 1.9, 1.0)}``.

    Rueckgabe: (parts, metrics) mit den neuen Boxen.
    """
    w = _clamp(scales.get("width"), 0.75, 1.30)
    h = _clamp(scales.get("height"), 0.75, 1.30)
    d = _clamp(scales.get("depth"), 0.75, 1.30)
    hs = _clamp(scales.get("head"), 0.75, 1.40)
    gap = 0.012
    arm_gap = 0.055  # mehr Luft seitlich fuer Arme (fix: Arme ragten leicht in Torso)

    def fallback_size(name: str) -> Tuple[float, float, float]:
        part = next((p for p in default_parts if p.name == name), None)
        return part.mesh.size() if part is not None else (1.0, 1.0, 1.0)

    def size_of(name: str) -> Tuple[float, float, float]:
        fb = fallback_size(name)
        natural = mesh_sizes.get(name)
        if not natural or min(natural) <= 1e-6:
            return fb
        # Mesh-Masse sind in Studs - Avatar-Skalen wirken genauso wie in
        # Roblox (Breite, Hoehe, Tiefe). Zusaetzlich pro Achse eine
        # Plausibilitaetsgrenze (0,5x .. 2x der Standard-Box): manche
        # Bundles liefern Mesh-Einheiten, die nicht Studs sind - ohne
        # Grenze wuerde daraus ein 100-Studs-Arm.
        scaled = (natural[0] * w, natural[1] * h, natural[2] * d)
        return tuple(max(0.5 * fb[i], min(2.0 * fb[i], scaled[i]))
                     for i in range(3))  # type: ignore[return-value]

    sizes = {name: size_of(name) for name in
             ("torso", "rechts_arm", "links_arm", "rechts_bein", "links_bein")}

    leg_w = max(sizes["rechts_bein"][0], sizes["links_bein"][0])

    torso_w, torso_h, torso_d = sizes["torso"]
    leg_h = max(sizes["rechts_bein"][1], sizes["links_bein"][1])
    torso_bottom = leg_h + gap
    torso_top = torso_bottom + torso_h

    head_r_default = 0.68 * hs * h
    hat_base_y: Optional[float] = None
    if headless:
        # Kein Kopf im Mesh (Headless Head): kurzer Kragen statt Hals, und
        # Kopfbedeckungen sitzen knapp ueber dem Torso statt dort, wo der
        # unsichtbare Kopf waere (sonst schweben sie weit ueber dem Koerper).
        head_r = head_r_default * 0.75
        neck_h = 0.16 * h
        head_center_y = torso_top + neck_h + head_r * 0.25
        hat_base_y = torso_top + neck_h * 0.8
    else:
        if head_size and min(head_size) > 1e-6:
            head_r = 0.5 * max(head_size) * hs
            body_h = torso_top  # ohne Kopf
            head_r = max(0.10 * body_h, min(0.17 * body_h, head_r))
        else:
            head_r = head_r_default
        neck_h = min(0.40 * h, 0.55 * head_r)
        head_center_y = torso_top + neck_h + head_r * 0.94

    colors = {p.name: p.color for p in default_parts}

    def color(name: str) -> str:
        return colors.get(name, "#c7ac78")

    parts: List[Part] = []
    # Beine: an der Hueftlinie aufgehaengt; das laengere Bein steht auf y = 0,
    # kuerzere Stumpf-Beine (z. B. Korblox) schweben originalgetreu an der Huefte.
    for name, sign in (("rechts_bein", 1.0), ("links_bein", -1.0)):
        sx, sy, sz = sizes[name]
        cy = leg_h - sy / 2.0
        parts.append(Part(name, apply_limb_box_uvs(
            make_box(sx, sy, sz, (sign * (leg_w / 2 + gap * 0.5), cy, 0.0))),
            color(name), "body"))
    # Torso
    parts.append(Part("torso", apply_torso_box_uvs(
        make_box(torso_w, torso_h, torso_d, (0.0, torso_bottom + torso_h / 2, 0.0))),
        color("torso"), "body"))
    # Arme: Oberkante je Arm an der Schulterhoehe, seitlich buendig am Torso
    right_arm_offset = torso_w / 2 + sizes["rechts_arm"][0] / 2 + arm_gap
    for name, sign in (("rechts_arm", 1.0), ("links_arm", -1.0)):
        sx, sy, sz = sizes[name]
        arm_offset = torso_w / 2 + sx / 2 + arm_gap
        center_y = torso_top - sy / 2
        parts.append(Part(name, apply_limb_box_uvs(
            make_box(sx, sy, sz, (sign * arm_offset, center_y, 0.0))),
            color(name), "body"))
    # Hals (verbindet Torso und Kopf, steckt in beiden)
    neck_bottom = torso_top - 0.06
    neck_top = head_center_y - head_r * 0.55
    neck_h_eff = max(0.08, neck_top - neck_bottom)
    parts.append(Part("hals", make_box(min(torso_w * 0.45, 0.7 * w), neck_h_eff,
                                       min(torso_d * 0.7, 0.7 * d),
                                       (0.0, neck_bottom + neck_h_eff / 2, 0.0)),
                      color("hals") if "hals" in colors else color("torso"), "body"))
    # Kopf: Kugel (wird in build_avatar durch das echte Kopf-Mesh ersetzt)
    if not headless:
        parts.append(Part("kopf", make_sphere(head_r, 32, 22,
                                              (0.0, head_center_y, -0.02 * d)),
                          color("kopf") if "kopf" in colors else color("torso"), "body"))

    new_metrics = BodyMetrics(
        height=head_center_y + head_r,
        torso_width=torso_w,
        torso_depth=torso_d,
        shoulder_y=torso_top,
        head_center_y=head_center_y,
        head_radius=head_r,
        hand_y=torso_top - sizes["rechts_arm"][1] + 0.15,
        hand_x=right_arm_offset,
        waist_y=torso_bottom + torso_h * 0.25,
        hat_base_y=hat_base_y,
    )
    return parts, new_metrics


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


def fit_mesh_to_part(mesh: MeshData, target_lo, target_hi,
                     uniform: bool = True, max_overflow: float = 1.06) -> MeshData:
    """Skaliert ein Mesh in eine Ziel-Box und zentriert es darin.

    ``uniform=True`` (Default): **gleichmaessig** skalieren, bis das Mesh in
    die Box passt. Vorher wurde jede Achse einzeln auf die Box gestreckt -
    ein Korblox-Arm (schmal, mit Klaue) oder ein Rthro-Torso wurde dadurch
    sichtbar in die Laenge/ Breite gezogen ("gedehnt", "falsch skaliert").

    ``uniform=False``: achsweise auf die Box strecken (altes Verhalten, nur
    noch fuer einfache Quader sinnvoll).

    ``max_overflow`` erlaubt eine minimale Ueberschreitung (Standard 6 %),
    damit ein flaches Mesh (z. B. ein Schild) den Koerper nicht winzig
    schrumpft: skaliert wird mit dem groessten Faktor, der die Box noch
    (fast) einhaelt.
    """
    size = mesh.size()
    target = tuple(float(target_hi[i]) - float(target_lo[i]) for i in range(3))
    if not uniform:
        factors = [target[i] / size[i] if size[i] > 1e-9 else 1.0 for i in range(3)]
        scaled = mesh.transform(tuple(factors))
    else:
        ratios = [target[i] / size[i] for i in range(3) if size[i] > 1e-9]
        if not ratios:
            scaled = mesh.transform(1.0)
        else:
            # Kleinster Faktor = passt garantiert; bei sehr duennen Meshes
            # (eine Achse fast 0) wuerde er das Teil winzig machen -> dann
            # den zweitkleinsten Faktor mit begrenzter Ueberschreitung nutzen.
            ratios.sort()
            factor = ratios[0]
            if len(ratios) > 1 and ratios[1] / ratios[0] < 0.5:
                factor = ratios[1] / max(1.0, max_overflow)
            scaled = mesh.transform(factor)
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
    # Die Part-Groesse aus dem RBXM ist nur brauchbar, wenn sie zur
    # Accessoire-Art passt. Bundles liefern oft Size 1x1x1 als Platzhalter,
    # waehrend das Mesh selbst 3 Studs breit ist - das wurde bisher auf
    # 1 Stud geschrumpft (Hut verschwindet im Kopf) oder umgekehrt
    # riesig skaliert. Plausibel ist alles im Faktor 0,6..1,8 um die
    # Zielgroesse; sonst gilt die Zielgroesse der Accessoire-Art.
    hint_size = None
    if size_hint is not None and len(size_hint) == 3 and all(v > 0 for v in size_hint):
        candidate = max(size_hint)
        if 0.6 * target <= candidate <= 1.8 * target:
            hint_size = candidate

    if hint_size is not None:
        # Form erhalten: gleichmaessig auf die groesste RBXM-Achse
        # skalieren. Pro-Achsen-Skalierung wuerde z. B. eine flache Krone
        # (im RBXM oft Size 1x1x1) zur Kugel verzerren.
        scale = hint_size / max_dim
        placed = mesh.transform(scale)
        scale_note = (("RBXM-Groesse " + "x".join(_de(v, 2) for v in size_hint)
                       + " Studs, form-erhaltend skaliert"))
    else:
        scale = target / max_dim
        placed = mesh.transform(scale)
        scale_note = f"auf Zielgroesse {_de(target, 2)} Studs skaliert"
        if size_hint is not None:
            scale_note += (" (RBXM-Groesse "
                           + "x".join(_de(float(v), 2) for v in size_hint)
                           + " unplausibel, ignoriert)")
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
        # Unterkante des Accessoires liegt auf der Kopfsphaere. Vorher
        # steckte der Hut zu 70 % im Kopf (bottom = Mitte + 0,3 r) -
        # sichtbar waren nur noch Ränder, der Rest war "im Kopf".
        if metrics.hat_base_y is not None:
            bottom = metrics.hat_base_y      # Headless: direkt ueber dem Kragen
        else:
            bottom = metrics.head_center_y + metrics.head_radius * 0.82
        center_y = bottom + hy * 0.92
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


# ---------------------------------------------------------------------------
# Exakter R15-Gelenk-Rig + Accessoire-Weld (aus echten RBXM-Attachments)
# ---------------------------------------------------------------------------

Matrix3 = Tuple[Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float]]
IDENTITY_MAT3: Matrix3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))

HEAD_ATTACHMENT_NAMES = {
    "HatAttachment",
    "HairAttachment",
    "FaceFrontAttachment",
    "FaceCenterAttachment",
    "NeckRigAttachment",
}


def _mat3_mul(a: Matrix3, b: Matrix3) -> Matrix3:
    return tuple(  # type: ignore[return-value]
        tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
        for i in range(3)
    )


def _mat3_transpose(m: Matrix3) -> Matrix3:
    return (
        (m[0][0], m[1][0], m[2][0]),
        (m[0][1], m[1][1], m[2][1]),
        (m[0][2], m[1][2], m[2][2]),
    )


def _mat3_vec(m: Matrix3, v: Tuple[float, float, float]) -> Tuple[float, float, float]:
    return (
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    )


def _as_mat3(raw: Any) -> Matrix3:
    if raw is None:
        return IDENTITY_MAT3
    try:
        rows = tuple(tuple(float(v) for v in row) for row in raw)
        if len(rows) == 3 and all(len(r) == 3 for r in rows):
            return rows  # type: ignore[return-value]
    except (TypeError, ValueError):
        pass
    return IDENTITY_MAT3


def _merge_world_meshes(meshes: List[MeshData], source_version: str = "r15-rigged") -> MeshData:
    positions: List[Tuple[float, float, float]] = []
    normals: List[Tuple[float, float, float]] = []
    uvs: List[Tuple[float, float]] = []
    faces: List[Tuple[int, int, int]] = []
    colors: List[Tuple[int, int, int, int]] = []
    all_colors = True
    for m in meshes:
        if m.is_empty:
            continue
        base = len(positions)
        positions.extend(m.positions)
        normals.extend(m.normals if len(m.normals) == len(m.positions) else [(0.0, 1.0, 0.0)] * len(m.positions))
        uvs.extend(m.uvs if len(m.uvs) == len(m.positions) else [(0.0, 0.0)] * len(m.positions))
        faces.extend((a + base, b + base, c + base) for a, b, c in m.faces)
        if m.colors and len(m.colors) == len(m.positions):
            colors.extend(m.colors)
        else:
            all_colors = False
    return MeshData(
        positions=positions,
        normals=normals,
        uvs=uvs,
        faces=faces,
        colors=colors if all_colors and colors else None,
        source_version=source_version,
    )


@dataclass
class RiggedAvatar:
    """Welt-Raum-Ergebnis des Attachment-basierten R15-Rigs.

    ``joint_frames``/``segment_driver``/``segment_spans`` bilden die
    Motor6D-Semantik ab, mit der Roblox Charaktere aufbaut und animiert
    (``Humanoid:BuildRigFromAttachments`` bzw. die RCC-Formel
    ``part1 = part0 * C0 * Transform * C1^-1``):

    * ``joint_frames``: Bind-Welt-Orientierung des koinzidierenden
      Attachment-Frames je Gelenk (= C0-Frame, Schlüssel wie
      ``joint_positions``). Posen-Transformationen sind in DIESEM Frame
      definiert, nicht in Weltachsen.
    * ``segment_driver``: R15-Segment -> Name des Gelenks, an dem es hängt
      (``None`` = Wurzel ``LowerTorso``).
    * ``segment_spans``: Koerperteil -> [(Segment, Vertex-Start, Vertex-Ende)]
      im zusammengefuegten Mesh - erlaubt pose-Anwendung STARR je Segment
      statt weicher Vertex-Verformung (R15 ohne Skinning ist starr).
    """

    body_meshes: Dict[str, MeshData] = field(default_factory=dict)
    head_mesh: Optional[MeshData] = None
    world_attachments: Dict[str, Tuple[Tuple[float, float, float], Matrix3]] = field(default_factory=dict)
    joint_positions: Dict[str, Tuple[float, float, float]] = field(default_factory=dict)
    joint_frames: Dict[str, Matrix3] = field(default_factory=dict)
    segment_driver: Dict[str, Optional[str]] = field(default_factory=dict)
    segment_spans: Dict[str, List[Tuple[str, int, int]]] = field(default_factory=dict)
    metrics: BodyMetrics = field(default_factory=BodyMetrics)
    scales_body: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    scales_head: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    #: Hinweise des Gelenk-Audits (fehlende/plausiblere Attachments, ersetzte
    #: Platzhalter-Gelenke). Landet als Warnung im Build-Log.
    notes: List[str] = field(default_factory=list)
    #: Je Gelenk der tatsaechlich verwendete Abstand zum Eltern-Gelenk in
    #: Studs (0 = Naht geschlossen). Nur zur Diagnose.
    joint_spans: Dict[str, float] = field(default_factory=dict)
    #: Platzierte Segment-Meshes (Weltraum, vor der Pose) je Instanzname -
    #: fuer das Diagnose-Manifest ("welches Teil sitzt wo?").
    placed_segments: Dict[str, MeshData] = field(default_factory=dict)
    #: Rohdaten je Segment (Groessen + Attachments) fuer das Diagnose-Manifest.
    segment_info: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def _attach_placement(
    parent_att: Tuple[Tuple[float, float, float], Matrix3],
    child_att: Tuple[Tuple[float, float, float], Matrix3],
) -> Tuple[Matrix3, Tuple[float, float, float]]:
    """Motor6D-Montage zweier RigAttachments (Roblox-Semantik).

    ``Humanoid:BuildRigFromAttachments()`` baut jeden Motor aus
    ``C0 = Eltern-Attachment.CFrame`` und ``C1 = Kind-Attachment.CFrame``;
    im Bind-Zustand (``Transform = Identitaet``) gilt
    ``child_world = parent_world * C0 * C1^-1`` - beide Attachment-Frames
    koinzidieren also exakt, Position UND Orientierung. Genau diese
    Platzierung liefert diese Funktion: ``(Rotation, Ursprung)`` fuer
    ``register_segment``. Keine geratenen Winkel, keine Offsets.
    """
    p_pos, p_rot = parent_att
    c_pos, c_rot = child_att
    rot = _mat3_mul(p_rot, _mat3_transpose(c_rot))
    rotated = _mat3_vec(rot, c_pos)
    origin = (p_pos[0] - rotated[0], p_pos[1] - rotated[1], p_pos[2] - rotated[2])
    return rot, origin


def _merge_with_spans(
    named_meshes: List[Tuple[str, MeshData]],
    source_version: str = "r15-rigged",
) -> Tuple[MeshData, List[Tuple[str, int, int]]]:
    """Segment-Meshes zusammenfuegen und Vertex-Spannen je Segment notieren."""
    merged = _merge_world_meshes([m for _, m in named_meshes], source_version)
    spans: List[Tuple[str, int, int]] = []
    base = 0
    for name, m in named_meshes:
        count = 0 if m.is_empty else len(m.positions)
        spans.append((name, base, base + count))
        base += count
    return merged, spans


def _scale_part_local(
    mp: Dict[str, Any],
    scale_vec: Tuple[float, float, float],
    rescale_to_size: bool = True,
) -> Tuple[MeshData, Dict[str, Tuple[Tuple[float, float, float], Matrix3]], bool]:
    """Bringt ein einzelnes RBXM-Segment samt Attachments in skalierten lokalen Stud-Raum.

    ``rescale_to_size=False``: MeshPart-Meshes NICHT auf die Size-Property
    des Parts stretchen. Fuer Accessoire-Handles ist das die Roblox-
    Semantik (Weld = exakte Attachment-Koinzidenz, Geometrie 1:1 in
    Studs); konvertierte Legacy-Handles tragen oft Platzhalter-Sizes wie
    2x2x2, die einen kleinen Hut auf das Dreifache aufblaehen wuerden.
    """
    mesh: MeshData = mp["mesh"]
    sx_s, sy_s, sz_s = scale_vec
    transparent = float(mp.get("transparency") or 0.0) >= 0.99

    if mp.get("rbxm_class") == "SpecialMesh" or not rescale_to_size:
        ms = mp.get("mesh_scale") if mp.get("rbxm_class") == "SpecialMesh" else None
        ms = ms or (1.0, 1.0, 1.0)
        mo = mp.get("mesh_offset") or (0.0, 0.0, 0.0)
        fx, fy, fz = float(ms[0]) * sx_s, float(ms[1]) * sy_s, float(ms[2]) * sz_s
        ox, oy, oz = float(mo[0]) * sx_s, float(mo[1]) * sy_s, float(mo[2]) * sz_s
        local_mesh = mesh.transform((fx, fy, fz), (ox, oy, oz))
    else:
        msize = mesh.size()
        rsize = mp.get("rbxm_size")
        if (
            rsize is not None
            and len(rsize) == 3
            and min(float(v) for v in rsize) > 1e-4
            and min(msize) > 1e-4
        ):
            bx = float(rsize[0]) / msize[0]
            by = float(rsize[1]) / msize[1]
            bz = float(rsize[2]) / msize[2]
        else:
            bx = by = bz = 1.0
        local_mesh = mesh.transform((bx * sx_s, by * sy_s, bz * sz_s))

    local_atts: Dict[str, Tuple[Tuple[float, float, float], Matrix3]] = {}
    for aname, ainfo in (mp.get("attachments") or {}).items():
        pos = ainfo.get("position")
        if pos is None:
            continue
        spos = (float(pos[0]) * sx_s, float(pos[1]) * sy_s, float(pos[2]) * sz_s)
        local_atts[aname] = (spos, _as_mat3(ainfo.get("rotation")))

    return local_mesh, local_atts, transparent


def _normalize(v: Tuple[float, float, float]) -> Tuple[float, float, float]:
    length = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) ** 0.5
    if length < 1e-12:
        return v
    return (v[0] / length, v[1] / length, v[2] / length)


def _dot(a: Tuple[float, float, float], b: Tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _face_center(mesh: MeshData, direction: Tuple[float, float, float],
                 band: float = 0.02) -> Tuple[float, float, float]:
    """Mittelpunkt der Extrem-Flaeche eines Meshes entlang ``direction``.

    Statt der Bounding-Box-Ecke (die bei schraeg skulptierten Paketen weit
    neben dem Gelenk liegt) wird der Schwerpunkt aller Vertices im obersten
    ``band``-Anteil der Ausdehnung genommen. Das ist genau die Flaeche, mit
    der ein Glied am Nachbarglied anliegt.
    """
    if mesh.is_empty:
        return (0.0, 0.0, 0.0)
    length = _dot(direction, direction) ** 0.5
    if length < 1e-9:
        direction = (0.0, 1.0, 0.0)
        length = 1.0
    d = (direction[0] / length, direction[1] / length, direction[2] / length)
    values = [_dot(p, d) for p in mesh.positions]
    hi = max(values)
    lo = min(values)
    tol = max(1e-6, abs(band) * abs(hi - lo))
    selected = [p for p, v in zip(mesh.positions, values) if v >= hi - tol]
    n = float(len(selected)) or 1.0
    return (sum(p[0] for p in selected) / n,
            sum(p[1] for p in selected) / n,
            sum(p[2] for p in selected) / n)


def _proximal_point_local(mesh: MeshData, joint_rot: Matrix3,
                          up_world: Tuple[float, float, float] = (0.0, 1.0, 0.0),
                          inward_world: Optional[Tuple[float, float, float]] = None
                          ) -> Tuple[float, float, float]:
    """Lokaler Punkt eines Segments, der an das Eltern-Gelenk gehoert.

    ``joint_rot`` ist die Welt-Orientierung des Gelenks (Eltern-Attachment).
    Ein Segment ohne eigenes RigAttachment wird so eingesetzt, dass seine
    obere Deckflaeche (im Gelenk-Frame) exakt auf dem Gelenk liegt - so
    bleibt die Naht geschlossen, statt dass eine geratene Konstante
    (frueher ``0,3 * scale``) das Glied nach unten/unten verschiebt.

    ``inward_world`` (optional): Richtung zum Koerper. Damit wird der Punkt
    zusaetzlich an die *Innenkante* des Segments geschoben - Arme sitzen
    seitlich am Torso (Schulter-Attachment an der Arm-Innenkante), nicht
    mittig ueber dem Gelenk (sonst steckt der halbe Arm im Torso).
    """
    inverse = _mat3_transpose(joint_rot)
    up_local = _mat3_vec(inverse, up_world)
    point = _face_center(mesh, up_local)
    if inward_world is None or mesh.is_empty:
        return point
    inward_local = _mat3_vec(inverse, inward_world)
    length = _dot(inward_local, inward_local) ** 0.5
    if length < 1e-9:
        return point
    direction = (inward_local[0] / length, inward_local[1] / length,
                 inward_local[2] / length)
    extreme = max(_dot(p, direction) for p in mesh.positions)
    delta = extreme - _dot(point, direction)
    return (point[0] + direction[0] * delta,
            point[1] + direction[1] * delta,
            point[2] + direction[2] * delta)


def _distal_point_local(mesh: MeshData, joint_rot: Matrix3,
                        up_world: Tuple[float, float, float] = (0.0, 1.0, 0.0)
                        ) -> Tuple[float, float, float]:
    """Gegenstueck zu :func:`_proximal_point_local` (untere Deckflaeche)."""
    up_local = _mat3_vec(_mat3_transpose(joint_rot), up_world)
    return _face_center(mesh, (-up_local[0], -up_local[1], -up_local[2]))


def _attachment_is_plausible(mesh: MeshData,
                             position: Tuple[float, float, float]) -> bool:
    """Liegt ein Attachment ueberhaupt am Mesh?

    Manche konvertierten Bundles tragen Platzhalter-Attachments weit
    ausserhalb der Geometrie. Wird so ein Wert uebernommen, dreht die Pose
    das Teil spaeter um einen Punkt, der neben dem Koerper liegt ("Drehpunkt
    verrutscht"). Solche Attachments werden verworfen und aus der Geometrie
    abgeleitet.
    """
    if mesh.is_empty:
        return False
    lo, hi = mesh.bbox()
    size = mesh.size()
    tol = max(0.05, 0.15 * max(size))
    for i in range(3):
        if position[i] < lo[i] - tol or position[i] > hi[i] + tol:
            return False
    return True


def has_rig_attachments(segments_by_part: Dict[str, List[Dict[str, Any]]], head_mp: Optional[Dict[str, Any]] = None) -> bool:
    """True, sobald mindestens ein Koerperteil oder Kopf echte RigAttachments besitzt."""
    for segs in segments_by_part.values():
        for mp in segs:
            if any(k.endswith("RigAttachment") for k in (mp.get("attachments") or {})):
                return True
    if head_mp and any(k.endswith("Attachment") for k in (head_mp.get("attachments") or {})):
        return True
    return False


def _rot_to_down(axis: Tuple[float, float, float]) -> Matrix3:
    """Rotation, die ``axis`` auf (0,-1,0) abbildet (Bind-Pose senkrecht)."""
    length = math.sqrt(sum(float(c) * float(c) for c in axis))
    if length < 1e-9:
        return IDENTITY_MAT3
    d = (axis[0] / length, axis[1] / length, axis[2] / length)
    up = (0.0, -1.0, 0.0)
    dot = sum(d[i] * up[i] for i in range(3))
    if dot > 1.0 - 1e-9:
        return IDENTITY_MAT3
    if dot < -1.0 + 1e-9:
        return ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))
    ax = (d[1] * up[2] - d[2] * up[1],
          d[2] * up[0] - d[0] * up[2],
          d[0] * up[1] - d[1] * up[0])
    norm = math.sqrt(sum(c * c for c in ax))
    ax = (ax[0] / norm, ax[1] / norm, ax[2] / norm)
    angle = math.acos(max(-1.0, min(1.0, dot)))
    c, si = math.cos(angle), math.sin(angle)
    x, y, z = ax
    return (
        (c + x * x * (1 - c), x * y * (1 - c) - z * si, x * z * (1 - c) + y * si),
        (y * x * (1 - c) + z * si, c + y * y * (1 - c), y * z * (1 - c) - x * si),
        (z * x * (1 - c) - y * si, z * y * (1 - c) + x * si, c + z * z * (1 - c)),
    )


def _classic_segment_matches(raw: Optional[Dict[str, Any]],
                             reference: Any) -> bool:
    """Ist das vorhandene Segment-Mesh genau dieses Standard-Teil?

    Nur dann darf es durch die Referenzgeometrie ersetzt werden. Ein
    ausgeruestetes Bundle (z. B. ein Korblox-Arm) hat andere Maße und bleibt
    unangetastet. Verglichen wird die Roh-Mesh-Groesse (Mannequin-Einheiten)
    mit der Groesse des Teils im offiziellen Mannequin.
    """
    if not raw:
        return True
    mesh = raw.get("mesh")
    if mesh is None or getattr(mesh, "is_empty", True):
        return True
    lo, hi = mesh.bbox()
    size = (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])
    for i in range(3):
        target = max(1e-6, float(reference[i]))
        if abs(size[i] - float(reference[i])) > 0.06 * target:
            return False
    return True


def _standard_segment_mesh(raw: Optional[MeshData], width: float, height: float,
                           depth: float, slice_name: str) -> Optional[MeshData]:
    """Skaliert ein Standard-Avatar-Mesh auf die R15-Masse eines Segments.

    Das R15-Block-Mesh kommt als Einheitskoerper (SpecialMesh-Standard), seine
    Proportionen ergeben sich erst aus der Part-Groesse. Deshalb wird es auf
    genau die Maße gezogen, die auch der Quader-Fallback haette, und bekommt
    die Kleidungs-UVs des Abschnitts (Shirt/Hose).
    """
    if raw is None or raw.is_empty:
        return None
    lo, hi = raw.bbox()
    size = (max(1e-6, hi[0] - lo[0]), max(1e-6, hi[1] - lo[1]),
            max(1e-6, hi[2] - lo[2]))
    # Die Block-Meshes haben abgerundete Enden. Stossen zwei Segmente exakt
    # aufeinander, bleiben zwischen den Rundungen Luecken (der Koerper wirkt
    # wie gestapelte Wuerfel). Deshalb wird jedes Segment etwas hoeher
    # gezogen und ueberlappt seine Nachbarn - die Naht ist dann geschlossen,
    # die Silhouette bleibt die des Standard-Koerpers.
    scaled = raw.transform(
        (width / size[0], height * R15_STANDARD_SEGMENT_OVERLAP / size[1],
         depth / size[2]))
    center = scaled.center()
    scaled = scaled.transform(1.0, (-center[0], -center[1], -center[2]))
    return apply_limb_uvs_projected(scaled, slice_name)


def assemble_r15_rig(
    segments_by_part: Dict[str, List[Dict[str, Any]]],
    head_mp: Optional[Dict[str, Any]],
    body_colors: Dict[str, Any],
    scales: Dict[str, Any],
    headless: bool = False,
    standard_meshes: Optional[Dict[str, MeshData]] = None,
    classic_body: Any = None,
) -> RiggedAvatar:
    """Setzt einen kompletten R15-Avatar ueber exakte `*RigAttachment`-Gelenke zusammen.

    ``standard_meshes`` (Segmentname -> MeshData) enthaelt die echten
    R15-Block-Meshes des Standard-Avatars. Sie ersetzen bei nicht
    ausgeruesteten Koerperteilen die Quader-Notloesung: gleiche R15-Masse und
    Gelenk-Kette, aber die gerundete Silhouette des Standard-Koerpers.

    ``classic_body`` (``mannequin.ClassicBody``) ist Roblox' offizieller
    klassischer Standard-Koerper aus ``ClassicMannequin.fbx``. Damit werden
    Arme und Beine exakt so gesetzt, wie Roblox selbst sie setzt: echte
    Teil-Meshes, Gelenke auf der Oberkante jedes Glieds, Lage relativ zum
    echten Torso des Avatars. Das ist die einzige Quelle fuer Schulter-,
    Ellbogen- und Knieposition - es gibt keine Prozentkonstanten mehr, die
    nachjustiert werden muessten.
    """
    standard_meshes = standard_meshes or {}
    sw = _clamp(scales.get("width"), 0.55, 1.60)
    sh = _clamp(scales.get("height"), 0.55, 1.60)
    sd = _clamp(scales.get("depth"), 0.55, 1.60)
    shead = _clamp(scales.get("head"), 0.55, 1.60)
    s_body = (sw, sh, sd)
    s_head = (shead, shead, shead)

    seg_lookup: Dict[str, Tuple[MeshData, Dict[str, Tuple[Tuple[float, float, float], Matrix3]], bool]] = {}
    raw_parts: Dict[str, Dict[str, Any]] = {}
    for segs in segments_by_part.values():
        for mp in segs:
            lname = (mp.get("label") or "").strip()
            if lname and lname not in seg_lookup:
                seg_lookup[lname] = _scale_part_local(mp, s_body)
                raw_parts[lname] = mp

    world_atts: Dict[str, Tuple[Tuple[float, float, float], Matrix3]] = {}
    joint_pos: Dict[str, Tuple[float, float, float]] = {}
    joint_frames: Dict[str, Matrix3] = {}
    segment_driver: Dict[str, Optional[str]] = {}
    segment_spans: Dict[str, List[Tuple[str, int, int]]] = {}
    placed_seg_mesh: Dict[str, MeshData] = {}
    placed_seg_vis: Dict[str, bool] = {}

    def register_segment(
        seg_name: str,
        local_mesh: MeshData,
        local_atts: Dict[str, Tuple[Tuple[float, float, float], Matrix3]],
        origin: Tuple[float, float, float],
        rot: Matrix3 = IDENTITY_MAT3,
        visible: bool = True,
        scale: float = 1.0,
    ) -> None:
        placed = local_mesh.rotate(rot).transform(scale, origin)
        placed_seg_mesh[seg_name] = placed
        placed_seg_vis[seg_name] = visible
        for aname, (apos, arot) in local_atts.items():
            ra = _mat3_vec(rot, apos)
            wpos = (origin[0] + scale * ra[0], origin[1] + scale * ra[1],
                    origin[2] + scale * ra[2])
            world_atts[aname] = (wpos, _mat3_mul(rot, arot))

    joint_spans: Dict[str, float] = {}
    notes: List[str] = []
    segment_info: Dict[str, Dict[str, Any]] = {}

    for _seg_name, _entry in seg_lookup.items():
        _raw = raw_parts.get(_seg_name, {})
        segment_info[_seg_name] = {
            "class": _raw.get("rbxm_class"),
            # Rohgeometrie VOR dem Strecken auf MeshPart.Size - zeigt, ob die
            # Bundle-Groesse zur Geometrie passt (Diagnose der Gelenk-Naht).
            "raw_mesh_size": ([round(float(v), 4) for v in _raw["mesh"].size()]
                              if _raw.get("mesh") is not None else None),
            "rbxm_size": ([round(float(v), 4) for v in _raw["rbxm_size"]]
                          if _raw.get("rbxm_size") else None),
            "mesh_size": [round(float(v), 4) for v in _entry[0].size()],
            "attachments": {k: [round(float(c), 4) for c in v[0]]
                            for k, v in _entry[1].items()},
        }

    def _dist3(a: Tuple[float, float, float], b: Tuple[float, float, float]) -> float:
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5

    def _attachment_is_centered(
        mesh: MeshData, pos: Tuple[float, float, float]) -> bool:
        """Klebt der Marker praktisch in der Segmentmitte (Platzhalter)?

        Ein Gelenk in der Teilmitte laesst das Kind um seine eigene Mitte
        drehen ("Unterarm dreht um den falschen Punkt"). Echte Marker liegen
        nahe der Deckflaeche - bei grossen Bundles relativ zur halben Hoehe
        gemessen, damit der Korblox-Arm (Schulter 0,38 bei 1,31 Halbhoehe)
        nicht faelschlich als Platzhalter gilt.
        """
        if mesh.is_empty:
            return True
        lo, hi = mesh.bbox()
        half = max(1e-6, (hi[1] - lo[1]) / 2.0)
        return abs(pos[1]) < 0.15 * half

    def _chain_child_attachment(
        seg_name: str,
        att_name: str,
        parent_att: Tuple[Tuple[float, float, float], Matrix3],
        face: str = "top",
        inward_world: Optional[Tuple[float, float, float]] = None,
    ) -> Tuple[Tuple[float, float, float], Matrix3]:
        """Attachment eines Kind-Segments - notfalls aus der Geometrie.

        Ein fehlendes oder unplausibles (weit ausserhalb des Meshes
        liegendes) RigAttachment wird durch die obere Deckflaeche des
        Segments im Gelenk-Frame ersetzt. Damit liegt das Segment immer
        bündig am Gelenk, statt um einen geratenen Betrag daneben zu haengen.
        """
        mesh, atts, _transparent = seg_lookup[seg_name]
        own = atts.get(att_name)
        child_rot = own[1] if own is not None else IDENTITY_MAT3
        # Authored Attachments werden uebernommen, solange sie ueberhaupt am
        # Mesh liegen. Bewusst *nicht* geprueft, ob sie "weit genug oben"
        # sitzen: Bundles wie der Korblox-Arm haben ihre Schulter bewusst
        # unterhalb der Mesh-Mitte (Schulterplatte/Stacheln darueber). Ein
        # solcher Marker ist gueltig - ihn zu verwerfen hat den Arm um
        # 1,7 Studs nach unten verschoben.
        if own is not None and _attachment_is_plausible(mesh, own[0]) \
                and not _attachment_is_centered(mesh, own[0]):
            return own
        if own is None:
            reason = "fehlt"
        elif not _attachment_is_plausible(mesh, own[0]):
            reason = "liegt ausserhalb des Meshes"
        else:
            reason = "liegt mitten im Segment (Platzhalter)"
        placed_rot = _mat3_mul(parent_att[1], _mat3_transpose(child_rot))
        notes.append(
            f"Segment '{seg_name}': Attachment '{att_name}' {reason} - "
            "Anschluss aus der Mesh-Geometrie abgeleitet (Naht bleibt zu).")
        # Orientierung beibehalten: konvertierte Legacy-Bundles haben oft
        # gedrehte Parts - mit Identitaets-Rotation wuerde das Segment
        # schief sitzen und alle Folgegelenke mitziehen.
        point = (_proximal_point_local(mesh, placed_rot, inward_world=inward_world)
                 if face == "top" else _distal_point_local(mesh, placed_rot))
        return (point, child_rot)

    def _chain_next_joint(
        seg_name: str,
        att_name: str,
        parent_att: Tuple[Tuple[float, float, float], Matrix3],
    ) -> Tuple[Tuple[float, float, float], Matrix3]:
        """Naechstes Gelenk aus dem eben platzierten Segment.

        Ist das Attachment vorhanden und liegt es wirklich *unterhalb* des
        Eltern-Gelenks, wird es uebernommen (exakte Roblox-Semantik). Fehlt
        es oder klebt es als Platzhalter mitten im Teil (Abstand < 25 % der
        Segmenthoehe), wird das Gelenk an die untere Deckflaeche gelegt -
        sonst waere z. B. der Ellbogen mitten im Unterarm (falscher Drehpunkt).
        """
        placed = placed_seg_mesh.get(seg_name)
        att = world_atts.get(att_name)
        if placed is not None:
            extent = max(placed.size()) if not placed.is_empty else 0.0
            if att is not None:
                span = _dist3(att[0], parent_att[0])
                # Welt-"oben" des Gelenk-Frames: R15-Attachments zeigen mit +Y
                # nach oben, das Glied laeuft nach -Y.
                up = _mat3_vec(parent_att[1], (0.0, 1.0, 0.0))
                projections = [_dot(p, up) for p in placed.positions]
                spread = ((max(projections) - min(projections))
                          if projections else 0.0)
                center = ((max(projections) + min(projections)) / 2.0
                          if projections else 0.0)
                # Platzhalter: klebt am Elterngelenk, liegt oberhalb der
                # Segmentmitte (dann dreht das Kind im Elternteil) oder gar
                # nicht am Segment.
                too_close = span < max(0.02, 0.10 * extent)
                above_center = _dot(att[0], up) > center - 0.05 * spread
                off_mesh = not _attachment_is_plausible(placed, att[0])
                if too_close or above_center or off_mesh:
                    reason = ("liegt ausserhalb des Segments"
                              if off_mesh and not (too_close or above_center)
                              else ("liegt mitten im Segment"
                                    if above_center and not too_close
                                    else "liegt zu nah am Elterngelenk"))
                    notes.append(
                        f"Gelenk '{att_name}' {reason} (Platzhalter-Attachment) - "
                        "an die Segment-Unterkante gesetzt.")
                    att = None
            if att is None:
                att = (_face_center(placed, (0.0, -1.0, 0.0)), parent_att[1])
                world_atts[att_name] = att
                notes.append(
                    f"Gelenk '{att_name}' aus der Geometrie von '{seg_name}' "
                    "abgeleitet (untere Deckflaeche).")
            joint_spans[att_name] = _dist3(att[0], parent_att[0])
        if att is None:
            # Kein platziertes Segment: konservativ gerade nach unten.
            att = ((parent_att[0][0], parent_att[0][1] - 0.45,
                    parent_att[0][2]), parent_att[1])
        return att

    # 1) Torso (LowerTorso -> WaistRigAttachment -> UpperTorso)
    if "LowerTorso" in seg_lookup and "UpperTorso" in seg_lookup:
        lt_m, lt_a, lt_tr = seg_lookup["LowerTorso"]
        ut_m, ut_a, ut_tr = seg_lookup["UpperTorso"]
        register_segment("LowerTorso", lt_m, lt_a, (0.0, 0.0, 0.0), IDENTITY_MAT3, not lt_tr)
        segment_driver["LowerTorso"] = None
        root_att = world_atts.get("RootRigAttachment", ((0.0, 0.0, 0.0), IDENTITY_MAT3))
        joint_pos["LowerTorso"] = root_att[0]
        joint_frames["LowerTorso"] = root_att[1]
        waist_att = world_atts.get("WaistRigAttachment", ((0.0, 0.2 * sh, 0.0), IDENTITY_MAT3))
        joint_pos["UpperTorso"] = waist_att[0]
        joint_frames["UpperTorso"] = waist_att[1]
        rot_ut, ut_origin = _attach_placement(
            waist_att,
            _chain_child_attachment("UpperTorso", "WaistRigAttachment", waist_att,
                                    face="bottom"))
        register_segment("UpperTorso", ut_m, ut_a, ut_origin, rot_ut, not ut_tr)
        segment_driver["UpperTorso"] = "UpperTorso"
        torso_named = [(n, placed_seg_mesh[n]) for n in ("UpperTorso", "LowerTorso")
                       if placed_seg_vis[n]]
        torso_mesh, torso_spans = _merge_with_spans(torso_named)
        segment_spans["torso"] = torso_spans
    else:
        # Standard-Blocky-Torso mit exakten R15-Attachments
        tw, th, td = 2.0 * sw, 2.0 * sh, 1.0 * sd
        torso_origin = (0.0, 0.8 * sh, 0.0)
        torso_box = apply_torso_box_uvs(make_box(tw, th, td, torso_origin))
        default_torso_atts: Dict[str, Tuple[Tuple[float, float, float], Matrix3]] = {
            "RootRigAttachment": ((0.0, 0.0, 0.0), IDENTITY_MAT3),
            "WaistRigAttachment": ((0.0, 0.2 * sh, 0.0), IDENTITY_MAT3),
            "LeftHipRigAttachment": ((-0.5 * sw, -0.2 * sh, 0.0), IDENTITY_MAT3),
            "RightHipRigAttachment": ((0.5 * sw, -0.2 * sh, 0.0), IDENTITY_MAT3),
            "NeckRigAttachment": ((0.0, 1.8 * sh, 0.0), IDENTITY_MAT3),
            "NeckAttachment": ((0.0, 1.8 * sh, 0.0), IDENTITY_MAT3),
            "LeftShoulderRigAttachment": ((-1.0 * sw, 1.4 * sh, 0.0), IDENTITY_MAT3),
            "RightShoulderRigAttachment": ((1.0 * sw, 1.4 * sh, 0.0), IDENTITY_MAT3),
            "LeftCollarAttachment": ((-1.0 * sw, 1.8 * sh, 0.0), IDENTITY_MAT3),
            "RightCollarAttachment": ((1.0 * sw, 1.8 * sh, 0.0), IDENTITY_MAT3),
            "BodyFrontAttachment": ((0.0, 1.0 * sh, -0.5 * sd), IDENTITY_MAT3),
            "BodyBackAttachment": ((0.0, 1.0 * sh, 0.5 * sd), IDENTITY_MAT3),
            "WaistCenterAttachment": ((0.0, 0.0, 0.0), IDENTITY_MAT3),
            "WaistFrontAttachment": ((0.0, 0.0, -0.5 * sd), IDENTITY_MAT3),
            "WaistBackAttachment": ((0.0, 0.0, 0.5 * sd), IDENTITY_MAT3),
        }
        world_atts.update(default_torso_atts)
        joint_pos["LowerTorso"] = (0.0, 0.0, 0.0)
        joint_pos["UpperTorso"] = (0.0, 0.2 * sh, 0.0)
        joint_frames["LowerTorso"] = IDENTITY_MAT3
        joint_frames["UpperTorso"] = IDENTITY_MAT3
        torso_mesh = torso_box

    body_meshes: Dict[str, MeshData] = {"torso": torso_mesh}

    # 2) Arme und Beine: exakte Motor6D-Montage ueber die RigAttachments.
    #    Roblox skaliert beim Avatar-Aufbau NICHTS: jedes Segment behaelt
    #    seine Mesh-Groesse, und die Gelenke liegen exakt auf den
    #    Attachments (kind_world = eltern_world * C0 * C1^-1). Genau so wird
    #    hier montiert. Ein frueherer Versuch, die Segmente zusaetzlich
    #    "zwischen kanonische Gelenke einzupassen", hat die Arme gestaucht
    #    und nach unten geschoben - deshalb gibt es keine Skalierung mehr.
    #    Nur fehlende bzw. echte Platzhalter-Attachments (Punkt im Nichts
    #    oder direkt auf dem Elterngelenk) werden aus der Geometrie ersetzt.
    chain_spans: Dict[str, Dict[str, Any]] = {}

    def _mount_chain(
        part_name: str,
        chain: List[Tuple[str, str]],
        root_att: Tuple[Tuple[float, float, float], Matrix3],
        inward_world: Optional[Tuple[float, float, float]],
        canonical_len: float,
        placeholder_len: float,
        scale_mode: str,
        kind: str,
        sign: float,
        align_top: Optional[Tuple[float, float, float]] = None,
    ) -> Tuple[float, float]:
        """Haengt die Segmente einer Extremitaet starr an ihren Attachments auf.

        Montage wie bei Roblox (``Kind = Eltern * C0 * C1^-1``): kein Segment
        wird gestaucht oder gestreckt, die Gelenke sitzen auf den Attachments.
        Zusaetzlich wird die *ganze* Extremitaet mit einem einzigen Faktor auf
        ihre kanonische Laenge gebracht - gleichmaessig auf allen drei Achsen,
        damit die Silhouette unangetastet bleibt.
        """
        names = [name for name, _att in chain]

        # --- 1) Offizieller Standard-Koerper (Roblox-Mannequin) -------------
        # Liegt die Referenzgeometrie vor, wird die Extremitaet genau wie im
        # Mannequin gesetzt: identische Teil-Meshes, Gelenke auf der Oberkante
        # jedes Glieds, Lage relativ zum echten Torso des Avatars. Damit ist
        # die Schulter dort, wo Roblox sie hat - ohne Schulterband, ohne
        # Laengen-Normierung, ohne Verschiebung.
        classic_placed: Optional[List[Any]] = None
        if classic_body is not None:
            try:
                import mannequin as _mannequin
                _cand = _mannequin.place_part(classic_body, part_name,
                                              torso_mesh.bbox())
            except Exception:  # pragma: no cover - Referenzdaten fehlen
                _cand = None
            _sizes = getattr(classic_body, "sizes", {}) or {}
            if _cand and len(_cand) == len(names) and all(
                _cand[i].name == names[i] for i in range(len(names))
            ) and all(
                seg.name not in seg_lookup
                or _classic_segment_matches(raw_parts.get(seg.name),
                                            _sizes.get(seg.name, seg.size))
                for seg in _cand
            ):
                classic_placed = _cand
        if classic_placed is not None:
            # Bind-Pose senkrecht: das Mannequin zeigt den Koerper in A-Pose
            # (Arme ~30 Grad ausgestellt). Roblox haengt die Glieder dagegen
            # senkrecht auf; gedreht wird um den Gelenkpunkt des Teils, und
            # zwar so, dass Gelenk -> naechstes Gelenk nach unten zeigt. Die
            # Gelenk-Abstaende stammen aus den *RigAttachments* der Teile
            # (Roblox-Rig), damit die Naht genau so bleibt wie bei Roblox.
            _slices = ("upper", "lower", "end")
            _joint = classic_placed[0].joint
            for index, seg in enumerate(classic_placed):
                _mesh, _atts, _tr = seg_lookup.get(seg.name, (seg.mesh, {}, False))
                _atts = dict(_atts or {})
                _slice = _slices[index] if index < len(_slices) else "end"
                _half = seg.size[1] / 2.0
                _prox_entry = _atts.get(chain[index][1]) if index < len(chain) else None
                _prox = (_prox_entry[0] if _prox_entry is not None
                         else (0.0, _half, 0.0))
                _dist_entry = (_atts.get(chain[index + 1][1])
                               if index + 1 < len(chain) else None)
                if _dist_entry is not None:
                    _dist = _dist_entry[0]
                elif index + 1 < len(classic_placed):
                    # Ohne Attachment: naechstes Gelenk aus dem Mannequin.
                    _nxt = classic_placed[index + 1].joint
                    _ctr = seg.center
                    _dist = (_nxt[0] - _ctr[0], _nxt[1] - _ctr[1],
                             _nxt[2] - _ctr[2])
                else:
                    _dist = (0.0, -_half, 0.0)
                _axis = (_dist[0] - _prox[0], _dist[1] - _prox[1],
                         _dist[2] - _prox[2])
                _rot = _rot_to_down(_axis)
                _len = math.sqrt(sum(c * c for c in _axis))
                # Mesh in Gelenk-Koordinaten, senkrecht ausgerichtet.
                _local = []
                for _p in seg.mesh.positions:
                    _rel = (_p[0] - _prox[0], _p[1] - _prox[1], _p[2] - _prox[2])
                    _local.append(_mat3_vec(_rot, _rel))
                _seg_mesh = apply_limb_uvs_projected(
                    MeshData(positions=_local, normals=[], uvs=[], faces=list(seg.mesh.faces),
                             source_version=seg.mesh.source_version), _slice)
                register_segment(seg.name, _seg_mesh, _atts, _joint,
                                 IDENTITY_MAT3, not _tr)
                joint_pos[seg.name] = _joint
                joint_frames[seg.name] = (_prox_entry[1] if _prox_entry is not None
                                          else IDENTITY_MAT3)
                segment_driver[seg.name] = seg.name
                # Naechstes Gelenk senkrecht unter dem aktuellen.
                _joint = (_joint[0], _joint[1] - _len, _joint[2])
            notes.append(
                f"Extremitaet '{part_name}': offizieller Standard-Koerper, "
                "senkrecht aufgehaengt (Roblox-ClassicMannequin: echte "
                "Teil-Meshes, Gelenke aus den RigAttachments).")
            _named = [(seg.name, placed_seg_mesh[seg.name])
                      for seg in classic_placed if seg.name in placed_seg_mesh]
            _merged, _spans = _merge_with_spans(_named)
            body_meshes[part_name] = _merged
            segment_spans[part_name] = _spans
            _top = classic_placed[0].joint
            _bottom = _joint
            chain_spans[part_name] = {"span": _dist3(_top, _bottom),
                                      "rescale": None, "factor": 1.0}
            return 0.0, _top[1]

        if any(name in seg_lookup for name in names):
            root = root_att[0]
            # --- Durchgang 1: starre Montage, Platzierungen mitschreiben -----
            placement: Dict[str, Tuple[MeshData, Dict[str, Any],
                                       Tuple[float, float, float], Matrix3, bool]] = {}
            chain_joints: Dict[str, Tuple[Tuple[float, float, float], Matrix3]] = {}
            parent_att = root_att
            for index, (seg_name, att_name) in enumerate(chain):
                if index > 0:
                    joint_pos[seg_name] = parent_att[0]
                    joint_frames[seg_name] = parent_att[1]
                    chain_joints[seg_name] = parent_att
                if seg_name in seg_lookup:
                    rot_s, origin_s = _attach_placement(
                        parent_att,
                        _chain_child_attachment(
                            seg_name, att_name, parent_att,
                            inward_world=inward_world if index == 0 else None))
                    mesh_s, atts_s, tr_s = seg_lookup[seg_name]
                    register_segment(seg_name, mesh_s, atts_s, origin_s, rot_s,
                                     not tr_s)
                    placement[seg_name] = (mesh_s, atts_s, origin_s, rot_s,
                                           not tr_s)
                    segment_driver[seg_name] = seg_name
                    if index + 1 < len(chain):
                        parent_att = _chain_next_joint(
                            seg_name, chain[index + 1][1], parent_att)
                elif index + 1 < len(chain):
                    previous = next((n for n, _a in reversed(chain[:index])
                                     if n in placed_seg_mesh), None)
                    if previous is not None:
                        joint_pos[chain[index + 1][0]] = _face_center(
                            placed_seg_mesh[previous], (0.0, -1.0, 0.0))
                        joint_frames.setdefault(chain[index + 1][0], parent_att[1])
                        chain_joints[chain[index + 1][0]] = (
                            joint_pos[chain[index + 1][0]],
                            joint_frames[chain[index + 1][0]])
            distal = parent_att

            # --- Faktor fuer die gesamte Extremitaet -------------------------
            span = _dist3(root, distal[0])

            def _rescale(factor: float, grund: str) -> None:
                """Skaliert die schon platzierte Extremitaet um ihr Wurzelgelenk.

                Ein Faktor fuer *alle drei Achsen* - die Silhouette bleibt,
                nur die Groesse aendert sich (kein Stauchen einzelner
                Segmente, keine Verzerrung).
                """
                notes.append(
                    f"Extremitaet '{part_name}': Gelenk-Kette {_de(span)} Studs "
                    f"auf {_de(span * factor)} Studs gebracht (Faktor "
                    f"{_de(factor)} gleichmaessig auf allen Achsen - keine "
                    f"Verzerrung; {grund}).")
                for seg_name, (mesh_s, atts_s, origin_s, rot_s,
                               vis) in list(placement.items()):
                    scaled = tuple(root[i] + factor * (origin_s[i] - root[i])
                                   for i in range(3))
                    placement[seg_name] = (mesh_s, atts_s, scaled, rot_s, vis)
                    register_segment(seg_name, mesh_s, atts_s, scaled, rot_s,
                                     vis, scale=factor)
                for seg_name, (jpos, jframe) in list(chain_joints.items()):
                    moved = tuple(root[i] + factor * (jpos[i] - root[i])
                                  for i in range(3))
                    joint_pos[seg_name] = moved
                    joint_frames[seg_name] = jframe
                    chain_joints[seg_name] = (moved, jframe)

            factor = 1.0
            if span > 1e-3 and canonical_len > 1e-3:
                ideal = canonical_len / span
                # Arme werden nur gestreckt, nie gestaucht: ein absichtlich
                # langer Bundle-Arm (Korblox Deathspeaker) bleibt lang, ein
                # zu kurzer waechst auf Huefthoehe. Beine werden gar nicht
                # auf eine Normlaenge gebracht - sie werden weiter unten
                # symmetrisch angeglichen (linkes/rechtes Bein gleich lang).
                if scale_mode == "nur_verlaengern" and ideal > 1.0 \
                        and abs(ideal - 1.0) > R15_LIMB_TOLERANCE:
                    factor = max(R15_LIMB_SCALE_MIN,
                                 min(R15_LIMB_SCALE_MAX, ideal))
            if abs(factor - 1.0) > 1e-3:
                _rescale(factor, "kanonische Armlaenge")

            # --- Schulterband: Oberkante des ersten Segments ----------------
            delta = 0.0
            top_vorher = distal[0][1]
            if align_top is not None:
                erstes = placed_seg_mesh.get(names[0])
                if erstes is not None and not erstes.is_empty:
                    top = erstes.bbox()[1][1]
                    top_vorher = top
                    if top < align_top[0] or top > align_top[1]:
                        ziel = align_top[2]
                    else:
                        ziel = top
                    delta = ziel - top
                    if abs(delta) > 1e-4:
                        # Ganze Kette starr verschieben - Gelenke, Segment-
                        # groessen und Naehte bleiben unveraendert.
                        for seg_name, (mesh_s, atts_s, origin_s, rot_s,
                                       vis) in list(placement.items()):
                            moved = (origin_s[0], origin_s[1] + delta,
                                     origin_s[2])
                            placement[seg_name] = (mesh_s, atts_s, moved,
                                                   rot_s, vis)
                            register_segment(seg_name, mesh_s, atts_s, moved,
                                             rot_s, vis, scale=factor)
                        for seg_name, (jpos, jframe) in list(chain_joints.items()):
                            moved = (jpos[0], jpos[1] + delta, jpos[2])
                            joint_pos[seg_name] = moved
                            chain_joints[seg_name] = (moved, jframe)

            named = [(n, placed_seg_mesh[n]) for n in names
                     if n in placed_seg_mesh and placed_seg_vis[n]]
            merged, spans = _merge_with_spans(named)
            body_meshes[part_name] = merged
            segment_spans[part_name] = spans
            chain_spans[part_name] = {"span": span * factor,
                                      "rescale": _rescale,
                                      "factor": factor}
            return delta, top_vorher
        # --- Kein einziges Segment vorhanden --------------------------------
        # Fruehere Fassung baute *einen* Klotz fuer die ganze Extremitaet -
        # im Render sah das aus wie ein eckiger Quader ohne Knie und ohne
        # Fuss (Bug-Report: "Beine sind einfach nur eckige Quader"). Jetzt
        # entstehen drei Quader in R15-Proportionen, exakt auf der
        # Gelenk-Kette: Ober-/Unterglied plus Hand bzw. Fuss (der Fuss steht
        # vorne ueber, wie beim klassischen Roblox-Koerper).
        root = root_att[0]
        total = max(0.30, placeholder_len)
        if kind == "leg":
            # Echte R15-Blockmasse: Oberschenkel 0,775 / Unterschenkel 0,829 /
            # Fuss 0,415 Studs (Summe 2,019 bei 2,0 Studs Torso-Hoehe) und ein
            # quadratischer Querschnitt von 1x1 Studs bei 2 Studs Torso-Breite
            # (also die halbe Torso-Breite je Bein, wie beim Standard-Avatar).
            # Die Beine sind *zwei* Koerper: eine schmale Luft zur Mitte, sonst
            # verschmelzen sie optisch zu einem Klotz.
            fracs = (0.384, 0.411, 0.205)
            lw = max(0.08, 0.46 * torso_w)
            ld = lw
            gap = 0.03 * torso_w
            lx = sign * (gap + lw / 2.0)
            foot_depth = 1.22 * ld
            foot_shift = 0.16 * ld
        else:
            # Echte R15-Blockmasse: Oberarm 0,672 / Unterarm 0,703 / Hand 0,352.
            fracs = (0.389, 0.407, 0.204)
            lw = max(0.08, 0.34 * torso_w)
            ld = max(0.08, 0.62 * torso_d)
            lx = (root[0] if abs(root[0]) > 1e-3
                  else sign * (0.5 * torso_w + 0.5 * lw))
            foot_depth = ld
            foot_shift = 0.0
        y = root[1]
        # Kleidungs-Texturen (Shirt/Hose) decken das ganze Glied ab. Jedes
        # Segment bekommt deshalb *seinen* Ausschnitt der Textur-Vorlage -
        # sonst wiederholt sich das Muster (Saum, Bluemchen) an jedem
        # Segment und das Bein bekommt Streifen.
        slices = ("upper", "lower", "end")
        for index, name in enumerate(names):
            h = total * fracs[index] if index < len(fracs) else 0.1
            is_foot = kind == "leg" and index == len(names) - 1
            depth = foot_depth if is_foot else ld
            # Fussspitze zeigt nach vorne (-Z), die Ferse bleibt hinten.
            z = root[2] - (foot_shift if is_foot else 0.0)
            slice_name = slices[index] if index < len(slices) else "end"
            box = _standard_segment_mesh(
                standard_meshes.get(name), lw, h, depth, slice_name)
            if box is None:
                box = apply_limb_box_uvs(make_box(lw, h, depth, (0.0, 0.0, 0.0)),
                                         slice_name)
            origin = (lx, y - h / 2.0, z)
            register_segment(name, box, {}, origin, IDENTITY_MAT3, True)
            segment_driver[name] = name
            joint_pos[name] = (lx, y, z)
            joint_frames[name] = IDENTITY_MAT3
            y -= h
        notes.append(
            f"Extremitaet '{part_name}': nicht ausgeruestet - Standard-Avatar "
            f"eingesetzt (R15-Blockkoerper aus {'Ober-/Unterschenkel und Fuss' if kind == 'leg' else 'Ober-/Unterarm und Hand'} "
            "in Standard-Massen, kein Klotz).")
        named = [(n, placed_seg_mesh[n]) for n in names if n in placed_seg_mesh]
        merged, spans = _merge_with_spans(named)
        body_meshes[part_name] = merged
        segment_spans[part_name] = spans
        return 0.0, root[1]

    t_lo, t_hi = torso_mesh.bbox()
    torso_w = max(1e-3, t_hi[0] - t_lo[0])
    torso_h = max(1e-3, t_hi[1] - t_lo[1])
    torso_d = max(1e-3, t_hi[2] - t_lo[2])

    # --- 2a) Schulter- und Hueftlinie an der sichtbaren Torso-Geometrie ------
    # Autor der Wahrheit sind die RigAttachments des Torsos: Roblox montiert die
    # Arme genau dort. Bei Standard-R15 liegt die Schulter 0,5 Studs unter der
    # Oberkante der 2 Studs hohen Torso-Haelfte, also bei 25 % der Hoehe.
    # Autoren-Werte in diesem Band bleiben deshalb unangetastet. Ein frueherer
    # Eingriff zog *jede* Schulter auf 4 % unter die Oberkante und haengte die
    # Arme damit sichtbar zu hoch ("Schultern sind zu hoch"). Nur klar
    # unbrauchbare Marker - oberhalb der Oberkante oder tiefer als 55 % der
    # Torso-Hoehe (dann haengen die Arme am Bauch) - werden auf die
    # R15-Schulterlinie gezogen. Die Huefte sitzt an der Unterkante.
    def _torso_band_joint(att_name: str, kind: str, sign: float) -> None:
        att = world_atts.get(att_name)
        if att is None:
            return
        x, y, z = att[0]
        if kind == "shoulder":
            # R15-Schulterlinie: 0,5 Studs unter der Oberkante bei 2 Studs
            # Torso-Hoehe = ein Viertel der Hoehe.
            kanonisch = t_hi[1] - R15_SHOULDER_FRACTION_OF_TORSO * torso_h
            zu_hoch = y > t_hi[1] + 0.02 * torso_h
            zu_tief = y < t_hi[1] - 0.55 * torso_h
            if zu_hoch or zu_tief:
                lage = ("ueber der Torso-Oberkante" if zu_hoch else
                        f"{_de(t_hi[1] - y)} Studs unter der Torso-Oberkante "
                        f"(bei {_de(torso_h)} Studs Torso-Hoehe)")
                notes.append(
                    f"Gelenk '{att_name}': Schulter lag {lage} - auf die "
                    f"R15-Schulterlinie gesetzt ({_de(R15_SHOULDER_FRACTION_OF_TORSO * 100, 0)} % "
                    "der Torso-Hoehe unter der Oberkante).")
                y = kanonisch
            # Nicht weiter innen als 40 % der halben Torso-Breite.
            min_x = 0.40 * torso_w / 2.0
            if abs(x) < min_x:
                x = sign * min_x
        else:  # hip
            if y < t_lo[1] - 0.05 * torso_h or y > t_lo[1] + 0.25 * torso_h:
                notes.append(
                    f"Gelenk '{att_name}': Huefte lag ausserhalb der unteren "
                    "Torso-Kante - an die Unterkante gesetzt.")
                y = t_lo[1]
        world_atts[att_name] = ((x, y, z), att[1])

    for _side, _sign in (("Right", 1.0), ("Left", -1.0)):
        _torso_band_joint(f"{_side}ShoulderRigAttachment", "shoulder", _sign)
        _torso_band_joint(f"{_side}HipRigAttachment", "hip", _sign)
    for part_name, side, sign in (("rechts_arm", "Right", 1.0), ("links_arm", "Left", -1.0)):
        u_name, l_name, h_name = f"{side}UpperArm", f"{side}LowerArm", f"{side}Hand"
        shoulder_att = world_atts.get(
            f"{side}ShoulderRigAttachment",
            ((sign * torso_w / 2.0, t_hi[1] - 0.2 * (t_hi[1] - t_lo[1]) / 2.0, 0.0),
             IDENTITY_MAT3))
        joint_pos[u_name] = shoulder_att[0]
        joint_frames[u_name] = shoulder_att[1]
        # Schulterband: Lage der *sichtbaren* Oberkante des Oberarms.
        # Nicht der Attachment-Wert entscheidet, sondern wo der Arm optisch
        # beginnt: sitzt er deutlich unter der Torso-Oberkante, haengen die
        # Arme am Bauch; ragt er darueber, wirken die Schultern hochgezogen.
        band = (t_hi[1] - R15_ARM_TOP_BAND[1] * torso_h,
                t_hi[1] - R15_ARM_TOP_BAND[0] * torso_h,
                t_hi[1] - R15_ARM_TOP_TARGET * torso_h)
        delta, top_vorher = _mount_chain(
            part_name,
            [(u_name, f"{side}ShoulderRigAttachment"),
             (l_name, f"{side}ElbowRigAttachment"),
             (h_name, f"{side}WristRigAttachment")],
            shoulder_att,
            (-sign, 0.0, 0.0),
            R15_ARM_LENGTH_PER_TORSO_HEIGHT * R15_WRIST_FRACTION_OF_ARM * torso_h,
            R15_ARM_LENGTH_PER_TORSO_HEIGHT * torso_h,
            "nur_verlaengern",
            "arm",
            sign,
            align_top=band)
        if abs(delta) > 1e-4:
            lage = ("ueber der Torso-Oberkante" if top_vorher > band[1] else
                    f"{_de(t_hi[1] - top_vorher)} Studs unter der "
                    "Torso-Oberkante")
            shoulder_att = ((shoulder_att[0][0], shoulder_att[0][1] + delta,
                             shoulder_att[0][2]), shoulder_att[1])
            world_atts[f"{side}ShoulderRigAttachment"] = shoulder_att
            # Schulter-Accessoires (z. B. Taschen-Katze) sitzen am
            # *ShoulderAttachment* - der wandert mit, sonst rutscht das
            # Accessoire am Verschub vorbei in die Achsel.
            accessoire = world_atts.get(f"{side}ShoulderAttachment")
            if accessoire is not None:
                world_atts[f"{side}ShoulderAttachment"] = (
                    (accessoire[0][0], accessoire[0][1] + delta,
                     accessoire[0][2]), accessoire[1])
            joint_pos[u_name] = shoulder_att[0]
            joint_frames[u_name] = shoulder_att[1]
            notes.append(
                f"Schulter '{side}': Oberkante des Oberarms lag {lage} - "
                "starr auf die Schulterlinie verschoben (Gelenke, Segment-"
                "groessen und Naehte unveraendert).")

    # 3) Beine (Hip -> Knee -> Ankle), gleiche Montage wie die Arme.
    for part_name, side, sign in (("rechts_bein", "Right", 1.0), ("links_bein", "Left", -1.0)):
        u_name, l_name, f_name = f"{side}UpperLeg", f"{side}LowerLeg", f"{side}Foot"
        hip_att = world_atts.get(
            f"{side}HipRigAttachment",
            ((sign * 0.5 * sw, t_lo[1], 0.0), IDENTITY_MAT3))
        joint_pos[u_name] = hip_att[0]
        joint_frames[u_name] = hip_att[1]
        _mount_chain(
            part_name,
            [(u_name, f"{side}HipRigAttachment"),
             (l_name, f"{side}KneeRigAttachment"),
             (f_name, f"{side}AnkleRigAttachment")],
            hip_att,
            None,
            # Nur fuer die Platzhalter-Erzeugung: Huefte bis Sohle.
            R15_ANKLE_FRACTION_OF_LEG * torso_h,
            R15_SOLE_FRACTION_OF_LEG * torso_h,
            "nie",
            "leg",
            sign)

    # 3b) Beine symmetrisch angleichen. Roblox skaliert beim Avatar-Aufbau
    #     nichts: kommen beide Beine aus demselben Paket, sind sie gleich lang
    #     und bleiben unangetastet (auch wenn ein "Woman"-Bein laenger ist als
    #     die R15-Norm). Weicht ein Bein vom anderen ab (konvertierte Bundles
    #     wie Korblox: 1,24 vs. 1,75 Studs), wuerde der Avatar auf einem Bein
    #     in der Luft stehen - dann wird das kuerzere gleichmaessig auf die
    #     Laenge des laengeren gebracht.
    beine = [("links_bein", "rechts_bein"), ("rechts_bein", "links_bein")]
    for kurz, lang in beine:
        info_kurz = chain_spans.get(kurz)
        info_lang = chain_spans.get(lang)
        if not info_kurz or not info_lang:
            continue
        if info_kurz["span"] < 1e-3 or info_lang["span"] < 1e-3:
            continue
        verhaeltnis = info_lang["span"] / info_kurz["span"]
        if verhaeltnis <= R15_LEG_SYMMETRY_TOLERANCE:
            continue
        faktor = max(R15_LIMB_SCALE_MIN, min(R15_LIMB_SCALE_MAX, verhaeltnis))
        info_kurz["rescale"](faktor, "Angleich an das andere Bein")

    # 4) Kopf (NeckRigAttachment) + dezenter Halskern
    neck_att = world_atts.get("NeckRigAttachment", ((0.0, torso_mesh.bbox()[1][1], 0.0), IDENTITY_MAT3))
    joint_pos["Head"] = neck_att[0]
    joint_frames["Head"] = neck_att[1]

    head_world: Optional[MeshData] = None
    head_r = 0.60 * shead
    if head_mp is not None:
        hm_loc, ha_loc, _htr = _scale_part_local(head_mp, s_head)
        head_att = ha_loc.get("NeckRigAttachment")
        if head_att is None or not _attachment_is_plausible(hm_loc, head_att[0]):
            # Kein brauchbares Hals-Attachment: Kopf mit seiner Unterkante
            # (Kinn/Unterseite) exakt auf das Halsgelenk setzen.
            head_att = (_distal_point_local(hm_loc, neck_att[1]), IDENTITY_MAT3)
            notes.append(
                "Kopf: Attachment 'NeckRigAttachment' fehlt oder ist unplausibel - "
                "Kopf-Unterkante auf das Halsgelenk gesetzt.")
        rot_h, h_orig = _attach_placement(neck_att, head_att)
        register_segment("Head", hm_loc, ha_loc, h_orig, rot_h, True)
        segment_driver["Head"] = "Head"
        if not headless:
            head_world = placed_seg_mesh["Head"]
            head_center = head_world.center()
            head_r = max(head_world.size()) / 2.0
        else:
            # Headless DynamicHead/Head besitzt echte Attachments im RBXM, aber
            # keine sichtbare Kopf-Geometrie: Kronen/Hoerner schweben exakt auf
            # echter Kopfhoehe ueber dem offenen Kragen wie im Spiel.
            head_center = h_orig
            head_r = 0.60 * shead
        world_atts.setdefault("HatAttachment", ((head_center[0], head_center[1] + 0.6 * shead, head_center[2]), IDENTITY_MAT3))
        world_atts.setdefault("HairAttachment", world_atts["HatAttachment"])
        world_atts.setdefault("FaceCenterAttachment", (head_center, IDENTITY_MAT3))
        world_atts.setdefault("FaceFrontAttachment", ((head_center[0], head_center[1], head_center[2] - head_r), IDENTITY_MAT3))
    elif not headless:
        head_r = 0.60 * shead
        head_center = (neck_att[0][0], neck_att[0][1] + 0.5 * shead, neck_att[0][2])
        head_world = make_sphere(head_r, 32, 22, head_center)
        world_atts["HatAttachment"] = ((head_center[0], head_center[1] + 0.6 * shead, head_center[2]), IDENTITY_MAT3)
        world_atts["HairAttachment"] = world_atts["HatAttachment"]
        world_atts["FaceCenterAttachment"] = (head_center, IDENTITY_MAT3)
        world_atts["FaceFrontAttachment"] = ((head_center[0], head_center[1], head_center[2] - head_r), IDENTITY_MAT3)
    else:
        head_center = (neck_att[0][0], neck_att[0][1] + 0.5 * shead, neck_att[0][2])
        world_atts["HatAttachment"] = ((head_center[0], head_center[1] + 0.6 * shead, head_center[2]), IDENTITY_MAT3)
        world_atts["HairAttachment"] = world_atts["HatAttachment"]
        world_atts["FaceCenterAttachment"] = (head_center, IDENTITY_MAT3)
        world_atts["FaceFrontAttachment"] = ((head_center[0], head_center[1], head_center[2] - head_r), IDENTITY_MAT3)

    # Echte R15-UpperTorsos enthalten bereits den modellierten Hals und Headless-
    # Avatare duerfen niemals einen kuenstlichen Quader-Hals am Kragen zeigen.
    if not headless and not segments_by_part.get("torso"):
        body_meshes["hals"] = make_box(
            min(0.45 * sw, torso_mesh.size()[0] * 0.32),
            0.24 * sh,
            min(0.45 * sd, torso_mesh.size()[2] * 0.42),
            (neck_att[0][0], neck_att[0][1] + 0.05 * sh, neck_att[0][2]),
        )

    # 5) Gesamtes Rig gemeinsam auf y = 0 stellen
    min_y = min(
        (m.bbox()[0][1] for m in body_meshes.values() if not m.is_empty),
        default=0.0,
    )
    shift_y = -min_y
    for k, m in list(body_meshes.items()):
        body_meshes[k] = m.transform(1.0, (0.0, shift_y, 0.0))
    # Diagnose-Meshes mitverschieben (gleiche Basis wie Gelenke/Meshes).
    for k, m in list(placed_seg_mesh.items()):
        placed_seg_mesh[k] = m.transform(1.0, (0.0, shift_y, 0.0))
    if head_world is not None:
        head_world = head_world.transform(1.0, (0.0, shift_y, 0.0))
    for k, (p, r) in list(world_atts.items()):
        world_atts[k] = ((p[0], p[1] + shift_y, p[2]), r)
    for k, p in list(joint_pos.items()):
        joint_pos[k] = (p[0], p[1] + shift_y, p[2])

    t_lo, t_hi = body_meshes["torso"].bbox()
    h_center_y = head_world.center()[1] if head_world is not None else head_center[1] + shift_y
    top_y = (
        head_world.bbox()[1][1]
        if head_world is not None
        else (body_meshes["hals"].bbox()[1][1] if "hals" in body_meshes else t_hi[1])
    )
    r_grip = world_atts.get("RightGripAttachment", ((t_hi[0] + 0.4, t_lo[1], 0.0), IDENTITY_MAT3))[0]
    waist_c = world_atts.get("WaistCenterAttachment", ((0.0, t_lo[1] + 0.2, 0.0), IDENTITY_MAT3))[0]

    rig_metrics = BodyMetrics(
        height=top_y,
        torso_width=t_hi[0] - t_lo[0],
        torso_depth=t_hi[2] - t_lo[2],
        shoulder_y=t_hi[1],
        head_center_y=h_center_y,
        head_radius=head_r,
        hand_y=r_grip[1],
        hand_x=r_grip[0],
        waist_y=waist_c[1],
        hat_base_y=(t_hi[1] + 0.08 * sh) if headless else None,
    )

    return RiggedAvatar(
        body_meshes=body_meshes,
        head_mesh=head_world,
        world_attachments=world_atts,
        joint_positions=joint_pos,
        joint_frames=joint_frames,
        segment_driver=segment_driver,
        segment_spans=segment_spans,
        metrics=rig_metrics,
        scales_body=s_body,
        scales_head=s_head,
        notes=notes,
        joint_spans=joint_spans,
        placed_segments=dict(placed_seg_mesh),
        segment_info=segment_info,
    )


def _mat3_from_forward_up(
    forward: Tuple[float, float, float],
    up: Tuple[float, float, float],
) -> Matrix3:
    """Roblox CFrame.new(pos, lookAt, up)-Konvention: -Z zeigt nach vorn.

    Legacy-Hüte (Klasse Hat) mounten über AttachmentForward/AttachmentUp.
    Default (0,0,-1)/(0,1,0) ergibt die Identitaet - die klassische
    unverdrehte Hut-Ausrichtung.
    """
    import math as _math

    def _norm(v: Tuple[float, float, float]) -> Tuple[float, float, float]:
        n = _math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
        return (v[0] / n, v[1] / n, v[2] / n)

    z = _norm((-forward[0], -forward[1], -forward[2]))  # -Z = Blickrichtung
    up_n = _norm(up)
    # X = up x z (rechts), danach Y = z x x fuer ein echtes Orthogonalsystem.
    x = (up_n[1] * z[2] - up_n[2] * z[1],
         up_n[2] * z[0] - up_n[0] * z[2],
         up_n[0] * z[1] - up_n[1] * z[0])
    x = _norm(x)
    y = (z[1] * x[2] - z[2] * x[1],
         z[2] * x[0] - z[0] * x[2],
         z[0] * x[1] - z[1] * x[0])
    y = _norm(y)
    # Matrix3-Konvention hier: Zeilen; die Achsen stehen in den SPALTEN
    # (wie bei Roblox CFrame-Rotationen aus dem RBXM).
    return (
        (x[0], y[0], z[0]),
        (x[1], y[1], z[1]),
        (x[2], y[2], z[2]),
    )


# Legacy-Hut-Arten: am Kopf befestigt -> klassischer Ankerpunkt Kopf-Top.
_LEGACY_HEAD_ANCHORS: Dict[int, str] = {
    8: "HatAttachment",     # Hut
    41: "HairAttachment",   # Haare
}


def weld_accessory(
    mp: Dict[str, Any],
    rig: RiggedAvatar,
) -> Optional[Tuple[MeshData, str]]:
    """Verschweisst ein Accessoire ueber sein RBXM-Attachment exakt mit dem Koerper-Rig."""
    acc_atts = mp.get("attachments") or {}
    matched_name: Optional[str] = None
    for cand in acc_atts:
        if cand in rig.world_attachments:
            matched_name = cand
            break

    legacy = False
    hat_mount = None
    if matched_name is None:
        # Legacy-Hut (Klasse Hat, Accessoire-Aera davor): das Handle hat
        # KEIN Attachment-Kind. Roblox schweisst solche Hüte klassisch mit
        # C0 = CFrame.new(0, headSize.Y/2, 0) am Kopf und C1 aus den
        # Hat-Properties AttachmentPos/AttachmentForward/AttachmentUp.
        # Das entspricht einem virtuellen Attachment-Paar:
        #   Kopf:  HatAttachment (= Kopf-Mitte + 0,5 * Kopfhoehe)
        #   Hut:   AttachmentPos/Forward/Up des Hat-Objekts
        # Kein Zentrieren, keine Zielgrossen-Streckung - die Mesh-Vertices
        # sind relativ zum Anbringpunkt authoriert.
        hat_mount = mp.get("hat_mount")
        anchor_name = _LEGACY_HEAD_ANCHORS.get(int(mp.get("type_id") or 0))
        is_legacy_handle = mp.get("rbxm_class") in (
            "SpecialMesh", "BlockMesh", "CylinderMesh", "FileMesh", "Part")
        if (anchor_name is not None and is_legacy_handle
                and anchor_name in rig.world_attachments):
            matched_name = anchor_name
            legacy = True
    if matched_name is None:
        return None

    if matched_name in HEAD_ATTACHMENT_NAMES:
        scale_vec = rig.scales_head
    else:
        sh = rig.scales_body[1]
        scale_vec = (sh, sh, sh)
    # Roblox-Render-Semantik am Accessoire: SpecialMesh/FileMesh-Geometrie
    # wird nur mit Mesh.Scale multipliziert (skaliert NICHT auf die
    # Part-Groesse), MeshPart-Geometrie dagegen skaliert auf die
    # Size-Property. (Roblox-Doku: "The mesh of a MeshPart scales on all
    # axis depending on the Size property of the MeshPart, the mesh of a
    # SpecialMesh does not.")
    is_legacy_mesh = mp.get("rbxm_class") in (
        "SpecialMesh", "BlockMesh", "CylinderMesh", "FileMesh")
    local_mesh, local_atts, _ = _scale_part_local(
        mp, scale_vec, rescale_to_size=not is_legacy_mesh)
    if legacy:
        hm = hat_mount or {}
        p_acc = hm.get("pos") or (0.0, 0.0, 0.0)
        r_acc = _mat3_from_forward_up(
            hm.get("forward") or (0.0, 0.0, -1.0),
            hm.get("up") or (0.0, 1.0, 0.0),
        )
    else:
        p_acc, r_acc = local_atts[matched_name]
    p_body, r_body = rig.world_attachments[matched_name]

    r_handle = _mat3_mul(r_body, _mat3_transpose(r_acc))
    rot_p_acc = _mat3_vec(r_handle, p_acc)
    p_handle = (
        p_body[0] - rot_p_acc[0],
        p_body[1] - rot_p_acc[1],
        p_body[2] - rot_p_acc[2],
    )
    placed = local_mesh.rotate(r_handle).transform(1.0, p_handle)
    layout = ACCESSORY_LAYOUT.get(int(mp.get("type_id") or 0), {"label": "Accessoire"})
    sz = placed.size()
    if legacy:
        mode = ("Legacy-Weld (Klasse Hat, keine Attachments: Anker "
                f"'{matched_name}' + AttachmentPos"
                + ("" if hat_mount else " (Standard 0,0,0)")
                + ", Originalgroesse erhalten)")
    else:
        mode = f"exakt per '{matched_name}' verschweisst"
    note = (
        f"{layout['label']}: {mode} "
        f"({_de(sz[0], 2)}x{_de(sz[1], 2)}x{_de(sz[2], 2)} Studs)"
    )
    return placed, note

