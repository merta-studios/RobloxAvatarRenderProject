"""Blender-Script: rendert die in build/ erzeugte Avatar-Szene als "Glas-Avatar".

Aufruf (headless):
    blender -b --factory-startup --python blender/render_glass.py -- \
        --manifest build/manifest.json --out render_avatar.png \
        --resolution 1280x720 --samples 64

Alles nach dem ersten `--` geht an dieses Script. Ohne Blender-Argumente
werden sinnvolle Defaults benutzt, damit man das Script auch aus der
Blender-GUI (Texteditor -> Run Script) starten kann.

Material: Principled BSDF mit Transmission. Koerperteile ohne Textur sind
klares Glas (Transmission 1.0), bekleidete Teile bleiben glaesern, zeigen
ihre Textur aber deutlich (Transmission ~0.6). Die Grundfarbe kommt aus
dem Manifest (Durchschnittsfarbe der jeweiligen Textur), damit ein
schwarzes Shirt nicht in Hautfarbe erstrahlt.

Wichtig fuer die Blickrichtung: der OBJ-Import (forward=-Z, up=Y) dreht
Roblox-Koordinaten (x, y, z) nach Blender (x, -z, y). Die Roblox-Front
(-Z) landet damit bei **+Y** - die Kamera steht deshalb auf der
+Y-Seite. (Vorher stand sie bei -Y und zeigte den Ruecken.)
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

# Kamera-Mathematik liegt in tools/scene_camera.py - so ist die
# "vorne ist +Y"-Konvention ohne Blender testbar.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "tools"))
from scene_camera import frame_distance  # noqa: E402

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
    parser.add_argument("--texture-strength", type=float, default=0.85,
                        help="Anteil der deckenden Textur am Glas "
                             "(0 = reines Glas, 1 = Textur pur)")
    parser.add_argument("--camera-azimuth", type=float, default=24.0,
                        help="Drehung um den Avatar in Grad (0 = frontal)")
    parser.add_argument("--camera-elevation", type=float, default=9.0,
                        help="Kamerahoehe in Grad (0 = auf Augenhoehe)")
    parser.add_argument("--no-floor", action="store_true")
    parser.add_argument("--background", default="#dfe8f2")
    parser.add_argument("--denoise/--no-denoise", default=True, dest="denoise")
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


def tup(v):
    return (v[0], v[1], v[2])


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


def new_mix_rgb(nodes, blend_type: str = "MIX", fac: float = 1.0,
                name: str = "Mix"):
    """MixRGB-Knoten (Blender 3.x/4.x/5.x-kompatibel) anlegen.

    Bevorzugt den klassischen ``ShaderNodeMixRGB``; gibt es den in einer
    kuenftigen Version nicht mehr, wird ``ShaderNodeMix`` (RGBA) benutzt.
    Rueckgabe: (node, inputs, outputs) mit inputs["fac"/"a"/"b"].
    """
    try:
        node = nodes.new("ShaderNodeMixRGB")
        node.blend_type = blend_type
        node.inputs["Fac"].default_value = fac
        return node, {"fac": node.inputs["Fac"], "a": node.inputs["Color1"],
                      "b": node.inputs["Color2"]}, node.outputs["Color"]
    except RuntimeError:  # pragma: no cover - sehr neue Blender-Version
        node = nodes.new("ShaderNodeMix")
        node.data_type = "RGBA"
        node.blend_type = blend_type
        sockets = [s for s in node.inputs if s.type == "RGBA"]
        fac_socket = next((s for s in node.inputs if s.type == "VALUE"), None)
        if fac_socket is not None:
            fac_socket.default_value = fac
        return node, {"fac": fac_socket,
                      "a": sockets[0] if sockets else node.inputs[0],
                      "b": sockets[1] if len(sockets) > 1 else node.inputs[1]}, \
            next(s for s in node.outputs if s.type == "RGBA")


def make_textured_base(nodes, links, image, body_rgb_linear, tint_linear,
                       tint_amount: float, name: str):
    """Textur -> Grundfarbe: transparente Pixel werden Koerperfarbe.

    Klassische Kleidung (585x559) ist aussenrum transparent. Ohne die
    Alpha-Mischung rendert Blender diese Bereiche schwarz - genau die
    dunklen Balken aus dem alten Render. ``tint_amount`` mischt zusaetzlich
    die Glasfarbe darueber (0 = Textur pur).
    """
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = image
    tex.projection = "FLAT"
    tex.extension = "EXTEND"
    tex.location = (-900, 260)
    tex.label = name

    alpha_mix, sockets, alpha_out = new_mix_rgb(nodes, "MIX", 1.0, "Alpha")
    alpha_mix.location = (-650, 240)
    sockets["a"].default_value = (body_rgb_linear[0], body_rgb_linear[1],
                                  body_rgb_linear[2], 1.0)
    links.new(tex.outputs["Color"], sockets["b"])
    links.new(tex.outputs["Alpha"], sockets["fac"])

    if tint_amount <= 0.0:
        return alpha_out, tex
    tint_mix, tint_sockets, tint_out = new_mix_rgb(nodes, "MIX", tint_amount,
                                                   "Tint")
    tint_mix.location = (-420, 220)
    links.new(alpha_out, tint_sockets["a"])
    tint_sockets["b"].default_value = (tint_linear[0], tint_linear[1],
                                       tint_linear[2], 1.0)
    return tint_out, tex


def _surface_socket(out_node):
    if out_node is None:
        return None
    return out_node.inputs.get("Surface") or out_node.inputs.get("Volume") or None


def make_material(name: str, body_rgb, tint_rgb, texture_path: str = "",
                  mono: bool = False, kind: str = "body",
                  texture_strength: float = 0.85):
    """Material fuer ein Teil: Glas mit sichtbarer Textur.

    ``texture_strength`` ist der Anteil des **deckenden** Textur-Shaders.
    Ein reines Glas (Transmission 1.0) zeigt von einer Textur kaum etwas -
    die Umgebung wird durchgelassen. Deshalb wird das Material aus zwei
    Shadern gemischt:

    * Glas-Shader (Transmission, Coat, IOR) fuer den Glaseindruck,
    * deckender Principled-Shader mit der Textur (Alpha gegen Koerperfarbe).

    ``texture_strength=0.7`` heisst: 70 % Textur, 30 % Glas. Ohne Textur
    (oder mit ``--mono-glass``) bleibt es beim reinen Glas.

    * ``kind="decal"``: flache Textur (T-Shirt-Grafik, Gesicht), deckend.
    * ``kind="accessory"``: mit Textur seidig-metallisch, sonst Metall.
    """
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    tree = mat.node_tree
    nodes, links = tree.nodes, tree.links

    out_node = nodes.get("Material Output")
    bsdf = nodes.get("Principled BSDF")
    if bsdf is None:  # pragma: no cover - sehr alte Blender-Version
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        out_node = out_node or nodes.new("ShaderNodeOutputMaterial")
        links.new(bsdf.outputs[0], out_node.inputs[0])

    has_texture = bool(texture_path) and os.path.exists(texture_path)
    image = None
    if has_texture:
        try:
            image = bpy.data.images.load(texture_path, check_existing=True)
        except Exception as exc:  # pragma: no cover
            log(f"  ! Textur {texture_path} konnte nicht geladen werden: {exc}")
            has_texture = False

    tint_linear = srgb_to_linear(tup(tint_rgb))
    body_linear = srgb_to_linear(tup(body_rgb))
    if mono:
        body_linear = tint_linear

    # --- Glas-Shader einrichten -------------------------------------------
    glass_color = mix(tint_linear, body_linear, 0.45)
    set_input(bsdf, "Base Color", (glass_color[0], glass_color[1],
                                   glass_color[2], 1.0))
    set_input(bsdf, "Roughness", 0.05)
    set_input(bsdf, "IOR", 1.45)
    set_input(bsdf, ("Transmission Weight", "Transmission"), 1.0)
    set_input(bsdf, ("Specular IOR Level", "Specular"), 0.5)
    set_input(bsdf, "Metallic", 0.0)
    set_input(bsdf, ("Coat Weight", "Clearcoat"), 0.2)
    set_input(bsdf, ("Coat Roughness", "Clearcoat Roughness"), 0.02)
    set_input(bsdf, ("Subsurface Weight", "Subsurface"), 0.0)

    if not has_texture or mono:
        # Ohne Textur (oder mit --mono-glass) bleibt es beim reinen Glas.
        return mat

    # Kein Glas-Tint auf der Textur: die Glasfarbe kommt ueber den
    # Shader-Mix (siehe unten), so bleibt die Textur farbecht.
    base_out, tex_node = make_textured_base(
        nodes, links, image, body_linear, tint_linear, 0.0,
        os.path.basename(texture_path))
    # Dunkle Kleidung (schwarzes Shirt, dunkle Hose) versinkt im Glas sonst
    # komplett. Ein Gamma-Knoten hebt die Mitteltöne an, ohne die Farben
    # zu verschieben - die Textur wird dadurch klar erkennbar.
    try:
        gamma_node = nodes.new("ShaderNodeGamma")
        gamma_node.location = (-560, 380)
        gamma_socket = gamma_node.inputs.get("Gamma")
        if gamma_socket is not None:
            gamma_socket.default_value = 0.72
        links.new(base_out, gamma_node.inputs["Color"])
        base_out = gamma_node.outputs["Color"]
    except (RuntimeError, KeyError):  # pragma: no cover - exotische Versionen
        pass
    try:
        tex_node.image.colorspace_settings.name = "sRGB"
    except (AttributeError, TypeError):  # pragma: no cover
        pass

    if kind == "decal":
        # Dekals sind flach und deckend: Textur + Alpha direkt auf dem BSDF.
        base_socket = bsdf.inputs.get("Base Color")
        if base_socket is not None:
            links.new(base_out, base_socket)
        alpha_in = bsdf.inputs.get("Alpha")
        if alpha_in is not None:
            links.new(tex_node.outputs["Alpha"], alpha_in)
        set_input(bsdf, "Roughness", 0.45)
        set_input(bsdf, "Metallic", 0.0)
        set_input(bsdf, ("Specular IOR Level", "Specular"), 0.35)
        set_input(bsdf, ("Transmission Weight", "Transmission"), 0.0)
        for attr, value in (("blend_method", "BLEND"),
                            ("surface_render_method", "BLENDED")):
            if hasattr(mat, attr):
                try:
                    setattr(mat, attr, value)
                except (TypeError, ValueError):
                    pass
        # Kein Backface-Culling: ein einseitiges Dekal darf nie
        # verschwinden, falls die Wickelrichtung mal abweicht.
        if hasattr(mat, "use_backface_culling"):
            mat.use_backface_culling = False
        return mat

    # --- Kleidung/Koerper mit Textur: deckender Shader + Glas mischen -----
    opaque = nodes.new("ShaderNodeBsdfPrincipled")
    opaque.location = (-200, -260)
    link_base = opaque.inputs.get("Base Color")
    if link_base is not None:
        links.new(base_out, link_base)
    if kind == "accessory":
        set_input(opaque, "Roughness", 0.28)
        set_input(opaque, "Metallic", 0.35)      # seidiger Glanz
        set_input(opaque, ("Coat Weight", "Clearcoat"), 0.5)
    else:
        # Textil: matt und ohne Lackschicht, damit Muster und Farben
        # lesbar bleiben (Glasglanz kommt vom gemischten Glas-Shader).
        set_input(opaque, "Roughness", 0.62)
        set_input(opaque, "Metallic", 0.0)
        set_input(opaque, ("Specular IOR Level", "Specular"), 0.25)
        set_input(opaque, ("Coat Weight", "Clearcoat"), 0.0)
    set_input(opaque, ("Specular IOR Level", "Specular"), 0.5)
    set_input(opaque, ("Transmission Weight", "Transmission"), 0.0)

    mix_shader = nodes.new("ShaderNodeMixShader")
    mix_shader.location = (60, -60)
    amount = max(0.0, min(1.0, float(texture_strength)))
    try:
        mix_shader.inputs[0].default_value = amount
    except (IndexError, TypeError):  # pragma: no cover
        pass
    links.new(bsdf.outputs[0], mix_shader.inputs[1])
    links.new(opaque.outputs[0], mix_shader.inputs[2])
    surface = _surface_socket(out_node)
    target = surface if surface is not None else out_node.inputs[0]
    links.new(mix_shader.outputs[0], target)
    return mat


def make_metal_material(name: str, color_rgb):
    """Metallischer Akzent fuer Accessoires ohne Textur."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    c = srgb_to_linear(tup(color_rgb))
    set_input(bsdf, "Base Color", (c[0], c[1], c[2], 1.0))
    set_input(bsdf, "Metallic", 0.95)
    set_input(bsdf, "Roughness", 0.12)
    return mat


def import_obj(path: str):
    """Importiert eine OBJ-Datei und gibt die neuen Objekte zurueck.

    Achsen bleiben bei Blenders OBJ-Default (forward=-Z, up=Y): Roblox
    ist ebenfalls Y-up mit -Z als Blickrichtung. Die Drehung nach
    Blender-Z-up macht der Importer, dabei wird aus der Roblox-Front
    (-Z) die Blender-Richtung +Y (siehe Modul-Docstring).
    """
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


def setup_camera(lo: Vector, hi: Vector, res_x: int, res_y: int,
                 azimuth: float = 24.0, elevation: float = 9.0,
                 margin: float = 1.12):
    """Kamera frontal-schraeg von vorne, Bildausschnitt aus der Bounding-Box.

    Die eigentliche Mathematik steckt in ``tools/scene_camera.py``
    (dort auch die Begruendung, warum die Kamera auf +Y steht).
    """
    center_t, location_t, distance = frame_distance(
        (lo.x, lo.y, lo.z), (hi.x, hi.y, hi.z), azimuth, elevation,
        res_x=res_x, res_y=res_y, margin=margin)
    center = Vector(center_t)

    cam_data = bpy.data.cameras.new("Kamera")
    cam_data.lens = 55.0
    cam_data.sensor_width = 36.0
    cam = bpy.data.objects.new("Kamera", cam_data)
    bpy.context.scene.collection.objects.link(cam)
    bpy.context.scene.camera = cam
    cam.location = Vector(location_t)
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    return cam, center, max(distance, 1.0)


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

    base_dir = os.path.dirname(os.path.abspath(args.manifest))
    res_x, res_y = (int(v) for v in args.resolution.lower().split("x"))

    reset_scene()
    scene = bpy.context.scene
    setup_world(args.background)
    setup_render(scene, res_x, res_y, args.samples, args.max_bounces, args.denoise)

    glass_tint = hex_to_rgb(args.glass_tint)
    metal_tint = hex_to_rgb(args.metal_tint)

    log(f"Manifest: {args.manifest} ({len(manifest.get('parts', []))} Teile), "
        f"{res_x}x{res_y}, {args.samples} Samples, Cycles CPU")

    imported = []
    material_cache = {}
    texture_cache = {}
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

        kind = part.get("kind") or "body"
        color = hex_to_rgb(part.get("color") or "#dfefff")
        texture = ""
        if part.get("texture"):
            candidate = os.path.join(base_dir, part["texture"])
            texture = candidate if os.path.exists(candidate) else ""

        key = ("mat", kind, tuple(round(c, 3) for c in color), texture,
               args.mono_glass)
        mat = material_cache.get(key)
        if mat is None:
            if kind == "accessory" and not texture and not args.mono_glass:
                mat = make_metal_material(f"Metall {part.get('name')}", metal_tint)
            else:
                mat = make_material(
                    f"{kind.capitalize()} {part.get('name')}", color, glass_tint,
                    texture, args.mono_glass, kind=kind,
                    texture_strength=max(0.0, min(1.0, args.texture_strength)))
            material_cache[key] = mat

        if texture:
            texture_cache.setdefault(texture, True)
        obj.data.materials.clear()
        obj.data.materials.append(mat)
        if kind == "body":
            recalc_normals(obj)
        if part.get("smooth"):
            shade_smooth(obj)
        else:
            for poly in obj.data.polygons:
                poly.use_smooth = False
        if kind == "body":
            add_bevel(obj, width=0.045 * (height_hint / 5.5), segments=2)
        imported.append(obj)

    if not imported:
        log("FEHLER: keine Geometrie importiert - Abbruch.")
        return 3

    lo, hi = scene_bounds(imported)
    height = max(hi.z - lo.z, 0.5)
    cam, center, distance = setup_camera(lo, hi, res_x, res_y,
                                         args.camera_azimuth, args.camera_elevation)

    # Lichter relativ zur Modellgroesse. Key/Fill kommen von vorne (+Y),
    # damit die Texturen auf Brust und Gesicht lesbar sind; der Rim von
    # hinten setzt die Silhouette.
    s = height / 5.5
    key = add_area_light("Key", (center.x + 5.5 * s, center.y + 6.5 * s,
                                 center.z + 7.0 * s),
                         energy=900 * s * s, size=5.0 * s, color=(1.0, 0.97, 0.94))
    fill = add_area_light("Fill", (center.x - 6.5 * s, center.y + 4.0 * s,
                                   center.z + 2.5 * s),
                          energy=320 * s * s, size=6.0 * s, color=(0.85, 0.92, 1.0))
    rim = add_area_light("Rim", (center.x - 2.0 * s, center.y - 7.5 * s,
                                 center.z + 6.0 * s),
                         energy=700 * s * s, size=4.0 * s, color=(0.92, 0.96, 1.0))

    if not args.no_floor:
        add_floor(distance * 0.5, z=lo.z)

    scene.render.filepath = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(scene.render.filepath) or ".", exist_ok=True)

    log(f"Kamera frontal-schraeg (Azimut {args.camera_azimuth:.0f} Grad, "
        f"Hoehe {args.camera_elevation:.0f} Grad), {len(texture_cache)} Texturen")
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
