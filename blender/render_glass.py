"""Blender-Script: rendert die in build/ erzeugte Avatar-Szene als "Glas-Avatar".

Aufruf (headless):
    blender -b --factory-startup --python blender/render_glass.py -- \
        --manifest build/manifest.json --out render_avatar.png \
        --resolution 1280x720 --samples 64

Alles nach dem ersten `--` geht an dieses Script. Ohne Blender-Argumente
werden sinnvolle Defaults benutzt, damit man das Script auch aus der
Blender-GUI (Texteditor -> Run Script) starten kann.

Material: Principled BSDF mit Transmission 1.0, Roughness 0.05, IOR 1.45.
Die Base Color ist ein helles Blau, das mit der jeweiligen Avatar-Farbe
(vbzw. Textur) leicht eingefaerbt wird - so bleibt der Avatar erkennbar,
wirkt aber wie Glas. Mit --mono-glass wird alles einfarbig hellblau.
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

# ---------------------------------------------------------------------------
# Argumente
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    parser = argparse.ArgumentParser(description="Glas-Render eines Roblox-Avatars")
    parser.add_argument("--manifest", default="build/manifest.json")
    parser.add_argument("--out", default="render_avatar.png")
    parser.add_argument("--resolution", default="1280x720",
                        help="Breite x Hoehe, z. B. 1280x720 oder 960x540")
    parser.add_argument("--samples", type=int, default=64)
    parser.add_argument("--max-bounces", type=int, default=6)
    parser.add_argument("--glass-tint", default="#8fd3ff",
                        help="Grundfarbe des Glases (hellblau)")
    parser.add_argument("--metal-tint", default="#cfeeff",
                        help="Farbe fuer metallisch glaenzende Accessoires")
    parser.add_argument("--mono-glass", action="store_true",
                        help="alle Teile in derselben hellblauen Glasfarbe")
    parser.add_argument("--no-floor", action="store_true")
    parser.add_argument("--background", default="#dfe8f2")
    parser.add_argument("--denoise/--no-denoise", default=True, dest="denoise")
    parser.add_argument("--glass-transmission", type=float, default=None,
                        help="Durchsichtigkeit des Glases (0..1, Default 0.78)")
    parser.add_argument("--glass-translucent", type=float, default=None,
                        help="Streuanteil an Texturflaechen (0..1, Default 0.08)")
    parser.add_argument("--glass-saturation", type=float, default=None,
                        help="Saettigung der Texturen (Default 1.15)")
    parser.add_argument("--save-blend", default="", help="optional .blend speichern")
    return parser.parse_args(argv)


def log(msg: str) -> None:
    print(f"[render] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Farb-Helfer (kein zusaetzlicher Import aus tools/, haelt das Script autark)
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


# ---------------------------------------------------------------------------
# Szene
# ---------------------------------------------------------------------------


def reset_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.system = "NONE"


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


# Feinabstimmung des Glas-Looks (per --glass-transmission /
# --glass-translucent / --glass-saturation auf der Kommandozeile).
# Wichtig: Roblox-Texturen sind kräftig; ein hoher Transmission-Wert laesst
# sie im Glas verschwinden (das war der Grund fuer "kaum sichtbar, immer
# dunkelgrau"). Deshalb ist Texturfarbe die Basis, Transmission nur ein
# Zusatz - und Texturflaechen werden zusaetzlich leicht gesaettigt.
GLASS_TRANSMISSION = 0.78
GLASS_TRANSLUCENT = 0.08
GLASS_SATURATION = 1.15
TEXTURED_OPACITY = 0.58      # Texturflaechen sind weniger durchsichtig


def load_texture_image(texture_path: str):
    """Bild laden (sRGB) - None, wenn es nicht geht."""
    if not texture_path or not os.path.exists(texture_path):
        return None
    try:
        img = bpy.data.images.load(texture_path, check_existing=True)
    except Exception as exc:  # pragma: no cover
        log(f"  ! Textur {texture_path} konnte nicht geladen werden: {exc}")
        return None
    try:
        img.colorspace_settings.name = "sRGB"
    except (AttributeError, TypeError):  # pragma: no cover
        pass
    return img


def make_glass_material(name: str, base_rgb, tint_rgb, texture_path: str = "",
                        mono: bool = False):
    """Glasiges Material mit sichtbarer Roblox-Textur.

    Die drei Fehler des alten Materials:

    1. Die Textur wurde per MULTIPLY mit der blauen Glastinte verrechnet -
       Rot/Gruen wurden dadurch fast schwarz ("immer dunkelgrau").
    2. Es wurde das *komplette* Template als Textur benutzt (statt des
       passenden Feldes, siehe tools/clothing_uv.py) - falsche Muster.
    3. Transmission 1.0 + AgX liess die Farbe komplett in der hellen
       Umgebung verschwinden ("ganz schwach sichtbar").

    Jetzt: Textur (samt Alphakanal) ist die Basis, die Glastinte kommt nur
    dort durch, wo das Template transparent ist. Dazu ein kleiner
    Streuanteil und ein klarer Glanz-Layer - Glas-Look bleibt, die
    Kleidung ist aber eindeutig erkennbar.
    """
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    tree = mat.node_tree
    nodes, links = tree.nodes, tree.links

    out = nodes.get("Material Output") or nodes.new("ShaderNodeOutputMaterial")
    bsdf = nodes.get("Principled BSDF")
    if bsdf is None:  # pragma: no cover - sehr alte Blender-Version
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        links.new(bsdf.outputs[0], out.inputs[0])

    glass_color = srgb_to_linear(
        tuple(tint_rgb) if mono else mix(tup(tint_rgb), tup(base_rgb), 0.45))

    set_input(bsdf, "Roughness", 0.05)
    set_input(bsdf, "IOR", 1.45)
    set_input(bsdf, ("Transmission Weight", "Transmission"), GLASS_TRANSMISSION)
    set_input(bsdf, ("Specular IOR Level", "Specular"), 0.5)
    set_input(bsdf, "Metallic", 0.0)
    set_input(bsdf, ("Coat Weight", "Clearcoat"), 0.55)
    set_input(bsdf, ("Coat Roughness", "Clearcoat Roughness"), 0.03)
    set_input(bsdf, ("Subsurface Weight", "Subsurface"), 0.0)
    set_input(bsdf, "Base Color", (glass_color[0], glass_color[1], glass_color[2], 1.0))

    img = load_texture_image(texture_path)
    if img is None:
        return mat

    tex = nodes.new("ShaderNodeTexImage")
    tex.image = img
    tex.projection = "FLAT"
    tex.extension = "EXTEND"
    tex.interpolation = "Linear"
    tex.location = (-900, 250)

    # Texturen etwas kraeftiger: Roblox-Texturen sind fuer In-Game-Licht
    # gemalt und wirken in einer hellen Studio-Szene sonst blass.
    hue = nodes.new("ShaderNodeHueSaturation")
    hue.inputs["Saturation"].default_value = GLASS_SATURATION
    hue.location = (-640, 300)
    links.new(tex.outputs["Color"], hue.inputs["Color"])

    # Alpha = "hier ist Stoff". Transparente Template-Bereiche fallen auf
    # die Koerperfarbe zurueck (statt schwarz zu werden).
    alpha = nodes.new("ShaderNodeMath")
    alpha.operation = "MULTIPLY"
    alpha.inputs[1].default_value = 1.0
    alpha.location = (-640, 40)
    links.new(tex.outputs["Alpha"], alpha.inputs[0])

    blend = nodes.new("ShaderNodeMixRGB")
    blend.blend_type = "MIX"
    blend.inputs["Color1"].default_value = (glass_color[0], glass_color[1],
                                            glass_color[2], 1.0)
    blend.location = (-380, 250)
    links.new(alpha.outputs[0], blend.inputs["Fac"])
    links.new(hue.outputs["Color"], blend.inputs["Color2"])
    color_socket = blend.outputs["Color"]
    links.new(color_socket, bsdf.inputs["Base Color"])

    # Texturflaechen etwas weniger durchsichtig, damit Farbe/Muster tragen.
    if not mono:
        set_input(bsdf, ("Transmission Weight", "Transmission"),
                  GLASS_TRANSMISSION * TEXTURED_OPACITY)

    # Kleiner Streuanteil: gibt der Kleidung Tiefe, ohne den Glaseindruck
    # zu verlieren (frueher waere das bei Transmission 1.0 unsichtbar).
    translucent = nodes.new("ShaderNodeBsdfTranslucent")
    translucent.location = (-100, -220)
    links.new(color_socket, translucent.inputs["Color"])
    mix_shader = nodes.new("ShaderNodeMixShader")
    mix_shader.inputs[0].default_value = GLASS_TRANSLUCENT
    mix_shader.location = (150, 0)
    links.new(bsdf.outputs[0], mix_shader.inputs[1])
    links.new(translucent.outputs[0], mix_shader.inputs[2])
    links.new(mix_shader.outputs[0], out.inputs["Surface"])

    return mat


def make_decal_material(name: str, texture_path: str, tint_rgb=(1.0, 1.0, 1.0)):
    """Material fuer Gesichts-/T-Shirt-Decals: Bild sichtbar, Rest transparent."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    tree = mat.node_tree
    nodes, links = tree.nodes, tree.links
    bsdf = nodes.get("Principled BSDF")
    base = srgb_to_linear(tup(tint_rgb))
    set_input(bsdf, "Base Color", (base[0], base[1], base[2], 1.0))
    set_input(bsdf, "Roughness", 0.35)
    set_input(bsdf, "Metallic", 0.0)
    set_input(bsdf, ("Specular IOR Level", "Specular"), 0.3)
    img = load_texture_image(texture_path)
    if img is not None:
        tex = nodes.new("ShaderNodeTexImage")
        tex.image = img
        tex.extension = "CLIP"
        tex.location = (-600, 200)
        links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        # Alpha nur ueber den Transparent-Mix (nicht zusaetzlich am
        # Principled-BSDF, sonst wirkt die Transparenz doppelt).
        out = nodes.get("Material Output") or nodes.new("ShaderNodeOutputMaterial")
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        transparent.location = (-100, -160)
        mix_shader = nodes.new("ShaderNodeMixShader")
        mix_shader.location = (150, 60)
        links.new(tex.outputs["Alpha"], mix_shader.inputs[0])
        links.new(transparent.outputs[0], mix_shader.inputs[1])
        links.new(bsdf.outputs[0], mix_shader.inputs[2])
        links.new(mix_shader.outputs[0], out.inputs["Surface"])
        try:
            mat.blend_method = "BLEND"
        except (AttributeError, TypeError):  # pragma: no cover
            pass
    return mat


def tup(v):
    return (v[0], v[1], v[2])


def make_metal_material(name: str, color_rgb, texture_path: str = ""):
    """Metallischer Akzent fuer Accessoires (bricht das viele Glas auf).

    Mit Textur traegt das Metall die echten Farben des Accessoires - vorher
    wurde jede Accessoire-Textur ignoriert und alles einfarbig hellblau.
    """
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    c = srgb_to_linear(tup(color_rgb))
    set_input(bsdf, "Base Color", (c[0], c[1], c[2], 1.0))
    set_input(bsdf, "Metallic", 0.35)
    set_input(bsdf, "Roughness", 0.32)
    set_input(bsdf, "IOR", 1.4)

    img = load_texture_image(texture_path)
    if img is not None:
        nodes, links = mat.node_tree.nodes, mat.node_tree.links
        tex = nodes.new("ShaderNodeTexImage")
        tex.image = img
        tex.extension = "REPEAT"
        tex.location = (-600, 200)
        brighten = nodes.new("ShaderNodeMixRGB")
        brighten.blend_type = "MIX"
        brighten.inputs["Fac"].default_value = 0.12
        brighten.inputs["Color1"].default_value = (1.0, 1.0, 1.0, 1.0)
        brighten.location = (-380, 200)
        links.new(tex.outputs["Color"], brighten.inputs["Color2"])
        blend = nodes.new("ShaderNodeMixRGB")
        blend.blend_type = "MIX"
        blend.inputs["Color1"].default_value = (c[0], c[1], c[2], 1.0)
        blend.location = (-160, 200)
        links.new(tex.outputs["Alpha"], blend.inputs["Fac"])
        links.new(brighten.outputs["Color"], blend.inputs["Color2"])
        links.new(blend.outputs["Color"], bsdf.inputs["Base Color"])
    return mat


def import_obj(path: str):
    """Importiert eine OBJ-Datei und gibt die neuen Objekte zurueck."""
    before = set(bpy.data.objects)
    if hasattr(bpy.ops.wm, "obj_import"):
        bpy.ops.wm.obj_import(filepath=path, forward_axis="NEGATIVE_Z", up_axis="Y")
    else:  # pragma: no cover - Blender < 4.0
        bpy.ops.import_scene.obj(filepath=path, axis_forward="-Z", axis_up="Y")
    new = [obj for obj in bpy.data.objects if obj not in before and obj.type == "MESH"]
    return new


def join_objects(objects):
    """Fasst mehrere Mesh-Objekte zu einem zusammen (ohne Operatoren)."""
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
    """Normale nach aussen neu berechnen (nur fuer geschlossene Koerperteile)."""
    import bmesh
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        bm.to_mesh(obj.data)
        obj.data.update()
    finally:
        bm.free()


def shade_smooth(obj, angle: float = 38.0) -> None:
    """Weich schattieren, harte Kanten aber erhalten.

    Frueher wurde blind jede Flaeche weichgezeichnet - bei den
    kantigen Bundle-Meshes (Arme, Stiefel) wirkte das wie zerlaufen.
    """
    for poly in obj.data.polygons:
        poly.use_smooth = True
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    angle_rad = math.radians(angle)
    for op_name in ("shade_smooth_by_angle", "shade_auto_smooth"):
        op = getattr(bpy.ops.object, op_name, None)
        if op is None:
            continue
        try:
            op(angle=angle_rad)
            return
        except (TypeError, RuntimeError, AttributeError):
            continue


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


def setup_world(color_hex: str, strength: float = 1.3) -> None:
    world = bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    c = srgb_to_linear(hex_to_rgb(color_hex))
    if bg:
        bg.inputs["Color"].default_value = (c[0], c[1], c[2], 1.0)
        bg.inputs["Strength"].default_value = strength


def add_area_light(name: str, location, energy: float, size: float, color=(1, 1, 1)):
    data = bpy.data.lights.new(name, type="AREA")
    data.energy = energy
    data.size = size
    data.color = color
    obj = bpy.data.objects.new(name, data)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    # In Richtung Modellmitte ausrichten
    direction = Vector((0, 0, 0)) - Vector(location)
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    return obj


def add_floor(radius: float, z: float = 0.0):
    bpy.ops.mesh.primitive_plane_add(size=max(radius * 8, 20), location=(0, 0, z - 0.002))
    floor = bpy.context.active_object
    floor.name = "Boden"
    mat = bpy.data.materials.new("Boden-Material")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    set_input(bsdf, "Base Color", (0.86, 0.89, 0.93, 1.0))
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

    # Klassische 3/4-Ansicht, leicht von oben.
    # Achtung: Roblox schaut nach -Z. Der OBJ-Import dreht das Modell um
    # +90 Grad um X (Roblox +Y -> Blender +Z), dadurch zeigt das Gesicht
    # im Blender-Raum nach +Y. Die Kamera muss also auf der +Y-Seite
    # stehen, sonst rendert die Pipeline den Ruecken des Avatars.
    direction = Vector((0.62, 1.0, 0.26)).normalized()

    # Abstand so waehlen, dass das Modell das Bild gut fuellt
    h_fov = cam_data.angle
    v_fov = 2 * math.atan(math.tan(h_fov / 2) * (res_y / res_x))
    half_h = size.z / 2.0
    # projizierte Breite in Blickrichtung (X und Y tragen beide bei)
    half_w = 0.5 * (size.x * 0.85 + size.y * 0.55)
    dist_v = half_h / math.tan(v_fov / 2.0)
    dist_h = half_w / math.tan(h_fov / 2.0)
    distance = max(dist_v, dist_h, radius) * 1.12
    cam.location = center + direction * distance
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    return cam, center, radius


def setup_render(scene, res_x: int, res_y: int, samples: int, max_bounces: int,
                 denoise: bool) -> None:
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
    # Kaustik kostet auf der CPU viel Zeit und bringt bei diesem Look kaum etwas
    if hasattr(scene.cycles, "caustics_reflective"):
        scene.cycles.caustics_reflective = False
        scene.cycles.caustics_refractive = False
    scene.cycles.blur_glossy = 1.0
    scene.render.use_persistent_data = True

    scene.render.resolution_x = res_x
    scene.render.resolution_y = res_y
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.film_transparent = False

    try:
        scene.view_settings.view_transform = "AgX"
        scene.view_settings.look = "AgX - Medium High Contrast"
    except (TypeError, AttributeError):
        try:
            scene.view_settings.view_transform = "Filmic"
        except (TypeError, AttributeError):
            pass


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

    global GLASS_TRANSMISSION, GLASS_TRANSLUCENT, GLASS_SATURATION
    if args.glass_transmission is not None:
        GLASS_TRANSMISSION = max(0.0, min(1.0, args.glass_transmission))
    if args.glass_translucent is not None:
        GLASS_TRANSLUCENT = max(0.0, min(1.0, args.glass_translucent))
    if args.glass_saturation is not None:
        GLASS_SATURATION = max(0.0, args.glass_saturation)

    base_dir = os.path.dirname(os.path.abspath(args.manifest))
    res_x, res_y = (int(v) for v in args.resolution.lower().split("x"))

    reset_scene()
    scene = bpy.context.scene
    setup_world(args.background)
    setup_render(scene, res_x, res_y, args.samples, args.max_bounces, args.denoise)

    glass_tint = srgb_to_linear(hex_to_rgb(args.glass_tint))
    metal_tint = hex_to_rgb(args.metal_tint)

    log(f"Manifest: {args.manifest} ({len(manifest.get('parts', []))} Teile), "
        f"{res_x}x{res_y}, {args.samples} Samples, Cycles CPU")

    imported = []
    material_cache = {}
    # grobe Modellgroesse fuer proportionsabhaengige Effekte
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

        kind = part.get("kind") or "body"
        if kind == "decal" and texture and not args.mono_glass:
            key = ("decal", texture)
            mat = material_cache.get(key)
            if mat is None:
                mat = make_decal_material(f"Decal {part.get('name')}", texture)
                material_cache[key] = mat
        elif kind == "accessory" and not args.mono_glass:
            key = ("metal", texture, tuple(round(c, 3) for c in color))
            mat = material_cache.get(key)
            if mat is None:
                mat = make_metal_material(f"Metall {part.get('name')}", metal_tint,
                                          texture)
                material_cache[key] = mat
        else:
            key = ("glass", tuple(round(c, 3) for c in color), texture, args.mono_glass)
            mat = material_cache.get(key)
            if mat is None:
                mat = make_glass_material(f"Glas {part.get('name')}", color, glass_tint,
                                          texture, args.mono_glass)
                material_cache[key] = mat

        obj.data.materials.clear()
        obj.data.materials.append(mat)
        if kind not in ("accessory", "decal"):
            recalc_normals(obj)
        if part.get("smooth"):
            shade_smooth(obj)
        else:
            for poly in obj.data.polygons:
                poly.use_smooth = False
        bevel_width = 0.045 * (height_hint / 5.5) if kind == "body" else 0.0
        add_bevel(obj, width=bevel_width, segments=2)
        imported.append(obj)

    if not imported:
        log("FEHLER: keine Geometrie importiert - Abbruch.")
        return 3

    lo, hi = scene_bounds(imported)
    height = max(hi.z - lo.z, 0.5)
    cam, center, radius = setup_camera(lo, hi, res_x, res_y)

    # Lichter relativ zur Modellgroesse
    s = height / 5.5
    # Key/Fill von der Kameraseite (+Y = Vorderseite des Avatars), Rim von
    # hinten - so bleiben Texturen und Koerperfarben hell und lesbar.
    key = add_area_light("Key", (center.x + 5.5 * s, center.y + 6.5 * s, center.z + 7.0 * s),
                         energy=1100 * s * s, size=5.0 * s, color=(1.0, 0.97, 0.94))
    fill = add_area_light("Fill", (center.x - 6.5 * s, center.y + 4.0 * s, center.z + 2.5 * s),
                          energy=420 * s * s, size=6.0 * s, color=(0.85, 0.92, 1.0))
    rim = add_area_light("Rim", (center.x - 2.0 * s, center.y - 7.5 * s, center.z + 6.0 * s),
                         energy=700 * s * s, size=4.0 * s, color=(0.92, 0.96, 1.0))

    if not args.no_floor:
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

    _ = (key, fill, rim, cam)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # pragma: no cover
        import traceback
        traceback.print_exc()
        log(f"FEHLER: {exc}")
        code = 1
    # Bei `blender --python` beendet sich Blender nicht automatisch mit dem Code
    if code:
        sys.exit(code)
