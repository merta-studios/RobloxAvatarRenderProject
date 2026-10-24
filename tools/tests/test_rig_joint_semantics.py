"""Regressionstests fuer die exakte Motor6D-Gelenk-Montage (Rigging-Fix).

Recherchierte Roblox-Semantik (Stand Oktober 2026, verifiziert gegen):

* ``Humanoid:BuildRigFromAttachments()`` (Roblox-Client-Tracker,
  ``avatar/unification/SetupAdapterParts.lua``): jeder Motor6D entsteht aus
  ``C0 = Eltern-RigAttachment.CFrame`` und ``C1 = Kind-RigAttachment.CFrame``.
* RCC-Emote-Thumbnails (``Thumbnailing/EmoteUtility.lua``):
  ``joint.Transform = pose.CFrame`` und
  ``part1 = part0 * C0 * Transform * C1^-1``.
* Echt-Dump eines Live-Charakters mit Custom-Body (Motor6D C0/C1 exakt gleich
  den Attachment-CFrames, alle Rotationen Identitaet) - die Formel
  rekonstruiert die Live-Posen mit Positionsdelta 0.
* Kanonisches R15-Layout (``avatar/unification/UnificationScaleOldRigMethod.lua``,
  ``UnificationScale.Target.Rigging``): Schulter/Elbow/Wrist/Hip/Knee/Ankle-
  Positionen - danach haengen Arme und Beine im Bind-Zustand von selbst
  senkrecht. Der Schulter->Ellbogen-Vektor im Arm-lokalen Raum hat per
  Definition ~34.6 Grad Horizontalkomponente (Schulter-Attachment an der
  Innenkante!) - das ist KEINE A-Pose und darf nie wegrotiert werden.

Der alte Code rotierte Arme mit geratenen Zielwinkeln (30/20 Grad), schob
die Schulter um 0.060 Studs nach aussen und setzte den Pose-Pivot VOR den
Offset -> Pivot <> Mesh-Anker -> sichtbare Luecken, vor allem an den Armen.

    python3 -m pytest tools/tests/test_rig_joint_semantics.py -q
"""

from __future__ import annotations

import math
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)
sys.path.insert(0, HERE)

import avatar_pose  # noqa: E402
import avatar_rig  # noqa: E402
import roblox_api  # noqa: E402
import roblox_mesh  # noqa: E402
from roblox_mesh import MeshData  # noqa: E402


I3 = avatar_rig.IDENTITY_MAT3

# ---------------------------------------------------------------------------
# Kanonisches R15-Layout (Roblox UnificationScale.Target.Rigging, exakt)
# ---------------------------------------------------------------------------

CANON = {
    "LowerTorso": {
        "size": (2.0, 0.4, 1.0),
        "atts": {
            "RootRigAttachment": ((0.0, 0.0, 0.0), I3),
            "WaistRigAttachment": ((0.0, 0.2, 0.0), I3),
            "LeftHipRigAttachment": ((-0.5, -0.2, 0.0), I3),
            "RightHipRigAttachment": ((0.5, -0.2, 0.0), I3),
        },
    },
    "UpperTorso": {
        "size": (2.0, 1.6, 1.0),
        "atts": {
            "WaistRigAttachment": ((0.0, -0.8, 0.0), I3),
            "NeckRigAttachment": ((0.0, 0.8, 0.0), I3),
            "LeftShoulderRigAttachment": ((-1.0, 0.563017, 0.0), I3),
            "RightShoulderRigAttachment": ((1.0, 0.563017, 0.0), I3),
        },
    },
    # Schulter-Attachment liegt an der Innenkante (+X fuer den linken Arm),
    # Ellbogen unter der Teilmitte: kanonisch, KEINE A-Pose.
    "LeftUpperArm": {
        "size": (1.0, 1.1687, 1.0),
        "atts": {
            "LeftShoulderRigAttachment": ((0.5, 0.394327, 0.0), I3),
            "LeftElbowRigAttachment": ((0.000479, -0.334056, 0.0), I3),
        },
    },
    "LeftLowerArm": {
        "size": (1.0, 1.0519, 1.0),
        "atts": {
            "LeftElbowRigAttachment": ((0.000479, 0.258688, 0.0), I3),
            "LeftWristRigAttachment": ((0.000479, -0.500929, 0.0), I3),
        },
    },
    "LeftHand": {
        "size": (1.0, 0.2504, 1.0),
        "atts": {
            "LeftWristRigAttachment": ((0.000479, 0.125045, 0.0), I3),
        },
    },
    "RightUpperArm": {
        "size": (1.0, 1.1687, 1.0),
        "atts": {
            "RightShoulderRigAttachment": ((-0.5, 0.394327, 0.0), I3),
            "RightElbowRigAttachment": ((0.000479, -0.334056, 0.0), I3),
        },
    },
    "RightLowerArm": {
        "size": (1.0, 1.0519, 1.0),
        "atts": {
            "RightElbowRigAttachment": ((0.000479, 0.258688, 0.0), I3),
            "RightWristRigAttachment": ((0.000479, -0.500929, 0.0), I3),
        },
    },
    "RightHand": {
        "size": (1.0, 0.2504, 1.0),
        "atts": {
            "RightWristRigAttachment": ((0.000479, 0.125045, 0.0), I3),
        },
    },
    "LeftUpperLeg": {
        "size": (1.0, 1.2166, 1.0),
        "atts": {
            "LeftHipRigAttachment": ((0.0, 0.420782, 0.0), I3),
            "LeftKneeRigAttachment": ((0.0, -0.401102, 0.0), I3),
        },
    },
    "LeftLowerLeg": {
        "size": (1.0, 1.1931, 1.0),
        "atts": {
            "LeftKneeRigAttachment": ((0.0, 0.379018, 0.0), I3),
            "LeftAnkleRigAttachment": ((0.0, -0.547157, 0.0), I3),
        },
    },
    "LeftFoot": {
        "size": (1.0, 0.3, 1.0),
        "atts": {
            "LeftAnkleRigAttachment": ((0.0, 0.101940, 0.0), I3),
        },
    },
    "RightUpperLeg": {
        "size": (1.0, 1.2166, 1.0),
        "atts": {
            "RightHipRigAttachment": ((0.0, 0.420782, 0.0), I3),
            "RightKneeRigAttachment": ((0.0, -0.401102, 0.0), I3),
        },
    },
    "RightLowerLeg": {
        "size": (1.0, 1.1931, 1.0),
        "atts": {
            "RightKneeRigAttachment": ((0.0, 0.379018, 0.0), I3),
            "RightAnkleRigAttachment": ((0.0, -0.547157, 0.0), I3),
        },
    },
    "RightFoot": {
        "size": (1.0, 0.3, 1.0),
        "atts": {
            "RightAnkleRigAttachment": ((0.0, 0.101940, 0.0), I3),
        },
    },
    "Head": {
        "size": (1.2, 1.2, 1.2),
        "atts": {
            "NeckRigAttachment": ((0.0, -0.5, 0.0), I3),
        },
    },
}


def _marker_mesh(spec, extra_markers=()):
    """Minimal-Mesh: Box + Marker-Vertices an den Attachment-Positionen.

    Die Marker erlauben exakte Koinzidenz-Pruefungen: nach der Montage muss
    ein Attachment-Marker des Kind-Segments exakt auf dem Gelenkpunkt
    (bzw. dem Eltern-Marker) liegen.
    """
    w, h, d = spec["size"]
    box = roblox_mesh.make_box(w, h, d, (0.0, 0.0, 0.0))
    markers = [pos for pos, _rot in spec["atts"].values()]
    markers.extend(extra_markers)
    positions = list(box.positions) + markers
    faces = list(box.faces) + [(0, 1, 2)]
    return MeshData(
        positions=positions,
        normals=list(box.normals) + [(0.0, 1.0, 0.0)] * len(markers),
        uvs=list(box.uvs) + [(0.0, 0.0)] * len(markers),
        faces=faces,
        colors=None,
        source_version="test-marker",
    )


def _segments(overrides=None, marker_extra=None):
    """segments_by_part wie build_avatar es liefert (Label = R15-Teilname)."""
    overrides = overrides or {}
    marker_extra = marker_extra or {}
    by_part = {
        "torso": ["LowerTorso", "UpperTorso"],
        "links_arm": ["LeftUpperArm", "LeftLowerArm", "LeftHand"],
        "rechts_arm": ["RightUpperArm", "RightLowerArm", "RightHand"],
        "links_bein": ["LeftUpperLeg", "LeftLowerLeg", "LeftFoot"],
        "rechts_bein": ["RightUpperLeg", "RightLowerLeg", "RightFoot"],
    }
    segments = {}
    for part, names in by_part.items():
        entries = []
        for name in names:
            spec = dict(CANON[name])
            spec["atts"] = dict(spec["atts"])
            if name in overrides:
                spec.update(overrides[name])
            if name in marker_extra:
                mesh = _marker_mesh(spec, marker_extra[name])
            else:
                mesh = _marker_mesh(spec)
            entries.append({
                "label": name,
                "mesh": mesh,
                "rbxm_class": "MeshPart",
                "rbxm_size": spec["size"],
                "attachments": {k: {"position": v[0], "rotation": v[1]}
                                for k, v in spec["atts"].items()},
                "transparency": 0.0,
            })
        segments[part] = entries
    return segments


def _assemble(segments):
    head_mp = {
        "mesh": _marker_mesh(CANON["Head"]),
        "rbxm_class": "MeshPart",
        "rbxm_size": CANON["Head"]["size"],
        "attachments": {k: {"position": v[0], "rotation": v[1]}
                        for k, v in CANON["Head"]["atts"].items()},
        "transparency": 0.0,
    }
    return avatar_rig.assemble_r15_rig(
        segments, head_mp,
        roblox_api.DEFAULT_BODY_COLORS,
        dict(roblox_api.DEFAULT_SCALES),
        headless=False,
    )


def _dist(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


# ---------------------------------------------------------------------------
# 1) Bind-Montage: Attachment-Koinzidenz (KEINE Lücken, KEINE geratenen Winkel)
# ---------------------------------------------------------------------------

def test_kanonischer_arm_haengt_spielerisch_senkrecht_ohne_rotationsheuristik():
    """Kanonisches R15-Layout: Arme muessen OHNE jede Korrektur senkrecht
    neben dem Torso haengen (Identitaets-Platzierung). Der alte Code hielt
    den kanonischen Schulter->Ellbogen-Vektor (34.6 Grad Horizontalanteil!)
    faelschlich fuer A-Pose und rotierte korrekte Arme schief."""
    rig = _assemble(_segments())
    arm = rig.body_meshes["links_arm"]
    lo, hi = arm.bbox()
    # Drei Segmente senkrecht: Breite exakt 1 Stud (keine Rotation!),
    # Hoehe ~1.93 Studs (1.17 + 1.05 + 0.25 minus Gelenk-Ueberlappung).
    assert hi[0] - lo[0] == pytest.approx(1.0, abs=1e-3)
    assert 1.8 < hi[1] - lo[1] < 2.1
    # Der Arm grenzt exakt an die Torso-Aussenseite (kein 0.060-Loch,
    # kein Schnitt in den Torso): Innenkante == Torso-Kante bei -1.0.
    torso = rig.body_meshes["torso"]
    t_lo, t_hi = torso.bbox()
    assert hi[0] == pytest.approx(t_lo[0], abs=1e-3)


def test_gelenkpunkte_liegen_exakt_auf_den_attachment_markern():
    """joint_positions muss der exakte Koinzidenzpunkt sein - der Pose-Pivot
    darf nicht um einen geratenen Offset daneben liegen."""
    rig = _assemble(_segments())
    mesh = rig.body_meshes["links_arm"]
    spans = {seg: (s, e) for seg, s, e in rig.segment_spans["links_arm"]}
    # Linker Oberarm: Box + 2 Marker (Schulter, Ellbogen) - die letzten
    # beiden Vertices sind die Marker. Box von make_box hat 24 Vertices.
    s, e = spans["LeftUpperArm"]
    # Marker haengen NACH der Box: Schulter-Marker = e-2, Ellbogen-Marker = e-1.
    shoulder_marker = mesh.positions[e - 2]
    elbow_marker = mesh.positions[e - 1]
    assert _dist(shoulder_marker, rig.joint_positions["LeftUpperArm"]) < 1e-9
    assert _dist(elbow_marker, rig.joint_positions["LeftLowerArm"]) < 1e-9
    # Unterarm: Ellbogen-Marker (erster seiner zwei Marker) koinzidiert.
    s2, e2 = spans["LeftLowerArm"]
    elbow_marker_la = mesh.positions[e2 - 2]
    wrist_marker_la = mesh.positions[e2 - 1]
    assert _dist(elbow_marker_la, rig.joint_positions["LeftLowerArm"]) < 1e-9
    assert _dist(wrist_marker_la, rig.joint_positions["LeftHand"]) < 1e-9
    # Naht: Oberarm- und Unterarm-Ellbogen exakt am selben Punkt.
    assert _dist(elbow_marker, elbow_marker_la) < 1e-9


def test_schulter_marker_koinzidiert_mit_torso_attachment():
    """Der Arm-Schulter-Marker liegt exakt auf dem Torso-Schulter-Gelenk."""
    rig = _assemble(_segments())
    arm = rig.body_meshes["links_arm"]
    spans = {seg: (s, e) for seg, s, e in rig.segment_spans["links_arm"]}
    s, e = spans["LeftUpperArm"]
    shoulder_marker = arm.positions[e - 2]
    assert _dist(shoulder_marker, rig.joint_positions["LeftUpperArm"]) < 1e-9
    # Torso-Attachment-Weltposition (nach dem globalen y-Shift) ---
    # joint_positions wurde mit verschoben, beide muessen identisch sein.
    att = rig.world_attachments["LeftShoulderRigAttachment"]
    assert _dist(shoulder_marker, att[0]) < 1e-9


def test_apose_skulptur_bleibt_exakt_am_attachment_und_ohne_luecke():
    """Ein A-pose-skulptiertes Paket (Ellbogen schraeg aussen) wird NIE
    wegrotiert: die Attachments koinzidieren trotzdem exakt - genauso
    baut Roblox die Charaktere (BuildRigFromAttachments ohne Korrektur)."""
    overrides = {
        # Skulptur: Ellbogen-Attachment deutlich weiter aussen/unten
        "LeftUpperArm": {
            "atts": {
                "LeftShoulderRigAttachment": ((0.5, 0.394327, 0.0), I3),
                "LeftElbowRigAttachment": ((-0.35, -0.55, 0.0), I3),
            },
        },
    }
    rig = _assemble(_segments(overrides=overrides))
    arm = rig.body_meshes["links_arm"]
    spans = {seg: (s, e) for seg, s, e in rig.segment_spans["links_arm"]}
    s, e = spans["LeftUpperArm"]
    shoulder_marker = arm.positions[e - 2]
    elbow_marker = arm.positions[e - 1]
    # Schulter unveraendert am Gelenk (kein +0.060-Offset mehr).
    assert _dist(shoulder_marker, rig.joint_positions["LeftUpperArm"]) < 1e-9
    s2, e2 = spans["LeftLowerArm"]
    elbow_marker_la = arm.positions[e2 - 2]
    # Ellbogen-Naht auch bei schraeger Skulptur exakt geschlossen.
    assert _dist(elbow_marker, elbow_marker_la) < 1e-9
    assert _dist(elbow_marker, rig.joint_positions["LeftLowerArm"]) < 1e-9
    # Der Arm wurde NICHT auf senkrecht rotiert: Ellbogen deutlich aussen.
    assert elbow_marker[0] < shoulder_marker[0] - 0.6


def test_gedrehte_attachment_frames_koinzidieren_in_der_orientierung():
    """Attachment-Frame mit Rotation: Kind-Segment wird so montiert, dass
    NICHT nur die Position, sondern der ganze Frame koinzidiert (child =
    parent * C0 * C1^-1). Torso-Attachment ungedreht, Arm-Attachment um
    90 Grad um Y gedreht: der Arm muss in der Welt entsprechend MITGEDREHT
    werden, damit die Frames deckungsgleich sind - ein ignorierte Rotation
    wuerde den Achs-Marker an die falsche Stelle setzen."""
    rot_y90 = ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0))
    # Achs-Marker 0.1 Studs entlang der lokalen Attachment-X-Achse:
    # Ry(90) mappt +X auf -Z -> lokal (0.5, 0.394, -0.1).
    overrides = {
        "LeftUpperArm": {
            "atts": {
                "LeftShoulderRigAttachment": ((0.5, 0.394327, 0.0), rot_y90),
                "LeftElbowRigAttachment": ((0.000479, -0.334056, 0.0), rot_y90),
            },
        },
    }
    marker_extra = {"LeftUpperArm": [(0.5, 0.394327, -0.1)]}
    rig = _assemble(_segments(overrides=overrides, marker_extra=marker_extra))
    arm = rig.body_meshes["links_arm"]
    spans = {seg: (s, e) for seg, s, e in rig.segment_spans["links_arm"]}
    s, e = spans["LeftUpperArm"]
    # Reihenfolge der Marker im Segment: Box, Schulter, Ellbogen, Achs-Marker.
    achse = arm.positions[e - 1]
    schulter = arm.positions[e - 3]
    # Position koinzidiert weiterhin exakt ...
    assert _dist(schulter, rig.joint_positions["LeftUpperArm"]) < 1e-9
    # ... und der Achs-Marker liegt in WELT-X-Richtung vom Gelenk (lokale
    # Attachment-X-Achse wurde auf die Welt-X-Achse des Torso-Frames
    # gedreht). Alte Code-Variante (Rotation ignoriert): (0, 0, 0.1)-Versatz.
    assert achse[0] == pytest.approx(schulter[0] + 0.1, abs=1e-9)
    assert achse[2] == pytest.approx(schulter[2], abs=1e-9)
    # Gelenk-Frame (C0) bleibt die UNGEDREHTE Eltern-Orientierung.
    frame = rig.joint_frames["LeftUpperArm"]
    assert all(abs(frame[i][j] - I3[i][j]) < 1e-9 for i in range(3) for j in range(3))


# ---------------------------------------------------------------------------
# 2) Pose-Anwendung: starr je Segment, exakt im C0-Frame (Motor6D)
# ---------------------------------------------------------------------------

def _rot(axis, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    if axis == "x":
        return ((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c))
    if axis == "y":
        return ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c))
    return ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))


def test_pose_dreht_arm_starr_ums_schultergelenk():
    """Motor6D-Pose (wie Emotes): Schulter-Transform rotiert das OBERARM-
    Segment starr um das Bind-Attachment; Unterarm/Hand folgen ueber die
    Kette - die Ellbogen-Naht bleibt exakt geschlossen."""
    rig = _assemble(_segments())
    pose = {
        "linker_oberarm": {
            "rotation": (0.0, 0.0, 0.0),
            "verschiebung": (0.0, 0.0, 0.0),
            "felder": {},
            "motor": ((0.0, 0.0, 0.0), _rot("z", 30.0)),
        },
    }
    ctx = avatar_pose.PoseContext(pose, dict(rig.joint_positions), rig=rig)
    assert ctx.active
    posed = ctx.apply_body("links_arm", rig.body_meshes["links_arm"])
    bind = rig.body_meshes["links_arm"]

    def seg_point(mesh, seg_name, idx_from_end):
        spans = {seg: (s, e) for seg, s, e in rig.segment_spans["links_arm"]}
        s, e = spans[seg_name]
        return mesh.positions[e - idx_from_end]

    shoulder = rig.joint_positions["LeftUpperArm"]
    # Starr: Abstand jedes Oberarm-Vertex zum Gelenk bleibt erhalten.
    spans = {seg: (s, e) for seg, s, e in rig.segment_spans["links_arm"]}
    su, eu = spans["LeftUpperArm"]
    for i in range(su, eu):
        assert _dist(posed.positions[i], shoulder) == pytest.approx(
            _dist(bind.positions[i], shoulder), abs=1e-9)
    # Ellbogen-Naht bleibt zu: Oberarm- und Unterarm-Ellbogen koinzidieren
    # auch NACH der Pose (beide haengen an derselben Transformation Kette).
    elbow_u_posed = seg_point(posed, "LeftUpperArm", 1)
    elbow_l_posed = seg_point(posed, "LeftLowerArm", 2)
    assert _dist(elbow_u_posed, elbow_l_posed) < 1e-9
    # Der Ellbogen ist mitgedreht: 30 Grad um Z um die Schulter.
    elbow_bind = seg_point(bind, "LeftUpperArm", 1)
    rel = (elbow_bind[0] - shoulder[0], elbow_bind[1] - shoulder[1], 0.0)
    r = _dist(rel, (0.0, 0.0, 0.0))
    winkel_bind = math.atan2(rel[1], rel[0])
    rel_posed = (elbow_u_posed[0] - shoulder[0], elbow_u_posed[1] - shoulder[1])
    winkel_posed = math.atan2(rel_posed[1], rel_posed[0])
    assert _dist(elbow_u_posed, shoulder) == pytest.approx(r, abs=1e-9)
    assert math.degrees(winkel_posed - winkel_bind) == pytest.approx(30.0, abs=1e-6)


def test_pose_im_gedrehten_gelenk_frame_ist_konjugiert():
    """Pose-CFrames sind im C0-Frame definiert (Roblox-Motor6D-Semantik):
    eine X-Drehung im um 90 Grad um Y gedrehten Gelenk-Frame wirkt in der
    Welt als Z-Drehung - NICHT als Welt-X-Drehung."""
    rot_y90 = ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0))
    rig = avatar_rig.assemble_r15_rig(
        _segments(), None, roblox_api.DEFAULT_BODY_COLORS,
        dict(roblox_api.DEFAULT_SCALES), headless=True)
    # Gelenk-Frame per Hand auf 90 Grad um Y drehen (wie ein Package mit
    # gedrehtem Attachment): joint_frames[LeftUpperArm] = Ry(90).
    rig.joint_frames["LeftUpperArm"] = rot_y90
    pose = {
        "linker_oberarm": {
            "rotation": (0.0, 0.0, 0.0),
            "verschiebung": (0.0, 0.0, 0.0),
            "felder": {},
            "motor": ((0.0, 0.0, 0.0), _rot("x", 90.0)),
        },
    }
    ctx = avatar_pose.PoseContext(pose, dict(rig.joint_positions), rig=rig)
    assert ctx.active
    posed = ctx.apply_body("links_arm", rig.body_meshes["links_arm"])
    bind = rig.body_meshes["links_arm"]
    spans = {seg: (s, e) for seg, s, e in rig.segment_spans["links_arm"]}
    # Ein Punkt direkt unter dem Schultergelenk (Marker-los: Box-Unterkante
    # des Oberarms) muss um die WELT-Z-Achse wandern: Ry * Rx * Ry^-1 = Rz.
    # Wir pruefen ueber die Box-Ecke: Abstand zum Gelenk bleibt (starr) und
    # die Rotation ist eine reine Z-Drehung (z-Koordinate unveraendert) -
    # eine WELT-X-Drehung (falsche Achsen) wuerde z sofort veraendern.
    su, eu = spans["LeftUpperArm"]
    bewegt = False
    for i in range(su, eu):
        assert posed.positions[i][2] == pytest.approx(bind.positions[i][2], abs=1e-9)
        if abs(posed.positions[i][0] - bind.positions[i][0]) > 1e-6:
            bewegt = True
    assert bewegt, "Pose muss das Oberarm-Segment tatsaechlich drehen"


def test_motor_pose_ohne_rig_gleich_euler_pose():
    """Ohne Rig-Frame (Fallback-Geometrie) ist die Motor-Formel exakt die
    klassische Pivot-Drehung - alte Posen.toml-Posen bleiben identisch."""
    joints = {
        "LowerTorso": (0.0, 0.0, 0.0), "UpperTorso": (0.0, 0.35, 0.0),
        "Head": (0.0, 0.9, 0.0), "LeftUpperArm": (1.0, 1.4, 0.0),
        "LeftLowerArm": (1.0, 0.9, 0.0), "LeftHand": (1.0, 0.6, 0.0),
    }
    mesh = roblox_mesh.make_box(0.4, 1.2, 0.4, (1.0, 0.8, 0.0))
    euler_pose = {"linker_oberarm": {"vor_hoch_schwingen_grad": 45.0}}
    ctx_euler = avatar_pose.PoseContext(euler_pose, joints)
    motor_pose = {
        "linker_oberarm": {
            "rotation": (45.0, 0.0, 0.0),
            "verschiebung": (0.0, 0.0, 0.0),
            "felder": {"drehung_grad": 45.0},
            "motor": ((0.0, 0.0, 0.0), _rot("x", 45.0)),
        },
    }
    ctx_motor = avatar_pose.PoseContext(motor_pose, joints)
    a = ctx_euler.apply_body("links_arm", mesh)
    b = ctx_motor.apply_body("links_arm", mesh)
    for pa, pb in zip(a.positions, b.positions):
        assert _dist(pa, pb) < 1e-9


# ---------------------------------------------------------------------------
# 3) Struktur: Spans/Driver fuer die starre Segment-Anwendung
# ---------------------------------------------------------------------------

def test_segment_spans_und_driver_vollstaendig():
    rig = _assemble(_segments())
    for part, spans in rig.segment_spans.items():
        mesh = rig.body_meshes[part]
        n = len(mesh.positions)
        assert spans, part
        # Lueckenlos und sortiert.
        cursor = 0
        for seg, s, e in spans:
            assert s == cursor
            assert 0 < e - s
            cursor = e
            if seg != "LowerTorso":
                assert rig.segment_driver.get(seg) == seg
        assert cursor == n
    assert rig.segment_driver.get("LowerTorso") is None
    assert rig.segment_driver.get("UpperTorso") == "UpperTorso"
    assert rig.segment_driver.get("Head") == "Head"
    for j in ("LeftUpperArm", "LeftLowerArm", "LeftHand",
              "LeftUpperLeg", "LeftLowerLeg", "LeftFoot",
              "UpperTorso", "LowerTorso", "Head"):
        assert j in rig.joint_frames
        assert j in rig.joint_positions


def test_handschriftliche_pose_weicht_starr_pro_segment_aus():
    """Auch klassische Posen.toml-Felder werden am echten Rig STARR je
    Segment angewandt (kein weiches Verformen der verschmolzenen Arme)."""
    rig = _assemble(_segments())
    pose = {"linker_unterarm": {"ellbogen_beugen_grad": 40.0}}
    ctx = avatar_pose.PoseContext(pose, dict(rig.joint_positions), rig=rig)
    posed = ctx.apply_body("links_arm", rig.body_meshes["links_arm"])
    bind = rig.body_meshes["links_arm"]
    spans = {seg: (s, e) for seg, s, e in rig.segment_spans["links_arm"]}
    # Oberarm-Segment bleibt exakt in Ruhe (starr, nicht anteilig verdreht).
    su, eu = spans["LeftUpperArm"]
    for i in range(su, eu):
        assert _dist(posed.positions[i], bind.positions[i]) < 1e-12
    # Unterarm-Segment hat sich als Ganzes bewegt.
    sl, el = spans["LeftLowerArm"]
    moved = max(_dist(posed.positions[i], bind.positions[i]) for i in range(sl, el))
    assert moved > 0.1


# Reihenfolge der Attachment-Marker je Segment (entspricht CANON).
_ATT_ORDER = {
    "LeftUpperArm": ["LeftShoulderRigAttachment", "LeftElbowRigAttachment"],
    "LeftLowerArm": ["LeftElbowRigAttachment", "LeftWristRigAttachment"],
    "LeftHand": ["LeftWristRigAttachment"],
    "RightUpperArm": ["RightShoulderRigAttachment", "RightElbowRigAttachment"],
    "RightLowerArm": ["RightElbowRigAttachment", "RightWristRigAttachment"],
    "RightHand": ["RightWristRigAttachment"],
    "LeftUpperLeg": ["LeftHipRigAttachment", "LeftKneeRigAttachment"],
    "LeftLowerLeg": ["LeftKneeRigAttachment", "LeftAnkleRigAttachment"],
    "LeftFoot": ["LeftAnkleRigAttachment"],
    "RightUpperLeg": ["RightHipRigAttachment", "RightKneeRigAttachment"],
    "RightLowerLeg": ["RightKneeRigAttachment", "RightAnkleRigAttachment"],
    "RightFoot": ["RightAnkleRigAttachment"],
}


def test_komposit_pose_haelt_alle_nahte_exakt_geschlossen():
    """Der eigentliche Anwendungsfall (Emote): MANY Gelenke gleichzeitig.
    Alle Attachment-Marker-Paare muessen auch NACH der Pose exakt
    koinzidieren (Nahtloesigkeit bis auf Maschinen-Epsilon) und jedes
    Segment starr bleiben - genau wie Motor6D in Roblox."""
    rig = _assemble(_segments())
    pose = {}
    for bone, axis, deg in (("linker_oberarm", "z", 35.0),
                            ("linker_unterarm", "x", -70.0),
                            ("linke_hand", "x", 15.0),
                            ("rechter_oberarm", "z", -35.0),
                            ("rechter_unterarm", "x", 70.0),
                            ("linkes_oberbein", "x", 25.0),
                            ("linkes_unterbein", "x", -45.0),
                            ("rechter_fuss", "x", 20.0),
                            ("oberer_torso", "y", 15.0),
                            ("kopf", "y", 30.0)):
        pose[bone] = {"rotation": (0.0, 0.0, 0.0), "verschiebung": (0.0, 0.0, 0.0),
                      "felder": {}, "motor": ((0.0, 0.0, 0.0), _rot(axis, deg))}
    ctx = avatar_pose.PoseContext(pose, dict(rig.joint_positions), rig=rig)
    assert ctx.active

    seam_pairs = {
        "links_arm": [("LeftUpperArm", "LeftLowerArm", "LeftElbowRigAttachment"),
                      ("LeftLowerArm", "LeftHand", "LeftWristRigAttachment")],
        "rechts_arm": [("RightUpperArm", "RightLowerArm", "RightElbowRigAttachment"),
                       ("RightLowerArm", "RightHand", "RightWristRigAttachment")],
        "links_bein": [("LeftUpperLeg", "LeftLowerLeg", "LeftKneeRigAttachment"),
                       ("LeftLowerLeg", "LeftFoot", "LeftAnkleRigAttachment")],
        "rechts_bein": [("RightUpperLeg", "RightLowerLeg", "RightKneeRigAttachment"),
                        ("RightLowerLeg", "RightFoot", "RightAnkleRigAttachment")],
    }
    for part, pairs in seam_pairs.items():
        posed = ctx.apply_body(part, rig.body_meshes[part])
        bind = rig.body_meshes[part]
        spans = {seg: (s, e) for seg, s, e in rig.segment_spans[part]}
        for parent, child, att in pairs:
            ip = spans[parent][1] - len(_ATT_ORDER[parent]) + _ATT_ORDER[parent].index(att)
            ic = spans[child][1] - len(_ATT_ORDER[child]) + _ATT_ORDER[child].index(att)
            assert _dist(posed.positions[ip], posed.positions[ic]) < 1e-9, (part, att)
        # Starrheit: Abstaende innerhalb jedes Segments bleiben exakt.
        for seg, s, e in rig.segment_spans[part]:
            for i, j in ((s, e - 1), (s + 1, e - 1), (s, s + 1)):
                d0 = _dist(bind.positions[i], bind.positions[j])
                d1 = _dist(posed.positions[i], posed.positions[j])
                assert d0 == pytest.approx(d1, abs=1e-9), (seg, i, j)
