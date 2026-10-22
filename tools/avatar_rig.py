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

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from avatar_uv import apply_limb_box_uvs, apply_torso_box_uvs
from roblox_mesh import MeshData, make_box, make_sphere, rotation_between
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
    """Welt-Raum-Ergebnis des Attachment-basierten R15-Rigs."""

    body_meshes: Dict[str, MeshData] = field(default_factory=dict)
    head_mesh: Optional[MeshData] = None
    world_attachments: Dict[str, Tuple[Tuple[float, float, float], Matrix3]] = field(default_factory=dict)
    joint_positions: Dict[str, Tuple[float, float, float]] = field(default_factory=dict)
    metrics: BodyMetrics = field(default_factory=BodyMetrics)
    scales_body: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    scales_head: Tuple[float, float, float] = (1.0, 1.0, 1.0)


def _scale_part_local(
    mp: Dict[str, Any],
    scale_vec: Tuple[float, float, float],
) -> Tuple[MeshData, Dict[str, Tuple[Tuple[float, float, float], Matrix3]], bool]:
    """Bringt ein einzelnes RBXM-Segment samt Attachments in skalierten lokalen Stud-Raum."""
    mesh: MeshData = mp["mesh"]
    sx_s, sy_s, sz_s = scale_vec
    transparent = float(mp.get("transparency") or 0.0) >= 0.99

    if mp.get("rbxm_class") == "SpecialMesh":
        ms = mp.get("mesh_scale") or (1.0, 1.0, 1.0)
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


def has_rig_attachments(segments_by_part: Dict[str, List[Dict[str, Any]]], head_mp: Optional[Dict[str, Any]] = None) -> bool:
    """True, sobald mindestens ein Koerperteil oder Kopf echte RigAttachments besitzt."""
    for segs in segments_by_part.values():
        for mp in segs:
            if any(k.endswith("RigAttachment") for k in (mp.get("attachments") or {})):
                return True
    if head_mp and any(k.endswith("Attachment") for k in (head_mp.get("attachments") or {})):
        return True
    return False


def assemble_r15_rig(
    segments_by_part: Dict[str, List[Dict[str, Any]]],
    head_mp: Optional[Dict[str, Any]],
    body_colors: Dict[str, Any],
    scales: Dict[str, Any],
    headless: bool = False,
) -> RiggedAvatar:
    """Setzt einen kompletten R15-Avatar ueber exakte `*RigAttachment`-Gelenke zusammen."""
    sw = _clamp(scales.get("width"), 0.55, 1.60)
    sh = _clamp(scales.get("height"), 0.55, 1.60)
    sd = _clamp(scales.get("depth"), 0.55, 1.60)
    shead = _clamp(scales.get("head"), 0.55, 1.60)
    s_body = (sw, sh, sd)
    s_head = (shead, shead, shead)

    seg_lookup: Dict[str, Tuple[MeshData, Dict[str, Tuple[Tuple[float, float, float], Matrix3]], bool]] = {}
    for segs in segments_by_part.values():
        for mp in segs:
            lname = (mp.get("label") or "").strip()
            if lname and lname not in seg_lookup:
                seg_lookup[lname] = _scale_part_local(mp, s_body)

    world_atts: Dict[str, Tuple[Tuple[float, float, float], Matrix3]] = {}
    joint_pos: Dict[str, Tuple[float, float, float]] = {}
    placed_seg_mesh: Dict[str, MeshData] = {}
    placed_seg_vis: Dict[str, bool] = {}

    def register_segment(
        seg_name: str,
        local_mesh: MeshData,
        local_atts: Dict[str, Tuple[Tuple[float, float, float], Matrix3]],
        origin: Tuple[float, float, float],
        rot: Matrix3 = IDENTITY_MAT3,
        visible: bool = True,
    ) -> None:
        placed = local_mesh.rotate(rot).transform(1.0, origin)
        placed_seg_mesh[seg_name] = placed
        placed_seg_vis[seg_name] = visible
        for aname, (apos, arot) in local_atts.items():
            ra = _mat3_vec(rot, apos)
            wpos = (origin[0] + ra[0], origin[1] + ra[1], origin[2] + ra[2])
            world_atts[aname] = (wpos, _mat3_mul(rot, arot))

    # 1) Torso (LowerTorso -> WaistRigAttachment -> UpperTorso)
    if "LowerTorso" in seg_lookup and "UpperTorso" in seg_lookup:
        lt_m, lt_a, lt_tr = seg_lookup["LowerTorso"]
        ut_m, ut_a, ut_tr = seg_lookup["UpperTorso"]
        register_segment("LowerTorso", lt_m, lt_a, (0.0, 0.0, 0.0), IDENTITY_MAT3, not lt_tr)
        joint_pos["LowerTorso"] = world_atts.get("RootRigAttachment", ((0.0, 0.0, 0.0), IDENTITY_MAT3))[0]
        waist_world = world_atts.get("WaistRigAttachment", ((0.0, 0.2 * sh, 0.0), IDENTITY_MAT3))[0]
        ut_waist = ut_a.get("WaistRigAttachment", ((0.0, -0.8 * sh, 0.0), IDENTITY_MAT3))[0]
        ut_origin = (waist_world[0] - ut_waist[0], waist_world[1] - ut_waist[1], waist_world[2] - ut_waist[2])
        register_segment("UpperTorso", ut_m, ut_a, ut_origin, IDENTITY_MAT3, not ut_tr)
        joint_pos["UpperTorso"] = waist_world
        torso_mesh = _merge_world_meshes(
            [m for n, m in (("UpperTorso", placed_seg_mesh["UpperTorso"]), ("LowerTorso", placed_seg_mesh["LowerTorso"])) if placed_seg_vis[n]]
        )
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
        torso_mesh = torso_box

    body_meshes: Dict[str, MeshData] = {"torso": torso_mesh}

    # 2) Arme (Shoulder -> Elbow -> Wrist; A-Pose wird auf natuerliches Herabhaengen rotiert)
    for part_name, side, sign in (("rechts_arm", "Right", 1.0), ("links_arm", "Left", -1.0)):
        u_name, l_name, h_name = f"{side}UpperArm", f"{side}LowerArm", f"{side}Hand"
        sh_att_name = f"{side}ShoulderRigAttachment"
        el_att_name = f"{side}ElbowRigAttachment"
        wr_att_name = f"{side}WristRigAttachment"
        shoulder_w = world_atts.get(sh_att_name, ((sign * 1.0 * sw, 1.4 * sh, 0.0), IDENTITY_MAT3))[0]
        joint_pos[u_name] = shoulder_w

        # Schulter leicht nach außen schieben, damit Arm-Volumen nicht in Torso schneidet
        # (echte UpperTorso-Meshes haben Schulter bei exakt Torso-Rand; das Arm-Mesh
        # ragt sonst 0.5 Studs nach innen in den Torso - „schief in den Körper“)
        shoulder_w = (shoulder_w[0] + sign * 0.060, shoulder_w[1], shoulder_w[2])

        if u_name in seg_lookup and l_name in seg_lookup and h_name in seg_lookup:
            um, ua, utr = seg_lookup[u_name]
            lm, la, ltr = seg_lookup[l_name]
            hm, ha, htr = seg_lookup[h_name]

            # Senkrecht leicht nach außen (30° statt 20°) - vorher 20° ließ Innenkante noch im Torso
            d_upper_target = (sign * 0.5000, -0.8660, 0.0)
            d_lower_target = (sign * 0.3420, -0.9397, -0.02)

            rot_u = IDENTITY_MAT3
            if sh_att_name in ua and el_att_name in ua:
                du = (
                    ua[el_att_name][0][0] - ua[sh_att_name][0][0],
                    ua[el_att_name][0][1] - ua[sh_att_name][0][1],
                    ua[el_att_name][0][2] - ua[sh_att_name][0][2],
                )
                len_u = (du[0] ** 2 + du[1] ** 2 + du[2] ** 2) ** 0.5
                horiz_u = (du[0] ** 2 + du[2] ** 2) ** 0.5
                if len_u > 0.15 and horiz_u > 0.28 * len_u:
                    rot_u = rotation_between(du, d_upper_target)

            u_sh_r = _mat3_vec(rot_u, ua.get(sh_att_name, ((0.0, 0.3 * sh, 0.0), IDENTITY_MAT3))[0])
            u_orig = (shoulder_w[0] - u_sh_r[0], shoulder_w[1] - u_sh_r[1], shoulder_w[2] - u_sh_r[2])
            register_segment(u_name, um, ua, u_orig, rot_u, not utr)

            elbow_w = world_atts.get(el_att_name, (shoulder_w[0], shoulder_w[1] - 0.6 * sh, shoulder_w[2]))[0]
            joint_pos[l_name] = elbow_w

            rot_l = rot_u
            if el_att_name in la and wr_att_name in la:
                dl = (
                    la[wr_att_name][0][0] - la[el_att_name][0][0],
                    la[wr_att_name][0][1] - la[el_att_name][0][1],
                    la[wr_att_name][0][2] - la[el_att_name][0][2],
                )
                len_l = (dl[0] ** 2 + dl[1] ** 2 + dl[2] ** 2) ** 0.5
                horiz_l = (dl[0] ** 2 + dl[2] ** 2) ** 0.5
                if len_l > 0.15 and horiz_l > 0.28 * len_l:
                    rot_l = rotation_between(dl, d_lower_target)

            l_el_r = _mat3_vec(rot_l, la.get(el_att_name, ((0.0, 0.35 * sh, 0.0), IDENTITY_MAT3))[0])
            l_orig = (elbow_w[0] - l_el_r[0], elbow_w[1] - l_el_r[1], elbow_w[2] - l_el_r[2])
            register_segment(l_name, lm, la, l_orig, rot_l, not ltr)

            wrist_w = world_atts.get(wr_att_name, (elbow_w[0], elbow_w[1] - 0.7 * sh, elbow_w[2]))[0]
            joint_pos[h_name] = wrist_w

            h_wr_r = _mat3_vec(rot_l, ha.get(wr_att_name, ((0.0, 0.15 * sh, 0.0), IDENTITY_MAT3))[0])
            h_orig = (wrist_w[0] - h_wr_r[0], wrist_w[1] - h_wr_r[1], wrist_w[2] - h_wr_r[2])
            register_segment(h_name, hm, ha, h_orig, rot_l, not htr)

            vis_arm = [placed_seg_mesh[n] for n in (u_name, l_name, h_name) if placed_seg_vis[n]]
            body_meshes[part_name] = _merge_world_meshes(vis_arm)
        else:
            aw, ah, ad = 1.0 * sw, 2.0 * sh, 1.0 * sd
            torso_half_w = torso_mesh.size()[0] / 2.0
            cx = sign * (max(abs(shoulder_w[0]), torso_half_w) + aw / 2.0 + 0.055)
            cy = shoulder_w[1] + 0.4 * sh - ah / 2.0
            cz = shoulder_w[2]
            body_meshes[part_name] = apply_limb_box_uvs(make_box(aw, ah, ad, (cx, cy, cz)))
            joint_pos[u_name] = (cx, cy + 0.6 * sh, cz)
            joint_pos[l_name] = (cx, cy - 0.1 * sh, cz)
            joint_pos[h_name] = (cx, cy - 0.75 * sh, cz)
            world_atts.setdefault(f"{side}ShoulderAttachment", ((cx, cy + ah / 2.0, cz), IDENTITY_MAT3))
            world_atts.setdefault(f"{side}GripAttachment", ((cx, cy - ah / 2.0 + 0.1 * sh, cz), IDENTITY_MAT3))

    # 3) Beine (Hip -> Knee -> Ankle)
    for part_name, side, sign in (("rechts_bein", "Right", 1.0), ("links_bein", "Left", -1.0)):
        u_name, l_name, f_name = f"{side}UpperLeg", f"{side}LowerLeg", f"{side}Foot"
        hip_att_name = f"{side}HipRigAttachment"
        knee_att_name = f"{side}KneeRigAttachment"
        ankle_att_name = f"{side}AnkleRigAttachment"
        hip_w = world_atts.get(hip_att_name, ((sign * 0.5 * sw, -0.2 * sh, 0.0), IDENTITY_MAT3))[0]
        joint_pos[u_name] = hip_w

        if u_name in seg_lookup and l_name in seg_lookup and f_name in seg_lookup:
            um, ua, utr = seg_lookup[u_name]
            lm, la, ltr = seg_lookup[l_name]
            fm, fa, ftr = seg_lookup[f_name]

            rot_u = IDENTITY_MAT3
            if hip_att_name in ua and knee_att_name in ua:
                du = (
                    ua[knee_att_name][0][0] - ua[hip_att_name][0][0],
                    ua[knee_att_name][0][1] - ua[hip_att_name][0][1],
                    ua[knee_att_name][0][2] - ua[hip_att_name][0][2],
                )
                if du[1] < -0.2 and (du[0] ** 2 + du[2] ** 2) > 0.05 * (du[1] ** 2):
                    rot_u = rotation_between(du, (0.0, -1.0, 0.0))

            u_hip_r = _mat3_vec(rot_u, ua.get(hip_att_name, ((0.0, 0.4 * sh, 0.0), IDENTITY_MAT3))[0])
            u_orig = (hip_w[0] - u_hip_r[0], hip_w[1] - u_hip_r[1], hip_w[2] - u_hip_r[2])
            register_segment(u_name, um, ua, u_orig, rot_u, not utr)

            knee_w = world_atts.get(knee_att_name, (hip_w[0], hip_w[1] - 0.8 * sh, hip_w[2]))[0]
            joint_pos[l_name] = knee_w

            rot_l = IDENTITY_MAT3
            if knee_att_name in la and ankle_att_name in la:
                dl = (
                    la[ankle_att_name][0][0] - la[knee_att_name][0][0],
                    la[ankle_att_name][0][1] - la[knee_att_name][0][1],
                    la[ankle_att_name][0][2] - la[knee_att_name][0][2],
                )
                if dl[1] < -0.2 and (dl[0] ** 2 + dl[2] ** 2) > 0.05 * (dl[1] ** 2):
                    rot_l = rotation_between(dl, (0.0, -1.0, 0.0))

            l_kn_r = _mat3_vec(rot_l, la.get(knee_att_name, ((0.0, 0.4 * sh, 0.0), IDENTITY_MAT3))[0])
            l_orig = (knee_w[0] - l_kn_r[0], knee_w[1] - l_kn_r[1], knee_w[2] - l_kn_r[2])
            register_segment(l_name, lm, la, l_orig, rot_l, not ltr)

            ankle_w = world_atts.get(ankle_att_name, (knee_w[0], knee_w[1] - 0.8 * sh, knee_w[2]))[0]
            joint_pos[f_name] = ankle_w

            f_an_r = fa.get(ankle_att_name, ((0.0, 0.1 * sh, 0.0), IDENTITY_MAT3))[0]
            f_orig = (ankle_w[0] - f_an_r[0], ankle_w[1] - f_an_r[1], ankle_w[2] - f_an_r[2])
            register_segment(f_name, fm, fa, f_orig, IDENTITY_MAT3, not ftr)

            vis_leg = [placed_seg_mesh[n] for n in (u_name, l_name, f_name) if placed_seg_vis[n]]
            body_meshes[part_name] = _merge_world_meshes(vis_leg)
        else:
            lw, lh, ld = 1.0 * sw, 2.0 * sh, 1.0 * sd
            cx = sign * max(abs(hip_w[0]), lw / 2.0 + 0.006)
            cy = hip_w[1] - lh / 2.0
            cz = hip_w[2]
            body_meshes[part_name] = apply_limb_box_uvs(make_box(lw, lh, ld, (cx, cy, cz)))
            joint_pos[u_name] = (cx, hip_w[1], cz)
            joint_pos[l_name] = (cx, hip_w[1] - 0.85 * sh, cz)
            joint_pos[f_name] = (cx, hip_w[1] - 1.75 * sh, cz)

    # 4) Kopf (NeckRigAttachment) + dezenter Halskern
    neck_w = world_atts.get("NeckRigAttachment", ((0.0, torso_mesh.bbox()[1][1], 0.0), IDENTITY_MAT3))[0]
    joint_pos["Head"] = neck_w

    head_world: Optional[MeshData] = None
    head_r = 0.60 * shead
    if head_mp is not None:
        hm_loc, ha_loc, _htr = _scale_part_local(head_mp, s_head)
        h_neck = ha_loc.get("NeckRigAttachment", ((0.0, -0.5 * shead, 0.0), IDENTITY_MAT3))[0]
        h_orig = (neck_w[0] - h_neck[0], neck_w[1] - h_neck[1], neck_w[2] - h_neck[2])
        register_segment("Head", hm_loc, ha_loc, h_orig, IDENTITY_MAT3, True)
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
        head_center = (neck_w[0], neck_w[1] + 0.5 * shead, neck_w[2])
        head_world = make_sphere(head_r, 32, 22, head_center)
        world_atts["HatAttachment"] = ((head_center[0], head_center[1] + 0.6 * shead, head_center[2]), IDENTITY_MAT3)
        world_atts["HairAttachment"] = world_atts["HatAttachment"]
        world_atts["FaceCenterAttachment"] = (head_center, IDENTITY_MAT3)
        world_atts["FaceFrontAttachment"] = ((head_center[0], head_center[1], head_center[2] - head_r), IDENTITY_MAT3)
    else:
        head_center = (neck_w[0], neck_w[1] + 0.5 * shead, neck_w[2])
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
            (neck_w[0], neck_w[1] + 0.05 * sh, neck_w[2]),
        )

    # 5) Gesamtes Rig gemeinsam auf y = 0 stellen
    min_y = min(
        (m.bbox()[0][1] for m in body_meshes.values() if not m.is_empty),
        default=0.0,
    )
    shift_y = -min_y
    for k, m in list(body_meshes.items()):
        body_meshes[k] = m.transform(1.0, (0.0, shift_y, 0.0))
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
        metrics=rig_metrics,
        scales_body=s_body,
        scales_head=s_head,
    )


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
    if matched_name is None:
        return None

    if matched_name in HEAD_ATTACHMENT_NAMES:
        scale_vec = rig.scales_head
    else:
        sh = rig.scales_body[1]
        scale_vec = (sh, sh, sh)
    local_mesh, local_atts, _ = _scale_part_local(mp, scale_vec)
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
    note = (
        f"{layout['label']}: exakt per '{matched_name}' verschweisst "
        f"({_de(sz[0], 2)}x{_de(sz[1], 2)}x{_de(sz[2], 2)} Studs)"
    )
    return placed, note

