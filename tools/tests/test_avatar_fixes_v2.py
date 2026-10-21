"""Umfassende Tests fuer exaktes R15-Rigging, Skinning, UV-Kompositor & Front-Render."""

from __future__ import annotations

import io
import math
import os
import struct
import sys

import pytest

np = pytest.importorskip("numpy")
PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)
sys.path.insert(0, HERE)

import avatar_rig  # noqa: E402
import avatar_uv  # noqa: E402
import build_avatar  # noqa: E402
import rbxm  # noqa: E402
import roblox_api  # noqa: E402
import roblox_mesh  # noqa: E402
from rbxm_factory import make_rbxm  # noqa: E402
from test_render_material import _load_module as load_render_module  # noqa: E402


# ---------------------------------------------------------------------------
# 1. RBXM: Ordner, Attachments, ShirtTemplate-Kleinschreibung, XML-Floats/Bools
# ---------------------------------------------------------------------------


def test_rbxm_shirt_template_wird_strukturiert_erkannt():
    """Regression: 'shirtTemplate' hatte grosses T in _TEXTURE_PROPS."""
    shirt = {
        "class": "Shirt",
        "props": {"Name": "Shirt", "ShirtTemplate": "http://www.roblox.com/asset/?id=9580459017"},
        "Parent": None,
    }
    entries = rbxm.extract_assets(make_rbxm([shirt]))
    assert len(entries) == 1
    assert entries[0]["class"] == "Shirt"
    assert entries[0]["texture_id"] == 9580459017


def test_rbxmx_ordner_attachments_transparency_und_originalsize():
    xml = b"""<roblox version="4">
      <Item class="Folder" referent="F1">
        <Properties><string name="Name">R15ArtistIntent</string></Properties>
        <Item class="MeshPart" referent="MP1">
          <Properties>
            <string name="Name">UpperTorso</string>
            <Content name="MeshId"><url>rbxassetid://8997174058</url></Content>
            <Vector3 name="size"><X>1.3</X><Y>1.7</Y><Z>0.84</Z></Vector3>
            <float name="Transparency">0.0</float>
            <bool name="Archivable">true</bool>
          </Properties>
          <Item class="Vector3Value" referent="OS1">
            <Properties>
              <string name="Name">OriginalSize</string>
              <Vector3 name="Value"><X>1.3</X><Y>1.7</Y><Z>0.84</Z></Vector3>
            </Properties>
          </Item>
          <Item class="StringValue" referent="ST1">
            <Properties>
              <string name="Name">AvatarPartScaleType</string>
              <string name="Value">Classic</string>
            </Properties>
          </Item>
          <Item class="Attachment" referent="A1">
            <Properties>
              <string name="Name">NeckRigAttachment</string>
              <CoordinateFrame name="CFrame">
                <X>0</X><Y>0.75</Y><Z>0.075</Z>
                <R00>1</R00><R01>0</R01><R02>0</R02>
                <R10>0</R10><R11>1</R11><R12>0</R12>
                <R20>0</R20><R21>0</R21><R22>1</R22>
              </CoordinateFrame>
            </Properties>
          </Item>
        </Item>
      </Item>
    </roblox>"""
    entries = rbxm.extract_assets(xml)
    assert len(entries) == 1
    e = entries[0]
    assert e["folder"] == "R15ArtistIntent"
    assert e["transparency"] == pytest.approx(0.0)
    assert e["original_size"] == pytest.approx((1.3, 1.7, 0.84))
    assert e["scale_type"] == "Classic"
    assert e["attachments"]["NeckRigAttachment"]["position"] == pytest.approx((0.0, 0.75, 0.075))


# ---------------------------------------------------------------------------
# 2. FileMesh Skinning + Linear Blend Skinning Pose Deformer
# ---------------------------------------------------------------------------


def _make_skinned_v6_glove_mesh() -> bytes:
    """Erzeugt ein v6.00-Mesh mit COREMESH + SKINNING (horizontaler Bind-Arm)."""
    box = roblox_mesh.make_box(0.6, 0.2, 0.2, center=(1.5, 3.5, 0.0))
    nverts = len(box.positions)
    nfaces = len(box.faces)

    verts_buf = bytearray()
    for p, n, uv in zip(box.positions, box.normals, box.uvs):
        verts_buf += struct.pack("<8f4B4B", p[0], p[1], p[2], n[0], n[1], n[2], uv[0], uv[1], 0, 0, 0, 255, 255, 255, 255, 255)
    faces_buf = b"".join(struct.pack("<3I", a, b, c) for a, b, c in box.faces)
    core_payload = struct.pack("<I", nverts) + bytes(verts_buf) + struct.pack("<I", nfaces) + faces_buf

    # Zwei Bones: RightLowerArm (Index 0, horizontal entlang +X) -> RightHand (Index 1)
    names = b"RightLowerArm\x00RightHand\x00"
    ident = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    bone0 = struct.pack("<Ihhf9f3f", 0, -1, -1, 1.0, *ident, 1.2, 3.5, 0.0)
    bone1 = struct.pack("<Ihhf9f3f", 14, 0, -1, 1.0, *ident, 1.8, 3.5, 0.0)
    skin_verts = struct.pack("<8B", 0, 1, 0, 0, 255, 0, 0, 0) * nverts
    subset = struct.pack("<5I26H", 0, nfaces, 0, nverts, 2, *([0, 1] + [0] * 24))
    skin_payload = (
        struct.pack("<I", nverts)
        + skin_verts
        + struct.pack("<I", 2)
        + bone0
        + bone1
        + struct.pack("<I", len(names))
        + names
        + struct.pack("<I", 1)
        + subset
    )

    out = bytearray(b"version 6.00\n")
    out += b"COREMESH" + struct.pack("<II", 1, len(core_payload)) + core_payload
    out += b"SKINNING" + struct.pack("<II", 1, len(skin_payload)) + skin_payload
    return bytes(out)


def test_skinning_chunk_parsing_und_pose_deformation():
    mesh = roblox_mesh.parse(_make_skinned_v6_glove_mesh())
    assert mesh.skinning is not None
    assert [b.name for b in mesh.skinning.bones] == ["RightLowerArm", "RightHand"]

    # Vor dem Skinning horizontal lang in X (0.6 > 0.2)
    assert mesh.size()[0] > mesh.size()[1]

    # Ziel-Rig hat senkrecht herabhaengenden Unterarm (0.8, 3.0, 0) -> (0.8, 2.4, 0)
    posed = roblox_mesh.skin_mesh_to_rig(
        mesh,
        {"RightLowerArm": (0.8, 3.0, 0.0), "RightHand": (0.8, 2.4, 0.0)},
        normal_offset=0.0,
    )
    assert posed is not None
    # Nach der 90-Grad-Drehung ist der Handschuh senkrecht in Y ausgerichtet und liegt auf X=0.8!
    assert posed.size()[1] == pytest.approx(0.6, abs=1e-4)
    assert posed.size()[0] == pytest.approx(0.2, abs=1e-4)
    assert posed.center()[0] == pytest.approx(0.8, abs=1e-4)
    assert posed.center()[1] == pytest.approx(2.7, abs=1e-4)


def test_rotation_between_ist_eigentliche_drehung():
    for a, b in [
        ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0)),
        ((0.4, -0.55, 0.07), (0.34, -0.94, 0.0)),
        ((0.0, 1.0, 0.0), (0.0, -1.0, 0.0)),  # 180-Grad-Grenzfall
    ]:
        rot = np.array(roblox_mesh.rotation_between(a, b))
        assert np.linalg.det(rot) == pytest.approx(1.0, abs=1e-5)
        mapped = rot @ (np.array(a) / np.linalg.norm(a))
        expected = np.array(b) / np.linalg.norm(b)
        assert np.allclose(mapped, expected, atol=1e-5)


# ---------------------------------------------------------------------------
# 3. Humanoid-Textur-Kompositor (`avatar_uv.py`)
# ---------------------------------------------------------------------------


def _make_classic_template_png(
    bg_rgba=(255, 0, 0, 255),  # absichtlich knallroter Hintergrund wie 'X'-Watermark!
    torso_front_rgba=(20, 180, 60, 255),
    sleeve_rgba=(30, 60, 200, 255),
    hand_rgba=(0, 0, 0, 0),
) -> bytes:
    arr = np.zeros((559, 585, 4), dtype=np.uint8)
    arr[:, :] = bg_rgba
    arr[74:202, 231:359] = torso_front_rgba
    arr[355:467, 217:281] = sleeve_rgba
    arr[355:483, 151:215] = (0, 0, 0, 0)
    arr[467:483, 217:281] = hand_rgba
    arr[485:549, 217:281] = hand_rgba
    buf = io.BytesIO()
    Image.fromarray(arr, mode="RGBA").save(buf, format="PNG")
    return buf.getvalue()


def test_humanoid_kompositor_ignoriert_template_wasserzeichen_und_laesst_haende_frei():
    skin = (0.78, 0.67, 0.47)
    shirt = _make_classic_template_png()

    torso_png = avatar_uv.composite_torso_texture(skin, shirt_bytes=shirt)
    assert torso_png is not None
    torso_im = Image.open(io.BytesIO(torso_png))
    assert torso_im.size == (388, 272)
    t_arr = np.asarray(torso_im)
    # Front-Panel (x=2..130, y=70..198) traegt gruene Shirt-Farbe, NICHT das rote Hintergrund-'X'
    front_px = t_arr[130, 64]
    assert front_px[1] > 170 and front_px[0] < 30 and front_px[3] == 255

    # Rechter Arm: Aermel (y=120, x=100) ist blau & opak, Hand (y=265, x=125) bleibt Haut-Glas (Alpha=0)!
    arm_png = avatar_uv.composite_limb_texture(skin, shirt, side="right")
    assert arm_png is not None
    a_arr = np.asarray(Image.open(io.BytesIO(arm_png)))
    assert a_arr[120, 100, 2] > 190 and a_arr[120, 100, 3] == 255
    assert a_arr[265, 125, 3] == 0  # Hand unbedeckt -> Hautfarbe im Glas
    # Spalte col2 (x=130..194) ist unbedeckt und darf keinen blauen Saum aus col1 (x=66..130) abbekommen
    assert a_arr[120, 131, 3] == 0


def test_headless_r15_erzeugt_keinen_hals_quader_und_nutzt_echte_kopfhoehe():
    """Headless-Avatare (z. B. MertaStudios) duerfen keinen beigen Hals-Quader am
    Kragen haben und Kronen/Hoerner schweben auf echter Headless-HatAttachment-Hoehe."""
    headless_mp = {
        "mesh": roblox_mesh.make_box(0.007, 0.007, 0.007),
        "rbxm_size": (1.0, 1.0, 1.0),
        "original_size": (1.0, 1.0, 1.0),
        "scale_type": "Classic",
        "attachments": {
            "NeckRigAttachment": {"position": (0.0, -0.5, 0.0), "rotation": avatar_rig.IDENTITY_MAT3},
            "HatAttachment": {"position": (0.0, 0.6, 0.0), "rotation": avatar_rig.IDENTITY_MAT3},
        },
    }
    rig = avatar_rig.assemble_r15_rig(
        {},
        headless_mp,
        roblox_api.DEFAULT_BODY_COLORS,
        dict(roblox_api.DEFAULT_SCALES),
        headless=True,
    )
    assert rig.head_mesh is None
    assert "hals" not in rig.body_meshes
    collar_top = rig.body_meshes["torso"].bbox()[1][1]
    hat_y = rig.world_attachments["HatAttachment"][0][1]
    assert hat_y - collar_top > 0.9


def test_opaque_mesh_texture_entfernt_alpha_loecher_ohne_hautfarben_bleed():
    arr = np.zeros((32, 32, 4), dtype=np.uint8)
    arr[:, :] = (18, 42, 78, 255)
    arr[10:14, 10:14] = (20, 50, 90, 0)  # Korblox-artiger Alpha=0-Fleck mit dunklem RGB
    buf = io.BytesIO()
    Image.fromarray(arr, mode="RGBA").save(buf, format="PNG")
    fixed = avatar_uv.prepare_opaque_mesh_texture(buf.getvalue())
    out = np.asarray(Image.open(io.BytesIO(fixed)))
    assert int(out[:, :, 3].min()) == 255
    assert tuple(out[12, 12, :3]) == (20, 50, 90)


# ---------------------------------------------------------------------------
# 4. Asymmetrische R15-Rigs & 180-Grad-Accessoire-Verschweissung
# ---------------------------------------------------------------------------


def test_asymmetrische_arme_und_korblox_stumpf_werden_nicht_gestreckt():
    """Korblox-Arm rechts (4,0 hoch) darf linken Standard-Arm (2,0 hoch)
    niemals auf 4,0 Studs strecken; kurzes Stumpf-Bein schwebt an der Huefte."""
    parts, metrics = avatar_rig.build_body(
        roblox_api.DEFAULT_BODY_COLORS,
        dict(roblox_api.DEFAULT_SCALES),
    )
    relayout, _ = avatar_rig.relayout_body(
        parts,
        metrics,
        {
            "torso": (2.0, 2.0, 1.0),
            "rechts_arm": (1.5, 4.0, 1.2),
            "links_arm": (1.0, 2.0, 1.0),
            "rechts_bein": (0.8, 1.1, 0.8),
            "links_bein": (1.0, 2.0, 1.0),
        },
        dict(roblox_api.DEFAULT_SCALES),
    )
    by_name = {p.name: p for p in relayout}
    assert by_name["links_arm"].mesh.size()[1] == pytest.approx(2.0)
    assert by_name["rechts_arm"].mesh.size()[1] == pytest.approx(4.0)
    assert by_name["links_bein"].mesh.bbox()[0][1] == pytest.approx(0.0)
    assert by_name["rechts_bein"].mesh.bbox()[0][1] == pytest.approx(0.9)


def test_weld_accessory_respektiert_180_grad_attachment_rotation_und_uniform_scale():
    """Gesichts-Accessoire mit gedrehtem HatAttachment landet vorne (-Z) im Gesicht
    und runde Röcke werden bei schmaler Breite (width=0.7) nicht oval gequetscht."""
    rig = avatar_rig.assemble_r15_rig(
        {},
        None,
        roblox_api.DEFAULT_BODY_COLORS,
        {"height": 0.9, "width": 0.7, "depth": 0.85, "head": 1.0},
        headless=False,
    )
    scars_mp = {
        "type_id": 8,
        "rbxm_class": "SpecialMesh",
        "mesh": roblox_mesh.make_box(0.8, 0.8, 0.1),
        "mesh_scale": (1.0, 1.0, 1.0),
        "mesh_offset": (0.0, 0.0, 0.0),
        "attachments": {
            "HatAttachment": {
                "position": (0.0, 0.6, -0.55),
                "rotation": ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, -1.0)),
            }
        },
    }
    welded_scars, _ = avatar_rig.weld_accessory(scars_mp, rig)
    # Muss vor dem Gesicht liegen (Z < head_center.z) statt am Hinterkopf!
    assert welded_scars.center()[2] < rig.head_mesh.center()[2] - 0.5

    skirt_mp = {
        "type_id": 47,
        "rbxm_class": "SpecialMesh",
        "mesh": roblox_mesh.make_box(3.4, 2.2, 3.4),
        "mesh_scale": (1.0, 1.0, 1.0),
        "mesh_offset": (0.0, 0.0, 0.0),
        "attachments": {
            "WaistCenterAttachment": {
                "position": (0.0, 0.8, 0.0),
                "rotation": avatar_rig.IDENTITY_MAT3,
            }
        },
    }
    welded_skirt, _ = avatar_rig.weld_accessory(skirt_mp, rig)
    # Runder Rock bleibt kreisrund (X == Z), kein Quetschen auf 0.7/0.85!
    assert welded_skirt.size()[0] == pytest.approx(welded_skirt.size()[2], abs=1e-5)


# ---------------------------------------------------------------------------
# 5. Blender-Renderer: Kamera & Lichter zeigen auf die Vorderseite (+Y)
# ---------------------------------------------------------------------------


def test_kamera_zeigt_auf_vorderseite_des_avatars():
    """Nach `obj_import(..., forward_axis='NEGATIVE_Z', up_axis='Y')` zeigt die
    Vorderseite des Roblox-Avatars (-Z) in Richtung Blender +Y."""
    render = load_render_module()
    source = open(render.__file__, encoding="utf-8").read()
    assert "Vector((-0.52, 1.0, 0.24))" in source
    assert 'alpha_mode = "CHANNEL_PACKED"' in source
    assert 'tex.extension = "REPEAT"' in source
