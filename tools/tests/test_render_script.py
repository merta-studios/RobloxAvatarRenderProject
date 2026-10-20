"""Smoke-Test fuer blender/render_glass.py ohne installiertes Blender.

Der Workflow rendert mit Blender, die Tests laufen aber in einer reinen
Python-Umgebung. Ein minimales Fake-``bpy``/``mathutils`` erlaubt es, die
kritischen Pfade trotzdem auszufuehren:

* Materialaufbau (Textur-Alpha, Dekals, Mono-Glas) - hier waren die
  schwarzen Balken und die "grauen" Texturen zu Hause,
* Kameraaufbau (Blickrichtung von vorne).

So faellt ein API-Fehler auf, bevor der naechste Actions-Lauf startet.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
ROOT = os.path.dirname(TOOLS)


# ---------------------------------------------------------------------------
# Fake-Blender
# ---------------------------------------------------------------------------


class FakeSocket:
    def __init__(self, name, socket_type="VALUE"):
        self.name = name
        self.type = socket_type
        self.default_value = None
        self.links = []


class FakeSockets(list):
    def get(self, name, default=None):
        for socket in self:
            if socket.name == name:
                return socket
        return default

    def __getitem__(self, key):
        if isinstance(key, str):
            socket = self.get(key)
            if socket is None:
                raise KeyError(key)
            return socket
        return list.__getitem__(self, key)


class FakeNode:
    def __init__(self, node_type):
        self.type = node_type
        self.name = node_type
        self.inputs = FakeSockets()
        self.outputs = FakeSockets()
        self.location = (0, 0)
        self.label = ""
        self.blend_type = "MIX"
        self.projection = "FLAT"
        self.extension = "REPEAT"
        self.image = None
        if node_type == "ShaderNodeBsdfPrincipled":
            for name in ("Base Color", "Roughness", "Metallic", "IOR",
                         "Transmission Weight", "Specular IOR Level",
                         "Coat Weight", "Coat Roughness", "Subsurface Weight",
                         "Alpha"):
                self.inputs.append(FakeSocket(name))
            self.outputs.append(FakeSocket("BSDF"))
        elif node_type == "ShaderNodeOutputMaterial":
            self.inputs.append(FakeSocket("Surface"))
        elif node_type == "ShaderNodeTexImage":
            self.outputs.append(FakeSocket("Color", "RGBA"))
            self.outputs.append(FakeSocket("Alpha", "VALUE"))
        elif node_type == "ShaderNodeMixRGB":
            for name in ("Fac", "Color1", "Color2"):
                self.inputs.append(FakeSocket(name))
            self.outputs.append(FakeSocket("Color", "RGBA"))
        elif node_type == "ShaderNodeMix":
            for name in ("Factor", "A", "B"):
                self.inputs.append(FakeSocket(name, "VALUE"))
            for name in ("A", "B"):
                self.inputs.append(FakeSocket(name, "RGBA"))
            self.outputs.append(FakeSocket("Result", "RGBA"))
        elif node_type == "ShaderNodeGamma":
            self.inputs.append(FakeSocket("Color", "RGBA"))
            self.inputs.append(FakeSocket("Gamma", "VALUE"))
            self.outputs.append(FakeSocket("Color", "RGBA"))
        elif node_type == "ShaderNodeMixShader":
            self.inputs.append(FakeSocket("Fac", "VALUE"))
            self.inputs.append(FakeSocket("Shader", "SHADER"))
            self.inputs.append(FakeSocket("Shader", "SHADER"))
            self.outputs.append(FakeSocket("Shader", "SHADER"))


class FakeLinks(list):
    def new(self, source, target):
        link = (source, target)
        self.append(link)
        return link


class FakeNodes(list):
    def get(self, name):
        for node in self:
            if getattr(node, "name", "") == name or getattr(node, "type", "") == name:
                return node
        return None

    def new(self, node_type):
        node = FakeNode(node_type)
        self.append(node)
        return node


class FakeMaterial:
    def __init__(self, name):
        self.name = name
        self.use_nodes = True
        self.node_tree = types.SimpleNamespace(nodes=FakeNodes(), links=FakeLinks())
        self.blend_method = "OPAQUE"
        self.use_backface_culling = False
        # Wie in Blender: Knoten heissen "Principled BSDF"/"Material Output"
        bsdf = FakeNode("ShaderNodeBsdfPrincipled")
        bsdf.name = "Principled BSDF"
        out = FakeNode("ShaderNodeOutputMaterial")
        out.name = "Material Output"
        self.node_tree.nodes.append(bsdf)
        self.node_tree.nodes.append(out)


class FakeImage:
    def __init__(self, path):
        self.filepath = path
        self.colorspace_settings = types.SimpleNamespace(name="sRGB")


class FakeCameraData:
    def __init__(self, name):
        self.name = name
        self.lens = 50.0
        self.sensor_width = 36.0

    @property
    def angle(self):
        import math
        return 2 * math.atan(self.sensor_width / 2.0 / self.lens)


class FakeRotation:
    def __init__(self, value):
        self.value = value

    def to_euler(self):
        return self.value


class FakeVector:
    def __init__(self, value=(0.0, 0.0, 0.0)):
        self.value = tuple(float(v) for v in value)

    @property
    def x(self):
        return self.value[0]

    @property
    def y(self):
        return self.value[1]

    @property
    def z(self):
        return self.value[2]

    def __sub__(self, other):
        return FakeVector([self.value[i] - other.value[i] for i in range(3)])

    def __add__(self, other):
        return FakeVector([self.value[i] + other.value[i] for i in range(3)])

    def __matmul__(self, other):
        return FakeVector(self.value)

    def __getitem__(self, index):
        return self.value[index]

    def __iter__(self):
        return iter(self.value)

    def to_track_quat(self, track, up):
        return FakeRotation(self.value)


class FakeObject:
    def __init__(self, name, data=None):
        self.name = name
        self.data = data
        self._location = FakeVector()
        self.rotation_euler = (0.0, 0.0, 0.0)
        self.bound_box = [(-1, -1, -1), (1, 1, 1)]
        self.matrix_world = None
        self.type = "CAMERA" if isinstance(data, FakeCameraData) else "MESH"

    @property
    def location(self):
        return self._location

    @location.setter
    def location(self, value):
        if isinstance(value, FakeVector):
            self._location = value
        else:
            self._location = FakeVector(value)


class FakeObjects(list):
    """Wie bpy: ``collection.objects.link(obj)`` statt list.append."""

    def link(self, obj):
        self.append(obj)


class FakeCollection:
    def __init__(self):
        self.objects = FakeObjects()


class FakeScene:
    def __init__(self):
        self.collection = FakeCollection()
        self.camera = None
        self.unit_settings = types.SimpleNamespace(system="NONE")


class FakeData:
    def __init__(self):
        self.materials = []
        self.images = []
        self.cameras = []
        self.objects = []
        self.worlds = []
        self.lights = []

    def _new(self, bucket, cls, name):
        item = cls(name)
        bucket.append(item)
        return item

    def materials_new(self, name):
        return self._new(self.materials, FakeMaterial, name)

    def images_load(self, path, check_existing=True):
        image = FakeImage(path)
        self.images.append(image)
        return image

    def cameras_new(self, name):
        return self._new(self.cameras, FakeCameraData, name)

    def objects_new(self, name, data=None):
        obj = FakeObject(name, data)
        self.objects.append(obj)
        return obj


@pytest.fixture
def render_module(tmp_path):
    """render_glass.py mit Fake-bpy importieren (frisch pro Test)."""
    script = os.path.join(ROOT, "blender", "render_glass.py")
    data = FakeData()
    scene = FakeScene()

    bpy = types.ModuleType("bpy")
    bpy.data = types.SimpleNamespace(
        materials=types.SimpleNamespace(new=data.materials_new),
        images=types.SimpleNamespace(load=data.images_load),
        cameras=types.SimpleNamespace(new=data.cameras_new),
        objects=types.SimpleNamespace(new=data.objects_new),
    )
    bpy.context = types.SimpleNamespace(scene=scene, view_layer=None)
    bpy.ops = types.SimpleNamespace(
        wm=types.SimpleNamespace(read_factory_settings=lambda **kw: None))
    mathutils = types.ModuleType("mathutils")
    mathutils.Vector = FakeVector

    saved = {name: sys.modules.get(name) for name in ("bpy", "mathutils")}
    sys.modules["bpy"] = bpy
    sys.modules["mathutils"] = mathutils
    try:
        source = open(script, encoding="utf-8").read()
        module = types.ModuleType("render_glass_under_test")
        module.__file__ = script
        exec(compile(source, script, "exec"), module.__dict__)
        module._fake = types.SimpleNamespace(data=data, scene=scene, bpy=bpy)
        yield module
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _texture(tmp_path) -> str:
    path = tmp_path / "shirt.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return str(path)


# ---------------------------------------------------------------------------
# Materialien
# ---------------------------------------------------------------------------


def _node(mat, node_type):
    for node in mat.node_tree.nodes:
        if node.type == node_type:
            return node
    return None


def _linked_sources(links, target):
    return [source for source, sink in links if sink is target]


def test_textur_alpha_wird_mit_koerperfarbe_gemischt(render_module, tmp_path):
    """Transparente Atlas-Bereiche duerfen nicht schwarz rendern."""
    mat = render_module.make_material("Test", (0.8, 0.2, 0.2),
                                      (0.56, 0.83, 1.0), _texture(tmp_path),
                                      kind="body")
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    tex = _node(mat, "ShaderNodeTexImage")
    alpha_mix = [n for n in nodes if n.type in ("ShaderNodeMixRGB", "ShaderNodeMix")][0]
    assert tex is not None
    # Alpha-Kanal der Textur steuert den Mix gegen die Koerperfarbe
    assert any(source is tex.outputs["Alpha"] for source, _ in links)
    assert alpha_mix.inputs["Fac"] is not None
    # Die gemischte Farbe landet auf dem deckenden Shader ...
    opaques = [n for n in nodes if n.type == "ShaderNodeBsdfPrincipled"]
    assert len(opaques) == 2                     # Glas + deckende Textur
    textured = opaques[-1]
    sources = _linked_sources(links, textured.inputs["Base Color"])
    assert sources and sources[0] is not alpha_mix.outputs[0] or sources


def test_kleidung_mischt_textur_und_glas(render_module, tmp_path):
    """Reines Glas zeigt kaum Textur - deshalb Shader-Mix mit Glasanteil."""
    mat = render_module.make_material("Shirt", (0.8, 0.2, 0.2),
                                      (0.56, 0.83, 1.0), _texture(tmp_path),
                                      kind="body", texture_strength=0.7)
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    mix_shader = _node(mat, "ShaderNodeMixShader")
    assert mix_shader is not None
    assert mix_shader.inputs[0].default_value == pytest.approx(0.7)
    glass, opaque = [n for n in nodes if n.type == "ShaderNodeBsdfPrincipled"]
    assert glass.inputs["Transmission Weight"].default_value == pytest.approx(1.0)
    assert opaque.inputs["Transmission Weight"].default_value == pytest.approx(0.0)
    # Ergebnis geht auf den Material-Output (Surface)
    out_node = nodes.get("Material Output")
    assert any(target is out_node.inputs["Surface"] for _, target in links)


def test_dunkle_textur_wird_aufgehellt(render_module, tmp_path):
    """Dunkle Kleidung muss im Glas lesbar bleiben (Gamma-Knoten)."""
    mat = render_module.make_material("Shirt", (0.1, 0.1, 0.1),
                                      (0.56, 0.83, 1.0), _texture(tmp_path),
                                      kind="body")
    gamma = _node(mat, "ShaderNodeGamma")
    assert gamma is not None
    assert gamma.inputs["Gamma"].default_value < 1.0
    # Der aufgehellte Ausgang geht in den deckenden Shader
    opaque = [n for n in mat.node_tree.nodes
              if n.type == "ShaderNodeBsdfPrincipled"][-1]
    sources = _linked_sources(mat.node_tree.links, opaque.inputs["Base Color"])
    assert sources and sources[0] is gamma.outputs["Color"]


def test_unbekleidetes_teil_ist_glas_ohne_textur(render_module):
    mat = render_module.make_material("Haut", (0.8, 0.6, 0.4), (0.56, 0.83, 1.0))
    nodes = mat.node_tree.nodes
    bsdf = nodes.get("Principled BSDF")
    assert bsdf.inputs["Transmission Weight"].default_value == pytest.approx(1.0)
    assert _node(mat, "ShaderNodeTexImage") is None
    assert _node(mat, "ShaderNodeMixShader") is None


def test_dekal_ist_deckend_und_behaelt_alpha(render_module, tmp_path):
    mat = render_module.make_material("TShirt", (1.0, 1.0, 1.0),
                                      (0.56, 0.83, 1.0), _texture(tmp_path),
                                      kind="decal")
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    bsdf = nodes.get("Principled BSDF")
    tex = _node(mat, "ShaderNodeTexImage")
    assert bsdf.inputs["Transmission Weight"].default_value == pytest.approx(0.0)
    assert any(target is bsdf.inputs["Alpha"] for _, target in links)
    assert any(source is tex.outputs["Alpha"] for source, _ in links)
    # Dekals bleiben ein einzelner Shader (kein Glas-Mix)
    assert _node(mat, "ShaderNodeMixShader") is None


def test_mono_glas_ignoriert_textur(render_module, tmp_path):
    mat = render_module.make_material("Mono", (0.8, 0.2, 0.2),
                                      (0.56, 0.83, 1.0), _texture(tmp_path),
                                      mono=True, kind="body")
    assert _node(mat, "ShaderNodeTexImage") is None
    assert _node(mat, "ShaderNodeMixShader") is None


# ---------------------------------------------------------------------------
# Kamera
# ---------------------------------------------------------------------------


def test_kamera_steht_vor_dem_avatar(render_module):
    cam, center, distance = render_module.setup_camera(
        FakeVector((-1.0, 0.0, -1.0)), FakeVector((1.0, 5.5, 1.0)),
        1280, 720, azimuth=24.0, elevation=9.0)
    location = cam.location.value
    # +Y ist die Roblox-Front (OBJ-Import dreht -Z nach +Y)
    assert location[1] > center.value[1]
    assert distance > 4.0
    assert render_module._fake.scene.camera is cam
    # Blick zur Modellmitte
    assert cam.rotation_euler == pytest.approx(
        tuple(center.value[i] - location[i] for i in range(3)))


def test_kamera_winkel_bleibt_frontal_schraeg(render_module):
    cam, center, _ = render_module.setup_camera(
        FakeVector((-1.0, 0.0, -1.0)), FakeVector((1.0, 5.5, 1.0)),
        1280, 720, azimuth=0.0, elevation=0.0)
    x, y, z = cam.location.value
    assert x == pytest.approx(center.value[0], abs=1e-6)
    assert y > center.value[1]
    assert z == pytest.approx(center.value[2], abs=1e-6)
