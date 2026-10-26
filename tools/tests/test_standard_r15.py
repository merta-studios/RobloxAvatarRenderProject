"""Exakter R15-Standard-Avatar (Blocky) - Geometrie, Gelenke, Anker.

Bug-Report (Runde 12): "Der Standard Avatar ist nicht exakt richtig",
"die Posen werden nicht richtig angewendet, weil die einzelnen
Koerperteile nicht richtig gerigt sind, die pivots verrutscht sind
(Schultern zu weit nach unten, Brille zu weit ueber den Augen)".

Statt Werte zu raten, nutzt die Pipeline jetzt Roblox' **offizielle**
Referenzdatei ``BlockyCharacter.fbx`` ("Standard R15 rigs -> Blocky",
das Template mit "a standard R15 rig and attachments"):
``tools/standard_r15.py`` liest daraus ``reference/standard_r15_rig.json``
(Geometrie, Skelett-Gelenke, ``*_Att``-Accessoire-Anker). Hier wird
festgeschrieben, dass

* der FBX-Reader die Transformationen korrekt liest (Properties70) -
  sonst landen alle Marker auf dem Ursprung,
* die Spezifikation die 15 Teile mit ihren RigAttachments liefert,
* Kopf-Anker exakt dort sitzen, wo Roblox sie hat (FaceFront auf der
  Kopf-Mitte an der Vorderflaeche - Brillen sitzen damit wie im Spiel),
* die Schulter am Torso-Rand knapp unter der Oberkante liegt (nicht
  0,237 Studs darunter - das war der "Schultern zu tief"-Bug),
* Accessoires exakt verschweisst werden und Posen um die echten
  Gelenke drehen.

    python3 -m pytest tools/tests/test_standard_r15.py -q
"""

from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

import avatar_pose  # noqa: E402
import avatar_rig  # noqa: E402
import fbx  # noqa: E402
import roblox_api  # noqa: E402
import roblox_mesh  # noqa: E402
import standard_r15  # noqa: E402

I3 = avatar_rig.IDENTITY_MAT3


def _att(pos, rot=None):
    return {"position": pos, "rotation": rot or I3}


def _rig(**kw):
    return avatar_rig.assemble_r15_rig(
        {}, None, dict(roblox_api.DEFAULT_BODY_COLORS),
        dict(roblox_api.DEFAULT_SCALES), headless=False,
        standard_segments=standard_r15.standard_segments(), **kw)


# ---------------------------------------------------------------------------
# 1) FBX-Reader: Transformationen aus Properties70
# ---------------------------------------------------------------------------

def test_fbx_reader_liest_transformationen_aus_properties70():
    """Regression: ohne Properties70-Lesart lagen alle Marker auf (0,0,0)."""
    path = os.path.join(standard_r15.ROOT, "reference", "official",
                        "BlockyCharacter.fbx")
    if not os.path.exists(path):
        pytest.skip("BlockyCharacter.fbx fehlt")
    doc = fbx.read_document(path)
    face = doc.markers.get("FaceFront_Att")
    assert face is not None
    assert face != (0.0, 0.0, 0.0)
    assert face[1] > 1.0          # Kopfhoehe ueber dem Ursprung
    assert face[2] > 0.0          # Maya-Konvention: Gesicht zeigt +Z
    hat = doc.markers.get("Hat_Att")
    assert hat[1] > face[1]       # Hut ueber den Augen


# ---------------------------------------------------------------------------
# 2) Spezifikation: 15 Teile, exakte Gelenke und Anker
# ---------------------------------------------------------------------------

def test_spezifikation_liefert_alle_15_teile_mit_rigattachments():
    spec = standard_r15.load()
    assert spec is not None, "reference/standard_r15_rig.json fehlt"
    parts = spec["parts"]
    assert set(standard_r15.PART_NAMES) == set(parts)
    for name, part in parts.items():
        atts = part["attachments"]
        rig_atts = [a for a in atts if a.endswith("RigAttachment")]
        assert rig_atts, f"{name} ohne RigAttachments"
        assert len(part["vertices"]) > 30


def test_kopf_anker_exakt_wie_in_blockycharacter_fbx():
    """FaceFront auf Kopf-Mitte an der Vorderflaeche, Hat auf der Oberkante.

    Genau die Marker ``FaceFront_Att``/``Hat_Att``/``Neck_Att`` der
    offiziellen Referenz - Brillen und Huete sitzen damit wie im Spiel,
    statt auf geratenen Punkten ("Brille zu weit ueber den Augen").
    """
    head = standard_r15.load()["parts"]["Head"]
    atts = head["attachments"]
    w, h, d = head["size"]
    front = atts["FaceFrontAttachment"]["position"]
    assert front[1] == pytest.approx(0.0, abs=0.02)          # Kopf-Mitte
    assert front[2] == pytest.approx(-0.5 * d, abs=0.02)     # Vorderflaeche
    assert abs(front[0]) < 0.02
    hat = atts["HatAttachment"]["position"]
    assert hat[1] == pytest.approx(0.5 * h, abs=0.03)        # Oberkante
    assert atts["HairAttachment"]["position"] == pytest.approx(hat, abs=1e-3)
    neck = atts["NeckRigAttachment"]["position"]
    assert neck[1] == pytest.approx(-0.5 * h, abs=0.03)      # Kinn/Unterkante
    # NeckAttachment (Halsketten) sitzt unter dem Kopf.
    assert atts["NeckAttachment"]["position"][1] < neck[1]


def test_schulter_und_hueft_gelenke_exakt():
    """Schulter am Torso-Rand knapp unter der Oberkante, Huefte unten."""
    parts = standard_r15.load()["parts"]
    ut = parts["UpperTorso"]
    _w, h, _d = ut["size"]
    shoulder = ut["attachments"]["LeftShoulderRigAttachment"]["position"]
    assert shoulder[0] == pytest.approx(-1.0, abs=0.02)      # Torso-Seitenkante
    assert (0.5 * h - shoulder[1]) / h == pytest.approx(0.0286, abs=0.002)
    lt = parts["LowerTorso"]
    hip = lt["attachments"]["LeftHipRigAttachment"]["position"]
    assert hip[1] < 0.0                                     # unterhalb des Ursprungs
    # Ellbogen-/Kniegelenke sitzen NICHT in der Segmentmitte.
    for seg, att in (("LeftUpperArm", "LeftElbowRigAttachment"),
                     ("LeftUpperLeg", "LeftKneeRigAttachment")):
        p = parts[seg]["attachments"][att]["position"]
        assert abs(p[1]) > 0.1, f"{att} klebt in {seg} in der Mitte"


# ---------------------------------------------------------------------------
# 3) Montage: exakte Pivots + Accessoire-Welds
# ---------------------------------------------------------------------------

def test_standard_rig_schulter_sitzt_nicht_zu_tief():
    """Der 'Schultern zu weit nach unten'-Bug: die Schaetz-Konstante legte
    die Schulter 0,237 Studs unter die Torso-Oberkante. Das exakte
    Standard-Rig hat sie 0,045 Studs darunter (2,86 % der Torso-Hoehe)."""
    rig = _rig()
    t_lo, t_hi = rig.body_meshes["torso"].bbox()
    torso_h = t_hi[1] - t_lo[1]
    shoulder = rig.joint_positions["LeftUpperArm"]
    assert shoulder[0] == pytest.approx(t_lo[0], abs=1e-3)   # Torso-Kante
    assert (t_hi[1] - shoulder[1]) == pytest.approx(0.0286 * torso_h, abs=0.02)
    # Gelenk = Attachment (Motor6D-Semantik), nicht Segmentmitte.
    assert shoulder == pytest.approx(
        rig.world_attachments["LeftShoulderRigAttachment"][0], abs=1e-6)


def test_brille_verschweisst_exakt_an_facefront():
    """Ein Gesichts-Accessoire mit ``FaceFrontAttachment`` landet exakt auf
    dem Standard-Anker: Kopf-Mitte, Vorderflaeche - nicht darueber."""
    rig = _rig()
    h_lo, h_hi = rig.head_mesh.bbox()
    face = rig.world_attachments["FaceFrontAttachment"][0]
    assert face[1] == pytest.approx((h_lo[1] + h_hi[1]) / 2.0, abs=0.02)
    assert face[2] == pytest.approx(h_lo[2], abs=0.02)       # Vorderflaeche (-Z)

    brille = {
        "label": "Brille", "rbxm_class": "MeshPart", "transparency": 0.0,
        "type_id": 42,
        "mesh": roblox_mesh.make_box(0.5, 0.15, 0.1),
        "attachments": {"FaceFrontAttachment": _att((0.0, 0.0, 0.0))},
    }
    placed, note = avatar_rig.weld_accessory(brille, rig)
    assert placed is not None
    p_lo, p_hi = placed.bbox()
    # Handle-Ursprung (das Attachment) sitzt exakt auf dem Gesichtspunkt.
    # Die Box haengt symmetrisch darum - ihr Zentrum == FaceFront.
    assert (p_lo[1] + p_hi[1]) / 2.0 == pytest.approx(face[1], abs=1e-3)
    assert (p_lo[2] + p_hi[2]) / 2.0 == pytest.approx(face[2], abs=1e-3)
    assert "FaceFrontAttachment" in note


def test_hut_verschweisst_exakt_an_hatattachment():
    rig = _rig()
    hat_world = rig.world_attachments["HatAttachment"][0]
    h_lo, h_hi = rig.head_mesh.bbox()
    assert hat_world[1] == pytest.approx(h_hi[1], abs=0.03)  # Oberkante
    hut = {
        "label": "Hut", "rbxm_class": "MeshPart", "transparency": 0.0,
        "type_id": 8,
        "mesh": roblox_mesh.make_box(0.6, 0.3, 0.6),
        "attachments": {"HatAttachment": _att((0.0, 0.15, 0.0))},
    }
    placed, _note = avatar_rig.weld_accessory(hut, rig)
    assert placed is not None
    _lo, hi = placed.bbox()
    # Das Attachment der Box liegt auf deren Oberkante - nach dem Weld sitzt
    # die Oberkante exakt auf dem Kopf-Anker.
    assert hi[1] == pytest.approx(hat_world[1], abs=1e-3)


def test_pose_dreht_um_die_exakten_standard_gelenke():
    """Die Pose dreht den Oberarm um das echte Schultergelenk - der Punkt,
    an dem die Rotation stattfindet, bleibt stehen."""
    rig = _rig()
    shoulder = rig.joint_positions["LeftUpperArm"]
    ctx = avatar_pose.PoseContext(
        {"linker_oberarm": {"vom_koerper_weg_grad": 60.0}},
        dict(rig.joint_positions), rig=rig)
    assert ctx.active
    assert ctx.transform_point(shoulder, "linker_oberarm") == pytest.approx(
        shoulder, abs=1e-6)   # Drehpunkt bleibt
    probe = (shoulder[0], shoulder[1] - 0.5, shoulder[2])
    moved = ctx.transform_point(probe, "linker_oberarm")
    # Starr um das Schultergelenk: Abstand bleibt, Arm schwenkt nach aussen.
    dist = sum((moved[i] - shoulder[i]) ** 2 for i in range(3)) ** 0.5
    assert dist == pytest.approx(0.5, abs=1e-6)
    assert abs(moved[0] - probe[0]) > 0.2
    assert moved[1] > probe[1] + 0.1


# ---------------------------------------------------------------------------
# 4) Proportionen der Notfall-Schaetzung stammen aus dem Standard-Rig
# ---------------------------------------------------------------------------

def test_schaetz_proportionen_stammen_aus_dem_standard_rig():
    props = standard_r15.joint_proportions()
    assert props, "Proportionen aus reference/standard_r15_rig.json fehlen"
    assert avatar_pose.SHOULDER_BELOW_TORSO_TOP_FRAC == pytest.approx(
        props["shoulder_below_torso_top_frac"], abs=1e-9)
    assert avatar_pose.ELBOW_FRACTION_OF_ARM == pytest.approx(
        props["elbow_fraction_of_arm"], abs=1e-9)
    assert avatar_pose.WRIST_FRACTION_OF_ARM == pytest.approx(
        props["wrist_fraction_of_arm"], abs=1e-9)
    assert avatar_pose.KNEE_FRACTION_OF_LEG == pytest.approx(
        props["knee_fraction_of_leg"], abs=1e-9)
    assert avatar_pose.ANKLE_FRACTION_OF_LEG == pytest.approx(
        props["ankle_fraction_of_leg"], abs=1e-9)


def test_kopf_anker_helper_skaliert_mit_dem_kopfradius():
    small = standard_r15.head_attachments((0.0, 1.0, 0.0), 0.3)
    big = standard_r15.head_attachments((0.0, 1.0, 0.0), 0.6)
    s = small["FaceFrontAttachment"][0]
    b = big["FaceFrontAttachment"][0]
    # Auf Augenhoehe (Kopf-Mitte), Vorderabstand skaliert mit dem Radius.
    assert s[1] == pytest.approx(1.0, abs=0.02)
    assert b[1] == pytest.approx(1.0, abs=0.02)
    assert abs(s[2]) == pytest.approx(0.5 * abs(b[2]), rel=0.03)
    assert abs(b[2]) == pytest.approx(0.6, rel=0.05)
