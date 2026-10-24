"""Regressionstests fuer nahtlose Gelenke bei echten (auch kaputten) Bundles.

Bug-Report: "bei der Pose sind manche Teile verschoben geriggt - Schulter zu
weit unten, Unterarm mit sehr falschem Drehpunkt, nicht nahtlos aneinander".

Ursachen, die hier abgedeckt sind:

* Fehlende ``*RigAttachment``-Daten an einem Segment wurden durch geratene
  Konstanten (``0,3 * scale`` usw.) ersetzt - das Glied hing daneben.
* Platzhalter-Attachments ``(0,0,0)`` (konvertierte Legacy-Bundles) setzten
  den Drehpunkt mitten in das Teil, statt an die Deckflaeche.
* Fehlte *ein* Segment (z. B. die Hand), wurde der ganze Arm durch einen
  Quader ersetzt.
* Die BBox-Schaetzung legte Schulter/Ellbogen an die Mesh-Oberkante bzw. in
  die Teilmitte, obwohl Roblox dort andere Proportionen hat.

    python3 -m pytest tools/tests/test_rig_seams.py -q
"""

from __future__ import annotations

import math
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

import avatar_pose  # noqa: E402
import avatar_rig  # noqa: E402
import roblox_api  # noqa: E402
import roblox_mesh  # noqa: E402

I3 = avatar_rig.IDENTITY_MAT3


def _seg(name, size, atts=None, transparency=0.0):
    return {"label": name, "mesh": roblox_mesh.make_box(*size, (0.0, 0.0, 0.0)),
            "rbxm_class": "MeshPart", "rbxm_size": size,
            "attachments": atts or {}, "transparency": transparency}


def _att(pos, rot=I3):
    return {"position": pos, "rotation": rot}


def _torso(with_shoulder=True, with_hip=True):
    lower = _seg("LowerTorso", (2.0, 0.4, 1.0), {
        "RootRigAttachment": _att((0.0, 0.0, 0.0)),
        "WaistRigAttachment": _att((0.0, 0.2, 0.0)),
        "LeftHipRigAttachment": _att((-0.5, -0.2, 0.0)),
        "RightHipRigAttachment": _att((0.5, -0.2, 0.0)),
    })
    upper_atts = {
        "WaistRigAttachment": _att((0.0, -0.8, 0.0)),
        "NeckRigAttachment": _att((0.0, 0.8, 0.0)),
    }
    if with_shoulder:
        upper_atts["LeftShoulderRigAttachment"] = _att((-1.0, 0.563017, 0.0))
        upper_atts["RightShoulderRigAttachment"] = _att((1.0, 0.563017, 0.0))
    upper = _seg("UpperTorso", (2.0, 1.6, 1.0), upper_atts)
    return [lower, upper]


def _assemble(segs):
    return avatar_rig.assemble_r15_rig(
        segs, None, roblox_api.DEFAULT_BODY_COLORS,
        dict(roblox_api.DEFAULT_SCALES), headless=True)


def _dist(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def _arm_groups(left_segments):
    return {"torso": _torso(), "links_arm": left_segments}


# ---------------------------------------------------------------------------
# 1) Fehlende Attachments: Anschluss aus der Geometrie, Naht bleibt zu
# ---------------------------------------------------------------------------

def test_arm_ohne_attachments_haengt_nahtlos_am_torso():
    segs = _arm_groups([
        _seg("LeftUpperArm", (1.0, 1.1687, 1.0)),
        _seg("LeftLowerArm", (1.0, 1.0519, 1.0)),
        _seg("LeftHand", (1.0, 0.2504, 1.0)),
    ])
    rig = _assemble(segs)
    arm = rig.body_meshes["links_arm"]
    lo, hi = arm.bbox()

    # Innenkante exakt am Torso-Rand (kein Loch, kein Hineinragen).
    assert hi[0] == pytest.approx(-1.0, abs=1e-3)
    # Volle Armhoehe: drei Segmente lueckenlos (ohne Ueberlappung).
    assert hi[1] - lo[1] == pytest.approx(1.1687 + 1.0519 + 0.2504, abs=1e-3)

    shoulder = rig.joint_positions["LeftUpperArm"]
    elbow = rig.joint_positions["LeftLowerArm"]
    wrist = rig.joint_positions["LeftHand"]
    # Gelenke sitzen an den Segmentgrenzen - nicht in der Teilmitte.
    assert shoulder[1] - elbow[1] == pytest.approx(1.1687, abs=1e-6)
    assert elbow[1] - wrist[1] == pytest.approx(1.0519, abs=1e-6)
    assert lo[1] == pytest.approx(wrist[1] - 0.2504, abs=1e-6)
    # Der Arm haengt gerade: Ellbogen/Handgelenk liegen auf der Armmitte,
    # die Schulter an der Arm-Innenkante (kanonisches R15, kein Schraegstand).
    assert elbow[0] == pytest.approx((lo[0] + hi[0]) / 2.0, abs=1e-9)
    assert wrist[0] == pytest.approx(elbow[0], abs=1e-9)


def test_fehlende_hand_ersetzt_den_arm_nicht_durch_einen_quader():
    segs = _arm_groups([
        _seg("LeftUpperArm", (1.0, 1.1687, 1.0), {
            "LeftShoulderRigAttachment": _att((0.5, 0.394327, 0.0)),
            "LeftElbowRigAttachment": _att((0.000479, -0.334056, 0.0)),
        }),
        _seg("LeftLowerArm", (1.0, 1.0519, 1.0), {
            "LeftElbowRigAttachment": _att((0.000479, 0.258688, 0.0)),
            "LeftWristRigAttachment": _att((0.000479, -0.500929, 0.0)),
        }),
    ])
    rig = _assemble(segs)
    arm = rig.body_meshes["links_arm"]
    # Echte Segmente statt der 2-Studs-Ersatzbox: 0,19 (Schulterkugel) +
    # 0,728 (Oberarm bis Ellbogen) + 0,793 (Unterarm bis Armende).
    assert arm.bbox()[1][1] - arm.bbox()[0][1] == pytest.approx(
        0.19 + 0.7284 + 0.7932, abs=0.01)
    assert {name for name, _s, _e in rig.segment_spans["links_arm"]} == {
        "LeftUpperArm", "LeftLowerArm"}
    # Das fehlende Handgelenk landet in der unteren Haelfte des Unterarms
    # (nicht am Ellbogen und nicht ausserhalb des Meshes).
    wrist = rig.joint_positions["LeftHand"]
    elbow = rig.joint_positions["LeftLowerArm"]
    lo = arm.bbox()[0][1]
    assert lo < wrist[1] < elbow[1] - 0.25 * 1.0519


# ---------------------------------------------------------------------------
# 2) Platzhalter-Attachments (0,0,0): Drehpunkt an der Deckflaeche
# ---------------------------------------------------------------------------

def test_platzhalter_gelenke_landen_an_der_segmentunterkante():
    segs = _arm_groups([
        _seg("LeftUpperArm", (1.0, 1.1687, 1.0), {
            "LeftShoulderRigAttachment": _att((0.5, 0.394327, 0.0)),
            "LeftElbowRigAttachment": _att((0.0, 0.0, 0.0)),      # Platzhalter
        }),
        _seg("LeftLowerArm", (1.0, 1.0519, 1.0), {
            "LeftElbowRigAttachment": _att((0.0, 0.0, 0.0)),      # Platzhalter
            "LeftWristRigAttachment": _att((0.0, 0.0, 0.0)),      # Platzhalter
        }),
        _seg("LeftHand", (1.0, 0.2504, 1.0), {
            "LeftWristRigAttachment": _att((0.0, 0.0, 0.0)),      # Platzhalter
        }),
    ])
    rig = _assemble(segs)
    shoulder = rig.joint_positions["LeftUpperArm"]
    elbow = rig.joint_positions["LeftLowerArm"]
    wrist = rig.joint_positions["LeftHand"]

    # Ellbogen am unteren Ende des Oberarms (nicht in dessen Mitte).
    assert shoulder[1] - elbow[1] == pytest.approx(1.1687 - 0.19, abs=1e-3)
    # Unterarm liegt lueckenlos darunter (kein Ueberlappen).
    lo, hi = rig.body_meshes["links_arm"].bbox()
    assert elbow[1] - wrist[1] == pytest.approx(1.0519, abs=1e-6)
    assert hi[1] - lo[1] == pytest.approx(1.1687 + 1.0519 + 0.2504, abs=1e-3)
    # Und das Audit erklaert, was ersetzt wurde.
    assert any("Platzhalter" in note for note in rig.notes)


def test_pose_dreht_platzhalter_gelenk_um_die_naht(monkeypatch):
    segs = _arm_groups([
        _seg("LeftUpperArm", (1.0, 1.1687, 1.0), {
            "LeftShoulderRigAttachment": _att((0.5, 0.394327, 0.0)),
            "LeftElbowRigAttachment": _att((0.0, 0.0, 0.0)),
        }),
        _seg("LeftLowerArm", (1.0, 1.0519, 1.0), {
            "LeftElbowRigAttachment": _att((0.0, 0.0, 0.0)),
            "LeftWristRigAttachment": _att((0.0, 0.0, 0.0)),
        }),
        _seg("LeftHand", (1.0, 0.2504, 1.0), {
            "LeftWristRigAttachment": _att((0.0, 0.0, 0.0)),
        }),
    ])
    rig = _assemble(segs)
    ctx = avatar_pose.PoseContext(
        {"linker_unterarm": {"ellbogen_beugen_grad": 60.0}},
        dict(rig.joint_positions), rig=rig)
    bind = rig.body_meshes["links_arm"]
    posed = ctx.apply_body("links_arm", bind)
    spans = {seg: (s, e) for seg, s, e in rig.segment_spans["links_arm"]}

    elbow = rig.joint_positions["LeftLowerArm"]
    # Oberarm bleibt starr in Ruhe (Drehpunkt liegt an seiner Unterkante).
    su, eu = spans["LeftUpperArm"]
    for i in range(su, eu):
        assert _dist(posed.positions[i], bind.positions[i]) < 1e-9
    # Der Unterarm bleibt starr am Ellbogen: kein Vertex wandert naeher an
    # das Gelenk heran oder davon weg (Naht bleibt zu, kein Aufreissen).
    sl, el = spans["LeftLowerArm"]
    for i in range(sl, el):
        assert _dist(posed.positions[i], elbow) == pytest.approx(
            _dist(bind.positions[i], elbow), abs=1e-9)
    # ... und er dreht wirklich um 60 Grad um das Ellbogengelenk.
    v = next((i for i in range(sl, el)
              if abs(bind.positions[i][2]) > 1e-6), None)
    assert v is not None
    # "ellbogen_beugen_grad" dreht im Gelenk-Frame um X -> in der Welt
    # bewegt sich der Vertex in der Y/Z-Ebene.
    rel = (bind.positions[v][1] - elbow[1], bind.positions[v][2] - elbow[2])
    rel2 = (posed.positions[v][1] - elbow[1], posed.positions[v][2] - elbow[2])
    winkel = math.degrees(math.atan2(rel2[1], rel2[0]) - math.atan2(rel[1], rel[0]))
    winkel = (winkel + 180.0) % 360.0 - 180.0
    assert abs(winkel) == pytest.approx(60.0, abs=1e-6)
    # Unterarm ist um 60 Grad um das Gelenk gedreht (nicht um die Teilmitte).
    moved = max(_dist(posed.positions[i], bind.positions[i]) for i in range(sl, el))
    assert moved > 0.3


# ---------------------------------------------------------------------------
# 3) BBox-Schaetzung (Rig ohne Attachments): kanonische Proportionen
# ---------------------------------------------------------------------------

def _fallback_joints():
    parts, metrics = avatar_rig.build_body(
        dict(roblox_api.DEFAULT_BODY_COLORS), dict(roblox_api.DEFAULT_SCALES))
    meshes = {p.name: p.mesh for p in parts}
    return avatar_pose.joints_from_parts(meshes, metrics), meshes, metrics


def test_schaetzung_setzt_schulter_an_die_torso_oberkante():
    joints, meshes, metrics = _fallback_joints()
    torso_lo, torso_hi = meshes["torso"].bbox()
    shoulder = joints["LeftUpperArm"]
    # Schulterlinie: kanonisch 0,237 Studs unter der Torso-Oberkante.
    assert shoulder[1] == pytest.approx(torso_hi[1] - 0.237, abs=0.02)
    # Innenkante des Arms, nicht dessen Mitte.
    assert shoulder[0] == pytest.approx(meshes["links_arm"].bbox()[1][0], abs=1e-6)


def test_schaetzung_setzt_ellbogen_nicht_in_die_armmitte():
    joints, meshes, metrics = _fallback_joints()
    shoulder = joints["LeftUpperArm"]
    elbow = joints["LeftLowerArm"]
    wrist = joints["LeftHand"]
    arm_lo = meshes["links_arm"].bbox()[0][1]
    arm_len = shoulder[1] - arm_lo
    # Kanonische R15-Anteile (Ellbogen ~42 %, Handgelenk ~86 % der Armlaenge).
    assert (shoulder[1] - elbow[1]) / arm_len == pytest.approx(0.419, abs=0.02)
    assert (shoulder[1] - wrist[1]) / arm_len == pytest.approx(0.856, abs=0.02)
    # Der alte Wert (58 % / 86 % ab Arm-Oberkante) lag deutlich tiefer.
    assert elbow[1] > shoulder[1] - 0.52 * arm_len


def test_schaetzung_setzt_huefte_an_die_torso_unterkante():
    joints, meshes, metrics = _fallback_joints()
    torso_lo = meshes["torso"].bbox()[0][1]
    hip = joints["LeftUpperLeg"]
    assert hip[1] == pytest.approx(torso_lo, abs=0.02)
    knee = joints["LeftLowerLeg"]
    leg_len = hip[1] - meshes["links_bein"].bbox()[0][1]
    assert (hip[1] - knee[1]) / leg_len == pytest.approx(0.411, abs=0.02)


# ---------------------------------------------------------------------------
# 4) Handgeschriebene Posen drehen im Gelenk-Frame (nicht in Weltachsen)
# ---------------------------------------------------------------------------

def _rig_with_rotated_shoulder():
    segs = _arm_groups([
        _seg("LeftUpperArm", (1.0, 1.1687, 1.0), {
            "LeftShoulderRigAttachment": _att((0.5, 0.394327, 0.0)),
            "LeftElbowRigAttachment": _att((0.000479, -0.334056, 0.0)),
        }),
        _seg("LeftLowerArm", (1.0, 1.0519, 1.0), {
            "LeftElbowRigAttachment": _att((0.000479, 0.258688, 0.0)),
            "LeftWristRigAttachment": _att((0.000479, -0.500929, 0.0)),
        }),
        _seg("LeftHand", (1.0, 0.2504, 1.0), {
            "LeftWristRigAttachment": _att((0.000479, 0.125045, 0.0)),
        }),
    ])
    rig = _assemble(segs)
    # Paket mit gedrehtem Schulter-Attachment (90 Grad um Y).
    rig.joint_frames["LeftUpperArm"] = ((0.0, 0.0, 1.0),
                                        (0.0, 1.0, 0.0),
                                        (-1.0, 0.0, 0.0))
    return rig


def test_posen_toml_drehung_wirkt_im_gelenk_frame():
    rig = _rig_with_rotated_shoulder()
    ctx = avatar_pose.PoseContext(
        {"linker_oberarm": {"vom_koerper_weg_grad": 40.0}},
        dict(rig.joint_positions), rig=rig)
    bind = rig.body_meshes["links_arm"]
    posed = ctx.apply_body("links_arm", bind)
    spans = {seg: (s, e) for seg, s, e in rig.segment_spans["links_arm"]}
    su, eu = spans["LeftUpperArm"]
    shoulder = rig.joint_positions["LeftUpperArm"]
    # Lokale Z-Achse (vom Koerper weg) ist im gedrehten Frame die Welt-X-
    # Achse: Ry(90) * Rz * Ry(90)^-1 = Rx. Eine Welt-Z-Drehung (alter Code)
    # wuerde x/y aendern, aber z unveraendert lassen - hier ist es umgekehrt.
    bewegt = False
    for i in range(su, eu):
        assert posed.positions[i][0] == pytest.approx(bind.positions[i][0], abs=1e-9)
        if abs(posed.positions[i][2] - bind.positions[i][2]) > 1e-6:
            bewegt = True
        # Start starr um die Schulter: Abstand bleibt erhalten.
        assert _dist(posed.positions[i], shoulder) == pytest.approx(
            _dist(bind.positions[i], shoulder), abs=1e-9)
    assert bewegt, "Pose muss um die Schulter drehen"


# ---------------------------------------------------------------------------
# 5) Nachskalierte Teile behalten ihre Skin-Gewichte (sanity_check)
# ---------------------------------------------------------------------------

def test_transform_und_rotate_erhalten_skinning():
    skin = roblox_mesh.SkinningData(
        bones=[roblox_mesh.SkinBone(
            name="RightUpperArm", parent=-1,
            position=(0.0, 0.5, 0.0),
            rotation=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        )],
        influences=[((0, 0, 0, 0), (1.0, 0.0, 0.0, 0.0))],
    )
    mesh = roblox_mesh.make_box(1.0, 1.0, 1.0)
    mesh.skinning = skin

    # sanity_check skaliert um die Teilemitte auf die Zielgroesse.
    rescaled = (mesh.transform(1.0, (-0.5, -0.5, -0.5))
                .transform(0.5)
                .transform(1.0, (0.5, 0.5, 0.5)))
    assert rescaled.skinning is not None
    bone = rescaled.skinning.bones[0]
    assert bone.name == "RightUpperArm"
    # Bone-Position wandert exakt wie die Vertices mit:
    # (0, 0.5, 0) -> [-0.5] -> [*0.5] -> [+0.5] = (0.25, 0.5, 0.25).
    assert bone.position == pytest.approx((0.25, 0.5, 0.25), abs=1e-9)
    assert rescaled.skinning.influences[0][1][0] == pytest.approx(1.0)

    turned = rescaled.rotate(((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0)))
    assert turned.skinning is not None
    # Ry(90): x -> z, z -> -x; y bleibt.
    assert turned.skinning.bones[0].position == pytest.approx(
        (0.25, 0.5, -0.25), abs=1e-9)


def test_geskinntes_mesh_bleibt_nach_sanity_check_weich():
    """Ein nachskaliertes Teil darf nicht starr werden (Regression)."""
    import build_avatar

    skin = roblox_mesh.SkinningData(
        bones=[roblox_mesh.SkinBone(
            name="RightUpperArm", parent=-1,
            position=(0.0, 0.0, 0.0),
            rotation=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        )],
        influences=[((0, 0, 0, 0), (1.0, 0.0, 0.0, 0.0))],
    )
    mesh = roblox_mesh.make_box(400.0, 400.0, 400.0)
    mesh.skinning = skin
    warnings = []
    fixed = build_avatar.sanity_check(mesh, 2.0, "TestHandschuh", warnings)
    assert warnings and "Sanity-Check" in warnings[0]
    assert fixed.skinning is not None, "Skin-Gewichte duerfen nicht verloren gehen"
    assert fixed.size()[0] == pytest.approx(2.0, rel=1e-3)


# ---------------------------------------------------------------------------
# 6) Posen.toml-Drehungen wirken im Gelenk-Frame (gedrehte Attachments)
# ---------------------------------------------------------------------------

def _rotate_with_frame(frame, degrees, axis=(1.0, 0.0, 0.0)):
    """Prueft: J * R * J^-1 dreht um die Gelenk-Achse, nicht um die Weltachse."""
    pivot = (0.3, 4.0, -0.2)
    rotation = [0.0, 0.0, 0.0]
    rotation[0] = degrees  # X-Achse (nicken / ellbogen)
    ctx = avatar_pose.PoseContext({"kopf": {"nicken_grad": degrees}},
                                  {"Head": pivot})
    ctx.rig = None
    local_world = avatar_pose._rotation_local(rotation, (0.0, 0.0, 0.0),
                                              pivot, None)
    local_frame = avatar_pose._rotation_local(rotation, (0.0, 0.0, 0.0),
                                              pivot, frame)
    point = (pivot[0] + 0.5, pivot[1] + 0.2, pivot[2] + 0.1)
    return (avatar_pose._mat4_point(local_world, point),
            avatar_pose._mat4_point(local_frame, point))


def test_rotation_local_ohne_frame_bleibt_weltachsen_drehung():
    frame = ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0))
    world, _framed = _rotate_with_frame(frame, 30.0)
    # Ohne Rig bleibt es die alte Weltachsen-Drehung (hier um X): x konstant.
    assert world[0] == pytest.approx(0.8, abs=1e-9)
    assert world[1] != pytest.approx(4.2, abs=1e-6)


def test_rotation_local_dreht_um_die_gelenk_achse():
    """Gelenk-Frame um Y gedreht: die lokale X-Achse ist jetzt Welt-Z."""
    frame = ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0))
    _world, framed = _rotate_with_frame(frame, 90.0)
    # Drehung um die Welt-Z-Achse laesst z unveraendert, bewegt aber x/y.
    assert framed[2] == pytest.approx(-0.1, abs=1e-9)
    assert abs(framed[1] - 4.2) > 0.2 or abs(framed[0] - 0.8) > 0.2
    # Der Gelenkpunkt selbst bleibt fix.
    pivot = (0.3, 4.0, -0.2)
    local = avatar_pose._rotation_local([90.0, 0.0, 0.0], (0.0, 0.0, 0.0),
                                        pivot, frame)
    assert avatar_pose._mat4_point(local, pivot) == pytest.approx(pivot, abs=1e-9)


def test_rotation_local_erhaelt_abstaende():
    frame = ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0))
    pivot = (0.3, 4.0, -0.2)
    local = avatar_pose._rotation_local([25.0, -10.0, 40.0], (0.0, 0.0, 0.0),
                                        pivot, frame)
    for offset in ((0.5, 0.0, 0.0), (0.0, -0.7, 0.2)):
        point = (pivot[0] + offset[0], pivot[1] + offset[1], pivot[2] + offset[2])
        moved = avatar_pose._mat4_point(local, point)
        assert _dist(moved, pivot) == pytest.approx(_dist(point, pivot), abs=1e-9)
