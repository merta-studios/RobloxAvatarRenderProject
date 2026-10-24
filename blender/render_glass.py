"""Blender-5.0-Script: rendert die in build/ erzeugte Avatar-Szene hochglaenzend (Glossy) mit EEVEE.

Aufruf (headless):
    blender -b --factory-startup --python blender/render_glass.py -- \
        --manifest build/manifest.json --out render_avatar.png \
        --resolution 2000x2000 --samples 16

Alles nach dem ersten `--` geht an dieses Script. Ohne Blender-Argumente
werden die exakten Blender-5.0-Defaults benutzt, damit man das Script auch
direkt aus der Blender-GUI (Texteditor -> Run Script) starten kann:

  * Render Engine: EEVEE (`BLENDER_EEVEE` in Blender 5.0, Fallback `BLENDER_EEVEE_NEXT`)
  * Samples: 16
  * Resolution: 2000x2000
  * World Properties: Environment Texture (`sunset_jhbcentral_4k.exr` im Repo-Root)
  * Material (Principled BSDF + Image Texture):
      - Roughness (Rauheit) = 0.0 (0..1)
      - Metallic = 0.2 (0..1)
      - Specular IOR Level (IQR/IOR Level) = 1.0 (0..1)
      - Transmission = 0.0 (deckend hochglaenzend wie Standard-Principled-BSDF)

Der Bildausschnitt und der Hintergrund kommen aus dem Manifest (Block
``render``), das ``tools/build_avatar.py`` aus der ``Configs.toml`` schreibt:

  * ``drehung_grad``             seitliche Drehung (-90 .. 90, 0 = frontal)
  * ``sichtbar_oben_prozent``    aus der Pose: freier Abstand ueber dem Kopf
  * ``sichtbar_unten_prozent``   aus der Pose: Abstand unter den Fuessen (negativ = Schnitt)
  * ``kamera_abstand_studs``     aus der Pose: zusaetzlicher Kamera-Abstand in Studs
  * ``hintergrund``              Bild-URL/-Datei, ``transparent``, ``glossy``
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import List

import bpy
from mathutils import Vector

try:  # im echten Blender immer da; Fake-bpy in Tests hat evtl. nur Vector
    from mathutils import Matrix
except ImportError:  # pragma: no cover
    Matrix = None  # type: ignore[assignment]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_ENV_TEXTURE = os.path.join(REPO_ROOT, "sunset_jhbcentral_4k.exr")

# ---------------------------------------------------------------------------
# Argumente
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    parser = argparse.ArgumentParser(description="Glossy-Render eines Roblox-Avatars (Blender 5.0 EEVEE)")
    parser.add_argument("--manifest", default="build/manifest.json")
    parser.add_argument("--out", default="render_avatar.png")
    parser.add_argument("--engine", default="eevee", choices=("eevee", "cycles"),
                        type=lambda s: s.lower(),
                        help="Render-Engine: 'eevee' (Default, Blender 5.0) oder 'cycles'")
    parser.add_argument("--resolution", default="2000x2000",
                        help="Breite x Hoehe, Default 2000x2000")
    parser.add_argument("--samples", type=int, default=16,
                        help="Render-Samples (Default 16 fuer EEVEE)")
    parser.add_argument("--env-texture", default=DEFAULT_ENV_TEXTURE,
                        help="Pfad zur World Environment Texture (.exr/.hdr), "
                             "Default: sunset_jhbcentral_4k.exr im Repository-Root")
    parser.add_argument("--env-strength", type=float, default=1.0,
                        help="World-Background-Strength der Environment Texture (Default 1.0)")
    parser.add_argument("--roughness", type=float, default=0.0,
                        help="Rauheit / Roughness 0..1 (Default 0.0)")
    parser.add_argument("--metallic", type=float, default=0.2,
                        help="Metallic 0..1 (Default 0.2)")
    parser.add_argument("--specular-level", "--ior-level", type=float, default=1.0,
                        dest="specular_level",
                        help="Specular IOR Level (IQR/IOR Level) 0..1 (Default 1.0)")
    parser.add_argument("--ior", type=float, default=1.5,
                        help="Brechungsindex des Principled BSDF (Blender-5.0-Default 1.5)")
    parser.add_argument("--transmission", type=float, default=0.0,
                        help="Transmission Weight 0..1 (Default 0.0 = deckend glossy)")
    parser.add_argument("--tint-strength", type=float, default=0.0,
                        help="Optionale Einfaerbung mit --glass-tint (Default 0.0 = echte Farben)")
    parser.add_argument("--texture-strength", type=float, default=1.0,
                        help="Deckkraft der Image-Texturen auf Base Color (Default 1.0)")
    parser.add_argument("--transparent", action="store_true",
                        help="Film -> Transparent aktivieren (Default: aus, HDRI sichtbar)")
    parser.add_argument("--with-floor", action="store_true",
                        help="Optionalen Schatten-Boden einfuegen (Default: aus)")
    parser.add_argument("--no-floor", action="store_true", default=True,
                        help="Kompatibilitaets-Flag (kein Boden ist jetzt Standard)")
    parser.add_argument("--studio-lights", action="store_true",
                        help="Optionale Dreipunkt-Area-Lichter zusaetzlich zum HDRI")
    # Rueckwaertskompatible Optionen fuer bestehende Aufrufe
    parser.add_argument("--max-bounces", type=int, default=6)
    parser.add_argument("--glass-tint", default="#eaf7ff")
    parser.add_argument("--metal-tint", default="#d8e9f2")
    parser.add_argument("--mono-glass", action="store_true")
    parser.add_argument("--background", default="#eaf1f8")
    parser.add_argument("--env-color", default="#3f4d5c")
    parser.add_argument("--denoise", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--save-blend", default="", help="optional .blend speichern")
    # Optionales Ueberschreiben der Werte aus dem Manifest (Configs.toml).
    parser.add_argument("--yaw", type=float, default=None,
                        help="Seitliche Drehung in Grad (-90..90); Default: Manifest/Configs.toml")
    parser.add_argument("--margin-top", type=float, default=None,
                        help="Freiraum ueber dem Kopf in Prozent; Default: Manifest")
    parser.add_argument("--margin-bottom", type=float, default=None,
                        help="Freiraum unter den Fuessen in Prozent; Default: Manifest")
    parser.add_argument("--background-image", default="",
                        help="Hintergrundbild (Pfad); Default: Manifest/Configs.toml")
    parser.add_argument("--background-glossy", action="store_true",
                        help="Hintergrund wie den Avatar glaenzen lassen")
    return parser.parse_args(argv)


def log(msg: str) -> None:
    print(f"[render] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Farb- und Pfad-Helfer (autark ohne Import aus tools/)
# ---------------------------------------------------------------------------


def hex_to_rgb(value: str):
    value = (value or "#cccccc").lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    try:
        return (int(value[0:2], 16) / 255.0,
                int(value[2:4], 16) / 255.0,
                int(value[4:6], 16) / 255.0)
    except (ValueError, IndexError):
        return (0.8, 0.8, 0.8)


def mix(a, b, t):
    """Lineare Mischung, t=0 -> a, t=1 -> b."""
    return tuple(a[i] * (1 - t) + b[i] * t for i in range(3))


def srgb_to_linear(c):
    return tuple((v / 12.92) if v <= 0.04045 else (((v + 0.055) / 1.055) ** 2.4) for v in c)


def tup(v):
    return (v[0], v[1], v[2])


def resolve_env_texture(path: str) -> str:
    """Loest den Pfad zur Environment-Textur robust auf (relativ zu CWD oder Repo-Root)."""
    if not path:
        return ""
    candidates = [
        path,
        os.path.join(REPO_ROOT, path),
        os.path.join(REPO_ROOT, os.path.basename(path)),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return ""


# ---------------------------------------------------------------------------
# Szene & Node-Helfer
# ---------------------------------------------------------------------------


def reset_scene() -> None:
    """Laedt die Blender-5.0-Factory-Szene und entfernt Standard-Objekte (Cube/Light/Camera).

    Durch `use_empty=False` bleiben saemtliche Blender-5.0-Standardwerte fuer
    EEVEE, Color Management (AgX) und World-Light-Probes exakt wie beim
    manuellen Oeffnen von Blender 5.0 erhalten.
    """
    try:
        bpy.ops.wm.read_factory_settings(use_empty=False)
    except TypeError:  # pragma: no cover - falls in Tests gestubbt
        bpy.ops.wm.read_factory_settings()

    for obj in list(getattr(bpy.data, "objects", ())):
        bpy.data.objects.remove(obj, do_unlink=True)
    for collection_name in ("meshes", "lights", "cameras", "materials"):
        coll = getattr(bpy.data, collection_name, None)
        if coll is not None:
            for datablock in list(coll):
                coll.remove(datablock)

    scene = bpy.context.scene
    if hasattr(scene, "unit_settings"):
        scene.unit_settings.system = "NONE"


def _ensure_use_nodes(datablock) -> None:
    """Aktiviert `use_nodes`, sofern die Blender-Version das Attribut noch benoetigt."""
    if hasattr(datablock, "use_nodes") and not datablock.use_nodes:
        datablock.use_nodes = True


def set_input(node, names, value) -> bool:
    """Setzt einen Socket, egal wie er in dieser Blender-Version heisst."""
    if isinstance(names, str):
        names = (names,)
    for name in names:
        socket = node.inputs.get(name)
        if socket is not None:
            try:
                socket.default_value = value
                return True
            except (TypeError, ValueError):
                continue
    return False


# ---------------------------------------------------------------------------
# Roblox-Kleidungsvorlagen: passendes Feld herausschneiden (Fallback)
# ---------------------------------------------------------------------------

CLOTHING_PANELS = {
    "shirt": ((128, 128), (64, 128), (128, 64)),
    "tshirt": ((128, 128), (64, 128), (128, 64)),
    "pants": ((64, 128), (128, 128), (128, 64)),
}
CLOTHING_TEMPLATE = (585.0, 559.0)


def _image_alpha_grid(img, max_side: int = 320):
    """Alpha-Kanal als numpy-Matrix, auf max_side verkleinert (0..1)."""
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - Blender bringt numpy mit
        return None
    width, height = img.size
    if width <= 0 or height <= 0:
        return None
    step = max(1, int(max(width, height) // max_side))
    sx = max(1, step)
    pixels = np.empty(width * height * 4, dtype=np.float32)
    try:
        img.pixels.foreach_get(pixels)
    except (TypeError, ValueError):  # pragma: no cover
        return None
    alpha = pixels.reshape(height, width, 4)[:, :, 3]
    alpha = alpha[::sx, ::sx]
    return alpha


def _find_opaque_panel(img, kind: str):
    """Position des groessten vollstaendig deckenden Vorlagen-Felds (0..1 UV)."""
    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        return None
    grid = _image_alpha_grid(img)
    if grid is None or grid.size == 0:
        return None
    height, width = grid.shape
    if float((grid < 0.5).mean()) < 0.02:
        return None
    table = np.zeros((height + 1, width + 1), dtype=np.float64)
    table[1:, 1:] = np.cumsum(np.cumsum(grid.astype(np.float64), axis=0), axis=1)

    sizes = CLOTHING_PANELS.get(kind or "shirt", CLOTHING_PANELS["shirt"])
    scale_x = width / CLOTHING_TEMPLATE[0]
    scale_y = height / CLOTHING_TEMPLATE[1]
    for panel_w, panel_h in sizes:
        w = max(2, int(round(panel_w * scale_x)))
        h = max(2, int(round(panel_h * scale_y)))
        if w >= width or h >= height:
            continue
        window = (table[h:, w:] - table[:-h, w:]
                  - table[h:, :-w] + table[:-h, :-w])
        hits = np.argwhere(window >= 0.999 * w * h)
        if len(hits) == 0:
            continue
        y, x = int(hits[0][0]), int(hits[0][1])
        return (x / width, y / height, (x + w) / width, (y + h) / height)
    return None


def _apply_clothing_uv(nodes, links, tex, kind: str):
    """Skaliert die UVs der Box auf das passende Vorlagen-Feld (nur Roh-Vorlagen)."""
    if kind not in ("shirt", "pants"):
        return False
    region = _find_opaque_panel(tex.image, kind)
    if region is None:
        return False
    u0, v0, u1, v1 = region
    mapping = nodes.new("ShaderNodeMapping")
    mapping.location = (-760, 120)
    mapping.vector_type = "POINT"
    mapping.inputs["Scale"].default_value = (u1 - u0, v1 - v0, 1.0)
    mapping.inputs["Location"].default_value = (u0, v0, 0.0)
    uv = nodes.new("ShaderNodeUVMap")
    uv.location = (-940, 120)
    links.new(uv.outputs["UV"], mapping.inputs["Vector"])
    links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
    return True


def _load_texture_node(nodes, texture_path: str):
    """Erstellt einen Image-Texture-Node (`ShaderNodeTexImage`) fuer `texture_path`."""
    if not texture_path or not os.path.exists(texture_path):
        return None
    try:
        img = bpy.data.images.load(texture_path, check_existing=True)
    except Exception as exc:  # pragma: no cover
        log(f"  ! Textur {texture_path} konnte nicht geladen werden: {exc}")
        return None
    if hasattr(img, "alpha_mode"):
        img.alpha_mode = "CHANNEL_PACKED"
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = img
    tex.projection = "FLAT"
    tex.extension = "REPEAT"
    tex.location = (-560, 260)
    tex.label = os.path.basename(texture_path)
    return tex


def _connect_mixed_base_color(nodes, links, bsdf, base_rgba, tex, texture_strength: float) -> None:
    """Mischt eine Roh-Textur mit Alpha ueber die Koerperfarbe in `Base Color`."""
    alpha_socket = tex.outputs["Alpha"]
    if texture_strength < 0.999:
        strength = nodes.new("ShaderNodeMath")
        strength.operation = "MULTIPLY"
        strength.location = (-460, 40)
        strength.inputs[1].default_value = max(0.0, min(1.0, texture_strength))
        links.new(alpha_socket, strength.inputs[0])
        alpha_socket = strength.outputs["Value"]

    mix_color = nodes.new("ShaderNodeMixRGB")
    mix_color.blend_type = "MIX"
    mix_color.location = (-280, 220)
    mix_color.inputs["Color1"].default_value = base_rgba
    links.new(tex.outputs["Color"], mix_color.inputs["Color2"])
    links.new(alpha_socket, mix_color.inputs["Fac"])
    links.new(mix_color.outputs["Color"], bsdf.inputs["Base Color"])


# ---------------------------------------------------------------------------
# Material: Exakter Blender-5.0-Glossy-Aufbau (Principled BSDF + Image Texture)
# ---------------------------------------------------------------------------


def make_glossy_material(
    name: str,
    base_rgb,
    tint_rgb=(0.92, 0.97, 1.0),
    texture_path: str = "",
    mono: bool = False,
    transmission: float = 0.0,
    roughness: float = 0.0,
    metallic: float = 0.2,
    specular_level: float = 1.0,
    tint_strength: float = 0.0,
    texture_strength: float = 1.0,
    ior: float = 1.5,
    texture_kind: str = "",
    decal: bool = True,
):
    """Erzeugt das exakte Glossy-Material wie in Blender 5.0:

    * Principled BSDF mit:
        - Base Color = Image Texture (bzw. Koerper-/Teilfarbe ohne Textur)
        - Roughness (Rauheit) = 0.0
        - Metallic = 0.2
        - Specular IOR Level (IQR/IOR Level) = 1.0
        - IOR = 1.5, Transmission = 0.0 (Standard Principled BSDF)
    """
    mat = bpy.data.materials.new(name)
    _ensure_use_nodes(mat)
    tree = mat.node_tree
    nodes, links = tree.nodes, tree.links

    bsdf = nodes.get("Principled BSDF")
    out = nodes.get("Material Output") or nodes.new("ShaderNodeOutputMaterial")
    if bsdf is None:  # pragma: no cover
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (0, 260)
    out.location = (320, 260)

    # Sicherstellen, dass Principled BSDF direkt am Material Output haengt
    for link in list(links):
        if link.to_node is out and link.to_socket.name == "Surface":
            links.remove(link)
    links.new(bsdf.outputs[0], out.inputs["Surface"])

    base = srgb_to_linear(tup(base_rgb)) if not mono else srgb_to_linear((0.93, 0.97, 1.0))
    tint = srgb_to_linear(tup(tint_rgb))
    tint_mix = 0.0 if mono else max(0.0, min(1.0, tint_strength))
    color = mix(base, tint, tint_mix)
    base_rgba = (color[0], color[1], color[2], 1.0)

    set_input(bsdf, "Base Color", base_rgba)
    set_input(bsdf, "Roughness", max(0.0, min(1.0, roughness)))
    set_input(bsdf, "Metallic", max(0.0, min(1.0, metallic)))
    set_input(bsdf, ("Specular IOR Level", "Specular"), max(0.0, min(1.0, specular_level)))
    set_input(bsdf, "IOR", ior)
    set_input(bsdf, ("Transmission Weight", "Transmission"), max(0.0, min(1.0, transmission)))
    set_input(bsdf, ("Coat Weight", "Clearcoat"), 0.0)
    set_input(bsdf, ("Coat Roughness", "Clearcoat Roughness"), 0.0)

    if transmission > 0.001 and hasattr(mat, "use_raytrace_refraction"):
        mat.use_raytrace_refraction = True

    tex = _load_texture_node(nodes, texture_path)
    if tex is None or not decal or mono:
        return mat

    if texture_kind:
        _apply_clothing_uv(nodes, links, tex, texture_kind)

    # Komponierte R15-Texturen ('composited') und aufbereitete Mesh-Texturen
    # ('mesh_surface') enthalten bereits die exakte Haut-/Oberflaechenfarbe im RGB-Kanal:
    # Image Texture wird 1:1 direkt mit Principled BSDF -> Base Color verbunden.
    if texture_kind in ("composited", "mesh_surface") and texture_strength >= 0.999:
        links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    else:
        _connect_mixed_base_color(nodes, links, bsdf, base_rgba, tex, texture_strength)

    return mat


def make_glass_material(name: str, base_rgb, tint_rgb, texture_path: str = "",
                        mono: bool = False, transmission: float = 0.0,
                        roughness: float = 0.0, metallic: float = 0.2, specular_level: float = 1.0,
                        tint_strength: float = 0.0,
                        texture_strength: float = 1.0, ior: float = 1.5,
                        texture_kind: str = "", decal: bool = True):
    """Kompatibler Alias fuer `make_glossy_material` (Koerperteile)."""
    return make_glossy_material(
        name=name,
        base_rgb=base_rgb,
        tint_rgb=tint_rgb,
        texture_path=texture_path,
        mono=mono,
        transmission=transmission,
        roughness=roughness,
        metallic=metallic,
        specular_level=specular_level,
        tint_strength=tint_strength,
        texture_strength=texture_strength,
        ior=ior,
        texture_kind=texture_kind,
        decal=decal,
    )


def make_accent_material(name: str, color_rgb, texture_path: str = "",
                         roughness: float = 0.0, transmission: float = 0.0,
                         metallic: float = 0.2, specular_level: float = 1.0,
                         ior: float = 1.5,
                         texture_strength: float = 1.0, texture_kind: str = ""):
    """Material fuer Accessoires: identisches Principled-BSDF-Glossy-Setup."""
    return make_glossy_material(
        name=name,
        base_rgb=color_rgb,
        texture_path=texture_path,
        mono=False,
        transmission=transmission,
        roughness=roughness,
        metallic=metallic,
        specular_level=specular_level,
        tint_strength=0.0,
        texture_strength=texture_strength,
        ior=ior,
        texture_kind=texture_kind,
        decal=True,
    )


# ---------------------------------------------------------------------------
# Geometrie-Import & Shading
# ---------------------------------------------------------------------------


def import_obj(path: str):
    """Importiert eine OBJ-Datei und gibt die neuen Mesh-Objekte zurueck."""
    before = set(bpy.data.objects)
    if hasattr(bpy.ops.wm, "obj_import"):
        bpy.ops.wm.obj_import(filepath=path, forward_axis="NEGATIVE_Z", up_axis="Y")
    else:  # pragma: no cover - Blender < 4.0
        bpy.ops.import_scene.obj(filepath=path, axis_forward="-Z", axis_up="Y")
    new = [obj for obj in bpy.data.objects if obj not in before and obj.type == "MESH"]
    return new


def join_objects(objects):
    """Fasst mehrere Mesh-Objekte zu einem zusammen."""
    if len(objects) <= 1:
        return objects[0] if objects else None
    base = objects[0]
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = base
    bpy.ops.object.join()
    return base


def recalc_normals(obj) -> None:
    """Normale nach aussen neu berechnen (nur fuer generierte Quader)."""
    import bmesh
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        bm.to_mesh(obj.data)
        obj.data.update()
    finally:
        bm.free()


def shade_smooth(obj) -> None:
    for poly in obj.data.polygons:
        poly.use_smooth = True


def add_bevel(obj, width: float, segments: int = 2) -> None:
    if width <= 0:
        return
    mod = obj.modifiers.new(name="Bevel", type="BEVEL")
    mod.width = width
    mod.segments = segments
    mod.limit_method = "ANGLE"
    mod.angle_limit = math.radians(35)
    if hasattr(mod, "use_clamp_overlap"):
        mod.use_clamp_overlap = True


# ---------------------------------------------------------------------------
# World Properties: Environment Texture (`sunset_jhbcentral_4k.exr`)
# ---------------------------------------------------------------------------


def setup_world(
    env_texture_path: str = DEFAULT_ENV_TEXTURE,
    env_strength: float = 1.0,
    fallback_color_hex: str = "#eaf1f8",
) -> str:
    """Richtet World Properties exakt wie in Blender 5.0 mit Environment Texture ein:

    `Environment Texture (sunset_jhbcentral_4k.exr)` -> `Background` -> `World Output`
    Sichtbar im Hintergrund, beleuchtet die Szene und erzeugt die scharfen
    Glossy-Spiegelungen auf allen Avatar-Teilen.
    """
    scene = bpy.context.scene
    world = scene.world
    if world is None:
        world = bpy.data.worlds.new("World")
        scene.world = world
    _ensure_use_nodes(world)

    tree = world.node_tree
    nodes, links = tree.nodes, tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputWorld")
    output.location = (300, 300)

    bg = nodes.new("ShaderNodeBackground")
    bg.location = (60, 300)
    bg.inputs["Strength"].default_value = env_strength
    links.new(bg.outputs["Background"], output.inputs["Surface"])

    resolved_exr = resolve_env_texture(env_texture_path)
    if resolved_exr:
        env_tex = nodes.new("ShaderNodeTexEnvironment")
        env_tex.location = (-260, 300)
        img = bpy.data.images.load(resolved_exr, check_existing=True)
        env_tex.image = img
        links.new(env_tex.outputs["Color"], bg.inputs["Color"])
        log(f"World Environment Texture: {resolved_exr} (Strength {env_strength:.2f})")
        return resolved_exr

    backdrop = srgb_to_linear(hex_to_rgb(fallback_color_hex))
    bg.inputs["Color"].default_value = (backdrop[0], backdrop[1], backdrop[2], 1.0)
    log(f"WARNUNG: Environment Texture '{env_texture_path}' nicht gefunden - Fallback-Farbe aktiv.")
    return ""


# ---------------------------------------------------------------------------
# Optionale Szene-Helfer & Kamera
# ---------------------------------------------------------------------------


def add_area_light(name: str, location, energy: float, size: float, color=(1, 1, 1),
                   target=None):
    data = bpy.data.lights.new(name, type="AREA")
    data.energy = energy
    data.size = size
    data.color = color
    obj = bpy.data.objects.new(name, data)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    aim = target if target is not None else Vector((0, 0, 0))
    direction = aim - Vector(location)
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    return obj


def add_floor(radius: float, z: float = 0.0):
    bpy.ops.mesh.primitive_plane_add(size=max(radius * 40, 120), location=(0, 0, z - 0.002))
    floor = bpy.context.active_object
    floor.name = "Boden"
    if hasattr(floor, "is_shadow_catcher"):
        floor.is_shadow_catcher = True
    mat = bpy.data.materials.new("Boden-Material")
    _ensure_use_nodes(mat)
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    set_input(bsdf, "Base Color", (0.88, 0.90, 0.94, 1.0))
    set_input(bsdf, "Roughness", 0.35)
    set_input(bsdf, "Metallic", 0.0)
    floor.data.materials.append(mat)
    return floor


def scene_bounds(objects):
    lo = Vector((1e9, 1e9, 1e9))
    hi = Vector((-1e9, -1e9, -1e9))
    for obj in objects:
        for corner in obj.bound_box:
            world = obj.matrix_world @ Vector(corner)
            for i in range(3):
                lo[i] = min(lo[i], world[i])
                hi[i] = max(hi[i], world[i])
    if lo.x > hi.x:
        return Vector((-1, -1, 0)), Vector((1, 1, 5))
    return lo, hi


# ---------------------------------------------------------------------------
# Kamera: perspektivisch exakter Bildausschnitt
# ---------------------------------------------------------------------------

def _v_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _v_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _v_mul(a, f):
    return (a[0] * f, a[1] * f, a[2] * f)


def _v_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _v_cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _v_norm(a, fallback=(0.0, 0.0, 1.0)):
    length = math.sqrt(_v_dot(a, a))
    if length < 1e-9:
        return fallback
    return (a[0] / length, a[1] / length, a[2] / length)


def camera_basis(yaw_deg: float):
    """Blickrichtungen der Kamera: (to_avatar, forward, right, up)."""
    direction = camera_direction(yaw_deg)
    forward = _v_mul(direction, -1.0)
    right = _v_norm(_v_cross(forward, (0.0, 0.0, 1.0)))
    up = _v_norm(_v_cross(right, forward))
    return direction, forward, right, up


def project_ndc(point, cam_location, forward, right, up, tan_h, tan_v):
    """Projiziert einen Punkt in normalisierte Bildkoordinaten (-1 .. +1)."""
    d = _v_sub(point, cam_location)
    depth = _v_dot(d, forward)
    if depth <= 1e-6:
        return None
    return (_v_dot(d, right) / depth / max(tan_h, 1e-6),
            _v_dot(d, up) / depth / max(tan_v, 1e-6))


def _span_over(points, cam, forward, right, up, tan_h, tan_v):
    ys = []
    xs = []
    for p in points:
        ndc = project_ndc(p, cam, forward, right, up, tan_h, tan_v)
        if ndc is None:
            continue
        xs.append(ndc[0])
        ys.append(ndc[1])
    if not ys:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), max(xs), min(ys), max(ys))


def compute_camera_placement(lo, hi, tan_h: float, tan_v: float,
                             yaw_deg: float = 0.0,
                             top_percent: float = 6.0,
                             bottom_percent: float = 6.0,
                             head_z=None, feet_z=None,
                             camera_distance_offset_studs: float = 0.0):
    """Kameraabstand und -hoehe so, dass der Bildausschnitt exakt passt.

    Rechnet **perspektivisch**: Punkte naeher an der Kamera werden groesser
    abgebildet. Dadurch sitzen die Fuesse (vorn) genauso an der richtigen
    Stelle wie der Kopf (hinten), und der Avatar wird nicht mehr unten
    abgeschnitten.

    * ``top_percent`` / ``bottom_percent`` >= 0: Abstand ueber der
      Kopfoberkante (ohne Accessoires) bzw. unter den Fuessen. Ragen
      Accessoires darueber hinaus, wird das Bild zusaetzlich aufgezogen,
      damit nichts abgeschnitten wird.
    * negative Prozentwerte: bewusstes Anschneiden (z. B. unten -40 ->
      nur der Torso). Dann gelten die Werte exakt, nichts wird aufgezogen.

    Rueckgabe: dict mit ``cam_location``, ``center``, ``distance``,
    ``kopf_prozent``, ``fuss_prozent``, ``aufgezogen`` und ``abgeschnitten``.
    """
    top = max(-100.0, min(95.0, float(top_percent)))
    bottom = max(-100.0, min(95.0, float(bottom_percent)))
    if top + bottom > 95.0:  # Sicherheitsnetz (die Config prueft das schon)
        factor = 95.0 / (top + bottom)
        top *= factor
        bottom *= factor

    cx = (lo[0] + hi[0]) / 2.0
    cy = (lo[1] + hi[1]) / 2.0
    feet = float(feet_z if feet_z is not None else lo[2])
    head = float(head_z if head_z is not None else hi[2])
    if head < feet:
        feet, head = head, feet
    body_span = max(1e-3, head - feet)

    direction, forward, right, up = camera_basis(yaw_deg)
    # Die Kamera steht auf der +Y-Seite: vorderste Punkte liegen bei max. y
    # und werden perspektivisch am groessten abgebildet.
    front_y = hi[1]
    head_point = (cx, front_y, head)
    feet_point = (cx, front_y, feet)
    corners = [(x, y, z) for x in (lo[0], hi[0])
               for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]

    target_top = 1.0 - 2.0 * top / 100.0
    target_bottom = -1.0 + 2.0 * bottom / 100.0
    target_span = max(1e-3, target_top - target_bottom)
    exact = top < 0.0 or bottom < 0.0

    def placement(distance: float):
        """Kamera fuer einen Abstand: Kopf und Fuesse exakt an den Raendern."""
        centre_target = (target_top + target_bottom) / 2.0
        low = feet - 4.0 * body_span
        high = head + 4.0 * body_span
        for _ in range(90):
            cz = (low + high) / 2.0
            cam = (cx + direction[0] * distance,
                   cy + direction[1] * distance,
                   cz)
            _, _, ymin, ymax = _span_over([head_point, feet_point], cam,
                                          forward, right, up, tan_h, tan_v)
            if (ymin + ymax) / 2.0 > centre_target:
                low = cz            # Bild noch zu hoch -> Kamera hoeher
            else:
                high = cz
        cz = (low + high) / 2.0
        cam = (cx + direction[0] * distance, cy + direction[1] * distance, cz)
        return cam

    def point_span(distance: float) -> float:
        cam = placement(distance)
        _, _, ymin, ymax = _span_over([head_point, feet_point], cam, forward,
                                      right, up, tan_h, tan_v)
        return ymax - ymin

    # Abstand per Bisektion: groesserer Abstand = kleinerer Ausschnitt.
    low_d, high_d = 0.05, 500.0
    for _ in range(70):
        mid = (low_d + high_d) / 2.0
        if point_span(mid) > target_span:
            low_d = mid
        else:
            high_d = mid
    base_distance = (low_d + high_d) / 2.0
    # Pose-Wert: zusaetzlicher Abstand in Studs (+ = weiter weg/kleiner,
    # - = naeher dran/groesser). Die vertikale Mitte folgt weiter den
    # sichtbar_*-Prozenten, nur die Groesse im Bild aendert sich.
    distance = max(0.5, base_distance + float(camera_distance_offset_studs or 0.0))

    def content_fits(d: float):
        cam = placement(d)
        xmin, xmax, ymin, ymax = _span_over(corners, cam, forward, right, up,
                                            tan_h, tan_v)
        fits_vertical = ymin >= -1.0 - 1e-6 and ymax <= 1.0 + 1e-6
        fits_horizontal = exact or (xmin >= -1.0 - 1e-6 and xmax <= 1.0 + 1e-6)
        return fits_vertical and fits_horizontal, cam

    abgeschnitten = False
    # Der eingestellte Abstand gilt bis zur Kopfoberkante (ohne Accessoires).
    # Damit ein normaler Hut/eine Krone trotzdem nicht angeschnitten wird,
    # darf die Kamera etwas weiter zurueck (max. 25 %). Reicht das fuer ein
    # weit schwebendes Accessoire (Halo o. Ae.) nicht, bleibt der Ausschnitt
    # wie eingestellt - dann wird geloggt, wie man es ins Bild bekommt.
    if not exact and not camera_distance_offset_studs and top > 0.0 and bottom > 0.0:
        max_distance = base_distance * 1.25
        for _ in range(80):
            fits, cam = content_fits(distance)
            if fits:
                break
            if distance >= max_distance - 1e-9:
                distance = max_distance
                abgeschnitten = True
                break
            distance = min(max_distance, distance * 1.01)

    cam = placement(distance)
    head_ndc = project_ndc(head_point, cam, forward, right, up, tan_h, tan_v)
    feet_ndc = project_ndc(feet_point, cam, forward, right, up, tan_h, tan_v)
    head_pct = (1.0 - head_ndc[1]) / 2.0 * 100.0 if head_ndc else 0.0
    feet_pct = (1.0 + feet_ndc[1]) / 2.0 * 100.0 if feet_ndc else 0.0

    return {
        "cam_location": cam,
        "center": (cx, cy, cam[2]),
        "distance": distance,
        "kopf_prozent": head_pct,
        "fuss_prozent": feet_pct,
        "aufgezogen": distance > base_distance * 1.001 and not camera_distance_offset_studs,
        "abgeschnitten": abgeschnitten,
    }


def camera_direction(yaw_deg: float):
    """Blickrichtung der Kamera zum Avatar: 0 = frontal (+Y), 90 = rechts (+X)."""
    yaw = math.radians(max(-90.0, min(90.0, float(yaw_deg))))
    return (math.sin(yaw), math.cos(yaw), 0.0)


def camera_fov_tangents(cam_data, res_x: int, res_y: int):
    """tan(halber horizontaler Winkel), tan(halber vertikaler Winkel)."""
    base_fov = cam_data.angle
    if res_x >= res_y:
        h_fov = base_fov
        v_fov = 2.0 * math.atan(math.tan(h_fov / 2.0) * (res_y / res_x))
    else:
        v_fov = base_fov
        h_fov = 2.0 * math.atan(math.tan(v_fov / 2.0) * (res_x / res_y))
    return math.tan(h_fov / 2.0), math.tan(v_fov / 2.0)


def _world_to_view(cam, points):
    """Projiziert Weltpunkte mit Blenders eigener Kamera-Mathematik.

    Rueckgabe: Liste von (x, y) in 0..1 (Ursprung unten links). Das ist
    exakt die Abbildung, die Blender rendert - unabhaengig davon, wie
    Sensor-Fit und Seitenverhaeltnis intern gerechnet werden.
    """
    from bpy_extras.object_utils import world_to_camera_view

    scene = bpy.context.scene
    # WICHTIG: cam.location/rotation_euler wurden gerade erst gesetzt -
    # matrix_world waere ohne Depsgraph-Update veraltet und
    # world_to_camera_view wuerde fuer jeden Punkt dieselbe (alte)
    # Projektion liefern (Phantom-Fehler -> Kamera ins Leere -> leeres
    # Bild). Ein view_layer.update() pro Projektion waere korrekt, ist
    # aber viel zu langsam (loest die komplette Szenen-Auswertung aus -
    # der Solver ruft das hier dutzendfach auf). Stattdessen setzt
    # place() die matrix_world direkt; hier nur noch als Notnagel
    # aktualisieren, falls sie offensichtlich nicht zur Position passt.
    try:
        mw = cam.matrix_world
        loc = mw.translation
        if (Vector(cam.location) - loc).length > 1e-6:
            bpy.context.view_layer.update()
    except Exception:
        pass
    out = []
    for p in points:
        v = world_to_camera_view(scene, cam, Vector(p))
        out.append((float(v.x), float(v.y)))
    return out


def _project_points(matrix_world, cam_data, scene, points):
    """Projiziert Weltpunkte in Kamera-NDC (0..1) mit expliziter Matrix.

    Mathematisch identisch zu ``bpy_extras.world_to_camera_view`` (gleiche
    Formeln: normalisierte, invertierte matrix_world, ``view_frame`` mit
    Lens/Sensor/Shift und Szenen-Seitenverhaeltnis), liest aber NICHT
    ``obj.matrix_world`` und braucht deshalb kein Depsgraph-Update: Der
    Kamera-Solver ruft die Projektion hunderte Male pro Render - jedes
    ``view_layer.update()`` dort loest die komplette Szenen-Auswertung aus
    und kostete auf den Software-Renderern Minuten (Timeout in Schritt 6).
    """
    try:
        inverse = matrix_world.normalized().inverted()
    except Exception:
        return [(0.5, 0.5) for _ in points]
    frame = [Vector(v) for v in cam_data.view_frame(scene=scene)[:3]]
    out = []
    for p in points:
        co_local = inverse @ Vector(p)
        z = -co_local.z
        if cam_data.type != 'ORTHO':
            if z == 0.0:
                out.append((0.5, 0.5))
                continue
            f = [-(v / (v.z / z)) for v in frame]
        else:
            f = frame
        min_x, max_x = f[2].x, f[1].x
        min_y, max_y = f[1].y, f[0].y
        out.append(((co_local.x - min_x) / (max_x - min_x),
                    (co_local.y - min_y) / (max_y - min_y)))
    return out


def fit_camera_with_blender(cam, focus_xy, head_z, feet_z, content_points,
                            yaw_deg: float = 0.0,
                            top_percent: float = 6.0,
                            bottom_percent: float = 6.0,
                            exact: bool = False,
                            max_scale: float = 1.25,
                            distance_offset_studs: float = 0.0,
                            deadline_s: float = 30.0):
    """Justiert Abstand und Hoehe der Kamera mit Blenders Projektion.

    Kopfoberkante (``head_z``) und Fuesse (``feet_z``) landen exakt auf den
    gewuenschten Prozentwerten. Bei positiven Raendern geht die Kamera
    zusaetzlich hoechstens ``max_scale`` weiter zurueck, damit Accessoires
    nicht abgeschnitten werden.

    ``deadline_s`` begrenzt die Laufzeit hart (Wall-Clock): Bei breiten
    Posen (z. B. Emotes mit ausgebreiteten Armen) laeuft der Aufzieh-Loop
    viele Solver-Durchlaeufe - der Solver darf den Render-Schritt nie
    aushungern, im Zweifel gewinnt das bisher beste Ergebnis.
    """
    target_top = 1.0 - top_percent / 100.0   # y-Koordinate (0 unten, 1 oben)
    target_bottom = bottom_percent / 100.0

    t0 = time.monotonic()
    scene = bpy.context.scene
    projektionen = [0]

    def zeit_ueberschritten() -> bool:
        return (time.monotonic() - t0) > deadline_s

    direction = Vector(camera_direction(yaw_deg)).normalized()
    head_point = (focus_xy[0], focus_xy[1], head_z)
    feet_point = (focus_xy[0], focus_xy[1], feet_z)
    focus = Vector((focus_xy[0], focus_xy[1], 0.0))
    body_span = max(0.2, abs(head_z - feet_z))

    base = Vector((cam.location.x, cam.location.y, 0.0)) - focus
    start_dist = max(0.2, base.length)

    def place(dist: float, z: float):
        loc = Vector((focus.x + direction.x * dist,
                      focus.y + direction.y * dist, z))
        cam.location = loc
        target = Vector((focus.x, focus.y, z))
        quat = (target - loc).to_track_quat("-Z", "Y")
        cam.rotation_euler = quat.to_euler()
        # matrix_world sofort selbst setzen statt auf das (teure)
        # Depsgraph-/View-Layer-Update zu warten - und die Projektion
        # direkt mit DIESER Matrix rechnen (_project_points liest nie
        # obj.matrix_world, damit kein view_layer.update() noetig wird).
        matrix = None
        if Matrix is not None:
            matrix = Matrix.LocRotScale(loc, quat, Vector((1.0, 1.0, 1.0)))
            try:
                cam.matrix_world = matrix.copy()
            except Exception:
                pass
        projektionen[0] += 1
        if matrix is not None:
            (_, head_y), (_, feet_y) = _project_points(
                matrix, cam.data, scene, [head_point, feet_point])
        else:  # pragma: no cover - mathutils ist in Blender immer da
            (_, head_y), (_, feet_y) = _world_to_view(cam, [head_point, feet_point])
        return head_y, feet_y

    def solve(dist: float, z: float, max_iter: int = 30) -> tuple:
        """Newton fuer (Abstand, Hoehe) auf (Kopf, Fuesse).

        Behaelt immer das bisher beste (dist, z): Auch bei Abbruch wegen
        Zeitlimit oder singulaerem Jakobi steht danach kein Schlechtwert.
        """
        best_dist, best_z, best_err = dist, z, float("inf")
        for _ in range(max_iter):
            head_y, feet_y = place(dist, z)
            r1 = head_y - target_top
            r2 = feet_y - target_bottom
            err = abs(r1) + abs(r2)
            if err < best_err:
                best_dist, best_z, best_err = dist, z, err
            if abs(r1) < 2e-4 and abs(r2) < 2e-4:
                break
            if zeit_ueberschritten():
                break
            step_d = max(1e-4, 0.01 * dist)
            step_z = max(1e-4, 0.01 * body_span)
            head_d, feet_d = place(dist + step_d, z)
            head_z2, feet_z2 = place(dist, z + step_z)
            j11, j21 = (head_d - head_y) / step_d, (feet_d - feet_y) / step_d
            j12, j22 = (head_z2 - head_y) / step_z, (feet_z2 - feet_y) / step_z
            det = j11 * j22 - j12 * j21
            if abs(det) < 1e-12:
                break
            d_dist = (-r1 * j22 + r2 * j12) / det
            d_z = (-r2 * j11 + r1 * j21) / det
            # gedämpft, damit das Verfahren stabil konvergiert
            dist = max(0.2 * start_dist, dist + max(-0.5 * dist, min(0.5 * dist, d_dist)))
            z += max(-0.5 * body_span, min(0.5 * body_span, d_z))
        # Kamera auf den Rueckgabewert synchronisieren: content_inside() und
        # die Endkontrolle projizieren mit der zuletzt gesetzten Matrix.
        place(best_dist, best_z)
        return best_dist, best_z

    dist = start_dist
    z = float(cam.location.z)
    dist, z = solve(dist, z)
    if distance_offset_studs:
        # Abstand aus der Pose fest vorgeben: nur noch die Hoehe so
        # nachfuehren, dass die Mitte zwischen Kopf/Fuessen stimmt.
        # Gedaempft, geclampt und mit Bestwert-Sicherung: selbst wenn die
        # Projektion einmal nicht reagiert, kann die Kamera so nicht
        # Schritt fuer Schritt aus dem Bild wandern (leeres Bild!).
        dist = max(0.2, dist + float(distance_offset_studs))
        target_mid = (target_top + target_bottom) / 2.0
        z_start = z
        best_z, best_err = z, float("inf")
        for _ in range(24):
            head_y, feet_y = place(dist, z)
            error = (head_y + feet_y) / 2.0 - target_mid
            if abs(error) < best_err:
                best_z, best_err = z, abs(error)
            if abs(error) < 2e-4 or zeit_ueberschritten():
                break
            z = max(z_start - body_span, min(z_start + body_span,
                                             z + error * body_span * 0.7))
        z = best_z
        place(dist, z)

    def content_inside() -> bool:
        # Projektion ueber die Matrix aus dem letzten place()-Aufruf
        # (matrix_world wurde dort direkt gesetzt) - kein Depsgraph-Update.
        try:
            mw = cam.matrix_world.copy()
        except Exception:
            mw = None
        if mw is not None:
            points = _project_points(mw, cam.data, scene, content_points)
        else:  # pragma: no cover
            points = _world_to_view(cam, content_points)
        for x, y in points:
            if y < -1e-6 or y > 1.0 + 1e-6:
                return False
            if not exact and (x < -1e-6 or x > 1.0 + 1e-6):
                return False
        return True

    aufgezogen = False
    abgeschnitten = False
    zeitlimit = False
    if not exact and not distance_offset_studs and top_percent > 0 and bottom_percent > 0:
        max_dist = dist * max_scale
        floor = dist  # Ratsche: Aufzieh-Niveau, das nicht wieder sinkt
        while not content_inside():
            if floor >= max_dist:
                abgeschnitten = True
                break
            if zeit_ueberschritten():
                zeitlimit = True
                abgeschnitten = True
                break
            # Monoton aufziehen. FRUEHER stand hier nur `dist = min(max_dist,
            # dist * 1.03)` - aber solve() stellte den Abstand fuer Kopf/
            # Fuesse sofort wieder zurueck, dist erreichte max_dist NIE und
            # die Schleife lief endlos (breite Emote-Posen -> Render-
            # Schritt starb im Zeitlimit, der Render startete nie).
            floor = min(max_dist, floor * 1.03)
            dist, z = solve(dist, z, max_iter=8)
            dist = max(dist, floor)
            place(dist, z)
            aufgezogen = True

    head_y, feet_y = place(dist, z)
    if zeitlimit:
        log(f"Hinweis: Kamera-Nachjustierung nach {deadline_s:.0f}s Zeitlimit "
            f"abgebrochen ({projektionen[0]} Projektionen) - bestes Ergebnis "
            "wird verwendet.")
    return {
        "kopf_prozent": (1.0 - head_y) * 100.0,
        "fuss_prozent": feet_y * 100.0,
        "aufgezogen": aufgezogen,
        "abgeschnitten": abgeschnitten,
        "distance": dist,
        "projektionen": projektionen[0],
        "dauer_s": time.monotonic() - t0,
    }


def setup_camera(lo: Vector, hi: Vector, res_x: int, res_y: int,
                 yaw_deg: float = 0.0, top_percent: float = 6.0,
                 bottom_percent: float = 6.0,
                 focus_lo=None, focus_hi=None,
                 camera_distance_offset_studs: float = 0.0):
    """Kamera exakt nach Configs.toml ausrichten (perspektivisch korrekt).

    * ``yaw_deg``: 0 = frontal (Avatar schaut zur Kamera), 90 = rechte Seite,
      -90 = linke Seite.
    * ``top_percent`` / ``bottom_percent``: freier Platz in Prozent der
      Bildhoehe oberhalb der Kopfoberkante (``focus_hi``, ohne Accessoires)
      bzw. unterhalb der Fuesse (``focus_lo``). Negative Werte schneiden das
      Bild an (z. B. unten -40 -> nur der Torso ist sichtbar).
    """
    cam_data = bpy.data.cameras.new("Kamera")
    cam_data.lens = 55.0
    cam_data.sensor_width = 36.0
    cam = bpy.data.objects.new("Kamera", cam_data)
    bpy.context.scene.collection.objects.link(cam)
    bpy.context.scene.camera = cam

    tan_h, tan_v = camera_fov_tangents(cam_data, res_x, res_y)
    placement = compute_camera_placement(
        (float(lo.x), float(lo.y), float(lo.z)),
        (float(hi.x), float(hi.y), float(hi.z)),
        tan_h, tan_v,
        yaw_deg=yaw_deg,
        top_percent=top_percent,
        bottom_percent=bottom_percent,
        head_z=float(focus_hi if focus_hi is not None else hi.z),
        feet_z=float(focus_lo if focus_lo is not None else lo.z),
        camera_distance_offset_studs=camera_distance_offset_studs,
    )

    center = Vector(placement["center"])

    def apply_placement() -> None:
        """Kamera exakt auf die (reine Mathematik-)Vorberechnung setzen."""
        cam.location = Vector(placement["cam_location"])
        cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()

    apply_placement()

    radius = max(0.5, (hi - lo).length / 2.0)
    log(f"Kamera: Drehung {yaw_deg:g}° | Abstand {placement['distance']:.2f} | "
        f"oben {placement['kopf_prozent']:.1f} % / unten "
        f"{placement['fuss_prozent']:.1f} % (gewuenscht {top_percent:g} / "
        f"{bottom_percent:g})")

    # Mit Blenders eigener Projektion nachjustieren: garantiert, dass die
    # Prozentwerte im fertigen Bild wirklich stimmen (Sensor-Fit, Perspektive).
    refined = placement
    try:
        exact = top_percent < 0 or bottom_percent < 0
        corners = [(x, y, z) for x in (float(lo.x), float(hi.x))
                   for y in (float(lo.y), float(hi.y))
                   for z in (float(lo.z), float(hi.z))]
        refined = fit_camera_with_blender(
            cam,
            focus_xy=(float(lo.x + hi.x) / 2.0, float(lo.y + hi.y) / 2.0),
            head_z=float(focus_hi if focus_hi is not None else hi.z),
            feet_z=float(focus_lo if focus_lo is not None else lo.z),
            content_points=corners,
            yaw_deg=yaw_deg,
            top_percent=top_percent,
            bottom_percent=bottom_percent,
            exact=exact,
            distance_offset_studs=camera_distance_offset_studs,
        )
        # Sicherheitsnetz: unplausible Ergebnisse verwerfen.
        plausible = (
            math.isfinite(refined["kopf_prozent"])
            and math.isfinite(refined["fuss_prozent"])
            and -100.0 <= refined["kopf_prozent"] <= 100.0
            and -100.0 <= refined["fuss_prozent"] <= 100.0
            and 0.2 * placement["distance"] <= refined["distance"] <= 5.0 * placement["distance"]
        )
        if not plausible:
            log("WARNUNG: Kamera-Nachjustierung uneindeutig - "
                "Vorberechnung wird benutzt.")
            refined = placement
            # Die Nachjustierung hat die Kamera bereits verstellt - ohne
            # Zuruecksetzen bliebe sie auf der verworfenen Position stehen
            # und der Avatar waere gar nicht im Bild (leeres Bild!).
            apply_placement()
        else:
            # Endkontrolle mit frischer Projektion: Kopf/Fuesse muessen
            # grob im Bild sein, sonst lieber die Vorberechnung nehmen.
            # EIN view_layer.update() (nicht pro Projektion wie frueher im
            # Solver): erst danach liest obj.matrix_world wirklich die soeben
            # gesetzte Kameraposition, die auch der Render verwenden wird.
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass
            focus_xy = (float(lo.x + hi.x) / 2.0, float(lo.y + hi.y) / 2.0)
            (_, head_y), (_, feet_y) = _world_to_view(cam, [
                (focus_xy[0], focus_xy[1],
                 float(focus_hi if focus_hi is not None else hi.z)),
                (focus_xy[0], focus_xy[1],
                 float(focus_lo if focus_lo is not None else lo.z)),
            ])
            if not (-0.6 <= head_y <= 1.6 and -0.6 <= feet_y <= 1.6
                    and head_y > feet_y):
                log("WARNUNG: Kamera-Endkontrolle fehlgeschlagen "
                    f"(Kopf y={head_y:.2f}, Fuesse y={feet_y:.2f}) - "
                    "Vorberechnung wird benutzt.")
                refined = placement
                apply_placement()
            else:
                log(f"Kamera (Blender-Projektion): oben {refined['kopf_prozent']:.1f} % / "
                    f"unten {refined['fuss_prozent']:.1f} % | Abstand "
                    f"{refined['distance']:.2f} | Nachjustierung "
                    f"{refined.get('projektionen', 0)} Projektionen in "
                    f"{refined.get('dauer_s', 0.0):.1f}s")
    except Exception as exc:  # pragma: no cover - bpy_extras fehlt
        log(f"WARNUNG: Kamera-Nachjustierung nicht moeglich ({exc}).")
        refined = placement
        apply_placement()

    if refined.get("aufgezogen"):
        log("Hinweis: Accessoires ragen ueber Kopf/Fuesse hinaus - Bild wurde "
            "etwas aufgezogen, damit nichts abgeschnitten wird.")
    if refined.get("abgeschnitten"):
        log("Hinweis: Ein Accessoire reicht weit ueber Kopf/Fuesse hinaus und "
            "ist nicht komplett im Bild. Der Ausschnitt folgt wie eingestellt "
            "der Kopfoberkante; fuer mehr Platz in der Pose (Posen.toml) "
            "sichtbar_oben_prozent bzw. sichtbar_unten_prozent erhoehen.")
    return cam, center, radius


# ---------------------------------------------------------------------------
# Hintergrundbild: Flaeche exakt hinter dem Avatar, bis zum Bildrand
# ---------------------------------------------------------------------------

# --------------------------------------------------------------------------- #
# CI-Diagnose: Teil-/Segment-Projektion ins Bild als GitHub-Notice            #
# --------------------------------------------------------------------------- #

def _ci_escape(text) -> str:
    return (str(text).replace("%", "%25").replace("\r", "%0D")
            .replace("\n", "%0A").replace(":", "%3A"))


def _ci_notice(title: str, message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    print(f"::notice title={_ci_escape(title)}::{_ci_escape(message)}")


def emit_projection_notices(cam, imported_parts, manifest, base_dir: str,
                            res_x: int, res_y: int) -> None:
    """Gibt eine ASCII-Silhouette und Bild-BBoxen der gerenderten Teile aus.

    Grundlage ist die ECHTE Blender-Geometrie (obj.data.vertices *
    matrix_world) - exakt das, was gerendert wird. Macht Pose, Montage und
    Accessory-Platzierung ohne Bildbetrachtung lesbar.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return

    # ASCII-Silhouette der posierten Geometrie aus Render-Sicht: macht die
    # Form (Pose, Montage, Accessory) ohne Bildbetrachtung lesbar. Nutzt die
    # ECHTE Blender-Geometrie (obj.data.vertices * matrix_world) - nicht die
    # OBJ-Dateien (deren Lesen fuer Kopf/Accessory schon mal leer ging).
    cols, rows = 56, 40
    grid = [[0] * cols for _ in range(rows)]
    part_chars = []
    for obj, part in imported_parts:
        name = str((part or {}).get("name") or obj.name)
        verts = [tuple(obj.matrix_world @ v.co) for v in obj.data.vertices]
        part_chars.append(f"{name}={len(verts)}v")
        if name == "kopf_mesh":
            ch_bit = 1
        elif (part or {}).get("accessory") or name == "Handle" or "handle" in name.lower():
            ch_bit = 2
        else:
            ch_bit = 4
        proj = _world_to_view(cam, verts)
        for u, v in proj:
            if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
                continue
            cx = min(cols - 1, int(u * cols))
            cy = min(rows - 1, int((1.0 - v) * rows))
            grid[cy][cx] |= ch_bit
    if any(any(c for c in row) for row in grid):
        lines = []
        legend = {1: "H", 2: "A", 3: "X", 4: "#", 5: "h", 6: "a", 7: "@"}
        for row in grid:
            lines.append("|" + "".join(
                " " if c == 0 else legend.get(c, "?") for c in row) + "|")
        _ci_notice("Silhouette Teile", ", ".join(part_chars)
                   + " | Zeichen: # nur Koerper, H Kopf, A Accessory,"
                   " X Kopf+Acc, h Kopf+Koerper, a Acc+Koerper, @ alles")
        # Bild-BBox je Teil (u/v-Bereich 0..1, v=1 oben) - eindeutig lesbar.
        img_bits: List[str] = []
        for obj, part in imported_parts:
            name = str((part or {}).get("name") or obj.name)
            wpts = [tuple(obj.matrix_world @ v.co) for v in obj.data.vertices]
            if not wpts:
                continue
            uv = _world_to_view(cam, wpts)
            us = [p[0] for p in uv]
            vs = [p[1] for p in uv]
            img_bits.append(f"{name}: u[{min(us):.2f}..{max(us):.2f}] "
                            f"v[{min(vs):.2f}..{max(vs):.2f}]")
        for _i in range(0, len(img_bits), 3):
            _ci_notice(f"Silhouette Bild-BBox ({_i // 3 + 1})",
                       " || ".join(img_bits[_i:_i + 3]))
        # Accessory-Platzierung - bewusst GANZ AM ENDE: GitHub schneidet
        # die Annotation-Liste pro Check-Run (~20) und zeigt sie in
        # umgekehrter Reihenfolge, daher ueberleben nur die letzten
        # Notices zuverlaessig.
        acc_bits: List[str] = []
        for obj, part in imported_parts:
            if (part or {}).get("kind") != "accessory":
                continue
            raw = part.get("raw_size") or []
            rsize = part.get("rbxm_size") or []
            acc_bits.append(
                f"{(part or {}).get('name')}: Modus {part.get('mode')}, "
                f"Kette {part.get('ancestry')}, platziert "
                + ("x".join(f"{float(v):.2f}" for v in raw) if raw else "?")
                + " Studs, RBXM-Size "
                + ("x".join(f"{float(v):.2f}" for v in rsize) if rsize else "?")
                + f" - {part.get('note')}")
        for _i in range(0, len(acc_bits), 2):
            _ci_notice(f"Accessory Platzierung ({_i // 2 + 1})",
                       " || ".join(acc_bits[_i:_i + 2]))
        for _i in range(0, len(lines), 10):
            _ci_notice(f"Silhouette ({_i // 10 + 1})",
                       "\n".join(lines[_i:_i + 10]))


def background_plane_geometry(cam_location, forward, tan_h: float, tan_v: float,
                              points, margin: float = 0.08):
    """Position und Groesse der Hintergrundflaeche (reine Zahlen, testbar).
    Die Flaeche steht senkrecht zur Blickrichtung, hinter dem gesamten Avatar,
    und ist so gross, dass sie den kompletten Bildausschnitt fuellt.

    Rueckgabe: ``(location, width, height)``
    """
    fx, fy, fz = float(forward[0]), float(forward[1]), float(forward[2])
    cx, cy, cz = (float(cam_location[0]), float(cam_location[1]),
                  float(cam_location[2]))
    depth = 0.0
    for p in points:
        d = ((float(p[0]) - cx) * fx + (float(p[1]) - cy) * fy
             + (float(p[2]) - cz) * fz)
        depth = max(depth, d)
    # Ein Stueck hinter dem hintersten Punkt, damit die Flaeche nirgends
    # den Avatar schneidet.
    distance = depth + max(0.05, 0.02 * abs(depth))
    width = 2.0 * distance * tan_h * (1.0 + margin)
    height = 2.0 * distance * tan_v * (1.0 + margin)
    location = (cx + fx * distance, cy + fy * distance, cz + fz * distance)
    return location, width, height


def make_background_material(name: str, image_path: str, glossy: bool,
                             roughness: float, metallic: float,
                             specular_level: float, ior: float = 1.5):
    """Material fuer die Hintergrundflaeche (Bild als Base Color).

    ``glossy=True`` benutzt exakt die Avatar-Werte (Rauheit, Metallic,
    Specular IOR Level), ``glossy=False`` macht die Flaeche matt.
    """
    mat = bpy.data.materials.new(name)
    _ensure_use_nodes(mat)
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = nodes.get("Principled BSDF")
    output = nodes.get("Material Output") or nodes.new("ShaderNodeOutputMaterial")
    if bsdf is None:
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    for link in list(links):
        if link.to_node is output and link.to_socket.name == "Surface":
            links.remove(link)
    links.new(bsdf.outputs[0], output.inputs["Surface"])

    tex = nodes.new("ShaderNodeTexImage")
    tex.location = (-380, 300)
    tex.extension = "EXTEND"
    image = bpy.data.images.load(image_path, check_existing=True)
    try:
        image.colorspace_settings.name = "sRGB"
    except (AttributeError, TypeError):
        pass
    tex.image = image
    links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    set_input(bsdf, "Base Color", (1.0, 1.0, 1.0, 1.0))
    if glossy:
        set_input(bsdf, "Roughness", roughness)
        set_input(bsdf, "Metallic", metallic)
        set_input(bsdf, "Specular IOR Level", specular_level)
    else:
        set_input(bsdf, "Roughness", 0.92)
        set_input(bsdf, "Metallic", 0.0)
        set_input(bsdf, "Specular IOR Level", 0.15)
    set_input(bsdf, "IOR", ior)
    set_input(bsdf, "Transmission Weight", 0.0)
    return mat


def add_background_plane(cam, location, width: float, height: float,
                         image_path: str, glossy: bool, roughness: float,
                         metallic: float, specular_level: float,
                         ior: float = 1.5):
    """Erzeugt die Bildflaeche hinter dem Avatar (Ausrichtung wie die Kamera)."""
    bpy.ops.mesh.primitive_plane_add(size=1.0, location=location)
    plane = bpy.context.active_object
    plane.name = "Hintergrund"
    plane.rotation_euler = cam.rotation_euler
    plane.scale = (width, height, 1.0)
    plane.data.materials.append(make_background_material(
        "Hintergrund-Material", image_path, glossy,
        roughness=roughness, metallic=metallic,
        specular_level=specular_level, ior=ior))
    log(f"Hintergrundbild: {image_path} auf Flaeche "
        f"{width:.2f}x{height:.2f} {'(glossy)' if glossy else '(matt)'}")
    return plane


# ---------------------------------------------------------------------------
# Render-Einstellungen (EEVEE Default in Blender 5.0, optional Cycles)
# ---------------------------------------------------------------------------


def _resolve_eevee_engine_id(scene) -> str:
    """Gibt 'BLENDER_EEVEE' (Blender 5.0) bzw. 'BLENDER_EEVEE_NEXT' (Blender 4.2) zurueck."""
    try:
        valid = {
            item.identifier
            for item in scene.render.bl_rna.properties["engine"].enum_items
        }
    except Exception:  # pragma: no cover
        valid = {"BLENDER_EEVEE"}
    if "BLENDER_EEVEE" in valid:
        return "BLENDER_EEVEE"
    if "BLENDER_EEVEE_NEXT" in valid:
        return "BLENDER_EEVEE_NEXT"
    return "BLENDER_EEVEE"


def setup_render(
    scene,
    res_x: int,
    res_y: int,
    samples: int,
    engine: str = "eevee",
    max_bounces: int = 6,
    denoise: bool = True,
    transparent: bool = False,
) -> str:
    if (engine or "eevee").lower() == "cycles":
        scene.render.engine = "CYCLES"
        scene.cycles.device = "CPU"
        scene.cycles.samples = samples
        scene.cycles.preview_samples = min(16, samples)
        scene.cycles.use_adaptive_sampling = True
        scene.cycles.adaptive_threshold = 0.02
        scene.cycles.use_denoising = denoise
        if denoise:
            try:
                scene.cycles.denoiser = "OPENIMAGEDENOISE"
            except TypeError:  # pragma: no cover
                pass
        scene.cycles.max_bounces = max_bounces
        scene.cycles.diffuse_bounces = 3
        scene.cycles.glossy_bounces = 4
        scene.cycles.transmission_bounces = max(4, max_bounces)
        scene.cycles.transparent_max_bounces = max(4, max_bounces)
        scene.cycles.volume_bounces = 1
        if hasattr(scene.cycles, "caustics_reflective"):
            scene.cycles.caustics_reflective = False
            scene.cycles.caustics_refractive = False
    else:
        eevee_id = _resolve_eevee_engine_id(scene)
        scene.render.engine = eevee_id
        eevee = scene.eevee
        if hasattr(eevee, "taa_render_samples"):
            eevee.taa_render_samples = samples
        if hasattr(eevee, "taa_samples"):
            eevee.taa_samples = min(16, samples)
        if hasattr(eevee, "use_shadows"):
            eevee.use_shadows = True
        if hasattr(eevee, "use_raytracing"):
            eevee.use_raytracing = True
        # Raytracing nur in Viertel-Aufloesung: Auf den GitHub-Runnern
        # rendert EEVEE per Software (llvmpipe/lavapipe, keine GPU) -
        # Raytracing in voller Aufloesung ist dort der mit Abstand
        # teuerste Schritt und liess Schritt 6 ins Job-Timeout laufen.
        # Viertel-Aufloesung behaelt die Spiegelungen, ist aber um
        # Groessenordnungen schneller.
        ray_options = getattr(eevee, "ray_tracing_options", None)
        if ray_options is not None:
            try:
                ray_options.resolution_scale = "1/4"
            except (TypeError, AttributeError):
                pass

    scene.render.resolution_x = res_x
    scene.render.resolution_y = res_y
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.film_transparent = bool(transparent)

    try:
        scene.display_settings.display_device = "sRGB"
    except (TypeError, AttributeError):
        pass
    try:
        scene.view_settings.view_transform = "AgX"
    except (TypeError, AttributeError):
        try:
            scene.view_settings.view_transform = "Filmic"
        except (TypeError, AttributeError):
            pass
    try:
        scene.view_settings.look = "None"
    except (TypeError, AttributeError):
        pass

    return scene.render.engine


# ---------------------------------------------------------------------------
# Hauptfunktion
# ---------------------------------------------------------------------------


def main() -> int:
    args = parse_args()
    started = time.time()

    if not os.path.exists(args.manifest):
        log(f"FEHLER: Manifest {args.manifest} nicht gefunden.")
        return 2
    with open(args.manifest, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    base_dir = os.path.dirname(os.path.abspath(args.manifest))
    res_x, res_y = (int(v) for v in args.resolution.lower().split("x"))

    # ---- Werte aus Configs.toml (Manifest-Block "render") -----------------
    render_cfg = manifest.get("render") or {}
    yaw_deg = (args.yaw if args.yaw is not None
               else float(render_cfg.get("drehung_grad") or 0.0))
    top_percent = (args.margin_top if args.margin_top is not None
                   else float(render_cfg.get("sichtbar_oben_prozent", 6.0)))
    bottom_percent = (args.margin_bottom if args.margin_bottom is not None
                      else float(render_cfg.get("sichtbar_unten_prozent", 6.0)))
    camera_distance_offset = float(render_cfg.get("kamera_abstand_studs", 0.0) or 0.0)

    background_cfg = render_cfg.get("hintergrund") or {}
    background_path = ""
    if args.background_image:
        background_path = args.background_image
    elif background_cfg.get("datei"):
        candidate = os.path.join(base_dir, background_cfg["datei"])
        if os.path.isfile(candidate):
            background_path = candidate
        else:
            log(f"WARNUNG: Hintergrundbild {candidate} fehlt - HDRI wird benutzt.")
    background_glossy = bool(background_cfg.get("glossy")) or args.background_glossy
    transparent = bool(background_cfg.get("transparent")) or args.transparent
    if background_path:
        transparent = False

    reset_scene()
    scene = bpy.context.scene
    active_engine = setup_render(
        scene,
        res_x=res_x,
        res_y=res_y,
        samples=args.samples,
        engine=args.engine,
        max_bounces=args.max_bounces,
        denoise=args.denoise,
        transparent=transparent,
    )
    log(f"Bildausschnitt (aus Pose): Drehung {yaw_deg:g}°, sichtbar oben {top_percent:g} %, "
        f"unten {bottom_percent:g} %, Kamera-Abstand {camera_distance_offset:+g} Studs | Hintergrund: "
        + ("transparent" if transparent else
           (os.path.basename(background_path) + (" (glossy)" if background_glossy else " (matt)"))
           if background_path else "HDRI"))
    loaded_exr = setup_world(
        env_texture_path=args.env_texture,
        env_strength=args.env_strength,
        fallback_color_hex=args.background,
    )

    glass_tint = hex_to_rgb(args.glass_tint)
    metal_tint = hex_to_rgb(args.metal_tint)

    log(f"Manifest: {args.manifest} ({len(manifest.get('parts', []))} Teile), "
        f"{res_x}x{res_y}, {args.samples} Samples, Engine {active_engine} "
        f"(Blender {bpy.app.version_string})")
    log(f"Material: Principled BSDF | Roughness={args.roughness:.2f}, "
        f"Metallic={args.metallic:.2f}, Specular IOR Level={args.specular_level:.2f}, "
        f"IOR={args.ior:.2f}, Transmission={args.transmission:.2f} | "
        f"World EXR={'aktiv' if loaded_exr else 'Fallback'}")

    imported = []
    imported_parts = []  # (Blender-Objekt, Manifest-Eintrag)
    material_cache = {}
    height_hint = float(manifest.get("metrics", {}).get("height") or 5.5)
    for part in manifest.get("parts", []):
        obj_path = os.path.join(base_dir, part.get("obj", ""))
        if not os.path.exists(obj_path):
            log(f"  ! fehlt: {obj_path}")
            continue
        try:
            new_objects = import_obj(obj_path)
        except Exception as exc:
            log(f"  ! Import fehlgeschlagen ({obj_path}): {exc}")
            continue
        if not new_objects:
            log(f"  ! keine Geometrie in {obj_path}")
            continue

        obj = join_objects(new_objects)
        if obj is None:
            continue
        obj.name = part.get("name", "teil")

        color = hex_to_rgb(part.get("color") or "#dfefff")
        texture = ""
        if part.get("texture"):
            candidate = os.path.join(base_dir, part["texture"])
            texture = candidate if os.path.exists(candidate) else ""

        texture_kind = part.get("texture_kind") or ""
        if part.get("kind") == "accessory" and not args.mono_glass:
            base_col = color if texture else metal_tint
            key = ("accent", texture, tuple(round(c, 3) for c in base_col),
                   args.roughness, args.metallic, args.specular_level,
                   args.transmission, args.ior, args.texture_strength,
                   texture_kind)
            mat = material_cache.get(key)
            if mat is None:
                mat = make_accent_material(
                    f"Glossy {part.get('name')}",
                    base_col,
                    texture_path=texture,
                    roughness=args.roughness,
                    transmission=args.transmission,
                    metallic=args.metallic,
                    specular_level=args.specular_level,
                    ior=args.ior,
                    texture_strength=args.texture_strength,
                    texture_kind=texture_kind,
                )
                material_cache[key] = mat
        else:
            key = ("glossy", tuple(round(c, 3) for c in color), texture,
                   args.mono_glass, args.transmission, args.roughness,
                   args.metallic, args.specular_level, args.tint_strength,
                   args.texture_strength, args.ior, texture_kind)
            mat = material_cache.get(key)
            if mat is None:
                mat = make_glass_material(
                    f"Glossy {part.get('name')}",
                    color,
                    glass_tint,
                    texture_path=texture,
                    mono=args.mono_glass,
                    transmission=args.transmission,
                    roughness=args.roughness,
                    metallic=args.metallic,
                    specular_level=args.specular_level,
                    tint_strength=args.tint_strength,
                    texture_strength=args.texture_strength,
                    ior=args.ior,
                    texture_kind=texture_kind,
                    decal=not args.mono_glass,
                )
                material_cache[key] = mat

        obj.data.materials.clear()
        obj.data.materials.append(mat)
        is_generated_box = (
            part.get("mesh_source") == "generated-box"
            or (
                part.get("mesh_source") is None
                and part.get("kind") != "accessory"
                and not part.get("asset_id")
                and part.get("name") not in ("kopf", "kopf_mesh")
            )
        )
        if is_generated_box:
            recalc_normals(obj)
        if part.get("smooth"):
            shade_smooth(obj)
        else:
            for poly in obj.data.polygons:
                poly.use_smooth = False
        bevel_width = 0.045 * (height_hint / 5.5) if is_generated_box else 0.0
        add_bevel(obj, width=bevel_width, segments=2)
        imported.append(obj)
        imported_parts.append((obj, part))

    if not imported:
        log("FEHLER: keine Geometrie importiert - Abbruch.")
        return 3

    lo, hi = scene_bounds(imported)
    height = max(hi.z - lo.z, 0.5)

    # Fuer die Prozentwerte zaehlen nur Koerperteile: die Oberkante des
    # Kopfes (nicht der Hut/Haare) und die Unterkante der Fuesse.
    body_z = []
    head_z = []
    for obj, part in imported_parts:
        if (part or {}).get("kind") != "body":
            continue
        for corner in obj.bound_box:
            world = obj.matrix_world @ Vector(corner)
            body_z.append(world.z)
            if (part or {}).get("name") in ("kopf", "kopf_mesh"):
                head_z.append(world.z)
    focus_lo = min(body_z) if body_z else lo.z
    focus_hi = max(head_z) if head_z else (max(body_z) if body_z else hi.z)
    # Headless/ohne Kopf-Mesh: die Kopfoberkante kommt aus dem Rig
    # (Manifest, nach der Pose berechnet) - sonst wuerde der freie Abstand
    # oben am Torso gemessen und Halo/Krone abgeschnitten.
    manifest_head_top = render_cfg.get("kopf_oberkante")
    if not head_z and isinstance(manifest_head_top, (int, float)):
        focus_hi = max(focus_hi, float(manifest_head_top))
        log(f"Kopfoberkante aus dem Rig (Headless/ohne Kopf-Mesh): "
            f"{float(manifest_head_top):.2f}")

    cam, center, radius = setup_camera(
        lo, hi, res_x, res_y,
        yaw_deg=yaw_deg,
        top_percent=top_percent,
        bottom_percent=bottom_percent,
        focus_lo=focus_lo,
        focus_hi=focus_hi,
        camera_distance_offset_studs=camera_distance_offset,
    )

    # CI-Diagnose: Wo landet jedes Teil/Segment im Bild? (nur in GitHub
    # Actions aktiv, sonst still.)
    try:
        emit_projection_notices(cam, imported_parts, manifest, base_dir,
                                res_x, res_y)
    except Exception as exc:  # pragma: no cover - Diagnose darf nie brechen
        log(f"Hinweis: Projektions-Diagnose nicht moeglich ({exc}).")

    # Hintergrundbild auf eine Flaeche exakt hinter dem Avatar legen.
    background_plane = None
    if background_path and not transparent:
        forward = (center - cam.location).normalized()
        tan_h, tan_v = camera_fov_tangents(cam.data, res_x, res_y)
        corners = [(x, y, z) for x in (lo.x, hi.x)
                   for y in (lo.y, hi.y) for z in (lo.z, hi.z)]
        location, plane_width, plane_height = background_plane_geometry(
            tuple(cam.location), tuple(forward), tan_h, tan_v, corners)
        background_plane = add_background_plane(
            cam, location, plane_width, plane_height, background_path, background_glossy,
            roughness=args.roughness, metallic=args.metallic,
            specular_level=args.specular_level, ior=args.ior)

    # Standardmaessig reine Beleuchtung & Spiegelungen ueber sunset_jhbcentral_4k.exr
    if args.studio_lights:
        s = height / 5.5
        add_area_light("Key", (center.x - 5.5 * s, center.y + 6.5 * s, center.z + 7.0 * s),
                       energy=350 * s * s, size=5.0 * s, color=(1.0, 0.98, 0.95), target=center)
        add_area_light("Fill", (center.x + 6.5 * s, center.y + 5.0 * s, center.z + 3.0 * s),
                       energy=180 * s * s, size=6.0 * s, color=(0.95, 0.97, 1.0), target=center)
        add_area_light("Rim", (center.x + 2.0 * s, center.y - 7.5 * s, center.z + 6.0 * s),
                       energy=250 * s * s, size=4.0 * s, color=(0.98, 0.95, 1.0), target=center)

    if args.with_floor:
        add_floor(radius, z=lo.z)

    scene.render.filepath = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(scene.render.filepath) or ".", exist_ok=True)

    log(f"Rendern nach {scene.render.filepath} ...")
    t0 = time.time()
    bpy.ops.render.render(write_still=True)
    log(f"Fertig in {time.time() - t0:.1f}s (gesamt {time.time() - started:.1f}s)")

    if args.save_blend:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(args.save_blend))
        log(f"Blend-Datei gespeichert: {args.save_blend}")

    _ = (cam, background_plane)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # pragma: no cover
        import traceback
        traceback.print_exc()
        log(f"FEHLER: {exc}")
        code = 1
    if code:
        sys.exit(code)
