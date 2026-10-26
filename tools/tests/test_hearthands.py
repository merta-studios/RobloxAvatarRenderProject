"""Regressionstests fuer die hearthands-Pose und das erzwungene Woman-Rig."""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
ROOT = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
sys.path.insert(0, HERE)

import avatar_pose  # noqa: E402
import avatar_rig  # noqa: E402
import build_avatar  # noqa: E402
import render_config  # noqa: E402
import roblox_api  # noqa: E402
import roblox_mesh  # noqa: E402
import standard_r15  # noqa: E402
from rbxm_factory import make_rbxm  # noqa: E402
from test_build_integration import encode_v2  # noqa: E402


WOMAN_IDS = {
    27: 86499666,
    28: 86499698,
    29: 86499716,
}


def _attachment(name, pos, parent):
    return {"class": "Attachment", "props": {"Name": name, "CFrame": pos},
            "Parent": parent}


def _woman_asset(type_id):
    """Woman-R15-Asset mit passenden Segmentnamen und RigAttachments."""
    instances = []

    def segment(name, position, size, atts):
        parent = len(instances)
        instances.append({"class": "MeshPart",
                          "props": {"Name": name,
                                    "MeshId": "rbxassetid://99000001",
                                    "size": size, "CFrame": position},
                          "Parent": None})
        for attachment_name, attachment_pos in atts.items():
            instances.append(_attachment(attachment_name, attachment_pos, parent))

    if type_id == 27:
        segment("LowerTorso", (0.0, 1.5, 0.0), (2.0, 1.0, 1.0), {
            "RootRigAttachment": (0.0, -0.5, 0.0),
            "WaistRigAttachment": (0.0, 0.5, 0.0),
            "LeftHipRigAttachment": (-0.5, -0.4, 0.0),
            "RightHipRigAttachment": (0.5, -0.4, 0.0),
        })
        segment("UpperTorso", (0.0, 2.5, 0.0), (2.0, 1.0, 1.0), {
            "WaistRigAttachment": (0.0, -0.5, 0.0),
            "NeckRigAttachment": (0.0, 0.5, 0.0),
            "LeftShoulderRigAttachment": (-1.0, 0.25, 0.0),
            "RightShoulderRigAttachment": (1.0, 0.25, 0.0),
        })
    else:
        side = "Right" if type_id == 28 else "Left"
        sign = 1.0 if side == "Right" else -1.0
        x = sign * 1.25
        segment(f"{side}UpperArm", (x, 2.0, 0.0), (0.5, 1.0, 0.5), {
            f"{side}ShoulderRigAttachment": (-sign * 0.25, 0.5, 0.0),
            f"{side}ElbowRigAttachment": (0.0, -0.5, 0.0),
        })
        segment(f"{side}LowerArm", (x, 1.0, 0.0), (0.5, 1.0, 0.5), {
            f"{side}ElbowRigAttachment": (0.0, 0.5, 0.0),
            f"{side}WristRigAttachment": (0.0, -0.5, 0.0),
        })
        segment(f"{side}Hand", (x, 0.5, 0.0), (0.5, 0.3, 0.5), {
            f"{side}WristRigAttachment": (0.0, 0.15, 0.0),
            f"{side}GripAttachment": (0.0, -0.1, -0.1),
        })
    return make_rbxm(instances)


def test_woman_body_is_added_only_for_non_woman_hearthands_slots():
    assets = [
        {"id": WOMAN_IDS[27], "name": "Woman Torso",
         "assetType": {"id": 27}},
        {"id": 4001, "name": "Custom Right Arm",
         "assetType": {"id": 28}},
    ]
    additions, forced = build_avatar.woman_body_overrides("hearthands", assets)
    assert [a["id"] for a in additions] == [WOMAN_IDS[28], WOMAN_IDS[29]]
    assert forced == {28: WOMAN_IDS[28], 29: WOMAN_IDS[29]}

    assert build_avatar.woman_body_overrides("Winken", assets) == ([], {})
    already_woman = [
        {"id": asset_id, "name": name,
         "assetType": {"id": type_id}}
        for type_id, (asset_id, name) in build_avatar.WOMAN_BODY_ASSETS.items()
    ]
    assert build_avatar.woman_body_overrides("hearthands", already_woman) == ([], {})


def test_heart_hand_cage_bends_thumb_down_and_finger_edge_up():
    wrist = (0.0, 0.0, 0.0)
    vertices = [
        (0.0, 0.0, 0.0),
        (-0.2, 0.2, 0.0), (0.2, 0.2, 0.0),
        (-0.2, 0.8, 0.0), (0.2, 0.8, 0.0), (0.0, 0.8, 0.0),
    ]
    shaped = avatar_pose.deform_hand_to_heart(vertices, wrist, "Right")

    # Beim rechten Arm liegt der Daumen auf der zur Körpermitte gerichteten
    # Seite (x negativ); seine Spitze klappt nach unten. Die andere Fingerkante
    # rundet sich nach oben. Beide ziehen leicht zur Mitte und nach vorn.
    assert shaped[3][0] < vertices[3][0]
    assert shaped[3][1] < vertices[3][1]
    assert shaped[4][0] < vertices[4][0]
    assert shaped[4][1] > vertices[4][1]
    assert shaped[3][2] < vertices[3][2]


def test_pose_context_wendet_herzform_nur_auf_handsegmente_an():
    class Rig:
        joint_frames = {}
        segment_spans = {"rechts_arm": [("RightHand", 0, 6)]}

    joints = {
        "LowerTorso": (0.0, 0.0, 0.0), "UpperTorso": (0.0, 2.0, 0.0),
        "Head": (0.0, 4.0, 0.0), "RightUpperArm": (1.0, 4.0, 0.0),
        "RightLowerArm": (1.0, 2.6, 0.0), "RightHand": (1.0, 1.6, 0.0),
        "LeftUpperArm": (-1.0, 4.0, 0.0), "LeftLowerArm": (-1.0, 2.6, 0.0),
        "LeftHand": (-1.0, 1.6, 0.0), "RightUpperLeg": (0.5, 2.0, 0.0),
        "RightLowerLeg": (0.5, 1.0, 0.0), "RightFoot": (0.5, 0.2, 0.0),
        "LeftUpperLeg": (-0.5, 2.0, 0.0), "LeftLowerLeg": (-0.5, 1.0, 0.0),
        "LeftFoot": (-0.5, 0.2, 0.0),
    }
    mesh = roblox_mesh.MeshData(
        positions=[(1.0, 1.6, 0.0), (0.8, 1.8, 0.0), (1.2, 1.8, 0.0),
                   (0.8, 2.4, 0.0), (1.2, 2.4, 0.0), (1.0, 2.4, 0.0)],
        normals=[(0.0, 0.0, 1.0)] * 6,
        faces=[(0, 1, 2), (1, 3, 5), (2, 5, 4)],
    )
    context = avatar_pose.PoseContext(
        {"rechte_hand": {"handgelenk_vor_beugen_grad": 1}},
        joints, rig=Rig(), hand_form="heart")
    posed = context.apply_body("rechts_arm", mesh)

    assert posed.positions != mesh.positions
    assert len(posed.normals) == len(mesh.positions)
    assert posed.positions[3][0] < mesh.positions[3][0]


def test_hearthands_bringt_beide_haende_zur_brustmitte():
    rig = avatar_rig.assemble_r15_rig(
        {}, None, dict(roblox_api.DEFAULT_BODY_COLORS),
        dict(roblox_api.DEFAULT_SCALES),
        standard_segments=standard_r15.standard_segments(),
    )
    pose = avatar_pose.load_poses(os.path.join(ROOT, "Posen.toml"))["hearthands"]
    context = avatar_pose.PoseContext(
        pose["teile"], rig.joint_positions, settings=pose["einstellungen"],
        rig=rig, hand_form=pose["hand_form"])
    assert context.active is True

    hand_bounds = {}
    for side, part_name in (("Right", "rechts_arm"), ("Left", "links_arm")):
        mesh = context.apply_body(part_name, rig.body_meshes[part_name])
        _, start, end = next(span for span in rig.segment_spans[part_name]
                             if span[0] == f"{side}Hand")
        vertices = mesh.positions[start:end]
        center_x = sum(vertex[0] for vertex in vertices) / len(vertices)
        center_y = sum(vertex[1] for vertex in vertices) / len(vertices)
        assert abs(center_x) < 0.3
        assert 3.0 < center_y < 3.7
        hand_bounds[side] = (min(vertex[0] for vertex in vertices),
                             max(vertex[0] for vertex in vertices))

    # Die beiden Hand-Meshes treffen sich in der Brustmitte statt seitlich zu
    # bleiben wie in der neutralen Stand-Pose.
    assert hand_bounds["Right"][0] <= hand_bounds["Left"][1]


def test_hearthands_build_laedt_und_verwendet_frauenteile(monkeypatch, tmp_path):
    avatar = {
        "_source": "hearthands-test",
        "playerAvatarType": "R15",
        "scales": dict(roblox_api.DEFAULT_SCALES),
        "bodyColors": dict(roblox_api.DEFAULT_BODY_COLORS),
        "assets": [
            {"id": 4001, "name": "Old Torso",
             "assetType": {"id": 27, "name": "Torso"}},
            {"id": 4002, "name": "Old Right Arm",
             "assetType": {"id": 28, "name": "RightArm"}},
            {"id": 4003, "name": "Old Left Arm",
             "assetType": {"id": 29, "name": "LeftArm"}},
        ],
    }
    raw_mesh = encode_v2(roblox_mesh.make_box(1.0, 1.0, 1.0))
    package_data = {type_id: _woman_asset(type_id)
                    for type_id in WOMAN_IDS}
    calls = []

    monkeypatch.setattr(roblox_api, "resolve_user_id",
                        lambda client, username: (1234, "Builderman"))
    monkeypatch.setattr(roblox_api, "fetch_avatar",
                        lambda client, user_id: dict(avatar))
    monkeypatch.setattr(roblox_api, "fetch_asset_details",
                        lambda client, asset_id: {"assetTypeId": 0, "name": ""})

    def fake_download(client, asset_id):
        calls.append(asset_id)
        data = {
            4001: raw_mesh, 4002: raw_mesh, 4003: raw_mesh,
            99000001: raw_mesh,
            **{asset_id: package_data[type_id]
               for type_id, asset_id in WOMAN_IDS.items()},
        }.get(asset_id)
        if data is None:
            return roblox_api.DownloadedAsset(asset_id, None, "", "", "not in test")
        return roblox_api.DownloadedAsset(asset_id, data,
                                          "application/octet-stream", "fake", "")

    monkeypatch.setattr(roblox_api, "download_asset", fake_download)
    config = dict(render_config.DEFAULTS)
    config.update({
        "pose": "hearthands",
        "_pfad": os.path.join(ROOT, "Configs.toml"),
        "_posen_pfad": os.path.join(ROOT, "Posen.toml"),
    })
    manifest = build_avatar.build("Builderman", str(tmp_path), verbose=False,
                                  config=config)

    assert all(asset_id in calls for asset_id in WOMAN_IDS.values())
    assert manifest["render"]["pose"] == "hearthands"
    assert manifest["render"]["pose_aktiv"] is True
    assert manifest["render"]["pose_hand_form"] == "heart"
    body_parts = {part["name"]: part for part in manifest["parts"]}
    assert body_parts["torso"]["asset_id"] == WOMAN_IDS[27]
    assert body_parts["rechts_arm"]["asset_id"] == WOMAN_IDS[28]
    assert body_parts["links_arm"]["asset_id"] == WOMAN_IDS[29]
    for side, part_name in (("Right", "rechts_arm"), ("Left", "links_arm")):
        arm_obj = tmp_path / body_parts[part_name]["obj"]
        vertices = [tuple(float(value) for value in line.split()[1:4])
                    for line in arm_obj.read_text(encoding="utf-8").splitlines()
                    if line.startswith("v ")]
        _, start, end = next(span for span in manifest["segment_spans"][part_name]
                             if span[0] == f"{side}Hand")
        hand_center_x = sum(vertex[0] for vertex in vertices[start:end]) / (end - start)
        assert abs(hand_center_x) < 0.5
    overrides = {item["id"] for item in manifest["assets"]
                 if item.get("pose_override") == "hearthands:woman_rig"}
    assert overrides == set(WOMAN_IDS.values())
    assert not any("Old Torso" in part.get("note", "")
                   for part in manifest["parts"])
