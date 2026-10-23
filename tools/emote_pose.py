#!/usr/bin/env python3
"""Emote-Posen: Roblox-Emote per Asset-ID als Pose fuer den Avatar-Render.

In ``Configs.toml`` steht statt eines Posen.toml-Namens einfach eine Emote-
Referenz::

    pose = "emote:3576893107"      # Katalog-/Emote-Asset-ID

Geladen wird genau die Haltung, die auch das **Katalog-Vorschaubild** zeigt -
und zwar deterministisch ueber die Daten, die Roblox selbst dafuer benutzt
(keine Bildvergleichs-Heuristik):

* Im Emote-Asset stecken als ``NumberValue``-Kinder der ``Animation`` die
  Thumbnail-Parameter: ``ThumbnailKeyframe`` (1-basierte Keyframe-Nummer bei
  KeyframeSequence-Emotes), ``ThumbnailTime`` (Sekunden bei CurveAnimation-
  Emotes) sowie Kamera-Werte (``ThumbnailZoom``, ``ThumbnailVerticalOffset``,
  ``ThumbnailHorizontalOffset``, ``ThumbnailCharacterRotation``).
* Roblox' eigene Thumbnail-Renderer (CoreScripts ``PublishAssetPrompt`` /
  ``Thumbnailing/EmoteUtility`` und das RCC-Skript ``AnimationSilhouette``)
  waehlen danach den Frame:

  - KeyframeSequence: Keyframe Nr. ``ThumbnailKeyframe``, falls gueltig
    (1..n), sonst der **Mittel-Keyframe** (``ceil(n/2)``).
  - CurveAnimation: Sampling bei ``ThumbnailTime``, falls gueltig
    (0..Dauer), sonst bei der **halben Dauer**.
  - Beim Publish-Dialog schreibt Roblox die Defaults ``ThumbnailKeyframe=1``
    bzw. ``ThumbnailTime=0`` ins Asset - UGC-Emotes zeigen also meist den
    ersten Frame, kuratierte Roblox-Emotes eigene Werte (z. B. 31, 70).

* Die Pose-Anwendung folgt der RCC-Formel ``joint.C1 = joint.C1 *
  pose.CFrame:inverse()``: entspricht einem Motor6D-Transform. Mit den
  RigAttachments als Pivot und identischen Rest-Orientierungen der Teile
  laesst sich das exakt als "Drehung um das Gelenk + Positions-Delta"
  ausdruecken - genau das Format, das ``avatar_pose.PoseContext`` hebelt.

CurveAnimations (Pflicht fuer moderne UGC-Emotes) werden als
``FloatCurve``-``ValuesAndTimes``-Blobs gelesen und mit derselben
Interpolation ausgewertet, die die Engine benutzt (konstant/linear/kubisch
Hermite inkl. Auto-Tangenten). Zeit-Einheit der Ticks: 1/2400 s.

Oeffentliche API (Details siehe jeweils Docstring)::

    parse_emote_reference(value)        "emote:123"/URL/Zahl -> 123
    load_emote_pose(...)                kompletter Laden->Pose-Ablauf
"""

from __future__ import annotations

import math
import re
import struct
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:  # direkter Import beim Aufruf aus tools/
    import avatar_pose
    import rbxm
    import roblox_api as api
except ImportError:  # pragma: no cover - Aufruf von ausserhalb
    from tools import avatar_pose  # type: ignore
    from tools import rbxm  # type: ignore
    from tools import roblox_api as api  # type: ignore

Vec3 = Tuple[float, float, float]
Mat3 = Tuple[Vec3, Vec3, Vec3]
CFrame = Tuple[Vec3, Mat3]  # (Position, Rotationsmatrix), Spaltenvektor-Konvention

#: Zeit-Einheit der Curve-Ticks: 2400 Ticks pro Sekunde (Roblox-Engine-Konstante).
TICKS_PER_SECOND = 2400.0

#: R15-Teilnamen (Pose-/Folder-Namen in Animationen) -> Knochen in avatar_pose.
POSE_BONE_NAMES: Dict[str, str] = {
    "LowerTorso": "unterer_torso",
    "UpperTorso": "oberer_torso",
    "Head": "kopf",
    "LeftUpperArm": "linker_oberarm",
    "LeftLowerArm": "linker_unterarm",
    "LeftHand": "linke_hand",
    "RightUpperArm": "rechter_oberarm",
    "RightLowerArm": "rechter_unterarm",
    "RightHand": "rechte_hand",
    "LeftUpperLeg": "linkes_oberbein",
    "LeftLowerLeg": "linkes_unterbein",
    "LeftFoot": "linker_fuss",
    "RightUpperLeg": "rechtes_oberbein",
    "RightLowerLeg": "rechtes_unterbein",
    "RightFoot": "rechter_fuss",
}

#: Namen der Thumbnail-NumberValues im Emote-Asset (Roblox-Publish-UI).
THUMBNAIL_VALUE_NAMES = (
    "ThumbnailKeyframe", "ThumbnailTime", "ThumbnailZoom",
    "ThumbnailVerticalOffset", "ThumbnailHorizontalOffset",
    "ThumbnailCharacterRotation", "ThumbnailBundleId", "ThumbnailFieldOfView",
)

#: Roblox-Rotationsordnungen (Enum-Wert -> Achsfolge, M = R_a0 * R_a1 * R_a2).
_ROTATION_ORDERS: Dict[int, Tuple[str, str, str]] = {
    0: ("X", "Y", "Z"),
    1: ("X", "Z", "Y"),
    2: ("Y", "Z", "X"),
    3: ("Y", "X", "Z"),
    4: ("Z", "X", "Y"),
    5: ("Z", "Y", "X"),
}

MAX_DOWNLOAD_HOPS = 4
MAX_OFFSET_STUDS = 50.0


class EmoteError(ValueError):
    """Emote konnte nicht geladen/ausgewertet werden (klare Meldung)."""


# ---------------------------------------------------------------------------
# Emote-Referenz aus der Pose-Angabe lesen
# ---------------------------------------------------------------------------

_EMOTE_PREFIX_RE = re.compile(r"^\s*emote\s*[:=]?\s*(\d{1,16})\s*$", re.IGNORECASE)
_CATALOG_URL_RE = re.compile(r"roblox\.com/(?:catalog|library)/(\d+)", re.IGNORECASE)


def parse_emote_reference(value: Any) -> Optional[int]:
    """``"emote:123"``, ``"Emote = 123"``, Katalog-URL oder ``"123"`` -> 123.

    Alles andere (inkl. ``None``/``"None"``/Posen.toml-Namen) -> ``None``.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "none":
        return None
    match = _EMOTE_PREFIX_RE.match(text)
    if match:
        return int(match.group(1))
    url_match = _CATALOG_URL_RE.search(text)
    if url_match:
        return int(url_match.group(1))
    if text.isdigit():
        number = int(text)
        return number if number > 0 else None
    return None


# ---------------------------------------------------------------------------
# Kleine Mathe-Helfer (Spaltenvektor-Konvention wie avatar_pose)
# ---------------------------------------------------------------------------

IDENTITY3: Mat3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def _mat3_mul(a: Mat3, b: Mat3) -> Mat3:
    return tuple(  # type: ignore[return-value]
        tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
        for i in range(3)
    )


def _axis_rotation(axis: str, radians: float) -> Mat3:
    c, s = math.cos(radians), math.sin(radians)
    if axis == "X":
        return ((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c))
    if axis == "Y":
        return ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c))
    return ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))


def euler_matrix_rbx(rx: float, ry: float, rz: float, order: int = 0) -> Mat3:
    """Rotationsmatrix aus Roblox-Eulerwinkeln (Radiant) wie die Engine sie baut.

    Roblox ``CFrame.FromEulerAngles(rx, ry, rz, order)`` komponiert die
    Achsen der Rotationsordnung als ``M = R_a0 * R_a1 * R_a2``
    (Spaltenvektor). Standard-Ordnung 0 = XYZ, also ``M = Rx * Ry * Rz``.
    """
    axes = _ROTATION_ORDERS.get(order) or _ROTATION_ORDERS[0]
    angles = {"X": rx, "Y": ry, "Z": rz}
    m = IDENTITY3
    for axis in axes:
        m = _mat3_mul(m, _axis_rotation(axis, angles[axis]))
    return m


def _orthonormalize(m: Mat3) -> Mat3:
    """Zeilen orthonormalisieren (Schutz gegen leicht entartete Matrizen)."""
    def sub(a: Vec3, b: Vec3, f: float) -> Vec3:
        return (a[0] - f * b[0], a[1] - f * b[1], a[2] - f * b[2])

    def dot(a: Vec3, b: Vec3) -> float:
        return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]

    def norm(a: Vec3) -> Vec3:
        length = math.sqrt(dot(a, a))
        if length < 1e-9 or not math.isfinite(length):
            return (0.0, 0.0, 0.0)
        return (a[0] / length, a[1] / length, a[2] / length)

    r0 = norm((m[0][0], m[0][1], m[0][2]))
    r1 = sub((m[1][0], m[1][1], m[1][2]), r0, dot((m[1][0], m[1][1], m[1][2]), r0))
    r1 = norm(r1)
    r2 = sub((m[2][0], m[2][1], m[2][2]), r0, dot((m[2][0], m[2][1], m[2][2]), r0))
    r2 = sub(r2, r1, dot(r2, r1))
    r2 = norm(r2)
    if r2 == (0.0, 0.0, 0.0):
        return IDENTITY3
    # Rechtshaendig erzwingen (det = +1), sonst spiegelte die Pose.
    cross = (
        r1[1] * r2[2] - r1[2] * r2[1],
        r1[2] * r2[0] - r1[0] * r2[2],
        r1[0] * r2[1] - r1[1] * r2[0],
    )
    if dot(cross, r0) < 0.0:
        r2 = (-r2[0], -r2[1], -r2[2])
    return (r0, r1, r2)


def mat3_to_euler_deg(m: Mat3) -> Vec3:
    """Matrix -> Euler (Grad) in der Konvention von avatar_pose (Rz * Ry * Rx).

    Gegenstueck zu ``avatar_pose._euler_matrix``: dort gilt
    ``M = Rz(rz) * Ry(ry) * Rx(rx)``. Gimbal-Lock (ry = +/-90 Grad) wird
    aufgeloest, indem rz = 0 gesetzt wird.
    """
    m = _orthonormalize(m)
    sy = max(-1.0, min(1.0, -m[2][0]))
    cy = math.sqrt(1.0 - sy * sy)
    if cy > 1e-6:
        rx = math.atan2(m[2][1], m[2][2])
        ry = math.asin(sy)
        rz = math.atan2(m[1][0], m[0][0])
    else:
        # Gimbal Lock: M = Ry(+-90) * Rx(rx), rz bleibt frei -> 0.
        sign = 1.0 if sy > 0.0 else -1.0
        rx = math.atan2(sign * m[0][1], sign * m[0][2])
        ry = math.copysign(math.pi / 2.0, sy)
        rz = 0.0
    return (math.degrees(rx), math.degrees(ry), math.degrees(rz))


def _quat_to_mat3(x: float, y: float, z: float, w: float) -> Mat3:
    length = math.sqrt(x * x + y * y + z * z + w * w)
    if length < 1e-9 or not math.isfinite(length):
        return IDENTITY3
    x, y, z, w = x / length, y / length, z / length, w / length
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )


def _cframe_mul(a: CFrame, b: CFrame) -> CFrame:
    """CFrame-Komposition (Ra|ta)*(Rb|tb) = (RaRb | Ra*tb + ta)."""
    (ta, ra), (tb, rb) = a, b
    rot = _mat3_mul(ra, rb)
    trans = (
        ra[0][0] * tb[0] + ra[0][1] * tb[1] + ra[0][2] * tb[2] + ta[0],
        ra[1][0] * tb[0] + ra[1][1] * tb[1] + ra[1][2] * tb[2] + ta[1],
        ra[2][0] * tb[0] + ra[2][1] * tb[1] + ra[2][2] * tb[2] + ta[2],
    )
    return (trans, rot)


# ---------------------------------------------------------------------------
# Instanz-Baum durchsuchen (rbxm.parse_instances-Records)
# ---------------------------------------------------------------------------

def _iter_tree(roots: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    stack: List[Dict[str, Any]] = list(roots)
    while stack:
        rec = stack.pop()
        out.append(rec)
        stack.extend(rec.get("children") or ())
    return out


def _record_value_num(rec: Dict[str, Any]) -> Optional[float]:
    """NumberValue-/IntValue-artiger Wert einer Instanz (Value-Property)."""
    value = rec.get("value_num")
    if value is None and rec.get("value_str") is not None:
        try:
            value = float(rec["value_str"])
        except (TypeError, ValueError):
            value = None
    if value is None and isinstance(rec.get("value_vec3"), tuple):
        # Vector3Value: X-Komponente ist als Daumenregel besser als nichts -
        # echte Thumbnail-Werte sind aber immer Number-/IntValues.
        value = float(rec["value_vec3"][0])
    if value is None or not math.isfinite(value):
        return None
    return float(value)


def read_thumbnail_metadata(roots: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    """Liest die ``Thumbnail*``-NumberValues aus einem Emote-Asset.

    Gesucht wird in den Kindern jeder ``Animation``-Instanz (dort legt sie
    Roblox' Publish-UI ab) und ersatzweise direkt unter den Wurzel-Instanzen.
    Fehlt ein Wert, steht er nicht im Dict.
    """
    meta: Dict[str, float] = {}
    candidates: List[Dict[str, Any]] = []
    for rec in _iter_tree(roots):
        if rec.get("class") == "Animation":
            candidates.append(rec)
    if not candidates:
        candidates = list(roots)
    for parent in candidates:
        for child in parent.get("children") or ():
            name = child.get("name") or ""
            if name in THUMBNAIL_VALUE_NAMES and name not in meta:
                value = _record_value_num(child)
                if value is not None:
                    meta[name] = value
        if len(meta) == len(THUMBNAIL_VALUE_NAMES):
            break
    return meta


# ---------------------------------------------------------------------------
# KeyframeSequence (klassische/offizielle Emotes)
# ---------------------------------------------------------------------------

def _pose_cframe(rec: Dict[str, Any]) -> Optional[CFrame]:
    position = rec.get("position")
    if position is None:
        return None
    rotation = rec.get("rotation") or IDENTITY3
    return ((float(position[0]), float(position[1]), float(position[2])),
            _orthonormalize(rotation))


def _collect_poses(pose_rec: Dict[str, Any], out: Dict[str, CFrame]) -> None:
    """Pose (+ SubPoses) einsammeln; Weight 0 wird wie bei Roblox ignoriert."""
    weight = pose_rec.get("weight")
    if weight is None:
        weight = 1.0
    name = pose_rec.get("name") or ""
    if weight > 0.0 and name and name not in out:
        cframe = _pose_cframe(pose_rec)
        if cframe is not None:
            out[name] = cframe
    for child in pose_rec.get("children") or ():
        if child.get("class") in ("Pose", "SubPose"):
            _collect_poses(child, out)


def parse_keyframe_sequence(roots: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """KeyframeSequence-Instanzen -> ``{"typ": "keyframes", "keyframes": [...]}``.

    Keyframes werden nach ``Time`` sortiert (wie ``GetKeyframes()``); jeder
    Eintrag enthaelt ``{"time": t, "posen": {Teilname: CFrame}}``.
    """
    for rec in _iter_tree(roots):
        if rec.get("class") != "KeyframeSequence":
            continue
        keyframes: List[Dict[str, Any]] = []
        for child in rec.get("children") or ():
            if child.get("class") != "Keyframe":
                continue
            time = child.get("time")
            posen: Dict[str, CFrame] = {}
            for pose in child.get("children") or ():
                if pose.get("class") == "Pose":
                    _collect_poses(pose, posen)
            keyframes.append({"time": float(time if time is not None else 0.0),
                              "posen": posen})
        keyframes.sort(key=lambda k: k["time"])
        if keyframes:
            return {"typ": "keyframes", "keyframes": keyframes,
                    "name": rec.get("name") or ""}
    return None


# ---------------------------------------------------------------------------
# FloatCurve: ValuesAndTimes-Blob dekodieren und auswerten
# ---------------------------------------------------------------------------

class _ByteReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def _read(self, count: int) -> bytes:
        if self.pos + count > len(self.data):
            raise EmoteError("Curve-Daten sind abgeschnitten (ValuesAndTimes zu kurz)")
        chunk = self.data[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def u8(self) -> int:
        return self._read(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self._read(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self._read(4))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self._read(4))[0]


def decode_float_curve(blob: bytes) -> List[Dict[str, Any]]:
    """FloatCurve-``ValuesAndTimes`` -> Keys (zeitlich sortiert).

    Format (Roblox-Engine, ``Anim::FloatTrack``):

    ::

        u32 version (0..2)     # 0: Interpolation als u32, keine Flags
        u32 key_count
        key_count * { u8 interpolation (0=konstant,1=linear,2=kubisch)
                      u8 flags (Bit0: links-Tangente gesetzt, Bit1: rechts)
                      f32 wert, f32 links, f32 rechts }
        u32 tick_version
        u32 time_count
        time_count * ( v0: f32 sekunden | v1: i32 ticks / 2400 )
    """
    if not blob:
        return []
    try:
        reader = _ByteReader(blob)
        version = reader.u32()
        if version > 2:
            return []  # unbekannte Zukunftsversion
        count = reader.u32()
        keys: List[Dict[str, Any]] = []
        for _ in range(count):
            if version == 0:
                mode = reader.u32() & 0xFF
                flags = 3
            else:
                mode = reader.u8()
                flags = reader.u8()
            value = reader.f32()
            left = reader.f32()
            right = reader.f32()
            keys.append({
                "time": 0.0,
                "value": value,
                "interp": mode if mode in (0, 1, 2) else 2,
                "left": left if (flags & 1) and math.isfinite(left) else None,
                "right": right if (flags & 2) and math.isfinite(right) else None,
            })
        tick_version = reader.u32()
        time_count = reader.u32()
        times: List[float] = []
        for _ in range(time_count):
            if tick_version == 0:
                times.append(reader.f32())
            else:
                times.append(reader.i32() / TICKS_PER_SECOND)
        if len(times) != len(keys):
            return []
        for key, time in zip(keys, times):
            key["time"] = time
        keys.sort(key=lambda k: k["time"])
        return keys
    except (EmoteError, struct.error):
        return []


def decode_rotation_curve(blob: bytes) -> List[Dict[str, Any]]:
    """RotationCurve-``ValuesAndTimes`` (Quaternion-Keys) -> Keys.

    Format: u32 version(1), u32 count, dann pro Key 25 Bytes:
    u8 gepackt (Interpolation = Bit 0..1, Tangenten-Flags = Bit 2..3),
    f32 x/y/z/w, f32 links/rechts - danach die Ticks wie bei FloatCurve.
    """
    if not blob:
        return []
    try:
        reader = _ByteReader(blob)
        version = reader.u32()
        if version > 1:
            return []
        count = reader.u32()
        keys: List[Dict[str, Any]] = []
        for _ in range(count):
            if version == 0:
                mode = reader.u32() & 0xFF
                flags = 3
            else:
                packed = reader.u8()
                mode = packed & 3
                flags = (packed >> 2) & 3
            x, y, z, w = reader.f32(), reader.f32(), reader.f32(), reader.f32()
            left = reader.f32()
            right = reader.f32()
            keys.append({
                "time": 0.0,
                "quat": (x, y, z, w),
                "interp": mode if mode in (0, 1, 2) else 2,
                "left": left if (flags & 1) and math.isfinite(left) else None,
                "right": right if (flags & 2) and math.isfinite(right) else None,
            })
        tick_version = reader.u32()
        time_count = reader.u32()
        times: List[float] = []
        for _ in range(time_count):
            times.append(reader.f32() if tick_version == 0
                         else reader.i32() / TICKS_PER_SECOND)
        if len(times) != len(keys):
            return []
        for key, time in zip(keys, times):
            key["time"] = time
        keys.sort(key=lambda k: k["time"])
        return keys
    except (EmoteError, struct.error):
        return []


def _complete_tangents(keys: List[Dict[str, Any]]) -> Tuple[List[float], List[float]]:
    """Tangenten wie die Engine ergaenzen (``FloatTrack::Complete``)."""
    count = len(keys)
    left = [0.0] * count
    right = [0.0] * count

    def slope(a: int, b: int) -> float:
        span = keys[b]["time"] - keys[a]["time"]
        if span == 0.0:
            return 0.0
        return (keys[b]["value"] - keys[a]["value"]) / span

    def auto_tangent(index: int) -> float:
        last = count - 1
        interp = keys[index]["interp"]
        if interp == 1:  # linear
            return slope(index, index + 1) if index != last else 0.0
        if interp != 2 or index == 0 or index == last:
            return 0.0
        previous = keys[index - 1]
        if previous["interp"] == 1:
            return slope(index - 1, index)
        if previous["interp"] != 2:
            return 0.0
        following = keys[index + 1]
        if following["value"] == previous["value"]:
            return 0.0
        along = (keys[index]["value"] - previous["value"]) / (following["value"] - previous["value"])
        weight = min(1.0 - along, along) * 4.0
        if not weight > 0.0:
            return 0.0
        return min(1.0, weight) * ((slope(index - 1, index) + slope(index, index + 1)) * 0.5)

    for index, key in enumerate(keys):
        lt, rt = key.get("left"), key.get("right")
        after_cubic = index > 0 and keys[index - 1]["interp"] == 2
        if lt is not None and rt is not None:
            left[index], right[index] = lt, rt
        elif rt is not None:
            left[index] = right[index] = rt
        elif lt is not None and (index == 0 or after_cubic):
            left[index] = right[index] = (0.0 if index == 0 else lt)
        else:
            left[index] = right[index] = auto_tangent(index)
    return left, right


def _segment_at(keys: List[Dict[str, Any]], t: float) -> Tuple[int, int, float, float]:
    """Segment wie ``FindKeySegment``: (from, to, dauer, alpha)."""
    count = len(keys)
    if count == 1:
        return 0, 0, 0.0, 0.0
    after = 0
    while after < count and keys[after]["time"] <= t:
        after += 1
    to = 1 if after == 0 else min(after, count - 1)
    frm = to - 1
    start = keys[frm]["time"]
    duration = keys[to]["time"] - start
    alpha = 0.0
    if duration > 0.0:
        alpha = max(0.0, min(1.0, (t - start) / duration))
    return frm, to, duration, alpha


def _hermite(a: float, b: float, out_tangent: float, in_tangent: float,
             u: float) -> float:
    square = u * u
    cube = square * u
    return ((cube - square) * in_tangent
            + (u + cube - 2.0 * square) * out_tangent
            + (1.0 + 2.0 * cube - 3.0 * square) * a
            - (2.0 * cube - 3.0 * square) * b)


def evaluate_float_curve(keys: List[Dict[str, Any]], t: float) -> Optional[float]:
    """FloatCurve zum Zeitpunkt t auswerten (konstant/linear/kubisch Hermite)."""
    if not keys:
        return None
    if len(keys) == 1:
        value = keys[0]["value"]
        return value if math.isfinite(value) else None
    frm, to, duration, alpha = _segment_at(keys, t)
    if frm == to:
        value = keys[frm]["value"]
        return value if math.isfinite(value) else None
    mode = keys[frm]["interp"]
    if mode == 1:  # linear
        value = keys[to]["value"] * alpha + keys[frm]["value"] * (1.0 - alpha)
    elif mode == 2:  # kubisch (Hermite mit skalierten Tangenten)
        left, right = _complete_tangents(keys)
        value = _hermite(keys[frm]["value"], keys[to]["value"],
                         right[frm] * duration, left[to] * duration, alpha)
    else:  # konstant
        value = keys[to]["value"] if alpha >= 1.0 else keys[frm]["value"]
    return value if math.isfinite(value) else None


def _slerp_shortest(a: Tuple[float, float, float, float],
                    b: Tuple[float, float, float, float],
                    u: float) -> Tuple[float, float, float, float]:
    dot = sum(x * y for x, y in zip(a, b))
    if dot < 0.0:
        b = tuple(-v for v in b)  # type: ignore[assignment]
        dot = -dot
    dot = max(-1.0, min(1.0, dot))
    if dot > 0.9995:
        mixed = tuple(x + (y - x) * u for x, y in zip(a, b))  # type: ignore[assignment]
        length = math.sqrt(sum(v * v for v in mixed)) or 1.0
        return tuple(v / length for v in mixed)  # type: ignore[return-value]
    theta = math.acos(dot)
    sin_theta = math.sin(theta)
    wa = math.sin((1.0 - u) * theta) / sin_theta
    wb = math.sin(u * theta) / sin_theta
    return tuple(wa * x + wb * y for x, y in zip(a, b))  # type: ignore[return-value]


def evaluate_rotation_curve(keys: List[Dict[str, Any]], t: float) -> Mat3:
    """Quaternion-RotationCurve zum Zeitpunkt t (Slerp wie die Engine)."""
    if not keys:
        return IDENTITY3
    if len(keys) == 1:
        quat = keys[0]["quat"]
        return _quat_to_mat3(*quat)
    frm, to, _duration, alpha = _segment_at(keys, t)
    if frm == to:
        return _quat_to_mat3(*keys[frm]["quat"])
    if keys[frm]["interp"] == 0:  # konstant
        quat = keys[to]["quat"] if alpha >= 1.0 else keys[frm]["quat"]
        return _quat_to_mat3(*quat)
    quat = _slerp_shortest(keys[frm]["quat"], keys[to]["quat"], alpha)
    return _quat_to_mat3(*quat)


# ---------------------------------------------------------------------------
# CurveAnimation (moderne UGC-Emotes)
# ---------------------------------------------------------------------------

def _float_curve_child(rec: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    for child in rec.get("children") or ():
        if child.get("class") == "FloatCurve" and (child.get("name") or "") == name:
            return decode_float_curve(child.get("values_and_times") or b"")
    return []


def parse_curve_animation(roots: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """CurveAnimation-Instanz -> ``{"typ": "curves", "gelenke": {...}}``.

    Struktur (Roblox-Engine): Ordner je R15-Teil (z. B. ``HumanoidRootPart``
    mit Kindern ``LowerTorso``, ``UpperTorso``, ...), darin ``Position``
    (Vector3Curve mit X/Y/Z-FloatCurves) und ``Rotation``
    (EulerRotationCurve mit X/Y/Z-FloatCurves + ``RotationOrder`` oder
    eine Quaternion-RotationCurve). Eulerwerte stehen im **Radiant**.
    """
    for rec in _iter_tree(roots):
        if rec.get("class") != "CurveAnimation":
            continue
        gelenke: Dict[str, Dict[str, Any]] = {}
        for folder in _iter_tree(rec.get("children") or ()):
            if folder.get("class") != "Folder":
                continue
            name = folder.get("name") or ""
            if not name:
                continue
            entry: Dict[str, Any] = {"position": None, "rotation": None}
            for child in folder.get("children") or ():
                cls = child.get("class") or ""
                cname = child.get("name") or ""
                if cls == "Vector3Curve" and cname == "Position":
                    entry["position"] = {
                        axis: _float_curve_child(child, axis)
                        for axis in ("X", "Y", "Z")
                    }
                elif cls == "EulerRotationCurve" and cname == "Rotation":
                    order = child.get("rotation_order")
                    entry["rotation"] = {
                        "art": "euler",
                        "ordnung": order if order in _ROTATION_ORDERS else 0,
                        "achsen": {axis: _float_curve_child(child, axis)
                                   for axis in ("X", "Y", "Z")},
                    }
                elif cls == "RotationCurve" and cname == "Rotation":
                    entry["rotation"] = {
                        "art": "quaternion",
                        "keys": decode_rotation_curve(child.get("values_and_times") or b""),
                    }
            has_data = (
                (entry["position"] and any(entry["position"][axis] for axis in ("X", "Y", "Z")))
                or (entry["rotation"] and (
                    (entry["rotation"]["art"] == "euler"
                     and any(entry["rotation"]["achsen"][axis] for axis in ("X", "Y", "Z")))
                    or (entry["rotation"]["art"] == "quaternion" and entry["rotation"]["keys"])))
            )
            if has_data:
                bisher = gelenke.get(name)
                if bisher is not None:
                    # Gleicher Ordnername mehrfach: erster Treffer gewinnt.
                    continue
                gelenke[name] = entry
        if gelenke:
            return {"typ": "curves", "gelenke": gelenke,
                    "name": rec.get("name") or "",
                    "laenge": _curve_length(gelenke)}
    return None


def _curve_length(gelenke: Dict[str, Dict[str, Any]]) -> float:
    """Dauer = groesste Key-Zeit ueber alle FloatCurves (wie EmoteUtility)."""
    laenge = 0.0

    def scan(curves: Dict[str, List[Dict[str, Any]]]) -> None:
        nonlocal laenge
        for keys in curves.values():
            if keys:
                laenge = max(laenge, keys[-1]["time"])

    for entry in gelenke.values():
        if entry.get("position"):
            scan(entry["position"])
        rotation = entry.get("rotation")
        if rotation:
            if rotation["art"] == "euler":
                scan(rotation["achsen"])
            elif rotation["keys"]:
                laenge = max(laenge, rotation["keys"][-1]["time"])
    return laenge


def sample_curve_animation(clip: Dict[str, Any], t: float) -> Dict[str, CFrame]:
    """CurveAnimation zum Zeitpunkt t samplen -> {Teilname: CFrame}."""
    gelenke = clip.get("gelenke") or {}
    posen: Dict[str, CFrame] = {}
    for name, entry in gelenke.items():
        position: Vec3 = (0.0, 0.0, 0.0)
        rotation: Mat3 = IDENTITY3
        pos_curves = entry.get("position")
        if pos_curves:
            xs = evaluate_float_curve(pos_curves["X"], t)
            ys = evaluate_float_curve(pos_curves["Y"], t)
            zs = evaluate_float_curve(pos_curves["Z"], t)
            position = (xs or 0.0, ys or 0.0, zs or 0.0)
        rot = entry.get("rotation")
        if rot:
            if rot["art"] == "euler":
                rx = evaluate_float_curve(rot["achsen"]["X"], t) or 0.0
                ry = evaluate_float_curve(rot["achsen"]["Y"], t) or 0.0
                rz = evaluate_float_curve(rot["achsen"]["Z"], t) or 0.0
                rotation = euler_matrix_rbx(rx, ry, rz, rot.get("ordnung") or 0)
            else:
                rotation = evaluate_rotation_curve(rot["keys"], t)
        posen[name] = (position, rotation)
    return posen


# ---------------------------------------------------------------------------
# Frame-Auswahl: exakt die Regeln von Roblox' Thumbnail-Rendern
# ---------------------------------------------------------------------------

def select_keyframe_index(count: int, thumbnail_keyframe: Optional[float]) -> int:
    """Index (0-basiert) des Thumbnail-Keyframes in einer KeyframeSequence.

    Roblox-Regel (EmoteUtility.GetThumbnailKeyframe / AnimationSilhouette):
    gueltige 1-basierte Nummer -> dieser Keyframe; sonst der Mittel-Keyframe
    ``ceil(count / 2)``.
    """
    n = int(thumbnail_keyframe) if thumbnail_keyframe is not None else 0
    if 0 < n <= count:
        return n - 1
    return max(0, math.ceil(count / 2) - 1)


def select_sample_time(laenge: float, thumbnail_time: Optional[float]) -> float:
    """Sample-Zeit fuer CurveAnimations (Roblox-Regel).

    Gueltige ``ThumbnailTime`` (0..Dauer) -> diese Zeit; sonst **halbe
    Dauer** (EmoteUtility.GetThumbnailKeyframeFromCurve).
    """
    t = float(thumbnail_time) if thumbnail_time is not None else -1.0
    if 0.0 <= t <= laenge:
        return t
    return laenge / 2.0


def closest_keyframe_index(keyframes: List[Dict[str, Any]], zeit: float) -> int:
    """Index des Keyframes, dessen Time einer Wunschzeit am naechsten liegt."""
    if not keyframes:
        return 0
    best = 0
    best_dist = float("inf")
    for index, keyframe in enumerate(keyframes):
        dist = abs(keyframe["time"] - zeit)
        if dist < best_dist:
            best_dist = dist
            best = index
    return best


# ---------------------------------------------------------------------------
# Pose-CFrames -> internes PoseContext-Format
# ---------------------------------------------------------------------------

def pose_cframes_to_pose_dict(posen: Dict[str, CFrame]
                              ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """{R15-Teilname: CFrame} -> normalisiertes Posen-Dict fuer PoseContext.

    * ``HumanoidRootPart``-Posen (Root-Motion) werden mit ``LowerTorso``
      kombiniert - unser Wurzelknochen ist ``unterer_torso``.
    * Unbekannte Namen (z. B. R6-``Torso``) werden gemeldet, aber uebergangen.
    * Mathematik: RCC wendet ``C1' = C1 * pose^-1`` an, d. h. das Teil
      bekommt lokal ``T(pivot) * pose * T(-pivot)``. Unser PoseContext baut
      ``T(offset) * T(pivot) * R * T(-pivot)`` - mit identischen Rest-
      Orientierungen ist das mit ``R = pose.R`` und ``offset = pose.t``
      exakt dieselbe Transformation.
    """
    posen = dict(posen)
    root = posen.pop("HumanoidRootPart", None)
    if root is not None:
        lower = posen.get("LowerTorso")
        posen["LowerTorso"] = _cframe_mul(root, lower) if lower is not None else root

    out: Dict[str, Dict[str, Any]] = {}
    unbekannt: List[str] = []
    for name, (trans, rot) in sorted(posen.items()):
        bone = POSE_BONE_NAMES.get(name)
        if bone is None:
            unbekannt.append(name)
            continue
        euler = mat3_to_euler_deg(rot)
        offset = (float(trans[0]), float(trans[1]), float(trans[2]))
        if not all(math.isfinite(v) for v in (*euler, *offset)):
            raise EmoteError(
                f"Emote enthaelt ungueltige Werte fuer '{name}' "
                f"(Translation {offset!r}).")
        if max(abs(v) for v in offset) > MAX_OFFSET_STUDS:
            raise EmoteError(
                f"Emote bewegt '{name}' um {max(abs(v) for v in offset):.1f} Studs "
                "- das sieht nach fehlerhaften Daten aus (Root-Bewegung ist bei "
                "Emotes stark begrenzt).")
        winkel = round(max(abs(v) for v in euler), 1)
        out[bone] = {
            "rotation": euler,
            "verschiebung": offset,
            "felder": {"drehung_grad": winkel},
        }
    return out, unbekannt


# ---------------------------------------------------------------------------
# Asset laden (Datei oder Download) und AnimationId verfolgen
# ---------------------------------------------------------------------------

def _read_emote_bytes(datei: str, emote_id: Optional[int], client: Any,
                      log: Callable[[str], None]) -> bytes:
    if datei:
        try:
            with open(datei, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            raise EmoteError(f"Emote-Datei '{datei}' konnte nicht gelesen werden: {exc}") from exc
        if not data:
            raise EmoteError(f"Emote-Datei '{datei}' ist leer.")
        log(f"      Emote aus Datei: {datei} ({len(data) / 1024:.1f} KB)")
        return data
    if client is None:
        raise EmoteError(
            "Emote-Pose braucht Netzwerk (kein Client, kein 'emote_datei' "
            "in der Configs.toml).")
    if emote_id is None:  # pragma: no cover - Referenz-Parsing stellt das sicher
        raise EmoteError("Keine Emote-Asset-ID gegeben.")
    asset = api.download_asset(client, emote_id)
    if not asset.data:
        raise EmoteError(
            f"Emote-Asset {emote_id} konnte nicht geladen werden "
            f"({asset.note or 'unbekannter Fehler'}). Ist die ID korrekt und "
            "der Secret ROBLOX_OPEN_CLOUD_API_KEY (Scope legacy-asset:manage) "
            "gesetzt?")
    log(f"      Emote-Asset {emote_id} geladen ({len(asset.data) / 1024:.1f} KB)")
    return asset.data


def _download_clip_bytes(asset_id: int, client: Any, log: Callable[[str], None]) -> bytes:
    if client is None:
        # z. B. Dry-Run mit lokaler emote_datei: Fuer den nachgeladenen Clip
        # reicht ein kurzlebiger Client. Ohne Netzwerk/API-Key schlaegt das
        # unten einfach mit einer klaren Meldung fehl (Warnung + Stand-Pose).
        client = api.HttpClient(verbose=False)
    asset = api.download_asset(client, asset_id)
    if not asset.data:
        raise EmoteError(
            f"Animations-Asset {asset_id} (aus der AnimationId des Emotes) "
            f"konnte nicht geladen werden ({asset.note or 'unbekannter Fehler'}).")
    log(f"      Animations-Asset {asset_id} geladen ({len(asset.data) / 1024:.1f} KB)")
    return asset.data


def parse_animation_clip(roots: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Sucht KeyframeSequence/CurveAnimation im Instanz-Baum (egal wo)."""
    clip = parse_keyframe_sequence(roots)
    if clip is not None:
        return clip
    return parse_curve_animation(roots)


def _find_animation_records(roots: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for rec in _iter_tree(roots):
        if rec.get("class") == "Animation" and rec.get("animation_url"):
            out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Zusammenfassung fuer Log/Manifest
# ---------------------------------------------------------------------------

def _fmt_de(value: float, nd: int = 2) -> str:
    return f"{value:.{nd}f}".replace(".", ",")


def emote_summary(info: Dict[str, Any]) -> str:
    """Kurze, lesbare Zusammenfassung einer Emote-Pose fuer Log/Manifest."""
    if not info:
        return "Emote-Pose"
    name = info.get("name") or ""
    quoted = f"'{name}' " if name else ""
    typ = (info.get("typ") or "").replace("_", " ")
    parts = [f"Emote {quoted}({info.get('emote_id', '?')}), {typ}"]
    if info.get("keyframes"):
        parts.append(f"Keyframe {info.get('frame', 0) + 1}/{info['keyframes']}")
    if info.get("zeit_s") is not None:
        parts.append(f"bei {_fmt_de(info['zeit_s'])} s")
    if info.get("auswahl"):
        parts.append(f"({info['auswahl']})")
    gelenke = info.get("gelenke") or 0
    parts.append(f"{gelenke} Gelenke")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Oeffentlicher Gesamtablauf
# ---------------------------------------------------------------------------

def load_emote_pose(emote_id: Optional[int] = None,
                    client: Any = None,
                    datei: str = "",
                    zeit: Optional[float] = None,
                    log: Optional[Callable[[str], None]] = None
                    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, float], Dict[str, Any]]:
    """Laedt ein Emote und baut daraus die Pose fuer ``avatar_pose.PoseContext``.

    Ablauf:

    1. Emote-Asset laden (Datei ``datei`` oder Download per ``client``).
    2. Thumbnail-Metadaten (``ThumbnailKeyframe``/``ThumbnailTime``/...) aus
       der ``Animation``-Instanz lesen.
    3. Falls noetig der ``AnimationId`` folgen (KeyframeSequence- bzw.
       CurveAnimation-Asset nachladen, inkl. Weiterleitungen).
    4. Frame waehlen - Rangfolge: ``zeit`` (manueller Override aus der
       Configs.toml), dann Thumbnail-Metadaten, dann Roblox-Fallback
       (Mittel-Keyframe / halbe Dauer).
    5. Pose-CFrames extrahieren und in das interne Pose-Format uebersetzen.

    Rueckgabe: ``(pose_dict, einstellungen, info)``. Fehler -> :class:`EmoteError`.
    """
    if log is None:
        def log(msg: str) -> None:  # noqa: F811
            print(msg, flush=True)

    data = _read_emote_bytes(datei, emote_id, client, log)
    try:
        roots = rbxm.parse_instances(data)
    except rbxm.RbxmError as exc:
        raise EmoteError(f"Emote-Daten sind kein lesbares RBXM/RBXMX: {exc}") from exc

    clip = parse_animation_clip(roots)
    meta = read_thumbnail_metadata(roots)
    hops = 0
    while clip is None and hops < MAX_DOWNLOAD_HOPS:
        # AnimationId folgen (Roblox-Katalog-Emotes verweisen auf ein
        # separates KeyframeSequence-/CurveAnimation-Asset).
        animation_recs = _find_animation_records(roots)
        next_id = None
        for rec in animation_recs:
            next_id = rbxm.parse_asset_id(rec.get("animation_url"))
            if next_id:
                break
        if not next_id:
            raise EmoteError(
                "Im Emote-Asset wurde weder eine KeyframeSequence/CurveAnimation "
                "noch eine Animation mit AnimationId gefunden - vermutlich keine "
                "Emote-Asset-ID.")
        data = _download_clip_bytes(next_id, client, log)
        try:
            roots = rbxm.parse_instances(data)
        except rbxm.RbxmError as exc:
            raise EmoteError(
                f"Animations-Asset {next_id} ist kein lesbares RBXM/RBXMX: {exc}"
            ) from exc
        clip = parse_animation_clip(roots)
        if not meta:
            meta = read_thumbnail_metadata(roots)
        hops += 1

    if clip is None:
        raise EmoteError(
            "Die Animationsdaten des Emotes konnten nicht gelesen werden "
            "(weder KeyframeSequence noch CurveAnimation).")

    info: Dict[str, Any] = {
        "emote_id": emote_id,
        "name": clip.get("name") or "",
        "typ": clip["typ"],
        "thumbnail": meta,
    }

    # ---- Frame waehlen (Roblox-Regeln, mit manuellem Override) ------------
    posen: Dict[str, CFrame]
    if clip["typ"] == "keyframes":
        keyframes = clip["keyframes"]
        info["keyframes"] = len(keyframes)
        info["dauer_s"] = keyframes[-1]["time"] if keyframes else 0.0
        if zeit is not None:
            index = closest_keyframe_index(keyframes, zeit)
            info["auswahl"] = f"emote_zeit {_fmt_de(zeit)} s (naechster Keyframe)"
        else:
            index = select_keyframe_index(len(keyframes), meta.get("ThumbnailKeyframe"))
            if "ThumbnailKeyframe" in meta and 0 < int(meta["ThumbnailKeyframe"]) <= len(keyframes):
                info["auswahl"] = f"ThumbnailKeyframe {int(meta['ThumbnailKeyframe'])}"
            else:
                info["auswahl"] = "Mittel-Keyframe (Roblox-Fallback)"
        info["frame"] = index
        info["zeit_s"] = keyframes[index]["time"] if keyframes else 0.0
        posen = keyframes[index]["posen"] if keyframes else {}
    else:
        laenge = clip.get("laenge") or 0.0
        info["dauer_s"] = laenge
        if zeit is not None:
            t = max(0.0, min(zeit, laenge))
            info["auswahl"] = f"emote_zeit {_fmt_de(zeit)} s"
        else:
            t = select_sample_time(laenge, meta.get("ThumbnailTime"))
            if "ThumbnailTime" in meta and 0.0 <= meta["ThumbnailTime"] <= laenge:
                info["auswahl"] = f"ThumbnailTime {_fmt_de(meta['ThumbnailTime'])} s"
            else:
                info["auswahl"] = "halbe Dauer (Roblox-Fallback)"
        info["zeit_s"] = t
        posen = sample_curve_animation(clip, t)

    if not posen:
        raise EmoteError("Das Emote enthaelt keine Pose-Daten fuer R15-Gelenke.")

    pose_dict, unbekannt = pose_cframes_to_pose_dict(posen)
    info["gelenke"] = sorted(pose_dict)
    info["gelenke_anzahl"] = len(pose_dict)
    info["unbekannte_gelenke"] = unbekannt
    if unbekannt:
        log("      ! Nicht-R15-Gelenke im Emote (ignoriert): "
            + ", ".join(sorted(set(unbekannt))))

    if not pose_dict:
        raise EmoteError(
            "Keine der Emote-Posen passt auf das R15-Rig (z. B. ein R6-Emote). "
            "Emotes muessen laut Roblox-Spezifikation fuer das Standard-R15 "
            "erstellt sein.")

    settings = dict(avatar_pose.POSE_SETTING_DEFAULTS)
    log(f"      Emote-Pose: {emote_summary(info)}")
    return pose_dict, settings, info
