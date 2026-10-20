"""Tests fuer FileMesh 6.00 (Chunk-Container, COREMESH v1) und 7.00
(Draco-komprimiert, COREMESH v2) sowie merge_meshes().

Hintergrund: Das Bricksmith-Badge von MertaStudios (FrontAccessory,
Mesh-Asset 110536326812594) scheiterte im echten Lauf mit "Datei zu
kurz fuer die angegebene Vertex-Anzahl", weil der Parser v6/v7 wie
v4 behandelte. In Wahrheit nutzen beide ein Chunk-Format; v7 ist
zusaetzlich Draco-komprimiert (Spec: devforum "Roblox FileMesh Format
Specification", Revision 3/30/2025).
"""

from __future__ import annotations

import os
import struct
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

import roblox_mesh  # noqa: E402


def _vertex_bytes(mesh, with_color=True):
    out = bytearray()
    for i, pos in enumerate(mesh.positions):
        nrm = mesh.normals[i] if i < len(mesh.normals) else (0.0, 1.0, 0.0)
        uv = mesh.uvs[i] if i < len(mesh.uvs) else (0.0, 0.0)
        out += struct.pack("<8f", *(pos + nrm + uv))
        out += struct.pack("<4b", 0, 0, -127, 127)  # Tangente
        if with_color:
            out += struct.pack("<4B", 255, 128, 64, 255)
    return bytes(out)


def _chunk(name: bytes, chunk_version: int, payload: bytes) -> bytes:
    return (name.ljust(8, b"\x00")[:8]
            + struct.pack("<II", chunk_version, len(payload)) + payload)


def make_v6(mesh, with_color=True, lod_offsets=None) -> bytes:
    core = (struct.pack("<I", len(mesh.positions))
            + _vertex_bytes(mesh, with_color)
            + struct.pack("<I", len(mesh.faces))
            + b"".join(struct.pack("<3I", *f) for f in mesh.faces))
    data = b"version 6.00\n" + _chunk(b"COREMESH", 1, core)
    if lod_offsets:
        lods = (struct.pack("<HB", 0, 1)
                + struct.pack("<I", len(lod_offsets))
                + b"".join(struct.pack("<I", o) for o in lod_offsets))
        data += _chunk(b"LODS", 1, lods)
    return data


def make_v7(mesh) -> bytes:
    DracoPy = pytest.importorskip("DracoPy")
    np = pytest.importorskip("numpy")
    points = np.array(mesh.positions, dtype=float)
    faces = np.array(mesh.faces, dtype=np.uint32)
    blob = DracoPy.encode(points, faces=faces)
    core = struct.pack("<I", len(blob)) + blob
    return b"version 7.00\n" + _chunk(b"COREMESH", 2, core)


# ---------------------------------------------------------------------------
# FileMesh 6.00
# ---------------------------------------------------------------------------


def test_v6_coremesh_mit_farben():
    original = roblox_mesh.make_box(2.0, 1.0, 3.0)
    parsed = roblox_mesh.parse(make_v6(original, with_color=True))
    assert parsed.source_version == "6.00"
    assert len(parsed.positions) == len(original.positions)
    assert len(parsed.faces) == len(original.faces)
    assert parsed.size() == pytest.approx((2.0, 1.0, 3.0), abs=1e-5)
    assert parsed.colors is not None
    assert parsed.colors[0] == (255, 128, 64, 255)


def test_v6_coremesh_ohne_farben_stride36():
    original = roblox_mesh.make_box(1.0, 1.0, 1.0)
    parsed = roblox_mesh.parse(make_v6(original, with_color=False))
    assert len(parsed.faces) == len(original.faces)
    assert parsed.size() == pytest.approx((1.0, 1.0, 1.0), abs=1e-5)


def test_v6_lods_chunk_begrenzt_auf_erstes_lod():
    original = roblox_mesh.make_box(1.0, 1.0, 1.0)  # 12 Faces
    parsed = roblox_mesh.parse(make_v6(original, lod_offsets=[0, 5, 12]))
    assert len(parsed.faces) == 5


def test_v6_ohne_coremesh_ist_fehler():
    data = b"version 6.00\n" + _chunk(b"LODS", 1, b"\x00" * 8)
    with pytest.raises(roblox_mesh.MeshParseError):
        roblox_mesh.parse(data)


# ---------------------------------------------------------------------------
# FileMesh 7.00 (Draco)
# ---------------------------------------------------------------------------


def test_v7_draco_wird_dekodiert():
    original = roblox_mesh.make_sphere(1.5, 10, 8)
    parsed = roblox_mesh.parse(make_v7(original))
    assert parsed.source_version == "7.00"
    assert not parsed.is_empty
    # Draco-Quantisierung: Groesse nur ungefaehr pruefen
    assert max(parsed.size()) == pytest.approx(3.0, abs=0.05)
    assert len(parsed.normals) == len(parsed.positions)  # ggf. berechnet


def test_v7_ohne_dracopy_klare_fehlermeldung(monkeypatch):
    pytest.importorskip("DracoPy")
    data = make_v7(roblox_mesh.make_box(1.0, 1.0, 1.0))
    monkeypatch.setitem(sys.modules, "DracoPy", None)
    with pytest.raises(roblox_mesh.MeshParseError, match="DracoPy"):
        roblox_mesh.parse(data)


# ---------------------------------------------------------------------------
# merge_meshes (R15-Segmente)
# ---------------------------------------------------------------------------


def test_merge_meshes_positioniert_segmente():
    segmente = [
        (roblox_mesh.make_box(1.0, 1.0, 1.0), (1.0, 1.0, 1.0), (0.0, 0.0, 0.0)),
        (roblox_mesh.make_box(1.0, 1.0, 1.0), (1.0, 1.0, 1.0), (0.0, 2.0, 0.0)),
    ]
    merged = roblox_mesh.merge_meshes(segmente)
    assert len(merged.faces) == 24
    lo, hi = merged.bbox()
    assert lo[1] == pytest.approx(-0.5)
    assert hi[1] == pytest.approx(2.5)
    assert merged.size()[1] == pytest.approx(3.0)
    # Face-Indizes des zweiten Segments muessen versetzt sein
    assert max(max(f) for f in merged.faces) == len(merged.positions) - 1
