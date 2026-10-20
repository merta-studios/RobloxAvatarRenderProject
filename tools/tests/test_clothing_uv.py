"""Tests fuer die Kleidungs-UV-Karte (tools/clothing_uv.py).

Hintergrund: Vorher lag das komplette 585x559-Shirt-Template auf jeder
Flaeche jedes Koerperteils. Ergebnis im Render: falsche Muster (Aermel
trugen das Brustmotiv), Hosen-Textur auf Fuessen und ein dunkelgrauer
Farbbrei, weil transparente Template-Bereiche als Schwarz gelesen wurden.

Hier wird geprueft, dass pro Flaeche genau das passende Feld benutzt wird
und dass die R15-Segmente (Ober-/Unterarm, Hand, Ober-/Unterschenkel,
Fuss) auf ihren festen Anteil im Feld fallen.
"""

from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

import clothing_uv  # noqa: E402
import roblox_mesh  # noqa: E402


def _face_uvs(mesh, normal):
    """Alle UVs der Vertices einer Flaeche (ueber die Normale ausgewaehlt).

    `make_box` erzeugt pro Flaeche eigene Vertices - dieselbe Position
    kommt also mehrfach mit verschiedenen UVs vor.
    """
    out = []
    for p, n, uv in zip(mesh.positions, mesh.normals, mesh.uvs):
        if all(abs(n[i] - normal[i]) < 1e-6 for i in range(3)):
            out.append((p, uv))
    assert out, f"keine Flaeche mit Normale {normal}"
    return out


def _corner(mesh, normal, corner):
    """UV der Ecke `corner` (+1/-1 je Achse) auf der Flaeche `normal`."""
    for p, uv in _face_uvs(mesh, normal):
        if all(abs(p[i] - corner[i]) < 1e-6 for i in range(3)):
            return uv
    raise AssertionError(f"keine Ecke {corner} auf Flaeche {normal}")


def _norm(u, v):
    return (u / clothing_uv.TEMPLATE_W, v / clothing_uv.TEMPLATE_H)


def _expected_uv(rect, u_norm, v_norm):
    """Erwartete UV-Koordinate fuer einen Punkt im Template-Feld.

    Roblox-Koordinaten: u_norm=0 ist der linke Rand, v_norm=0 der untere
    (Roblox-Y nach oben), Bild-Y laeuft nach unten -> v wird gespiegelt.
    """
    x, y, w, h = rect
    u = (x + u_norm * w) / clothing_uv.TEMPLATE_W
    v = 1.0 - (y + (1.0 - v_norm) * h) / clothing_uv.TEMPLATE_H
    return (round(u, 6), round(v, 6))


def test_jede_flaeche_bekommt_ihr_eigenes_feld():
    box = roblox_mesh.make_box(2.0, 2.0, 1.0)
    mesh = clothing_uv.assign_template_uvs(box, clothing_uv.STYLE_SHIRT, limb="torso")

    # (Normale, Ecke u=0/v=0, Ecke u=1/v=1)
    # Roblox: "vorne" ist -Z, die rechte Koerperseite ist +X.
    faces = [
        ((0.0, 0.0, -1.0), (1.0, -1.0, -0.5), (-1.0, 1.0, -0.5)),   # front
        ((0.0, 0.0, 1.0), (-1.0, -1.0, 0.5), (1.0, 1.0, 0.5)),      # back
        ((1.0, 0.0, 0.0), (1.0, -1.0, 0.5), (1.0, 1.0, -0.5)),      # right side
        ((-1.0, 0.0, 0.0), (-1.0, -1.0, -0.5), (-1.0, 1.0, 0.5)),   # left side
        ((0.0, 1.0, 0.0), (1.0, 1.0, -0.5), (-1.0, 1.0, 0.5)),      # top
        ((0.0, -1.0, 0.0), (1.0, -1.0, 0.5), (-1.0, -1.0, -0.5)),   # bottom
    ]
    keys = ["front", "back", "right", "left", "top", "bottom"]
    used = set()
    for (normal, corner_lo, corner_hi), key in zip(faces, keys):
        rect = clothing_uv.TORSO_PANELS[key]
        lo = _corner(mesh, normal, corner_lo)
        hi = _corner(mesh, normal, corner_hi)
        assert lo == pytest.approx(_expected_uv(rect, 0.0, 0.0), abs=1e-6)
        assert hi == pytest.approx(_expected_uv(rect, 1.0, 1.0), abs=1e-6)
        used.add(rect)
    # Jede Flaeche hat ein eigenes Feld (vorher lag ueberall das ganze Bild)
    assert len(used) == 6


def test_uvs_bleiben_im_feld():
    box = roblox_mesh.make_box(2.0, 2.0, 1.0)
    mesh = clothing_uv.assign_template_uvs(box, clothing_uv.STYLE_SHIRT, limb="torso")
    front = clothing_uv.TORSO_PANELS["front"]
    u_lo, u_hi = _norm(front[0], 0)[0], _norm(front[0] + front[2], 0)[0]
    v_lo = 1.0 - _norm(0, front[1] + front[3])[1]
    v_hi = 1.0 - _norm(0, front[1])[1]
    for _, uv in _face_uvs(mesh, (0.0, 0.0, -1.0)):
        assert u_lo - 1e-6 <= uv[0] <= u_hi + 1e-6
        assert v_lo - 1e-6 <= uv[1] <= v_hi + 1e-6


@pytest.mark.parametrize("segment,share", [
    ("LeftUpperArm", 0.5),
    ("LeftLowerArm", 0.375),
    ("LeftHand", 0.125),
    ("LeftUpperLeg", 0.5),
    ("LeftLowerLeg", 0.375),
    ("LeftFoot", 0.125),
])
def test_r15_segmente_teilen_das_feld(segment, share):
    """Roblox: Oberarm 64 px, Unterarm 48 px, Hand 16 px (von 128)."""
    assert clothing_uv.segment_slice(segment) == pytest.approx(
        _expected(segment), abs=1e-9)
    box = roblox_mesh.make_box(1.0, 1.0, 1.0)
    mesh = clothing_uv.assign_template_uvs(
        box, clothing_uv.STYLE_SHIRT, limb="left", slice_name=segment)
    start, end = _expected(segment)
    assert start < end
    assert end - start == pytest.approx(share, abs=1e-9)
    # Oberkante des Segments = (0.5 * h) oberhalb der Feldmitte
    field = clothing_uv.LIMB_PANELS_LEFT["front"]
    oben = _corner(mesh, (0.0, 0.0, -1.0), (-0.5, 0.5, -0.5))
    unten = _corner(mesh, (0.0, 0.0, -1.0), (-0.5, -0.5, -0.5))
    erwartete_hoehe = (end - start) * field[3] / clothing_uv.TEMPLATE_H
    assert abs(oben[1] - unten[1]) == pytest.approx(erwartete_hoehe, abs=1e-6)
    # ... und sie liegt im richtigen Anteil des Feldes
    assert oben[1] == pytest.approx(
        1.0 - (field[1] + start * field[3]) / clothing_uv.TEMPLATE_H, abs=1e-6)


def _expected(segment):
    return clothing_uv.SEGMENT_SLICES[segment.replace("Left", "")
                                      .replace("Right", "")]


def test_oberhalb_und_unterhalb_des_segments_unterscheiden_sich():
    box = roblox_mesh.make_box(1.0, 1.0, 1.0)
    oben = clothing_uv.assign_template_uvs(
        box, clothing_uv.STYLE_PANTS, limb="left", slice_name="LeftUpperLeg")
    unten = clothing_uv.assign_template_uvs(
        box, clothing_uv.STYLE_PANTS, limb="left", slice_name="LeftFoot")
    assert (_corner(oben, (0.0, 0.0, -1.0), (-0.5, 0.5, -0.5))
            != _corner(unten, (0.0, 0.0, -1.0), (-0.5, 0.5, -0.5)))


def test_hose_nutzt_nur_den_unteren_torso_streifen():
    """R15: LowerTorso = untere 32 von 128 Pixeln des Torso-Blocks."""
    box = roblox_mesh.make_box(2.0, 2.0, 1.0)
    mesh = clothing_uv.assign_template_uvs(box, clothing_uv.STYLE_PANTS, limb="torso")
    rect = clothing_uv._panel_rects(clothing_uv.STYLE_PANTS, "torso")["front"]
    assert rect[3] == pytest.approx(clothing_uv.PANTS_TORSO_HEIGHT)
    top_uv = _corner(mesh, (0.0, 0.0, -1.0), (-1.0, 1.0, -0.5))
    # Oberkante des Hosenstreifens (nicht des ganzen Torso-Feldes)
    assert top_uv[1] == pytest.approx(1.0 - (rect[1] / clothing_uv.TEMPLATE_H), abs=1e-6)


def test_links_rechts_im_template_ist_vertauscht():
    """Man sieht den Avatar von vorne: das linke Feld zeigt die rechte Seite."""
    rechts = clothing_uv.LIMB_PANELS_RIGHT["front"]
    links = clothing_uv.LIMB_PANELS_LEFT["front"]
    assert rechts[0] < links[0]  # rechtes Koerperteil liegt links im Bild


def test_front_only_laesst_andere_flaechen_leer():
    """T-Shirt (Brust-Logo): nur die Frontflaeche bekommt ein UV."""
    box = roblox_mesh.make_box(1.0, 1.0, 1.0)
    mesh = clothing_uv.assign_template_uvs(
        box, clothing_uv.STYLE_SHIRT, limb="torso", front_only=True)
    hinten = _corner(mesh, (0.0, 0.0, 1.0), (-0.5, -0.5, 0.5))
    vorne = _corner(mesh, (0.0, 0.0, -1.0), (-0.5, -0.5, -0.5))
    assert hinten == (0.0, 0.0)
    assert vorne != (0.0, 0.0)


def test_stil_je_asset_typ():
    assert clothing_uv.part_style(11) == clothing_uv.STYLE_SHIRT     # Shirt
    assert clothing_uv.part_style(12) == clothing_uv.STYLE_PANTS     # Pants
    assert clothing_uv.part_style(65) == clothing_uv.STYLE_SHIRT     # ShirtAccessory
    assert clothing_uv.part_style(70) == clothing_uv.STYLE_PANTS     # LeftShoe
    assert clothing_uv.part_style(8) is None                          # Hut


def test_mesh_ohne_uvs_bekommt_trotzdem_welche():
    mesh = roblox_mesh.MeshData(
        positions=[(0, 0, 0), (1, 0, 0), (1, 1, 0)],
        normals=[(0, 0, -1)] * 3,
        uvs=[],
        faces=[(0, 1, 2)],
    )
    out = clothing_uv.assign_template_uvs(mesh, clothing_uv.STYLE_SHIRT)
    assert len(out.uvs) == len(out.positions)
