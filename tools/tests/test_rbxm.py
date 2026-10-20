"""Tests fuer den RBXM-/RBXMX-Parser (tools/rbxm.py).

Alle Testdaten werden synthetisch mit rbxm_factory erzeugt - kein
Netzwerk, keine echten Roblox-Dateien.
"""

from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)
sys.path.insert(0, HERE)

import rbxm  # noqa: E402
from rbxm_factory import make_rbxm  # noqa: E402


def _meshpart(name="Arm", mesh_id=111, texture_id=222, size=(1.0, 2.0, 1.0),
              pos=(0.5, 3.0, -0.25), Parent=None):
    props = {"Name": name, "size": size, "CFrame": pos}
    if mesh_id is not None:
        props["MeshId"] = f"rbxassetid://{mesh_id}"
    if texture_id is not None:
        props["TextureID"] = f"rbxassetid://{texture_id}"
    return {"class": "MeshPart", "props": props, "Parent": Parent}


def test_rbxm_uncompressed_meshpart():
    data = make_rbxm([_meshpart()])
    entries = rbxm.extract_assets(data)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["class"] == "MeshPart"
    assert entry["name"] == "Arm"
    assert entry["mesh_id"] == 111
    assert entry["texture_id"] == 222
    assert entry["size"] == pytest.approx((1.0, 2.0, 1.0))
    assert entry["position"] == pytest.approx((0.5, 3.0, -0.25))


def test_rbxm_mehrere_instanzen_einer_klasse():
    # Prueft Byte-Interleaving + Wertezuordnung bei Anzahl > 1
    data = make_rbxm([
        _meshpart("A", 101, None, (1.0, 1.0, 1.0), (1.0, 2.0, 3.0)),
        _meshpart("B", 102, 202, (2.0, 3.0, 4.0), (-1.5, 0.5, 10.0)),
        _meshpart("C", 103, None, (0.25, 0.5, 0.75), (0.0, 0.0, 0.0)),
    ])
    entries = rbxm.extract_assets(data)
    assert [e["mesh_id"] for e in entries] == [101, 102, 103]
    assert [e["name"] for e in entries] == ["A", "B", "C"]
    assert entries[1]["texture_id"] == 202
    assert entries[0]["texture_id"] is None
    assert entries[1]["size"] == pytest.approx((2.0, 3.0, 4.0))
    assert entries[2]["position"] == pytest.approx((0.0, 0.0, 0.0))
    assert entries[1]["position"] == pytest.approx((-1.5, 0.5, 10.0))


def test_rbxm_zlib_chunk():
    data = make_rbxm([_meshpart()], compression="zlibraw")
    entries = rbxm.extract_assets(data)
    assert entries[0]["mesh_id"] == 111
    assert entries[0]["size"] == pytest.approx((1.0, 2.0, 1.0))


def test_rbxm_lz4_chunk():
    pytest.importorskip("lz4.block")
    data = make_rbxm([_meshpart()], compression="lz4")
    entries = rbxm.extract_assets(data)
    assert entries[0]["mesh_id"] == 111
    assert entries[0]["texture_id"] == 222


def test_rbxm_zstd_chunk():
    pytest.importorskip("zstandard")
    data = make_rbxm([_meshpart()], compression="zstd")
    entries = rbxm.extract_assets(data)
    assert entries[0]["mesh_id"] == 111
    assert entries[0]["position"] == pytest.approx((0.5, 3.0, -0.25))


def test_rbxm_meshid_als_content_typ():
    data = make_rbxm([_meshpart()], meshid_as_content=True)
    entries = rbxm.extract_assets(data)
    assert entries[0]["mesh_id"] == 111


def test_rbxm_name_als_shared_string():
    data = make_rbxm([_meshpart(name="Krone")], name_as_shared=True)
    entries = rbxm.extract_assets(data)
    assert entries[0]["name"] == "Krone"
    assert entries[0]["mesh_id"] == 111


def test_rbxm_specialmesh_erbt_part_groesse():
    part = {"class": "Part",
            "props": {"Name": "Torso", "size": (2.0, 2.0, 1.0), "CFrame": (0.0, 3.0, 0.0)},
            "Parent": None}
    mesh = {"class": "SpecialMesh",
            "props": {"Name": "Mesh", "MeshId": "rbxassetid://4711",
                      "TextureId": "rbxassetid://4712", "Scale": (0.5, 1.5, 2.0)},
            "Parent": 0}
    entries = rbxm.extract_assets(make_rbxm([part, mesh]))
    assert len(entries) == 1
    entry = entries[0]
    assert entry["mesh_id"] == 4711
    assert entry["texture_id"] == 4712
    assert entry["size"] == pytest.approx((1.0, 3.0, 2.0))  # Part-Groesse * Scale
    assert entry["position"] == pytest.approx((0.0, 3.0, 0.0))
    assert entry["name"] == "Torso"  # Name des Parts statt "Mesh"


def test_rbxm_surfaceappearance_liefert_textur_zum_meshpart():
    part = _meshpart(texture_id=None)
    appearance = {"class": "SurfaceAppearance",
                  "props": {"Name": "SurfaceAppearance",
                            "ColorMap": "rbxassetid://777"},
                  "Parent": 0}
    entries = rbxm.extract_assets(make_rbxm([part, appearance]))
    assert len(entries) == 1
    assert entries[0]["mesh_id"] == 111
    assert entries[0]["texture_id"] == 777


def test_rbxm_cframe_mit_expliziter_matrix():
    inst = _meshpart(pos=None)
    inst["props"]["CFrame"] = ("M", (7.0, 8.0, 9.0))
    entries = rbxm.extract_assets(make_rbxm([inst]))
    assert entries[0]["position"] == pytest.approx((7.0, 8.0, 9.0))


def test_rbxm_ohne_assets_ist_leere_liste():
    folder = {"class": "Ordner", "props": {"Name": "Leer"}, "Parent": None}
    assert rbxm.extract_assets(make_rbxm([folder])) == []


def test_rbxm_kaputte_datei_wirft_saubere_exception():
    for kaputt in (b"", b"muell", b"<roblox!", rbxm.MAGIC,
                   rbxm.MAGIC + b"\x00" * 17,
                   make_rbxm([_meshpart()])[:40]):
        with pytest.raises(rbxm.RbxmError):
            rbxm.extract_assets(kaputt)


@pytest.mark.parametrize("wert,erwartet", [
    ("rbxassetid://123456", 123456),
    ("http://www.roblox.com/asset/?id=123456", 123456),
    ("https://assetdelivery.roblox.com/v1/asset/?id=42&foo=bar", 42),
    ("987", 987),
    ("rbxassetid://0", None),
    ("", None),
    (None, None),
    ("rbxasset://fonts/families/builder.json", None),
])
def test_parse_asset_id(wert, erwartet):
    assert rbxm.parse_asset_id(wert) == erwartet


RBXMX = b"""<roblox xmlns:xmime="http://www.w3.org/2005/05/xmlmime" version="4">
  <Item class="Part" referent="RBX0">
    <Properties>
      <string name="Name">Handle</string>
      <Vector3 name="size"><X>1</X><Y>2</Y><Z>1</Z></Vector3>
      <CoordinateFrame name="CFrame"><X>0</X><Y>4.5</Y><Z>0</Z>
        <R00>1</R00><R01>0</R01><R02>0</R02><R10>0</R10><R11>1</R11><R12>0</R12>
        <R20>0</R20><R21>0</R21><R22>1</R22></CoordinateFrame>
    </Properties>
    <Item class="SpecialMesh" referent="RBX1">
      <Properties>
        <string name="Name">Mesh</string>
        <Content name="MeshId"><url>rbxassetid://31337</url></Content>
        <Content name="TextureId"><url>http://www.roblox.com/asset/?id=31338</url></Content>
        <Vector3 name="Scale"><X>2</X><Y>1</Y><Z>2</Z></Vector3>
      </Properties>
    </Item>
  </Item>
  <Item class="MeshPart" referent="RBX2">
    <Properties>
      <string name="Name">Helm</string>
      <Content name="MeshId"><url>rbxassetid://555</url></Content>
      <Content name="TextureID"><null></null></Content>
      <Vector3 name="size"><X>1.5</X><Y>1</Y><Z>1.5</Z></Vector3>
    </Properties>
  </Item>
</roblox>
"""


def test_rbxmx_xml_wird_gelesen():
    entries = rbxm.extract_assets(RBXMX)
    assert len(entries) == 2
    special, helm = entries[0], entries[1]
    assert special["mesh_id"] == 31337
    assert special["texture_id"] == 31338
    assert special["size"] == pytest.approx((2.0, 2.0, 2.0))  # Part * Scale
    assert special["position"] == pytest.approx((0.0, 4.5, 0.0))
    assert special["name"] == "Handle"
    assert helm["class"] == "MeshPart"
    assert helm["mesh_id"] == 555
    assert helm["texture_id"] is None


def test_rbxmx_kaputtes_xml():
    with pytest.raises(rbxm.RbxmError):
        rbxm.extract_assets(b"<roblox><Item>")


def test_is_rbxm_erkennung():
    assert rbxm.is_rbxm(make_rbxm([_meshpart()]))
    assert rbxm.is_rbxm(RBXMX)
    assert not rbxm.is_rbxm(b"version 2.00\n")
    assert not rbxm.is_rbxm(b"")
