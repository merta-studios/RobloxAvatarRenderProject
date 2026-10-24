#!/usr/bin/env python3
"""Posen fuer den Avatar-Render: laden, pruefen und auf das Rig anwenden.

Die Posen stehen in ``Posen.toml``. Jede Pose beschreibt in sprechenden,
*relativen* Delta-Werten, wie sich ein Gelenk gegenueber der Stand-Pose
veraendert (z. B. ``vor_hoch_schwingen_grad`` am Oberarm statt einer
rohen ``rotation = [x, y, z]``), plus eigene Kamera-/Ausschnitt-Werte
(``sichtbar_oben_prozent``, ``kamera_abstand_studs``, ``avatar_hoch_studs``).

* Laden/Validieren der Posen (``load_poses``)
* Gelenkpunkte: exakte ``*RigAttachment``-Positionen haben Vorrang; fehlende
  Gelenke werden aus den Nachbar-Gelenken der Kette abgeleitet (nie geraten
  mitten ins Teil), damit jede Drehung um das echte Gelenk (Pivot) laeuft.
* FK-Kette: Eltern-Drehungen wirken auf alle Kinder wie bei einem Skelett.
* Mesh-Verformung: gewichtete Quaternionen-Ueberblendung (statt Positionen
  mehrerer starrer Transformationen zu mitteln) - das Volumen bleibt an
  Ellbogen/Knie/Hals erhalten und die Uebergaenge bleiben nahtlos.
  Accessoires folgen starr ihrem Knochen (Hut -> Kopf, ...).

Koordinaten (Roblox): X = rechts, Y = hoch, Z = hinten (Gesicht: -Z).
"""

from __future__ import annotations

import math
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # direkter Import beim Aufruf aus tools/
    from roblox_mesh import MeshData
except ImportError:  # pragma: no cover - Aufruf von ausserhalb
    from tools.roblox_mesh import MeshData  # type: ignore

Point = Tuple[float, float, float]
Vec3 = Point
Mat3 = Tuple[Vec3, Vec3, Vec3]
Mat4 = Tuple[Tuple[float, float, float, float], ...]

# ---------------------------------------------------------------------------
# Koerperteile und Knochen-Hierarchie
# ---------------------------------------------------------------------------

#: Alle Koerperteile, die in Posen.toml angesprochen werden koennen.
BODY_PART_KEYS: Tuple[str, ...] = (
    "oberer_torso",
    "unterer_torso",
    "linker_oberarm",
    "linker_unterarm",
    "linke_hand",
    "rechter_oberarm",
    "rechter_unterarm",
    "rechte_hand",
    "kopf",
    "linkes_oberbein",
    "linkes_unterbein",
    "linker_fuss",
    "rechtes_oberbein",
    "rechtes_unterbein",
    "rechter_fuss",
)

#: Knochen -> (Eltern-Knochen, Gelenkname im Rig).
BONE_PARENT: Dict[str, Optional[str]] = {
    "unterer_torso": None,
    "oberer_torso": "unterer_torso",
    "kopf": "oberer_torso",
    "linker_oberarm": "oberer_torso",
    "linker_unterarm": "linker_oberarm",
    "linke_hand": "linker_unterarm",
    "rechter_oberarm": "oberer_torso",
    "rechter_unterarm": "rechter_oberarm",
    "rechte_hand": "rechter_unterarm",
    "linkes_oberbein": "unterer_torso",
    "linkes_unterbein": "linkes_oberbein",
    "linker_fuss": "linkes_unterbein",
    "rechtes_oberbein": "unterer_torso",
    "rechtes_unterbein": "rechtes_oberbein",
    "rechter_fuss": "rechtes_unterbein",
}

#: Gelenkname, den ``avatar_rig.assemble_r15_rig`` in ``joint_positions`` nutzt.
BONE_JOINT: Dict[str, str] = {
    "unterer_torso": "LowerTorso",
    "oberer_torso": "UpperTorso",
    "kopf": "Head",
    "linker_oberarm": "LeftUpperArm",
    "linker_unterarm": "LeftLowerArm",
    "linke_hand": "LeftHand",
    "rechter_oberarm": "RightUpperArm",
    "rechter_unterarm": "RightLowerArm",
    "rechte_hand": "RightHand",
    "linkes_oberbein": "LeftUpperLeg",
    "linkes_unterbein": "LeftLowerLeg",
    "linker_fuss": "LeftFoot",
    "rechtes_oberbein": "RightUpperLeg",
    "rechtes_unterbein": "RightLowerLeg",
    "rechter_fuss": "RightFoot",
}

#: Umkehrung: R15-Gelenk-/Segmentname -> Knochen (starr je Segment anwenden).
JOINT_BONE: Dict[str, str] = {joint: bone for bone, joint in BONE_JOINT.items()}

#: Eltern zuerst (topologische Reihenfolge fuer die Matrizen).
BONE_ORDER: Tuple[str, ...] = (
    "unterer_torso",
    "oberer_torso",
    "kopf",
    "linker_oberarm",
    "linker_unterarm",
    "linke_hand",
    "rechter_oberarm",
    "rechter_unterarm",
    "rechte_hand",
    "linkes_oberbein",
    "linkes_unterbein",
    "linker_fuss",
    "rechtes_oberbein",
    "rechtes_unterbein",
    "rechter_fuss",
)

#: Rig-Teilname (avatar_rig/build_avatar) -> Kette aus Knochen.
PART_CHAINS: Dict[str, Tuple[str, ...]] = {
    "torso": ("unterer_torso", "oberer_torso"),
    "hals": ("oberer_torso", "kopf"),
    "kopf": ("kopf",),
    "kopf_mesh": ("kopf",),
    "links_arm": ("linker_oberarm", "linker_unterarm", "linke_hand"),
    "rechts_arm": ("rechter_oberarm", "rechter_unterarm", "rechte_hand"),
    "links_bein": ("linkes_oberbein", "linkes_unterbein", "linker_fuss"),
    "rechts_bein": ("rechtes_oberbein", "rechtes_unterbein", "rechter_fuss"),
}

#: Accessoire-Anker (aus avatar_rig.ACCESSORY_LAYOUT) -> Knochen.
ANCHOR_BONE: Dict[str, str] = {
    "head_top": "kopf",
    "head_center": "kopf",
    "face_front": "kopf",
    "neck": "oberer_torso",
    "shoulders": "oberer_torso",
    "chest_front": "oberer_torso",
    "back": "oberer_torso",
    "waist": "unterer_torso",
    "right_hand": "rechte_hand",
}

MAX_ROTATION_DEG = 180.0
MAX_OFFSET_STUDS = 2.0
BLEND_FRACTION = 0.22

# ---------------------------------------------------------------------------
# Sprechende Pose-Felder je Koerperteil (Delta-Werte zur Stand-Pose)
# ---------------------------------------------------------------------------
# Feldname -> (Achse, Vorzeichen). Achsen: 0=X (vor/zurueck kippen),
# 1=Y (um Hoch-/Laengsachse drehen), 2=Z (seitlich kippen/spreizen).
# Seitliche Felder werden fuer die linke Koerperseite automatisch gespiegelt:
# "vom Koerper weg" / "nach aussen" heisst auf beiden Seiten wirklich aussen.
# Feldnamen mit ``_studs`` sind Positions-Deltas, der Rest Grad-Deltas.

def _side(bone: str) -> float:
    return -1.0 if bone.startswith(("linker", "linke", "linkes")) else 1.0


def _fields(bone: str) -> Dict[str, Tuple[int, float]]:
    side = _side(bone)
    # Positions-Deltas (Studs): hoch / vor (Gesichtsrichtung) / aussen.
    offsets = {
        "hoch_studs": (1, 1.0),
        "vor_studs": (2, -1.0),          # Gesicht zeigt nach -Z
        "seitlich_nach_aussen_studs": (0, side),
    }
    if bone in ("oberer_torso", "unterer_torso", "kopf"):
        offsets = {
            "hoch_studs": (1, 1.0),
            "vor_studs": (2, -1.0),
            "nach_rechts_studs": (0, 1.0),
        }
        rotations = {
            "nach_vorne_beugen_grad": (0, 1.0),
            "nach_links_drehen_grad": (1, 1.0),
            "nach_rechts_neigen_grad": (2, -1.0),
        }
        if bone == "kopf":
            rotations = {
                "nicken_grad": (0, 1.0),   # + = Kinn zur Brust
                "nach_links_drehen_grad": (1, 1.0),
                "nach_rechts_neigen_grad": (2, -1.0),
            }
        return {**rotations, **offsets}
    if bone.endswith("oberarm"):
        rotations = {
            "vor_hoch_schwingen_grad": (0, 1.0),   # 0=haengt, 90=waagerecht vor, 180=oben
            "vom_koerper_weg_grad": (2, side),      # + = seitlich vom Torso weg heben
            "laengs_drehung_grad": (1, 1.0),        # um die eigene Arm-Achse
        }
    elif bone.endswith("unterarm"):
        rotations = {
            "ellbogen_beugen_grad": (0, 1.0),       # + = Unterarm klappt nach vorne
            "laengs_drehung_grad": (1, 1.0),
        }
    elif bone.endswith("hand"):
        rotations = {
            "handgelenk_vor_beugen_grad": (0, 1.0),
            "handgelenk_nach_aussen_grad": (2, side),
            "laengs_drehung_grad": (1, 1.0),
        }
    elif bone.endswith("oberbein"):
        rotations = {
            "vor_hoch_schwingen_grad": (0, 1.0),    # 90 = Oberschenkel waagerecht
            "vom_koerper_weg_grad": (2, side),
            "laengs_drehung_grad": (1, 1.0),
        }
    elif bone.endswith("unterbein"):
        rotations = {
            "knie_beugen_grad": (0, -1.0),          # + = Unterschenkel klappt nach hinten
            "laengs_drehung_grad": (1, 1.0),
        }
    elif bone.endswith("fuss"):
        rotations = {
            "spitze_senken_grad": (0, -1.0),        # + = Fussspitze zeigt nach unten
            "fuss_nach_aussen_drehen_grad": (1, -side),
            "laengs_drehung_grad": (2, side),
        }
    else:  # pragma: no cover - alle Knochen oben abgedeckt
        rotations = {}
    return {**rotations, **offsets}


#: Erlaubte Feldnamen je Koerperteil (fuer Validierung + Fehlermeldungen).
BONE_FIELDS: Dict[str, Dict[str, Tuple[int, float]]] = {
    bone: _fields(bone) for bone in BODY_PART_KEYS
}

#: Pose-weite Einstellungen (Kamera/Ausschnitt) mit Standardwerten.
POSE_SETTING_DEFAULTS: Dict[str, float] = {
    "sichtbar_oben_prozent": 6.0,     # Freiraum ueber dem Kopf (% Bildhoehe)
    "sichtbar_unten_prozent": 6.0,    # Freiraum unter den Fuessen (negativ = Schnitt)
    "kamera_abstand_studs": 0.0,      # zusaetzlicher Kamera-Abstand (+ weiter weg)
    "avatar_hoch_studs": 0.0,         # ganzen Avatar anheben (+) / senken (-)
}
POSE_SETTING_LIMITS: Dict[str, Tuple[float, float]] = {
    "sichtbar_oben_prozent": (-100.0, 95.0),
    "sichtbar_unten_prozent": (-100.0, 95.0),
    "kamera_abstand_studs": (-6.0, 30.0),
    "avatar_hoch_studs": (-3.0, 5.0),
}


class PoseError(ValueError):
    """Posen-Datei fehlt, ist ungueltig oder nennt unbekannte Koerperteile."""


# ---------------------------------------------------------------------------
# Kleine Matrix-Helfer (4x4 affin, Zeilenweise)
# ---------------------------------------------------------------------------

IDENTITY4: Mat4 = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)


def _mat4_mul(a: Mat4, b: Mat4) -> Mat4:
    return tuple(
        tuple(sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4))
        for i in range(4)
    )  # type: ignore[return-value]


def _translation(offset: Sequence[float]) -> Mat4:
    return (
        (1.0, 0.0, 0.0, float(offset[0])),
        (0.0, 1.0, 0.0, float(offset[1])),
        (0.0, 0.0, 1.0, float(offset[2])),
        (0.0, 0.0, 0.0, 1.0),
    )


def _euler_matrix(rotation: Sequence[float]) -> Mat4:
    """Rotationsmatrix aus Grad-Angaben, Reihenfolge X -> Y -> Z."""
    rx, ry, rz = (math.radians(float(v)) for v in rotation)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)

    # Rz * Ry * Rx
    m00 = cz * cy
    m01 = cz * sy * sx - sz * cx
    m02 = cz * sy * cx + sz * sx
    m10 = sz * cy
    m11 = sz * sy * sx + cz * cx
    m12 = sz * sy * cx - cz * sx
    m20 = -sy
    m21 = cy * sx
    m22 = cy * cx
    return (
        (m00, m01, m02, 0.0),
        (m10, m11, m12, 0.0),
        (m20, m21, m22, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def _mat4_point(m: Mat4, p: Sequence[float]) -> Vec3:
    x, y, z = float(p[0]), float(p[1]), float(p[2])
    return (
        m[0][0] * x + m[0][1] * y + m[0][2] * z + m[0][3],
        m[1][0] * x + m[1][1] * y + m[1][2] * z + m[1][3],
        m[2][0] * x + m[2][1] * y + m[2][2] * z + m[2][3],
    )


def _mat3_of(m: Mat4) -> Mat3:
    return ((m[0][0], m[0][1], m[0][2]),
            (m[1][0], m[1][1], m[1][2]),
            (m[2][0], m[2][1], m[2][2]))


def _mat3_mul(a: Mat3, b: Mat3) -> Mat3:
    return tuple(  # type: ignore[return-value]
        tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
        for i in range(3))


def _mat3_transpose(m: Mat3) -> Mat3:
    return ((m[0][0], m[1][0], m[2][0]),
            (m[0][1], m[1][1], m[2][1]),
            (m[0][2], m[1][2], m[2][2]))


def _mat4_from(pos: Vec3, rot: Mat3) -> Mat4:
    return (
        (rot[0][0], rot[0][1], rot[0][2], pos[0]),
        (rot[1][0], rot[1][1], rot[1][2], pos[1]),
        (rot[2][0], rot[2][1], rot[2][2], pos[2]),
        (0.0, 0.0, 0.0, 1.0),
    )


def _motor_local(motor: Tuple[Vec3, Mat3], pivot: Point,
                 frame: Optional[Mat3]) -> Mat4:
    """Motor6D-Transform -> lokale Welt-Transformation des Gelenks.

    Roblox wertet Posen als ``child = parent * C0 * Transform * C1^-1`` aus;
    auf Bind-Weltkoordinaten uebertragen heisst das (Herleitung ueber die
    koinzidierenden Attachment-Frames): das Kind-Segment erhaelt delta
    ``J * T * J^-1`` auf das Eltern-Delta, wobei ``J`` der Bind-Welt-Frame
    des Eltern-Attachments ist (Position = Gelenkpivot, Rotation =
    ``frame``) und ``T`` der Pose-CFrame (Position + Rotation). Ohne Rig
    (``frame`` ist None) gilt ``J = (pivot, Identitaet)`` - dann ist das
    exakt die bisherige Formel ``T(t) * T(pivot) * R * T(-pivot)``.
    """
    t, r = motor
    jr = frame if frame is not None else (
        (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    # A = J * T
    a_pos = _add(pivot, _mat3_vec(jr, t))
    a_rot = _mat3_mul(jr, r)
    # local = A * J^-1  mit  J^-1 = (-jr^T * pivot, jr^T)
    inv_rot = _mat3_transpose(jr)
    inv_pos = _mat3_vec(inv_rot, (-pivot[0], -pivot[1], -pivot[2]))
    pos = _add(a_pos, _mat3_vec(a_rot, inv_pos))
    rot = _mat3_mul(a_rot, inv_rot)
    return _mat4_from(pos, rot)


def _mat3_vec(m: Mat3, v: Sequence[float]) -> Vec3:
    x, y, z = float(v[0]), float(v[1]), float(v[2])
    return (m[0][0] * x + m[0][1] * y + m[0][2] * z,
            m[1][0] * x + m[1][1] * y + m[1][2] * z,
            m[2][0] * x + m[2][1] * y + m[2][2] * z)


def _sub(a: Sequence[float], b: Sequence[float]) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a: Sequence[float], b: Sequence[float]) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _mul(a: Sequence[float], f: float) -> Vec3:
    return (a[0] * f, a[1] * f, a[2] * f)


def _length(v: Sequence[float]) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _normalize(v: Sequence[float], fallback: Vec3 = (0.0, 1.0, 0.0)) -> Vec3:
    length = _length(v)
    if length < 1e-9:
        return fallback
    return (v[0] / length, v[1] / length, v[2] / length)


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


# Quaternionen (w, x, y, z): gewichtetes Mitteln erhaelt das Volumen an
# Gelenken, waehrend gemittelte Positionen starrer Transformationen das
# Mesh am Ellbogen/Knie sichtbar einschnueren ("Candy-Wrapper").
Quat = Tuple[float, float, float, float]


def _mat3_to_quat(m: Mat3) -> Quat:
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        q = (0.25 * s, (m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s,
             (m[1][0] - m[0][1]) / s)
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2.0
        q = ((m[2][1] - m[1][2]) / s, 0.25 * s, (m[0][1] + m[1][0]) / s,
             (m[0][2] + m[2][0]) / s)
    elif m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2.0
        q = ((m[0][2] - m[2][0]) / s, (m[0][1] + m[1][0]) / s, 0.25 * s,
             (m[1][2] + m[2][1]) / s)
    else:
        s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2.0
        q = ((m[1][0] - m[0][1]) / s, (m[0][2] + m[2][0]) / s,
             (m[1][2] + m[2][1]) / s, 0.25 * s)
    length = math.sqrt(sum(v * v for v in q)) or 1.0
    return tuple(v / length for v in q)  # type: ignore[return-value]


def _quat_to_mat3(q: Quat) -> Mat3:
    w, x, y, z = q
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )


def _blend_transforms(items: List[Tuple[Mat4, float]]) -> Tuple[Mat3, Vec3]:
    """Gewichtete starre Transformation: Rotation slerp-artig, Translation lerp."""
    ref: Optional[Quat] = None
    acc = [0.0, 0.0, 0.0, 0.0]
    translation = [0.0, 0.0, 0.0]
    for matrix, weight in items:
        quat = _mat3_to_quat(_mat3_of(matrix))
        if ref is None:
            ref = quat
        if sum(a * b for a, b in zip(quat, ref)) < 0.0:  # gleiche Halbkugel
            quat = tuple(-v for v in quat)  # type: ignore
        for i in range(4):
            acc[i] += quat[i] * weight
        origin = _mat4_point(matrix, (0.0, 0.0, 0.0))
        for i in range(3):
            translation[i] += origin[i] * weight
    length = math.sqrt(sum(v * v for v in acc)) or 1.0
    quat = tuple(v / length for v in acc)  # type: ignore
    return _quat_to_mat3(quat), (translation[0], translation[1], translation[2])  # type: ignore


# ---------------------------------------------------------------------------
# Posen laden und pruefen
# ---------------------------------------------------------------------------

def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PoseError(f"{where}: '{value!r}' ist keine Zahl.")
    return float(value)


def _check_part(part: str, spec: Any, where: str) -> Dict[str, Any]:
    """Sprechende Delta-Felder eines Teils -> Euler-Grad + Welt-Offset."""
    if part not in BONE_PARENT:
        raise PoseError(
            f"{where}: unbekanntes Koerperteil '{part}'. Erlaubt: "
            + ", ".join(BODY_PART_KEYS))
    if not isinstance(spec, dict):
        raise PoseError(f"{where}: '{part}' muss eine Tabelle sein, z. B. "
                        "[Pose.rechter_oberarm] mit vor_hoch_schwingen_grad.")
    allowed = BONE_FIELDS[part]
    unknown = set(spec) - set(allowed)
    if unknown:
        hint = (" Die alten Felder 'rotation'/'verschiebung' gibt es nicht "
                "mehr." if unknown & {"rotation", "verschiebung"} else "")
        raise PoseError(
            f"{where}.{part}: unbekannte Felder {sorted(unknown)}.{hint} "
            f"Erlaubt fuer '{part}': " + ", ".join(sorted(allowed)) + ".")
    rotation = [0.0, 0.0, 0.0]
    offset = [0.0, 0.0, 0.0]
    felder: Dict[str, float] = {}
    for field, value in spec.items():
        number = _number(value, f"{where}.{part}.{field}")
        axis, sign = allowed[field]
        if field.endswith("_studs"):
            if abs(number) > MAX_OFFSET_STUDS:
                raise PoseError(
                    f"{where}.{part}.{field}: {number:g} Studs ist zu viel "
                    f"(Positions-Deltas max. +/-{MAX_OFFSET_STUDS:.0f}).")
            offset[axis] += sign * number
        else:
            if abs(number) > MAX_ROTATION_DEG:
                raise PoseError(
                    f"{where}.{part}.{field}: {number:g} Grad ist zu gross "
                    f"(max. +/-{MAX_ROTATION_DEG:.0f}).")
            rotation[axis] += sign * number
        felder[field] = number
    return {"rotation": tuple(rotation), "verschiebung": tuple(offset),
            "felder": felder}


def normalize_pose(pose: Any, name: str = "Pose") -> Dict[str, Dict[str, Any]]:
    """Prueft die Koerperteile einer Pose (Pose-Einstellungen s. load_poses)."""
    if pose is None:
        return {}
    if not isinstance(pose, dict):
        raise PoseError(f"Pose '{name}' muss eine Tabelle sein.")
    normalized: Dict[str, Dict[str, Any]] = {}
    for part, spec in pose.items():
        if part in ("beschreibung", "description") or part in POSE_SETTING_DEFAULTS:
            continue
        if isinstance(spec, dict) and "felder" in spec:
            # Bereits normalisiert (z. B. aus load_poses) - unveraendert durchreichen.
            entry = {
                "rotation": tuple(float(v) for v in spec["rotation"]),
                "verschiebung": tuple(float(v) for v in spec["verschiebung"]),
                "felder": dict(spec["felder"]),
            }
            motor = spec.get("motor")
            if motor is not None:
                # Motor6D-CFrame (Emotes): exakte Gelenk-Transformation
                # (Position + Rotationsmatrix) direkt durchreichen.
                entry["motor"] = motor
            normalized[part] = entry
            continue
        normalized[part] = _check_part(part, spec, f"Pose '{name}'")
    return normalized


def normalize_settings(table: Dict[str, Any], name: str = "Pose") -> Dict[str, float]:
    """Kamera-/Ausschnitt-Werte einer Pose pruefen (mit Defaults ergaenzen)."""
    settings = dict(POSE_SETTING_DEFAULTS)
    for key in POSE_SETTING_DEFAULTS:
        if key not in table:
            continue
        value = _number(table[key], f"Pose '{name}'.{key}")
        low, high = POSE_SETTING_LIMITS[key]
        if not low <= value <= high:
            raise PoseError(f"Pose '{name}'.{key}: {value:g} ausserhalb "
                            f"{low:g}..{high:g}.")
        settings[key] = value
    if (settings["sichtbar_oben_prozent"] + settings["sichtbar_unten_prozent"]
            > 95.0):
        raise PoseError(
            f"Pose '{name}': sichtbar_oben_prozent + sichtbar_unten_prozent "
            "darf hoechstens 95 ergeben (sonst bleibt kein Platz im Bild).")
    return settings


def load_poses(path: str) -> Dict[str, Dict[str, Any]]:
    """Laedt alle Posen aus ``Posen.toml`` (Name -> {beschreibung, teile})."""
    if not os.path.isfile(path):
        raise PoseError(
            f"Posen-Datei '{path}' fehlt. Bitte im Repository-Root anlegen "
            "(oder 'posen_datei' in der Configs.toml anpassen)."
        )
    try:
        import tomllib
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except ImportError:  # pragma: no cover - alte Pythons
        import render_config
        raw = render_config._read_toml(path)  # type: ignore[attr-defined]
    except Exception as exc:
        raise PoseError(f"'{path}' ist kein gueltiges TOML: {exc}") from exc

    poses: Dict[str, Dict[str, Any]] = {}
    for name, table in raw.items():
        if not isinstance(table, dict):
            raise PoseError(
                f"'{path}': '{name}' muss eine Pose-Tabelle sein "
                "(z. B. [Winken]).")
        description = table.get("beschreibung") or table.get("description") or ""
        if not isinstance(description, str):
            raise PoseError(f"'{path}': 'beschreibung' von '{name}' muss Text sein.")
        poses[name] = {
            "beschreibung": description,
            "teile": normalize_pose(table, name),
            "einstellungen": normalize_settings(table, name),
        }
    return poses


# ---------------------------------------------------------------------------
# Gelenkpunkte
# ---------------------------------------------------------------------------

def joints_from_parts(part_meshes: Dict[str, Any], metrics: Any) -> Dict[str, Point]:
    """Schaetzt die R15-Gelenke aus den tatsaechlichen Koerper-Meshes.

    Wird nur als Rueckfall benutzt; wenn das RBXM echte ``*RigAttachment``
    enthaelt, ueberschreibt ``build_avatar`` diese Werte mit den exakten
    Gelenkpositionen aus ``avatar_rig.assemble_r15_rig``.
    """
    def bbox(name: str):
        mesh = part_meshes.get(name)
        if mesh is None or mesh.is_empty:
            return None
        return mesh.bbox()

    torso = bbox("torso")
    if torso is not None:
        t_lo, t_hi = torso
    else:
        t_lo = (-0.5, 0.0, -0.5)
        t_hi = (0.5, 2.0, 0.5)

    torso_lo_y = t_lo[1]
    torso_hi_y = t_hi[1]
    waist_y = float(getattr(metrics, "waist_y", torso_lo_y + 0.5 * (torso_hi_y - torso_lo_y)))
    waist_y = max(torso_lo_y, min(torso_hi_y, waist_y))

    # Das Kopf-Gelenk sitzt am Hals, also an der Oberkante des Torsos -
    # nicht an der Unterkante einer eventuell grossen Kopfkugel.
    neck_y = torso_hi_y

    joints: Dict[str, Point] = {
        "LowerTorso": (0.0, torso_lo_y, 0.0),
        "UpperTorso": (0.0, waist_y, 0.0),
        "Head": (0.0, neck_y, 0.0),
    }

    for part_name, side in (("links_arm", "Left"), ("rechts_arm", "Right")):
        box = bbox(part_name)
        if box is None:
            continue
        lo, hi = box
        length = max(1e-3, hi[1] - lo[1])
        # Schultergelenk liegt an der *inneren* Oberkante des Arms (Roblox:
        # ShoulderRigAttachment am Torso-Rand), nicht in der Armmitte -
        # sonst bleibt beim Heben des Arms eine Luecke zum Torso.
        cx = hi[0] if side == "Left" else lo[0]
        cz = (lo[2] + hi[2]) / 2.0
        joints[f"{side}UpperArm"] = (cx, hi[1], cz)
        joints[f"{side}LowerArm"] = (cx, hi[1] - 0.58 * length, cz)
        joints[f"{side}Hand"] = (cx, hi[1] - 0.86 * length, cz)

    for part_name, side in (("links_bein", "Left"), ("rechts_bein", "Right")):
        box = bbox(part_name)
        if box is None:
            continue
        lo, hi = box
        length = max(1e-3, hi[1] - lo[1])
        cx = (lo[0] + hi[0]) / 2.0
        cz = (lo[2] + hi[2]) / 2.0
        joints[f"{side}UpperLeg"] = (cx, hi[1], cz)
        joints[f"{side}LowerLeg"] = (cx, hi[1] - 0.50 * length, cz)
        joints[f"{side}Foot"] = (cx, hi[1] - 0.90 * length, cz)

    return joints


# ---------------------------------------------------------------------------
# Gewichte entlang einer Knochen-Kette
# ---------------------------------------------------------------------------

def _closest_point_on_chain(point: Sequence[float],
                            pts: Sequence[Point]) -> Tuple[int, float, float]:
    """Naechster Punkt auf dem Streckenzug: (Segment, s entlang der Kette, Abstand)."""
    best = (0, 0.0, float("inf"))
    cumulative = 0.0
    for index in range(len(pts) - 1):
        a, b = pts[index], pts[index + 1]
        ab = _sub(b, a)
        seg_len = _length(ab)
        if seg_len < 1e-9:
            continue
        ap = _sub(point, a)
        t = (ap[0] * ab[0] + ap[1] * ab[1] + ap[2] * ab[2]) / (seg_len * seg_len)
        t = max(0.0, min(1.0, t))
        closest = _add(a, _mul(ab, t))
        dist = _length(_sub(point, closest))
        if dist < best[2]:
            best = (index, cumulative + t * seg_len, dist)
        cumulative += seg_len
    return best


def chain_weights(point: Sequence[float],
                  pts: Sequence[Point],
                  bones: Sequence[str],
                  blend_fraction: float = BLEND_FRACTION) -> Dict[str, float]:
    """Weiche Gewichte fuer einen Punkt entlang der Kette ``pts``."""
    count = len(bones)
    if count == 0 or len(pts) < 2:
        return {}
    if count == 1:
        return {bones[0]: 1.0}

    lengths = [_length(_sub(pts[i + 1], pts[i])) for i in range(len(pts) - 1)]
    boundaries: List[float] = []
    running = 0.0
    for index in range(len(lengths) - 1):
        running += lengths[index]
        boundaries.append(running)

    _, s, _ = _closest_point_on_chain(point, pts)

    widths: List[float] = []
    for index, boundary in enumerate(boundaries):
        neighbour = min(lengths[index], lengths[index + 1])
        widths.append(max(0.02, blend_fraction * neighbour))

    weights: Dict[str, float] = {}
    total = 0.0
    for index in range(count):
        left = 1.0
        if index > 0:
            left = _smoothstep((s - boundaries[index - 1]) / widths[index - 1] + 0.5)
        right = 0.0
        if index < count - 1:
            right = _smoothstep((s - boundaries[index]) / widths[index] + 0.5)
        weight = left * (1.0 - right)
        if weight > 1e-6:
            weights[bones[index]] = weight
            total += weight
    if total <= 1e-9:
        return {bones[0]: 1.0}
    return {name: value / total for name, value in weights.items()}


# ---------------------------------------------------------------------------
# Pose-Kontext
# ---------------------------------------------------------------------------

def _motor_is_active(motor: Any) -> bool:
    """Motor6D-CFrame von der Identitaet unterscheidbar?"""
    if not motor:
        return False
    try:
        t, r = motor
        if any(abs(float(v)) > 1e-9 for v in t):
            return True
        for i in range(3):
            for j in range(3):
                soll = 1.0 if i == j else 0.0
                if abs(float(r[i][j]) - soll) > 1e-9:
                    return True
    except (TypeError, ValueError, IndexError):
        return True
    return False


class PoseContext:
    """Wendet eine (bereits validierte) Pose auf Welt-Meshes an.

    Mit ``rig`` (ein ``avatar_rig.RiggedAvatar``) werden Koerperteile mit
    echten R15-Segmenten STARR je Segment transformiert - exakt wie
    Motor6D-Gelenke in Roblox (R15 ohne Skinning ist starr). Ohne ``rig``
    (prozedurale Fallback-Geometrie) bleibt die bisherige weiche
    Ketten-Verformung entlang der Gelenkpunkte.
    """

    def __init__(self, pose: Dict[str, Dict[str, Any]],
                 joints: Dict[str, Point],
                 settings: Optional[Dict[str, float]] = None,
                 rig: Any = None):
        # ``pose`` darf roh (sprechende Felder), normalisiert oder mit
        # ``motor``-CFrames (Emotes) sein.
        self.rig = rig
        self.pose = normalize_pose(pose) if pose else {}
        self.settings = {**POSE_SETTING_DEFAULTS, **(settings or {})}
        self.joints = self._resolve_joints(joints)
        self.avatar_offset: Vec3 = (0.0, float(self.settings["avatar_hoch_studs"]), 0.0)
        self.active = (
            any(any(abs(v) > 1e-9 for v in spec["rotation"]) or
                any(abs(v) > 1e-9 for v in spec["verschiebung"]) or
                _motor_is_active(spec.get("motor"))
                for spec in self.pose.values())
            or any(abs(v) > 1e-9 for v in self.avatar_offset))
        self.transforms: Dict[str, Mat4] = {}
        self.rotations: Dict[str, Mat3] = {}
        self._build_transforms()
        self._chain_cache: Dict[str, Tuple[List[Point], List[str]]] = {}

    @staticmethod
    def _resolve_joints(joints: Dict[str, Point]) -> Dict[str, Point]:
        """Jedes Gelenk auf einen echten Pivot festlegen.

        Exakte RigAttachment-Positionen bleiben unangetastet. Fehlt ein
        Gelenk, wird es aus den Nachbarn derselben Kette interpoliert bzw.
        am Kettenanfang/-ende verankert - niemals auf (0,0,0) oder in die
        Teilmitte geraten, sonst reisst die Drehung das Mesh auseinander.
        """
        resolved = {k: (float(p[0]), float(p[1]), float(p[2]))
                    for k, p in joints.items()}
        chains = (
            ("LowerTorso", "UpperTorso", "Head"),
            ("LeftUpperArm", "LeftLowerArm", "LeftHand"),
            ("RightUpperArm", "RightLowerArm", "RightHand"),
            ("LeftUpperLeg", "LeftLowerLeg", "LeftFoot"),
            ("RightUpperLeg", "RightLowerLeg", "RightFoot"),
        )
        for chain in chains:
            known = [i for i, n in enumerate(chain) if n in resolved]
            for i, joint_name in enumerate(chain):
                if joint_name in resolved:
                    continue
                if known:
                    near = min(known, key=lambda k: abs(k - i))
                    # Am Kettenende: 0.45 Studs hinter dem letzten Gelenk
                    # in Kettenrichtung; dazwischen: Mitte der Nachbarn.
                    before = [k for k in known if k < i]
                    after = [k for k in known if k > i]
                    if before and after:
                        a, b = resolved[chain[before[-1]]], resolved[chain[after[0]]]
                        resolved[joint_name] = tuple((a[j] + b[j]) / 2 for j in range(3))  # type: ignore
                    elif before:
                        a = resolved[chain[before[-1]]]
                        prev = resolved[chain[before[-2]]] if len(before) > 1 else None
                        d = _sub(a, prev) if prev else (0.0, -1.0, 0.0)
                        step = _mul(_normalize(d, (0.0, -1.0, 0.0)), 0.45)
                        resolved[joint_name] = _add(a, step)  # type: ignore
                    else:
                        resolved[joint_name] = resolved[chain[near]]
                else:
                    resolved[joint_name] = (0.0, 0.0, 0.0)
        return resolved

    # -- Skelett ---------------------------------------------------------
    def _build_transforms(self) -> None:
        for bone in BONE_ORDER:
            spec = self.pose.get(bone) or {}
            rotation = spec.get("rotation", (0.0, 0.0, 0.0))
            offset = spec.get("verschiebung", (0.0, 0.0, 0.0))
            pivot = self.joints[BONE_JOINT[bone]]  # nach _resolve_joints immer da
            motor = spec.get("motor")
            if motor is not None:
                # Motor6D-Semantik (Emotes): Rotation im Frame des Eltern-
                # Attachments (C0), nicht in Weltachsen. Ohne Rig-Frame
                # exakt identisch zur klassischen Formel unten.
                frame = None
                if self.rig is not None:
                    frame = self.rig.joint_frames.get(BONE_JOINT[bone])
                local = _motor_local(motor, pivot, frame)
            else:
                # Drehung exakt um den Gelenk-Pivot; das Positions-Delta wirkt
                # im Eltern-Raum (hoch/vor/aussen), verschiebt also den Pivot
                # selbst - Kinder und Accessoires folgen nahtlos.
                local = _mat4_mul(_translation(offset),
                                  _mat4_mul(_translation(pivot),
                                            _mat4_mul(_euler_matrix(rotation),
                                                      _translation((-pivot[0], -pivot[1], -pivot[2])))))
            parent = BONE_PARENT[bone]
            world = _mat4_mul(self.transforms[parent], local) if parent else local
            if parent is None and any(abs(v) > 1e-9 for v in self.avatar_offset):
                world = _mat4_mul(_translation(self.avatar_offset), world)
            self.transforms[bone] = world
            self.rotations[bone] = _mat3_of(world)

    def transform_point(self, point: Sequence[float], bone: str = "kopf") -> Vec3:
        """Einen Weltpunkt mit der Pose eines Knochens mitbewegen."""
        return _mat4_point(self.transforms[bone], point)

    def _joint(self, key: str) -> Point:
        return self.joints.get(key, (0.0, 0.0, 0.0))

    def _chain(self, part_name: str) -> Tuple[List[Point], List[str]]:
        if part_name in self._chain_cache:
            return self._chain_cache[part_name]
        bones = list(PART_CHAINS[part_name])

        if part_name == "torso":
            lower = self._joint("LowerTorso")
            upper = self._joint("UpperTorso")
            top = self._joint("Head") if "Head" in self.joints else _add(upper, _sub(upper, lower))
            pts = [lower, upper, top]
        elif part_name == "hals":
            upper = self._joint("UpperTorso")
            head = self._joint("Head")
            pts = [upper, head, _add(head, (0.0, 0.4, 0.0))]
        elif part_name in ("links_arm", "rechts_arm"):
            side = "Left" if part_name == "links_arm" else "Right"
            shoulder = self._joint(f"{side}UpperArm")
            elbow = self._joint(f"{side}LowerArm")
            wrist = self._joint(f"{side}Hand")
            direction = _normalize(_sub(wrist, elbow), (0.0, -1.0, 0.0))
            pts = [shoulder, elbow, wrist, _add(wrist, _mul(direction, 0.35))]
        else:
            side = "Left" if part_name == "links_bein" else "Right"
            hip = self._joint(f"{side}UpperLeg")
            knee = self._joint(f"{side}LowerLeg")
            ankle = self._joint(f"{side}Foot")
            direction = _normalize(_sub(ankle, knee), (0.0, -1.0, 0.0))
            pts = [hip, knee, ankle, _add(ankle, _mul(direction, 0.30))]

        self._chain_cache[part_name] = (pts, bones)
        return pts, bones

    def _bone_segment(self, bone: str) -> Tuple[Point, Point]:
        start = self._joint(BONE_JOINT[bone])
        child = next((b for b, p in BONE_PARENT.items() if p == bone), None)
        if child is not None:
            return start, self._joint(BONE_JOINT[child])
        parent = BONE_PARENT[bone]
        if parent is not None:
            direction = _normalize(_sub(start, self._joint(BONE_JOINT[parent])),
                                   (0.0, -1.0, 0.0))
            return start, _add(start, _mul(direction, 0.45))
        return start, _add(start, (0.0, 1.0, 0.0))

    def nearest_bone(self, point: Sequence[float]) -> str:
        best_bone = "oberer_torso"
        best_dist = float("inf")
        for bone in BONE_ORDER:
            a, b = self._bone_segment(bone)
            _, _, dist = _closest_point_on_chain(point, [a, b])
            if dist < best_dist:
                best_dist = dist
                best_bone = bone
        return best_bone

    # -- Mesh-Verformung -------------------------------------------------
    def _transform_mesh(self, mesh: MeshData,
                        weight_fn) -> MeshData:
        positions: List[Vec3] = []
        normals: List[Vec3] = []
        has_normals = len(mesh.normals) == len(mesh.positions)

        for vertex in mesh.positions:
            weights = weight_fn(vertex)
            if not weights:
                positions.append(vertex)
                if has_normals:
                    normals.append(mesh.normals[len(positions) - 1])
                continue
            # Eine einzige gewichtete starre Transformation pro Vertex
            # (Rotation quaternionen-gemischt): kein Einschnueren am Gelenk.
            rotation, translation = _blend_transforms(
                [(self.transforms[bone], w) for bone, w in weights.items()])
            # p' = R_mix * p + t_mix: fuer einen einzelnen Knochen exakt
            # dessen Transformation, dazwischen eine starre Ueberblendung
            # um den interpolierten Pivot (kein Volumenverlust).
            rotated = _mat3_vec(rotation, vertex)
            positions.append((rotated[0] + translation[0],
                              rotated[1] + translation[1],
                              rotated[2] + translation[2]))

            if has_normals:
                normal = mesh.normals[len(positions) - 1]
                normals.append(_normalize(_mat3_vec(rotation, normal), normal))

        return MeshData(
            positions=positions,
            normals=normals if has_normals else [],
            uvs=list(mesh.uvs),
            faces=list(mesh.faces),
            colors=list(mesh.colors) if mesh.colors else None,
            source_version=mesh.source_version,
        )

    def _apply_segment_spans(self, mesh: MeshData,
                             spans: List[Tuple[str, int, int]]) -> MeshData:
        """Starr je R15-Segment transformieren (Motor6D-Semantik).

        Jedes Segment bekommt die Transformation ``transforms[bone]`` seines
        Gelenks - Rotation exakt um das Bind-Attachment, Verkettung ueber
        die Eltern-Gelenke. Kein weiches Verformen: R15-Teile sind starr,
        dadurch bleiben die Gelenke auch bei starken Posen nahtlos.
        """
        positions: List[Vec3] = list(mesh.positions)
        has_normals = len(mesh.normals) == len(mesh.positions)
        normals: List[Vec3] = list(mesh.normals) if has_normals else []
        for seg_name, start, end in spans:
            if end <= start:
                continue
            bone = JOINT_BONE.get(seg_name)
            if bone is None:
                continue
            transform = self.transforms[bone]
            rotation = self.rotations[bone]
            for i in range(start, end):
                positions[i] = _mat4_point(transform, positions[i])
                if has_normals:
                    n = normals[i]
                    normals[i] = _normalize(_mat3_vec(rotation, n), n)
        return MeshData(
            positions=positions,
            normals=normals if has_normals else [],
            uvs=list(mesh.uvs),
            faces=list(mesh.faces),
            colors=list(mesh.colors) if mesh.colors else None,
            source_version=mesh.source_version,
        )

    def apply_body(self, part_name: str, mesh: MeshData) -> MeshData:
        """Verformt ein Koerperteil (torso, Arme, Beine, Kopf, Hals)."""
        if not self.active or mesh.is_empty:
            return mesh
        if self.rig is not None:
            spans = self.rig.segment_spans.get(part_name)
            if spans:
                return self._apply_segment_spans(mesh, spans)
        chain = PART_CHAINS.get(part_name)
        if chain is None:
            return mesh

        if len(chain) == 1:
            bone = chain[0]
            transform = self.transforms[bone]
            rotation = self.rotations[bone]
            return MeshData(
                positions=[_mat4_point(transform, p) for p in mesh.positions],
                normals=([_normalize(_mat3_vec(rotation, n), n) for n in mesh.normals]
                         if len(mesh.normals) == len(mesh.positions) else []),
                uvs=list(mesh.uvs),
                faces=list(mesh.faces),
                colors=list(mesh.colors) if mesh.colors else None,
                source_version=mesh.source_version,
            )

        pts, bones = self._chain(part_name)
        return self._transform_mesh(
            mesh, lambda p: chain_weights(p, pts, bones))

    def apply_accessory(self, mesh: MeshData, anchor: str = "") -> MeshData:
        """Dreht/verschiebt ein Accessoire starr mit seinem Knochen."""
        if not self.active or mesh.is_empty:
            return mesh
        bone = ANCHOR_BONE.get(anchor)
        if bone is None:
            if not mesh.positions:
                return mesh
            lo, hi = mesh.bbox()
            center = ((lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0,
                      (lo[2] + hi[2]) / 2.0)
            bone = self.nearest_bone(center)
        transform = self.transforms[bone]
        rotation = self.rotations[bone]
        return MeshData(
            positions=[_mat4_point(transform, p) for p in mesh.positions],
            normals=([_normalize(_mat3_vec(rotation, n), n) for n in mesh.normals]
                     if len(mesh.normals) == len(mesh.positions) else []),
            uvs=list(mesh.uvs),
            faces=list(mesh.faces),
            colors=list(mesh.colors) if mesh.colors else None,
            source_version=mesh.source_version,
        )


def pose_summary(pose: Dict[str, Dict[str, Any]],
                 settings: Optional[Dict[str, float]] = None) -> str:
    """Kurze, lesbare Zusammenfassung fuer Log/Manifest."""
    parts = []
    for part in BODY_PART_KEYS:
        spec = pose.get(part)
        if not spec:
            continue
        felder = spec.get("felder") or {}
        active = {k: v for k, v in felder.items() if abs(v) > 1e-9}
        if not active:
            continue
        parts.append(f"{part}: " + ", ".join(
            f"{k}={v:g}" for k, v in active.items()))
    if settings:
        parts.append("Kamera/Ausschnitt: " + ", ".join(
            f"{k}={settings[k]:g}" for k in POSE_SETTING_DEFAULTS
            if k in settings))
    return "; ".join(parts) if parts else "neutrale Stand-Pose"
