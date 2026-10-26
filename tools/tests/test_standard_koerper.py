"""Der R15-Standard-Avatar (Blockkoerper) ersetzt fehlende Koerperteile.

Bug-Report (Runde 8): "Der Standard Avatar ist nicht der richtige blocky
R15 standard", "Arme komplett kaputt, ueberall Ober- Unterarm und Hand
verstreut", "Beine falsch rum, Fuesse schauen nach hinten".

Ursachen:

* Als Standard wurde der **klassische** Koerper (``ClassicMannequin.fbx``)
  eingesetzt - die R6-Optik mit schmalen Gliedern und Fuessen mit Zehen.
  Roblox ersetzt fehlende Teile bei einem R15-Avatar aber durch den
  **R15-Blockkoerper** (``BlockyCharacter.fbx``: quadratische 1x1-Blockteile).
* Die *ausgeruesteten* Arme wurden dabei durch umgehaengte Classic-Teile
  ersetzt. Deren Drehpunkte stimmten nicht mehr mit den ``*RigAttachment``-
  Daten ueberein, die die Pose benutzt - im Emote lagen Oberarm, Unterarm
  und Hand verstreut. Zusaetzlich schaute der Classic-Fuss mit den Zehen
  nach hinten (+Z statt -Z).

Festgelegt wird hier:

* ``reference/standard_r15.json`` (aus ``BlockyCharacter.fbx``) liefert die
  15 Blockteile mit den dokumentierten Massen.
* Fehlende Extremitaeten werden daraus gebaut: drei Segmente mit
  quadratischem Querschnitt (halbe Torso-Breite, wie R15-Block 1x1 Studs).
* Ausgeruestete Teile bleiben **unangetastet**: Montage exakt ueber ihre
  RigAttachments (``Kind = Eltern * C0 * C1^-1``), keine Schulterlinien-
  Konstante, keine Laengen-Normierung, keine Ersetzung. Die klassische
  Referenz (``classic_body.json``) bleibt nur als Mass-Abgleich erhalten.

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
import standard_r15  # noqa: E402

I3 = avatar_rig.IDENTITY_MAT3


@pytest.fixture(scope="module")
def klassisch():
    body = mannequin.cached()
    if body is None or not body.meshes:
        pytest.skip("reference/classic_body.json fehlt")
    return body


@pytest.fixture(scope="module")
def standard():
    body = mannequin.cached(mannequin.STANDARD_PATH)
    if body is None or not body.meshes:
        pytest.skip("reference/standard_r15.json fehlt")
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
    """Torso-Segmente mit RigAttachments (wie beim echten Avatar)."""
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


def _rig(segs, **kw):
    return avatar_rig.assemble_r15_rig(
        segs, None, roblox_api.DEFAULT_BODY_COLORS,
        dict(roblox_api.DEFAULT_SCALES), headless=True, **kw)


def test_standard_referenz_enthaelt_die_15_blockteile(standard):
    """R15-Blockkoerper aus BlockyCharacter.fbx: 15 Teile, exakte Masse."""
    assert len(standard.meshes) == 15
    assert set(mannequin.PART_NAMES) <= set(standard.meshes)
    # Dokumentierte Masse des offiziellen R15-Blockkoerpers (Studs).
    assert standard.sizes["UpperTorso"] == pytest.approx((2.0, 1.6, 1.0), abs=2e-3)
    assert standard.sizes["LowerTorso"] == pytest.approx((2.0, 0.4, 1.0), abs=2e-3)
    assert standard.sizes["LeftUpperArm"] == pytest.approx((1.0, 1.1687, 1.0), abs=2e-3)
    assert standard.sizes["LeftLowerLeg"] == pytest.approx((1.0, 1.1931, 1.0), abs=2e-3)
    # Der Block-Fuss ist ein flacher Quader (1 x 0,3 x 1) - keine Zehen, die
    # nach hinten zeigen koennten.
    assert standard.sizes["LeftFoot"] == pytest.approx((1.0, 0.3, 1.0), abs=2e-3)
    for teil in standard.meshes.values():
        assert len(teil.positions) > 50
        assert len(teil.faces) > 50


def test_klassische_referenz_bleibt_zum_massabgleich(klassisch):
    """Der klassische Koerper bleibt als Referenzdatei erhalten."""
    assert len(klassisch.meshes) == 15
    assert klassisch.sizes["UpperTorso"] == pytest.approx(
        (1.3034, 1.6987, 0.842), abs=2e-3)
    assert klassisch.metrics["arm_top_below_torso"] == pytest.approx(0.216, abs=0.01)


def test_ausgeruesteter_arm_bleibt_unangetastet(klassisch):
    """Die Arme des Avatars werden nicht ersetzt und nicht umgehaengt.

    Frueher ersetzte der Classic-Zweig sie durch senkrecht gedrehte
    Mannequin-Teile - danach zeigten die Pose-Drehpunkte ins Leere und die
    Segmente lagen im Emote verstreut. Jetzt gilt: Schulter-, Ellbogen- und
    Handgelenk liegen exakt auf den RigAttachments, die Geometrie ist die
    der ausgeruesteten Teile, nichts wird verschoben oder normiert.
    """
    segmente = _arm_segmente(klassisch)
    rig = _rig({"torso": _torso_segmente(klassisch), "links_arm": segmente})
    # Weder ersetzt noch per Schulterband verschoben noch laengennormiert.
    assert not any("Standard-Avatar" in n and "links_arm" in n for n in rig.notes)
    assert not any("Schulterlinie" in n for n in rig.notes)
    assert not any("gebracht (Faktor" in n for n in rig.notes)
    # Schulter exakt auf dem Torso-Attachment (kein Band, kein Offset).
    schulter = rig.joint_positions["LeftUpperArm"]
    att_pos = rig.world_attachments["LeftShoulderRigAttachment"][0]
    assert schulter == pytest.approx(att_pos, abs=1e-6)
    # Die Kette haengt an den Attachments nach unten: Ellbogen unter der
    # Schulter, Handgelenk unter dem Ellbogen.
    ellbogen = rig.joint_positions["LeftLowerArm"]
    hand = rig.joint_positions["LeftHand"]
    assert ellbogen[1] < schulter[1]
    assert hand[1] < ellbogen[1]
    assert ellbogen == pytest.approx(
        rig.world_attachments["LeftElbowRigAttachment"][0], abs=1e-6)
    # Geometrie der ausgeruesteten Teile (nicht ersetzt): gleiche Flaechenzahl.
    erwartet = sum(len(s["mesh"].faces) for s in segmente)
    assert len(rig.body_meshes["links_arm"].faces) == erwartet


def test_fehlende_beine_kommen_aus_dem_r15_standard(klassisch):
    """Nicht getragene Beine = exakter R15-Standard (BlockyCharacter.fbx).

    Montiert ueber die echten RigAttachments (Motor6D-Semantik) - die
    Gelenke liegen damit exakt auf den Roblox-Pivots, nichts wird auf
    geschaetzte Fraktionen gestreckt.
    """
    rig = _rig({"torso": _torso_segmente(klassisch)},
               standard_segments=standard_r15.standard_segments())
    assert any("Standard-Avatar" in n and "LeftUpperLeg" in n for n in rig.notes)
    spans = rig.segment_spans["links_bein"]
    assert [s for s, _a, _b in spans] == ["LeftUpperLeg", "LeftLowerLeg", "LeftFoot"]
    bein = rig.body_meshes["links_bein"]
    # Block-Meshes des Standards (788 Flaechen), kein Quader (36).
    assert len(bein.faces) > 300
    lo, hi = bein.bbox()
    # Fuss auf dem Boden (das Gesamt-Rig wird auf y = 0 gestellt).
    assert lo[1] == pytest.approx(0.0, abs=1e-6)
    # EXAKTE Gelenke: Knie und Fussgelenk sitzen auf den RigAttachments des
    # Standard-Rigs (Pivots wie in Roblox), nicht in Segmentmitten.
    assert rig.joint_positions["LeftUpperLeg"] == pytest.approx(
        rig.world_attachments["LeftHipRigAttachment"][0], abs=1e-6)
    assert rig.joint_positions["LeftLowerLeg"] == pytest.approx(
        rig.world_attachments["LeftKneeRigAttachment"][0], abs=1e-6)
    assert rig.joint_positions["LeftFoot"] == pytest.approx(
        rig.world_attachments["LeftAnkleRigAttachment"][0], abs=1e-6)
    # Natuerliche Masse des Standard-Blocks: 1,0 x 1,0 Studs Querschnitt,
    # Huefte bis Sohle = exakte Standard-Laenge (2,038 Studs). Das
    # Oberbein-Mesh ueberragt das Hueftgelenk wie im Original leicht.
    assert (hi[0] - lo[0]) == pytest.approx(1.0, abs=0.03)
    assert (hi[2] - lo[2]) == pytest.approx(1.0, abs=0.03)
    hip_y = rig.joint_positions["LeftUpperLeg"][1]
    assert (hip_y - lo[1]) == pytest.approx(2.038, rel=0.03)
    assert 0.0 < (hi[1] - hip_y) < 0.2
    # Kleidungs-UVs der Roblox-Vorlage (Hose) auf allen Segmenten.
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
    rig = _rig({"torso": _torso_segmente(klassisch), "links_arm": segmente})
    assert not any("Standard-Avatar" in n and "links_arm" in n for n in rig.notes)
    arm = rig.body_meshes["links_arm"]
    lo, hi = arm.bbox()
    assert hi[1] - lo[1] > 2.5  # die 2-Studs-Segmente des Pakets


def test_ohne_standard_meshes_bleibt_der_r15_rueckfall(klassisch):
    """Fehlt die Referenzdatei, entstehen R15-Quader gleicher Masse."""
    rig = _rig({"torso": _torso_segmente(klassisch)})
    spans = rig.segment_spans["links_bein"]
    assert [s for s, _a, _b in spans] == ["LeftUpperLeg", "LeftLowerLeg", "LeftFoot"]
    lo, hi = rig.body_meshes["links_bein"].bbox()
    assert lo[1] == pytest.approx(0.0, abs=1e-6)
