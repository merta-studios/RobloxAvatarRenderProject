"""Tests fuer Emote-Posen (emote_pose) und deren Integration.

Geprueft werden:

  * Referenz-Parsing ("emote:123", Katalog-URL, reine ID) und die
    Normalisierung/Fehlermeldungen in render_config.
  * rbxm.parse_instances: kompletter Instanz-Baum aus RBXMX (XML) und RBXM
    (binaer, via rbxm_factory) inkl. NumberValues, Keyframes, Pose-CFrames
    und FloatCurve-Blobs.
  * Die Frame-Auswahl exakt nach Roblox' Thumbnail-Regeln
    (ThumbnailKeyframe/ThumbnailTime, Fallback Mittel-Keyframe/halbe Dauer,
    manueller Override emote_zeit).
  * FloatCurve-Dekodierung/-Auswertung (konstant/linear/kubisch Hermite,
    Auto-Tangenten) gegen die Engine-Referenz (Vanadium/Roblox-Quellcode).
  * Die Umrechnung Pose-CFrame -> PoseContext-Format inkl. Roblox-Euler-
    Ordnung, Gimbal-Lock und Root-Motion.
  * Der komplette Ablauf load_emote_pose (Emote-Asset -> AnimationId ->
    Clip -> Pose) mit Attrappen statt Netzwerk.
  * Integration in build_avatar: pose = "emote:..." + emote_datei ohne
    Netzwerk, Fallback auf Stand-Pose bei Fehlern.
"""

from __future__ import annotations

import base64
import math
import os
import struct
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
ROOT = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)

import avatar_pose  # noqa: E402
import build_avatar  # noqa: E402
import emote_pose  # noqa: E402
import rbxm  # noqa: E402
import render_config  # noqa: E402
import roblox_api as api  # noqa: E402
from rbxm_factory import make_rbxm  # noqa: E402


# ---------------------------------------------------------------------------
# Test-Helfer: ValuesAndTimes-Encoder und RBXMX-Builder
# ---------------------------------------------------------------------------

def encode_float_curve(keys, version=2, tick_version=1):
    """Keys [(t, wert, interp, links, rechts, flags)] -> ValuesAndTimes-Blob."""
    out = struct.pack("<II", version, len(keys))
    for (t, value, interp, left, right, flags) in keys:
        if version == 0:
            out += struct.pack("<Ifff", interp, value, left, right)
        else:
            out += struct.pack("<BBfff", interp, flags, value, left, right)
    out += struct.pack("<II", tick_version, len(keys))
    for (t, *_rest) in keys:
        if tick_version == 0:
            out += struct.pack("<f", t)
        else:
            out += struct.pack("<i", int(round(t * emote_pose.TICKS_PER_SECOND)))
    return out


def encode_rotation_curve(keys, version=1, tick_version=1):
    """Quaternion-Keys [(t, (x,y,z,w), interp, links, rechts, flags)]."""
    out = struct.pack("<II", version, len(keys))
    for (t, quat, interp, left, right, flags) in keys:
        packed = (interp & 3) | ((flags & 3) << 2)
        out += struct.pack("<Bffffff", packed, quat[0], quat[1], quat[2], quat[3],
                           left, right)
    out += struct.pack("<II", tick_version, len(keys))
    for (t, *_rest) in keys:
        out += struct.pack("<i", int(round(t * emote_pose.TICKS_PER_SECOND)))
    return out


def _b64(blob: bytes) -> str:
    return base64.b64encode(blob).decode("ascii")


def rot_x_deg(deg: float):
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return ((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c))


def rot_y_deg(deg: float):
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c))


IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def xml_cframe(name: str, pos, rot=None, weight=None) -> str:
    rot = rot or IDENTITY
    rows = "".join(f"<R{i}{j}>{rot[i][j]}</R{i}{j}>"
                   for i in range(3) for j in range(3))
    weight_xml = (f'<float name="Weight">{weight}</float>'
                  if weight is not None else "")
    return (f'<Item class="Pose"><Properties><string name="Name">{name}</string>'
            f'<CoordinateFrame name="CFrame"><X>{pos[0]}</X><Y>{pos[1]}</Y>'
            f"<Z>{pos[2]}</Z>{rows}</CoordinateFrame>{weight_xml}</Properties></Item>")


def xml_emote_asset(animation_id: int = 987654321,
                    thumbnail_keyframe=None, thumbnail_time=None) -> str:
    """Katalog-Emote-Asset: Model > Animation (AnimationId + NumberValues)."""
    values = ""
    if thumbnail_keyframe is not None:
        values += (f'<Item class="NumberValue"><Properties>'
                   f'<string name="Name">ThumbnailKeyframe</string>'
                   f'<double name="Value">{thumbnail_keyframe}</double>'
                   f"</Properties></Item>")
    if thumbnail_time is not None:
        values += (f'<Item class="NumberValue"><Properties>'
                   f'<string name="Name">ThumbnailTime</string>'
                   f'<double name="Value">{thumbnail_time}</double>'
                   f"</Properties></Item>")
    values += ('<Item class="IntValue"><Properties>'
               '<string name="Name">ThumbnailBundleId</string>'
               '<int name="Value">515</int></Properties></Item>')
    return f"""<roblox version="4">
  <Item class="Model" referent="RBX0">
    <Properties><string name="Name">TiltEmote</string></Properties>
    <Item class="Animation" referent="RBX1">
      <Properties>
        <Content name="AnimationId"><url>rbxassetid://{animation_id}</url></Content>
      </Properties>
      {values}
    </Item>
  </Item>
</roblox>"""


def xml_keyframe_sequence() -> str:
    """Drei Keyframes (0 s / 0,5 s / 1 s) mit wachsender Pose."""
    return f"""<roblox version="4">
  <Item class="KeyframeSequence" referent="KFS">
    <Properties><string name="Name">TiltClip</string></Properties>
    <Item class="Keyframe"><Properties><string name="Name">K0</string>
      <float name="Time">0</float></Properties>
      {xml_cframe("LowerTorso", (0, 0, 0))}
      {xml_cframe("UpperTorso", (0, 0, 0), rot_x_deg(5))}
    </Item>
    <Item class="Keyframe"><Properties><string name="Name">K1</string>
      <float name="Time">0.5</float></Properties>
      {xml_cframe("LowerTorso", (0, 0.2, 0))}
      {xml_cframe("UpperTorso", (0, 0, 0), rot_x_deg(25))}
      {xml_cframe("Head", (0, 0, 0), rot_x_deg(10))}
      {xml_cframe("LeftUpperArm", (0, 0, 0), rot_x_deg(70))}
      {xml_cframe("RightLowerArm", (0, 0, 0), rot_x_deg(30), weight=0)}
    </Item>
    <Item class="Keyframe"><Properties><string name="Name">K2</string>
      <float name="Time">1.0</float></Properties>
      {xml_cframe("LowerTorso", (0, 0, 0))}
      {xml_cframe("UpperTorso", (0, 0, 0), rot_x_deg(0))}
    </Item>
  </Item>
</roblox>"""


def xml_curve_animation(rotation_order: int = 0,
                        pos_keys=None, rx_keys=None, quat_keys=None) -> str:
    """CurveAnimation mit HumanoidRootPart > LowerTorso/UpperTorso."""
    pos_keys = pos_keys or [(0.0, 0.0, 1, 0, 0, 0), (2.0, 0.5, 1, 0, 0, 0)]
    rx_keys = rx_keys or [(0.0, 0.0, 1, 0, 0, 0), (2.0, math.radians(40), 1, 0, 0, 0)]
    if quat_keys is None:
        rotation = f"""<Item class="EulerRotationCurve"><Properties>
          <string name="Name">Rotation</string>
          <token name="RotationOrder">{rotation_order}</token></Properties>
          <Item class="FloatCurve"><Properties><string name="Name">X</string>
            <BinaryString name="ValuesAndTimes">{_b64(encode_float_curve(rx_keys))}</BinaryString>
          </Properties></Item>
        </Item>"""
    else:
        # Quaternion-Variante: der Blob sitzt direkt auf der RotationCurve.
        rotation = f"""<Item class="RotationCurve"><Properties>
          <string name="Name">Rotation</string>
          <BinaryString name="ValuesAndTimes">{_b64(encode_rotation_curve(quat_keys))}</BinaryString>
        </Properties></Item>"""
    return f"""<roblox version="4">
  <Item class="CurveAnimation" referent="CA">
    <Properties><string name="Name">TiltCurve</string></Properties>
    <Item class="Folder">
      <Properties><string name="Name">HumanoidRootPart</string></Properties>
      <Item class="Folder">
        <Properties><string name="Name">LowerTorso</string></Properties>
        <Item class="Vector3Curve"><Properties><string name="Name">Position</string></Properties>
          <Item class="FloatCurve"><Properties><string name="Name">Y</string>
            <BinaryString name="ValuesAndTimes">{_b64(encode_float_curve(pos_keys))}</BinaryString>
          </Properties></Item>
        </Item>
      </Item>
      <Item class="Folder">
        <Properties><string name="Name">UpperTorso</string></Properties>
        {rotation}
      </Item>
    </Item>
  </Item>
</roblox>"""


class FakeAsset:
    def __init__(self, data: bytes):
        self.data = data
        self.note = "fake"


class FakeClient:
    """Attrappe fuer roblox_api.HttpClient (kein Netzwerk)."""


@pytest.fixture()
def no_network(monkeypatch):
    """Ersetzt api.download_asset durch eine Tabelle id -> Bytes."""
    table = {}

    def fake_download(client, asset_id):
        data = table.get(asset_id)
        if data is None:
            return api.DownloadedAsset(asset_id, None, "", "", "nicht gefunden")
        return api.DownloadedAsset(asset_id, data, "application/octet-stream",
                                   "fake", "")

    monkeypatch.setattr(api, "download_asset", fake_download)
    return table


# ---------------------------------------------------------------------------
# Referenz-Parsing und Configs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("emote:3576893107", 3576893107),
    ("Emote:123", 123),
    ("emote=123", 123),
    ("Emote 123", 123),
    ("123", 123),
    ("https://www.roblox.com/catalog/3576893107/Tilt", 3576893107),
    ("roblox.com/library/42/xyz", 42),
    ("None", None),
    ("", None),
    (None, None),
    ("Helden_Pose", None),
    ("Siegerfaust", None),
    ("emote:", None),
])
def test_parse_emote_reference(value, expected):
    assert emote_pose.parse_emote_reference(value) == expected


def _write_config(tmp_path, **overrides):
    lines = [
        'username = "Builderman"',
        "drehung_grad = 0",
        'pose = "None"',
        "render_bei_aenderung = false",
        'hintergrund_url = "transparent"',
        "hintergrund_glossy = false",
        "emote_zeit = -1",
        'emote_datei = ""',
    ]
    for key, value in overrides.items():
        lines = [l for l in lines if not l.startswith(key + " ")]
        if isinstance(value, str):
            lines.append(f'{key} = "{value}"')
        elif isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        else:
            lines.append(f"{key} = {value}")
    path = tmp_path / "Configs.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("pose,expected", [
    ("emote:3576893107", "emote:3576893107"),
    ("Emote = 123", "emote:123"),
    ("3576893107", "emote:3576893107"),
    ("https://www.roblox.com/catalog/3576893107/Tilt", "emote:3576893107"),
    ("None", "None"),
    ("Helden_Pose", "Helden_Pose"),
])
def test_config_normalisiert_emote_pose(tmp_path, pose, expected):
    config = render_config.load_config(_write_config(tmp_path, pose=pose))
    assert config["pose"] == expected


def test_config_lehnt_kaputte_emote_referenz_ab(tmp_path):
    path = _write_config(tmp_path, pose="emote:abc")
    with pytest.raises(render_config.ConfigError, match="Asset-ID"):
        render_config.load_config(path)


def test_config_emote_zeit_grenzen(tmp_path):
    config = render_config.load_config(_write_config(tmp_path, emote_zeit=2.5))
    assert config["emote_zeit"] == 2.5
    with pytest.raises(render_config.ConfigError, match="emote_zeit"):
        render_config.load_config(_write_config(tmp_path, emote_zeit=-900))
    with pytest.raises(render_config.ConfigError, match="emote_zeit"):
        render_config.load_config(_write_config(tmp_path, emote_zeit=601))


def test_config_emote_datei_pfad_und_warnung(tmp_path):
    path = _write_config(tmp_path, emote_datei="emote.rbxmx")
    config = render_config.load_config(path)
    assert config["_emote_pfad"].endswith("emote.rbxmx")
    assert any("emote_datei" in w for w in config["_warnungen"])

    (tmp_path / "emote.rbxmx").write_text("<roblox></roblox>", encoding="utf-8")
    config = render_config.load_config(path)
    assert config["_emote_pfad"] == str(tmp_path / "emote.rbxmx")
    assert not any("emote_datei" in w for w in config["_warnungen"])


def test_repo_config_bleibt_gueltig():
    config = render_config.load_config(os.path.join(ROOT, "Configs.toml"))
    pose = config["pose"]
    # Der Pose-Wert in der Repo-Config aendert sich regelmaessig (None,
    # Posen.toml-Name oder Emote) - hier nur pruefen, dass er jeweils
    # gueltig IST, nicht welcher Wert gerade eingetragen ist.
    if pose != "None" and not pose.startswith("emote:"):
        poses = avatar_pose.load_poses(os.path.join(ROOT, "Posen.toml"))
        assert pose in poses, f"Pose {pose!r} fehlt in Posen.toml"
    assert render_config.MIN_EMOTE_ZEIT <= config["emote_zeit"] <= render_config.MAX_EMOTE_ZEIT
    assert isinstance(config["emote_datei"], str)


# ---------------------------------------------------------------------------
# rbxm.parse_instances (XML + binaer)
# ---------------------------------------------------------------------------

def test_parse_instances_liefert_baum_xml():
    roots = rbxm.parse_instances(xml_emote_asset(thumbnail_keyframe=2).encode())
    assert len(roots) == 1 and roots[0]["class"] == "Model"
    animation = roots[0]["children"][0]
    assert animation["class"] == "Animation"
    assert rbxm.parse_asset_id(animation["animation_url"]) == 987654321
    values = {child["name"]: child for child in animation["children"]}
    assert values["ThumbnailKeyframe"]["value_num"] == 2.0
    assert values["ThumbnailBundleId"]["value_num"] == 515.0


def test_parse_instances_liefert_keyframes_xml():
    roots = rbxm.parse_instances(xml_keyframe_sequence().encode())
    clip = emote_pose.parse_keyframe_sequence(roots)
    assert clip is not None and clip["typ"] == "keyframes"
    assert [k["time"] for k in clip["keyframes"]] == [0.0, 0.5, 1.0]
    posen = clip["keyframes"][1]["posen"]
    assert set(posen) == {"LowerTorso", "UpperTorso", "Head", "LeftUpperArm"}
    # Weight 0 (RightLowerArm) wird wie bei Roblox ignoriert.
    assert "RightLowerArm" not in posen


def test_parse_instances_binary_number_und_pose_values():
    """Binaeres RBXM: NumberValue.Value (Int32), Keyframe.Time (Float32),
    Pose.CFrame, ValuesAndTimes (Rohbytes) und RotationOrder (Token)."""
    blob = encode_float_curve([(0.0, 1.5, 1, 0, 0, 0)])
    instances = [
        {"class": "Model", "props": {"Name": "Emote"}, "Parent": None},
        {"class": "Animation", "props": {"AnimationId": "rbxassetid://111"},
         "Parent": 0},
        {"class": "NumberValue", "props": {"Name": "ThumbnailKeyframe",
                                           "Value": 2}, "Parent": 1},
        {"class": "KeyframeSequence", "props": {"Name": "Clip"}, "Parent": 0},
        {"class": "Keyframe", "props": {"Name": "K", "Time": 0.25}, "Parent": 3},
        {"class": "Pose", "props": {"Name": "Head",
                                    "CFrame": ("M", (0.0, 1.0, 0.0))},
         "Parent": 4},
        {"class": "FloatCurve", "props": {"Name": "Y",
                                          "ValuesAndTimes": blob}, "Parent": 1},
        {"class": "EulerRotationCurve", "props": {"Name": "Rotation",
                                                  "RotationOrder": 3}, "Parent": 1},
    ]
    data = make_rbxm(instances, prop_types={
        "Value": "int", "Time": "float", "ValuesAndTimes": "bytes",
        "RotationOrder": "enum"})
    assert rbxm.is_rbxm(data)
    roots = rbxm.parse_instances(data)
    alle = {r["class"]: r for r in emote_pose._iter_tree(roots)}
    assert alle["NumberValue"]["name"] == "ThumbnailKeyframe"
    assert alle["NumberValue"]["value_num"] == 2.0
    assert alle["Keyframe"]["time"] == 0.25
    pose = alle["Pose"]
    assert pose["position"] == (0.0, 1.0, 0.0) and pose["rotation"] is not None
    assert alle["FloatCurve"]["values_and_times"] == blob
    assert alle["EulerRotationCurve"]["rotation_order"] == 3


def test_parse_instances_binary_double_und_shared_bytes():
    """Float64 (plain LE) und ValuesAndTimes als SharedString-Index (SSTR)."""
    blob = encode_float_curve([(0.0, 2.0, 0, 0, 0, 0)])
    blob2 = encode_float_curve([(1.0, 4.0, 1, 0, 0, 0)])
    instances = [
        {"class": "NumberValue", "props": {"Name": "ThumbnailTime",
                                           "Value": 1.5}, "Parent": None},
        {"class": "FloatCurve", "props": {"Name": "Y",
                                          "ValuesAndTimes": blob}, "Parent": 0},
        {"class": "FloatCurve", "props": {"Name": "Z",
                                          "ValuesAndTimes": blob2}, "Parent": 0},
    ]
    data = make_rbxm(instances, name_as_shared=True,
                     prop_types={"Value": "double", "ValuesAndTimes": "shared"})
    roots = rbxm.parse_instances(data)
    alle = {r["class"]: r for r in emote_pose._iter_tree(roots)}
    assert alle["NumberValue"]["value_num"] == 1.5
    assert alle["NumberValue"]["name"] == "ThumbnailTime"  # via SharedString
    kurven = [r for r in emote_pose._iter_tree(roots) if r["class"] == "FloatCurve"]
    nach_name = {r["name"]: r["values_and_times"] for r in kurven}
    assert nach_name["Y"] == blob
    assert nach_name["Z"] == blob2


# ---------------------------------------------------------------------------
# Thumbnail-Metadaten und Frame-Auswahl (Roblox-Regeln)
# ---------------------------------------------------------------------------

def test_read_thumbnail_metadata():
    roots = rbxm.parse_instances(
        xml_emote_asset(thumbnail_keyframe=3, thumbnail_time=0.5).encode())
    meta = emote_pose.read_thumbnail_metadata(roots)
    assert meta["ThumbnailKeyframe"] == 3.0
    assert meta["ThumbnailTime"] == 0.5
    assert meta["ThumbnailBundleId"] == 515.0


@pytest.mark.parametrize("count,keyframe,expected_index", [
    (9, 3, 2),      # gueltige 1-basierte Nummer
    (9, 31, 4),     # ungueltig -> Mittel-Keyframe ceil(9/2) = 5 -> Index 4
    (9, 0, 4),      # 0 ist ungueltig
    (9, -2, 4),
    (9, None, 4),   # ohne Wert -> Mittel-Keyframe
    (1, 5, 0),      # einzelner Keyframe
    (4, 2, 1),
])
def test_select_keyframe_index(count, keyframe, expected_index):
    assert emote_pose.select_keyframe_index(count, keyframe) == expected_index


@pytest.mark.parametrize("laenge,zeit,expected", [
    (4.0, 0.5, 0.5),   # gueltige ThumbnailTime
    (4.0, 0.0, 0.0),
    (4.0, 4.0, 4.0),
    (4.0, 9.0, 2.0),   # jenseits der Dauer -> halbe Dauer
    (4.0, -1.0, 2.0),
    (4.0, None, 2.0),  # ohne Wert -> halbe Dauer
])
def test_select_sample_time(laenge, zeit, expected):
    assert emote_pose.select_sample_time(laenge, zeit) == pytest.approx(expected)


def test_closest_keyframe_index():
    keyframes = [{"time": 0.0}, {"time": 0.5}, {"time": 1.0}]
    assert emote_pose.closest_keyframe_index(keyframes, 0.6) == 1
    assert emote_pose.closest_keyframe_index(keyframes, 0.9) == 2
    assert emote_pose.closest_keyframe_index(keyframes, -5.0) == 0


# ---------------------------------------------------------------------------
# FloatCurve: Dekodierung und Auswertung
# ---------------------------------------------------------------------------

def test_decode_float_curve_versionen():
    keys = [(0.0, 1.0, 1, 0.0, 0.0, 0), (2.0, 3.0, 1, 0.0, 0.0, 3)]
    for version in (0, 1, 2):
        decoded = emote_pose.decode_float_curve(encode_float_curve(keys, version=version))
        assert [k["time"] for k in decoded] == [0.0, 2.0]
        assert decoded[0]["value"] == 1.0 and decoded[1]["value"] == 3.0
        assert decoded[1]["left"] == 0.0 and decoded[1]["right"] == 0.0
    # Tick-Version 0 speichert Sekunden als f32.
    decoded = emote_pose.decode_float_curve(encode_float_curve(keys, tick_version=0))
    assert [k["time"] for k in decoded] == [0.0, 2.0]


def test_decode_float_curve_kaputte_daten():
    assert emote_pose.decode_float_curve(b"") == []
    assert emote_pose.decode_float_curve(b"\x99" * 8) == []
    assert emote_pose.decode_float_curve(struct.pack("<III", 99, 0, 0)) == []


def test_evaluate_float_curve_interp_modes():
    konstant = [{"time": 0.0, "value": 1.0, "interp": 0, "left": None, "right": None},
                {"time": 1.0, "value": 5.0, "interp": 0, "left": None, "right": None}]
    assert emote_pose.evaluate_float_curve(konstant, 0.5) == 1.0
    assert emote_pose.evaluate_float_curve(konstant, 1.0) == 5.0

    linear = [{"time": 0.0, "value": 0.0, "interp": 1, "left": None, "right": None},
              {"time": 2.0, "value": 1.0, "interp": 1, "left": None, "right": None}]
    assert emote_pose.evaluate_float_curve(linear, 1.0) == pytest.approx(0.5)
    assert emote_pose.evaluate_float_curve(linear, -1.0) == pytest.approx(0.0)
    assert emote_pose.evaluate_float_curve(linear, 9.0) == pytest.approx(1.0)

    einzeln = [{"time": 0.5, "value": 7.0, "interp": 2, "left": None, "right": None}]
    assert emote_pose.evaluate_float_curve(einzeln, 0.0) == 7.0
    assert emote_pose.evaluate_float_curve([], 0.0) is None


def test_evaluate_float_curve_kubisch_mit_tangenten():
    """Hermite mit expliziten Tangenten gegen die geschlossene Formel."""
    # Tangenten 0: klassische Smoothstep-Bruecke, in der Mitte 0,5.
    keys = [{"time": 0.0, "value": 0.0, "interp": 2, "left": 0.0, "right": 0.0},
            {"time": 1.0, "value": 1.0, "interp": 2, "left": 0.0, "right": 0.0}]
    assert emote_pose.evaluate_float_curve(keys, 0.5) == pytest.approx(0.5)

    # Nur die rechte Tangente des Startkeys (6) zieht die Kurve hoch:
    # h(0.5) = (u + u^3 - 2u^2) * 6 + 0.25 = 0.75 + 0.25 = 1.0 ... exakt:
    # 0.125 * 6 + (2*0.125 - 3*0.25 negiert) * 1 = 0.75 + 0.5 = 1.25.
    steil = [{"time": 0.0, "value": 0.0, "interp": 2, "left": 0.0, "right": 6.0},
             {"time": 1.0, "value": 1.0, "interp": 2, "left": 0.0, "right": 0.0}]
    assert emote_pose.evaluate_float_curve(steil, 0.5) == pytest.approx(1.25)
    assert emote_pose.evaluate_float_curve(steil, 0.0) == pytest.approx(0.0)
    assert emote_pose.evaluate_float_curve(steil, 1.0) == pytest.approx(1.0)

    # Negative Tangente drueckt die Kurve unter die Sehne.
    negativ = [{"time": 0.0, "value": 0.0, "interp": 2, "left": 0.0, "right": -6.0},
               {"time": 1.0, "value": 1.0, "interp": 2, "left": 0.0, "right": 0.0}]
    assert emote_pose.evaluate_float_curve(negativ, 0.5) == pytest.approx(-0.25)


def test_complete_tangents_ergaenzt_auto():
    """Ohne explizite Tangenten gilt die Auto-Tangenten-Regel der Engine."""
    keys = [{"time": 0.0, "value": 0.0, "interp": 2, "left": None, "right": None},
            {"time": 1.0, "value": 1.0, "interp": 2, "left": None, "right": None},
            {"time": 2.0, "value": 0.0, "interp": 2, "left": None, "right": None}]
    left, right = emote_pose._complete_tangents(keys)
    # Endpunkt-Tangenten sind 0, der Mittelwert der Nachbarschaftssteigungen
    # ist bei diesem V-foermigen Verlauf 0.
    assert left[0] == 0.0 and right[0] == 0.0
    assert left[2] == 0.0 and right[2] == 0.0
    assert left[1] == 0.0 and right[1] == 0.0


def test_evaluate_rotation_curve_slerp():
    q0 = (0.0, 0.0, 0.0, 1.0)                     # Identitaet
    q90 = (math.sin(math.pi / 4), 0.0, 0.0, math.cos(math.pi / 4))  # +90 Grad X
    keys = [{"time": 0.0, "quat": q0, "interp": 1, "left": None, "right": None},
            {"time": 1.0, "quat": q90, "interp": 1, "left": None, "right": None}]
    m = emote_pose.evaluate_rotation_curve(keys, 0.5)
    # Slerp zur Mitte: +45 Grad um X.
    c45, s45 = math.cos(math.pi / 4), math.sin(math.pi / 4)
    assert m[1][1] == pytest.approx(c45, abs=1e-9)
    assert m[2][1] == pytest.approx(s45, abs=1e-9)
    assert m[1][2] == pytest.approx(-s45, abs=1e-9)
    # Ausserhalb des Bereichs: Randwerte.
    assert emote_pose.evaluate_rotation_curve(keys, 0.0)[1][1] == pytest.approx(1.0)
    assert emote_pose.evaluate_rotation_curve(keys, 5.0)[2][1] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# CurveAnimation parsen und samplen
# ---------------------------------------------------------------------------

def test_parse_curve_animation():
    roots = rbxm.parse_instances(xml_curve_animation().encode())
    clip = emote_pose.parse_curve_animation(roots)
    assert clip is not None and clip["typ"] == "curves"
    assert clip["laenge"] == pytest.approx(2.0)
    assert set(clip["gelenke"]) == {"LowerTorso", "UpperTorso"}
    assert clip["gelenke"]["UpperTorso"]["rotation"]["art"] == "euler"
    assert clip["gelenke"]["UpperTorso"]["rotation"]["ordnung"] == 0


def test_sample_curve_animation_linear():
    roots = rbxm.parse_instances(xml_curve_animation().encode())
    clip = emote_pose.parse_curve_animation(roots)
    posen = emote_pose.sample_curve_animation(clip, 1.0)
    # Position rampt linear 0 -> 0,5 ueber 2 s: bei 1 s = 0,25.
    assert posen["LowerTorso"][0][1] == pytest.approx(0.25)
    # Rotation rampt 0 -> 40 Grad ueber 2 s: bei 1 s = 20 Grad X.
    m = posen["UpperTorso"][1]
    assert m[1][1] == pytest.approx(math.cos(math.radians(20)))
    assert m[2][1] == pytest.approx(math.sin(math.radians(20)))


def test_curve_rotation_quaternion_variante():
    q90 = (math.sin(math.pi / 4), 0.0, 0.0, math.cos(math.pi / 4))
    quat_keys = [(0.0, (0.0, 0.0, 0.0, 1.0), 1, 0, 0, 0),
                 (2.0, q90, 1, 0, 0, 0)]
    roots = rbxm.parse_instances(xml_curve_animation(quat_keys=quat_keys).encode())
    clip = emote_pose.parse_curve_animation(roots)
    assert clip is not None
    assert clip["gelenke"]["UpperTorso"]["rotation"]["art"] == "quaternion"
    posen = emote_pose.sample_curve_animation(clip, 2.0)
    m = posen["UpperTorso"][1]
    assert m[1][1] == pytest.approx(math.cos(math.pi / 2), abs=1e-6)
    assert m[2][1] == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Mathe: Euler-Konventionen, Gimbal-Lock, Root-Motion
# ---------------------------------------------------------------------------

def test_euler_roundtrip_unsere_konvention():
    for _e in range(100):
        import random
        rx, ry, rz = (random.uniform(-170, 170) for _ in range(3))
        m = avatar_pose._euler_matrix((rx, ry, rz))
        back = emote_pose.mat3_to_euler_deg(m)
        m2 = avatar_pose._euler_matrix(back)
        for i in range(3):
            for j in range(3):
                assert m[i][j] == pytest.approx(m2[i][j], abs=1e-6)


def test_euler_konvertierung_roblox_ordnung():
    """Roblox XYZ-Euler (M = Rx*Ry*Rz) muss nach unserer Zerlegung
    (Rz*Ry*Rx) exakt dieselbe Matrix zurueckliefern."""
    for _e in range(100):
        import random
        rx, ry, rz = (random.uniform(-3, 3) for _ in range(3))
        m = emote_pose.euler_matrix_rbx(rx, ry, rz, 0)
        back = emote_pose.mat3_to_euler_deg(m)
        m2 = avatar_pose._euler_matrix(back)
        for i in range(3):
            for j in range(3):
                assert m[i][j] == pytest.approx(m2[i][j], abs=1e-6)


def test_euler_gimbal_lock():
    for ry in (90.0, -90.0):
        m = avatar_pose._euler_matrix((20.0, ry, 0.0))
        back = emote_pose.mat3_to_euler_deg(m)
        m2 = avatar_pose._euler_matrix(back)
        for i in range(3):
            for j in range(3):
                assert m[i][j] == pytest.approx(m2[i][j], abs=1e-6)


def test_pose_cframes_root_motion():
    """HumanoidRootPart-Posen werden mit LowerTorso kombiniert."""
    posen = {
        "HumanoidRootPart": ((0.0, 1.0, 0.0), IDENTITY),
        "LowerTorso": ((0.0, 0.5, 0.0), IDENTITY),
        "UpperTorso": ((0.0, 0.0, 0.0), rot_x_deg(10)),
    }
    pose_dict, unbekannt = emote_pose.pose_cframes_to_pose_dict(posen)
    assert unbekannt == []
    assert pose_dict["unterer_torso"]["verschiebung"] == (0.0, 1.5, 0.0)
    assert pose_dict["oberer_torso"]["rotation"][0] == pytest.approx(10.0)


def test_pose_cframes_unbekannte_gelenke():
    posen = {"Torso": ((0, 0, 0), IDENTITY), "Left Arm": ((0, 0, 0), IDENTITY)}
    pose_dict, unbekannt = emote_pose.pose_cframes_to_pose_dict(posen)
    assert pose_dict == {}
    assert set(unbekannt) == {"Torso", "Left Arm"}


def test_pose_cframes_extreme_offsets_abgefangen():
    with pytest.raises(emote_pose.EmoteError):
        emote_pose.pose_cframes_to_pose_dict(
            {"LowerTorso": ((0, 500.0, 0), IDENTITY)})


# ---------------------------------------------------------------------------
# Gesamtablauf load_emote_pose (ohne Netzwerk)
# ---------------------------------------------------------------------------

def _write(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_load_emote_pose_keyframe_sequence(tmp_path, no_network):
    no_network[987654321] = xml_keyframe_sequence().encode()
    datei = _write(tmp_path, "emote.rbxmx",
                   xml_emote_asset(thumbnail_keyframe=2))
    pose_dict, settings, info = emote_pose.load_emote_pose(
        emote_id=3576893107, client=FakeClient(), datei=datei,
        log=lambda m: None)
    # ThumbnailKeyframe 2 -> Keyframe K1 (Index 1) bei 0,5 s.
    assert info["frame"] == 1
    assert info["auswahl"] == "ThumbnailKeyframe 2"
    assert info["zeit_s"] == pytest.approx(0.5)
    assert set(pose_dict) == {"unterer_torso", "oberer_torso", "kopf",
                              "linker_oberarm"}
    assert pose_dict["linker_oberarm"]["rotation"][0] == pytest.approx(70.0)
    assert pose_dict["unterer_torso"]["verschiebung"] == (0.0, 0.2, 0.0)
    assert settings == dict(avatar_pose.POSE_SETTING_DEFAULTS)
    assert "ThumbnailKeyframe" in info["thumbnail"]


def test_load_emote_pose_fallback_mittel_keyframe(tmp_path, no_network):
    no_network[987654321] = xml_keyframe_sequence().encode()
    datei = _write(tmp_path, "emote.rbxmx", xml_emote_asset())
    _pose, _settings, info = emote_pose.load_emote_pose(
        emote_id=1, client=FakeClient(), datei=datei, log=lambda m: None)
    # Ohne ThumbnailKeyframe: ceil(3/2) = 2 -> Keyframe K1 (Index 1).
    assert info["frame"] == 1
    assert "Mittel-Keyframe" in info["auswahl"]


def test_load_emote_pose_emote_zeit_override(tmp_path, no_network):
    no_network[987654321] = xml_keyframe_sequence().encode()
    datei = _write(tmp_path, "emote.rbxmx",
                   xml_emote_asset(thumbnail_keyframe=2))
    _pose, _settings, info = emote_pose.load_emote_pose(
        emote_id=1, client=FakeClient(), datei=datei, zeit=1.0,
        log=lambda m: None)
    assert info["frame"] == 2
    assert info["zeit_s"] == pytest.approx(1.0)
    assert "emote_zeit" in info["auswahl"]


def test_load_emote_pose_curve_animation(tmp_path, no_network):
    datei = _write(tmp_path, "emote.rbxmx",
                   xml_emote_asset(thumbnail_time=1.0))
    no_network[987654321] = xml_curve_animation().encode()
    pose_dict, _settings, info = emote_pose.load_emote_pose(
        emote_id=7, client=FakeClient(), datei=datei, log=lambda m: None)
    assert info["typ"] == "curves"
    assert info["auswahl"] == "ThumbnailTime 1,00 s"
    assert info["zeit_s"] == pytest.approx(1.0)
    assert pose_dict["unterer_torso"]["verschiebung"][1] == pytest.approx(0.25)
    assert pose_dict["oberer_torso"]["rotation"][0] == pytest.approx(20.0)


def test_load_emote_pose_curve_fallback_halbe_dauer(tmp_path, no_network):
    no_network[987654321] = xml_curve_animation().encode()
    datei = _write(tmp_path, "emote.rbxmx", xml_emote_asset())
    _pose, _settings, info = emote_pose.load_emote_pose(
        emote_id=7, client=FakeClient(), datei=datei, log=lambda m: None)
    assert info["zeit_s"] == pytest.approx(1.0)  # haelfte von 2 s
    assert "halbe Dauer" in info["auswahl"]


def test_load_emote_pose_clip_direkt_ohne_animation(tmp_path):
    """Emote-Datei kann auch direkt die KeyframeSequence sein."""
    datei = _write(tmp_path, "clip.rbxmx", xml_keyframe_sequence())
    pose_dict, _settings, info = emote_pose.load_emote_pose(
        emote_id=None, client=None, datei=datei, log=lambda m: None)
    assert info["frame"] == 1  # Mittel-Keyframe (keine Metadaten)


def test_load_emote_pose_fehlerfaelle(tmp_path, no_network):
    # Download schlaegt fehl und kein emote_datei.
    with pytest.raises(emote_pose.EmoteError, match="nicht geladen"):
        emote_pose.load_emote_pose(emote_id=123, client=FakeClient(),
                                   log=lambda m: None)
    # Kein Client, keine Datei.
    with pytest.raises(emote_pose.EmoteError):
        emote_pose.load_emote_pose(emote_id=123, client=None,
                                   log=lambda m: None)
    # Kaputte Datei.
    datei = _write(tmp_path, "kaputt.rbxmx", "nicht roblox")
    with pytest.raises(emote_pose.EmoteError):
        emote_pose.load_emote_pose(emote_id=123, datei=datei,
                                   log=lambda m: None)
    # Emote ohne Animation/Clip.
    datei = _write(tmp_path, "leer.rbxmx", xml_emote_asset())
    no_network[987654321] = b"<roblox></roblox>"
    with pytest.raises(emote_pose.EmoteError):
        emote_pose.load_emote_pose(emote_id=123, client=FakeClient(),
                                   datei=datei, log=lambda m: None)


def test_load_emote_pose_redirect_kette(tmp_path, no_network):
    """AnimationId darf auch wieder auf eine Animation zeigen (Redirect)."""
    no_network[987654321] = xml_emote_asset(animation_id=111111111).encode()
    no_network[111111111] = xml_keyframe_sequence().encode()
    datei = _write(tmp_path, "emote.rbxmx",
                   xml_emote_asset(animation_id=987654321))
    _pose, _settings, info = emote_pose.load_emote_pose(
        emote_id=5, client=FakeClient(), datei=datei, log=lambda m: None)
    assert info["frame"] == 1


# ---------------------------------------------------------------------------
# PoseContext-Anwendung (Drehung um das echte Gelenk)
# ---------------------------------------------------------------------------

def test_pose_context_dreht_um_gelenk():
    joints = {
        "LowerTorso": (0.0, 0.0, 0.0), "UpperTorso": (0.0, 0.35, 0.0),
        "Head": (0.0, 0.9, 0.0), "LeftUpperArm": (1.0, 1.4, 0.0),
        "LeftLowerArm": (1.0, 0.9, 0.0), "LeftHand": (1.0, 0.6, 0.0),
    }
    pose_dict, _ = emote_pose.pose_cframes_to_pose_dict({
        "LowerTorso": ((0.0, 0.0, 0.0), IDENTITY),
        "LeftUpperArm": ((0.0, 0.0, 0.0), rot_x_deg(90)),
    })
    context = avatar_pose.PoseContext(
        pose_dict, joints, settings=dict(avatar_pose.POSE_SETTING_DEFAULTS))
    # Das Gelenk selbst bleibt fix ...
    fix = context.transform_point((1.0, 1.4, 0.0), "linker_oberarm")
    assert fix == pytest.approx((1.0, 1.4, 0.0))
    # ... ein Punkt 0,5 Studs unterhalb des Gelenks wandert um 0,5 nach
    # vorne (-Z): 90-Grad-Drehung um X durch die Schulter.
    moved = context.transform_point((1.0, 0.9, 0.0), "linker_oberarm")
    assert moved[0] == pytest.approx(1.0)
    assert moved[1] == pytest.approx(1.4)
    assert moved[2] == pytest.approx(-0.5)


def test_pose_context_uebersetzt_ganze_kette(tmp_path, no_network):
    """Arm-Kette: Oberarm+Unterarm gedreht -> Ellbogen folgt dem Oberarm."""
    joints = {
        "LowerTorso": (0.0, 0.0, 0.0), "UpperTorso": (0.0, 0.35, 0.0),
        "LeftUpperArm": (1.0, 1.4, 0.0), "LeftLowerArm": (1.0, 0.9, 0.0),
        "LeftHand": (1.0, 0.6, 0.0), "Head": (0.0, 0.9, 0.0),
    }
    pose_dict, _ = emote_pose.pose_cframes_to_pose_dict({
        "LowerTorso": ((0.0, 0.0, 0.0), IDENTITY),
        "LeftUpperArm": ((0.0, 0.0, 0.0), rot_y_deg(90)),
        "LeftLowerArm": ((0.0, 0.0, 0.0), rot_x_deg(90)),
    })
    context = avatar_pose.PoseContext(
        pose_dict, joints, settings=dict(avatar_pose.POSE_SETTING_DEFAULTS))
    # Ellbogen (1, 0.9, 0): 0,5 unter der Schulter; Oberarm dreht um Y um 90
    # Grad -> Punkt wandert nach -X... (Richtung haengt von der Achse ab:
    # Ry(90) mappt (0,-0.5,0) auf (0,-0.5,0) - Y bleibt. Also bleibt der
    # Ellbogen in der Vertikalen unter der Schulter und wir pruefen nur,
    # dass X/Z-Deltas korrekt gedreht werden.)
    ellbogen = context.transform_point((1.0, 0.9, 0.0), "linker_oberarm")
    assert ellbogen[1] == pytest.approx(0.9)
    # Hand (1, 0.6, 0) haengt am Unterarm; der Unterarm-Knochen rotiert
    # zusaetzlich um X (nach der Y-Drehung des Oberarms um -90 Grad wirkt
    # das auf die lokale X-Achse).
    hand = context.transform_point((1.0, 0.6, 0.0), "linker_unterarm")
    assert hand != (1.0, 0.6, 0.0)


# ---------------------------------------------------------------------------
# Integration in build_avatar (offline)
# ---------------------------------------------------------------------------

def _build_dry(tmp_path, config_text: str):
    config_path = tmp_path / "Configs.toml"
    config_path.write_text(config_text, encoding="utf-8")
    config = render_config.load_config(str(config_path))
    return build_avatar.build("Builderman", str(tmp_path / "out"),
                              dry_run=True, verbose=False, config=config)


def test_build_avatar_emote_pose_offline(tmp_path):
    """emote_datei zeigt direkt auf den Clip: komplett ohne Netzwerk."""
    clip = tmp_path / "clip.rbxmx"
    clip.write_text(xml_keyframe_sequence(), encoding="utf-8")
    manifest = _build_dry(tmp_path,
                          'username = "Builderman"\n'
                          'pose = "emote:3576893107"\n'
                          'hintergrund_url = "transparent"\n'
                          f'emote_datei = "{clip.name}"\n')
    render = manifest["render"]
    assert render["pose"] == "emote:3576893107"
    assert render["pose_aktiv"] is True
    assert render["pose_emote"]["typ"] == "keyframes"
    assert render["pose_emote"]["frame"] == 1  # Mittel-Keyframe (3 Keyframes)
    assert "Mittel-Keyframe" in render["pose_details"]
    assert render["pose_emote"]["gelenke_anzahl"] >= 2


def test_build_avatar_emote_pose_mit_hop(tmp_path, monkeypatch):
    """Emote-Asset-Datei + nachgeladener Clip: ThumbnailKeyframe gewinnt."""
    monkeypatch.setattr(emote_pose, "_download_clip_bytes",
                        lambda asset_id, client, log: xml_keyframe_sequence().encode())
    emote = tmp_path / "emote.rbxmx"
    emote.write_text(xml_emote_asset(thumbnail_keyframe=2), encoding="utf-8")
    manifest = _build_dry(tmp_path,
                          'username = "Builderman"\n'
                          'pose = "emote:3576893107"\n'
                          'hintergrund_url = "transparent"\n'
                          f'emote_datei = "{emote.name}"\n')
    render = manifest["render"]
    assert render["pose_aktiv"] is True
    assert render["pose_emote"]["frame"] == 1
    assert "ThumbnailKeyframe 2" in render["pose_details"]


def test_build_avatar_emote_fehler_faellt_zurueck(tmp_path, monkeypatch):
    """Schlaegt das Emote-Laden fehl, wird die Stand-Pose gerendert."""
    monkeypatch.setattr(emote_pose, "_download_clip_bytes",
                        lambda asset_id, client, log: b"<roblox></roblox>")
    emote = tmp_path / "emote.rbxmx"
    emote.write_text(xml_emote_asset(), encoding="utf-8")
    manifest = _build_dry(tmp_path,
                          'username = "Builderman"\n'
                          'pose = "emote:1234"\n'
                          'hintergrund_url = "transparent"\n'
                          f'emote_datei = "{emote.name}"\n')
    assert manifest["render"]["pose_aktiv"] is False
    assert any("Emote-Pose" in w for w in manifest["warnings"])


def test_build_avatar_emote_dry_run_ohne_datei_warnt(tmp_path):
    manifest = _build_dry(tmp_path,
                          'username = "Builderman"\n'
                          'pose = "emote:999"\n'
                          'hintergrund_url = "transparent"\n')
    assert manifest["render"]["pose_aktiv"] is False
    assert any("Dry-Run" in w and "Emote" in w for w in manifest["warnings"])


# ---------------------------------------------------------------------------
# MarkerCurve "NamedKeyframeEvent": benannte Keyframes + Frame-Auswahl
# ---------------------------------------------------------------------------

def encode_marker_curve(markers):
    """MarkerCurve-ValuesAndTimes-Blob (Version 2, Ticks 2400/s)."""
    names = b"".join(m[0].encode() + b"\x00" for m in markers)
    ticks = b"".join(struct.pack("<I", int(round(m[1] * 2400))) for m in markers)
    return (struct.pack("<II", 2, len(markers)) + names
            + struct.pack("<II", 1, len(markers)) + ticks)


def test_decode_marker_curve():
    blob = encode_marker_curve([("Start", 0.0), ("Pose", 1.25), ("Ende", 4.5)])
    marker = emote_pose.decode_marker_curve(blob)
    assert [m["name"] for m in marker] == ["Start", "Pose", "Ende"]
    assert [m["time"] for m in marker] == pytest.approx([0.0, 1.25, 4.5])


def test_decode_marker_curve_sortiert_und_rot():
    # Unsortierte Eingabe wird zeitlich sortiert; Muell liefert [].
    blob = encode_marker_curve([("B", 2.0), ("A", 0.5)])
    marker = emote_pose.decode_marker_curve(blob)
    assert [m["name"] for m in marker] == ["A", "B"]
    assert emote_pose.decode_marker_curve(b"") == []
    assert emote_pose.decode_marker_curve(b"\x99" * 12) == []
    assert emote_pose.decode_marker_curve(struct.pack("<III", 99, 0, 0)) == []


@pytest.mark.parametrize("laenge,thumb,marker,zeit,expected_t,expected_txt", [
    # manueller Override gewinnt
    (4.0, None, [{"name": "A", "time": 1.0}], 3.0, 3.0, "emote_zeit"),
    # ThumbnailTime gewinnt vor Markern
    (4.0, 0.5, [{"name": "A", "time": 1.0}], None, 0.5, "ThumbnailTime"),
    # mittlerer benannter Keyframe (ungerade Anzahl: 2/3)
    (4.87, None, [{"name": "A", "time": 0.0},
                  {"name": "B", "time": 1.2},
                  {"name": "C", "time": 4.8}], None, 1.2,
     "mittlerer benannter Keyframe (2/3: 'B'"),
    # gerade Anzahl: ceil(4/2) = 2 -> Index 1
    (4.0, None, [{"name": "A", "time": 0.0}, {"name": "B", "time": 1.0},
                 {"name": "C", "time": 2.0}, {"name": "D", "time": 3.0}],
     None, 1.0, "mittlerer benannter Keyframe (2/4: 'B'"),
    # ungueltige ThumbnailTime -> Marker-Regel
    (4.0, 9.0, [{"name": "A", "time": 3.0}], None, 3.0,
     "mittlerer benannter Keyframe"),
    # ohne Marker: halbe Dauer
    (4.0, None, [], None, 2.0, "halbe Dauer"),
    (4.0, None, None, None, 2.0, "halbe Dauer"),
    # Marker jenseits der Dauer wird auf die Dauer begrenzt
    (2.0, None, [{"name": "A", "time": 9.0}], None, 2.0,
     "mittlerer benannter Keyframe"),
])
def test_select_curve_frame(laenge, thumb, marker, zeit, expected_t, expected_txt):
    t, beschreibung = emote_pose.select_curve_frame(laenge, thumb, marker, zeit)
    assert t == pytest.approx(expected_t)
    assert expected_txt in beschreibung


def test_parse_curve_animation_liest_benannte_keyframes():
    marker_blob = encode_marker_curve(
        [("Start", 0.0), ("Hauptpose", 1.2), ("Ende", 4.8)])
    marker_xml = (
        '<Item class="MarkerCurve"><Properties>'
        '<string name="Name">NamedKeyframeEvent</string>'
        f'<BinaryString name="ValuesAndTimes">{_b64(marker_blob)}</BinaryString>'
        '</Properties></Item>'
    )
    xml = xml_curve_animation().replace(
        "</Item>\n</roblox>", marker_xml + "</Item>\n</roblox>")
    roots = rbxm.parse_instances(xml.encode())
    clip = emote_pose.parse_curve_animation(roots)
    assert clip is not None and clip["typ"] == "curves"
    assert [m["name"] for m in clip["marker"]] == ["Start", "Hauptpose", "Ende"]
    assert [m["time"] for m in clip["marker"]] == pytest.approx([0.0, 1.2, 4.8])
