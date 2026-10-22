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
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

import bpy
from mathutils import Vector

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


def setup_camera(lo: Vector, hi: Vector, res_x: int, res_y: int):
    center = (lo + hi) / 2.0
    size = hi - lo
    radius = max(0.5, (size.length) / 2.0)

    cam_data = bpy.data.cameras.new("Kamera")
    cam_data.lens = 55.0
    cam_data.sensor_width = 36.0
    cam = bpy.data.objects.new("Kamera", cam_data)
    bpy.context.scene.collection.objects.link(cam)
    bpy.context.scene.camera = cam

    # Klassische 3/4-Vorderansicht: `obj_import(..., forward_axis='NEGATIVE_Z', up_axis='Y')`
    # dreht Roblox -Z (Vorne) auf Blender +Y!
    direction = Vector((-0.52, 1.0, 0.24)).normalized()

    # Exakte Perspektiv-Einpassung aller 8 BBox-Ecken fuer beliebige Seitenverhaeltnisse
    # (inkl. 2000x2000 Quadrat), damit weder Hoerner/Kronen oben noch Schuhe unten
    # angeschnitten werden.
    base_fov = cam_data.angle
    if res_x >= res_y:
        h_fov = base_fov
        v_fov = 2.0 * math.atan(math.tan(h_fov / 2.0) * (res_y / res_x))
    else:
        v_fov = base_fov
        h_fov = 2.0 * math.atan(math.tan(v_fov / 2.0) * (res_x / res_y))

    fill = 0.86
    tan_h = max(1e-4, math.tan(h_fov / 2.0) * fill)
    tan_v = max(1e-4, math.tan(v_fov / 2.0) * fill)

    forward = -direction
    world_up = Vector((0.0, 0.0, 1.0))
    right = forward.cross(world_up).normalized()
    up = right.cross(forward).normalized()

    distance = radius
    for x in (lo.x, hi.x):
        for y in (lo.y, hi.y):
            for z in (lo.z, hi.z):
                offset = Vector((x, y, z)) - center
                zf = offset.dot(forward)
                xr = abs(offset.dot(right))
                yu = abs(offset.dot(up))
                distance = max(distance, xr / tan_h - zf, yu / tan_v - zf)

    cam.location = center + direction * distance
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    return cam, center, radius


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
        transparent=args.transparent,
    )
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

    if not imported:
        log("FEHLER: keine Geometrie importiert - Abbruch.")
        return 3

    lo, hi = scene_bounds(imported)
    height = max(hi.z - lo.z, 0.5)
    cam, center, radius = setup_camera(lo, hi, res_x, res_y)

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

    _ = cam
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
