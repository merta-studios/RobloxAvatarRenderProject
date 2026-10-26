"""Exakter R15-Standard-Koerper (Blocky) als Rig-Spezifikation.

**Quelle:** Roblox' offizielle Referenzdatei ``BlockyCharacter.fbx``
(``create.roblox.com/docs/avatar/resources`` -> "Standard R15 rigs" ->
**Blocky** - das Template, das laut Doku "a standard R15 rig and
attachments" enthaelt). Sie liegt vollstaendig in
``reference/official/BlockyCharacter.fbx`` und wird hier mit dem
FBX-Reader (``tools/fbx.py``) in eine kompakte Rig-Spezifikation
(``reference/standard_r15_rig.json``) uebersetzt.

Damit wird nichts mehr geschaetzt, wenn ein Koerperteil nicht ausgeruestet
ist:

* **Geometrie**: die 15 ``*_Geo``-Meshes des Blocky-Koerpers (in Teil-
  Koordinaten, Ursprung = BBox-Mittelpunkt).
* **Gelenke**: die Skelett-Knochen der FBX (Waist, Neck, Shoulder, Elbow,
  Wrist, Hip, Knee, Ankle) - als ``*RigAttachment``-Paare, damit die
  Montage ueber die Roblox-Motor6D-Semantik
  (``Kind = Eltern * C0 * C1^-1``) exakt den Bind-Pose-Zustand ergibt.
  Jede Pose dreht damit um das echte Gelenk (Pivot), nicht um geratene
  Punkte.
* **Accessoire-Anker**: die ``*_Att``-Marker der FBX (Hat_Att, Hair_Att,
  FaceFront_Att, ...) als ``HatAttachment``/``FaceFrontAttachment``/... -
  exakt die Attachments, an denen Roblox Accessoire-Meshes verschweisst.
  Brillen sitzen damit so wie im Spiel, weil BEIDE Seiten (Koerper-Anker
  und Accessoire-Attachment) aus Roblox-Originaldaten stammen.

Koordinaten: Roblox-Konvention (X = rechts, Y = hoch, Z = hinten,
Gesicht zeigt nach -Z). Die FBX steht in Maya-Konvention (Gesicht +Z,
linke Seite +X); umgerechnet wird mit einer 180-Grad-Drehung um Y.

Aufbau:

    python tools/standard_r15.py            # JSON aus der FBX neu bauen
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
FBX_PATH = os.path.join(ROOT, "reference", "official", "BlockyCharacter.fbx")
JSON_PATH = os.path.join(ROOT, "reference", "standard_r15_rig.json")

Vec3 = Tuple[float, float, float]
Mat3 = Tuple[Vec3, Vec3, Vec3]

IDENTITY: Mat3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))

#: Skelett der FBX (Elternteil -> Kind) mit dem RigAttachment-Namen des
#: Gelenks zwischen ihnen (Roblox ``Humanoid:BuildRigFromAttachments``).
SKELETON: Dict[str, Tuple[Optional[str], str]] = {
    #  kind: (Elternteil, RigAttachment-Name des Gelenks)
    "LowerTorso": (None, "RootRigAttachment"),
    "UpperTorso": ("LowerTorso", "WaistRigAttachment"),
    "Head": ("UpperTorso", "NeckRigAttachment"),
    "LeftUpperArm": ("UpperTorso", "LeftShoulderRigAttachment"),
    "LeftLowerArm": ("LeftUpperArm", "LeftElbowRigAttachment"),
    "LeftHand": ("LeftLowerArm", "LeftWristRigAttachment"),
    "RightUpperArm": ("UpperTorso", "RightShoulderRigAttachment"),
    "RightLowerArm": ("RightUpperArm", "RightElbowRigAttachment"),
    "RightHand": ("RightLowerArm", "RightWristRigAttachment"),
    "LeftUpperLeg": ("LowerTorso", "LeftHipRigAttachment"),
    "LeftLowerLeg": ("LeftUpperLeg", "LeftKneeRigAttachment"),
    "LeftFoot": ("LeftLowerLeg", "LeftAnkleRigAttachment"),
    "RightUpperLeg": ("LowerTorso", "RightHipRigAttachment"),
    "RightLowerLeg": ("RightUpperLeg", "RightKneeRigAttachment"),
    "RightFoot": ("RightLowerLeg", "RightAnkleRigAttachment"),
}

#: ``*_Att``-Marker der FBX -> (Roblox-Attachment-Name, zustaendiges Teil).
#: Die Marker haengen in der FBX am Skelett-Knochen; zustaendig ist das
#: Koerperteil, zu dem der Knochen gehoert (Neck_Att sitzt am Kopf-Knochen,
#: wie beim echten Standard-Avatar, bei dem ``NeckAttachment`` im Kopf ist).
ATTACHMENT_MARKERS: Dict[str, Tuple[str, str]] = {
    "Hat_Att": ("HatAttachment", "Head"),
    "Hair_Att": ("HairAttachment", "Head"),
    "FaceFront_Att": ("FaceFrontAttachment", "Head"),
    "FaceCenter_Att": ("FaceCenterAttachment", "Head"),
    "Neck_Att": ("NeckAttachment", "Head"),
    "LeftCollar_Att": ("LeftCollarAttachment", "UpperTorso"),
    "RightCollar_Att": ("RightCollarAttachment", "UpperTorso"),
    "BodyFront_Att": ("BodyFrontAttachment", "UpperTorso"),
    "BodyBack_Att": ("BodyBackAttachment", "UpperTorso"),
    "WaistFront_Att": ("WaistFrontAttachment", "LowerTorso"),
    "WaistBack_Att": ("WaistBackAttachment", "LowerTorso"),
    "WaistCenter_Att": ("WaistCenterAttachment", "LowerTorso"),
    "LeftShoulder_Att": ("LeftShoulderAttachment", "LeftUpperArm"),
    "RightShoulder_Att": ("RightShoulderAttachment", "RightUpperArm"),
    "LeftGrip_Att": ("LeftGripAttachment", "LeftHand"),
    "RightGrip_Att": ("RightGripAttachment", "RightHand"),
    "LeftFoot_Att": ("LeftFootAttachment", "LeftFoot"),
    "RightFoot_Att": ("RightFootAttachment", "RightFoot"),
}

#: Reihenfolge der 15 Koerperteile (wie in ``mannequin.PART_NAMES``).
PART_NAMES: Tuple[str, ...] = (
    "UpperTorso", "LowerTorso", "Head",
    "LeftUpperArm", "LeftLowerArm", "LeftHand",
    "RightUpperArm", "RightLowerArm", "RightHand",
    "LeftUpperLeg", "LeftLowerLeg", "LeftFoot",
    "RightUpperLeg", "RightLowerLeg", "RightFoot",
)


# ---------------------------------------------------------------------------
# FBX (Maya) -> Roblox
# ---------------------------------------------------------------------------

def _maya_to_roblox_pos(p: Vec3) -> Vec3:
    """180-Grad-Drehung um Y: Gesicht +Z -> -Z, linke Seite +X -> -X."""
    return (-p[0], p[1], -p[2])


def _maya_to_roblox_mat(m: Mat3) -> Mat3:
    """R' = C * R * C mit C = Ry(180) = diag(-1, 1, -1)."""
    return (
        (m[0][0], -m[0][1], m[0][2]),
        (-m[1][0], m[1][1], -m[1][2]),
        (m[2][0], -m[2][1], m[2][2]),
    )


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


# ---------------------------------------------------------------------------
# Aufbau aus der FBX
# ---------------------------------------------------------------------------

def _clean(name: str) -> str:
    return name.split("\x00")[0].split("::")[0]


def build_spec(fbx_path: str = FBX_PATH) -> Dict[str, Any]:
    """Liest ``BlockyCharacter.fbx`` und baut die Rig-Spezifikation."""
    import fbx  # lokal, damit das Modul ohne FBX-Parser importierbar bleibt

    doc = fbx.read_document(fbx_path)

    # Welt-Frames der Model-Knoten (Knochen + Marker) in Roblox-Koordinaten.
    frames: Dict[str, Tuple[Vec3, Mat3]] = {}
    for name, (pos, rot) in doc.marker_frames.items():
        frames[name] = (_maya_to_roblox_pos(pos), _maya_to_roblox_mat(rot))

    # Geometrie je Teil (Weltkoordinaten des Bind-Pose-Zustands).
    geo_by_name = {_clean(p.name): p for p in doc.parts}
    parts: Dict[str, Any] = {}

    def part_mesh(part_name: str):
        part = geo_by_name.get(f"{part_name}_Geo") or geo_by_name.get(part_name)
        return part

    # Zunaechst alle BBox-Mittelpunkte (Urspruenge der Teil-Koordinaten).
    centers: Dict[str, Vec3] = {}
    for name in PART_NAMES:
        part = part_mesh(name)
        if part is None or not part.positions:
            continue
        lo, hi = part.bbox()
        centers[name] = _maya_to_roblox_pos(
            ((lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0, (lo[2] + hi[2]) / 2.0))

    attachments_by_part: Dict[str, Dict[str, Any]] = {n: {} for n in PART_NAMES}

    def add_attachment(part_name: str, att_name: str, world_pos: Vec3,
                       world_rot: Mat3) -> None:
        center = centers.get(part_name)
        if center is None:
            return
        # Teil-Koordinaten: Ursprung = BBox-Mittelpunkt, Achsen = Weltrichtung
        # (Bind-Pose). Rotation: Weltrichtung des Attachments.
        attachments_by_part.setdefault(part_name, {})[att_name] = {
            "position": [round(v, 5) for v in _sub(world_pos, center)],
            "rotation": [[round(c, 6) for c in row] for row in world_rot],
        }

    # 1) Gelenke aus dem Skelett: pro Knochen die RigAttachments auf dem
    #    Kind- UND Elternteil (Roblox setzt beide auf denselben Punkt).
    for bone, (parent, att_name) in SKELETON.items():
        frame = frames.get(bone)
        if frame is None:
            continue
        pos, rot = frame
        if bone in attachments_by_part:
            add_attachment(bone, att_name, pos, rot)
        if parent and parent in attachments_by_part:
            add_attachment(parent, att_name, pos, rot)

    # 2) Accessoire-Anker aus den *_Att-Markern.
    for marker, (att_name, owner) in ATTACHMENT_MARKERS.items():
        frame = frames.get(marker)
        if frame is None:
            continue
        pos, rot = frame
        add_attachment(owner, att_name, pos, rot)

    # 3) Meshes in Teil-Koordinaten (zusammen mit Attachments speichern).
    for name in PART_NAMES:
        part = part_mesh(name)
        if part is None or not part.positions:
            continue
        center = centers[name]
        positions = [_sub(_maya_to_roblox_pos(p), center) for p in part.positions]
        lo = [min(p[i] for p in positions) for i in range(3)]
        hi = [max(p[i] for p in positions) for i in range(3)]
        parts[name] = {
            "size": [round(hi[i] - lo[i], 5) for i in range(3)],
            "vertices": [round(c, 5) for p in positions for c in p],
            "faces": [i for f in part.faces for i in f],
            "uvs": [round(c, 5) for uv in (part.uvs or []) for c in uv],
            "attachments": attachments_by_part.get(name, {}),
        }

    return {
        "source": "BlockyCharacter.fbx (create.roblox.com/docs/avatar/resources,"
                  " Standard R15 rigs -> Blocky)",
        "space": "roblox-teilkoordinaten: Ursprung = BBox-Mittelpunkt,"
                 " Achsen = Weltachsen der Bind-Pose (X rechts, Y hoch,"
                 " Z hinten)",
        "parts": parts,
    }


def dump(spec: Dict[str, Any], path: str = JSON_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, separators=(",", ":"))
    print(f"  -> {path} ({os.path.getsize(path) / 1024:.0f} KB)")


# ---------------------------------------------------------------------------
# Laden zur Renderzeit
# ---------------------------------------------------------------------------

_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}


def load(path: str = JSON_PATH) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def cached(path: str = JSON_PATH) -> Optional[Dict[str, Any]]:
    if path not in _CACHE:
        _CACHE[path] = load(path)
    return _CACHE[path]


def standard_segments(path: str = JSON_PATH) -> Dict[str, Dict[str, Any]]:
    """Segmente des Standard-Avatars im RBXM-Format fuer ``avatar_rig``.

    ``{Segmentname: {"mesh": MeshData, "attachments": {Name: {position,
    rotation}}}}`` - damit montiert ``avatar_rig.assemble_r15_rig`` fehlende
    Koerperteile genauso wie ausgeruestete: ueber die exakten
    ``*RigAttachment``-Paare (Motor6D-Semantik), ohne geschaetzte
    Gelenkfraktionen.
    """
    from roblox_mesh import MeshData  # lokal, kein Import-Zyklus zur Laufzeit

    spec = cached(path)
    if not spec:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for name, part in (spec.get("parts") or {}).items():
        flat = part.get("vertices") or []
        flat_f = part.get("faces") or []
        flat_uv = part.get("uvs") or []
        positions = [(flat[i], flat[i + 1], flat[i + 2])
                     for i in range(0, len(flat) - 2, 3)]
        faces = [(flat_f[i], flat_f[i + 1], flat_f[i + 2])
                 for i in range(0, len(flat_f) - 2, 3)]
        uvs = [(flat_uv[i], flat_uv[i + 1])
               for i in range(0, len(flat_uv) - 1, 2)]
        atts: Dict[str, Any] = {}
        for aname, ainfo in (part.get("attachments") or {}).items():
            pos = ainfo.get("position") or (0.0, 0.0, 0.0)
            rot = ainfo.get("rotation")
            atts[aname] = {
                "position": (float(pos[0]), float(pos[1]), float(pos[2])),
                "rotation": tuple(tuple(float(c) for c in row)
                                  for row in rot) if rot else None,
            }
        out[name] = {
            "mesh": MeshData(positions=positions, normals=[], uvs=uvs,
                             faces=faces, source_version="r15-standard-blocky"),
            "attachments": atts,
        }
    return out


# ---------------------------------------------------------------------------
# Abgeleitete Proportionen (fuer die BBox-Schaetzung in avatar_pose)
# ---------------------------------------------------------------------------

#: Exakte Kopf-Anker des Standard-Rigs, normiert auf die halbe Kopfhoehe.
#: Quelle: ``BlockyCharacter.fbx`` (FaceFront_Att auf Kopf-Mitte an der
#: Vorderflaeche, Hat_Att/Hair_Att auf der Oberkante, Neck_Att unter dem
#: Kinn). Letzter Notfall-Wert, falls die Spezifikationsdatei fehlt.
FALLBACK_HEAD_ATTACHMENTS: Dict[str, Tuple[Vec3, Mat3]] = {
    "FaceFrontAttachment": ((0.0, 0.001, -0.998), IDENTITY),
    "FaceCenterAttachment": ((0.0, -0.156, 0.0), IDENTITY),
    "HatAttachment": ((0.0, 1.009, 0.0), IDENTITY),
    "HairAttachment": ((0.0, 1.009, 0.0), IDENTITY),
    "NeckAttachment": ((0.0, -1.465, 0.0), IDENTITY),
}


def head_attachments(head_center: Vec3, head_radius: float) -> Dict[str, Tuple[Vec3, Mat3]]:
    """Kopf-Anker des Standard-Rigs auf einen Kopf beliebiger Groesse skaliert.

    Wird benutzt, wenn ein ausgeruesteter Kopf keine eigenen
    ``*Attachment``-Kinder mitbringt: dann sitzen Hut, Brille & Co. exakt
    dort, wo Roblox sie am Standard-Kopf hat (nicht auf geratenen Punkten).
    """
    spec = cached()
    head = ((spec or {}).get("parts") or {}).get("Head") or {}
    size = head.get("size") or (1.2, 1.2, 1.2)
    r0 = float(size[1]) / 2.0
    scale = float(head_radius) / max(1e-6, r0)
    out: Dict[str, Tuple[Vec3, Mat3]] = {}
    for name, info in (head.get("attachments") or {}).items():
        if name.endswith("RigAttachment"):
            continue
        p = info.get("position") or (0.0, 0.0, 0.0)
        rot = info.get("rotation")
        out[name] = (
            (head_center[0] + float(p[0]) * scale,
             head_center[1] + float(p[1]) * scale,
             head_center[2] + float(p[2]) * scale),
            tuple(tuple(float(c) for c in row) for row in rot) if rot else IDENTITY,
        )
    if not out:
        for name, (p, rot) in FALLBACK_HEAD_ATTACHMENTS.items():
            out[name] = (
                (head_center[0] + p[0] * head_radius,
                 head_center[1] + p[1] * head_radius,
                 head_center[2] + p[2] * head_radius),
                rot)
    return out

def joint_proportions(path: str = JSON_PATH) -> Dict[str, float]:
    """Exakte Anteilswerte des Standard-Rigs fuer geschaetzte Gelenke.

    Anteile gemessen am Standard-Koerper selbst (Knochen + Mesh-Enden),
    damit auch die *Notfall*-Schaetzung mit den echten Roblox-Werten
    arbeitet statt mit geratenen Konstanten:

    * ``shoulder_below_torso_top_frac`` - Schulterhoehe unter der
      Torso-Oberkante, als Anteil der Torso-Hoehe,
    * ``elbow_fraction_of_arm`` / ``wrist_fraction_of_arm`` -
      Schulter -> Ellbogen/Handgelenk, gemessen bis zum Hand-Mesh-Ende,
    * ``knee_fraction_of_leg`` / ``ankle_fraction_of_leg`` -
      Huefte -> Knie/Fussgelenk, gemessen bis zur Fusssohle.
    """
    spec = cached(path)
    if not spec:
        return {}
    parts = spec.get("parts") or {}

    def att_pos(part: str, att: str) -> Optional[Vec3]:
        a = (parts.get(part) or {}).get("attachments", {}).get(att)
        if not a:
            return None
        p = a.get("position")
        return (float(p[0]), float(p[1]), float(p[2])) if p else None

    out: Dict[str, float] = {}

    # Schulter: Abstand Torso-Oberkante -> Schultergelenk als Anteil der
    # Torso-Hoehe (damit skaliert die Schaetzung mit jedem Torso mit).
    torso = parts.get("UpperTorso") or {}
    t_size = torso.get("size") or (0.0, 0.0, 0.0)
    shoulder = att_pos("UpperTorso", "LeftShoulderRigAttachment")
    if shoulder and t_size[1]:
        out["shoulder_below_torso_top_frac"] = (
            (float(t_size[1]) / 2.0 - float(shoulder[1])) / float(t_size[1]))

    # Arm: Schulter -> Ellbogen -> Handgelenk -> Hand-Ende (in Teil-Koordinaten
    # der Kette; die Gelenke liegen paarweise auf demselben Teil, ihr
    # vertikaler Abstand entspricht dem Welt-Abstand der Standpose).
    arm_total = _chain_length(parts, ["LeftUpperArm", "LeftLowerArm", "LeftHand"],
                             "LeftShoulderRigAttachment", "LeftElbowRigAttachment",
                             "LeftWristRigAttachment")
    if arm_total and arm_total.get("joint_span"):
        s_e = arm_total["joint_span"][0]
        e_w = arm_total["joint_span"][1]
        total = arm_total["total"]
        if total > 1e-6:
            out["elbow_fraction_of_arm"] = s_e / total
            out["wrist_fraction_of_arm"] = (s_e + e_w) / total

    leg_total = _chain_length(parts, ["LeftUpperLeg", "LeftLowerLeg", "LeftFoot"],
                             "LeftHipRigAttachment", "LeftKneeRigAttachment",
                             "LeftAnkleRigAttachment")
    if leg_total and leg_total.get("joint_span"):
        h_k = leg_total["joint_span"][0]
        k_a = leg_total["joint_span"][1]
        total = leg_total["total"]
        if total > 1e-6:
            out["knee_fraction_of_leg"] = h_k / total
            out["ankle_fraction_of_leg"] = (h_k + k_a) / total
    return out


def _chain_length(parts: Dict[str, Any], segments: List[str],
                  proximal: str, middle: str, distal: str) -> Optional[Dict[str, Any]]:
    """Gelenk-Spannen und Gesamtlaenge einer Kette (Standpose, vertikal).

    Je zwei Gelenk-Attachments liegen auf demselben Teil - ihr vertikaler
    Abstand ist damit der Welt-Abstand der Standpose (die Teile haengen im
    Bind-Pose-Zustand senkrecht aneinander).
    """
    def ap(part: str, att: str) -> Optional[Vec3]:
        a = (parts.get(part) or {}).get("attachments", {}).get(att)
        if not a:
            return None
        p = a.get("position")
        return (float(p[0]), float(p[1]), float(p[2])) if p else None

    up_prox = ap(segments[0], proximal)   # z. B. Schulter am Oberarm
    up_dist = ap(segments[0], middle)     # Ellbogen am Oberarm
    mid_prox = ap(segments[1], middle)    # Ellbogen am Unterarm
    mid_dist = ap(segments[1], distal)    # Handgelenk am Unterarm
    end_prox = ap(segments[2], distal)    # Handgelenk an der Hand
    if None in (up_prox, up_dist, mid_prox, mid_dist, end_prox):
        return None
    span_se = abs(up_prox[1] - up_dist[1])
    span_ew = abs(mid_prox[1] - mid_dist[1])
    size = (parts.get(segments[2]) or {}).get("size") or (0.0, 0.0, 0.0)
    end_len = abs(end_prox[1] + float(size[1]) / 2.0)
    return {"joint_span": (span_se, span_ew), "total": span_se + span_ew + end_len}


def main(argv: Optional[List[str]] = None) -> int:
    if not os.path.exists(FBX_PATH):
        print(f"FBX fehlt: {FBX_PATH}", file=sys.stderr)
        return 1
    spec = build_spec()
    dump(spec)
    parts = spec["parts"]
    print(f"R15-Standard (Blocky): {len(parts)} Teile")
    for name in PART_NAMES:
        p = parts.get(name)
        if not p:
            print(f"  ! {name} fehlt")
            continue
        atts = ", ".join(sorted(p["attachments"]))
        print(f"  {name:16s} size={[round(v, 3) for v in p['size']]}  {atts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
