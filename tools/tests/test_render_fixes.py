"""Tests fuer die Render-Fixes (Rig, Kamera, Texturen).

Hintergrund (Bug-Report):
  * Koerperteile waren "leicht verschoben" bzw. verrenkt,
  * der Avatar wurde von hinten gezeigt,
  * die Texturen waren falsch: ein Raster aus dem Kleidungs-Atlas auf
    Beinen und Torso, alles andere grau/weiss.

Die Tests hier sichern die Ursachen ab, ohne Blender und ohne Netzwerk.
"""

from __future__ import annotations

import json
import math
import os
import struct
import sys
import zlib

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)
sys.path.insert(0, HERE)

import avatar_rig  # noqa: E402
import avatar_uv  # noqa: E402
import build_avatar  # noqa: E402
import roblox_api  # noqa: E402
import roblox_mesh  # noqa: E402
import scene_camera  # noqa: E402
from rbxm_factory import make_rbxm  # noqa: E402
from test_build_integration import PNG_BYTES, encode_v2  # noqa: E402

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def png_rgba(width: int, height: int, pixels) -> bytes:
    """Minimales PNG (8 Bit RGBA) aus einer Funktion (x, y) -> (r,g,b,a)."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # Filter "None"
        for x in range(width):
            raw.extend(pixels(x, y))
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b""))


def half_red_half_green_atlas() -> bytes:
    """585x559-Atlas: linke Haelfte rot, rechte Haelfte gruen.

    Damit laesst sich pruefen, ob Panels an der richtigen Stelle gesucht
    werden (Torso-Front links-mittig, Torso-Back rechts).
    """
    def pixel(x, y):
        return (220, 30, 30, 255) if x < avatar_uv.ATLAS_WIDTH // 2 else (30, 190, 60, 255)

    return png_rgba(avatar_uv.ATLAS_WIDTH, avatar_uv.ATLAS_HEIGHT, pixel)


def _uv_of_bbox_face(mesh, axis):
    """UVs der Flaeche, deren Normale in ``axis``-Richtung zeigt."""
    for base in range(0, len(mesh.positions), 4):
        normal = mesh.normals[base]
        if avatar_uv._axis_name(normal) == axis:
            return mesh.uvs[base:base + 4]
    return []


def _meshpart(name, mesh_id, size, pos, texture_id=None):
    props = {"Name": name, "MeshId": f"rbxassetid://{mesh_id}",
             "size": size, "CFrame": pos}
    if texture_id is not None:
        props["TextureID"] = f"rbxassetid://{texture_id}"
    return {"class": "MeshPart", "props": props, "Parent": None}


def _install(monkeypatch, avatar, downloads):
    type_by_id = {a["id"]: a["assetType"]["id"] for a in avatar["assets"]}

    def fake_download(client, asset_id):
        data = downloads.get(asset_id)
        if data is None:
            return roblox_api.DownloadedAsset(asset_id, None, "", "", "fake 404")
        return roblox_api.DownloadedAsset(asset_id, data, "application/octet-stream",
                                          "fake", "")

    monkeypatch.setattr(roblox_api, "resolve_user_id", lambda c, u: (7, "Test"))
    monkeypatch.setattr(roblox_api, "fetch_avatar",
                        lambda c, uid: dict(avatar, _source="fixture"))
    monkeypatch.setattr(roblox_api, "fetch_asset_details",
                        lambda c, aid: {"assetTypeId": type_by_id.get(aid, 0),
                                        "name": ""})
    monkeypatch.setattr(roblox_api, "download_asset", fake_download)


def _base_avatar(assets):
    return {
        "playerAvatarType": "R15",
        "scales": dict(roblox_api.DEFAULT_SCALES),
        "bodyColors": dict(roblox_api.DEFAULT_BODY_COLORS),
        "assets": assets,
    }


# ---------------------------------------------------------------------------
# UV-Atlas (klassische Kleidung)
# ---------------------------------------------------------------------------


def test_atlas_panels_liegen_im_bild():
    for name, rect in avatar_uv.TORSO.items():
        x0, y0, x1, y1 = rect
        assert 0 <= x0 < x1 <= avatar_uv.ATLAS_WIDTH, name
        assert 0 <= y0 < y1 <= avatar_uv.ATLAS_HEIGHT, name
    # Torso-Front sitzt links von Torso-Back (offizielles Template)
    assert avatar_uv.TORSO["front"][0] < avatar_uv.TORSO["back"][0]
    # Arme/Beine teilen sich das Gliedmassen-Layout
    assert avatar_uv.PART_REGIONS["links_bein"] is avatar_uv.LIMB_LEFT
    assert avatar_uv.PART_REGIONS["rechts_arm"] is avatar_uv.LIMB_RIGHT


def test_quader_bekommt_panel_uvs():
    box = roblox_mesh.make_box(2.0, 2.0, 1.0)
    mesh = avatar_uv.apply_box_uv(box, "torso")
    front = _uv_of_bbox_face(mesh, "-z")     # Roblox-Front
    back = _uv_of_bbox_face(mesh, "+z")
    assert len(front) == 4 and len(back) == 4
    x0, y0, x1, y1 = avatar_uv.TORSO["front"]
    u_min = min(u for u, _ in front)
    v_min = min(v for _, v in front)
    u_max = max(u for u, _ in front)
    v_max = max(v for _, v in front)
    # V ist gespiegelt (OBJ-Ursprung unten links)
    assert u_min == pytest.approx((x0 + 1) / 585, abs=0.005)
    assert u_max == pytest.approx((x1 - 1) / 585, abs=0.005)
    assert v_min == pytest.approx(1.0 - (y1 - 1) / 559, abs=0.005)
    assert v_max == pytest.approx(1.0 - (y0 + 1) / 559, abs=0.005)
    # Front und Back duerfen nicht dasselbe Panel sein
    assert min(u for u, _ in front) != pytest.approx(min(u for u, _ in back))


def test_arme_und_beine_nutzen_ihre_eigenen_panels():
    left_leg = avatar_uv.apply_box_uv(roblox_mesh.make_box(1, 2, 1), "links_bein")
    right_leg = avatar_uv.apply_box_uv(roblox_mesh.make_box(1, 2, 1), "rechts_bein")
    u_left = min(u for u, _ in _uv_of_bbox_face(left_leg, "-z"))
    u_right = min(u for u, _ in _uv_of_bbox_face(right_leg, "-z"))
    assert abs(u_left - u_right) > 0.1  # linker/rechter Block im Atlas


def test_atlas_projektion_auf_echtes_mesh():
    """Ein Mesh mit eigenen UVs bekommt Panels statt des ganzen Atlas."""
    sphere = roblox_mesh.make_sphere(0.6, 20, 14)
    projected = avatar_uv.project_atlas_uvs(sphere, "torso")
    assert len(projected.uvs) == len(projected.positions)
    front_region = avatar_uv.TORSO["front"]
    u0, _v0, u1, _v1 = avatar_uv.region_uv(front_region)
    # Kein Vertex darf breiter als sein Panel streuen (vorher 0..1 = Atlas)
    for uv in projected.uvs:
        assert 0.0 <= uv[0] <= 1.0 and 0.0 <= uv[1] <= 1.0
    spread = [uv[0] for uv in projected.uvs]
    assert min(spread) >= min(avatar_uv.region_uv(r)[0] for r in avatar_uv.TORSO.values())
    assert max(spread) <= max(avatar_uv.region_uv(r)[2] for r in avatar_uv.TORSO.values())
    assert u1 > u0


def test_kopf_und_hals_haben_keinen_kleidungsatlas():
    assert not avatar_uv.has_clothing_layout("kopf")
    assert not avatar_uv.has_clothing_layout("hals")
    assert avatar_uv.has_clothing_layout("links_arm")


# ---------------------------------------------------------------------------
# Rig: gestapelt, nichts verschoben
# ---------------------------------------------------------------------------


def test_rig_teile_sitzen_lueckenlos_aufeinander():
    parts, metrics = avatar_rig.build_body(roblox_api.DEFAULT_BODY_COLORS,
                                           roblox_api.DEFAULT_SCALES)
    by_name = {p.name: p for p in parts}
    top = {name: by_name[name].mesh.bbox()[1][1] for name in by_name}
    bottom = {name: by_name[name].mesh.bbox()[0][1] for name in by_name}

    # Beine stehen auf dem Boden, Torso sitzt auf den Beinen (max. 0,1 Luft)
    assert bottom["links_bein"] == pytest.approx(0.0, abs=1e-6)
    assert bottom["rechts_bein"] == pytest.approx(0.0, abs=1e-6)
    # minimale Ueberlappung an der Huefte (dunkle Nahtstellen vermeiden),
    # aber keine Luecke
    assert -0.06 <= bottom["torso"] - top["links_bein"] < 0.1
    # Hals im Torso, Kopf auf dem Hals
    assert bottom["hals"] < top["torso"]
    assert bottom["kopf"] < top["hals"]
    # Arme haengen an den Schultern (Oberkante knapp unter der
    # Torso-Oberkante, wie beim R15-Schultergelenk)
    assert -0.02 <= top["torso"] - top["links_arm"] < 0.2
    assert abs(bottom["links_arm"] - bottom["rechts_arm"]) < 1e-6
    # Linke/rechte Seite ist spiegelbildlich (nichts einseitig verschoben)
    assert by_name["links_arm"].mesh.center()[0] == pytest.approx(
        -by_name["rechts_arm"].mesh.center()[0], abs=1e-6)
    assert by_name["links_bein"].mesh.bbox()[0][0] < 0 < by_name["rechts_bein"].mesh.bbox()[1][0]
    assert metrics.height == pytest.approx(top["kopf"], abs=0.01)


def test_rig_nutzt_echte_teilgroessen():
    """R15-Masse gross/breit muessen den Aufbau bestimmen (kein R6-Quader)."""
    scales = dict(roblox_api.DEFAULT_SCALES)
    sizes = {"torso": (2.4, 2.6, 1.4), "links_bein": (1.2, 2.4, 1.2),
             "rechts_bein": (1.2, 2.4, 1.2), "links_arm": (1.2, 2.6, 1.2),
             "rechts_arm": (1.2, 2.6, 1.2), "kopf": (1.5, 1.5, 1.5)}
    parts, metrics = avatar_rig.build_body(roblox_api.DEFAULT_BODY_COLORS,
                                           scales, sizes)
    by_name = {p.name: p for p in parts}
    size = by_name["torso"].mesh.size()
    assert size[0] == pytest.approx(2.4)
    assert size[1] == pytest.approx(2.6)
    assert metrics.head_radius == pytest.approx(0.75)
    # Torso sitzt passgenau auf den laengeren Beinen
    assert by_name["torso"].mesh.bbox()[0][1] == pytest.approx(2.4, abs=0.05)
    assert by_name["links_bein"].mesh.size()[1] == pytest.approx(2.4)


def test_keine_doppelten_koerper_teile():
    """Ueberlappende Quader erzeugen dunkle Nahtstellen im Glas."""
    parts, _ = avatar_rig.build_body(roblox_api.DEFAULT_BODY_COLORS,
                                     roblox_api.DEFAULT_SCALES)
    by_name = {p.name: p for p in parts}
    torso_lo, torso_hi = by_name["torso"].mesh.bbox()
    for name in ("links_bein", "rechts_bein"):
        lo, hi = by_name[name].mesh.bbox()
        overlap = min(hi[1], torso_hi[1]) - max(lo[1], torso_lo[1])
        assert overlap <= 0.05, name


def test_t_pose_arme_werden_aufgerichtet():
    lying = roblox_mesh.make_box(2.0, 0.9, 0.9)     # Arm zeigt nach X
    placed, note = avatar_rig.upright_limb(lying, "links_arm")
    size = placed.size()
    assert "T-Pose" in note
    assert size[1] > size[0]        # jetzt senkrecht
    assert size[1] == pytest.approx(2.0, abs=0.01)


def test_senkrechter_arm_bleibt_unveraendert():
    upright = roblox_mesh.make_box(0.9, 2.0, 0.9)
    placed, note = avatar_rig.upright_limb(upright, "rechts_arm")
    assert note == ""
    assert placed.size() == pytest.approx((0.9, 2.0, 0.9))


# ---------------------------------------------------------------------------
# Kleidung: Auswahl und Dekals
# ---------------------------------------------------------------------------


def test_hose_landet_auf_beinen_und_torso_nicht_auf_armen(monkeypatch, tmp_path):
    atlas = half_red_half_green_atlas()
    avatar = _base_avatar([
        {"id": 11, "name": "Shirt", "assetType": {"id": 11}},
        {"id": 12, "name": "Pants", "assetType": {"id": 12}},
    ])
    _install(monkeypatch, avatar, {11: atlas, 12: atlas})
    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    by_name = {p["name"]: p for p in manifest["parts"]}
    assert by_name["torso"]["texture"] == "textures/11.png"
    assert by_name["links_bein"]["texture"] == "textures/12.png"
    assert by_name["rechts_bein"]["texture"] == "textures/12.png"
    # Ein klassisches Shirt deckt die Aermel mit ab
    assert by_name["links_arm"]["texture"] == "textures/11.png"
    # Kleidungs-UVs nur auf bekleideten Teilen
    assert by_name["torso"]["uv"] == "classic-atlas"
    assert by_name["links_bein"]["uv"] == "classic-atlas"


def test_teilfarbe_kommt_aus_der_textur(monkeypatch, tmp_path):
    """Ohne Texturfarbe rendert das Glas in Hautfarbe - das war "alles grau"."""
    atlas = half_red_half_green_atlas()
    avatar = _base_avatar([{"id": 11, "name": "Shirt", "assetType": {"id": 11}}])
    _install(monkeypatch, avatar, {11: atlas})
    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    torso = next(p for p in manifest["parts"] if p["name"] == "torso")
    # Grundfarbe = Hautfarbe (fuer durchsichtige Atlas-Stellen), Tint =
    # Durchschnitt der eigenen Atlas-Panels (hier rot).
    r = int(torso["tint"][1:3], 16)
    g = int(torso["tint"][3:5], 16)
    assert r + g > 120          # nicht schwarz/grau
    assert r > g or g > r       # rote oder gruene Durchschnittsfarbe


def test_tshirt_und_gesicht_werden_dekals(monkeypatch, tmp_path):
    avatar = _base_avatar([
        {"id": 20, "name": "TShirt", "assetType": {"id": 2}},
        {"id": 21, "name": "Face", "assetType": {"id": 18}},
    ])
    _install(monkeypatch, avatar, {20: PNG_BYTES, 21: PNG_BYTES})
    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    kinds = {p["name"]: p["kind"] for p in manifest["parts"]}
    assert kinds.get("tshirt_decal") == "decal"
    assert kinds.get("face_decal") == "decal"
    # T-Shirt-Grafik darf NICHT als Koerpertextur missbraucht werden
    torso = next(p for p in manifest["parts"] if p["name"] == "torso")
    assert not torso.get("texture")
    assert (tmp_path / "parts" / "decal_tshirt.obj").exists()


def test_layered_kleidung_wird_zur_huellen(monkeypatch, tmp_path):
    avatar = _base_avatar([
        {"id": 31, "name": "Jacke", "assetType": {"id": 67}},
    ])
    jacket = make_rbxm([_meshpart("Handle", 3101, (0.6, 0.6, 0.6),
                                  (0.0, 3.0, 0.0))])
    _install(monkeypatch, avatar, {
        31: jacket, 3101: encode_v2(roblox_mesh.make_box(0.6, 0.6, 0.6)),
    })
    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    jacket_part = next(p for p in manifest["parts"] if p["kind"] == "accessory")
    assert "Huelle" in (jacket_part.get("note") or "")
    # Die Huelle ist so breit wie der Torso - nicht 1,6 Studs vor der Brust
    torso = next(p for p in manifest["parts"] if p["name"] == "torso")
    def width(part):
        xs = [float(line.split()[1]) for line in
              (tmp_path / part["obj"]).read_text().splitlines()
              if line.startswith("v ")]
        return max(xs) - min(xs)
    assert width(jacket_part) > width(torso) * 0.9


# ---------------------------------------------------------------------------
# R15-Meshes: echte Masse und Atlas-Projektion
# ---------------------------------------------------------------------------


def test_r15_torso_bekommt_atlas_projektion(monkeypatch, tmp_path):
    atlas = half_red_half_green_atlas()
    avatar = _base_avatar([
        {"id": 11, "name": "Shirt", "assetType": {"id": 11}},
        {"id": 27, "name": "Torso", "assetType": {"id": 27}},
    ])
    torso_rbxm = make_rbxm([
        _meshpart("UpperTorso", 2701, (2.0, 2.0, 1.0), (0.0, 3.0, 0.0)),
        _meshpart("LowerTorso", 2702, (2.0, 1.0, 1.0), (0.0, 1.5, 0.0)),
    ])
    downloads = {11: atlas, 27: torso_rbxm,
                 2701: encode_v2(roblox_mesh.make_sphere(1.0, 16, 12)),
                 2702: encode_v2(roblox_mesh.make_sphere(0.8, 16, 12))}
    _install(monkeypatch, avatar, downloads)
    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    torso = next(p for p in manifest["parts"] if p["name"] == "torso")
    assert torso["uv"] == "classic-atlas"
    assert torso["texture"] == "textures/11.png"

    # UVs des OBJ muessen im Torso-Bereich des Atlas liegen (nicht 0..1)
    uvs = []
    for line in (tmp_path / torso["obj"]).read_text().splitlines():
        if line.startswith("vt "):
            uvs.append(float(line.split()[1]))
    panel_min = min(avatar_uv.region_uv(r)[0] for r in avatar_uv.TORSO.values())
    panel_max = max(avatar_uv.region_uv(r)[2] for r in avatar_uv.TORSO.values())
    assert min(uvs) >= panel_min - 1e-6
    assert max(uvs) <= panel_max + 1e-6
    assert max(uvs) - min(uvs) > 0.1


def test_riesige_rbxm_groesse_verkleinert_den_koerper(monkeypatch, tmp_path):
    avatar = _base_avatar([
        {"id": 29, "name": "LeftArm", "assetType": {"id": 29}},
    ])
    arm = make_rbxm([_meshpart("LeftUpperArm", 2901, (30.0, 30.0, 30.0),
                               (0.0, 3.0, 0.0))])
    _install(monkeypatch, avatar, {
        29: arm, 2901: encode_v2(roblox_mesh.make_box(1.0, 2.0, 1.0)),
    })
    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    # Hoehe bleibt im plausiblen Bereich (kein 30-Studs-Arm)
    assert 4.5 < manifest["metrics"]["height"] < 7.0
    assert any("begrenzt" in w or "unplausibel" in w
               for w in manifest["warnings"])


# ---------------------------------------------------------------------------
# Kamera: von vorne, leicht schraeg
# ---------------------------------------------------------------------------


def test_kamera_steht_vor_dem_avatar():
    """OBJ-Import dreht Roblox-Front (-Z) nach Blender-+Y.

    Die Kamera muss also auf +Y stehen - stand sie auf -Y, sah man den
    Ruecken (genau der Bug-Report).
    """
    forward_dir = scene_camera.camera_direction(0.0, 0.0)
    assert forward_dir[1] > 0.99        # +Y = vorne
    default = scene_camera.camera_direction(24.0, 9.0)
    assert default[1] > 0.8             # immer noch klar von vorne
    assert default[0] > 0.2             # leicht schraeg zur Seite
    assert default[2] > 0.0             # leicht von oben


def test_kamera_abstand_passt_zur_bounding_box():
    lo, hi = (-1.7, 0.0, -0.7), (1.7, 5.5, 0.7)
    center, location, distance = scene_camera.frame_distance(lo, hi, 24.0, 9.0)
    assert distance > 5.0
    assert location[1] > center[1]      # Kamera vor dem Modell (+Y)
    # Modellmitte liegt im Bild, Fuss und Kopf sind sichtbar
    for point in ((0.0, 0.0, 0.0), (0.0, 5.5, 0.0)):
        x, y, depth = scene_camera.project(point, location, 1280, 720)
        assert depth > 0
        assert 0.0 <= x <= 1280.0 and 0.0 <= y <= 720.0


def test_render_script_nutzt_front_kamera_und_textur_alpha():
    text = (os.path.join(os.path.dirname(TOOLS), "blender", "render_glass.py"))
    with open(text, encoding="utf-8") as fh:
        source = fh.read()
    # Kamera kommt aus dem getesteten Modul (keine zweite Konvention)
    assert "from scene_camera import frame_distance" in source
    # Textur-Alpha wird mit der Koerperfarbe gemischt (keine schwarzen Raender)
    assert 'tex.outputs["Alpha"]' in source
    # Dekals behalten ihre eigene Alpha und werden nicht glaesern
    assert 'kind == "decal"' in source


# ---------------------------------------------------------------------------
# Software-Vorschau (tools/preview_scene.py)
# ---------------------------------------------------------------------------


def test_preview_scene_rendert_von_vorne(monkeypatch, tmp_path):
    """Die Vorschau nutzt dieselbe Kamera-Konvention wie der Blender-Render."""
    numpy = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    sys.path.insert(0, TOOLS)
    import preview_scene

    atlas = half_red_half_green_atlas()
    avatar = _base_avatar([
        {"id": 11, "name": "Shirt", "assetType": {"id": 11}},
        {"id": 12, "name": "Pants", "assetType": {"id": 12}},
    ])
    _install(monkeypatch, avatar, {11: atlas, 12: atlas})
    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    assert manifest["parts"]

    out = tmp_path / "preview.png"
    info = preview_scene.render(str(tmp_path / "manifest.json"), str(out),
                                resolution="240x200")
    assert out.exists() and out.stat().st_size > 1000
    # Kamera steht vor dem Avatar (+Y in Blender-Koordinaten)
    assert info["camera"][1] > 0
    bbox_min, bbox_max = info["bbox"]
    assert bbox_min[2] == pytest.approx(0.0, abs=1e-6)   # Fuesse auf dem Boden
    assert bbox_max[2] > 4.0                             # Z = oben


def test_mesh_eigene_textur_bleibt_mit_mesh_uvs(monkeypatch, tmp_path):
    """Ein 3D-Bundle mit eigener Textur darf NICHT auf den Atlas gebogen werden."""
    atlas = half_red_half_green_atlas()
    avatar = _base_avatar([
        {"id": 11, "name": "Shirt", "assetType": {"id": 11}},
        {"id": 27, "name": "Torso", "assetType": {"id": 27}},
    ])
    torso_rbxm = make_rbxm([
        _meshpart("UpperTorso", 2701, (2.0, 2.0, 1.0), (0.0, 3.0, 0.0),
                  texture_id=2709),
    ])
    _install(monkeypatch, avatar, {
        11: atlas, 27: torso_rbxm,
        2701: encode_v2(roblox_mesh.make_box(2.0, 2.0, 1.0)),
        2709: PNG_BYTES,
    })
    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    torso = next(p for p in manifest["parts"] if p["name"] == "torso")
    assert torso["texture"] == "textures/2709.png"
    assert torso["uv"] == "mesh"


# ---------------------------------------------------------------------------
# Geometrie-Primitive: Wickelrichtung passend zur Normale
# ---------------------------------------------------------------------------


def _geometric_normal(mesh, face):
    a, b, c = (mesh.positions[i] for i in face)
    u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2],
         u[0] * v[1] - u[1] * v[0])
    length = math.sqrt(sum(x * x for x in n)) or 1.0
    return tuple(x / length for x in n)


@pytest.mark.parametrize("facing", ["-Z", "+Z", "-X", "+X", "+Y", "-Y"])
def test_plane_winding_passt_zur_normale(facing):
    plane = roblox_mesh.make_plane(2.0, 3.0, (0.0, 1.0, 0.0), facing)
    normal = plane.normals[0]
    for face in plane.faces:
        assert sum(_geometric_normal(plane, face)[i] * normal[i]
                   for i in range(3)) > 0.9


def test_sphere_patch_zeigt_nach_aussen():
    patch = roblox_mesh.make_sphere_patch(1.0)
    for face in patch.faces:
        normal = _geometric_normal(patch, face)
        assert sum(normal[i] * patch.normals[face[0]][i] for i in range(3)) > 0.0
    # Patch liegt vorne (-Z) und traegt UVs 0..1
    lo, hi = patch.bbox()
    assert lo[2] < -0.5 and hi[2] < 0.0
    assert min(u for u, *_ in patch.uvs) == pytest.approx(0.0)
    assert max(u for u, *_ in patch.uvs) == pytest.approx(1.0)


def test_sphere_winding_zeigt_nach_aussen():
    sphere = roblox_mesh.make_sphere(1.0, 16, 12)
    for face in sphere.faces:
        assert sum(_geometric_normal(sphere, face)[i]
                   * sphere.normals[face[0]][i] for i in range(3)) > 0.0


def test_proportionen_werden_harmonisiert():
    """Beine/Arme aus RBXM duerfen den Torso nicht ueberragen."""
    sizes = {"torso": (1.0, 2.0, 0.9),
             "links_bein": (0.9, 2.0, 0.9), "rechts_bein": (0.9, 2.0, 0.9),
             "links_arm": (0.9, 3.4, 1.2), "rechts_arm": (0.9, 3.4, 1.2)}
    notes = avatar_rig.harmonize_part_sizes(sizes)
    assert sizes["links_bein"][0] == pytest.approx(0.5, abs=0.02)
    assert sizes["rechts_bein"][0] == pytest.approx(0.5, abs=0.02)
    assert sizes["links_arm"][0] == pytest.approx(0.55, abs=0.02)
    assert sizes["links_arm"][1] == pytest.approx(2.24, abs=0.02)
    assert sizes["links_arm"][2] == pytest.approx(0.99, abs=0.02)
    assert len(notes) >= 3           # jede Korrektur wird begruendet


def test_harmonisierung_laesst_plausible_masse_in_ruhe():
    sizes = {"torso": (2.0, 2.0, 1.0),
             "links_bein": (1.0, 2.0, 1.0), "rechts_bein": (1.0, 2.0, 1.0),
             "links_arm": (1.0, 2.0, 1.0), "rechts_arm": (1.0, 2.0, 1.0)}
    notes = avatar_rig.harmonize_part_sizes(sizes)
    assert notes == []
    assert sizes["links_bein"] == (1.0, 2.0, 1.0)


def test_beine_sind_nicht_breiter_als_der_torso(monkeypatch, tmp_path):
    """Regression: im echten Render waren die Beine breiter als der Torso."""
    avatar = _base_avatar([{"id": 27, "name": "Torso", "assetType": {"id": 27}},
                           {"id": 30, "name": "LeftLeg", "assetType": {"id": 30}}])
    torso = make_rbxm([_meshpart("UpperTorso", 2701, (1.0, 2.0, 0.9),
                                 (0.0, 3.0, 0.0))])
    leg = make_rbxm([_meshpart("LeftUpperLeg", 3001, (1.0, 2.0, 1.0),
                               (0.5, 1.0, 0.0))])
    _install(monkeypatch, avatar, {
        27: torso, 2701: encode_v2(roblox_mesh.make_box(1.0, 2.0, 0.9)),
        30: leg, 3001: encode_v2(roblox_mesh.make_box(1.0, 2.0, 1.0)),
    })
    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    by_name = {p["name"]: p for p in manifest["parts"]}

    def width(name):
        xs = [float(line.split()[1]) for line in
              (tmp_path / by_name[name]["obj"]).read_text().splitlines()
              if line.startswith("v ")]
        return max(xs) - min(xs)

    # beide Beine zusammen bleiben innerhalb der Torso-Breite
    assert width("links_bein") + width("rechts_bein") <= width("torso") * 1.05
    assert any("Proportionen" in w for w in manifest["warnings"])


def test_dunkle_kopf_textur_faellt_auf_koerperfarbe_zurueck(monkeypatch, tmp_path):
    """Eine fast schwarze Karte ist meist Roughness/Normal - kein Farbbild."""
    avatar = _base_avatar([{"id": 79, "name": "Head", "assetType": {"id": 79}}])
    head = make_rbxm([_meshpart("Head", 7901, (1.0, 1.0, 1.0),
                                (0.0, 5.0, 0.0), texture_id=7902)])
    dark_map = png_rgba(32, 32, lambda x, y: (3, 3, 3, 255))
    _install(monkeypatch, avatar, {
        79: head, 7901: encode_v2(roblox_mesh.make_sphere(0.5, 16, 12)),
        7902: dark_map,
    })
    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    kopf = next(p for p in manifest["parts"] if p["name"] == "kopf_mesh")
    assert not kopf.get("texture")
    # Koerperfarbe des Kopfes (hell) statt Schwarz
    assert int(kopf["color"][1:3], 16) > 100
    assert any("Zusatz-Map" in w for w in manifest["warnings"])


def test_helle_kopf_textur_bleibt_erhalten(monkeypatch, tmp_path):
    avatar = _base_avatar([{"id": 79, "name": "Head", "assetType": {"id": 79}}])
    head = make_rbxm([_meshpart("Head", 7901, (1.0, 1.0, 1.0),
                                (0.0, 5.0, 0.0), texture_id=7902)])
    skin = png_rgba(32, 32, lambda x, y: (240, 200, 170, 255))
    _install(monkeypatch, avatar, {
        79: head, 7901: encode_v2(roblox_mesh.make_sphere(0.5, 16, 12)),
        7902: skin,
    })
    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    kopf = next(p for p in manifest["parts"] if p["name"] == "kopf_mesh")
    assert kopf["texture"] == "textures/7902.png"


# ---------------------------------------------------------------------------
# Runde 3: echte Bundle-Masse, gerade Glieder, Accessoire-Groesse/-Farbe
# ---------------------------------------------------------------------------

def test_rbxm_masse_werden_nicht_erneut_skaliert():
    """Bundle-Groessen sind bereits skaliert - Scales nicht doppelt anwenden.

    Vorher wurde der Torso eines Avatars mit width=0,7 zusaetzlich
    gequetscht: 1,33 statt 1,77 Studs breit, Gesamthoehe 4,7 statt 5,5.
    """
    scales = {"width": 0.7, "height": 0.9, "depth": 0.85, "head": 0.95}
    sizes = {"torso": (1.77, 1.85, 0.94), "rechts_arm": (0.75, 1.9, 0.8),
             "links_arm": (0.75, 1.9, 0.8), "rechts_bein": (0.75, 1.8, 0.94),
             "links_bein": (0.75, 1.8, 0.94), "kopf": (1.2, 1.2, 1.2)}
    parts, metrics = avatar_rig.build_body(roblox_api.DEFAULT_BODY_COLORS,
                                           scales, sizes)
    torso = next(p for p in parts if p.name == "torso")
    assert torso.mesh.size()[0] == pytest.approx(1.77, abs=0.01)
    assert metrics.head_radius == pytest.approx(0.6, abs=0.01)
    # Kein Schrumpfen: Beine (1,8) + Torso (1,85) + Kopf (~1,2) = ~5 Studs
    assert 4.8 < metrics.height < 5.2


def test_fehlende_teile_werden_aus_dem_torso_abgeleitet():
    """Nur Torso/Arme aus dem Bundle -> Beine passen zum Torso statt R6."""
    parts, _ = avatar_rig.build_body(
        roblox_api.DEFAULT_BODY_COLORS, roblox_api.DEFAULT_SCALES,
        {"torso": (1.8, 1.9, 0.95), "rechts_arm": (0.8, 1.9, 0.8),
         "links_arm": (0.8, 1.9, 0.8)})
    by_name = {p.name: p for p in parts}
    assert by_name["links_bein"].mesh.size()[0] == pytest.approx(0.9, abs=0.01)


def test_gestapelte_glieder_stehen_gerade():
    """R15-Segmente: senkrecht gestapelt, kein A-Pose-Spagat.

    Vorher wurden die Bind-Pose-X-Positionen uebernommen (Hand 1,48 Studs
    neben der Schulter) und das Ganze dann in die senkrechte Rig-Box
    gepresst - die Arme wirkten verrenkt/zerknittert.
    """
    upper = roblox_mesh.make_box(0.75, 0.9, 0.6)
    lower = roblox_mesh.make_box(0.66, 0.9, 0.6)
    hand = roblox_mesh.make_box(0.55, 0.6, 0.55)
    stacked = roblox_mesh.stack_segments([
        (upper, (0.75, 0.9, 0.6), (0.85, 0.0, 0.0)),
        (lower, (0.66, 0.9, 0.6), (1.20, -0.58, 0.0)),
        (hand, (0.55, 0.6, 0.55), (1.48, -1.14, -0.30)),
    ], target_height=2.3)
    lo, hi = stacked.bbox()
    # alle Segmente liegen uebereinander (keine seitliche Streuung)
    assert hi[0] - lo[0] <= 0.76
    # und der Stapel ist so hoch wie die Bind-Pose
    assert (hi[1] - lo[1]) == pytest.approx(2.3, abs=0.02)
    # Oberkante = Schulter, Unterkante = Hand
    assert hi[1] == pytest.approx(0.0, abs=0.02)


def test_bundle_positionen_ergeben_keinen_Spagat(monkeypatch, tmp_path):
    avatar = _base_avatar([{"id": 28, "name": "RightArm", "assetType": {"id": 28}}])
    arm = make_rbxm([
        _meshpart("RightUpperArm", 2801, (0.75, 0.9, 0.6), (0.85, 0.0, 0.0)),
        _meshpart("RightLowerArm", 2802, (0.66, 0.9, 0.6), (1.20, -0.58, 0.0)),
        _meshpart("RightHand", 2803, (0.55, 0.6, 0.55), (1.48, -1.14, -0.3)),
    ])
    _install(monkeypatch, avatar, {
        28: arm,
        2801: encode_v2(roblox_mesh.make_box(0.75, 0.9, 0.6)),
        2802: encode_v2(roblox_mesh.make_box(0.66, 0.9, 0.6)),
        2803: encode_v2(roblox_mesh.make_box(0.55, 0.6, 0.55)),
    })
    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    arm_part = next(p for p in manifest["parts"] if p["name"] == "rechts_arm")
    xs, ys = [], []
    for line in (tmp_path / arm_part["obj"]).read_text().splitlines():
        if line.startswith("v "):
            xs.append(float(line.split()[1]))
            ys.append(float(line.split()[2]))
    assert max(xs) - min(xs) <= 0.8      # senkrecht, nicht schraeg
    assert max(ys) - min(ys) > 1.5       # volle Arm-Laenge
    assert "gestapelt" in arm_part["note"]


def test_teilfarbe_kommt_aus_den_eigenen_atlas_panels():
    """Das weisse Template darf die Kleidungsfarbe nicht nach grau ziehen."""
    def px(x, y):
        # Alle Panels dunkelblau, der Rest des Atlas weiss (Template)
        for rect in list(avatar_uv.TORSO.values()):
            if rect[0] <= x < rect[2] and rect[1] <= y < rect[3]:
                return (20, 40, 90, 255)
        return (255, 255, 255, 255)
    atlas = png_rgba(585, 559, px)
    path = _write(tmp_path_png(atlas), atlas)
    whole = build_avatar.texture_average_color(path, "#ffffff")
    panels = build_avatar.panel_average_color(path, "torso", "#ffffff")
    assert int(whole[1:3], 16) > 150          # weiss-grau (falsch)
    assert int(panels[1:3], 16) < 60          # dunkelblau (richtig)
    assert int(panels[5:7], 16) > 70


def tmp_path_png(data: bytes):
    import tempfile
    fd, name = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    with open(name, "wb") as fh:
        fh.write(data)
    return name


def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def test_transparentes_gesicht_ist_keine_zusatz_map(monkeypatch, tmp_path):
    """Gesichter sind durchsichtige PNGs mit schwarzen Augen."""
    avatar = _base_avatar([{"id": 79, "name": "Head", "assetType": {"id": 79}}])
    head = make_rbxm([_meshpart("Head", 7901, (1.0, 1.0, 1.0),
                                (0.0, 5.0, 0.0), texture_id=7902)])
    face = png_rgba(64, 64, lambda x, y: (0, 0, 0, 255)
                    if 28 <= y <= 34 and (26 <= x <= 32 or 40 <= x <= 46)
                    else (0, 0, 0, 0))
    _install(monkeypatch, avatar, {
        79: head, 7901: encode_v2(roblox_mesh.make_sphere(0.5, 16, 12)),
        7902: face,
    })
    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    kopf = next(p for p in manifest["parts"] if p["name"] == "kopf_mesh")
    assert kopf.get("texture")
    assert not any("Zusatz-Map" in w for w in manifest["warnings"])


def test_handschuh_paar_wird_an_beide_haende_gesetzt(monkeypatch, tmp_path):
    avatar = _base_avatar([{"id": 67, "name": "Gloves",
                            "assetType": {"id": 67}}])
    pair = make_rbxm([_meshpart("Handle", 6701, (3.5, 0.55, 0.6), (0.0, 0.0, 0.0))])
    _install(monkeypatch, avatar, {
        67: pair,
        6701: encode_v2(roblox_mesh.merge_meshes([
            (roblox_mesh.make_box(0.5, 0.55, 0.6),
             (0.5, 0.55, 0.6), (1.5, 0.0, 0.0)),
            (roblox_mesh.make_box(0.5, 0.55, 0.6),
             (0.5, 0.55, 0.6), (-1.5, 0.0, 0.0)),
        ])),
    })
    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    gloves = [p for p in manifest["parts"] if p["kind"] == "accessory"]
    assert len(gloves) == 2
    xs = []
    for part in gloves:
        for line in (tmp_path / part["obj"]).read_text().splitlines():
            if line.startswith("v "):
                xs.append(float(line.split()[1]))
    assert min(xs) < -1.0 and max(xs) > 1.0        # links und rechts
    assert not any(-0.6 < x < 0.6 for x in xs)     # nicht mehr quer im Koerper


def test_accessoire_farbe_kommt_aus_der_textur(monkeypatch, tmp_path):
    avatar = _base_avatar([{"id": 8, "name": "Hut", "assetType": {"id": 8}}])
    hat = make_rbxm([_meshpart("Handle", 8001, (1.0, 1.0, 1.0),
                              (0.0, 5.0, 0.0), texture_id=8002)])
    red = png_rgba(16, 16, lambda x, y: (180, 20, 30, 255))
    _install(monkeypatch, avatar, {
        8: hat, 8001: encode_v2(roblox_mesh.make_box(1.2, 0.5, 1.2)),
        8002: red,
    })
    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    hut = next(p for p in manifest["parts"] if p["kind"] == "accessory")
    assert int(hut["color"][1:3], 16) > 100        # rot, nicht hellblau
    assert hut["color"] != "#dff4ff"


def test_layered_jacke_umfasst_auch_die_arme(monkeypatch, tmp_path):
    avatar = _base_avatar([{"id": 67, "name": "Jacke",
                            "assetType": {"id": 67}}])
    jacket = make_rbxm([_meshpart("Handle", 6701, (1.0, 1.0, 1.0), (0.0, 0.0, 0.0))])
    _install(monkeypatch, avatar, {
        67: jacket, 6701: encode_v2(roblox_mesh.make_box(3.0, 2.0, 1.0)),
    })
    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    size = manifest["metrics"]
    xs = [float(line.split()[1]) for line in
          (tmp_path / next(p for p in manifest["parts"]
                           if p["kind"] == "accessory")["obj"]).read_text().splitlines()
          if line.startswith("v ")]
    # Huelle deckt Torso + Aermel ab, bleibt aber innerhalb der Schultern
    assert max(xs) - min(xs) == pytest.approx(
        size["torso_width"] + 2 * 0.5 * 1.0 * 1.95, abs=0.2)


def test_rock_haengt_an_der_taillie(monkeypatch, tmp_path):
    avatar = _base_avatar([{"id": 47, "name": "Rock", "assetType": {"id": 47}}])
    skirt = make_rbxm([_meshpart("Handle", 4701, (1.0, 1.0, 1.0), (0.0, 0.0, 0.0))])
    _install(monkeypatch, avatar, {
        47: skirt, 4701: encode_v2(roblox_mesh.make_box(3.4, 2.4, 3.8)),
    })
    manifest = build_avatar.build("Test", str(tmp_path), verbose=False)
    part = next(p for p in manifest["parts"] if p["kind"] == "accessory")
    ys = [float(line.split()[2]) for line in (tmp_path / part["obj"]).read_text().splitlines()
          if line.startswith("v ")]
    torso_top = 4.0  # Torso-Oberkante des Standard-Rigs (~4,0)
    assert max(ys) < torso_top        # kein Umhang bis zur Brust
    assert min(ys) < 1.5              # Rock reicht bis zu den Beinen
