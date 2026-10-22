"""Blender-Script: rendert die in build/ erzeugte Avatar-Szene als "Glas-Avatar".

Aufruf (headless):
    blender -b --factory-startup --python blender/render_glass.py -- \
        --manifest build/manifest.json --out render_avatar.png \
        --resolution 1280x720 --samples 64

Alles nach dem ersten `--` geht an dieses Script. Ohne Blender-Argumente
werden sinnvolle Defaults benutzt, damit man das Script auch aus der
Blender-GUI (Texteditor -> Run Script) starten kann.

Material ("Milchglas mit sichtbaren Texturen"): Principled BSDF mit
Transmission 0.35, Roughness 0.18, IOR 1.35. Die Base Color kommt aus der
Textur des Teils (Alpha der Shirt-/Hosen-Vorlagen wird mit der Koerperfarbe
aufgefuellt), das Glas-Blau wird nur noch schwach (--tint-strength, Default
0.18) eingemischt - vorher hat eine MULTIPLY-Mischung mit kraeftigem Blau
alle Texturen zugekleistert ("alles grau/dunkelblau").

Zusaetzlich steuert die Textur-Helligkeit Roughness und Transmission:
bedruckte Stellen bleiben kompakter und zeigen ihre Farbe, freie Stellen
bleiben glasig. Mit --mono-glass wird alles einfarbig hellblau.
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
    parser.add_argument("--transmission", type=float, default=0.65,
                        help="Glasanteil 0..1 (Default 0.65; 1.0 = starkes Glas, "
                             "Texturen verschwinden dann)")
    parser.add_argument("--ior", type=float, default=1.35,
                        help="Brechungsindex (nahe 1.0 = kaum Verzerrung, "
                             "1.45 = starkes Glas)")
    parser.add_argument("--roughness", type=float, default=0.0,
                        help="Grund-Rauheit des Glases (Default 0.0 - perfekt glatt, wie in Blender)")
    parser.add_argument("--metallic", type=float, default=0.2,
                        help="Metallness des Glases (Default 0.2 - wie in Blender eingestellt)")
    parser.add_argument("--tint-strength", type=float, default=0.10,
                        help="Wie stark das Glas-Blau eingefaerbt wird (0 = keine "
                             "Verfaerbung, 1 = alles blau)")
    parser.add_argument("--texture-strength", type=float, default=1.0,
                        help="Deckkraft der Texturen auf der Base Color (1.0 = voll)")
    parser.add_argument("--glass-tint", default="#eaf7ff",
                        help="Grundfarbe des Glases (sehr helles Blau)")
    parser.add_argument("--metal-tint", default="#d8e9f2",
                        help="Farbe fuer glaenzende Accessoires ohne Textur")
    parser.add_argument("--mono-glass", action="store_true",
                        help="alle Teile in derselben hellblauen Glasfarbe")
    parser.add_argument("--no-floor", action="store_true")
    parser.add_argument("--background", default="#eaf1f8",
                        help="Hintergrund (Kamera-Strahlen)")
    parser.add_argument("--env-color", default="#3f4d5c",
                        help="Umgebungslicht/Reflexionsfarbe des Glases")
    parser.add_argument("--env-strength", type=float, default=1.15,
                        help="Staerke des Umgebungslichts (kleiner = mehr "
                             "Kontrast im Glas)")
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


# ---------------------------------------------------------------------------
# Roblox-Kleidungsvorlagen: passendes Feld herausschneiden
# ---------------------------------------------------------------------------

# Bekannte Feldgroessen der klassischen Roblox-Vorlage (585x559):
#   Torso vorne/hinten 128x128, Aermel/Seiten 64x128, Schultern 128x64,
#   Enden 64x64. Fuer ein Koerperteil wird das erste vollstaendig
#   deckende Feld dieser Groessen benutzt.
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
    """Position des groessten vollstaendig deckenden Vorlagen-Felds.

    Rueckgabe: (u0, v0, u1, v1) in UV-Koordinaten (0..1, Ursprung unten
    links wie bei Blender-Bildern) oder None, wenn nichts passt.
    """
    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        return None
    grid = _image_alpha_grid(img)
    if grid is None or grid.size == 0:
        return None
    height, width = grid.shape
    # Ohne Transparenz ist es keine Vorlage (z. B. ein fertig gemaltes
    # Bild oder ein Mesh-Atlas) - dann bleibt die Textur unbeschnitten.
    if float((grid < 0.5).mean()) < 0.02:
        return None
    # Summed-Area-Table: damit ist "Fenster vollstaendig deckend?"
    # in wenigen numpy-Operationen beantwortet (statt jeder Pixel-Schleife).
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
    """Skaliert die UVs der Box auf das passende Vorlagen-Feld.

    Nur fuer unkomponierte Roh-Vorlagen ('shirt'/'pants') aktiv; bereits per
    `avatar_uv` komponierte R15-Texturen ('composited') und Bundle-Oberflaechen
    ('mesh_surface') besitzen exakte 1:1-UV-Koordinaten.
    """
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
    """Bild-Textur-Node fuer eine Textur-Datei (oder None)."""
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
    tex.location = (-900, 240)
    tex.label = os.path.basename(texture_path)
    return tex


def _texture_detail_nodes(nodes, links, tex, low: float, high: float):
    """Helligkeit der Textur -> Wert zwischen low und high.

    Roblox-Kleidung ist ein Template-Bild: bedruckte Stellen sind farbig
    und eher dunkel, unbedruckte (transparente) Stellen hell. Ueber diese
    Rampe bekommen bedruckte Flaechen ihre Farbe zurueck, weil sie dort
    weniger durchsichtig/rauer sind.
    """
    bw = nodes.new("ShaderNodeRGBToBW")
    bw.location = (-680, -120)
    links.new(tex.outputs["Color"], bw.inputs["Color"])
    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.location = (-480, -120)
    ramp.color_ramp.elements[0].position = 0.05
    ramp.color_ramp.elements[0].color = (low, low, low, 1.0)
    ramp.color_ramp.elements[1].position = 0.95
    ramp.color_ramp.elements[1].color = (high, high, high, 1.0)
    links.new(bw.outputs["Val"], ramp.inputs["Fac"])
    return ramp


def make_glass_material(name: str, base_rgb, tint_rgb, texture_path: str = "",
                        mono: bool = False, transmission: float = 0.65,
                        roughness: float = 0.0, metallic: float = 0.2,
                        tint_strength: float = 0.10,
                        texture_strength: float = 1.0, ior: float = 1.35,
                        texture_kind: str = "", decal: bool = True):
    """Glas-Material mit sichtbarer Textur ("farbiges Dekor auf Glas").

    Aufbau (bei vorhandener Textur):
      * Glas-Koerper: Principled mit Transmission (`transmission`), niedriger
        Roughness, hohem IOR - die Roblox-Silhouette wirkt wie Glas.
      * Textur-Ebene: zweiter Principled mit der **echten Texturfarbe**
        (Alpha 0 = Glas). Sie wird ueber den Textur-Alpha mit dem Glas
        gemischt - Kleidung/Logos bleiben also voll lesbar, waehrend
        unbekleidete Stellen (Kopf, Arme ohne Shirt) Glas bleiben.
      * Klassische Roblox-Kleidung (Shirt/Pants) nutzt nur das passende
        Feld der Vorlage (`texture_kind`), nicht das ganze 585x559-Bild.

    Vorher: eine einzige Principled-Surface mit Transmission 1.0, Base
    Color = MULTIPLY aus kraeftigem Hellblau und Textur, Roughness 0.05.
    Ergebnis: alles grau/dunkelblau, Texturen nicht erkennbar.
    """
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    tree = mat.node_tree
    nodes, links = tree.nodes, tree.links

    bsdf = nodes.get("Principled BSDF")
    if bsdf is None:  # pragma: no cover - sehr alte Blender-Version
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        out = nodes.get("Material Output") or nodes.new("ShaderNodeOutputMaterial")
        links.new(bsdf.outputs[0], out.inputs[0])
    bsdf.location = (-120, 260)
    bsdf.name = "Glas"

    base = srgb_to_linear(tup(base_rgb)) if not mono else srgb_to_linear((0.93, 0.97, 1.0))
    tint = srgb_to_linear(tup(tint_rgb))
    tint_mix = 0.0 if mono else max(0.0, min(1.0, tint_strength))
    color = mix(base, tint, tint_mix)

    set_input(bsdf, "Base Color", (color[0], color[1], color[2], 1.0))
    set_input(bsdf, "Roughness", roughness)
    set_input(bsdf, "IOR", ior)
    set_input(bsdf, ("Transmission Weight", "Transmission"), transmission)
    set_input(bsdf, ("Specular IOR Level", "Specular"), 0.5)
    set_input(bsdf, "Metallic", metallic)
    set_input(bsdf, ("Coat Weight", "Clearcoat"), 0.25)
    set_input(bsdf, ("Coat Roughness", "Clearcoat Roughness"), 0.03)

    tex = _load_texture_node(nodes, texture_path)
    if tex is None or not decal:
        return mat
    if texture_kind:
        _apply_clothing_uv(nodes, links, tex, texture_kind)

    # Textur-Ebene: deckende, leicht matte Farbe mit etwas Eigenlicht, damit
    # das Motiv auch in dunklen Reflexionen lesbar bleibt.
    surface = nodes.new("ShaderNodeBsdfPrincipled")
    surface.name = "Textur"
    surface.location = (-120, -180)
    set_input(surface, "Base Color", (color[0], color[1], color[2], 1.0))
    set_input(surface, "Roughness", max(0.25, roughness + 0.22))
    set_input(surface, "Metallic", 0.0)
    set_input(surface, "IOR", 1.45)
    set_input(surface, ("Specular IOR Level", "Specular"), 0.35)
    set_input(surface, ("Coat Weight", "Clearcoat"), 0.1)

    if texture_kind in ("composited", "mesh_surface"):
        links.new(tex.outputs["Color"], surface.inputs["Base Color"])
    else:
        mix_color = nodes.new("ShaderNodeMixRGB")
        mix_color.blend_type = "MIX"
        mix_color.location = (-420, -60)
        mix_color.inputs["Color1"].default_value = (color[0], color[1], color[2], 1.0)
        links.new(tex.outputs["Color"], mix_color.inputs["Color2"])
        links.new(tex.outputs["Alpha"], mix_color.inputs["Fac"])
        links.new(mix_color.outputs["Color"], surface.inputs["Base Color"])
    set_input(surface, ("Emission Color", "Emission"), (0.0, 0.0, 0.0, 1.0))

    # Deckkraft der Textur: Alpha * texture_strength
    alpha = tex.outputs["Alpha"]
    if texture_strength < 0.999:
        strength = nodes.new("ShaderNodeMath")
        strength.operation = "MULTIPLY"
        strength.location = (-620, -240)
        strength.inputs[1].default_value = max(0.0, min(1.0, texture_strength))
        links.new(alpha, strength.inputs[0])
        alpha = strength.outputs["Value"]

    mix_shader = nodes.new("ShaderNodeMixShader")
    mix_shader.location = (220, 80)
    links.new(alpha, mix_shader.inputs["Fac"])
    links.new(bsdf.outputs[0], mix_shader.inputs[1])
    links.new(surface.outputs[0], mix_shader.inputs[2])
    out = nodes.get("Material Output") or nodes.new("ShaderNodeOutputMaterial")
    # Bestehende Verbindung zur Ausgabe ersetzen
    for link in list(links):
        if link.to_node is out and link.to_socket.name == "Surface":
            links.remove(link)
    out.location = (460, 80)
    links.new(mix_shader.outputs[0], out.inputs["Surface"])
    return mat


def tup(v):
    return (v[0], v[1], v[2])


def make_accent_material(name: str, color_rgb, texture_path: str = "",
                         roughness: float = 0.22, transmission: float = 0.10,
                         texture_strength: float = 1.0, texture_kind: str = ""):
    """Material fuer Accessoires (Huete, Badges, ...).

    Frueher war das pures Metall (Metallic 0.95) in Hellblau - dadurch
    waren Huete nur weisse/blaue Silhouetten und ihre Texturen unsichtbar.
    Jetzt: glaenzend, leicht metallisch, mit der echten Textur als
    Base Color, damit man Huete und Abzeichen erkennt.
    """
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    tree = mat.node_tree
    nodes, links = tree.nodes, tree.links
    bsdf = nodes.get("Principled BSDF")

    c = srgb_to_linear(tup(color_rgb))
    set_input(bsdf, "Base Color", (c[0], c[1], c[2], 1.0))
    set_input(bsdf, "Metallic", 0.35)
    set_input(bsdf, "Roughness", roughness)
    set_input(bsdf, "IOR", 1.45)
    set_input(bsdf, ("Transmission Weight", "Transmission"), transmission)
    set_input(bsdf, ("Coat Weight", "Clearcoat"), 0.3)
    set_input(bsdf, ("Coat Roughness", "Clearcoat Roughness"), 0.05)

    tex = _load_texture_node(nodes, texture_path)
    if tex is not None:
        if texture_kind:
            _apply_clothing_uv(nodes, links, tex, texture_kind)
        mix_color = nodes.new("ShaderNodeMixRGB")
        mix_color.blend_type = "MIX"
        mix_color.location = (-380, 240)
        mix_color.inputs["Color1"].default_value = (c[0], c[1], c[2], 1.0)
        links.new(tex.outputs["Color"], mix_color.inputs["Color2"])
        alpha = tex.outputs["Alpha"]
        strength = nodes.new("ShaderNodeMath")
        strength.operation = "MULTIPLY"
        strength.location = (-680, 60)
        strength.inputs[1].default_value = max(0.0, min(1.0, texture_strength))
        links.new(alpha, strength.inputs[0])
        links.new(strength.outputs["Value"], mix_color.inputs["Fac"])
        links.new(mix_color.outputs["Color"], bsdf.inputs["Base Color"])
        # Texturierte Accessoires (Stoff, Haare, Rosen, Krone, Abzeichen):
        # seidiger statt spiegelnder Glanz, damit dunkle Motive nicht unter
        # weissen Clearcoat-Spiegelstreifen verschwinden.
        ramp = _texture_detail_nodes(nodes, links, tex,
                                     low=max(0.32, roughness + 0.25),
                                     high=min(0.85, roughness + 0.55))
        links.new(ramp.outputs["Color"], bsdf.inputs["Roughness"])
        set_input(bsdf, "Metallic", 0.05)
        set_input(bsdf, ("Coat Weight", "Clearcoat"), 0.04)
        set_input(bsdf, ("Coat Roughness", "Clearcoat Roughness"), 0.25)
        return mat

    # Ohne Textur bleibt ein sauberer, leicht metallischer Akzent
    set_input(bsdf, "Metallic", 0.85)
    set_input(bsdf, "Roughness", min(0.5, roughness))
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


def setup_world(color_hex: str, strength: float = 1.3,
                env_hex: str = "", env_strength: float = 0.55) -> None:
    """Welt mit getrennter Hintergrund- und Beleuchtungsfarbe.

    Ein rein helles Umgebungslicht (Strength 1.3, fast weiss) hat das
    Glas von innen ausgeleuchtet: alles wurde milchig-grau und die
    Texturen verschwanden. Deshalb:
      * Kamera-Strahlen sehen den hellen Hintergrund (`color_hex`)
      * alle anderen Strahlen sehen ein gedaempftes, leicht blaues
        Umgebungslicht (`env_hex`, `env_strength`) - das gibt dem Glas
        dunkle Reflexe und damit Kontrast ("Glas-Look").
    """
    world = bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    tree = world.node_tree
    nodes, links = tree.nodes, tree.links
    bg = nodes.get("Background")
    output = nodes.get("World Output") or nodes.new("ShaderNodeOutputWorld")
    backdrop = srgb_to_linear(hex_to_rgb(color_hex))
    if bg:
        bg.inputs["Color"].default_value = (backdrop[0], backdrop[1], backdrop[2], 1.0)
        bg.inputs["Strength"].default_value = strength
    if not env_hex:
        return
    env = srgb_to_linear(hex_to_rgb(env_hex))
    light_path = nodes.new("ShaderNodeLightPath")
    light_path.location = (-620, 320)
    mixn = nodes.new("ShaderNodeMixRGB")
    mixn.blend_type = "MIX"
    mixn.location = (-360, 240)
    mixn.inputs["Color1"].default_value = (env[0], env[1], env[2], 1.0)
    mixn.inputs["Color2"].default_value = (backdrop[0], backdrop[1], backdrop[2], 1.0)
    links.new(light_path.outputs["Is Camera Ray"], mixn.inputs["Fac"])
    env_bg = nodes.new("ShaderNodeBackground")
    env_bg.location = (-80, 240)
    env_bg.inputs["Strength"].default_value = env_strength
    links.new(mixn.outputs["Color"], env_bg.inputs["Color"])
    links.new(env_bg.outputs["Background"], output.inputs["Surface"])


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
    mat.use_nodes = True
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

    base_dir = os.path.dirname(os.path.abspath(args.manifest))
    res_x, res_y = (int(v) for v in args.resolution.lower().split("x"))

    reset_scene()
    scene = bpy.context.scene
    setup_world(args.background, env_hex=args.env_color,
                env_strength=args.env_strength)
    setup_render(scene, res_x, res_y, args.samples, args.max_bounces, args.denoise)

    glass_tint = srgb_to_linear(hex_to_rgb(args.glass_tint))
    metal_tint = hex_to_rgb(args.metal_tint)

    log(f"Manifest: {args.manifest} ({len(manifest.get('parts', []))} Teile), "
        f"{res_x}x{res_y}, {args.samples} Samples, Cycles CPU")
    log(f"Material: Transmission {args.transmission:.2f}, Roughness "
        f"{args.roughness:.2f}, Metallic {args.metallic:.2f}, IOR {args.ior:.2f}, Tint {args.tint_strength:.2f}, "
        f"Textur {args.texture_strength:.2f}")

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

        if part.get("kind") == "accessory" and not args.mono_glass:
            # Accessoires glaenzen, sind aber nicht mehr pures Weiss-Metall:
            # mit Textur zeigt der Hut sein Motiv, ohne Textur bleibt ein
            # heller, leicht metallischer Akzent.
            texture_kind = part.get("texture_kind") or ""
            key = ("accent", texture, tuple(round(c, 3) for c in color),
                   texture_kind)
            mat = material_cache.get(key)
            if mat is None:
                mat = make_accent_material(
                    f"Akzent {part.get('name')}",
                    color if texture else metal_tint,
                    texture, roughness=args.roughness + 0.08,
                    texture_strength=args.texture_strength,
                    texture_kind=texture_kind)
                material_cache[key] = mat
        else:
            texture_kind = part.get("texture_kind") or ""
            key = ("glass", tuple(round(c, 3) for c in color), texture,
                   args.mono_glass, args.transmission, args.roughness, args.metallic,
                   args.tint_strength, args.texture_strength, texture_kind)
            mat = material_cache.get(key)
            if mat is None:
                mat = make_glass_material(
                    f"Glas {part.get('name')}", color, glass_tint, texture,
                    args.mono_glass, transmission=args.transmission,
                    roughness=args.roughness, metallic=args.metallic,
                    tint_strength=args.tint_strength,
                    texture_strength=args.texture_strength, ior=args.ior,
                    texture_kind=texture_kind, decal=not args.mono_glass)
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

    # Studio-Dreipunktlicht passend zur Vorderansicht (+Y)
    s = height / 5.5
    key = add_area_light("Key", (center.x - 5.5 * s, center.y + 6.5 * s, center.z + 7.0 * s),
                         energy=900 * s * s, size=5.0 * s, color=(1.0, 0.97, 0.94), target=center)
    fill = add_area_light("Fill", (center.x + 6.5 * s, center.y + 5.0 * s, center.z + 3.0 * s),
                          energy=420 * s * s, size=6.0 * s, color=(0.88, 0.94, 1.0), target=center)
    rim = add_area_light("Rim", (center.x + 2.0 * s, center.y - 7.5 * s, center.z + 6.0 * s),
                         energy=700 * s * s, size=4.0 * s, color=(0.92, 0.96, 1.0), target=center)

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
