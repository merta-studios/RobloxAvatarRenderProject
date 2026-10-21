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
    # Linker/rechter Arm bzw. linkes/rechtes Bein sind beim Rig gespiegelt:
    # haben BEIDE Seiten ein echtes Mesh, wird die Breite/Tiefe angeglichen
    # (symmetrischer Avatar). Fehlt eine Seite, bleibt die Fallback-Box
    # unangetastet - sonst wuerde ein einzelner Mesh-Arm die Breite der
    # Gegenseite erben.
    def mirror(index: int, names) -> None:
        present = [n for n in names if n in mesh_sizes]
        if len(present) < 2:
            return
        widest = max(sizes[n][index] for n in present)
        for name in present:
            size = list(sizes[name])
            size[index] = widest
            sizes[name] = tuple(size)  # type: ignore[assignment]

    # Alle drei Achsen spiegeln: die linken und rechten Teile kommen oft aus
    # unterschiedlichen Bundles (z. B. Korblox-Arm rechts, Standard-Arm links).
    # Ohne Angleichung war ein Arm 4,0 Studs lang und der andere 3,0 - die
    # ueberzaehligen Segmente hingen sichtbar in der Luft.
    for index in (0, 1, 2):
        mirror(index, ARM_PARTS)
        mirror(index, LEG_PARTS)

    leg_w = max(sizes["rechts_bein"][0], sizes["links_bein"][0])
    arm_w = max(sizes["rechts_arm"][0], sizes["links_arm"][0])

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
    # Beine (untere Kante auf y = 0)
    for name, sign in (("rechts_bein", 1.0), ("links_bein", -1.0)):
        sx, sy, sz = sizes[name]
        parts.append(Part(name, make_box(sx, sy, sz,
                                        (sign * (leg_w / 2 + gap * 0.5), sy / 2, 0.0)),
                          color(name), "body"))
    # Torso
    parts.append(Part("torso", make_box(torso_w, torso_h, torso_d,
                                       (0.0, torso_bottom + torso_h / 2, 0.0)),
                      color("torso"), "body"))
    # Arme: Oberkante auf Schulterhoehe (Torso-Oberkante)
    arm_offset = torso_w / 2 + arm_w / 2 + gap
    for name, sign in (("rechts_arm", 1.0), ("links_arm", -1.0)):
        sx, sy, sz = sizes[name]
        center_y = torso_top - sy / 2
        parts.append(Part(name, make_box(sx, sy, sz, (sign * arm_offset, center_y, 0.0)),
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
        hand_x=arm_offset,
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
