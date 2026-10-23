"""Integrationstest des Live-Pfads mit gefälschten API-Antworten.

Der komplette Ablauf von `build_avatar.build()` wird durchlaufen (Assets
klassifizieren, FileMesh parsen, Kopf ersetzen, Accessoires platzieren,
Texturen zuordnen, Manifest schreiben) - aber ohne Netzwerk: die
Roblox-Funktionen werden durch Attrappen ersetzt.
"""

from __future__ import annotations

import json
import os
import struct
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

import build_avatar  # noqa: E402
import render_config  # noqa: E402
import roblox_api  # noqa: E402
import roblox_mesh  # noqa: E402

ROOT = os.path.dirname(TOOLS)


def encode_v2(mesh: roblox_mesh.MeshData) -> bytes:
    """Serialisiert ein MeshData als FileMesh 2.00 (40-Byte-Vertices)."""
    out = bytearray(b"version 2.00\n")
    out += struct.pack("<HBBII", 12, 40, 12, len(mesh.positions), len(mesh.faces))
    for i, pos in enumerate(mesh.positions):
        nrm = mesh.normals[i] if len(mesh.normals) == len(mesh.positions) else (0.0, 0.0, 1.0)
        uv = mesh.uvs[i] if len(mesh.uvs) == len(mesh.positions) else (0.0, 0.0)
        out += struct.pack("<8f", pos[0], pos[1], pos[2], nrm[0], nrm[1], nrm[2], uv[0], uv[1])
        out += struct.pack("<4b", 0, 0, 127, 127)
        out += struct.pack("<4B", 255, 255, 255, 255)
    for (a, b, c) in mesh.faces:
        out += struct.pack("<3I", a, b, c)
    return bytes(out)


PNG_BYTES = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
             b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
             b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82")

AVATAR = {
    "_source": "fake",
    "playerAvatarType": "R15",
    "scales": {"height": 1.05, "width": 0.95, "head": 1.1, "depth": 1.0,
               "proportion": 0.0, "bodyType": 0.0},
    "bodyColors": {"headColorId": 226, "torsoColorId": 194, "leftArmColorId": 226,
                   "rightArmColorId": 226, "leftLegColorId": 9, "rightLegColorId": 9},
    "assets": [
        {"id": 1001, "name": "Head", "assetType": {"id": 17, "name": "Head"}},
        {"id": 1002, "name": "Freizeit-Hut", "assetType": {"id": 8, "name": "Hat"}},
        {"id": 1003, "name": "Blauhemd", "assetType": {"id": 11, "name": "Shirt"}},
        {"id": 1004, "name": "Emote", "assetType": {"id": 61, "name": "EmoteAnimation"}},
        {"id": 1005, "name": "Kapput", "assetType": {"id": 41, "name": "HairAccessory"}},
    ],
}

TYPE_BY_ID = {1001: 17, 1002: 8, 1003: 11, 1004: 61, 1005: 41}


@pytest.fixture
def fake_api(monkeypatch):
    """Ersetzt alle Netzwerk-Aufrufe durch lokale Attrappen."""
    head_mesh = roblox_mesh.make_box(1.0, 1.0, 1.0)      # "Head" als Quader
    hat_mesh = roblox_mesh.make_sphere(2.0, 12, 8)       # Hut: Kugel, 2 Studs
    hair_mesh = roblox_mesh.make_box(2.0, 1.0, 2.0)

    def fake_resolve(client, username):
        return 1234, "Builderman"

    def fake_avatar(client, user_id):
        return dict(AVATAR)

    def fake_details(client, asset_id):
        return {"assetTypeId": TYPE_BY_ID.get(asset_id, 0),
                "name": {1001: "Head", 1002: "Freizeit-Hut", 1003: "Blauhemd"}.get(asset_id, "")}

    def fake_download(client, asset_id):
        payload = {
            1001: encode_v2(head_mesh),
            1002: encode_v2(hat_mesh),
            1003: PNG_BYTES,
            1005: None,  # simuliert einen fehlgeschlagenen Download
        }.get(asset_id)
        if payload is None:
            return roblox_api.DownloadedAsset(asset_id, None, "", "",
                                              "assetdelivery: HTTP 403 User is not authorized")
        return roblox_api.DownloadedAsset(asset_id, payload, "application/octet-stream", "fake", "")

    monkeypatch.setattr(roblox_api, "resolve_user_id", fake_resolve)
    monkeypatch.setattr(roblox_api, "fetch_avatar", fake_avatar)
    monkeypatch.setattr(roblox_api, "fetch_asset_details", fake_details)
    monkeypatch.setattr(roblox_api, "download_asset", fake_download)


def test_voller_lauf_mit_attrappen(fake_api, tmp_path):
    manifest = build_avatar.build("builderman", str(tmp_path), verbose=False)

    assert manifest["user_id"] == 1234
    assert manifest["avatar_type"] == "R15"
    assert manifest["dry_run"] is False

    names = [p["name"] for p in manifest["parts"]]
    # Der Kopf wurde durch das echte Head-Mesh ersetzt
    assert "kopf_mesh" in names
    assert "kopf" not in names
    # Der Hut wurde platziert
    assert any("Freizeit-Hut" in n for n in names)
    # Audio/Animationen werden nie geladen
    assert not any("1004" in json.dumps(p) for p in manifest["parts"])

    # Shirt-Textur ist auf dem Torso gelandet
    torso = next(p for p in manifest["parts"] if p["name"] == "torso")
    assert torso["texture"], "Torso sollte die Shirt-Textur tragen"
    assert (tmp_path / torso["texture"]).exists()

    # Fehlgeschlagener Download landet unter "skipped", ohne Abbruch
    skipped_ids = {item["asset_id"] for item in manifest["skipped"]}
    assert 1005 in skipped_ids
    # Animationen (hier: Emote 1004) zaehlen NICHT mehr als "uebersprungen",
    # sondern werden getrennt als ignored_animations gezaehlt
    assert 1004 not in skipped_ids
    assert manifest["stats"]["ignored_animations"] == 1
    assert manifest["stats"]["failed"] == 1
    assert [item["asset_id"] for item in manifest["ignored"]] == [1004]

    # Dateien sind da und nicht leer
    for part in manifest["parts"]:
        assert (tmp_path / part["obj"]).stat().st_size > 50
    assert (tmp_path / "manifest.json").exists()
    assert (tmp_path / "avatar.json").exists()
    summary = (tmp_path / "summary.md").read_text()
    assert "Builderman" in summary

    # Der Hut muss oberhalb der Körpermitte sitzen
    hat = next(p for p in manifest["parts"] if "Freizeit-Hut" in p["name"])
    top = 0.0
    for line in (tmp_path / hat["obj"]).read_text().splitlines():
        if line.startswith("v "):
            top = max(top, float(line.split()[2]))
    assert top > 4.0


def test_build_scheitert_nicht_ohne_assets(fake_api, monkeypatch, tmp_path):
    leer = dict(AVATAR)
    leer["assets"] = []
    monkeypatch.setattr(roblox_api, "fetch_avatar", lambda client, user_id: dict(leer))

    manifest = build_avatar.build("Builderman", str(tmp_path), verbose=False)
    assert manifest["stats"]["accessories"] == 0
    assert manifest["stats"]["parts"] >= 7
    assert (tmp_path / "render_avatar.png").exists() is False  # Render macht Blender


def test_build_meldet_unbekannten_username(tmp_path):
    def boom(client, username):
        raise roblox_api.RobloxError("Roblox-Username 'gibtsnicht' wurde nicht gefunden.")

    original = roblox_api.resolve_user_id
    roblox_api.resolve_user_id = boom
    try:
        with pytest.raises(roblox_api.RobloxError):
            build_avatar.build("gibtsnicht", str(tmp_path), verbose=False)
    finally:
        roblox_api.resolve_user_id = original


def _pose_config(pose_name):
    config = dict(render_config.DEFAULTS)
    config.update({
        "_pfad": os.path.join(ROOT, "Configs.toml"),
        "_posen_pfad": os.path.join(ROOT, "Posen.toml"),
        "pose": pose_name,
    })
    return config


def _max_y(path):
    top = 0.0
    for line in open(path, encoding="utf-8"):
        if line.startswith("v "):
            top = max(top, float(line.split()[2]))
    return top


def test_pose_bewegt_koerper_und_accessoires(fake_api, tmp_path):
    stand = build_avatar.build("Builderman", str(tmp_path / "stand"), verbose=False)
    posed = build_avatar.build(
        "Builderman", str(tmp_path / "posed"), verbose=False,
        config=_pose_config("Denker"))

    assert stand["render"]["pose"] == "None"
    assert stand["render"]["pose_aktiv"] is False
    assert posed["render"]["pose"] == "Denker"
    assert posed["render"]["sichtbar_oben_prozent"] == 7
    assert posed["render"]["avatar_hoch_studs"] == -0.7
    assert posed["render"]["pose_aktiv"] is True

    def hat_top(manifest, base):
        hat = next(p for p in manifest["parts"] if "Freizeit-Hut" in p["name"])
        return _max_y(os.path.join(base, hat["obj"]))

    # "Denker" senkt den Avatar ab: auch der Hut als Accessoire muss mitgehen.
    assert hat_top(posed, tmp_path / "posed") < hat_top(stand, tmp_path / "stand") - 0.1
