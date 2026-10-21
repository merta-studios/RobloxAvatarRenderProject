"""Regressionstests fuer den Rig-Fix: CFrame-Rotationen + Stand-Pose-Stapel.

Bug: Die Pipeline ignorierte CFrame-Rotationen aus RBXM-Modellen und
uebernahm die Bind-Pose-Positionen der R15-Segmente (A-Pose). Beim
Einpressen in die senkrechte Rig-Box standen die Teile schief/verstellt
(falsche Position und Rotation), Accessoires (Bruststueck, Huete) ragten
seitlich raus. Groessen waren korrekt und bleiben es.

    python3 -m pytest tools/tests -q
"""

from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)
sys.path.insert(0, HERE)

import avatar_rig  # noqa: E402
import build_avatar  # noqa: E402
import rbxm  # noqa: E402
import roblox_api  # noqa: E402
import roblox_mesh  # noqa: E402
from rbxm_factory import make_rbxm  # noqa: E402


# ---------------------------------------------------------------------------
# RBXM: CFrame-Rotationen lesen (Matrix + 24er-Sondertabelle)
# ---------------------------------------------------------------------------

def _meshpart(name="Arm", mesh_id=111, size=(1.0, 2.0, 1.0), cframe=None):
    props = {"Name": name, "MeshId": f"rbxassetid://{mesh_id}",
             "size": size}
    props["CFrame"] = cframe if cframe is not None else (0.5, 3.0, -0.25)
    return {"class": "MeshPart", "props": props, "Parent": None}


def test_rbxm_explizite_rotationsmatrix_wird_gelesen():
    # ID 0x00: 9 rohe Float32 (R00..R22) vor den Positionen
    matrix = (0.0, 0.0, 1.0, 0.0, 1.0, 0.0, -1.0, 0.0, 0.0)  # 90 Grad um Y
    data = make_rbxm([_meshpart(cframe=("M", (1.0, 2.0, 3.0), matrix))])
    entry = rbxm.extract_assets(data)[0]
    assert entry["position"] == pytest.approx((1.0, 2.0, 3.0))
    assert entry["rotation"][0] == pytest.approx((0.0, 0.0, 1.0))
    assert entry["rotation"][1] == pytest.approx((0.0, 1.0, 0.0))
    assert entry["rotation"][2] == pytest.approx((-1.0, 0.0, 0.0))


@pytest.mark.parametrize("rot_id,expected_row0", [
    (0x02, (1.0, 0.0, 0.0)),    # Identitaet
    (0x03, (1.0, 0.0, 0.0)),    # 90 Grad um X
    (0x14, (-1.0, 0.0, 0.0)),   # 180 Grad um Y
    (0x20, (0.0, 0.0, 1.0)),    # 90 Grad um Y
])
def test_rbxm_sondertabelle_wird_aufgeloest(rot_id, expected_row0):
    """Die 24 Sonder-IDs sind KEINE Identitaet (nur 0x02) - vor dem Fix
    wurden sie alle als identisch behandelt."""
    data = make_rbxm([_meshpart(cframe=("R", rot_id, (4.0, 5.0, 6.0)))])
    entry = rbxm.extract_assets(data)[0]
    assert entry["position"] == pytest.approx((4.0, 5.0, 6.0))
    assert entry["rotation"] is not None
    assert entry["rotation"][0] == pytest.approx(expected_row0)


def test_rbxm_alle_24_sondertabelle_ids_sind_im_parser():
    assert len(rbxm._BASIC_ROTATIONS) == 24
    identitaet = [i for i, m in rbxm._BASIC_ROTATIONS.items()
                  if m == ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))]
    assert identitaet == [0x02]  # nur 0x02 ist leer - alle anderen drehen


def test_rbxm_unbekannte_rotations_id_gibt_keinen_absturz():
    data = make_rbxm([_meshpart(cframe=("R", 0x77, (1.0, 1.0, 1.0)))])
    entry = rbxm.extract_assets(data)[0]
    assert entry["position"] == pytest.approx((1.0, 1.0, 1.0))
    assert entry["rotation"] is None  # -> als Identitaet behandelt


def test_rbxm_rotation_wird_vom_parent_geerbt():
    # Sondermesh ohne eigenen CFrame erbt Rotation + Position des Parts
    part = {"class": "Part",
            "props": {"Name": "Handle", "size": (1.0, 1.0, 1.0),
                      "CFrame": ("R", 0x14, (7.0, 8.0, 9.0))},
            "Parent": None}
    mesh = {"class": "SpecialMesh",
            "props": {"Name": "Mesh", "MeshId": "rbxassetid://4711"},
            "Parent": 0}
    entries = rbxm.extract_assets(make_rbxm([part, mesh]))
    assert len(entries) == 1
    assert entries[0]["position"] == pytest.approx((7.0, 8.0, 9.0))
    assert entries[0]["rotation"][0] == pytest.approx((-1.0, 0.0, 0.0))


def test_rbxmx_xml_cframe_liefert_rotation():
    xml = b"""<roblox version="4">
      <Item class="MeshPart" referent="RBX0">
        <Properties>
          <string name="Name">Hut</string>
          <Content name="MeshId"><url>rbxassetid://555</url></Content>
          <Vector3 name="size"><X>1</X><Y>1</Y><Z>1</Z></Vector3>
          <CoordinateFrame name="CFrame">
            <X>1</X><Y>2</Y><Z>3</Z>
            <R00>0</R00><R01>0</R01><R02>1</R02>
            <R10>0</R10><R11>1</R11><R12>0</R12>
            <R20>-1</R20><R21>0</R21><R22>0</R22>
          </CoordinateFrame>
        </Properties>
      </Item>
    </roblox>"""
    entry = rbxm.extract_assets(xml)[0]
    assert entry["position"] == pytest.approx((1.0, 2.0, 3.0))
    assert entry["rotation"][0] == pytest.approx((0.0, 0.0, 1.0))
    assert entry["rotation"][2] == pytest.approx((-1.0, 0.0, 0.0))


# ---------------------------------------------------------------------------
# roblox_mesh: rotate() + stack_segments()
# ---------------------------------------------------------------------------

def test_mesh_rotate_90_grad_um_y():
    mesh = roblox_mesh.make_box(2.0, 1.0, 1.0)  # lang auf X
    rot = ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0))
    turned = mesh.rotate(rot)
    size = turned.size()
    # X- und Z-Achse tauschen sich
    assert size[0] == pytest.approx(1.0, abs=1e-6)
    assert size[2] == pytest.approx(2.0, abs=1e-6)
    assert size[1] == pytest.approx(1.0, abs=1e-6)


def test_mesh_rotate_none_ist_identitaet():
    mesh = roblox_mesh.make_box(1.0, 2.0, 3.0)
    assert mesh.rotate(None).positions == mesh.positions


def test_stack_segments_stapelt_senkrecht():
    """Bind-Pose-Diagonale -> gerader Stapel: Obersegment oben, Groesse
    bleibt die RBXM-Groesse, Luecken verhindern exakt gleiche Flaechen."""
    oben = roblox_mesh.make_box(1.0, 1.0, 1.0)
    unten = roblox_mesh.make_box(0.8, 0.6, 0.8)
    stacked = roblox_mesh.stack_segments(
        [(oben, (1.0, 1.0, 1.0), (0.0, 0.0, 0.0)),
         (unten, (0.8, 0.6, 0.8), (5.0, -9.0, 2.0))],  # Bind-Pose-Muell
        gap=0.02)
    lo, hi = stacked.bbox()
    # Gesamtgroesse = 1 + Luecke + 0.6, Oberkante bei 0
    assert hi[1] == pytest.approx(0.0, abs=1e-6)
    assert lo[1] == pytest.approx(-1.62, abs=1e-6)
    # Beide Segmente mittig auf x=z=0 (kein Diagonalversatz mehr)
    size = stacked.size()
    assert size[0] == pytest.approx(1.0, abs=1e-6)
    assert size[2] == pytest.approx(1.0, abs=1e-6)
    # Reihenfolge: das breite Segment (oben) liegt wirklich oben:
    # in der oberen Haelfte ist die Ausdehnung 1.0 breit, unten 0.8
    top_band = [p[0] for p in stacked.positions if p[1] > -0.5]
    assert max(top_band) - min(top_band) == pytest.approx(1.0, abs=1e-6)
    lower_band = [p[0] for p in stacked.positions if p[1] < -1.0]
    assert max(lower_band) - min(lower_band) == pytest.approx(0.8, abs=1e-6)


def test_stack_segments_faengt_leere_geometrie_ab():
    stacked = roblox_mesh.stack_segments([])
    assert stacked.is_empty


# ---------------------------------------------------------------------------
# place_accessory: CFrame-Rotation anwenden
# ---------------------------------------------------------------------------

def test_accessoire_rotation_wird_angewendet():
    """Vor dem Fix stand ein um 90 Grad gedrehtes Bruststueck seitlich
    statt in Blickrichtung (-Z)."""
    _, metrics = avatar_rig.build_body(roblox_api.DEFAULT_BODY_COLORS,
                                       roblox_api.DEFAULT_SCALES)
    stueck = roblox_mesh.make_box(1.7, 1.4, 0.3)  # flach in X, schmal in Z
    rot90y = ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0))
    placed, note = avatar_rig.place_accessory(
        stueck, 45, metrics, size_hint=(1.0, 1.0, 1.0), rotation=rot90y)
    size = placed.size()
    # Nach 90-Grad-Drehung ragt das Stueck in Z (vorne), nicht mehr in X
    assert size[2] > size[0]
    assert "CFrame-Rotation" in note
    # Anker chest_front: vorne am Brustkorb (Z negativ = vorne)
    lo, hi = placed.bbox()
    assert hi[2] < 0.6


def test_accessoire_identitaetsrotation_eraehlt_notiz():
    _, metrics = avatar_rig.build_body(roblox_api.DEFAULT_BODY_COLORS,
                                       roblox_api.DEFAULT_SCALES)
    identitaet = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    _, note = avatar_rig.place_accessory(
        roblox_mesh.make_box(1.0, 1.0, 1.0), 8, metrics,
        rotation=identitaet)
    assert "CFrame-Rotation" not in note  # keine Wiederholung bei Identitaet


# ---------------------------------------------------------------------------
# build_avatar: Körperteile werden gestapelt statt nach Bind-Pose gemischt
# ---------------------------------------------------------------------------

def _box_mesh_bytes(w, h, d):
    from test_build_integration import encode_v2
    return encode_v2(roblox_mesh.make_box(w, h, d))


def _obj_points(path):
    points = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("v "):
                points.append(tuple(float(v) for v in line.split()[1:4]))
    return points


def test_koerpersegmente_werden_gestapelt(monkeypatch, tmp_path):
    """Zwei Segmente mit Bind-Pose-Positionen (diagonal) muessen am Ende
    als gerader, unverzerrter Stapel an der Schulter landen - nicht als
    Diagonalstufen und nicht in eine Schema-F-Box gestreckt."""
    avatar = {
        "playerAvatarType": "R15",
        "scales": dict(roblox_api.DEFAULT_SCALES),
        "bodyColors": dict(roblox_api.DEFAULT_BODY_COLORS),
        "assets": [{"id": 500, "name": "RightArm",
                    "assetType": {"id": 28, "name": "RightArm"}}],
    }
    # Bind-Pose wie in echten Bundles: diagonal nach aussen/unten
    bundle = make_rbxm([
        {"class": "MeshPart",
         "props": {"Name": "RightUpperArm", "MeshId": "rbxassetid://911",
                   "size": (1.0, 1.0, 1.0), "CFrame": (1.4, 0.5, 0.0)},
         "Parent": None},
        {"class": "MeshPart",
         "props": {"Name": "RightHand", "MeshId": "rbxassetid://912",
                   "size": (0.8, 0.6, 0.8), "CFrame": (2.0, -0.7, -0.3)},
         "Parent": None},
    ])
    downloads = {500: bundle, 911: _box_mesh_bytes(1.0, 1.0, 1.0),
                 912: _box_mesh_bytes(0.8, 0.6, 0.8)}

    import roblox_api as api
    monkeypatch.setattr(api, "resolve_user_id", lambda c, u: (1, "Test"))
    monkeypatch.setattr(api, "fetch_avatar", lambda c, u: dict(avatar))
    monkeypatch.setattr(api, "fetch_asset_details",
                        lambda c, a: {"assetTypeId": 28, "name": "RightArm"})
    monkeypatch.setattr(
        api, "download_asset",
        lambda c, a: (api.DownloadedAsset(a, downloads[a], "", "", "")
                      if a in downloads else
                      api.DownloadedAsset(a, None, "", "", "fehlt")))

    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    arm = next(p for p in manifest["parts"] if p["name"] == "rechts_arm")
    assert "2 R15-Segmente" in arm["note"]
    assert "gestapelt" in arm["note"]

    # Arm-Box bei Standard-Skala: Torso 2 breit -> Arm x 1.5..2.5,
    # y 2.0..4.0 (Beine 2 hoch, Torso 2 hoch), z -0.5..0.5
    lo = [1e9] * 3
    hi = [-1e9] * 3
    for p in _obj_points(tmp_path / arm["obj"]):
        for i in range(3):
            lo[i] = min(lo[i], p[i])
            hi[i] = max(hi[i], p[i])
    # Schulter = Torso-Oberkante: Beine 2 hoch + gap (0.012) + Torso 2
    # -> 4.012. Die Arm-Oberkante liegt jetzt exakt dort (vorher wurde der
    # Arm in die 2 Studs hohe Box gestreckt).
    assert hi[1] == pytest.approx(4.012, abs=0.01)   # Oberkante = Schulter
    # Kein Strecken: die Hoehe ist die Summe der Mesh-Hoehen (1.0 + gap +
    # 0.6 = 1.612) und nicht die 2 Studs der Standard-Box.
    assert hi[1] - lo[1] == pytest.approx(1.612, abs=0.02)
    # Kein Diagonalversatz: obere und untere Haelfte sind beide mittig
    # (Zentrum x = Torso 1 + Armhälfte 0.5 + gap = 1.512). Vorher driftete
    # die Hand nach innen (Diagonale der Bind-Pose).
    upper = [p for p in _obj_points(tmp_path / arm["obj"]) if p[1] > 3.0]
    lower = [p for p in _obj_points(tmp_path / arm["obj"]) if p[1] < 3.0]
    for band in (upper, lower):
        xs = [p[0] for p in band]
        assert (min(xs) + max(xs)) / 2 == pytest.approx(1.512, abs=0.05)


def test_r15_segment_reihenfolge_obersegment_zuerst():
    """_pick_r15_segments liefert oben->unten (Upper vor Lower vor Hand)."""
    group = [
        {"label": "RightHand"},
        {"label": "RightLowerArm"},
        {"label": "RightUpperArm"},
    ]
    picked = build_avatar._pick_r15_segments(
        group, build_avatar.R15_SEGMENT_NAMES["rechts_arm"])
    assert [p["label"] for p in picked] == ["RightUpperArm", "RightLowerArm",
                                            "RightHand"]


def test_sort_segments_top_down_dedupliziert_varianten():
    """Bundles enthalten pro Segment oft zwei Varianten - der Fallback
    darf sie nicht mischen."""
    def mp(y, mesh_id):
        return {"rbxm_position": (0.0, y, 0.0), "mesh_asset_id": mesh_id}
    group = [mp(0.0, 1), mp(-1.0, 2), mp(-2.0, 3),   # Variante A
             mp(0.5, 1), mp(-0.5, 2), mp(-1.5, 3)]   # Variante B (Doppel)
    out = build_avatar._sort_segments_top_down(group)
    assert len(out) == 3
    ys = [m["rbxm_position"][1] for m in out]
    assert ys == sorted(ys, reverse=True)
    assert len({m["mesh_asset_id"] for m in out}) == 3


# ---------------------------------------------------------------------------
# Kein Strecken: Rig-Boxen kommen aus den Mesh-Groessen
# ---------------------------------------------------------------------------

def test_relayout_body_uebernimmt_mesh_proportionen():
    """Ein schlanker Arm (1,0 x 1,6 x 0,7) darf nicht auf die 1x2x1-Box
    gestreckt werden - Groesse und Anschluss kommen aus dem Mesh."""
    parts, metrics = avatar_rig.build_body(roblox_api.DEFAULT_BODY_COLORS,
                                           dict(roblox_api.DEFAULT_SCALES))
    neu_parts, neu_metrics = avatar_rig.relayout_body(
        parts, metrics, {"rechts_arm": (1.0, 1.6, 0.7)},
        dict(roblox_api.DEFAULT_SCALES))
    arm = next(p for p in neu_parts if p.name == "rechts_arm")
    assert arm.mesh.size() == pytest.approx((1.0, 1.6, 0.7), abs=1e-6)
    # Oberkante des Arms sitzt an der Schulter (Torso-Oberkante)
    assert arm.mesh.center()[1] + 0.8 == pytest.approx(neu_metrics.shoulder_y, abs=1e-6)


def test_relayout_body_begrenzt_ausreisser_groessen():
    """Mesh-Einheiten sind nicht immer Studs: ein 100-Studs-Arm darf die
    Szene nicht sprengen (Grenze: 2x Standard-Box)."""
    parts, metrics = avatar_rig.build_body(roblox_api.DEFAULT_BODY_COLORS,
                                           dict(roblox_api.DEFAULT_SCALES))
    neu_parts, _ = avatar_rig.relayout_body(
        parts, metrics, {"links_arm": (10.0, 60.0, 10.0)},
        dict(roblox_api.DEFAULT_SCALES))
    arm = next(p for p in neu_parts if p.name == "links_arm")
    assert max(arm.mesh.size()) <= 4.0  # 2x Standard-Box (1x2x1)


def test_relayout_body_kopf_sitzt_auf_dem_hals():
    parts, metrics = avatar_rig.build_body(roblox_api.DEFAULT_BODY_COLORS,
                                           dict(roblox_api.DEFAULT_SCALES))
    neu_parts, neu_metrics = avatar_rig.relayout_body(
        parts, metrics, {"torso": (2.0, 2.2, 1.0)}, dict(roblox_api.DEFAULT_SCALES),
        head_size=(1.2, 1.2, 1.2))
    kopf = next(p for p in neu_parts if p.name == "kopf")
    hals = next(p for p in neu_parts if p.name == "hals")
    torso = next(p for p in neu_parts if p.name == "torso")
    lo, hi = torso.mesh.bbox()
    hlo, hhi = hals.mesh.bbox()
    klo, khi = kopf.mesh.bbox()
    assert hlo[1] <= hi[1] + 1e-6        # Hals beginnt im Torso
    assert hhi[1] >= klo[1] - 1e-6       # Hals reicht in den Kopf
    assert khi[1] == pytest.approx(neu_metrics.height, abs=1e-6)


def test_headless_kein_kopf_und_huete_tief():
    """Headless-Avatar: kein Kopf, kurzer Kragen und Hutfreigabe knapp ueber
    dem Torso (vorher schwebten Huete weit ueber dem Koerper)."""
    parts, metrics = avatar_rig.build_body(roblox_api.DEFAULT_BODY_COLORS,
                                           dict(roblox_api.DEFAULT_SCALES))
    neu_parts, neu_metrics = avatar_rig.relayout_body(
        parts, metrics, {"torso": (2.0, 2.0, 1.0)}, dict(roblox_api.DEFAULT_SCALES),
        headless=True)
    assert "kopf" not in [p.name for p in neu_parts]
    assert neu_metrics.hat_base_y is not None
    hals = next(p for p in neu_parts if p.name == "hals")
    hlo, hhi = hals.mesh.bbox()
    assert hhi[1] - hlo[1] <= 0.5          # kurzer Kragen statt langem Hals
    # Hut-Unterkante liegt nur wenig ueber dem Torso
    assert neu_metrics.hat_base_y - neu_metrics.shoulder_y < 0.3
    hut = roblox_mesh.make_box(1.5, 0.6, 1.5)
    placed, _ = avatar_rig.place_accessory(hut, 8, neu_metrics)
    lo, hi = placed.bbox()
    assert lo[1] - neu_metrics.shoulder_y < 0.5
