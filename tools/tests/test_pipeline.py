"""Tests fuer die Roblox-Avatar-Render-Pipeline.

Nur Standardbibliothek + pytest, ohne Netzwerk:
    python3 -m pytest tools/tests -q
"""

from __future__ import annotations

import json
import os
import pathlib
import struct
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
ROOT = pathlib.Path(os.path.dirname(TOOLS))
sys.path.insert(0, TOOLS)

import avatar_rig  # noqa: E402
import build_avatar  # noqa: E402
import roblox_api  # noqa: E402
import roblox_mesh  # noqa: E402


# ---------------------------------------------------------------------------
# Hilfsfunktionen zum Bauen synthetischer Mesh-Dateien
# ---------------------------------------------------------------------------

TRIANGLES = [
    # (3x Position, 3x Normale, 3x UV)
    (
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
        ((0.0, 0.0, -1.0), (0.0, 0.0, -1.0), (0.0, 0.0, -1.0)),
        ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
    ),
]


def _flatten(tris):
    positions, normals, uvs = [], [], []
    for pos, nrm, uv in tris:
        for p in pos:
            positions.extend(p)
        for n in nrm:
            normals.extend(n)
        for t in uv:
            uvs.extend(t)
    return positions, normals, uvs


def build_v2(tris=TRIANGLES, with_color=True) -> bytes:
    positions, normals, uvs = _flatten(tris)
    count = len(positions) // 3
    stride = 40 if with_color else 36
    out = bytearray(b"version 2.00\n")
    out += struct.pack("<HBBII", 12, stride, 12, count, len(tris))
    for i in range(count):
        out += struct.pack("<8f", positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2],
                           normals[i * 3], normals[i * 3 + 1], normals[i * 3 + 2],
                           uvs[i * 2], uvs[i * 2 + 1])
        out += struct.pack("<4b", 0, 0, 127, 127)   # Tangent (immer vorhanden)
        if with_color:
            out += struct.pack("<4B", 200, 100, 50, 255)  # RGBA
    for i in range(len(tris)):
        out += struct.pack("<3I", i * 3, i * 3 + 1, i * 3 + 2)
    return bytes(out)


def build_v3(tris=TRIANGLES, lods=(0, 1), extra_lod_faces=1) -> bytes:
    """v3 mit LOD-Offsets: das Haupt-Mesh hat nur die ersten `lods[1]` Faces."""
    positions, normals, uvs = _flatten(tris)
    count = len(positions) // 3
    total_faces = len(tris) + extra_lod_faces
    out = bytearray(b"version 3.00\n")
    out += struct.pack("<HBBHHII", 16, 36, 12, 4, len(lods), count, total_faces)
    for i in range(count):
        out += struct.pack("<8f", positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2],
                           normals[i * 3], normals[i * 3 + 1], normals[i * 3 + 2],
                           uvs[i * 2], uvs[i * 2 + 1])
        out += struct.pack("<4b", 0, 0, 127, 127)
    for i in range(total_faces):
        out += struct.pack("<3I", 0, 1, 2)
    for offset in lods:
        out += struct.pack("<I", offset)
    return bytes(out)


def build_v4(tris=TRIANGLES, version=b"version 4.00\n", with_bones=False) -> bytes:
    positions, normals, uvs = _flatten(tris)
    count = len(positions) // 3
    bones = 2 if with_bones else 0
    out = bytearray(version)
    out += struct.pack("<HHIIHHIHBB", 24, 0, count, len(tris), 0, bones, 0, 0, 0, 0)
    for i in range(count):
        out += struct.pack("<8f", positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2],
                           normals[i * 3], normals[i * 3 + 1], normals[i * 3 + 2],
                           uvs[i * 2], uvs[i * 2 + 1])
        out += struct.pack("<4b", 0, 0, 0, 127)
        out += struct.pack("<4B", 255, 255, 255, 255)
    if with_bones:
        for _ in range(count):
            out += bytes([0, 0, 0xFF, 0xFF]) + bytes([255, 0, 0, 0])
    for i in range(len(tris)):
        out += struct.pack("<3I", i * 3, i * 3 + 1, i * 3 + 2)
    return bytes(out)


def build_v5(tris=TRIANGLES) -> bytes:
    positions, normals, uvs = _flatten(tris)
    count = len(positions) // 3
    out = bytearray(b"version 5.00\n")
    out += struct.pack("<HHIIHHIHBBII", 32, 0, count, len(tris), 0, 0, 0, 0, 0, 0, 1, 0)
    for i in range(count):
        out += struct.pack("<8f", positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2],
                           normals[i * 3], normals[i * 3 + 1], normals[i * 3 + 2],
                           uvs[i * 2], uvs[i * 2 + 1])
        out += struct.pack("<4b", 0, 0, 127, 127)
    for i in range(len(tris)):
        out += struct.pack("<3I", i * 3, i * 3 + 1, i * 3 + 2)
    return bytes(out)


def build_v1(num_faces=1, scale_values=True) -> bytes:
    f = 0.5 if scale_values else 1.0
    lines = ["version 1.00" if scale_values else "version 1.01", str(num_faces)]
    for _ in range(num_faces):
        for vertex in ((0, 0, 0), (1, 0, 0), (0, 1, 0)):
            lines.append("[%s][0,0,-1][0.25,0.75,0]" % ",".join(str(c) for c in vertex))
    _ = f
    return ("\n".join(lines) + "\n").encode("ascii")


# ---------------------------------------------------------------------------
# Mesh-Parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("builder,expected_version", [
    (build_v2, "2.00"),
    (build_v3, "3.00"),
    (build_v4, "4.00"),
    (build_v5, "5.00"),
    (build_v1, "1.00"),
])
def test_parse_alle_versionen(builder, expected_version):
    mesh = roblox_mesh.parse(builder())
    assert mesh.source_version == expected_version
    assert len(mesh.positions) == 3
    assert len(mesh.faces) == 1
    assert not mesh.is_empty


def test_parse_positionen_und_flip_der_uvs():
    mesh = roblox_mesh.parse(build_v2())
    assert mesh.positions[0] == pytest.approx((0.0, 0.0, 0.0))
    assert mesh.positions[1] == pytest.approx((1.0, 0.0, 0.0))
    # Roblox-UV (0.25) wird zu 1-0.25 = 0.75
    assert mesh.uvs[0] == pytest.approx((0.0, 1.0))
    assert mesh.uvs[1] == pytest.approx((1.0, 1.0))


def test_parse_vertex_farben_stride_40():
    mesh = roblox_mesh.parse(build_v2(with_color=True))
    assert mesh.colors is not None
    assert mesh.colors[0] == (200, 100, 50, 255)


def test_parse_stride_36_ohne_farben():
    mesh = roblox_mesh.parse(build_v2(with_color=False))
    assert len(mesh.positions) == 3
    assert mesh.colors == [(255, 255, 255, 255)] * 3


def test_v3_schneidet_lods_ab():
    # 1 Haupt-Face + 1 zusaetzliches LOD-Face
    mesh = roblox_mesh.parse(build_v3())
    assert len(mesh.faces) == 1


def test_v4_mit_bones_und_skinning():
    mesh = roblox_mesh.parse(build_v4(with_bones=True))
    assert len(mesh.positions) == 3
    assert len(mesh.faces) == 1


def test_v7_ohne_chunks_wird_sauber_abgelehnt():
    # FileMesh 7.00 ist ein Chunk-Container (COREMESH v2, Draco) und
    # NICHT v4-kompatibel - die alte Annahme verursachte im echten Lauf
    # "Datei zu kurz fuer die angegebene Vertex-Anzahl" beim Badge.
    # Ein v4-Koerper mit 7.00-Zeile muss sauber scheitern:
    with pytest.raises(roblox_mesh.MeshParseError):
        roblox_mesh.parse(build_v4(version=b"version 7.00\n"))


def test_kaputte_datei_wirft_fehler():
    with pytest.raises(roblox_mesh.MeshParseError):
        roblox_mesh.parse(b"kein mesh")
    with pytest.raises(roblox_mesh.MeshParseError):
        roblox_mesh.parse(b"version 2.00\n" + b"\x00" * 10)


def test_write_obj_erzeugt_gueltige_datei(tmp_path):
    mesh = roblox_mesh.parse(build_v2())
    target = tmp_path / "out.obj"
    mesh.write_obj(str(target))
    text = target.read_text()
    assert "v 0.000000 0.000000 0.000000" in text
    assert "vn " in text and "vt " in text
    assert text.strip().splitlines()[-1].startswith("f ")


def test_sniff_version_und_is_filemesh():
    assert roblox_mesh.sniff_version(build_v2()) == "2.00"
    assert roblox_mesh.is_filemesh(build_v1())
    assert not roblox_mesh.is_filemesh(b"\x89PNG\r\n\x1a\n")


# ---------------------------------------------------------------------------
# Farben
# ---------------------------------------------------------------------------


def test_brickcolor_tabelle():
    colors = roblox_api.brick_colors()
    assert len(colors) > 150
    assert roblox_api.brick_color_hex(194) == "#a3a2a5"   # Medium stone grey
    assert roblox_api.brick_color_hex(24) == "#f5cd30"    # Bright yellow
    assert roblox_api.brick_color_hex("1") == "#f2f3f3"   # White


def test_unbekannte_farbe_ist_deterministisch():
    a = roblox_api.brick_color_hex(99999)
    b = roblox_api.brick_color_hex(99999)
    assert a == b and a.startswith("#") and len(a) == 7


def test_hex_zu_rgb():
    assert roblox_api.hex_to_rgb("#ffffff") == pytest.approx((1.0, 1.0, 1.0))
    assert roblox_api.hex_to_rgb("#000") == pytest.approx((0.0, 0.0, 0.0))


# ---------------------------------------------------------------------------
# Username-Handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("Builderman", "Builderman"),
    ("  builderman\n", "builderman"),
    ("\ufeffNoob123\r\n", "Noob123"),
    ("https://www.roblox.com/users/12345/profile", "12345"),
])
def test_username_normalisieren(raw, expected):
    assert roblox_api.normalize_username(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "ab", "user name", "bad!char", "x" * 21])
def test_ungueltige_usernamen(raw):
    with pytest.raises(roblox_api.RobloxError):
        roblox_api.normalize_username(raw)


# ---------------------------------------------------------------------------
# Rig
# ---------------------------------------------------------------------------


def test_koerper_hat_alle_teile_und_steht_auf_dem_boden():
    parts, metrics = avatar_rig.build_body(roblox_api.DEFAULT_BODY_COLORS,
                                           roblox_api.DEFAULT_SCALES)
    names = {p.name for p in parts}
    assert {"kopf", "torso", "hals", "rechts_arm", "links_arm",
            "rechts_bein", "links_bein"} <= names
    lo, hi = 1e9, -1e9
    for part in parts:
        for _, y, _ in part.mesh.positions:
            lo = min(lo, y)
            hi = max(hi, y)
    assert lo == pytest.approx(0.0, abs=1e-6)
    assert 3.0 < hi < 8.0
    assert metrics.head_center_y > metrics.shoulder_y


def test_scales_veraendern_die_groesse():
    _, klein = avatar_rig.build_body(roblox_api.DEFAULT_BODY_COLORS,
                                     {"height": 0.8, "width": 0.8, "head": 0.8})
    _, gross = avatar_rig.build_body(roblox_api.DEFAULT_BODY_COLORS,
                                     {"height": 1.2, "width": 1.2, "head": 1.2})
    assert gross.height > klein.height


def test_kaputte_scales_werden_abgefangen():
    _, metrics = avatar_rig.build_body({"headColorId": "quatsch"},
                                       {"height": "abc", "width": None, "head": float("nan")})
    assert 3.0 < metrics.height < 8.0


def test_accessoire_wird_auf_den_kopf_gesetzt():
    _, metrics = avatar_rig.build_body(roblox_api.DEFAULT_BODY_COLORS,
                                       roblox_api.DEFAULT_SCALES)
    hat = roblox_mesh.make_sphere(3.0, 12, 8)   # 3 Studs Durchmesser
    placed, note = avatar_rig.place_accessory(hat, 8, metrics)
    lo, hi = placed.bbox()
    # Der Hut liegt oberhalb der Kopfmitte, ueberlappt sie aber leicht
    assert hi[1] > metrics.head_center_y + metrics.head_radius
    assert lo[1] < hi[1]
    assert hi[1] - lo[1] <= 2.5
    assert "Anker 'head_top'" in note


def test_accessoire_unbekannter_typ_bekommt_fallback():
    _, metrics = avatar_rig.build_body(roblox_api.DEFAULT_BODY_COLORS,
                                       roblox_api.DEFAULT_SCALES)
    placed, note = avatar_rig.place_accessory(roblox_mesh.make_box(1, 1, 1), 9999, metrics)
    assert len(placed.faces) == 12
    assert "Anker" in note


def test_accessoire_rbxm_groesse_form_erhaltend():
    """Eine flache Krone (3x0,7x2) darf nicht zur Kugel verzerrt werden:
    gleichmaessige Skalierung, Form bleibt erhalten."""
    _, metrics = avatar_rig.build_body(roblox_api.DEFAULT_BODY_COLORS,
                                       roblox_api.DEFAULT_SCALES)
    krone = roblox_mesh.make_box(3.0, 0.7, 2.0)
    placed, note = avatar_rig.place_accessory(
        krone, 8, metrics, size_hint=(1.75, 1.75, 1.75))
    size = placed.size()
    assert max(size) == pytest.approx(1.75)
    assert size[0] / size[2] == pytest.approx(1.5, abs=0.02)  # Proportion!
    assert size[1] == pytest.approx(0.7 / 3.0 * 1.75, abs=0.02)
    assert "form-erhaltend" in note


def test_accessoire_unplausible_rbxm_groesse_wird_ignoriert():
    """RBXM-Platzhalter 1x1x1 bei einem 3 Studs breiten Hut: frueher wurde
    der Hut auf 1 Stud geschrumpft (verschwand im Kopf). Jetzt gilt die
    Zielgroesse der Accessoire-Art (Hut: 1,75 Studs)."""
    _, metrics = avatar_rig.build_body(roblox_api.DEFAULT_BODY_COLORS,
                                       roblox_api.DEFAULT_SCALES)
    hut = roblox_mesh.make_box(3.0, 2.0, 0.7)
    placed, note = avatar_rig.place_accessory(hut, 8, metrics,
                                              size_hint=(1.0, 1.0, 1.0))
    assert max(placed.size()) == pytest.approx(1.75)
    assert "unplausibel" in note
    # Form bleibt erhalten (3 : 2 : 0,7)
    size = placed.size()
    assert size[0] / size[1] == pytest.approx(1.5, abs=0.02)


# ---------------------------------------------------------------------------
# Headless-Erkennung (DynamicHead)
# ---------------------------------------------------------------------------


def test_headless_normaler_kopf_bleibt():
    head = roblox_mesh.make_sphere(0.6, 12, 8)
    ist, grund, info = build_avatar.head_geometry_info(
        head, (1.2, 1.2, 1.2))
    assert ist is False
    assert "Faces" in info


def test_headless_winzige_part_groesse():
    # So versteckt das echte Asset "Headless Head" den Kopf: normales
    # Mesh, aber Part-Groesse im RBXM ist 0,01 Studs.
    head = roblox_mesh.make_sphere(0.6, 12, 8)
    ist, grund, _ = build_avatar.head_geometry_info(
        head, (0.01, 0.01, 0.01))
    assert ist is True
    assert "Part-Groesse" in grund


def test_headless_winzige_geometrie_und_wenige_faces():
    winzig = roblox_mesh.make_sphere(0.001, 8, 6)
    ist, _, _ = build_avatar.head_geometry_info(winzig, None)
    assert ist is True
    box = roblox_mesh.make_box(1.2, 1.2, 1.2)  # nur 12 Faces
    ist2, grund2, _ = build_avatar.head_geometry_info(box, (1.2, 1.2, 1.2))
    assert ist2 is True
    assert "Geometrie" in grund2


def test_headless_punkt_geometrie_in_grosser_bbox():
    positions = [(0.0, 0.0, 0.0)] * 63 + [(10.0, 0.0, 0.0)]
    faces = [(3 * i, 3 * i + 1, 3 * i + 2) for i in range(21)]
    speck = roblox_mesh.MeshData(positions, [], [], faces, None, "test")
    ist, grund, _ = build_avatar.head_geometry_info(speck, (1.0, 1.0, 1.0))
    assert ist is True
    assert "BBox" in grund


def test_headless_geometrie_ausserhalb_ursprung_versteckt():
    head = roblox_mesh.make_sphere(0.6, 12, 8).transform(1.0, (100.0, 0, 0))
    ist, grund, _ = build_avatar.head_geometry_info(head, (1.2, 1.2, 1.2))
    assert ist is True
    assert "Ursprung" in grund


# ---------------------------------------------------------------------------
# Asset-Erkennung
# ---------------------------------------------------------------------------


def test_klassifizierung():
    assert roblox_api.classify_asset(build_v2()) == "mesh"
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    assert roblox_api.classify_asset(png) == "texture"
    assert roblox_api.classify_asset(b"<roblox!\x89\xff\r\n\x1a\n" + b"\x00" * 16) == "rbxm"
    assert roblox_api.classify_asset(b"\x00\x01\x02", declared_type_id=1) == "texture"
    assert roblox_api.classify_asset(b"\x00\x01\x02", declared_type_id=4) == "mesh"


def test_maybe_decompress_gzip_und_zlib():
    import gzip as gz
    import zlib

    payload = b"<roblox!\x89\xff\r\n\x1a\n" + b"\x00" * 64
    assert roblox_api.maybe_decompress(payload) == payload
    assert roblox_api.maybe_decompress(gz.compress(payload)) == payload
    assert roblox_api.maybe_decompress(zlib.compress(payload)) == payload
    # Kaputtes gzip bleibt unveraendert (kein Absturz)
    kaputt = b"\x1f\x8b" + b"\xff" * 16
    assert roblox_api.maybe_decompress(kaputt) == kaputt


def test_download_asset_entpackt_gzip():
    # Die Asset Delivery liefert RBXM-Modelle oft gzip-gepackt aus -
    # download_asset muss sie transparent entpacken (echter Lauf:
    # sonst "Mesh nicht lesbar" fuer alle RBXM-Assets).
    import gzip as gz

    payload = b"<roblox!\x89\xff\r\n\x1a\n" + b"\x00" * 64

    class FakeClient:
        api_key = ""

        def get(self, url, headers=None, allow_status=()):
            return roblox_api.Response(200, gz.compress(payload), {}, url)

    result = roblox_api.download_asset(FakeClient(), 27112025)
    assert result.data == payload
    assert roblox_api.classify_asset(result.data, 27) == "rbxm"


# ---------------------------------------------------------------------------
# Kompletter Pipeline-Lauf ohne Netzwerk
# ---------------------------------------------------------------------------


def test_dry_run_erzeugt_manifest_und_obj_dateien(tmp_path):
    out = tmp_path / "build"
    result = subprocess.run(
        [sys.executable, os.path.join(TOOLS, "build_avatar.py"),
         "--dry-run", "--out", str(out), "--quiet"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["dry_run"] is True
    assert manifest["stats"]["parts"] >= 7
    assert (out / "summary.md").exists()
    assert (out / "avatar.json").exists()

    for part in manifest["parts"]:
        obj_path = out / part["obj"]
        assert obj_path.exists(), obj_path
        text = obj_path.read_text()
        assert text.count("v ") > 0
        assert "f " in text

    lo, hi = manifest["bbox"]["min"], manifest["bbox"]["max"]
    assert hi[1] > lo[1]  # Modell hat Hoehe


def test_manifest_ohne_username_scheitert_sauber(tmp_path):
    empty = tmp_path / "username.txt"
    empty.write_text("\n")
    result = subprocess.run(
        [sys.executable, os.path.join(TOOLS, "build_avatar.py"),
         "--username-file", str(empty), "--out", str(tmp_path / "b")],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2
    assert "username.txt" in result.stderr


# ---------------------------------------------------------------------------
# Repository-Dateien
# ---------------------------------------------------------------------------


def test_wichtige_dateien_existieren():
    for rel in ("username.txt", ".github/workflows/avatar-render.yml",
                ".github/workflows/auto-merge.yml", "blender/render_glass.py",
                "tools/build_avatar.py", "tools/roblox_api.py",
                "tools/roblox_mesh.py", "tools/avatar_rig.py", "tools/diagnose.py",
                ".github/workflows/diagnose.yml", "AGENTS.md", "ANLEITUNG.md"):
        assert (ROOT / rel).exists(), rel


def test_username_txt_ist_gueltig():
    content = (ROOT / "username.txt").read_text(encoding="utf-8-sig")
    assert roblox_api.normalize_username(content)


def test_workflows_sind_gueltiges_yaml():
    yaml = pytest.importorskip("yaml")
    for name in ("avatar-render.yml", "auto-merge.yml", "tests.yml", "diagnose.yml"):
        path = ROOT / ".github/workflows" / name
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        assert isinstance(data, dict)
        # PyYAML (YAML 1.1) liest das Schluesselwort "on" als True
        triggers = data.get("on", data.get(True))
        assert triggers is not None, f"{name}: Trigger fehlt"
        assert "jobs" in data and data["jobs"]
        for job in data["jobs"].values():
            assert "runs-on" in job and "steps" in job


def test_auto_merge_workflow_merged_jeden_branch_ohne_pull_request():
    # Arena-Sessions pushen auf arena/<session-id>-...; frueher reagierte der
    # Workflow nur auf agent-changes und hat im Fehlerfall einen PR erstellt.
    yaml = pytest.importorskip("yaml")
    path = ROOT / ".github/workflows/auto-merge.yml"
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    triggers = data.get("on", data.get(True))

    # Jeder Branch loest den Merge aus ...
    assert "**" in triggers["push"]["branches"]
    # ... ausser den Ziel-/Sonderbranches (kein Selbst-Merge, keine Schleife).
    assert "!main" in triggers["push"]["branches"]
    assert "!renders" in triggers["push"]["branches"]

    # Kein Pull-Request-Trigger und keine PR-Kommandos mehr.
    assert "pull_request" not in triggers
    assert "gh pr create" not in text
    assert "gh pr merge" not in text

    # Quelle ist der tatsaechlich gepushte Branch, kein fest verdrahteter Name.
    source = data["env"]["SOURCE_BRANCH"]
    assert "github.ref_name" in source
    assert "agent-changes" not in source

    # Einziger Weg nach main: direkter Push.
    assert "HEAD:${TARGET_BRANCH}" in text


def test_agents_datei_verbietet_pull_requests():
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "pull request" in lowered
    assert "nie" in lowered
    assert "auto-merge.yml" in text


def test_tests_workflow_laeuft_auf_jedem_arbeitsbranch():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    triggers = data.get("on", data.get(True))
    push = triggers["push"]
    # Alle Branches ausser dem Render-Ausgabebranch.
    assert push.get("branches-ignore") == ["renders"]
    assert "branches" not in push


def test_render_workflow_hat_artifact_und_secret():
    yaml = pytest.importorskip("yaml")
    text = (ROOT / ".github/workflows/avatar-render.yml").read_text()
    data = yaml.safe_load(text)
    steps = data["jobs"]["render"]["steps"]
    names = [s.get("name", "") for s in steps]
    assert any("username.txt" in n for n in names)
    uploads = [s for s in steps if s.get("uses", "").startswith("actions/upload-artifact")]
    assert uploads, "upload-artifact fehlt"
    main_upload = uploads[0]
    assert main_upload["with"]["name"] == "avatar-render"
    assert main_upload["with"]["path"] == "render_avatar.png"
    assert main_upload["with"]["retention-days"] == 7
    assert "ROBLOX_OPEN_CLOUD_API_KEY" in text
    assert "blender/render_glass.py" in text


def test_render_workflow_veroeffentlicht_renders_branch_und_release():
    # Bugfix Handy: Artifacts sind session-gebunden (iOS-App -> 404), darum
    # zusaetzlich renders-Branch + Release mit stabiler URL.
    yaml = pytest.importorskip("yaml")
    text = (ROOT / ".github/workflows/avatar-render.yml").read_text()
    data = yaml.safe_load(text)
    assert data["permissions"]["contents"] == "write"
    assert "HEAD:renders" in text
    assert "latest.png" in text
    assert "avatar-render-latest" in text
    assert "gh release" in text and "--clobber" in text
    assert "raw.githubusercontent.com" in text
    assert "pip install --quiet zstandard lz4" in text
    # Pfad-Filter bleibt eng, damit der renders-Push keine Schleife ausloest
    triggers = data.get("on", data.get(True))
    paths = triggers["push"]["paths"]
    assert "username.txt" in paths
    assert not any("png" in p for p in paths)


def test_anleitung_ist_deutsch_und_vollstaendig():
    text = (ROOT / "ANLEITUNG.md").read_text(encoding="utf-8")
    for needle in ("ROBLOX_OPEN_CLOUD_API_KEY", "legacy-asset:manage", "username.txt",
                   "Actions", "Artifacts", "artifact", "GPU", "Nutzungsbedingungen"):
        assert needle.lower() in text.lower(), needle
