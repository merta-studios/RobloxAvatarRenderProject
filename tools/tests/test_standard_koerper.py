"""Der klassische Standard-Koerper aus Roblox' Mannequin.

Bug-Report (Runde 6): "die Standard-Avatar-Teile sehen immer noch nicht wie die
exakten Teile aus" und "die Schultern sind wieder zu weit oben".

Ursache (an den Daten belegt, nicht geschaetzt):

* Die Koerperteile dieses Avatars sind **klassische** Teile. Ihre Roh-Meshes
  stimmen auf drei Nachkommastellen mit ``ClassicMannequin.fbx`` ueberein
  (Oberarm 0.749 x 0.888 x 0.584 usw.) - es sind also genau die offiziellen
  Standard-Teile, nur eben nicht die R15-*Block*-Meshes.
* Die Schulter wurde anschliessend von ``R15_ARM_TOP_BAND`` ("hoechstens 2-6 %
  unter der Torso-Oberkante") auf 4 % unter die Oberkante gezogen. Im
  Mannequin sitzt die Armoberkante aber **21,6 %** der Torso-Hoehe unter der
  Oberkante - der Arm hing dadurch 0,28 Studs zu hoch.

Die Tests hier pruefen, dass beides nicht mehr passieren kann:

* die Referenzdatei liefert 15 Teile mit den dokumentierten Maßen,
* Arme und Beine werden relativ zum echten Torso genau wie im Mannequin
  gesetzt (Armoberkante 21,6 %, Beinlaenge 131,7 % der Torso-Hoehe),
* die Gelenke kommen aus den ``*RigAttachment``-Daten der Teile (Schulter
  0,27 Studs ueber der OberTorso-Mitte) - identisch mit dem Mannequin,
* ein echtes Bundle mit anderen Maßen wird **nicht** ersetzt.

    python3 -m pytest tools/tests/test_standard_koerper.py -q
"""

from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

import avatar_rig  # noqa: E402
import mannequin  # noqa: E402
import roblox_api  # noqa: E402
import roblox_mesh  # noqa: E402

I3 = avatar_rig.IDENTITY_MAT3


@pytest.fixture(scope="module")
def klassisch():
    body = mannequin.cached()
    if body is None or not body.meshes:
        pytest.skip("reference/classic_body.json fehlt")
    return body


def _att(pos, rot=I3):
    return {"position": pos, "rotation": rot}


def _seg(name, mesh, size, atts=None):
    return {"label": name, "mesh": mesh, "rbxm_class": "MeshPart",
            "rbxm_size": size, "attachments": atts or {}, "transparency": 0.0}


def _arm_segmente(koerper):
    """Arm-Segmente mit den echten ``*RigAttachment``-Daten des Avatars."""
    daten = {
        "LeftUpperArm": {"LeftShoulderRigAttachment": (0.1673, 0.2961, 0.0011),
                         "LeftElbowRigAttachment": (-0.1121, -0.2031, 0.0575)},
        "LeftLowerArm": {"LeftElbowRigAttachment": (0.1312, 0.3229, 0.0617),
                         "LeftWristRigAttachment": (-0.1221, -0.3608, -0.1683)},
        "LeftHand": {"LeftWristRigAttachment": (0.076, 0.1501, 0.086)},
    }
    return [
        _seg(name, koerper.meshes[name], koerper.sizes[name],
             {a: _att(p) for a, p in atts.items()})
        for name, atts in daten.items()
    ]


def _torso_segmente(koerper):
    """Torso aus den Mannequin-Teilen (wie beim echten Avatar)."""
    teile = []
    for name in ("LowerTorso", "UpperTorso"):
        teile.append(_seg(name, koerper.meshes[name], koerper.sizes[name], {
            "RootRigAttachment": _att((0.0, -0.2, 0.0)),
            "WaistRigAttachment": _att((0.0, 0.2, 0.0)),
            "NeckRigAttachment": _att((0.0, 0.8, 0.0)),
            "LeftShoulderRigAttachment": _att((-0.43, 0.30, 0.06)),
            "RightShoulderRigAttachment": _att((0.43, 0.30, 0.06)),
            "LeftHipRigAttachment": _att((-0.24, -0.19, 0.02)),
            "RightHipRigAttachment": _att((0.24, -0.19, 0.02)),
        }))
    return teile


def test_referenz_enthaelt_die_15_standard_teile(klassisch):
    assert len(klassisch.meshes) == 15
    assert set(mannequin.PART_NAMES) <= set(klassisch.meshes)
    # Dokumentierte Maße des offiziellen Mannequins (Studs).
    assert klassisch.sizes["UpperTorso"] == pytest.approx((1.3034, 1.6987, 0.842),
                                                          abs=2e-3)
    assert klassisch.sizes["LeftUpperArm"] == pytest.approx((0.7489, 0.8883, 0.5844),
                                                            abs=2e-3)
    for teil in klassisch.meshes.values():
        assert len(teil.positions) > 100
        assert len(teil.faces) > 100
        assert len(teil.uvs) == len(teil.positions)


def test_teile_sitzen_wie_im_mannequin(klassisch):
    """Armoberkante 21,6 % und Beinlaenge 131,7 % der Torso-Hoehe."""
    lo, hi = (-0.464, 0.0, -0.358), (0.464, 1.663, 0.358)
    arme = mannequin.place_part(klassisch, "links_arm", (lo, hi))
    beine = mannequin.place_part(klassisch, "links_bein", (lo, hi))
    torso_h = hi[1] - lo[1]
    arm_lo, arm_hi = arme[0].mesh.bbox()
    arm_oben = arme[0].center[1] + arm_hi[1]
    assert arm_oben == pytest.approx(hi[1] - 0.216 * torso_h, abs=0.01 * torso_h)
    bein_lo, bein_hi = beine[0].mesh.bbox()
    oben = beine[0].center[1] + bein_hi[1]
    unten = beine[-1].center[1] + beine[-1].mesh.bbox()[0][1]
    assert (oben - unten) / torso_h == pytest.approx(1.317, abs=0.03)
    # Die Beine greifen wie im Mannequin in den Torso (kein Spalt).
    assert oben > lo[1]
    _ = (arm_lo, bein_lo)


def test_rig_setzt_die_schulter_auf_die_mannequin_hoehe(klassisch):
    """Schultergelenk 21,6 % unter der Torso-Oberkante - ohne Schulterband."""
    rig = avatar_rig.assemble_r15_rig(
        {"torso": _torso_segmente(klassisch),
         "links_arm": _arm_segmente(klassisch)}, None,
        roblox_api.DEFAULT_BODY_COLORS, dict(roblox_api.DEFAULT_SCALES),
        headless=True, classic_body=klassisch)
    assert any("ClassicMannequin" in n for n in rig.notes)
    # Kein Schulterband-Eingriff mehr (der schob den Arm auf 4 %).
    assert not any("Schulterlinie" in n for n in rig.notes)
    t_lo, t_hi = rig.body_meshes["torso"].bbox()
    torso_h = t_hi[1] - t_lo[1]
    schulter = rig.joint_positions["LeftUpperArm"]
    assert (t_hi[1] - schulter[1]) / torso_h == pytest.approx(0.216, abs=0.02)
    assert schulter[0] < 0  # linker Arm liegt links (Szene schaut nach -Z)
    # Der Arm haengt senkrecht: Ellenbogen genau unter der Schulter.
    ellbogen = rig.joint_positions["LeftLowerArm"]
    assert ellbogen[0] == pytest.approx(schulter[0], abs=0.02)
    assert ellbogen[2] == pytest.approx(schulter[2], abs=0.02)
    assert ellbogen[1] < schulter[1]
    # Und die sichtbare Armoberkante liegt tiefer als das alte Band (4 %).
    arm_lo, arm_hi = rig.body_meshes["links_arm"].bbox()
    assert 0.05 < (t_hi[1] - arm_hi[1]) / torso_h < 0.30


def test_fehlende_beine_kommen_aus_dem_mannequin(klassisch):
    """Frueher wurden fehlende Beine aus R15-Block-Meshes/Quadern gebaut."""
    rig = avatar_rig.assemble_r15_rig(
        {"torso": _torso_segmente(klassisch)}, None,
        roblox_api.DEFAULT_BODY_COLORS, dict(roblox_api.DEFAULT_SCALES),
        headless=True, classic_body=klassisch)
    bein = rig.body_meshes["links_bein"]
    # Das Mannequin-Bein hat 222 + 124 + 231 Dreiecke - kein Quader (12).
    assert len(bein.faces) > 300
    t_lo, t_hi = rig.body_meshes["torso"].bbox()
    lo, hi = bein.bbox()
    assert (hi[1] - lo[1]) / (t_hi[1] - t_lo[1]) == pytest.approx(1.317, abs=0.08)
    # UVs des Standard-Koerpers (Roblox-Kleidungsvorlage) bleiben erhalten.
    assert all(0.0 <= u <= 1.0 and 0.0 <= v <= 1.0 for u, v in bein.uvs)


def test_fremdes_bundle_wird_nicht_ersetzt(klassisch):
    """Ein ausgeruestetes Paket mit anderen Maßen bleibt unangetastet."""
    fremd = roblox_mesh.make_box(2.0, 2.0, 2.0, (0.0, 0.0, 0.0))
    segmente = [
        _seg("LeftUpperArm", fremd, (2.0, 2.0, 2.0), {
            "LeftShoulderRigAttachment": _att((0.0, 1.0, 0.0)),
            "LeftElbowRigAttachment": _att((0.0, -1.0, 0.0)),
        }),
        _seg("LeftLowerArm", fremd, (2.0, 2.0, 2.0), {
            "LeftElbowRigAttachment": _att((0.0, 1.0, 0.0)),
            "LeftWristRigAttachment": _att((0.0, -1.0, 0.0)),
        }),
        _seg("LeftHand", fremd, (2.0, 2.0, 2.0), {
            "LeftWristRigAttachment": _att((0.0, 1.0, 0.0)),
        }),
    ]
    rig = avatar_rig.assemble_r15_rig(
        {"torso": _torso_segmente(klassisch), "links_arm": segmente}, None,
        roblox_api.DEFAULT_BODY_COLORS, dict(roblox_api.DEFAULT_SCALES),
        headless=True, classic_body=klassisch)
    assert not any("links_arm" in n and "ClassicMannequin" in n for n in rig.notes)
    arm = rig.body_meshes["links_arm"]
    lo, hi = arm.bbox()
    assert hi[1] - lo[1] > 2.5  # die 2-Studs-Segmente des Pakets


def test_ohne_referenz_bleibt_der_bisherige_weg(klassisch):
    """Ohne ``classic_body`` aendert sich nichts (Rueckfall bleibt erhalten)."""
    rig = avatar_rig.assemble_r15_rig(
        {"torso": _torso_segmente(klassisch)}, None,
        roblox_api.DEFAULT_BODY_COLORS, dict(roblox_api.DEFAULT_SCALES),
        headless=True)
    assert not any("ClassicMannequin" in n for n in rig.notes)
    assert "links_arm" in rig.body_meshes
