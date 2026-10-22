#!/usr/bin/env python3
"""Posen fuer den Avatar-Render: laden, pruefen und auf das Rig anwenden.

Die Posen stehen in ``Posen.toml`` im Repository-Root. Dort kann fuer jedes
Koerperteil eine Drehung (Grad, alle drei Achsen) und eine leichte
Verschiebung (Studs) angegeben werden. Diese Datei enthaelt die Logik:

* Laden/Validieren der Posen (``load_poses``)
* Gelenkpunkte des Rigs bestimmen (``joints_from_parts`` - Rueckfall, wenn das
  RBXM-Rig keine exakten ``*RigAttachment``-Gelenke geliefert hat)
* Transformationen der Knochen-Kette berechnen (Eltern-Drehungen wirken auf
  alle Kinder, wie bei einem echten Skelett)
* Mesh-Verformung mit weichen Uebergaengen an den Gelenken (Linear Blend
  Skinning entlang der Kette). Accessoires folgen starr dem passenden Knochen
  (Hut -> Kopf, Gear -> rechte Hand, ...).

Koordinatensystem des Rigs (Roblox): X = rechts, Y = hoch, Z = hinten
(das Gesicht zeigt nach -Z). Drehungen werden in Grad angegeben und in der
Reihenfolge X, dann Y, dann Z angewendet.
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
MAX_OFFSET_STUDS = 5.0
BLEND_FRACTION = 0.16


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


# ---------------------------------------------------------------------------
# Posen laden und pruefen
# ---------------------------------------------------------------------------

def _numbers(value: Any, count: int, where: str) -> Tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != count:
        raise PoseError(f"{where}: erwartet eine Liste mit {count} Zahlen, "
                        f"z. B. [0, 0, 0] (gefunden: {value!r}).")
    result: List[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise PoseError(f"{where}: '{item!r}' ist keine Zahl.")
        result.append(float(item))
    return tuple(result)


def _check_part(part: str, spec: Any, where: str) -> Dict[str, Tuple[float, ...]]:
    if part not in BONE_PARENT:
        raise PoseError(
            f"{where}: unbekanntes Koerperteil '{part}'. Erlaubt: "
            + ", ".join(BODY_PART_KEYS)
        )
    if not isinstance(spec, dict):
        raise PoseError(
            f"{where}: '{part}' muss eine Tabelle mit rotation/verschiebung sein."
        )
    rotation = _numbers(spec.get("rotation", [0, 0, 0]), 3, f"{where}.{part}.rotation")
    offset = _numbers(spec.get("verschiebung", [0, 0, 0]), 3,
                      f"{where}.{part}.verschiebung")
    for value in rotation:
        if abs(value) > MAX_ROTATION_DEG:
            raise PoseError(
                f"{where}.{part}.rotation: {value:g}° ist zu gross "
                f"(maximal +/-{MAX_ROTATION_DEG:.0f}°).")
    for value in offset:
        if abs(value) > MAX_OFFSET_STUDS:
            raise PoseError(
                f"{where}.{part}.verschiebung: {value:g} Studs ist zu viel "
                f"('leichte Verschiebungen', maximal +/-{MAX_OFFSET_STUDS:.0f}).")
    unknown = set(spec) - {"rotation", "verschiebung"}
    if unknown:
        raise PoseError(
            f"{where}.{part}: unbekannte Felder {sorted(unknown)} "
            "(erlaubt: rotation, verschiebung).")
    return {"rotation": rotation, "verschiebung": offset}


def normalize_pose(pose: Any, name: str = "Pose") -> Dict[str, Dict[str, Tuple[float, ...]]]:
    """Prueft eine einzelne Pose und ergaenzt fehlende Teile mit 0-Werten."""
    if pose is None:
        return {}
    if not isinstance(pose, dict):
        raise PoseError(f"Pose '{name}' muss eine Tabelle sein.")
    normalized: Dict[str, Dict[str, Tuple[float, ...]]] = {}
    for part, spec in pose.items():
        if part in ("beschreibung", "description"):
            continue
        normalized[part] = _check_part(part, spec, f"Pose '{name}'")
    return normalized


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

class PoseContext:
    """Wendet eine (bereits validierte) Pose auf Welt-Meshes an."""

    def __init__(self, pose: Dict[str, Dict[str, Tuple[float, ...]]],
                 joints: Dict[str, Point]):
        self.pose = normalize_pose(pose) if pose else {}
        self.joints = dict(joints)
        self.active = any(
            any(abs(v) > 1e-9 for v in spec["rotation"]) or
            any(abs(v) > 1e-9 for v in spec["verschiebung"])
            for spec in self.pose.values()
        )
        self.transforms: Dict[str, Mat4] = {}
        self.rotations: Dict[str, Mat3] = {}
        self._build_transforms()
        self._chain_cache: Dict[str, Tuple[List[Point], List[str]]] = {}

    # -- Skelett ---------------------------------------------------------
    def _build_transforms(self) -> None:
        for bone in BONE_ORDER:
            spec = self.pose.get(bone) or {}
            rotation = spec.get("rotation", (0.0, 0.0, 0.0))
            offset = spec.get("verschiebung", (0.0, 0.0, 0.0))
            pivot = self.joints.get(BONE_JOINT[bone])
            local = IDENTITY4
            if pivot is not None:
                rot = _euler_matrix(rotation)
                local = _mat4_mul(_translation(pivot), rot)
                local = _mat4_mul(local, _translation((-pivot[0], -pivot[1], -pivot[2])))
            local = _mat4_mul(local, _translation(offset))
            parent = BONE_PARENT[bone]
            self.transforms[bone] = (
                _mat4_mul(self.transforms[parent], local) if parent
                else local
            )
            self.rotations[bone] = _mat3_of(self.transforms[bone])

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
            x = y = z = 0.0
            for bone, weight in weights.items():
                px, py, pz = _mat4_point(self.transforms[bone], vertex)
                x += px * weight
                y += py * weight
                z += pz * weight
            positions.append((x, y, z))

            if has_normals:
                normal = mesh.normals[len(positions) - 1]
                nx = ny = nz = 0.0
                for bone, weight in weights.items():
                    rx, ry, rz = _mat3_vec(self.rotations[bone], normal)
                    nx += rx * weight
                    ny += ry * weight
                    nz += rz * weight
                normals.append(_normalize((nx, ny, nz), normal))

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


def pose_summary(pose: Dict[str, Dict[str, Tuple[float, ...]]]) -> str:
    """Kurze, lesbare Zusammenfassung fuer Log/Manifest."""
    parts = []
    for part in BODY_PART_KEYS:
        spec = pose.get(part)
        if not spec:
            continue
        rotation = spec["rotation"]
        offset = spec["verschiebung"]
        if not any(abs(v) > 1e-9 for v in rotation + offset):
            continue
        parts.append(
            f"{part}: Drehung "
            + "/".join(f"{v:g}" for v in rotation)
            + "°, Verschiebung "
            + "/".join(f"{v:g}" for v in offset)
        )
    return "; ".join(parts) if parts else "neutrale Stand-Pose"
