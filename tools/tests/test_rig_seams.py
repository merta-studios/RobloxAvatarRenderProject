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
    # Die Segmente behalten ihre Mesh-Groesse (nichts wird gestaucht):
    # Armhoehe = 1,1687 + 1,0519 + 0,2504 = 2,471 Studs.
    arm_len = 1.1687 + 1.0519 + 0.2504
    assert hi[1] - lo[1] == pytest.approx(arm_len, abs=2e-3)

    shoulder = rig.joint_positions["LeftUpperArm"]
    elbow = rig.joint_positions["LeftLowerArm"]
    wrist = rig.joint_positions["LeftHand"]
    # Gelenke liegen genau an den Segmentgrenzen (keine Luecke, keine
    # Ueberlappung) - nicht in der Teilmitte.
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

    # Ein Gelenk, das exakt auf dem Elterngelenk klebt, ist ein Platzhalter:
    # es wird an die Segmentunterkante gelegt (statt den Unterarm in den
    # Oberarm zu schieben) - der Unterarm beginnt dort und haengt darunter.
    arm = rig.body_meshes["links_arm"]
    spans = {seg: (s, e) for seg, s, e in rig.segment_spans["links_arm"]}
    sl, el = spans["LeftLowerArm"]
    lower_top = max(arm.positions[i][1] for i in range(sl, el))
    lower_bottom = min(arm.positions[i][1] for i in range(sl, el))
    assert wrist[1] < elbow[1]
    # Gelenk an der Deckflaeche: Oberkante des Unterarms == Ellbogen,
    # Unterkante == Handgelenk (skalenunabhaengig, keine Teilmitte).
    assert lower_top == pytest.approx(elbow[1], abs=1e-6)
    assert lower_bottom == pytest.approx(wrist[1], abs=1e-6)
    lo, hi = arm.bbox()
    # Kein Segment wurde gestaucht: die Armhoehe ist mindestens so gross wie
    # das laengste Einzelsegment (1,17 Studs) - nicht auf 0,x zusammengedrueckt.
    assert hi[1] - lo[1] > 1.0
    # Die Platzhalter-Erkennung ist im Audit dokumentiert.
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


# ---------------------------------------------------------------------------
# 7) Falsch skalierte Bundles: nichts wird gestaucht
# ---------------------------------------------------------------------------

def test_grosse_bundles_werden_nicht_gestaucht():
    """Regression: der Korblox-Arm ist absichtlich groesser als ein Standard-
    R15-Arm (2,6-Studs-Oberarm). Fruehere Retargeting-Versionen stauchten ihn
    auf 0,9 Studs ("Arme plattgedrueckt") und schoben die Kette nach unten.
    Jetzt gilt: Mesh-Groessen bleiben, Gelenke sitzen auf den Attachments."""
    sizes = {
        "LeftUpperArm": (0.6641, 2.6216, 0.8129),
        "LeftLowerArm": (0.8138, 0.9094, 0.8621),
        "LeftHand": (0.7628, 0.9388, 0.7475),
    }
    segs = _arm_groups([
        _seg("LeftUpperArm", sizes["LeftUpperArm"], {
            "LeftShoulderRigAttachment": _att((0.2901, 0.3839, -0.0241)),
            "LeftElbowRigAttachment": _att((-0.2099, -1.1479, -0.0241)),
        }),
        _seg("LeftLowerArm", sizes["LeftLowerArm"], {
            "LeftElbowRigAttachment": _att((-0.0612, 0.3842, -0.0002)),
            "LeftWristRigAttachment": _att((-0.0612, -0.3758, -0.0002)),
        }),
        _seg("LeftHand", sizes["LeftHand"], {
            "LeftWristRigAttachment": _att((-0.0155, 0.1225, -0.0062)),
        }),
    ])
    rig = _assemble(segs)
    arm = rig.body_meshes["links_arm"]
    lo, hi = arm.bbox()
    # Der Oberarm behaelt seine 2,62 Studs Hoehe (keine Stauchung).
    spans = {seg: (s, e) for seg, s, e in rig.segment_spans["links_arm"]}
    su, eu = spans["LeftUpperArm"]
    upper_h = (max(arm.positions[i][1] for i in range(su, eu))
               - min(arm.positions[i][1] for i in range(su, eu)))
    assert upper_h == pytest.approx(sizes["LeftUpperArm"][1], abs=1e-3)
    # Der ganze Arm ist entsprechend lang (kein 1,74-Studs-Zwangsraster).
    assert hi[1] - lo[1] > 2.5
    # Gelenke exakt auf den authorten Attachment-Markern.
    shoulder = rig.joint_positions["LeftUpperArm"]
    elbow = rig.joint_positions["LeftLowerArm"]
    wrist = rig.joint_positions["LeftHand"]
    assert _dist(shoulder, elbow) == pytest.approx(
        math.dist((0.2901, 0.3839), (-0.2099, -1.1479)), abs=1e-6)
    assert _dist(elbow, wrist) == pytest.approx(
        math.dist((-0.0612, 0.3842), (-0.0612, -0.3758)), abs=1e-6)
    # Naht: die Segmente ueberlappen sich am Ellbogen (kein Spalt) - so
    # montiert auch Roblox, weil sich die Meshes ueber das Gelenk hinaus
    # erstrecken.
    sl, el = spans["LeftLowerArm"]
    upper_bottom = min(arm.positions[i][1] for i in range(su, eu))
    lower_top = max(arm.positions[i][1] for i in range(sl, el))
    assert upper_bottom < lower_top
    # Nichts wurde umgerastert -> keine Raster-Warnung.
    assert not any("gestaucht" in note for note in rig.notes)


# ---------------------------------------------------------------------------
# 8) Inkonsistente Bundles: ein Faktor pro Extremitaet, Fuesse auf dem Boden
# ---------------------------------------------------------------------------

def test_kurzes_bein_wird_an_das_andere_angeglichen():
    """Korblox-Bein (real): Huefte->Knie 0,86, Knie->Fussgelenk 0,60 - zusammen
    deutlich kuerzer als das linke Bein. Vorher stand der Avatar auf einem Bein
    in der Luft. Weil Roblox selbst nie skaliert, wird nur *angeglichen*: das
    kuerzere Bein bekommt die Laenge des laengeren - mit einem Faktor auf allen
    Achsen, damit nichts plattgedrueckt wird."""
    segs = {
        "torso": _torso(),
        "links_bein": [
            _seg("LeftUpperLeg", (0.8153, 1.0267, 0.8159), {
                "LeftHipRigAttachment": _att((-0.0529, 0.316, 0.0169)),
                "LeftKneeRigAttachment": _att((-0.0529, -0.506, 0.0169)),
            }),
            _seg("LeftLowerLeg", (0.8301, 1.0376, 0.9252), {
                "LeftKneeRigAttachment": _att((-0.0169, 0.2896, -0.0499)),
                "LeftAnkleRigAttachment": _att((-0.0169, -0.6364, -0.0499)),
            }),
            _seg("LeftFoot", (0.727, 0.5679, 1.2969), {
                "LeftAnkleRigAttachment": _att((0.0129, -0.0566, 0.1421)),
            }),
        ],
        "rechts_bein": [
            # Originalgroessen des Korblox-Bundles: deutlich kleiner als ein
            # Standardbein, der Fuss ist ein 0,26-Studs-Wuerfel.
            _seg("RightUpperLeg", (0.3142, 0.8622, 0.3906), {
                "RightHipRigAttachment": _att((0.0188, 0.2418, 0.03)),
                "RightKneeRigAttachment": _att((0.1168, -0.7032, -0.1079)),
            }),
            _seg("RightLowerLeg", (0.3501, 0.5965, 0.2993), {
                "RightKneeRigAttachment": _att((0.0103, 0.2647, -0.0671)),
                "RightAnkleRigAttachment": _att((0.0103, -0.6616, 0.0598)),
            }),
            _seg("RightFoot", (0.2636, 0.2636, 0.2636), {
                "RightAnkleRigAttachment": _att((0.0, 0.0, 0.0)),
            }),
        ],
    }
    rig = _assemble(segs)
    left = _dist(rig.joint_positions["LeftUpperLeg"],
                 rig.joint_positions["LeftFoot"])
    right = _dist(rig.joint_positions["RightUpperLeg"],
                  rig.joint_positions["RightFoot"])
    # Beide Beine sind gleich lang - kein Bein in der Luft.
    assert right == pytest.approx(left, rel=0.02)
    # Und zwar ohne Verzerrung: Hoehe/Breite des rechten Oberschenkels bleibt
    # wie authored (0,8622/0,3142) - gestaucht wurde nichts.
    bein = rig.body_meshes["rechts_bein"]
    spans = {seg: (a, b) for seg, a, b in rig.segment_spans["rechts_bein"]}
    s0, e0 = spans["RightUpperLeg"]
    ys = [bein.positions[i][1] for i in range(s0, e0)]
    xs = [bein.positions[i][0] for i in range(s0, e0)]
    assert (max(ys) - min(ys)) / (max(xs) - min(xs)) == pytest.approx(
        0.8622 / 0.3142, rel=0.02)
    assert any("gleichmaessig auf allen Achsen" in n for n in rig.notes)


def test_gleich_lange_beine_bleiben_unskaliert():
    """Beine aus demselben Koerperpaket (z. B. "Woman") sind von Haus aus
    laenger als die R15-Norm - Roblox rendert sie trotzdem so. Es wird nur
    angeglichen, nie auf eine Normlaenge gestaucht."""
    segs = {
        "torso": _torso(),
        "links_bein": [
            _seg("LeftUpperLeg", (0.8153, 1.0267, 0.8159), {
                "LeftHipRigAttachment": _att((-0.0529, 0.316, 0.0169)),
                "LeftKneeRigAttachment": _att((-0.0529, -0.506, 0.0169)),
            }),
            _seg("LeftLowerLeg", (0.8301, 1.0376, 0.9252), {
                "LeftKneeRigAttachment": _att((-0.0169, 0.2896, -0.0499)),
                "LeftAnkleRigAttachment": _att((-0.0169, -0.6364, -0.0499)),
            }),
        ],
        "rechts_bein": [
            _seg("RightUpperLeg", (0.8153, 1.0267, 0.8159), {
                "RightHipRigAttachment": _att((0.0529, 0.316, 0.0169)),
                "RightKneeRigAttachment": _att((0.0529, -0.506, 0.0169)),
            }),
            _seg("RightLowerLeg", (0.8301, 1.0376, 0.9252), {
                "RightKneeRigAttachment": _att((0.0169, 0.2896, -0.0499)),
                "RightAnkleRigAttachment": _att((0.0169, -0.6364, -0.0499)),
            }),
        ],
    }
    rig = _assemble(segs)
    bein = rig.body_meshes["links_bein"]
    spans = {seg: (a, b) for seg, a, b in rig.segment_spans["links_bein"]}
    s0, e0 = spans["LeftUpperLeg"]
    ys = [bein.positions[i][1] for i in range(s0, e0)]
    # Oberschenkel behaelt exakt seine authored Hoehe (1,0267 Studs).
    assert max(ys) - min(ys) == pytest.approx(1.0267, abs=1e-3)
    # Und es wurde nichts nachskaliert.
    assert not any("Faktor" in n for n in rig.notes)


def test_langer_arm_wird_nicht_gestaucht():
    """Der Korblox-Arm ist absichtlich laenger als der Standardarm (Stacheln
    ueber der Schulter). Arme werden nur verlaengert, nie gestaucht."""
    rig = _assemble(_arm_groups([
        _seg("LeftUpperArm", (0.6641, 1.2, 0.8129), {
            "LeftShoulderRigAttachment": _att((0.29, 0.3839, -0.0241)),
            "LeftElbowRigAttachment": _att((-0.21, -0.6119, -0.0241)),
        }),
        _seg("LeftLowerArm", (0.8138, 1.0, 0.8621), {
            "LeftElbowRigAttachment": _att((-0.0612, 0.3842, -0.0002)),
            "LeftWristRigAttachment": _att((-0.0612, -0.5758, -0.0002)),
        }),
    ]))
    # Kette laenger als kanonisch (1,49) -> bleibt unveraendert.
    span = _dist(rig.joint_positions["LeftUpperArm"],
                 rig.joint_positions["LeftHand"])
    assert span > 1.75
    assert not any("Faktor" in n for n in rig.notes)


# ---------------------------------------------------------------------------
# 9) Schulterlinie und Platzhalter-Beine
# ---------------------------------------------------------------------------

def _torso_mit_schulter(y_in_upper: float):
    torso = _torso(with_shoulder=True)
    torso[1]["attachments"]["LeftShoulderRigAttachment"] = _att((-1.0, y_in_upper, 0.0))
    torso[1]["attachments"]["RightShoulderRigAttachment"] = _att((1.0, y_in_upper, 0.0))
    return torso


def _arm_segmente():
    return [
        _seg("LeftUpperArm", (1.0, 1.1687, 1.0), {
            "LeftShoulderRigAttachment": _att((0.5, 0.394327, 0.0)),
            "LeftElbowRigAttachment": _att((0.000479, -0.334056, 0.0)),
        }),
        _seg("LeftLowerArm", (1.0, 1.0519, 1.0), {
            "LeftElbowRigAttachment": _att((0.000479, 0.258688, 0.0)),
            "LeftWristRigAttachment": _att((0.000479, -0.268097, 0.0)),
        }),
    ]


def test_zu_tiefe_schulter_wird_auf_die_schulterlinie_gesetzt():
    """Bug: "Schultern sind viel zu weit nach unten gerutscht". Manche Bundles
    tragen ein LeftShoulderRigAttachment weit unten im Torso - dann haengen die
    Arme mitten am Bauch. Marker ausserhalb des R15-Bandes (tiefer als 55 % der
    Torso-Hoehe) werden auf die Schulterlinie (25 % unter der Oberkante)
    gezogen."""
    # Oberkante bei +0.8, Torso 2,0 hoch -> Marker bei -0.6 = 70 % darunter.
    rig = _assemble({"torso": _torso_mit_schulter(-0.6),
                     "links_arm": _arm_segmente()})
    t_lo, t_hi = rig.body_meshes["torso"].bbox()
    shoulder = rig.joint_positions["LeftUpperArm"]
    # R15-Schulterlinie: 0,5 Studs unter der Oberkante bei 2 Studs Hoehe.
    assert shoulder[1] == pytest.approx(t_hi[1] - 0.25 * (t_hi[1] - t_lo[1]),
                                        abs=1e-3)
    assert any("R15-Schulterlinie" in n for n in rig.notes)
    # Und der Arm haengt daran (Oberkante im oberen Torso-Drittel).
    arm_lo, arm_hi = rig.body_meshes["links_arm"].bbox()
    assert arm_hi[1] > t_hi[1] - 0.35 * (t_hi[1] - t_lo[1])


def test_autoren_schulter_bleibt_auf_ihrem_platz():
    """Regression: eine Fassung zog *jede* Schulter auf 4 % unter die
    Torso-Oberkante und haengte die Arme damit zu hoch ("Schultern sind zu
    hoch"). Autoren-Attachments innerhalb des R15-Bandes bleiben unangetastet -
    auch das der Woman-Bundle-Torsi (rund 28 % unter der Oberkante)."""
    # 0.30 unter der Oberkante von 1.6 = 19 % -> mitten im Band.
    rig = _assemble({"torso": _torso_mit_schulter(0.5),
                     "links_arm": _arm_segmente()})
    t_lo, t_hi = rig.body_meshes["torso"].bbox()
    torso_h = t_hi[1] - t_lo[1]
    shoulder = rig.joint_positions["LeftUpperArm"]
    # Genau auf dem Attachment (0.5 in UpperTorso-Lokalkoordinaten) - nicht
    # nach oben gezogen.
    assert shoulder[1] == pytest.approx(t_hi[1] - 0.30, abs=1e-3)
    assert shoulder[1] < t_hi[1] - 0.12 * torso_h
    assert not any("Schulterlinie" in n for n in rig.notes)


def test_schulter_ueber_der_torso_oberkante_wird_gezogen():
    """Ein Marker oberhalb der Torso-Oberkante ist unbrauchbar (Arme schweben
    ueber den Schultern) - er wird ebenfalls auf die Schulterlinie gesetzt."""
    rig = _assemble({"torso": _torso_mit_schulter(1.2),
                     "links_arm": _arm_segmente()})
    t_lo, t_hi = rig.body_meshes["torso"].bbox()
    shoulder = rig.joint_positions["LeftUpperArm"]
    assert shoulder[1] <= t_hi[1]
    assert shoulder[1] == pytest.approx(t_hi[1] - 0.25 * (t_hi[1] - t_lo[1]),
                                        abs=1e-3)
    assert any("Schulterlinie" in n for n in rig.notes)


def test_platzhalter_beine_sind_kein_einzelner_klotz():
    """Ohne Bein-Assets im Avatar (z. B. SunsetSiya) entstanden vorher zwei
    riesige Quader - ein Klotz pro Bein, so hoch wie der Torso. Jetzt gibt es
    Ober-/Unterschenkel und einen Fuss, und der Fuss steht auf dem Boden."""
    rig = _assemble({"torso": _torso()})
    bein = rig.body_meshes["links_bein"]
    spans = rig.segment_spans["links_bein"]
    # Drei Segmente: Ober-/Unterschenkel + Fuss (Knie beugbar).
    assert [s for s, _a, _b in spans] == ["LeftUpperLeg", "LeftLowerLeg", "LeftFoot"]
    lo, hi = bein.bbox()
    assert lo[1] == pytest.approx(0.0, abs=1e-6)
    t_lo, t_hi = rig.body_meshes["torso"].bbox()
    torso_h = t_hi[1] - t_lo[1]
    # Beinlaenge (Huefte bis Sohle) = Torso-Hoehe: kein Riesenklotz.
    assert hi[1] - lo[1] == pytest.approx(torso_h, rel=0.05)
    # Ein Bein ist 46 % so breit wie der Torso (Standard-R15: halbe
    # Torso-Breite je Bein) und beruehrt die Mitte nicht (zwei getrennte
    # Beine, kein verschmolzener Klotz).
    assert hi[0] - lo[0] == pytest.approx(0.46 * (t_hi[0] - t_lo[0]), rel=0.05)
    inner = min(abs(lo[0]), abs(hi[0]))  # Abstand der Innenkante zur Mitte
    assert inner > 0.02
    # Und es ist nicht tiefer als der Torso (sonst wirkt es wie ein Klotz).
    assert hi[2] - lo[2] < 1.3 * (t_hi[2] - t_lo[2])
    # Der Fuss steht vorne ueber (kein flacher Abschluss).
    s_f, e_f = {s: (a, b) for s, a, b in spans}["LeftFoot"]
    z_foot = [bein.positions[i][2] for i in range(s_f, e_f)]
    s_u, e_u = {s: (a, b) for s, a, b in spans}["LeftUpperLeg"]
    z_upper = [bein.positions[i][2] for i in range(s_u, e_u)]
    assert min(z_foot) < min(z_upper) - 0.02
    assert any("kein Klotz" in n for n in rig.notes)
