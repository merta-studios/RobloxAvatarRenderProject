"""Tests fuer die Option "ausgeruestete Pose des Accounts".

``pose = "ausgeruestet"`` laedt die Pose, die der Account selbst traegt:

1. ausgeruestete ``PoseAnimation`` (56) bzw. ``IdleAnimation`` (51) -
   das ist die "Pose" aus dem Roblox-Avatar-Editor (erster Frame).
2. sonst das ausgeruestete Emote (Slot 1, danach die weiteren Slots) -
   daraus der Frame, den Roblox fuer Katalog-/Profilbilder benutzt.

    python3 -m pytest tools/tests/test_equipped_pose.py -q
"""

from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
ROOT = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)

import avatar_pose  # noqa: E402
import build_avatar  # noqa: E402
import emote_pose  # noqa: E402
import render_config  # noqa: E402
import roblox_api as api  # noqa: E402


# ---------------------------------------------------------------------------
# Referenz-Parsing / Config-Normalisierung
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("ausgeruestet", "auto"),
    ("ausgerüstet", "auto"),
    ("AUSGERUESTET", "auto"),
    ("equipped", "auto"),
    ("auto", "auto"),
    ("Profil", "auto"),
    ("getragen", "auto"),
    ("ausgeruestet:emote", "emote"),
    ("ausgeruestet = idle", "idle"),
    ("ausgeruestet:pose", "idle"),
    ("ausgeruestet:foo", "auto"),
    ("Helden_Pose", None),
    ("emote:123", None),
    ("None", None),
    ("", None),
])
def test_parse_equipped_reference(value, expected):
    assert emote_pose.parse_equipped_reference(value) == expected


@pytest.mark.parametrize("raw,expected", [
    ("ausgeruestet", "ausgeruestet"),
    ("AUSGERÜSTET", "ausgeruestet"),
    ("equipped", "ausgeruestet"),
    ("ausgeruestet:emote", "ausgeruestet:emote"),
    ("ausgeruestet = idle", "ausgeruestet:idle"),
    ("Helden_Pose", "Helden_Pose"),
    ("emote:3576893107", "emote:3576893107"),
    ("None", "None"),
])
def test_config_normalisiert_ausgeruestete_pose(raw, expected):
    assert render_config.normalize_pose_value(raw) == expected


def test_config_lehnt_unbekannte_quelle_ab():
    with pytest.raises(render_config.ConfigError):
        render_config.normalize_pose_value("ausgeruestet:irgendwas")


# ---------------------------------------------------------------------------
# Kandidaten aus den Avatar-Daten
# ---------------------------------------------------------------------------

def _avatar(**over):
    base = {
        "playerAvatarType": "R15",
        "scales": dict(api.DEFAULT_SCALES),
        "bodyColors": dict(api.DEFAULT_BODY_COLORS),
        "assets": [],
        "emotes": [],
    }
    base.update(over)
    return base


def test_pose_animation_hat_vorrang_vor_idle_und_emote():
    avatar = _avatar(
        assets=[
            {"id": 111, "name": "Ninja Idle", "assetType": {"id": 51}},
            {"id": 222, "name": "Coole Pose", "assetType": {"id": 56}},
            {"id": 333, "name": "Walk", "assetType": {"id": 55}},
        ],
        emotes=[{"assetId": 900, "assetName": "Winken", "position": 1}],
    )
    candidates = api.equipped_pose_candidates(avatar)
    assert [c["asset_id"] for c in candidates] == [222, 111, 900]
    assert candidates[0]["kind"] == "pose"
    assert candidates[1]["kind"] == "idle"
    assert candidates[2]["kind"] == "emote"


def test_emotes_kommen_nach_position_sortiert():
    avatar = _avatar(emotes=[
        {"assetId": 30, "assetName": "C", "position": 3},
        {"assetId": 10, "assetName": "A", "position": 1},
        {"assetId": 20, "assetName": "B", "position": 2},
    ])
    candidates = api.equipped_pose_candidates(avatar)
    assert [c["asset_id"] for c in candidates] == [10, 20, 30]
    assert candidates[0]["source"] == "Emote-Slot 1"


def test_bewegungsclips_sind_keine_pose():
    avatar = _avatar(assets=[
        {"id": 1, "name": "Run", "assetType": {"id": 53}},
        {"id": 2, "name": "Jump", "assetType": {"id": 52}},
    ])
    assert api.equipped_pose_candidates(avatar) == []


def test_magerer_account_warnt_ohne_absturz():
    warnings = []
    assert api.equipped_pose_candidates(_avatar(), warnings) == []
    assert warnings and "Stand-Pose" in warnings[0]


def test_fixture_avatar_liefert_kandidaten():
    with open(os.path.join(HERE, "fixtures", "merta_avatar.json"), encoding="utf-8") as fh:
        avatar = json.load(fh)
    candidates = api.equipped_pose_candidates(avatar)
    assert candidates, "MertaStudios traegt Idle-Animation und Emotes"
    assert candidates[0]["kind"] in ("pose", "idle")
    assert any(c["kind"] == "emote" for c in candidates)


# ---------------------------------------------------------------------------
# load_equipped_pose: Frame-Wahl, Kandidaten-Reihenfolge, Fehlerfaelle
# ---------------------------------------------------------------------------

def _fake_clip(asset_id, *args, **kwargs):
    """Attrappe fuer load_emote_pose: liefert eine winzige echte Pose."""
    pose = {"linker_oberarm": {"rotation": (0.0, 0.0, 10.0),
                               "verschiebung": (0.0, 0.0, 0.0),
                               "felder": {"vom_koerper_weg_grad": 10.0}}}
    settings = dict(avatar_pose.POSE_SETTING_DEFAULTS)
    info = {"emote_id": asset_id, "name": f"Clip {asset_id}", "typ": "keyframes",
            "keyframes": 3, "frame": 0, "zeit_s": 0.0, "gelenke": 1}
    return pose, settings, info


def test_load_equipped_pose_nutzt_idle_frame_null(monkeypatch):
    calls = []

    def fake(emote_id=None, client=None, datei="", zeit=None, log=None):
        calls.append((emote_id, zeit, datei))
        return _fake_clip(emote_id)

    monkeypatch.setattr(emote_pose, "load_emote_pose", fake)
    avatar = _avatar(assets=[{"id": 222, "name": "Pose", "assetType": {"id": 56}}],
                     emotes=[{"assetId": 900, "assetName": "Winken", "position": 1}])
    pose, settings, info = emote_pose.load_equipped_pose(avatar=avatar, log=lambda m: None)
    assert calls and calls[0][0] == 222
    # Pose-/Idle-Animationen: erster Frame = Standpose.
    assert calls[0][1] == 0.0
    assert info["equipped"]["kind"] == "pose"
    assert "ausgeruestet" in emote_pose.emote_summary(info)
    assert settings is avatar_pose.POSE_SETTING_DEFAULTS or settings


def test_load_equipped_pose_nutzt_emote_thumbnail_regel(monkeypatch):
    calls = []

    def fake(emote_id=None, client=None, datei="", zeit=None, log=None):
        calls.append((emote_id, zeit))
        return _fake_clip(emote_id)

    monkeypatch.setattr(emote_pose, "load_emote_pose", fake)
    avatar = _avatar(emotes=[{"assetId": 900, "assetName": "Winken", "position": 1}])
    emote_pose.load_equipped_pose(avatar=avatar, log=lambda m: None)
    # Emote: None = Roblox-Thumbnail-Regel (Vorschau-Frame).
    assert calls == [(900, None)]


def test_load_equipped_pose_erzwingt_quelle(monkeypatch):
    calls = []

    def fake(emote_id=None, client=None, datei="", zeit=None, log=None):
        calls.append(emote_id)
        return _fake_clip(emote_id)

    monkeypatch.setattr(emote_pose, "load_emote_pose", fake)
    avatar = _avatar(assets=[{"id": 222, "assetType": {"id": 56}}],
                     emotes=[{"assetId": 900, "position": 1}])
    emote_pose.load_equipped_pose(avatar=avatar, prefer="emote", log=lambda m: None)
    assert calls == [900]


def test_load_equipped_pose_ueberspringt_kaputten_kandidaten(monkeypatch):
    calls = []

    def fake(emote_id=None, client=None, datei="", zeit=None, log=None):
        calls.append(emote_id)
        if emote_id == 222:
            raise emote_pose.EmoteError("R6-Animation ohne R15-Gelenke")
        return _fake_clip(emote_id)

    monkeypatch.setattr(emote_pose, "load_emote_pose", fake)
    avatar = _avatar(assets=[{"id": 222, "assetType": {"id": 51}}],
                     emotes=[{"assetId": 900, "position": 1}])
    pose, settings, info = emote_pose.load_equipped_pose(avatar=avatar, log=lambda m: None)
    assert calls == [222, 900]
    assert info["equipped"]["asset_id"] == 900


def test_load_equipped_pose_ohne_kandidaten_meldet_klar():
    with pytest.raises(emote_pose.EmoteError) as excinfo:
        emote_pose.load_equipped_pose(avatar=_avatar(), log=lambda m: None)
    assert "ausgeruestet" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# Integration in build_avatar.setup_pose
# ---------------------------------------------------------------------------

def _body_parts():
    colors = dict(api.DEFAULT_BODY_COLORS)
    parts, metrics = build_avatar.avatar_rig.build_body(colors, dict(api.DEFAULT_SCALES))
    return parts, metrics


def test_setup_pose_laedt_ausgeruestete_pose(monkeypatch):
    seen = {}

    def fake_load_equipped_pose(**kwargs):
        seen.update(kwargs)
        pose, settings, info = _fake_clip(900)
        info["equipped"] = {"asset_id": 900, "kind": "emote",
                            "source": "Emote-Slot 1", "name": "Winken"}
        return pose, settings, info

    monkeypatch.setattr(emote_pose, "load_equipped_pose", fake_load_equipped_pose)
    parts, metrics = _body_parts()
    warnings = []
    config = {"pose": "ausgeruestet", "emote_zeit": -1}
    avatar = _avatar(emotes=[{"assetId": 900, "assetName": "Winken", "position": 1}])
    ctx, info = build_avatar.setup_pose(
        config, parts, metrics, None, False, warnings,
        client=None, dry_run=False, avatar=avatar, user_id=1234)
    assert ctx is not None and ctx.active
    assert info["name"] == "ausgeruestet"
    assert info["equipped"]["asset_id"] == 900
    assert seen["avatar"] is avatar
    assert seen["prefer"] == "auto"


def test_setup_pose_dry_run_laedt_keine_pose(monkeypatch):
    def fail(**kwargs):  # pragma: no cover - darf nicht aufgerufen werden
        raise AssertionError("Dry-Run darf nichts herunterladen")

    monkeypatch.setattr(emote_pose, "load_equipped_pose", fail)
    parts, metrics = _body_parts()
    warnings = []
    ctx, info = build_avatar.setup_pose(
        {"pose": "ausgeruestet"}, parts, metrics, None, False, warnings,
        client=None, dry_run=True, avatar=_avatar())
    assert ctx is None
    assert any("Dry-Run" in w for w in warnings)


def test_setup_pose_faellt_bei_fehler_auf_stand_pose(monkeypatch):
    def broken(**kwargs):
        raise emote_pose.EmoteError("kein Netz")

    monkeypatch.setattr(emote_pose, "load_equipped_pose", broken)
    parts, metrics = _body_parts()
    warnings = []
    ctx, info = build_avatar.setup_pose(
        {"pose": "ausgeruestet"}, parts, metrics, None, False, warnings,
        client=None, dry_run=False, avatar=_avatar())
    assert ctx is None
    assert any("Stand-Pose" in w for w in warnings)


def test_repo_posen_datei_kennt_keine_ausgeruestete_pose():
    """Sanity: "ausgeruestet" ist ein Sonderwert, kein Posen.toml-Name."""
    poses = avatar_pose.load_poses(os.path.join(ROOT, "Posen.toml"))
    assert "ausgeruestet" not in poses


# ---------------------------------------------------------------------------
# Fehlende Koerperteile: Standard-Avatar, kein Paket-Nachladen
# ---------------------------------------------------------------------------

def _woman_assets(with_legs=False):
    assets = [
        {"id": 86499666, "name": "Woman Torso",
         "assetType": {"id": 27, "name": "Torso"}},
        {"id": 86499698, "name": "Woman Right Arm",
         "assetType": {"id": 28, "name": "RightArm"}},
        {"id": 86499716, "name": "Woman Left Arm",
         "assetType": {"id": 29, "name": "LeftArm"}},
    ]
    if with_legs:
        assets += [
            {"id": 86499753, "name": "Woman Left Leg",
             "assetType": {"id": 30, "name": "LeftLeg"}},
            {"id": 86499793, "name": "Woman Right Leg",
             "assetType": {"id": 31, "name": "RightLeg"}},
        ]
    return assets


def test_fehlende_beine_werden_erkannt_aber_nicht_nachgeladen():
    """Traegt der Avatar nur Torso + Arme (Woman-Paket), fehlen die Beine.

    Roblox setzt dafuer den **Standard-Avatar** ein - nicht die Beine des
    getragenen Pakets. Eine frueherere Fassung hat die Paket-Beine
    nachgeladen ("Woman Left/Right Leg"); das war falsch und ist entfernt.
    Die Funktion macht nur noch Bestandsaufnahme (kein Netzwerk)."""
    import roblox_api

    class _Client:
        def get(self, url, allow_status=None):  # pragma: no cover
            raise AssertionError("Katalog darf nicht mehr gefragt werden")

    fehlend = roblox_api.missing_body_part_names(_woman_assets())
    assert fehlend == ["LeftLeg", "RightLeg"]
    assert not hasattr(roblox_api, "missing_body_parts")
    assert not hasattr(roblox_api, "asset_bundles")


def test_vollstaendiger_koerper_hat_keine_fehlenden_teile():
    import roblox_api

    assert roblox_api.missing_body_part_names(_woman_assets(with_legs=True)) == []


def test_fehlende_beine_werden_als_standard_blockkoerper_gerendert():
    """Ohne Bein-Assets entsteht der R15-Blockkoerper des Standard-Avatars:
    Ober-/Unterschenkel plus Fuss, in echten R15-Massen, kein Klotz."""
    import roblox_api
    import roblox_mesh
    import avatar_rig

    def _seg(name, size):
        return {"label": name, "mesh": roblox_mesh.make_box(*size, (0.0, 0.0, 0.0)),
                "rbxm_class": "MeshPart", "rbxm_size": size,
                "attachments": {}, "transparency": 0.0}

    torso = [
        _seg("LowerTorso", (2.0, 0.4, 1.0)),
        _seg("UpperTorso", (2.0, 1.6, 1.0)),
    ]
    rig = avatar_rig.assemble_r15_rig(
        {"torso": torso}, None, roblox_api.DEFAULT_BODY_COLORS,
        dict(roblox_api.DEFAULT_SCALES), headless=True)
    spans = rig.segment_spans["links_bein"]
    assert [name for name, _a, _b in spans] == [
        "LeftUpperLeg", "LeftLowerLeg", "LeftFoot"]
    assert any("Standard-Avatar" in n for n in rig.notes)
