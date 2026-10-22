"""Tests fuer das Glas-/Textur-Material in `blender/render_glass.py`.

Der Bug-Report: "man sieht kaum die Texturen bei dem starken Glas, alles
ist grau, Mega dunkles Blau und Weiss". Ursachen waren

  * Transmission 1.0 + Roughness 0.05 (starkes Glas) zusammen mit einer
    MULTIPLY-Mischung aus kraeftigem Hellblau und Textur,
  * ein rein helles Umgebungsweltlicht, das das Glas von innen flutete,
  * Roblox-Kleidungsvorlagen, die komplett (inkl. Aermel und
    transparenten Luecken) auf jede Flaeche gelegt wurden.

`bpy` gibt es in der Testumgebung nicht - die Module werden gestubbt, damit
die reinen Python-Teile (Farb-Helfer, Vorlagen-Erkennung, Defaults)
pruefbar bleiben.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types

import pytest

np = pytest.importorskip("numpy", reason="numpy kommt mit DracoPy/Blender")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "blender", "render_glass.py")


class _FakeImage:
    """Minimales Abbild eines Blender-Images fuer `_find_opaque_panel`."""

    def __init__(self, rgba: np.ndarray):
        self._rgba = np.asarray(rgba, dtype=np.float32)
        self.size = (self._rgba.shape[1], self._rgba.shape[0])
        self.pixels = _FakePixels(self._rgba)


class _FakePixels:
    def __init__(self, rgba):
        self._rgba = rgba

    def foreach_get(self, out):
        out[:] = self._rgba.reshape(-1)


def _load_module():
    """render_glass.py importieren, ohne echtes Blender."""
    bpy = types.ModuleType("bpy")
    bpy.data = types.SimpleNamespace(materials=[], images=[], objects=[],
                                     worlds=[], lights=[])
    bpy.ops = types.SimpleNamespace()
    bpy.context = types.SimpleNamespace(scene=types.SimpleNamespace())
    mathutils = types.ModuleType("mathutils")

    class _Vector(tuple):
        def __new__(cls, values):
            return super().__new__(cls, values)

    mathutils.Vector = _Vector
    saved = {name: sys.modules.get(name) for name in ("bpy", "mathutils")}
    sys.modules["bpy"] = bpy
    sys.modules["mathutils"] = mathutils
    try:
        spec = importlib.util.spec_from_file_location("render_glass_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture(scope="module")
def render():
    return _load_module()


def _template(width=585, height=559, panels=()):
    """RGBA-Template: transparent, mit deckenden Feldern (x, y, w, h)."""
    rgba = np.zeros((height, width, 4), dtype=np.float32)
    for (x, y, w, h) in panels:
        rgba[y:y + h, x:x + w, 0:3] = 0.2
        rgba[y:y + h, x:x + w, 3] = 1.0
    return _FakeImage(rgba)


# ---------------------------------------------------------------------------
# Farb-Helfer
# ---------------------------------------------------------------------------

def test_hex_to_rgb(render):
    assert render.hex_to_rgb("#ff8000") == pytest.approx((1.0, 128 / 255, 0.0))
    assert render.hex_to_rgb("fff") == pytest.approx((1.0, 1.0, 1.0))
    assert render.hex_to_rgb("kaputt") == pytest.approx((0.8, 0.8, 0.8))


def test_srgb_to_linear_monoton(render):
    dark = render.srgb_to_linear((0.0, 0.0, 0.0))
    mid = render.srgb_to_linear((0.5, 0.5, 0.5))
    bright = render.srgb_to_linear((1.0, 1.0, 1.0))
    assert dark == pytest.approx((0.0, 0.0, 0.0))
    assert bright == pytest.approx((1.0, 1.0, 1.0))
    assert 0.2 < mid[0] < 0.25  # 0.5 sRGB ist ~0.214 linear


# ---------------------------------------------------------------------------
# Vorlagen-Erkennung (Kleidung)
# ---------------------------------------------------------------------------

def test_findet_torso_feld_der_shirt_vorlage(render):
    """Das deckende 128x128-Torso-Feld wird gefunden (UV-Koordinaten)."""
    img = _template(panels=[(231, 74, 128, 128)])
    region = render._find_opaque_panel(img, "shirt")
    assert region is not None
    u0, v0, u1, v1 = region
    assert u0 == pytest.approx(231 / 585, abs=0.01)
    assert u1 - u0 == pytest.approx(128 / 585, abs=0.01)
    # Blender-Bildzeilen laufen von unten (Zeile 0 = unterste Zeile), der
    # Puffer des Images beginnt also unten - v0 ist direkt y/Hoehe.
    assert v0 == pytest.approx(74 / 559, abs=0.01)
    assert v1 - v0 == pytest.approx(128 / 559, abs=0.01)


def test_findet_aermel_feld_wenn_kein_torso_feld(render):
    img = _template(panels=[(40, 300, 64, 128)])
    region = render._find_opaque_panel(img, "shirt")
    assert region is not None
    u0, _, u1, _ = region
    assert u1 - u0 == pytest.approx(64 / 585, abs=0.02)


def test_ohne_transparenz_keine_vorlagenbeschneidung(render):
    """Ein fertig gemaltes Bild (ohne Alpha) darf NICHT beschnitten werden."""
    img = _template(panels=[(0, 0, 585, 559)])
    assert render._find_opaque_panel(img, "shirt") is None


def test_ohne_deckendes_feld_keine_beschneidung(render):
    img = _template(panels=[(10, 10, 20, 20)])  # zu klein fuer jedes Feld
    assert render._find_opaque_panel(img, "shirt") is None


# ---------------------------------------------------------------------------
# Defaults: kein "starkes Glas" mehr
# ---------------------------------------------------------------------------

def test_defaults_lassen_texturen_lesbar(render):
    """Regression: Transmission 1.0 / Roughness 0.05 hat alles zugekleistert.
    Nach User-Wunsch: Roughness 0.0 (perfekt glatt) und Metallic 0.2 fuer staerkeren Glas-Effekt,
    Texturen bleiben durch separate Textur-Ebene (Alpha-Mix) trotzdem voll sichtbar."""
    argv = sys.argv
    sys.argv = [argv[0], "--"]
    try:
        args = render.parse_args()
    finally:
        sys.argv = argv
    assert 0.3 <= args.transmission <= 0.85
    assert 0.0 <= args.roughness <= 0.25   # 0.0 = perfekt glatt (User-Wunsch), trotzdem Texturen sichtbar
    assert 0.0 <= args.metallic <= 0.5     # 0.2 fuer Glas-Effekt
    assert args.tint_strength <= 0.25      # kaum Blau ueber die Textur
    assert args.texture_strength >= 0.8    # Textur darf nicht ausblenden
    assert args.ior <= 1.45
    assert args.env_strength > 0           # dunkle Umgebung = Glas-Kontrast
    assert args.env_color != args.background


def test_kein_multiply_von_grellem_blau_ueber_die_textur():
    text = open(SCRIPT, encoding="utf-8").read()
    # Die Textur wird per MIX mit der Koerperfarbe gemischt, nicht per
    # MULTIPLY mit der Glasfarbe (das liess alles blau/grau werden).
    assert "blend_type = \"MULTIPLY\"" not in text
    assert "make_metal_material" not in text  # Accessoires waren reines Weiss-Metall
