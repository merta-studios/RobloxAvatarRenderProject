"""Tests fuer das Blender-5.0-Glossy-Setup in `blender/render_glass.py`.

Geprueft werden:
  * Exakte Defaults nach Blender-5.0-Vorgabe:
      - Render Engine EEVEE (`BLENDER_EEVEE`), Samples 16, Resolution 2000x2000
      - World Properties: Environment Texture `sunset_jhbcentral_4k.exr` (Strength 1.0)
      - Material (Principled BSDF + Image Texture):
          Rauheit (Roughness) = 0.0, Metallic = 0.2, Specular IOR Level = 1.0,
          Transmission = 0.0 (deckend hochglaenzend)
  * Node-Aufbau fuer World (`Environment Texture -> Background -> World Output`)
    sowie fuer Koerperteile & Accessoires (`Image Texture -> Principled BSDF -> Material Output`)
  * Farb-Helfer & klassische Kleidungs-Vorlagenerkennung (`_find_opaque_panel`).
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
EXR_FILE = os.path.join(ROOT, "sunset_jhbcentral_4k.exr")


class _FakeImage:
    """Minimales Abbild eines Blender-Images fuer `_find_opaque_panel` & Node-Tests."""

    def __init__(self, rgba: np.ndarray | None = None, filepath: str = ""):
        if rgba is None:
            rgba = np.ones((4, 4, 4), dtype=np.float32)
        self._rgba = np.asarray(rgba, dtype=np.float32)
        self.size = (self._rgba.shape[1], self._rgba.shape[0])
        self.pixels = _FakePixels(self._rgba)
        self.filepath = filepath
        self.alpha_mode = "STRAIGHT"


class _FakePixels:
    def __init__(self, rgba):
        self._rgba = rgba

    def foreach_get(self, out):
        out[:] = self._rgba.reshape(-1)


class _FakeSocket:
    def __init__(self, name: str, default_value=None):
        self.name = name
        self.default_value = default_value


class _FakeSocketMap(dict):
    def __getitem__(self, key):
        if key not in self:
            self[key] = _FakeSocket(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        return super().get(key, default)


class _FakeNode:
    def __init__(self, node_type: str, name: str | None = None):
        self.bl_idname = node_type
        self.name = name or node_type
        self.location = (0, 0)
        self.label = ""
        self.image = None
        self.projection = "FLAT"
        self.extension = "REPEAT"
        self.blend_type = "MIX"
        self.operation = "MULTIPLY"
        self.vector_type = "POINT"
        if node_type == "ShaderNodeBsdfPrincipled":
            self.inputs = _FakeSocketMap({
                "Base Color": _FakeSocket("Base Color", (0.8, 0.8, 0.8, 1.0)),
                "Metallic": _FakeSocket("Metallic", 0.0),
                "Roughness": _FakeSocket("Roughness", 0.5),
                "IOR": _FakeSocket("IOR", 1.5),
                "Alpha": _FakeSocket("Alpha", 1.0),
                "Specular IOR Level": _FakeSocket("Specular IOR Level", 0.5),
                "Transmission Weight": _FakeSocket("Transmission Weight", 0.0),
                "Coat Weight": _FakeSocket("Coat Weight", 0.0),
                "Coat Roughness": _FakeSocket("Coat Roughness", 0.03),
            })
            self.outputs = _FakeSocketMap({0: _FakeSocket("BSDF"), "BSDF": _FakeSocket("BSDF")})
        else:
            self.inputs = _FakeSocketMap()
            self.outputs = _FakeSocketMap({0: _FakeSocket("Out")})


class _FakeLink:
    def __init__(self, from_node, from_socket, to_node, to_socket):
        self.from_node = from_node
        self.from_socket = from_socket
        self.to_node = to_node
        self.to_socket = to_socket


class _FakeNodes(list):
    def new(self, node_type: str):
        default_names = {
            "ShaderNodeBsdfPrincipled": "Principled BSDF",
            "ShaderNodeOutputMaterial": "Material Output",
            "ShaderNodeOutputWorld": "World Output",
            "ShaderNodeBackground": "Background",
        }
        node = _FakeNode(node_type, default_names.get(node_type, node_type))
        node._owner = self
        self.append(node)
        return node

    def get(self, name: str):
        for n in self:
            if n.name == name:
                return n
        return None


class _FakeLinks(list):
    def new(self, from_socket, to_socket):
        from_node = getattr(from_socket, "_node", None)
        to_node = getattr(to_socket, "_node", None)
        link = _FakeLink(from_node, from_socket, to_node, to_socket)
        self.append(link)
        return link

    def remove(self, link):
        super().remove(link)


def _attach_socket_owners(node: _FakeNode) -> _FakeNode:
    orig_in = node.inputs
    orig_out = node.outputs

    class _OwnedSockets(_FakeSocketMap):
        def __getitem__(self, key):
            sock = super().__getitem__(key)
            sock._node = node
            return sock

        def get(self, key, default=None):
            sock = super().get(key, default)
            if sock is not None:
                sock._node = node
            return sock

    node.inputs = _OwnedSockets(orig_in)
    node.outputs = _OwnedSockets(orig_out)
    for s in list(node.inputs.values()) + list(node.outputs.values()):
        s._node = node
    return node


class _FakeNodeTree:
    def __init__(self, initial=()):
        self.nodes = _FakeNodes()
        self.links = _FakeLinks()
        for ntype in initial:
            _attach_socket_owners(self.nodes.new(ntype))
        orig_new = self.nodes.new
        self.nodes.new = lambda nt: _attach_socket_owners(orig_new(nt))
        if len(self.nodes) == 2:
            self.links.new(self.nodes[0].outputs[0], self.nodes[1].inputs["Surface"])


class _FakeMaterial:
    def __init__(self, name: str):
        self.name = name
        self.use_nodes = True
        self.use_raytrace_refraction = False
        self.node_tree = _FakeNodeTree(("ShaderNodeBsdfPrincipled", "ShaderNodeOutputMaterial"))


class _FakeWorld:
    def __init__(self, name: str):
        self.name = name
        self.use_nodes = True
        self.node_tree = _FakeNodeTree(("ShaderNodeBackground", "ShaderNodeOutputWorld"))


class _FakeMaterials(list):
    def new(self, name: str):
        m = _FakeMaterial(name)
        self.append(m)
        return m


class _FakeWorlds(list):
    def new(self, name: str):
        w = _FakeWorld(name)
        self.append(w)
        return w


class _FakeImages(list):
    def load(self, path: str, check_existing: bool = True):
        img = _FakeImage(filepath=path)
        self.append(img)
        return img


def _load_module():
    """render_glass.py importieren, ohne echtes Blender."""
    bpy = types.ModuleType("bpy")
    bpy.app = types.SimpleNamespace(version_string="5.0.1")
    bpy.data = types.SimpleNamespace(
        materials=_FakeMaterials(),
        images=_FakeImages(),
        objects=[],
        meshes=[],
        cameras=[],
        worlds=_FakeWorlds(),
        lights=[],
    )
    bpy.ops = types.SimpleNamespace(
        wm=types.SimpleNamespace(read_factory_settings=lambda **kw: None)
    )
    enum_items = [
        types.SimpleNamespace(identifier="BLENDER_EEVEE"),
        types.SimpleNamespace(identifier="BLENDER_WORKBENCH"),
        types.SimpleNamespace(identifier="CYCLES"),
    ]
    render_settings = types.SimpleNamespace(
        engine="BLENDER_EEVEE",
        resolution_x=1920,
        resolution_y=1080,
        resolution_percentage=100,
        film_transparent=False,
        image_settings=types.SimpleNamespace(file_format="PNG", color_mode="RGBA"),
        bl_rna=types.SimpleNamespace(
            properties={"engine": types.SimpleNamespace(enum_items=enum_items)}
        ),
    )
    eevee_settings = types.SimpleNamespace(
        taa_render_samples=64,
        taa_samples=16,
        use_shadows=True,
        use_raytracing=True,
    )
    scene = types.SimpleNamespace(
        world=None,
        render=render_settings,
        eevee=eevee_settings,
        display_settings=types.SimpleNamespace(display_device="sRGB"),
        view_settings=types.SimpleNamespace(view_transform="AgX", look="None"),
        unit_settings=types.SimpleNamespace(system="NONE"),
    )
    bpy.context = types.SimpleNamespace(scene=scene)

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
        module._fake_bpy = bpy
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
    img = _template(panels=[(231, 74, 128, 128)])
    region = render._find_opaque_panel(img, "shirt")
    assert region is not None
    u0, v0, u1, v1 = region
    assert u0 == pytest.approx(231 / 585, abs=0.01)
    assert u1 - u0 == pytest.approx(128 / 585, abs=0.01)
    assert v0 == pytest.approx(74 / 559, abs=0.01)
    assert v1 - v0 == pytest.approx(128 / 559, abs=0.01)


def test_findet_aermel_feld_wenn_kein_torso_feld(render):
    img = _template(panels=[(40, 300, 64, 128)])
    region = render._find_opaque_panel(img, "shirt")
    assert region is not None
    u0, _, u1, _ = region
    assert u1 - u0 == pytest.approx(64 / 585, abs=0.02)


def test_ohne_transparenz_keine_vorlagenbeschneidung(render):
    img = _template(panels=[(0, 0, 585, 559)])
    assert render._find_opaque_panel(img, "shirt") is None


def test_ohne_deckendes_feld_keine_beschneidung(render):
    img = _template(panels=[(10, 10, 20, 20)])
    assert render._find_opaque_panel(img, "shirt") is None


# ---------------------------------------------------------------------------
# Exakte Blender-5.0-Vorgaben (EEVEE, 16 Samples, 2000x2000, EXR, Principled BSDF)
# ---------------------------------------------------------------------------


def test_exakte_blender5_glossy_defaults(render):
    argv = sys.argv
    sys.argv = [argv[0], "--"]
    try:
        args = render.parse_args()
    finally:
        sys.argv = argv

    assert args.engine == "eevee"
    assert args.resolution == "2000x2000"
    assert args.samples == 16
    assert args.roughness == pytest.approx(0.0)
    assert args.metallic == pytest.approx(0.2)
    assert args.specular_level == pytest.approx(1.0)
    assert args.transmission == pytest.approx(0.0)
    assert args.ior == pytest.approx(1.5)
    assert args.tint_strength == pytest.approx(0.0)
    assert args.texture_strength == pytest.approx(1.0)
    assert args.env_strength == pytest.approx(1.0)
    assert args.transparent is False
    assert args.with_floor is False
    assert args.studio_lights is False


def test_sunset_exr_liegt_im_repo_und_ist_echtes_openexr(render):
    assert os.path.isfile(EXR_FILE), "sunset_jhbcentral_4k.exr fehlt im Repo-Root"
    resolved = render.resolve_env_texture(render.DEFAULT_ENV_TEXTURE)
    assert os.path.samefile(resolved, EXR_FILE)
    with open(EXR_FILE, "rb") as fh:
        magic = fh.read(4)
    assert magic == b"\x76\x2f\x31\x01", "sunset_jhbcentral_4k.exr ist kein gueltiges OpenEXR"


def test_world_nutzt_environment_texture_direkt_ohne_lightpath(render):
    sys.modules["bpy"] = render._fake_bpy
    try:
        loaded = render.setup_world(EXR_FILE, env_strength=1.0)
    finally:
        sys.modules.pop("bpy", None)

    assert os.path.samefile(loaded, EXR_FILE)
    world = render._fake_bpy.context.scene.world
    assert world is not None
    node_types = [n.bl_idname for n in world.node_tree.nodes]
    assert "ShaderNodeTexEnvironment" in node_types
    assert "ShaderNodeBackground" in node_types
    assert "ShaderNodeOutputWorld" in node_types
    # Kein LightPath-Kamera-Override mehr: HDRI ist direkt sichtbar & spiegelt voll
    assert "ShaderNodeLightPath" not in node_types
    bg = next(n for n in world.node_tree.nodes if n.bl_idname == "ShaderNodeBackground")
    assert bg.inputs["Strength"].default_value == pytest.approx(1.0)


def test_material_setzt_exakt_roughness_0_metallic_02_ior_level_1(render, tmp_path):
    tex_file = tmp_path / "avatar_tex.png"
    tex_file.write_bytes(b"\x89PNG\r\n\x1a\n")

    sys.modules["bpy"] = render._fake_bpy
    try:
        body_mat = render.make_glass_material(
            "Body",
            (0.78, 0.67, 0.47),
            (0.92, 0.97, 1.0),
            texture_path=str(tex_file),
            texture_kind="composited",
        )
        acc_mat = render.make_accent_material(
            "Accessory",
            (0.85, 0.91, 0.95),
            texture_path=str(tex_file),
            texture_kind="mesh_surface",
        )
    finally:
        sys.modules.pop("bpy", None)

    for mat in (body_mat, acc_mat):
        principled = [n for n in mat.node_tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"]
        # Ein einzelner Principled BSDF (kein MixShader mit Metallic=0 mehr!)
        assert len(principled) == 1
        bsdf = principled[0]
        assert bsdf.inputs["Roughness"].default_value == pytest.approx(0.0)
        assert bsdf.inputs["Metallic"].default_value == pytest.approx(0.2)
        assert bsdf.inputs["Specular IOR Level"].default_value == pytest.approx(1.0)
        assert bsdf.inputs["Transmission Weight"].default_value == pytest.approx(0.0)
        # Image Texture ist direkt mit Base Color verbunden
        img_nodes = [n for n in mat.node_tree.nodes if n.bl_idname == "ShaderNodeTexImage"]
        assert len(img_nodes) == 1
        assert any(
            lk.from_node is img_nodes[0]
            and lk.to_node is bsdf
            and lk.to_socket.name == "Base Color"
            for lk in mat.node_tree.links
        )


def test_setup_render_konfiguriert_blender5_eevee_2000x2000_16_samples(render):
    scene = render._fake_bpy.context.scene
    engine_id = render.setup_render(scene, 2000, 2000, 16, engine="eevee", transparent=False)
    assert engine_id == "BLENDER_EEVEE"
    assert scene.render.engine == "BLENDER_EEVEE"
    assert scene.eevee.taa_render_samples == 16
    assert scene.render.resolution_x == 2000
    assert scene.render.resolution_y == 2000
    assert scene.render.film_transparent is False
    assert scene.view_settings.view_transform == "AgX"
    assert scene.view_settings.look == "None"


def test_workflow_nutzt_blender5_eevee_2000x2000_16_samples():
    yaml = pytest.importorskip("yaml")
    wf_path = os.path.join(ROOT, ".github", "workflows", "avatar-render.yml")
    text = open(wf_path, encoding="utf-8").read()
    data = yaml.safe_load(text)
    assert data["env"]["BLENDER_SERIES"] == "Blender5.0"
    assert data["env"]["BLENDER_VERSION"].startswith("5.0")
    assert "2000x2000" in data["env"]["PIPELINE_RESOLUTION"]
    assert "'16'" in data["env"]["PIPELINE_SAMPLES"] or '"16"' in data["env"]["PIPELINE_SAMPLES"]
    assert "--engine eevee" in text
    assert "libvulkan1" in text
    assert "libgl1-mesa-dri" in text


def test_kein_multiply_von_grellem_blau_ueber_die_textur():
    text = open(SCRIPT, encoding="utf-8").read()
    assert 'blend_type = "MULTIPLY"' not in text
    assert "make_metal_material" not in text
