#!/usr/bin/env python3
"""Laedt einen Roblox-Avatar und baut daraus eine Blender-Szene (Manifest + OBJs).

Beispiel:
    python3 tools/build_avatar.py --username Builderman --out build
    python3 tools/build_avatar.py --dry-run --out build   # ohne Netzwerk

Ergebnis (im Ausgabeordner):
    manifest.json    Beschreibung der Szene (Parts, Farben, Texturen)
    parts/*.obj      Geometrie (Koerper + echte Roblox-Accessoire-Meshes)
    textures/*       heruntergeladene Texturen (PNG/JPG)
    avatar.json      Rohdaten der Avatar-API (zum Debuggen)
    summary.md       Kurzbericht (fuer GitHub Step Summary)

Accessoires und die getragenen Koerperteile liefert Roblox als
RBXM-Modelle aus; die Mesh-/Textur-IDs werden mit tools/rbxm.py aus dem
Modell gelesen und danach wie bisher geladen. Das Skript braucht nur die
Standardbibliothek; fuer komprimierte RBXM-Chunks kommen optional die
Module `lz4` und `zstandard` dazu (im Workflow per pip installiert).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import avatar_rig  # noqa: E402
import avatar_uv  # noqa: E402
import rbxm  # noqa: E402
import roblox_api as api  # noqa: E402
import roblox_mesh  # noqa: E402

# Diese Koerperteile sind rund und werden weich schattiert, der Rest bleibt flach
# (glatte Normalen auf Quadern wirken sonst wie "Plattenpanzer").
SMOOTH_BODY_PARTS = {"kopf", "kopf_mesh"}

# Welche Textur auf welchem Koerperteil landet. Klassische Kleidung
# (Shirt 11 / Hose 12) liegt im 585x559-Atlas an derselben Stelle wie die
# Layered-Varianten (65/66), ist aber die "Basis" - deshalb zuerst.
# T-Shirt (2) ist ein Dekal (eigene Flaeche vor der Brust) und darf nicht
# als Koerper-Textur dienen; Gesicht (18) liegt auf dem Kopf-Mesh.
TEXTURE_PRIORITY = {
    "torso": [11, 65, 67, 68, 64, 12, 66, 69, 72],
    # Ein klassisches Shirt deckt auch die Aermel ab (die Arm-Panels
    # liegen im selben Atlas).
    "links_arm": [11, 65, 67, 68],
    "rechts_arm": [11, 65, 67, 68],
    "links_bein": [12, 66, 69, 70],
    "rechts_bein": [12, 66, 69, 71],
    "kopf_mesh": [18],
}


# AssetTypeId der getragenen Koerperteile -> Rig-Teilname. Diese Assets
# kommen als RBXM und ersetzen das jeweilige Rig-Teil durch das echte Mesh.
BODY_PART_TYPES = {
    27: "torso",        # Torso
    28: "rechts_arm",   # RightArm
    29: "links_arm",    # LeftArm
    30: "links_bein",   # LeftLeg
    31: "rechts_bein",  # RightLeg
}
HEAD_ASSET_TYPE = 17
DYNAMIC_HEAD_TYPE = 79
# Ein DynamicHead mit weniger Faces ist praktisch unsichtbar ("Headless")
HEADLESS_MAX_FACES = 20
# ... oder mit winziger Part-Groesse im RBXM (echte Koepfe: > 0,8 Studs)
HEADLESS_MAX_SIZE = 0.25
# ... oder winziger Datei-Geometrie
HEADLESS_MAX_MESH_SIZE = 0.05
# Obergrenze fuer Downloads, die erst durch RBXM-Inhalte bekannt werden
MAX_FOLLOWUP_DOWNLOADS = 40
# Sanity-Check: plausible Groesse eines einzelnen Teils in Studs
SANITY_MAX_AXIS = 12.0
SANITY_MIN_AXIS = 0.05


def log(msg: str) -> None:
    print(msg, flush=True)


def fmt_de(value: Any, nd: int = 2) -> str:
    """Zahl in deutscher Schreibweise: 5.496 -> "5,50" (Komma statt Punkt)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:.{nd}f}".replace(".", ",")


def _trimmed_size(mesh) -> tuple:
    """Robuste Ausdehnung: 1%- bis 99%-Perzentil je Achse.

    Erkennt "Speck"-Geometrie: fast alle Vertices an einem Punkt, nur
    wenige Ausreisser spannen eine grosse Bounding-Box auf (so
    verstecken manche "Headless"-Meshes ihre Unsichtbarkeit).
    """
    if not mesh.positions:
        return (0.0, 0.0, 0.0)
    out = []
    for axis in range(3):
        values = sorted(p[axis] for p in mesh.positions)
        n = len(values)
        lo_i = min(n - 1, int(n * 0.01))
        hi_i = max(0, int(n * 0.99) - 1)
        out.append(values[hi_i] - values[lo_i] if hi_i >= lo_i else 0.0)
    return tuple(out)


def head_geometry_info(mesh, rbxm_size=None):
    """Kennzahlen eines Kopf-Meshes + Headless-Verdikt.

    "Headless"-DynamicHeads (z. B. Asset 'Headless Head') verstecken
    den Kopf auf mehrere Arten - alle werden geprueft:
      * kaum Geometrie (< HEADLESS_MAX_FACES Faces)
      * winzige Part-Groesse im RBXM (< 0,25 Studs; echte Koepfe > 0,8)
      * winzige Datei-Geometrie (< 0,05 Einheiten)
      * "Speck": getrimmte Ausdehnung winzig gegenueber der BBox
      * Geometrie weit weg vom Ursprung (im Koerper versteckt)

    Rueckgabe: (ist_headless: bool, begruendung: str, kennzahlen: str)
    """
    size = mesh.size()
    center = mesh.center()
    trimmed = _trimmed_size(mesh)
    max_dim = max(size) if size else 0.0
    size_txt = "x".join(fmt_de(v, 3) for v in size)
    center_txt = "/".join(fmt_de(v, 2) for v in center)
    rbxm_txt = ("x".join(fmt_de(v, 3) for v in rbxm_size)
                if rbxm_size else "unbekannt")
    info = (f"{len(mesh.faces)} Faces, Mesh-BBox {size_txt}, "
            f"Zentrum {center_txt}, RBXM-Groesse {rbxm_txt} Studs")

    if len(mesh.faces) < HEADLESS_MAX_FACES:
        return True, "kaum Geometrie", info
    if rbxm_size and max(rbxm_size) < HEADLESS_MAX_SIZE:
        return True, "winzige Part-Groesse im RBXM", info
    if max_dim < HEADLESS_MAX_MESH_SIZE:
        return True, "winzige Datei-Geometrie", info
    if max_dim > 0 and max(trimmed) < 0.02 * max_dim:
        return True, "Punkt-Geometrie in grosser BBox versteckt", info
    if max_dim > 0 and max(abs(v) for v in center) > 3.0 * max_dim:
        return True, "Geometrie weit ausserhalb des Ursprungs versteckt", info
    return False, "", info


# Erwartete R15-Segmentnamen je Rig-Koerperteil. Bundle-RBXMs enthalten
# oft mehrere Varianten (R6/R15/LOD) - nur diese Segmente gehoeren zum
# sichtbaren R15-Koerperteil.
R15_SEGMENT_NAMES = {
    "torso": ["UpperTorso", "LowerTorso"],
    "links_arm": ["LeftUpperArm", "LeftLowerArm", "LeftHand"],
    "rechts_arm": ["RightUpperArm", "RightLowerArm", "RightHand"],
    "links_bein": ["LeftUpperLeg", "LeftLowerLeg", "LeftFoot"],
    "rechts_bein": ["RightUpperLeg", "RightLowerLeg", "RightFoot"],
}


def _normalize_name(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _pick_r15_segments(group: list, expected_names: list) -> list:
    """Aus einer Mesh-Gruppe die R15-Segmente per Instanzname auswaehlen.

    Je erwartetem Segmentnamen wird der erste passende Eintrag
    genommen (Namensgleichheit nach Normalisierung, z. B.
    'LeftUpperArm' ~ 'leftupperarm'). Liefert [] wenn nichts passt.
    """
    picked: list = []
    for expected in expected_names:
        wanted = _normalize_name(expected)
        for mesh_part in group:
            if _normalize_name(mesh_part.get("label") or "") == wanted:
                picked.append(mesh_part)
                break
    return picked


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def asset_type_id(asset: Dict[str, Any], details: Dict[str, Any]) -> int:
    """AssetTypeId aus der Avatar-API oder aus den Asset-Details."""
    at = asset.get("assetType") or {}
    if isinstance(at, dict) and at.get("id"):
        return int(at["id"])
    if details.get("assetTypeId"):
        return int(details["assetTypeId"])
    return 0


def texture_filename(asset_id: int, data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return f"{asset_id}.png"
    if data[:3] == b"\xff\xd8\xff":
        return f"{asset_id}.jpg"
    return f"{asset_id}.bin"


def write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def effective_scales(raw: Any) -> Dict[str, float]:
    """Normalisiert die Scales (fehlende Werte = 1.0 / 0.0)."""
    scales = dict(api.DEFAULT_SCALES)
    if isinstance(raw, dict):
        for key in ("height", "width", "head", "depth", "proportion", "bodyType"):
            if raw.get(key) is not None:
                try:
                    scales[key] = float(raw[key])
                except (TypeError, ValueError):
                    pass
    return scales


def effective_body_colors(raw: Any) -> Dict[str, Any]:
    colors = dict(api.DEFAULT_BODY_COLORS)
    if isinstance(raw, dict):
        for key in list(colors.keys()):
            if raw.get(key) is not None:
                colors[key] = raw[key]
    return colors


def sanity_check(mesh: roblox_mesh.MeshData, target_max: float, label: str,
                 warnings: List[str]) -> roblox_mesh.MeshData:
    """Prueft ein platziertes Teil auf plausible Groesse und skaliert sonst neu.

    Mesh-Einheiten aus RBXM-Modellen sind nicht immer Studs - ein Teil mit
    200 Studs wuerde die ganze Szene sprengen. Teile groesser als
    SANITY_MAX_AXIS oder kleiner als SANITY_MIN_AXIS werden deshalb um
    ihre Mitte auf die Zielgroesse des Ankers skaliert (mit Warnung).
    """
    size = mesh.size()
    max_axis = max(size) if size else 0.0
    if SANITY_MIN_AXIS <= max_axis <= SANITY_MAX_AXIS:
        return mesh
    if max_axis <= 0.0 or target_max <= 0.0:
        return mesh
    center = mesh.center()
    factor = target_max / max_axis
    rescaled = (
        mesh.transform(1.0, (-center[0], -center[1], -center[2]))
        .transform(factor)
        .transform(1.0, center)
    )
    warnings.append(
        f"Sanity-Check: Teil '{label}' war {fmt_de(max_axis)} Studs gross und "
        f"wurde auf {fmt_de(target_max)} Studs skaliert "
        "(Mesh-Einheiten sind nicht immer Studs)."
    )
    return rescaled


def _segment_size(mesh_part: Dict[str, Any]) -> tuple:
    """Part-Groesse eines R15-Segments in Studs (RBXM-Size, sonst Mesh-BBox)."""
    hint = mesh_part.get("rbxm_size")
    if hint and len(hint) == 3 and all(float(v) > 0.05 for v in hint):
        return tuple(float(v) for v in hint)
    return mesh_part["mesh"].size()


def _combine_segments(rig_name: str, group: List[Dict[str, Any]],
                      warnings: List[str]) -> Dict[str, Any]:
    """Mehrere R15-Segmente (Oberarm/Unterarm/Hand ...) zu einem Teil machen.

    Die Segmente werden anhand ihrer Instanznamen ausgewaehlt, auf ihre
    Part-Groesse gebracht und **senkrecht gestapelt**. Die Positionen aus
    dem RBXM beschreiben dagegen die Bind-Pose des Pakets (Arme oft
    schraeg) - uebernommen ergibt das verrenkte Gliedmassen.
    """
    segments = _pick_r15_segments(group, R15_SEGMENT_NAMES.get(rig_name, []))
    segments = [mp for mp in segments
                if mp.get("mesh") is not None and not mp["mesh"].is_empty
                and max(_segment_size(mp)) > 0.05]
    if len(segments) >= 2:
        # Zielhoehe = Abstand von Oberkante des ersten bis Unterkante des
        # letzten Segments in der Bind-Pose (ohne die schraege A-Pose).
        target_height = 0.0
        tops, bottoms = [], []
        for mp in segments:
            pos = mp.get("rbxm_position")
            if pos is None:
                continue
            half = _segment_size(mp)[1] / 2.0
            tops.append(float(pos[1]) + half)
            bottoms.append(float(pos[1]) - half)
        if tops and bottoms:
            target_height = max(tops) - min(bottoms)
        stacked = roblox_mesh.stack_segments(
            [(mp["mesh"], _segment_size(mp), mp.get("rbxm_position"))
             for mp in segments],
            source_version="stacked-r15", target_height=target_height)
        pseudo = dict(segments[0])
        pseudo["mesh"] = stacked
        pseudo["stacked"] = True
        # Part-Groesse aus den Segmenten selbst: Breite/Tiefe = dickstes
        # Segment, Hoehe = Bind-Pose. So bleibt der Torso so breit wie im
        # Bundle, auch wenn der Stapel zum Verbinden gestaucht wurde.
        widths = [_segment_size(mp) for mp in segments]
        pseudo["part_size"] = (
            max(w[0] for w in widths),
            target_height or stacked.size()[1],
            max(w[2] for w in widths))
        pseudo["source_ids"] = [id(mp) for mp in segments]
        pseudo["texture_file"] = next(
            (mp["texture_file"] for mp in segments if mp.get("texture_file")),
            None)
        names = ", ".join((mp.get("label") or "?") for mp in segments)
        pseudo["note_suffix"] = (f", {len(segments)} R15-Segmente gestapelt "
                                 f"({names})")
        return pseudo
    if len(group) == 1:
        single = dict(group[0])
        single["part_size"] = _segment_size(single)
        single["source_ids"] = [id(group[0])]
        return single
    largest = max(group, key=lambda mp: max(mp["mesh"].size()))
    replacement = dict(largest)
    replacement["part_size"] = _segment_size(replacement)
    replacement["source_ids"] = [id(mp) for mp in group]
    warnings.append(
        f"Koerperteil '{rig_name}': {len(group)} Meshes gefunden, aber kein "
        f"R15-Segmentsatz - nur das groesste Segment verwendet "
        f"(Asset {largest['asset_id']}).")
    return replacement


def _replacement_size(mesh_part: Dict[str, Any], warnings: List[str],
                      label: str, prefer_geometry: bool = False):
    """Plausible Teilgroesse (Studs) fuer den Rig-Aufbau bestimmen.

    * gestapelte R15-Glieder: Bounding-Box des Stapels,
    * einzelne Meshes: die Part-Groesse aus dem RBXM (so rendert Roblox
      das Mesh ebenfalls - gestreckt auf die Part-Groesse),
    * sonst die Mesh-Bounding-Box.

    Unplausible Werte (Mesh-Einheiten sind nicht immer Studs) werden
    verworfen, dann greifen die Standardmasse des Rigs.
    """
    size = None
    if mesh_part.get("part_size"):
        size = tuple(float(v) for v in mesh_part["part_size"])
    elif prefer_geometry:
        # Koepfe erscheinen in Roblox groesser als ihre Part-Groesse -
        # die Geometrie beschreibt den sichtbaren Kopf besser.
        size = mesh_part["mesh"].size()
    else:
        hint = mesh_part.get("rbxm_size")
        if hint and len(hint) == 3 and all(float(v) > 0 for v in hint):
            size = tuple(float(v) for v in hint)
        else:
            size = mesh_part["mesh"].size()
    if not size or not all(v > 0 for v in size):
        return None
    if not (SANITY_MIN_AXIS <= max(size) <= SANITY_MAX_AXIS):
        warnings.append(
            f"Koerperteil '{label}': Groesse {fmt_de(max(size))} Studs ist "
            "unplausibel - es gelten die Standardmasse des Rigs."
        )
        return None
    # Einzelne Teile duerfen den Koerper nicht sprengen (Roblox-Koerper
    # sind ~5-6 Studs hoch). 0,35x bis 2,5x der Standardgroesse reicht.
    base = avatar_rig.DEFAULT_PART_SIZES.get(label)
    if base:
        clamped = tuple(
            max(base[i] * 0.35, min(base[i] * 2.5, float(size[i])))
            for i in range(3))
        if any(abs(clamped[i] - float(size[i])) > 0.01 for i in range(3)):
            warnings.append(
                f"Koerperteil '{label}': Groesse auf "
                + "x".join(fmt_de(v) for v in clamped)
                + " Studs begrenzt (Roblox-Koerper sind rund 5-6 Studs hoch)."
            )
        size = clamped
    return tuple(float(v) for v in size)


# ---------------------------------------------------------------------------
# Mini-PNG-Decoder (nur Standardbibliothek) fuer die Farb-Bestimmung
# ---------------------------------------------------------------------------


def png_pixels(data: bytes):
    """Dekodiert ein PNG (8 Bit, nicht interlaced) zu (Breite, Hoehe, RGBA).

    Wird nur benutzt, um die Durchschnittsfarbe einer Kleidungs-Textur zu
    bestimmen (damit das Glas nicht in Hautfarbe erstrahlt, obwohl der
    Avatar ein schwarzes Shirt traegt). Rueckgabe ist ``None``, wenn das
    Bild nicht dekodierbar ist - der Aufrufer faellt dann auf die
    Koerperfarbe zurueck.
    """
    import struct as _struct
    import zlib as _zlib

    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos = 8
    width = height = bit_depth = color_type = interlace = 0
    palette = b""
    idat = bytearray()
    while pos + 8 <= len(data):
        length, ctype = _struct.unpack_from(">I4s", data, pos)
        pos += 8
        chunk = data[pos:pos + length]
        pos += length + 4  # Laenge + CRC
        if ctype == b"IHDR":
            (width, height, bit_depth, color_type,
             _comp, _filt, interlace) = _struct.unpack(">IIBBBBB", chunk[:13])
        elif ctype == b"PLTE":
            palette = chunk
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break
    if bit_depth != 8 or interlace != 0 or width <= 0 or height <= 0:
        return None
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        return None
    try:
        raw = _zlib.decompress(bytes(idat))
    except _zlib.error:
        return None
    stride = width * channels
    if len(raw) < (stride + 1) * height:
        return None

    out = bytearray(width * height * 4)
    prev = bytearray(stride)
    offset = 0
    for y in range(height):
        filter_type = raw[offset]
        offset += 1
        line = bytearray(raw[offset:offset + stride])
        offset += stride
        if filter_type == 1:      # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filter_type == 2:    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filter_type == 3:    # Average
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:    # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        prev = line

        for x in range(width):
            src = x * channels
            dst = (y * width + x) * 4
            if color_type == 6:
                out[dst:dst + 4] = line[src:src + 4]
            elif color_type == 2:
                out[dst:dst + 3] = line[src:src + 3]
                out[dst + 3] = 255
            elif color_type == 0:
                out[dst] = out[dst + 1] = out[dst + 2] = line[src]
                out[dst + 3] = 255
            elif color_type == 4:
                out[dst] = out[dst + 1] = out[dst + 2] = line[src]
                out[dst + 3] = line[src + 1]
            elif color_type == 3:
                index = line[src] * 3
                if index + 2 < len(palette):
                    out[dst:dst + 3] = palette[index:index + 3]
                out[dst + 3] = 255
    return width, height, bytes(out)


def texture_average_color(path: str, fallback: str,
                          alpha_min: int = 32) -> str:
    """Durchschnittsfarbe der sichtbaren Pixel einer Textur (Hex).

    Transparente Bereiche des Kleidungs-Atlasses werden ignoriert -
    sonst waere jede Hose "schwarz", weil das Template aussenrum
    transparent ist.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return fallback
    decoded = png_pixels(data)
    if not decoded:
        return fallback
    width, height, rgba = decoded
    total_r = total_g = total_b = 0
    count = 0
    step = max(1, (width * height) // 4096)  # grosse Texturen ausduennen
    for index in range(0, width * height, step):
        r, g, b, a = rgba[index * 4:index * 4 + 4]
        if a >= alpha_min:
            total_r += r
            total_g += g
            total_b += b
            count += 1
    if not count:
        return fallback
    return "#%02x%02x%02x" % (total_r // count, total_g // count, total_b // count)


def _luminance(hex_color: str) -> float:
    """Relative Helligkeit einer Hex-Farbe (0 = schwarz, 1 = weiss)."""
    value = (hex_color or "#000000").lstrip("#")
    try:
        r, g, b = (int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except (ValueError, IndexError):
        return 0.5
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


# Texturen unter dieser Helligkeit sind fuer Koepfe unbrauchbar: echte
# Gesichts-/Hautkarten sind hell, eine fast schwarze Karte ist meist eine
# Roughness-/Normal-Map aus dem RBXM (dann bleibt der Kopf sonst schwarz).
DARK_TEXTURE_LUMINANCE = 0.12


def _usable_head_texture(texture: Optional[str], out_dir: str,
                         body_color: str) -> Optional[str]:
    """Kopf-Textur nur uebernehmen, wenn sie wie ein Farbbild aussieht.

    Gesichter (DynamicHead/Face) sind fast immer durchsichtige PNGs mit
    schwarzen Augen/Brauen - die Durchschnittsfarbe ist dann schwarz, das
    Bild aber trotzdem die richtige Gesichts-Textur. Nur *flaechendeckend*
    dunkle Karten sind Zusatz-Maps (Roughness/Metallic).
    """
    if not texture:
        return None
    path = os.path.join(out_dir, texture)
    if texture_is_mostly_transparent(path, threshold=200, max_ratio=0.5):
        return texture
    color = texture_average_color(path, "", alpha_min=32)
    if not color:
        return texture          # nicht dekodierbar -> unveraendert lassen
    if _luminance(color) < DARK_TEXTURE_LUMINANCE and _luminance(body_color) > 0.2:
        return None
    return texture


def panel_average_color(path: str, part_name: str, fallback: str,
                        alpha_min: int = 64) -> str:
    """Durchschnittsfarbe **nur der Panels** eines Koerperteils (Hex).

    Ein Kleidungs-Atlas enthaelt alle Koerperflaechen plus ein weisses
    Template mit Beschriftungen. Der Durchschnitt ueber das ganze Bild ist
    deshalb grau ("alles grau/weiss") - erst der Durchschnitt ueber die
    Panels des jeweiligen Teils liefert die echte Kleidungsfarbe
    (schwarzes Shirt, blaue Jeans, ...). Nur deckende Pixel zaehlen.
    """
    regions = avatar_uv.part_regions(part_name)
    if not regions:
        return texture_average_color(path, fallback, alpha_min)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return fallback
    decoded = png_pixels(data)
    if not decoded:
        return texture_average_color(path, fallback, alpha_min)
    width, height, rgba = decoded
    total = [0, 0, 0]
    count = 0
    for (x0, y0, x1, y1) in regions.values():
        x0, x1 = max(0, int(x0)), min(width, int(x1))
        y0, y1 = max(0, int(y0)), min(height, int(y1))
        if x1 <= x0 or y1 <= y0:
            continue
        step = max(1, int(((x1 - x0) * (y1 - y0)) / 900))
        for y in range(y0, y1, step):
            row = y * width
            for x in range(x0, x1, step):
                i = (row + x) * 4
                if rgba[i + 3] < alpha_min:
                    continue
                total[0] += rgba[i]
                total[1] += rgba[i + 1]
                total[2] += rgba[i + 2]
                count += 1
    if not count:
        return fallback
    return "#%02x%02x%02x" % (total[0] // count, total[1] // count,
                              total[2] // count)


def _texture_color_hint(texture: Optional[str], body_color: str,
                        out_dir: str, part_name: str = "") -> str:
    """Farbe fuer ein Teil: Durchschnitt der Textur, sonst Koerperfarbe.

    Bei Kleidungs-Atlanten (Shirt/Pants) wird nur der Bereich der eigenen
    Panels ausgewertet, sonst zieht das weisse Template die Farbe nach
    grau.
    """
    if not texture:
        return body_color
    path = os.path.join(out_dir, texture)
    if part_name and avatar_uv.has_clothing_layout(part_name):
        return panel_average_color(path, part_name, body_color)
    return texture_average_color(path, body_color)


def texture_is_mostly_transparent(path: str, threshold: int = 200,
                                  max_ratio: float = 0.5) -> bool:
    """True, wenn der Grossteil der Textur durchsichtig ist.

    Gesichts-/Dekal-Texturen bestehen fast nur aus transparenten Pixeln mit
    Augen, Mund und Brauen. Solche Bilder sind *keine* Zusatz-Maps
    (Roughness/Normal), auch wenn ihre Durchschnittsfarbe schwarz ist.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return False
    decoded = png_pixels(data)
    if not decoded:
        return False
    width, height, rgba = decoded
    total = width * height
    if total <= 0:
        return False
    step = max(1, total // 8192)
    opaque = 0
    seen = 0
    for index in range(0, total, step):
        seen += 1
        if rgba[index * 4 + 3] >= threshold:
            opaque += 1
    return seen > 0 and (opaque / seen) < max_ratio


def texture_visible_ratio(path: str, min_alpha: int = 40) -> float:
    """Anteil der Pixel mit sichtbarer Deckkraft (0..1)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return 0.0
    decoded = png_pixels(data)
    if not decoded:
        return 1.0
    width, height, rgba = decoded
    total = width * height
    if total <= 0:
        return 0.0
    step = max(1, total // 8192)
    seen = visible = 0
    for index in range(0, total, step):
        seen += 1
        if rgba[index * 4 + 3] >= min_alpha:
            visible += 1
    return visible / seen if seen else 0.0


def texture_mean_alpha(path: str) -> float:
    """Mittlere Deckkraft einer Textur (0..1).

    Shirt-Grafiken sind deckend (1.0). Ein "Belly"-Overlay mit 10 %
    Deckkraft wuerde als Dekal nur als milchige Flaeche vor der Brust
    haengen - solche Grafiken werden uebersprungen.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return 1.0
    decoded = png_pixels(data)
    if not decoded:
        return 1.0
    width, height, rgba = decoded
    total = width * height
    if total <= 0:
        return 1.0
    step = max(1, total // 8192)
    acc = 0.0
    seen = 0
    for index in range(0, total, step):
        acc += rgba[index * 4 + 3] / 255.0
        seen += 1
    return acc / seen if seen else 1.0


def _looks_like_pair(mesh) -> bool:
    """Spricht das Mesh fuer ein Paar (Handschuhe, Stiefel)?

    Ein Paar ist deutlich breiter als hoch, flach und liegt symmetrisch um
    x = 0 (beide Seiten in einem Mesh).
    """
    size = mesh.size()
    if max(size) <= 0:
        return False
    lo, hi = mesh.bbox()
    if not (lo[0] < -0.2 * size[0] and hi[0] > 0.2 * size[0]):
        return False
    return size[0] > 1.6 * max(size[1], size[2]) and size[0] > 1.2


def _place_accessory_variants(mesh_part: Dict[str, Any], metrics,
                              warnings: List[str]) -> List[Any]:
    """Accessoire platzieren - ein oder zwei Teile.

    * Ein Paar (z. B. Handschuhe, beide Seiten in einem Mesh) wird an
      x = 0 getrennt und je Haelfte an die passende Hand gesetzt.
    * Alles andere geht durch ``avatar_rig.place_accessory``.

    Wichtig fuer die Groesse: die Part-Groesse ``size`` aus dem RBXM ist
    bei SpecialMesh-Objekten immer 1x1x1 - sie beschreibt *nicht* die
    sichtbare Groesse. Solche Meshes sind bereits in Studs modelliert und
    werden unveraendert uebernommen (nur mit ``mesh_scale`` skaliert).
    """
    mesh = mesh_part["mesh"]
    scale = mesh_part.get("rbxm_scale")
    if (isinstance(scale, (list, tuple)) and len(scale) == 3
            and all(float(v) > 0 for v in scale)
            and any(abs(float(v) - 1.0) > 1e-3 for v in scale)):
        mesh = mesh.transform(tuple(float(v) for v in scale))

    type_id = int(mesh_part["type_id"])
    if _looks_like_pair(mesh):
        left, right = roblox_mesh.split_mesh_x(mesh)
        variants = []
        for side, half in ((-1.0, left), (1.0, right)):
            if half.is_empty:
                continue
            variants.append(avatar_rig.place_hand_accessory(half, metrics, side))
        if variants:
            return variants

    # Kein size_hint: die Part-Groesse im Accessoire-RBXM ist 1x1x1 (der
    # Roblox-Accessoire-Pipe skaliert das Handle erst zur Laufzeit). Wer sie
    # als Groesse nimmt, schrumpft jeden Hut und jeden Rock auf 1 Stud.
    placed, note = avatar_rig.place_accessory(mesh, type_id, metrics)
    hint = mesh_part.get("rbxm_size")
    if (hint and len(hint) == 3 and max(float(v) for v in hint) > SANITY_MAX_AXIS):
        warnings.append(
            f"Accessoire '{mesh_part['label'] or mesh_part['type_name']}': "
            f"RBXM-Groesse {fmt_de(max(float(v) for v in hint))} Studs ist "
            f"unplausibel - auf {fmt_de(max(placed.size()))} Studs begrenzt "
            "(Mesh-Einheiten sind nicht immer Studs).")
    placed = sanity_check(
        placed, avatar_rig.accessory_target_size(type_id, metrics),
        mesh_part["label"] or mesh_part["type_name"], warnings)
    return [(placed, note)]


def _add_decals(parts_dir: str, textures: List[Dict[str, Any]],
                metrics, body_parts: List[Any], has_head_mesh: bool,
                headless: bool) -> List[Dict[str, Any]]:
    """T-Shirt-Grafik (Brust) und Gesichts-Textur als duenne Dekal-Flaechen.

    Beide Assets sind keine 585x559-Atlanten, sondern kleine Bilder fuer
    genau eine Flaeche. Ohne diesen Schritt landet gar keine Textur auf
    dem Avatar ("alles grau") - oder ein wildes Raster.
    """
    from roblox_mesh import make_plane, make_sphere_patch

    decals: List[Dict[str, Any]] = []
    torso = next((p for p in body_parts if p.name == "torso"), None)
    if torso is not None:
        tshirt = next((tex for tex in textures if int(tex["type_id"]) == 2), None)
        if tshirt is not None and texture_mean_alpha(
                os.path.join(os.path.dirname(parts_dir), tshirt["file"])) < 0.5:
            # Fast vollstaendig durchsichtige Grafik (z. B. ein kaum
            # sichtbares "Belly"-Overlay) - als Dekal wuerde sie nur als
            # weisse Flaeche vor der Brust haengen.
            tshirt = None
        if tshirt is not None:
            lo, hi = torso.mesh.bbox()
            width, depth = hi[0] - lo[0], hi[2] - lo[2]
            height = hi[1] - lo[1]
            offset = max(0.012, depth * 0.03)
            plane = make_plane(width * 0.995, height * 0.995,
                               (0.0, (lo[1] + hi[1]) / 2.0,
                                lo[2] - offset),
                               facing="-Z")
            fname = "decal_tshirt.obj"
            plane.write_obj(os.path.join(parts_dir, fname))
            decals.append({
                "name": "tshirt_decal",
                "obj": f"parts/{fname}",
                "color": "#ffffff",
                "kind": "decal",
                "smooth": True,
                "texture": tshirt["file"],
                "asset_id": tshirt["asset_id"],
                "note": "T-Shirt-Grafik als Dekal auf der Brust (UV 0..1)",
            })

    if headless or has_head_mesh:
        return decals
    face = next((tex for tex in textures if int(tex["type_id"]) == 18), None)
    if face is None:
        return decals
    radius = metrics.head_radius
    patch = make_sphere_patch(radius * 1.004,
                              (0.0, metrics.head_center_y, -0.02),
                              spread_u=0.78, spread_v=0.66)
    fname = "decal_face.obj"
    patch.write_obj(os.path.join(parts_dir, fname))
    decals.append({
        "name": "face_decal",
        "obj": f"parts/{fname}",
        "color": "#ffffff",
        "kind": "decal",
        "smooth": True,
        "texture": face["file"],
        "asset_id": face["asset_id"],
        "note": "Gesichts-Textur als gewoelbtes Dekal auf dem Kopf",
    })
    return decals


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------


def build(username_raw: str, out_dir: str, dry_run: bool = False,
          max_assets: int = 80, verbose: bool = True) -> Dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    parts_dir = os.path.join(out_dir, "parts")
    textures_dir = os.path.join(out_dir, "textures")
    os.makedirs(parts_dir, exist_ok=True)
    os.makedirs(textures_dir, exist_ok=True)

    warnings: List[str] = []
    parts: List[Dict[str, Any]] = []
    textures: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    ignored: List[Dict[str, Any]] = []
    avatar_raw: Dict[str, Any] = {}
    mesh_parts: List[Dict[str, Any]] = []
    headless = {"active": False, "label": "", "reason": "", "info": ""}

    def set_headless(label: str, reason: str = "", info: str = "") -> None:
        if not headless["active"]:
            headless["active"] = True
            headless["label"] = label
            headless["reason"] = reason
            headless["info"] = info

    # ---- Avatar-Daten -----------------------------------------------------
    if dry_run:
        username = api.normalize_username(username_raw) if username_raw else "Builderman"
        user_id = 0
        avatar_raw = {
            "_source": "dry-run",
            "playerAvatarType": "R15",
            "scales": dict(api.DEFAULT_SCALES),
            "bodyColors": dict(api.DEFAULT_BODY_COLORS),
            "assets": [],
        }
        log("[1/4] Dry-Run: keine Netzwerk-Anfragen, Koerper mit Standardfarben")
    else:
        username = api.normalize_username(username_raw)
        client = api.HttpClient(verbose=verbose)
        if not client.api_key:
            warnings.append(
                "Kein Secret ROBLOX_OPEN_CLOUD_API_KEY gesetzt - Asset-Downloads "
                "laufen nur ueber die oeffentlichen Fallback-Endpunkte und koennen "
                "fehlschlagen (401)."
            )
            log("  ! ROBLOX_OPEN_CLOUD_API_KEY ist nicht gesetzt (Fallback aktiv)")
        else:
            log("  i Open-Cloud-Key gefunden (x-api-key wird mitgeschickt)")

        log(f"[1/4] Roblox-User-ID fuer '{username}' ermitteln ...")
        user_id, username = api.resolve_user_id(client, username)
        log(f"      -> UserId {user_id} ({username})")

        log("[2/4] Avatar-Definition laden ...")
        avatar_raw = api.fetch_avatar(client, user_id)
        if avatar_raw.get("_degraded"):
            warnings.append(
                "Nur 'currently-wearing' verfuegbar - Farben/Scales sind Standardwerte."
            )

        # -- Helfer fuer Nachlade-Downloads (IDs aus RBXM-Modellen) ---------
        followup_cache: Dict[int, Optional[bytes]] = {}
        followup_state = {"attempts": 0, "warned": False}
        texture_files: Dict[int, Optional[str]] = {}

        def download_followup(asset_id: int) -> Optional[bytes]:
            """Laedt eine ID, die erst in einem RBXM stand (Cache + Limit)."""
            if asset_id in followup_cache:
                return followup_cache[asset_id]
            if followup_state["attempts"] >= MAX_FOLLOWUP_DOWNLOADS:
                if not followup_state["warned"]:
                    followup_state["warned"] = True
                    warnings.append(
                        f"RBXM-Nachlade-Limit ({MAX_FOLLOWUP_DOWNLOADS}) erreicht - "
                        "weitere Meshes/Texturen werden nicht geladen."
                    )
                followup_cache[asset_id] = None
                return None
            followup_state["attempts"] += 1
            result = api.download_asset(client, asset_id)
            followup_cache[asset_id] = result.data
            return result.data

        def register_texture(asset_id: int, data: bytes, type_name: str,
                             label: str, type_id: int) -> Optional[str]:
            fname = texture_filename(asset_id, data)
            with open(os.path.join(textures_dir, fname), "wb") as fh:
                fh.write(data)
            rel = f"textures/{fname}"
            textures.append({"asset_id": asset_id, "name": label,
                             "asset_type": type_name, "file": rel,
                             "type_id": type_id})
            texture_files[asset_id] = rel
            return rel

        def save_texture_by_id(asset_id: int, type_name: str, label: str,
                               type_id: int) -> Optional[str]:
            if asset_id in texture_files:
                return texture_files[asset_id]
            raw = download_followup(asset_id)
            rel = None
            if raw is not None and api.classify_asset(raw, 0) == "texture":
                rel = register_texture(asset_id, raw, type_name, label, type_id)
            texture_files[asset_id] = rel
            return rel

        def add_mesh(raw: bytes, parent_asset_id: int, mesh_asset_id: int,
                     type_id: int, type_name: str, label: str,
                     rbxm_size: Optional[Any], texture_file: Optional[str],
                     rbxm_position: Optional[Any] = None) -> None:
            """Parst ein FileMesh und reiht es in mesh_parts ein."""
            try:
                mesh = roblox_mesh.parse(raw)
            except roblox_mesh.MeshParseError as exc:
                skipped.append({"asset_id": mesh_asset_id, "name": label,
                                "asset_type": type_name,
                                "reason": f"Mesh nicht lesbar: {exc}"})
                return
            head_info = ""
            if type_id == DYNAMIC_HEAD_TYPE:
                is_headless, reason, head_info = head_geometry_info(
                    mesh, rbxm_size)
                if mesh.is_empty or is_headless:
                    # DynamicHead ohne sichtbare Geometrie = "Headless"
                    set_headless(label or type_name, reason, head_info)
                    return
            if mesh.is_empty:
                skipped.append({"asset_id": mesh_asset_id, "name": label,
                                "asset_type": type_name,
                                "reason": "Mesh ohne Faces"})
                return
            mesh_parts.append({
                "asset_id": parent_asset_id,
                "mesh_asset_id": mesh_asset_id,
                "type_id": type_id,
                "type_name": type_name,
                "label": label,
                "mesh": mesh,
                "rbxm_size": rbxm_size,
                "rbxm_position": rbxm_position,
                "rbxm_scale": None,
                "class": None,
                "texture_file": texture_file,
                "head_info": head_info,
            })

        def process_rbxm(data: bytes, parent_asset_id: int, type_id: int,
                         type_name: str, label: str, depth: int = 1) -> None:
            """RBXM-Modell auswerten: Mesh-/Textur-IDs nachladen."""
            try:
                entries = rbxm.extract_assets(data)
            except rbxm.RbxmError as exc:
                skipped.append({"asset_id": parent_asset_id, "name": label,
                                "asset_type": type_name,
                                "reason": f"RBXM nicht lesbar: {exc}"})
                return
            if not entries:
                if type_id == DYNAMIC_HEAD_TYPE:
                    set_headless(label or type_name)
                else:
                    skipped.append({"asset_id": parent_asset_id, "name": label,
                                    "asset_type": type_name,
                                    "reason": "RBXM enthielt keine Mesh-/Textur-IDs"})
                return

            parts_before, skips_before = len(mesh_parts), len(skipped)
            textures_before, headless_before = len(textures), headless["active"]
            for entry in entries:
                entry_label = entry.get("name") or label
                texture_file = None
                if entry.get("texture_id"):
                    texture_file = save_texture_by_id(
                        int(entry["texture_id"]), type_name, entry_label, type_id)
                mesh_id = entry.get("mesh_id")
                if not mesh_id:
                    continue
                mesh_id = int(mesh_id)
                raw = download_followup(mesh_id)
                if raw is None:
                    skipped.append({"asset_id": mesh_id, "name": entry_label,
                                    "asset_type": type_name,
                                    "reason": "Mesh-Download aus RBXM fehlgeschlagen "
                                              f"(Parent-Asset {parent_asset_id})"})
                    continue
                sub_kind = api.classify_asset(raw, 4)
                if sub_kind == "rbxm" and depth < 2:
                    process_rbxm(raw, mesh_id, type_id, type_name, entry_label, depth + 1)
                    continue
                if sub_kind == "texture":
                    # Die ID war doch eine Textur (kann beim Fallback-Scan
                    # in rbxm.py passieren) - als Textur ablegen.
                    if mesh_id not in texture_files:
                        register_texture(mesh_id, raw, type_name, entry_label, type_id)
                    continue
                if sub_kind != "mesh":
                    skipped.append({"asset_id": mesh_id, "name": entry_label,
                                    "asset_type": type_name,
                                    "reason": f"Unerwarteter Dateityp '{sub_kind}' "
                                              "fuer Mesh-ID aus RBXM"})
                    continue
                log(f"      + Mesh {mesh_id} aus RBXM ({entry.get('class', '?')}"
                    f"{', mit Textur' if texture_file else ''})")
                add_mesh(raw, parent_asset_id, mesh_id, type_id, type_name,
                         entry_label, entry.get("size"), texture_file,
                         entry.get("position"))
                mesh_parts[-1]["rbxm_scale"] = entry.get("mesh_scale")
                mesh_parts[-1]["class"] = entry.get("class")

            if (len(mesh_parts) == parts_before and len(skipped) == skips_before
                    and len(textures) == textures_before
                    and headless["active"] == headless_before):
                skipped.append({"asset_id": parent_asset_id, "name": label,
                                "asset_type": type_name,
                                "reason": "RBXM: keine verwertbare Geometrie gefunden"})

        # ---- Assets ---------------------------------------------------------
        assets = avatar_raw.get("assets") or []
        log(f"[3/4] {len(assets)} Assets verarbeiten ...")

        for index, asset in enumerate(assets[:max_assets]):
            try:
                aid = int(asset.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if not aid:
                continue
            label = asset.get("name") or ""
            declared = asset_type_id(asset, {})
            if declared in api.IGNORED_ASSET_TYPES:
                # Animationen/Audio: gar nicht herunterladen, nur zaehlen
                ignored.append({
                    "asset_id": aid, "name": label,
                    "asset_type": api.ASSET_TYPE_NAMES.get(declared, "Animation"),
                })
                continue

            details = api.fetch_asset_details(client, aid)
            type_id = asset_type_id(asset, details)
            type_name = api.ASSET_TYPE_NAMES.get(type_id, "Unbekannt")
            if not label:
                label = details.get("name") or type_name
            if type_id in api.IGNORED_ASSET_TYPES:
                ignored.append({"asset_id": aid, "name": label,
                                "asset_type": type_name})
                continue

            dl = api.download_asset(client, aid)
            if dl.data is None:
                skipped.append({
                    "asset_id": aid,
                    "name": label,
                    "asset_type": type_name,
                    "reason": "Download fehlgeschlagen",
                    "detail": dl.note[:400],
                })
                log(f"      - {aid} {type_name}: Download fehlgeschlagen ({dl.note[:120]})")
                continue

            # Manche Endpunkte liefern RBXM-Modelle gzip-gepackt aus -
            # erst entpacken, sonst erkennt classify_asset sie nicht.
            dl.data = api.maybe_decompress(dl.data)
            kind = api.classify_asset(dl.data, type_id)
            log(f"      - {aid} {type_name:<22} {kind:<8} {len(dl.data)/1024:8.1f} KB via {dl.source}")

            if kind == "mesh":
                add_mesh(dl.data, aid, aid, type_id, type_name, label, None, None)
            elif kind == "texture":
                fname = texture_filename(aid, dl.data)
                with open(os.path.join(textures_dir, fname), "wb") as fh:
                    fh.write(dl.data)
                textures.append({"asset_id": aid, "name": label, "asset_type": type_name,
                                 "file": f"textures/{fname}", "type_id": type_id})
            elif kind == "rbxm":
                # Accessoires und Koerperteile kommen als Modell - die
                # eigentlichen Mesh-/Textur-IDs stecken darin.
                process_rbxm(dl.data, aid, type_id, type_name, label)
            else:
                skipped.append({"asset_id": aid, "name": label, "asset_type": type_name,
                                "reason": f"Dateityp '{kind}' wird nicht unterstuetzt"})

    # ---- Koerper bauen ----------------------------------------------------
    log("[4/4] Rig bauen und OBJ-Dateien schreiben ...")
    scales = effective_scales(avatar_raw.get("scales"))
    body_colors = effective_body_colors(avatar_raw.get("bodyColors"))

    # Kleidungs-Texturen den Koerperteilen zuordnen (mit Prioritaet:
    # klassisches Shirt vor Layered-Varianten, Hose nur auf die Beine).
    texture_for_part: Dict[str, str] = {}
    for target, order in TEXTURE_PRIORITY.items():
        for wanted_type in order:
            match = next((tex for tex in textures
                          if int(tex["type_id"]) == wanted_type), None)
            if match is not None:
                texture_for_part[target] = match["file"]
                break

    # Echte Koerperteil-Meshes (Typ 27-31) den Rig-Teilen zuordnen.
    # R15-Bundles liefern pro Asset mehrere Segmente (z. B. LeftArm =
    # UpperArm + LowerArm + Hand, teils in mehreren Varianten). Die
    # R15-Segmente werden anhand ihrer Instanznamen ausgewaehlt und per
    # RBXM-Position zu einem Koerperteil zusammengefuehrt - statt wie
    # frueher nur das erste Segment (z. B. nur die Hand) zu verwenden.
    body_groups: Dict[str, list] = {}
    for mesh_part in mesh_parts:
        rig_name = BODY_PART_TYPES.get(int(mesh_part["type_id"]))
        if rig_name is not None:
            body_groups.setdefault(rig_name, []).append(mesh_part)

    body_replacements: Dict[str, Dict[str, Any]] = {}
    for rig_name, group in body_groups.items():
        body_replacements[rig_name] = _combine_segments(rig_name, group, warnings)

    # Teilmasse fuer den Rig. Sie kommen aus den RBXM-Bundles und sind dort
    # **bereits skalierte** Roblox-Part-Groessen (Studs) - sie duerfen nicht
    # noch einmal mit den Avatar-Scales multipliziert werden.
    part_sizes: Dict[str, Any] = {}
    for rig_name, replacement in body_replacements.items():
        if rig_name.endswith("arm") or rig_name.endswith("bein"):
            # Einzelnes, waagerecht modelliertes Glied (R6-Pakete):
            # aufrichten, damit es senkrecht haengt.
            if not replacement.get("stacked"):
                straightened, note = avatar_rig.upright_limb(
                    replacement["mesh"], rig_name)
                if note:
                    replacement["mesh"] = straightened
                    replacement["upright_note"] = note
                    replacement["part_size"] = (replacement["part_size"][1],
                                                replacement["part_size"][0],
                                                replacement["part_size"][2])
        size = _replacement_size(replacement, warnings, rig_name)
        if size is not None:
            part_sizes[rig_name] = size

    # Kopf: echtes Head-/DynamicHead-Mesh suchen (DynamicHead hat Vorrang).
    # Degenerierte DynamicHead-Meshes (< HEADLESS_MAX_FACES) sind bereits
    # in add_mesh() als Headless aussortiert und stehen nicht in mesh_parts.
    head_mesh: Optional[Dict[str, Any]] = None
    for wanted_type in (DYNAMIC_HEAD_TYPE, HEAD_ASSET_TYPE):
        for mesh_part in mesh_parts:
            if int(mesh_part["type_id"]) == wanted_type:
                head_mesh = mesh_part
                break
        if head_mesh is not None:
            break

    if head_mesh is not None and not headless["active"]:
        head_size = _replacement_size(head_mesh, warnings, "kopf",
                                      prefer_geometry=True)
        if head_size is not None:
            # Der Kopf ist rund: die groesste Achse bestimmt den Durchmesser.
            part_sizes["kopf"] = (max(head_size),) * 3

    body_parts, metrics = avatar_rig.build_body(body_colors, scales, part_sizes or None)
    warnings.extend(metrics.notes)

    body_parts_from_mesh = 0
    for part in body_parts:
        if part.name == "kopf":
            if headless["active"]:
                continue  # Headless-Avatar: gar kein Kopf
            if head_mesh is not None:
                size = head_mesh["mesh"].size()
                max_dim = max(size) or 1.0
                # Auf den Kopf-Durchmesser des Rigs skalieren (der kommt
                # aus dem echten Mesh, siehe part_sizes oben).
                target = metrics.head_radius * 2.0
                placed = head_mesh["mesh"].transform(target / max_dim)
                c = placed.center()
                placed = placed.transform(1.0, (-c[0], metrics.head_center_y - c[1], -c[2]))
                placed = sanity_check(placed, target, "Kopf", warnings)
                fname = "kopf_mesh.obj"
                placed.write_obj(os.path.join(parts_dir, fname))
                kopf_textur = _usable_head_texture(
                    head_mesh.get("texture_file"), out_dir, part.color)
                if head_mesh.get("texture_file") and not kopf_textur:
                    warnings.append(
                        "Kopf-Textur wirkt wie eine dunkle Zusatz-Map "
                        "(Roughness/Normal) - der Kopf nutzt die "
                        "Koerperfarbe des Avatars."
                    )
                # Grundfarbe bleibt die Hautfarbe: durchsichtige Stellen
                # der Gesichts-Textur (Stirn, Wangen) zeigen sie.
                parts.append({
                    "name": "kopf_mesh",
                    "obj": f"parts/{fname}",
                    "color": part.color,
                    "tint": _texture_color_hint(kopf_textur, part.color,
                                                out_dir, "kopf"),
                    "kind": "body",
                    "smooth": True,
                    "texture": (kopf_textur
                                or texture_for_part.get("kopf_mesh")),
                    "asset_id": head_mesh["asset_id"],
                    "note": ("Echtes Kopf-Mesh des Avatars"
                             + (f" ({head_mesh['head_info']})"
                                if head_mesh.get("head_info") else "")),
                })
                warnings.append("Kopf wurde durch das echte Head-Mesh des Avatars ersetzt.")
                continue

        replacement = body_replacements.get(part.name)
        if replacement is not None:
            lo, hi = part.mesh.bbox()
            placed = avatar_rig.fit_mesh_to_part(replacement["mesh"], lo, hi)
            placed = sanity_check(placed, max(hi[i] - lo[i] for i in range(3)),
                                  part.name, warnings)
            own_texture = replacement.get("texture_file")
            cloth_texture = texture_for_part.get(part.name)
            texture = own_texture or cloth_texture
            # Echte Meshes bringen eigene UVs mit - fuer den klassischen
            # Kleidungsatlas werden sie neu projiziert (sonst erscheint
            # der ganze Atlas gestaucht als Raster auf dem Koerper).
            # Eine mesh-eigene Textur passt dagegen zu den Mesh-UVs und
            # bleibt unangetastet.
            atlas_uv = (bool(cloth_texture) and not own_texture
                        and avatar_uv.has_clothing_layout(part.name))
            if atlas_uv:
                placed = avatar_uv.project_atlas_uvs(placed, part.name)
            fname = f"{part.name}.obj"
            placed.write_obj(os.path.join(parts_dir, fname))
            parts.append({
                "name": part.name,
                "obj": f"parts/{fname}",
                # Grundfarbe = Koerperfarbe (Haut unter durchsichtigen
                # Atlas-Stellen), Tint = echte Kleidungsfarbe der Panels.
                "color": part.color,
                "tint": _texture_color_hint(texture, part.color, out_dir,
                                            part.name),
                "kind": "body",
                "smooth": True,
                "texture": texture,
                "asset_id": replacement["asset_id"],
                "uv": "classic-atlas" if atlas_uv else "mesh",
                "note": (f"Echtes Mesh aus RBXM (Mesh-Asset "
                         f"{replacement['mesh_asset_id']})"
                         + replacement.get("note_suffix", "")
                         + (f"; {replacement['upright_note']}"
                            if replacement.get("upright_note") else "")),
            })
            body_parts_from_mesh += 1
            continue

        fname = f"{part.name}.obj"
        part.mesh.write_obj(os.path.join(parts_dir, fname))
        texture = texture_for_part.get(part.name)
        parts.append({
            "name": part.name,
            "obj": f"parts/{fname}",
            "color": part.color,
            "tint": _texture_color_hint(texture, part.color, out_dir,
                                        part.name),
            "kind": "body",
            "smooth": part.name in SMOOTH_BODY_PARTS,
            "texture": texture,
            "uv": ("classic-atlas" if texture and
                   avatar_uv.has_clothing_layout(part.name) else "box"),
        })

    # ---- Dekals: T-Shirt-Grafik und Gesicht ------------------------------
    # T-Shirt (2) und Face (18) sind kleine Bilder fuer eine einzelne
    # Flaeche, kein 585x559-Atlas. Sie liegen als duenne Flaeche vor der
    # Brust bzw. gewoelbt vor dem Gesicht.
    decals = _add_decals(parts_dir, textures, metrics, body_parts,
                         head_mesh is not None, headless["active"])
    parts.extend(decals)

    if headless["active"]:
        detail = ""
        if headless["reason"]:
            detail += f" Grund: {headless['reason']}."
        if headless["info"]:
            detail += f" Kennzahlen: {headless['info']}."
        warnings.append(
            "Headless/DynamicHead erkannt: Das Kopf-Asset "
            f"'{headless['label']}' enthaelt keine sichtbare Geometrie - "
            f"der Rig-Kopf wird nicht gerendert.{detail}"
        )

    # Accessoires platzieren
    consumed = {source for replacement in body_replacements.values()
                for source in replacement.get("source_ids", [])}
    if head_mesh is not None:
        consumed.add(id(head_mesh))
    used_filenames = set()
    accessory_assets = 0
    for mesh_part in mesh_parts:
        if id(mesh_part) in consumed:
            continue
        if int(mesh_part["type_id"]) in BODY_PART_TYPES:
            continue  # Duplikat (Warnung steht oben)
        if int(mesh_part["type_id"]) == HEAD_ASSET_TYPE:
            continue  # nur ein Kopf wird verwendet

        placed_variants = _place_accessory_variants(mesh_part, metrics, warnings)
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                       for ch in (mesh_part["label"] or mesh_part["type_name"]))[:28]
        file_id = mesh_part.get("mesh_asset_id") or mesh_part["asset_id"]
        # Accessoires faerben sich nach ihrer Textur (schwarze Haare bleiben
        # schwarz). Vorher waren alle hart auf hellblau gesetzt - daher
        # "der Rest hat gar keine passende Farbe".
        farbe = _texture_color_hint(mesh_part.get("texture_file"), "#c9d3dc",
                                   out_dir)
        for index, (placed, note) in enumerate(placed_variants):
            fname = f"accessoire_{file_id}_{safe or 'teil'}.obj"
            if index:
                fname = (f"accessoire_{file_id}_{safe or 'teil'}"
                         f"_{index + 1}.obj")
            counter = 2
            while fname in used_filenames:
                fname = (f"accessoire_{file_id}_{safe or 'teil'}"
                         f"_{index + counter + 1}.obj")
                counter += 1
            used_filenames.add(fname)
            placed.write_obj(os.path.join(parts_dir, fname))
            parts.append({
                "name": (f"{safe or mesh_part['type_name']} "
                         f"({mesh_part['asset_id']})"),
                "obj": f"parts/{fname}",
                "color": farbe,
                "kind": "accessory",
                "smooth": True,
                "asset_id": mesh_part["asset_id"],
                "asset_type": mesh_part["type_name"],
                "note": note,
                "texture": mesh_part.get("texture_file"),
            })
            accessory_assets += 1

    # ---- Manifest ---------------------------------------------------------
    bounds_lo = [1e9, 1e9, 1e9]
    bounds_hi = [-1e9, -1e9, -1e9]
    for part in parts:
        path = os.path.join(out_dir, part["obj"])
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("v "):
                        _, x, y, z = line.split()
                        for i, val in enumerate((float(x), float(y), float(z))):
                            bounds_lo[i] = min(bounds_lo[i], val)
                            bounds_hi[i] = max(bounds_hi[i], val)
        except OSError:
            continue
    if bounds_lo[0] > bounds_hi[0]:
        bounds_lo, bounds_hi = [-1, 0, -1], [1, 5, 1]

    manifest = {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "username": username,
        "user_id": user_id,
        "avatar_type": avatar_raw.get("playerAvatarType") or "R15",
        "avatar_source": avatar_raw.get("_source", ""),
        "dry_run": dry_run,
        "scales": scales,
        "body_colors": body_colors,
        "metrics": {
            "height": round(metrics.height, 3),
            "height_de": fmt_de(metrics.height),
            "torso_width": round(metrics.torso_width, 3),
            "head_radius": round(metrics.head_radius, 3),
        },
        "bbox": {"min": [round(v, 3) for v in bounds_lo],
                 "max": [round(v, 3) for v in bounds_hi]},
        "parts": parts,
        "textures": textures,
        "skipped": skipped,
        "ignored": ignored,
        "warnings": warnings,
        "stats": {
            "parts": len(parts),
            "body_parts": len(body_parts),
            "body_parts_from_mesh": body_parts_from_mesh,
            "accessories": accessory_assets,
            "textures": len(textures),
            "skipped": len(skipped),
            # Alles in "skipped" ist ein echter Fehlschlag (Download/Parse);
            # Animationen stehen getrennt unter ignored_animations.
            "failed": len(skipped),
            "ignored_animations": len(ignored),
            "assets_seen": len(avatar_raw.get("assets") or []),
        },
    }
    write_json(os.path.join(out_dir, "manifest.json"), manifest)
    write_json(os.path.join(out_dir, "avatar.json"), avatar_raw)
    write_summary(os.path.join(out_dir, "summary.md"), manifest)

    log("")
    log(f"Fertig: {manifest['stats']['parts']} Teile "
        f"({accessory_assets} Accessoires, {body_parts_from_mesh} Koerperteile "
        f"aus echten Meshes, {len(textures)} Texturen), "
        f"{len(skipped)} Assets fehlgeschlagen, "
        f"{len(ignored)} Animationen ignoriert")
    log(f"Höhe: {fmt_de(manifest['metrics']['height'])} Studs (typisch 5–6), "
        f"Bounding-Box: {manifest['bbox']['min']} .. {manifest['bbox']['max']}")
    if warnings:
        for w in warnings:
            log(f"  ! {w}")
    return manifest


def write_summary(path: str, manifest: Dict[str, Any]) -> None:
    s = manifest["stats"]
    height_de = manifest["metrics"].get("height_de") or fmt_de(manifest["metrics"]["height"])
    lines = [
        f"# Avatar-Render: `{manifest['username']}`",
        "",
        f"* UserId: **{manifest['user_id']}**",
        f"* Avatar-Typ: **{manifest['avatar_type']}**",
        f"* Quelle: `{manifest['avatar_source']}`",
        f"* Teile: **{s['parts']}** (Accessoires: {s['accessories']}, "
        f"Körperteile aus echten Meshes: {s.get('body_parts_from_mesh', 0)}, "
        f"Texturen: {s['textures']}, fehlgeschlagen: {s.get('failed', s['skipped'])}, "
        f"Animationen ignoriert: {s.get('ignored_animations', 0)})",
        f"* Höhe: {height_de} Studs (typisch 5–6)",
        "",
        "## Teile",
        "",
        "| Teil | Typ | Farbe | Notiz |",
        "| --- | --- | --- | --- |",
    ]
    for part in manifest["parts"]:
        note = (part.get("note") or "").replace("|", "/")
        lines.append(f"| {part['name']} | {part['kind']} | `{part['color']}` | {note} |")
    if manifest["skipped"]:
        lines += ["", "## Fehlgeschlagen / übersprungen", "",
                  "| Asset | Typ | Grund |", "| --- | --- | --- |"]
        for item in manifest["skipped"]:
            lines.append(f"| {item.get('asset_id')} | {item.get('asset_type', '')} | "
                         f"{item.get('reason', '')} |")
    if manifest.get("ignored"):
        names = ", ".join(
            f"{item.get('name') or item.get('asset_id')} ({item.get('asset_type', '')})"
            for item in manifest["ignored"]
        )
        lines += ["", "## Ignoriert (Animationen/Audio, werden nicht gerendert)", "", names]
    if manifest["warnings"]:
        lines += ["", "## Hinweise", ""]
        lines += [f"* {w}" for w in manifest["warnings"]]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def read_username_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Roblox-Avatar -> Blender-Szene")
    parser.add_argument("--username", default=None,
                        help="Roblox-Username (sonst username.txt)")
    parser.add_argument("--username-file", default="username.txt")
    parser.add_argument("--out", default="build", help="Ausgabeordner (Default: build)")
    parser.add_argument("--dry-run", action="store_true",
                        help="ohne Netzwerk: nur Koerper mit Standardfarben bauen")
    parser.add_argument("--max-assets", type=int, default=80)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    username = args.username or read_username_file(args.username_file)
    if not username and not args.dry_run:
        print("FEHLER: kein Username uebergeben und username.txt ist leer.", file=sys.stderr)
        return 2

    try:
        build(username, args.out, dry_run=args.dry_run, max_assets=args.max_assets,
              verbose=not args.quiet)
    except api.RobloxError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
