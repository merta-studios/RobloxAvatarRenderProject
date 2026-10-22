"""Tests fuer Configs.toml (render_config) und Posen (avatar_pose).

Geprueft werden:
  * Laden/Pruefen der Config samt Grenzen (Drehung -90..90, Freiraum).
  * Der Render-Schalter wird korrekt gelesen und zurueckgesetzt.
  * Posen aus Posen.toml sind gueltig und enthalten Beispiel-Posen.
  * Posen werden auf das Rig angewendet (Drehung um das richtige Gelenk,
    Accessoires folgen, Stand-Pose bleibt unveraendert).
"""

from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
ROOT = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)

import avatar_pose  # noqa: E402
import render_config  # noqa: E402
import roblox_mesh  # noqa: E402

CONFIG = os.path.join(ROOT, "Configs.toml")
POSES = os.path.join(ROOT, "Posen.toml")


def _write_config(tmp_path, **overrides):
    lines = [
        'username = "Builderman"',
        "drehung_grad = 0",
        'pose = "None"',
        "freiraum_oben_prozent = 6",
        "freiraum_unten_prozent = 6",
        "render_bei_aenderung = false",
        'hintergrund_url = "transparent"',
        "hintergrund_glossy = false",
    ]
    for key, value in overrides.items():
        lines = [l for l in lines if not l.startswith(key + " ")]
        if isinstance(value, str):
            value = f'"{value}"'
        elif isinstance(value, bool):
            value = "true" if value else "false"
        lines.append(f"{key} = {value}")
    path = tmp_path / "Configs.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Configs.toml
# ---------------------------------------------------------------------------

def test_repo_config_ist_gueltig():
    config = render_config.load_config(CONFIG)
    assert config["username"]
    assert -90 <= config["drehung_grad"] <= 90
    assert config["_pfad"].endswith("Configs.toml")
    assert config["_posen_pfad"].endswith("Posen.toml")


def test_config_liest_alle_werte_typisiert(tmp_path):
    path = _write_config(
        tmp_path,
        drehung_grad=25,
        pose="Winken",
        freiraum_oben_prozent=-12.5,
        freiraum_unten_prozent=-40,
        render_bei_aenderung=True,
        hintergrund_url="https://example.com/bild.png",
        hintergrund_glossy=True,
    )
    config = render_config.load_config(str(path))
    assert config["drehung_grad"] == 25
    assert config["pose"] == "Winken"
    assert config["freiraum_oben_prozent"] == -12.5
    assert config["freiraum_unten_prozent"] == -40
    assert config["render_bei_aenderung"] is True
    assert config["hintergrund_glossy"] is True
    assert config["_transparent"] is False


@pytest.mark.parametrize("value", [91, -91, 180])
def test_drehung_ausserhalb_90_grad_wird_abgelehnt(tmp_path, value):
    path = _write_config(tmp_path, drehung_grad=value)
    with pytest.raises(render_config.ConfigError, match="drehung_grad"):
        render_config.load_config(str(path))


def test_zu_viel_freiraum_wird_abgelehnt(tmp_path):
    path = _write_config(tmp_path, freiraum_oben_prozent=60,
                         freiraum_unten_prozent=60)
    with pytest.raises(render_config.ConfigError, match="hoechstens"):
        render_config.load_config(str(path))


def test_negative_freiraumwerte_sind_erlaubt(tmp_path):
    path = _write_config(tmp_path, freiraum_oben_prozent=-10,
                         freiraum_unten_prozent=-40)
    config = render_config.load_config(str(path))
    assert config["freiraum_unten_prozent"] == -40


def test_ungueltiger_schalter_wird_abgelehnt(tmp_path):
    path = _write_config(tmp_path)
    text = path.read_text(encoding="utf-8").replace(
        "render_bei_aenderung = false", 'render_bei_aenderung = "vielleicht"')
    path.write_text(text, encoding="utf-8")
    with pytest.raises(render_config.ConfigError, match="true oder false"):
        render_config.load_config(str(path))


def test_unbekannter_schluessel_warnt_statt_abzubrechen(tmp_path):
    path = _write_config(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + "tippfehler = 1\n",
                    encoding="utf-8")
    config = render_config.load_config(str(path))
    assert any("tippfehler" in w for w in config["_warnungen"])


def test_reset_render_flag_erhaelt_kommentare(tmp_path):
    path = _write_config(tmp_path, render_bei_aenderung=True)
    text = path.read_text(encoding="utf-8")
    assert "render_bei_aenderung = true" in text
    assert render_config.reset_render_flag(str(path)) is True
    after = path.read_text(encoding="utf-8")
    assert "render_bei_aenderung = false" in after
    assert after.count("\n") == text.count("\n")
    # Zweiter Aufruf aendert nichts mehr.
    assert render_config.reset_render_flag(str(path)) is False


def test_github_output_enthaelt_username_und_schalter(tmp_path):
    path = _write_config(tmp_path, render_bei_aenderung=True)
    config = render_config.load_config(str(path))
    lines = render_config.as_github_output(config).strip().splitlines()
    assert "username=Builderman" in lines
    assert "render_jetzt=true" in lines


# ---------------------------------------------------------------------------
# Posen
# ---------------------------------------------------------------------------

def test_repo_posen_laden_und_haben_beispiele():
    poses = avatar_pose.load_poses(POSES)
    assert {"Winken", "T_Pose", "Laufen", "Sitzen", "Jubel"} <= set(poses)
    for name, entry in poses.items():
        assert isinstance(entry["beschreibung"], str)
        assert entry["teile"], f"Pose {name} ist leer"
        for part, spec in entry["teile"].items():
            assert part in avatar_pose.BODY_PART_KEYS
            assert len(spec["rotation"]) == 3
            assert len(spec["verschiebung"]) == 3


def test_unbekanntes_koerperteil_wird_gemeldet(tmp_path):
    path = tmp_path / "Posen.toml"
    path.write_text(
        '[Kaputt]\n'
        '[Kaputt.rechter_fussball]\n'
        'rotation = [0, 0, 10]\n', encoding="utf-8")
    with pytest.raises(avatar_pose.PoseError, match="unbekanntes Koerperteil"):
        avatar_pose.load_poses(str(path))


def test_zu_grosse_verschiebung_wird_gemeldet(tmp_path):
    path = tmp_path / "Posen.toml"
    path.write_text(
        '[Kaputt]\n'
        '[Kaputt.kopf]\n'
        'rotation = [0, 0, 0]\n'
        'verschiebung = [0, 99, 0]\n', encoding="utf-8")
    with pytest.raises(avatar_pose.PoseError, match="verschiebung"):
        avatar_pose.load_poses(str(path))


def _arm_joints():
    return {
        "LowerTorso": (0.0, 0.0, 0.0),
        "UpperTorso": (0.0, 2.0, 0.0),
        "Head": (0.0, 4.0, 0.0),
        "RightUpperArm": (1.0, 4.0, 0.0),
        "RightLowerArm": (1.0, 2.6, 0.0),
        "RightHand": (1.0, 1.6, 0.0),
        "LeftUpperArm": (-1.0, 4.0, 0.0),
        "LeftLowerArm": (-1.0, 2.6, 0.0),
        "LeftHand": (-1.0, 1.6, 0.0),
        "RightUpperLeg": (0.5, 2.0, 0.0),
        "RightLowerLeg": (0.5, 1.0, 0.0),
        "RightFoot": (0.5, 0.2, 0.0),
        "LeftUpperLeg": (-0.5, 2.0, 0.0),
        "LeftLowerLeg": (-0.5, 1.0, 0.0),
        "LeftFoot": (-0.5, 0.2, 0.0),
    }


def _arm_mesh():
    return roblox_mesh.make_box(1.0, 2.0, 1.0, (1.0, 3.0, 0.0))


def test_t_pose_hebt_den_arm_um_die_schulter():
    joints = _arm_joints()
    pose = {"rechter_oberarm": {"rotation": (0.0, 0.0, 90.0),
                                "verschiebung": (0.0, 0.0, 0.0)}}
    context = avatar_pose.PoseContext(pose, joints)
    posed = context.apply_body("rechts_arm", _arm_mesh())
    lo, hi = posed.bbox()

    # Der Arm steht jetzt waagerecht: breit in X, duenn in Y, beginnt an der
    # Schulter (x = 1.0) und reicht bis etwa 1 + 2 = 3.
    assert hi[0] > 2.4
    assert hi[1] - lo[1] < 1.2
    assert lo[0] == pytest.approx(1.0, abs=0.1)


def test_neutrale_pose_laesst_das_mesh_unveraendert():
    mesh = _arm_mesh()
    context = avatar_pose.PoseContext({}, _arm_joints())
    result = context.apply_body("rechts_arm", mesh)
    assert result.positions == mesh.positions
    assert context.active is False


def test_kopf_drehung_ist_starr_um_das_halsgelenk():
    joints = _arm_joints()
    pose = {"kopf": {"rotation": (0.0, 90.0, 0.0),
                     "verschiebung": (0.0, 0.0, 0.0)}}
    context = avatar_pose.PoseContext(pose, joints)
    head = roblox_mesh.make_box(1.0, 1.0, 1.0, (0.0, 4.5, 0.0))
    posed = context.apply_body("kopf", head)
    # 90 Grad um die Hochachse: der Kopf bleibt auf seiner Hoehe, dreht sich
    # aber um das Halsgelenk (y = 4.0).
    assert posed.center()[1] == pytest.approx(4.5, abs=1e-6)
    assert posed.center()[0] == pytest.approx(0.0, abs=0.3)


def test_accessoire_folgt_dem_kopf():
    joints = _arm_joints()
    pose = {"kopf": {"rotation": (0.0, 0.0, 0.0),
                     "verschiebung": (0.0, 0.5, 0.0)}}
    context = avatar_pose.PoseContext(pose, joints)
    hat = roblox_mesh.make_box(1.0, 0.5, 1.0, (0.0, 5.0, 0.0))
    posed = context.apply_accessory(hat, anchor="head_top")
    assert posed.center()[1] == pytest.approx(5.5, abs=1e-6)


def test_unterarm_dreht_nur_den_unteren_teil():
    joints = _arm_joints()
    pose = {"rechter_unterarm": {"rotation": (40.0, 0.0, 0.0),
                                 "verschiebung": (0.0, 0.0, 0.0)}}
    context = avatar_pose.PoseContext(pose, joints)
    mesh = _arm_mesh()
    posed = context.apply_body("rechts_arm", mesh)
    lo, hi = posed.bbox()
    # Die Schulter oben bleibt (fast) unveraendert, der Unterarm schwingt nach
    # vorne (-Z).
    assert hi[1] == pytest.approx(4.0, abs=0.08)
    assert lo[2] < -0.3


def test_hintergrund_sonderwerte(tmp_path):
    hdri = render_config.load_config(str(_write_config(tmp_path, hintergrund_url="hdri")))
    assert hdri["_transparent"] is False
    assert hdri["_hdri"] is True

    transparent = render_config.load_config(
        str(_write_config(tmp_path, hintergrund_url="transparent")))
    assert transparent["_transparent"] is True
    assert transparent["_hdri"] is False

    bild = render_config.load_config(
        str(_write_config(tmp_path, hintergrund_url="https://example.com/bg.jpg")))
    assert bild["_transparent"] is False
    assert bild["_hdri"] is False
