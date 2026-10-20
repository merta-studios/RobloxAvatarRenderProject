"""End-to-End-Test mit dem echten Avatar von MertaStudios (UserId 8593053805).

Die Avatar-Definition (18 Assets) stammt 1:1 von
`avatar.roblox.com/v1/users/8593053805/avatar` (siehe
fixtures/merta_avatar.json). Die Asset-Downloads werden gemockt: Koerperteile
und Accessoires kommen als synthetische RBXM-Modelle (wie im echten Leben -
genau daran scheiterte die Pipeline vorher mit "0 Accessoires, 16
uebersprungen"), Shirt/Pants als PNG, die 7 Animationen duerfen gar nicht
erst heruntergeladen werden, der DynamicHead ist ein "Headless Head" mit
degenerierter Geometrie.
"""

from __future__ import annotations

import gzip
import json
import os
import struct
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)
sys.path.insert(0, HERE)

import build_avatar  # noqa: E402
import roblox_api  # noqa: E402
import roblox_mesh  # noqa: E402
from rbxm_factory import make_rbxm  # noqa: E402
from test_build_integration import PNG_BYTES, encode_v2  # noqa: E402

USER_ID = 8593053805

ANIMATION_IDS = {
    88812736819289, 92684641308924, 92979925036969, 95949707663366,
    116737021859282, 121991851630650, 131180413715531,
}


def _tiny_mesh() -> roblox_mesh.MeshData:
    """Ein einziges Dreieck - die degenerierte Geometrie des Headless Head."""
    return roblox_mesh.MeshData(
        positions=[(0.0, 0.0, 0.0), (0.01, 0.0, 0.0), (0.0, 0.01, 0.0)],
        normals=[(0.0, 0.0, 1.0)] * 3,
        uvs=[(0.0, 0.0)] * 3,
        faces=[(0, 1, 2)],
    )


def _rbxm_meshpart(name, mesh_id, size=(1.0, 2.0, 1.0), texture_id=None):
    props = {"Name": name, "MeshId": f"rbxassetid://{mesh_id}",
             "size": size, "CFrame": (0.0, 1.0, 0.0)}
    if texture_id is not None:
        props["TextureID"] = f"rbxassetid://{texture_id}"
    return make_rbxm([{"class": "MeshPart", "props": props, "Parent": None}])


def _rbxm_torso_specialmesh(mesh_id):
    """Wie aeltere Bundles: Part + SpecialMesh-Kind statt MeshPart."""
    part = {"class": "Part",
            "props": {"Name": "Torso", "size": (2.0, 2.0, 1.0),
                      "CFrame": (0.0, 3.0, 0.0)},
            "Parent": None}
    mesh = {"class": "SpecialMesh",
            "props": {"Name": "Mesh", "MeshId": f"rbxassetid://{mesh_id}"},
            "Parent": 0}
    return make_rbxm([part, mesh])


def _pants_rbxm(template_id):
    """Klassische Hose als RBXM-Modell (Bild-ID in PantsTemplate)."""
    return make_rbxm([{"class": "Pants",
                       "props": {"Name": "Formal",
                                 "PantsTemplate": f"rbxassetid://{template_id}"},
                       "Parent": None}])


def _rbxm_arm_segments():
    """R15-Arm als zwei Segmente (UpperArm oben, Hand unten).

    So liefern echte Bundle-RBXMs die Koerperteile aus - die Pipeline
    muss sie per CFrame-Position zusammenfuehren, statt nur das erste
    Segment zu benutzen (im echten Lauf war der Korblox-Arm nur eine
    schwebende Klaue)."""
    upper = {"class": "MeshPart",
             "props": {"Name": "LeftUpperArm",
                       "MeshId": "rbxassetid://900000002",
                       "size": (1.0, 1.0, 1.0), "CFrame": (0.0, 1.5, 0.0)},
             "Parent": None}
    hand = {"class": "MeshPart",
            "props": {"Name": "LeftHand",
                      "MeshId": "rbxassetid://900000022",
                      "size": (0.8, 0.6, 0.8), "CFrame": (0.0, 0.3, 0.0)},
            "Parent": None}
    return make_rbxm([upper, hand])


def _badge_mesh_bytes():
    """Badge als echtes FileMesh 7.00 (Draco), ohne DracoPy als v2.

    Das echte Bricksmith-Badge (Mesh 110536326812594) ist ein v7-Mesh
    und scheiterte frueher mit 'Datei zu kurz'."""
    badge = roblox_mesh.make_box(0.6, 0.6, 0.2)
    try:
        import DracoPy
        import numpy as np
    except ImportError:
        return encode_v2(badge)
    blob = DracoPy.encode(np.array(badge.positions, dtype=float),
                        faces=np.array(badge.faces, dtype=np.uint32))
    core = struct.pack("<I", len(blob)) + blob
    return (b"version 7.00\n" + b"COREMESH"
            + struct.pack("<II", 2, len(core)) + core)


def _merta_downloads():
    box = lambda w, h, d: encode_v2(roblox_mesh.make_box(w, h, d))  # noqa: E731
    sphere = encode_v2(roblox_mesh.make_sphere(1.0, 12, 8))
    return {
        # Koerperteile (RBXM) + nachgeladene Meshes
        27112025: _rbxm_torso_specialmesh(900000001),
        900000001: box(3.0, 1.0, 0.5),
        27112052: _rbxm_arm_segments(),   # LeftArm: UpperArm + Hand
        900000002: box(1.0, 1.0, 1.0),
        900000022: box(0.8, 0.6, 0.8),
        139607625: _rbxm_meshpart("RightArm", 900000003),
        900000003: sphere,
        134082507: _rbxm_meshpart("LeftLeg", 900000004),
        900000004: box(1.0, 2.0, 1.0),
        139607718: _rbxm_meshpart("RightLeg", 900000005),
        900000005: sphere,
        # Accessoires (RBXM; "Green" zusaetzlich gzip-gepackt, wie es die
        # Asset Delivery im echten Lauf ausgeliefert hat)
        73553031478451: gzip.compress(
            _rbxm_meshpart("Green", 900000011, size=(1.6, 0.8, 1.6))),
        900000011: sphere,
        126241427519956: _rbxm_meshpart("White Void Crown", 900000012,
                                        size=(1.4, 1.2, 1.4)),
        900000012: box(1.4, 1.2, 1.4),
        72574684946317: _rbxm_meshpart("Bricksmith Badge", 900000013,
                                       size=(0.6, 0.6, 0.2)),
        900000013: _badge_mesh_bytes(),   # FileMesh 7.00 wie in echt
        # Headless Head: RBXM zeigt auf ein degeneriertes Mesh (1 Face)
        15093053680: _rbxm_meshpart("Head", 900000014, size=(0.01, 0.01, 0.01)),
        900000014: encode_v2(_tiny_mesh()),
        # Kleidung: Shirt kommt direkt als PNG, Pants als RBXM-Modell
        # mit PantsTemplate (beides im echten Lauf so beobachtet)
        13576957688: PNG_BYTES,   # Shirt
        99060033706897: _pants_rbxm(900000021),  # Pants "Formal"
        900000021: PNG_BYTES,
    }


def _install_fakes(monkeypatch, avatar, downloads):
    calls = []

    type_by_id = {a["id"]: a["assetType"]["id"] for a in avatar["assets"]}
    name_by_id = {a["id"]: a["name"] for a in avatar["assets"]}

    def fake_download(client, asset_id):
        calls.append(asset_id)
        payload = downloads.get(asset_id)
        if payload is None:
            return roblox_api.DownloadedAsset(asset_id, None, "", "", "fake: 404")
        return roblox_api.DownloadedAsset(
            asset_id, payload, "application/octet-stream", "fake", "")

    monkeypatch.setattr(roblox_api, "resolve_user_id",
                        lambda client, u: (USER_ID, "MertaStudios"))
    monkeypatch.setattr(roblox_api, "fetch_avatar",
                        lambda client, uid: dict(avatar, _source="fixture"))
    monkeypatch.setattr(roblox_api, "fetch_asset_details",
                        lambda client, aid: {"assetTypeId": type_by_id.get(aid, 0),
                                             "name": name_by_id.get(aid, "")})
    monkeypatch.setattr(roblox_api, "download_asset", fake_download)
    return calls


@pytest.fixture
def merta_avatar():
    path = os.path.join(HERE, "fixtures", "merta_avatar.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_merta_avatar_keine_uebersprungen(merta_avatar, monkeypatch, tmp_path):
    calls = _install_fakes(monkeypatch, merta_avatar, _merta_downloads())

    manifest = build_avatar.build("MertaStudios", str(tmp_path), verbose=False)
    stats = manifest["stats"]

    assert manifest["user_id"] == USER_ID
    # Das war der Bug: frueher "16 uebersprungen, 0 Accessoires"
    assert manifest["skipped"] == []
    assert stats["skipped"] == 0
    assert stats["failed"] == 0
    assert stats["accessories"] == 3            # Green, White Void Crown, Badge
    assert stats["body_parts_from_mesh"] == 5   # Torso, 2 Arme, 2 Beine
    assert stats["ignored_animations"] == 7
    assert stats["textures"] == 2               # Shirt + Pants

    # Animationen werden gar nicht erst heruntergeladen
    assert not (set(calls) & ANIMATION_IDS)

    names = [p["name"] for p in manifest["parts"]]
    # Headless: kein Kopf im Modell (DynamicHead ohne Geometrie)
    assert "kopf" not in names
    assert "kopf_mesh" not in names
    assert any("Headless/DynamicHead erkannt" in w for w in manifest["warnings"])

    # Alle fuenf Koerperteile sind echte Meshes mit Kleidungs-Texturen
    by_name = {p["name"]: p for p in manifest["parts"]}
    for part_name in ("torso", "rechts_arm", "links_arm", "rechts_bein", "links_bein"):
        part = by_name[part_name]
        assert part["kind"] == "body"
        assert "Echtes Mesh aus RBXM" in (part.get("note") or "")
        assert (tmp_path / part["obj"]).stat().st_size > 50
    # Der linke Arm wurde aus seinen zwei R15-Segmenten zusammengefuehrt
    assert "2 R15-Segmente" in (by_name["links_arm"].get("note") or "")
    assert by_name["torso"]["texture"].endswith("13576957688.png")
    # Hosen-Textur wurde aus dem Pants-RBXM (PantsTemplate) nachgeladen
    assert by_name["links_bein"]["texture"].endswith("900000021.png")
    assert by_name["rechts_bein"]["texture"].endswith("900000021.png")

    # 6 Koerper-Teile (ohne Kopf, mit Hals) + 3 Accessoires
    assert stats["parts"] == 9
    accessory_names = " ".join(p["name"] for p in manifest["parts"]
                               if p["kind"] == "accessory")
    assert "Green" in accessory_names
    assert "White_Void_Crown" in accessory_names
    assert "Bricksmith_Badge" in accessory_names


def test_hoehe_deutsche_schreibweise(merta_avatar, monkeypatch, tmp_path):
    _install_fakes(monkeypatch, merta_avatar, _merta_downloads())
    manifest = build_avatar.build("MertaStudios", str(tmp_path), verbose=False)

    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    # Entscheidend ist die Schreibweise: Komma statt Punkt (sonst liest
    # man "5.398" als fuenftausend...). fmt_de(5.496) == "5,50" prueft
    # test_fmt_de zusaetzlich direkt am Beispielwert aus dem Bug-Report.
    hoehe = manifest["metrics"]["height"]
    erwartet = f"{hoehe:.2f}".replace(".", ",")
    assert f"{erwartet} Studs" in summary
    assert "(typisch 5–6)" in summary
    assert manifest["metrics"]["height_de"] == erwartet
    # R15-Koerper (Scales 1.0) ist rund 5,5 Studs hoch - nicht 50 und
    # nicht 0,5 (das war der urspruengliche Formatierungs-Bug).
    assert 5.0 < hoehe < 6.0
    assert "5.398" not in summary
    assert "5.496" not in summary


@pytest.mark.parametrize("wert,nd,erwartet", [
    (5.496, 2, "5,50"),     # Beispiel aus dem Bug-Report ("5000 Studs")
    (5.3976, 2, "5,40"),
    (1, 2, "1,00"),
    (0.049, 2, "0,05"),
    (12.345, 1, "12,3"),
    ("kaputt", 2, "kaputt"),
])
def test_fmt_de(wert, nd, erwartet):
    assert build_avatar.fmt_de(wert, nd) == erwartet


def _obj_max_axis(path):
    lo = [1e18] * 3
    hi = [-1e18] * 3
    for line in path.read_text().splitlines():
        if line.startswith("v "):
            for i, value in enumerate(line.split()[1:4]):
                lo[i] = min(lo[i], float(value))
                hi[i] = max(hi[i], float(value))
    return max(hi[i] - lo[i] for i in range(3))


def test_riesiges_mesh_wird_neu_skaliert(monkeypatch, tmp_path):
    avatar = {
        "playerAvatarType": "R15",
        "scales": dict(roblox_api.DEFAULT_SCALES),
        "bodyColors": dict(roblox_api.DEFAULT_BODY_COLORS),
        "assets": [{"id": 42, "name": "Riesenhut",
                    "assetType": {"id": 8, "name": "Hat"}}],
    }
    downloads = {
        # RBXM behauptet 200 Studs Groesse (Einheiten sind keine Studs)
        42: _rbxm_meshpart("Riesenhut", 42001, size=(200.0, 200.0, 200.0)),
        42001: encode_v2(roblox_mesh.make_box(2.0, 2.0, 2.0)),
    }
    _install_fakes(monkeypatch, avatar, downloads)

    manifest = build_avatar.build("MertaStudios", str(tmp_path), verbose=False)

    hut = next(p for p in manifest["parts"] if p["kind"] == "accessory")
    assert _obj_max_axis(tmp_path / hut["obj"]) <= 12.0
    assert any("begrenzt" in w for w in manifest["warnings"])
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "unplausibel" in summary


def test_rbxm_textur_wird_ans_teil_gehaengt(monkeypatch, tmp_path):
    avatar = {
        "playerAvatarType": "R15",
        "scales": dict(roblox_api.DEFAULT_SCALES),
        "bodyColors": dict(roblox_api.DEFAULT_BODY_COLORS),
        "assets": [{"id": 43, "name": "Textur-Hut",
                    "assetType": {"id": 8, "name": "Hat"}}],
    }
    downloads = {
        43: _rbxm_meshpart("Textur-Hut", 43001, size=(1.5, 1.0, 1.5),
                           texture_id=43002),
        43001: encode_v2(roblox_mesh.make_sphere(1.0, 12, 8)),
        43002: PNG_BYTES,
    }
    _install_fakes(monkeypatch, avatar, downloads)

    manifest = build_avatar.build("MertaStudios", str(tmp_path), verbose=False)

    hut = next(p for p in manifest["parts"] if p["kind"] == "accessory")
    assert hut["texture"] == "textures/43002.png"
    assert (tmp_path / "textures" / "43002.png").exists()
    assert manifest["stats"]["textures"] == 1
    assert manifest["skipped"] == []
