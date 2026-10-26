"""Regressionstests fuer die hearthands-Pose und das erzwungene Woman-Rig."""

from __future__ import annotations

import math
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
    """Woman-R15-Asset mit den *echten* Woman-Proportionen.

    Segmentgrößen und RigAttachment-Positionen stammen aus einem echten
    Render-Lauf (Diagnose "Segment-Rohdaten"): schmaler Torso (Schulter
    ±0,61 statt ±1,0 beim Blocky-Rig), kurzer Oberarm und eine in der
    Bind-Pose nach außen/vorne geneigte Armkette. Genau an diesen
    Proportionen ist die hearthands-Pose abgestimmt – mit den früheren,
    Blocky-ähnlichen Fake-Werten (Schulter ±1,0, senkrechte Arme) wäre der
    X-Arme-Fehler im Test unsichtbar geblieben.
    """
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
        segment("LowerTorso", (0.0, 0.0, 0.0), (1.33, 0.41, 0.72), {
            "RootRigAttachment": (0.0, -0.46, 0.02),
            "WaistRigAttachment": (0.0, 0.10, -0.05),
            "LeftHipRigAttachment": (-0.35, -0.21, 0.02),
            "RightHipRigAttachment": (0.35, -0.21, 0.02),
        })
        segment("UpperTorso", (0.0, 0.79, -0.05), (1.30, 1.70, 0.84), {
            "WaistRigAttachment": (0.0, -0.69, 0.0),
            "NeckRigAttachment": (0.0, 0.75, 0.08),
            "LeftShoulderRigAttachment": (-0.61, 0.33, 0.08),
            "RightShoulderRigAttachment": (0.61, 0.33, 0.08),
        })
    else:
        side = "Right" if type_id == 28 else "Left"
        sign = 1.0 if side == "Right" else -1.0
        segment(f"{side}UpperArm", (sign * 0.85, 0.79, 0.03), (0.75, 0.89, 0.58), {
            f"{side}ShoulderRigAttachment": (-sign * 0.24, 0.33, 0.0),
            f"{side}ElbowRigAttachment": (sign * 0.16, -0.23, 0.07),
        })
        segment(f"{side}LowerArm", (sign * 1.20, 0.18, -0.10), (0.66, 0.90, 0.59), {
            f"{side}ElbowRigAttachment": (-sign * 0.19, 0.36, 0.07),
            f"{side}WristRigAttachment": (sign * 0.19, -0.40, -0.20),
        })
        segment(f"{side}Hand", (sign * 1.48, -0.37, -0.27), (0.55, 0.59, 0.53), {
            f"{side}WristRigAttachment": (-sign * 0.09, 0.17, 0.10),
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


def test_smooth_normals_by_position_vereinheitlicht_uv_duplikate():
    mesh = roblox_mesh.MeshData(
        positions=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0),
                   (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        normals=[(0.0, 0.0, 1.0)] * 6,
        uvs=[(0.0, 0.0)] * 6,
        faces=[(0, 1, 2), (3, 4, 5)],
    )
    smoothed = roblox_mesh.smooth_normals_by_position(mesh)
    for index in range(3):
        assert smoothed.normals[index] == smoothed.normals[index + 3]
    assert len(smoothed.positions) == len(smoothed.normals)
    assert smoothed.faces == mesh.faces


def test_heart_hand_cage_formt_lobus_kerbe_und_spitze():
    """Der neue Cage bildet eine echte Halbherz-Silhouette statt einer Spitze."""
    wrist = (0.0, 0.0, 0.0)
    grid = [(x, y, 0.0) for y in (0.0, 0.2, 0.4, 0.6, 0.8)
            for x in (-0.2, 0.0, 0.2)]
    mirrored_grid = [(-x, y, z) for x, y, z in grid]
    right = avatar_pose.deform_hand_to_heart(grid, wrist, "Right", center_x=0.0)
    left = avatar_pose.deform_hand_to_heart(mirrored_grid, wrist, "Left",
                                            center_x=0.0)

    # Die linke Haelfte ist exakt gespiegelt.
    for r, l in zip(right, left):
        assert abs(r[0] + l[0]) < 1e-6
        assert abs(r[1] - l[1]) < 1e-6

    # Oben liegt die Innenkante deutlich tiefer als der aeussere Lobus:
    # dadurch entsteht in der Mitte die V-Kerbe des Herzens.
    assert right[14][1] > right[12][1] + 0.10
    # Unten treffen sich beide Haelfte in einer schmalen Spitze.
    bottom_y = min(p[1] for p in right)
    bottom_x = [p[0] for p in right if abs(p[1] - bottom_y) < 1e-6]
    assert max(abs(x) for x in bottom_x) < 0.03
    # Die Herzmitte ist deutlich breiter als die Spitze.
    assert max(p[0] for p in right[6:9]) > max(p[0] for p in right[0:3]) + 0.10


def test_hearthands_kamera_ist_dicht_und_schneidet_beine_ab():
    pose = avatar_pose.load_poses(os.path.join(ROOT, "Posen.toml"))["hearthands"]
    settings = pose["einstellungen"]
    assert settings["sichtbar_unten_prozent"] <= -30
    assert settings["kamera_abstand_studs"] <= -0.5
    assert settings["sichtbar_oben_prozent"] >= 10


def test_herzpose_fuegt_gelenkmanschetten_zwischen_armsegmenten_ein():
    """Starre R15-Segmente bekommen bei der Herzpose eine weiche Bruecke."""
    class Rig:
        joint_frames = {}
        segment_spans = {
            "rechts_arm": [("RightUpperArm", 0, 8),
                           ("RightLowerArm", 8, 16)],
        }

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
    upper = [(1.0 + x, 3.5 + y, z)
             for x in (-0.2, 0.2) for y in (-0.2, 0.2) for z in (-0.2, 0.2)]
    lower = [(1.0 + x, 2.8 + y, z)
             for x in (-0.2, 0.2) for y in (-0.2, 0.2) for z in (-0.2, 0.2)]
    positions = upper + lower
    mesh = roblox_mesh.MeshData(
        positions=positions,
        normals=[(0.0, 1.0, 0.0)] * len(positions),
        uvs=[(0.0, 0.0)] * len(positions),
        faces=[(0, 1, 2), (8, 9, 10), (0, 8, 9)],
    )
    context = avatar_pose.PoseContext(
        {"rechter_oberarm": {"vor_hoch_schwingen_grad": 1}},
        joints, rig=Rig(), hand_form="heart")
    posed = context.apply_body("rechts_arm", mesh)

    assert len(posed.positions) > len(mesh.positions)
    assert len(posed.faces) > len(mesh.faces)
    assert len(posed.normals) == len(posed.positions)
    assert all(math.isfinite(value) for p in posed.positions for value in p)

    connectors = posed.positions[len(mesh.positions):]
    posed_upper = posed.positions[:8]
    posed_lower = posed.positions[8:16]

    def nearest(point, candidates):
        return min(math.dist(point, other) for other in candidates)

    # Mindestens ein Manschetten-Vertice liegt gleichzeitig in der Naehe
    # beider Segmente - die Luecke ist damit geometrisch ueberbrueckt.
    assert any(nearest(c, posed_upper) < 0.35 and nearest(c, posed_lower) < 0.35
               for c in connectors)


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


def test_hearthands_auf_standard_rig_nur_fallback_naeherung():
    """Blocky-Standard-Rig = nur Fallback (hearthands erzwingt das Woman-Rig).

    Die Pose ist auf die Woman-Proportionen abgestimmt (schmale Schultern);
    auf dem breiten Blocky-Rig (Schulter ±1,0, lange Oberarme) treffen sich
    die Hände deshalb nicht exakt, müssen aber deutlich zur Brustmitte
    wandern, vor dem Torso auf Brusthöhe landen und dürfen auf keinen Fall
    auf die Gegenseite kreuzen (der alte X-Arme-Fehler). Das exakte
    Aufeinandertreffen sichert der Build-Test mit dem Woman-Fixture ab.
    """
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

    def segment_center(mesh, part_name, segment_name):
        _, start, end = next(span for span in rig.segment_spans[part_name]
                             if span[0] == segment_name)
        vertices = mesh.positions[start:end]
        return tuple(sum(vertex[i] for vertex in vertices) / len(vertices)
                     for i in range(3))

    hand_centers = {}
    for side, part_name in (("Right", "rechts_arm"), ("Left", "links_arm")):
        bind_center = segment_center(rig.body_meshes[part_name], part_name,
                                     f"{side}Hand")
        mesh = context.apply_body(part_name, rig.body_meshes[part_name])
        center = segment_center(mesh, part_name, f"{side}Hand")
        lower_center = segment_center(mesh, part_name, f"{side}LowerArm")
        hand_centers[side] = center

        # Deutlich zur Mitte gewandert, aber auf der eigenen Seite geblieben.
        assert abs(center[0]) < abs(bind_center[0]) - 0.8
        assert (center[0] > 0.15) if side == "Right" else (center[0] < -0.15)
        # Brusthöhe und klar vor dem Torso (Torso-Front bei z=-0,5).
        assert 2.85 < center[1] < 3.45
        assert center[2] < -0.55
        # Unterarm/Ellbogen bleibt außerhalb der Hand (kein X-Kreuzen).
        assert abs(lower_center[0]) > abs(center[0]) + 0.25

    # Rechter Hand-Schwerpunkt bleibt rechts vom linken.
    assert hand_centers["Right"][0] > hand_centers["Left"][0]


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
    # Die Herzpose rendert beide Arme mit echtem Smooth-Shading.
    assert body_parts["rechts_arm"]["smooth"] is True
    assert body_parts["links_arm"]["smooth"] is True
    def load_vertices(part_name):
        part_obj = tmp_path / body_parts[part_name]["obj"]
        return [tuple(float(value) for value in line.split()[1:4])
                for line in part_obj.read_text(encoding="utf-8").splitlines()
                if line.startswith("v ")]

    def segment_vertices(part_name, segment_name):
        vertices = load_vertices(part_name)
        _, start, end = next(span for span in manifest["segment_spans"][part_name]
                             if span[0] == segment_name)
        return vertices[start:end]

    def center_of(vertices):
        return tuple(sum(vertex[i] for vertex in vertices) / len(vertices)
                     for i in range(3))

    torso_vertices = load_vertices("torso")
    torso_lo = tuple(min(v[i] for v in torso_vertices) for i in range(3))
    torso_hi = tuple(max(v[i] for v in torso_vertices) for i in range(3))

    hand_bounds = {}
    for side, part_name in (("Right", "rechts_arm"), ("Left", "links_arm")):
        hand_vertices = segment_vertices(part_name, f"{side}Hand")
        hand_center = center_of(hand_vertices)
        lower_center = center_of(segment_vertices(part_name, f"{side}LowerArm"))
        x_lo = min(v[0] for v in hand_vertices)
        x_hi = max(v[0] for v in hand_vertices)
        y_lo = min(v[1] for v in hand_vertices)
        hand_bounds[side] = (x_lo, x_hi)
        sign = 1.0 if side == "Right" else -1.0

        # Hand-Schwerpunkt an der Brustmitte auf der EIGENEN Seite. Mit den
        # alten Winkeln landete er auf der Gegenseite (rechter Arm: x=-0,33,
        # Fingerspitzen bis x=-0,72) - das X-Arme-Bild im echten Render.
        assert -0.05 <= sign * hand_center[0] <= 0.35
        # Auch die innerste Hand-Ecke (Fingerspitze) bleibt nahe der Mitte.
        inner_edge = x_lo if side == "Right" else x_hi
        assert sign * inner_edge >= -0.35
        # Obere Brust (nicht am Hals, nicht am Bauch) und klar vor dem Torso.
        assert torso_lo[1] + 0.5 * (torso_hi[1] - torso_lo[1]) < hand_center[1] \
            < torso_hi[1] - 0.3
        assert hand_center[2] < torso_lo[2] - 0.25
        # Fingerspitzen zeigen nach unten zur Herzspitze (Hand-Unterkante
        # deutlich unter dem Hand-Schwerpunkt), Ellbogen/Unterarm außen.
        assert y_lo < hand_center[1] - 0.25
        assert abs(lower_center[0]) > abs(hand_center[0]) + 0.3

    # Die Hände berühren sich an der Brustmitte, ohne dass eine Hand
    # mehrheitlich auf der Gegenseite endet.
    assert hand_bounds["Right"][0] <= 0.10
    assert hand_bounds["Left"][1] >= -0.10
    assert hand_bounds["Right"][0] <= hand_bounds["Left"][1] + 0.05
    overrides = {item["id"] for item in manifest["assets"]
                 if item.get("pose_override") == "hearthands:woman_rig"}
    assert overrides == set(WOMAN_IDS.values())
    assert not any("Old Torso" in part.get("note", "")
                   for part in manifest["parts"])
