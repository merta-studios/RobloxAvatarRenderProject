"""UV-Zuordnung fuer klassische Roblox-Kleidung (Shirt/Pants, 585x559 px).

Warum das noetig ist
--------------------
Roblox-Shirts und -Hosen sind **keine** freien Bilder, sondern ein festes
UV-Atlas mit 585x559 Pixeln. Jede Koerperflaeche hat dort ihr eigenes
Rechteck ("Panel"). Wer eine Kleidungs-Textur einfach mit UV 0..1 auf einen
Quader legt (so war es vorher), bekommt genau das, was im Render zu sehen
war: ein wildes Raster aus allen Panels gleichzeitig, verzerrt und ohne
Bezug zur Kleidung - und der Rest der Figur bleibt farblos.

Dieses Modul kennt die Panels des offiziellen Templates (Pixel-genau) und
vergibt die UVs pro Flaeche:

* Torso:   vorn / hinten / rechts / links / oben / unten
* Arm:     vorn / hinten / aussen / innen / oben / unten
* Bein:    identisches Layout wie der jeweilige Arm (Pants-Template)

Die Panels liegen im Bild mit dem Ursprung **oben links**; OBJ/Blender
erwarten den Ursprung unten links, daher wird V gespiegelt.

Referenz: Roblox "Classic clothing" (585x559) - Reihenfolge eines
Gliedmassen-Blocks von links nach rechts: aussen, hinten, innen, vorn
(Deckflaechen ueber/unter dem vorderen Panel).
"""

from __future__ import annotations

from typing import Any, Dict, Sequence, Tuple

from roblox_mesh import MeshData

# Groesse des offiziellen Templates in Pixeln.
ATLAS_WIDTH = 585
ATLAS_HEIGHT = 559

Rect = Tuple[float, float, float, float]  # (x0, y0, x1, y1) in Pixeln, y von oben

# --- Torso (Shirt und Pants teilen sich diese Panels) ----------------------
TORSO: Dict[str, Rect] = {
    "front": (231, 74, 359, 202),
    "back": (427, 74, 555, 202),
    "right": (165, 74, 229, 202),    # +X = rechte Seite des Charakters
    "left": (361, 74, 425, 202),     # -X = linke Seite des Charakters
    "top": (231, 8, 359, 72),
    "bottom": (231, 204, 359, 268),
}

# --- Rechter Arm / rechtes Bein (Block unten links im Template) -----------
LIMB_RIGHT: Dict[str, Rect] = {
    "outer": (19, 355, 82, 483),     # +X (Aussen-/Innenseite je Seite)
    "back": (85, 355, 148, 483),
    "inner": (151, 355, 214, 483),
    "front": (217, 355, 280, 483),
    "top": (217, 289, 280, 352),
    "bottom": (217, 485, 280, 549),
}

# --- Linker Arm / linkes Bein (Block unten rechts im Template) ------------
LIMB_LEFT: Dict[str, Rect] = {
    "front": (308, 355, 371, 483),
    "inner": (374, 355, 437, 483),   # +X zeigt zum Torso
    "back": (440, 355, 503, 483),
    "outer": (506, 355, 569, 483),   # -X
    "top": (308, 289, 371, 352),
    "bottom": (308, 485, 371, 549),
}

# Rig-Teilname -> Panelsatz. Der Torso hat eigene Panels, Arme und Beine
# benutzen die Gliedmassen-Bloecke (Pants liegt auf denselben Pixeln).
PART_REGIONS: Dict[str, Dict[str, Rect]] = {
    "torso": TORSO,
    "rechts_arm": LIMB_RIGHT,
    "links_arm": LIMB_LEFT,
    "rechts_bein": LIMB_RIGHT,
    "links_bein": LIMB_LEFT,
}

# Hals/Kopf tragen keine klassische Kleidung.
NO_CLOTHING_PARTS = {"kopf", "kopf_mesh", "hals"}


def has_clothing_layout(part_name: str) -> bool:
    return part_name in PART_REGIONS


def side_region(part_name: str, axis: str) -> str:
    """Panelname fuer eine Seitenflaeche.

    ``axis`` ist ``"+x"``, ``"-x"``, ``"+y"``, ``"-y"``, ``"+z"`` oder
    ``"-z"`` (Roblox: -Z ist vorne).
    """
    if axis == "+z":          # Ruecken
        return "back"
    if axis == "-z":          # Brust
        return "front"
    if axis == "+y":
        return "top"
    if axis == "-y":
        return "bottom"
    # Seitenflaechen: beim Torso heissen sie right/left, bei Gliedmassen
    # outer/inner (aussen = weg vom Koerper).
    if part_name == "torso":
        return "right" if axis == "+x" else "left"
    if part_name == "rechts_arm" or part_name == "rechts_bein":
        # Rechte Seite liegt bei +X; +X ist dort die Aussenseite.
        return "outer" if axis == "+x" else "inner"
    return "inner" if axis == "+x" else "outer"


def region_uv(region: Rect, inset: float = 1.0) -> Tuple[float, float, float, float]:
    """Panel-Rechteck -> UV-Rechteck in OBJ-Konvention (V von unten).

    ``inset`` schneidet ein paar Pixel Rand ab: an den Panel-Kanten liegt
    im Template oft die Nachbarflaeche, ohne Rand "blutet" sie ins Bild.
    """
    x0, y0, x1, y1 = region
    x0, y0, x1, y1 = x0 + inset, y0 + inset, x1 - inset, y1 - inset
    return (x0 / ATLAS_WIDTH, 1.0 - y1 / ATLAS_HEIGHT,
            x1 / ATLAS_WIDTH, 1.0 - y0 / ATLAS_HEIGHT)


def _face_frame(normal: Sequence[float]) -> Tuple[Tuple[float, float, float],
                                                  Tuple[float, float, float]]:
    """Bildrichtungen einer Flaeche: (rechts, oben) aus Sicht von aussen."""
    nx, ny, nz = (float(v) for v in normal)
    if abs(ny) >= max(abs(nx), abs(nz)):
        # Deck-/Bodenflaeche: rechts = +X, oben zeigt nach hinten (bzw.
        # beim Boden nach vorne), damit die Panels an das Frontpanel
        # anschliessen.
        return (1.0, 0.0, 0.0), (0.0, 0.0, 1.0 if ny > 0 else -1.0)
    # Seitenflaeche: oben = +Y, rechts = Blickrichtung x oben mit
    # Blickrichtung = -Normale.
    fx, fz = -nx, -nz
    right = (-fz, 0.0, fx)
    length = (right[0] ** 2 + right[2] ** 2) ** 0.5 or 1.0
    return (right[0] / length, 0.0, right[2] / length), (0.0, 1.0, 0.0)


def _axis_name(normal: Sequence[float]) -> str:
    nx, ny, nz = (float(v) for v in normal)
    axis = max(range(3), key=lambda i: abs((nx, ny, nz)[i]))
    sign = "+" if (nx, ny, nz)[axis] >= 0 else "-"
    return sign + "xyz"[axis]


def apply_box_uv(mesh: MeshData, part_name: str, inset: float = 1.0) -> MeshData:
    """Vergibt klassische Kleidungs-UVs auf einem Quader (je Flaeche 4 Vertices).

    Erwartet die Geometrie von :func:`roblox_mesh.make_box` (sechs Flaechen
    mit je vier eigenen Vertices und Face-Normalen). Andere Meshes werden
    unveraendert zurueckgegeben, damit die Funktion gefahrlos ueberall
    aufgerufen werden kann.
    """
    regions = PART_REGIONS.get(part_name)
    if regions is None or len(mesh.positions) % 4 != 0 or not mesh.positions:
        return mesh
    uvs = list(mesh.uvs) if len(mesh.uvs) == len(mesh.positions) else \
        [(0.0, 0.0)] * len(mesh.positions)
    if len(mesh.normals) != len(mesh.positions):
        return mesh

    for base in range(0, len(mesh.positions), 4):
        quad = mesh.positions[base:base + 4]
        normal = mesh.normals[base]
        name = side_region(part_name, _axis_name(normal))
        region = regions.get(name)
        if region is None:
            continue
        right, up = _face_frame(normal)
        cx = sum(p[0] for p in quad) / 4.0
        cy = sum(p[1] for p in quad) / 4.0
        cz = sum(p[2] for p in quad) / 4.0
        # Ausdehnung der Flaeche in Bildrichtung bestimmen
        half_r = max(abs((p[0] - cx) * right[0] + (p[1] - cy) * right[1]
                         + (p[2] - cz) * right[2]) for p in quad) or 1.0
        half_u = max(abs((p[0] - cx) * up[0] + (p[1] - cy) * up[1]
                         + (p[2] - cz) * up[2]) for p in quad) or 1.0
        u0, v0, u1, v1 = region_uv(region, inset)
        for offset, point in enumerate(quad):
            s = ((point[0] - cx) * right[0] + (point[1] - cy) * right[1]
                 + (point[2] - cz) * right[2]) / half_r      # -1 .. +1
            t = ((point[0] - cx) * up[0] + (point[1] - cy) * up[1]
                 + (point[2] - cz) * up[2]) / half_u         # -1 .. +1
            u = u0 + (s + 1.0) * 0.5 * (u1 - u0)
            v = v0 + (t + 1.0) * 0.5 * (v1 - v0)
            uvs[base + offset] = (u, v)
    return MeshData(
        positions=list(mesh.positions),
        normals=list(mesh.normals),
        uvs=uvs,
        faces=list(mesh.faces),
        colors=list(mesh.colors) if mesh.colors else None,
        source_version=mesh.source_version,
    )


def project_atlas_uvs(mesh: MeshData, part_name: str, inset: float = 1.0,
                      smooth_passes: int = 1) -> MeshData:
    """Zylindrische Projektion des Kleidungs-Atlasses auf ein echtes Mesh.

    Echte R15-Koerperteile bringen ihre eigenen (Mesh-)UVs mit - die
    passen nicht zu einem 585x559-Kleidungsatlas. Wird so ein Mesh nur in
    die Part-Groesse gestreckt, erscheint der komplette Atlas gestaucht
    als Raster auf dem Koerper (genau das war im Render zu sehen).

    Hier wird stattdessen jede Flaeche anhand ihrer Normalen dem richtigen
    Panel zugeordnet und die Vertex-Position auf die Panel-Flaeche
    projiziert (gleiches Prinzip wie eine zylindrische Kleidungs-Huelle).
    Die UVs sind damit unabhaengig von der Mesh-Aufloesung korrekt.
    """
    regions = PART_REGIONS.get(part_name)
    if regions is None or not mesh.positions or not mesh.faces:
        return mesh
    positions = mesh.positions
    normals = mesh.normals if len(mesh.normals) == len(positions) else None
    uvs = list(mesh.uvs) if len(mesh.uvs) == len(positions) else \
        [(0.0, 0.0)] * len(positions)

    count = len(positions)
    faces = [f for f in mesh.faces if max(f) < count]
    if not faces:
        return mesh

    # Pro Flaeche (6 Achsen) die Projektion einmal berechnen - sonst
    # waere das fuer grosse Meshes quadratisch (Faces x Vertices).
    frame_cache: Dict[str, Dict[str, Any]] = {}
    for _ in range(max(1, smooth_passes)):
        assignments = [None] * count
        for face in faces:
            if normals is not None:
                normal = [sum(normals[i][k] for i in face) for k in range(3)]
            else:
                p0, p1, p2 = (positions[i] for i in face)
                normal = [
                    (p1[1] - p0[1]) * (p2[2] - p0[2]) - (p1[2] - p0[2]) * (p2[1] - p0[1]),
                    (p1[2] - p0[2]) * (p2[0] - p0[0]) - (p1[0] - p0[0]) * (p2[2] - p0[2]),
                    (p1[0] - p0[0]) * (p2[1] - p0[1]) - (p1[1] - p0[1]) * (p2[0] - p0[0]),
                ]
            if not any(abs(v) > 1e-12 for v in normal):
                continue
            axis = _axis_name(normal)
            frame = frame_cache.get(axis)
            if frame is None:
                right, up = _face_frame(normal)
                ru = [p[0] * right[0] + p[1] * right[1] + p[2] * right[2]
                      for p in positions]
                rv = [p[0] * up[0] + p[1] * up[1] + p[2] * up[2]
                      for p in positions]
                u_min, u_max = min(ru), max(ru)
                v_min, v_max = min(rv), max(rv)
                span_u = (u_max - u_min) or 1.0
                span_v = (v_max - v_min) or 1.0
                u0, v0, u1, v1 = region_uv(
                    regions[side_region(part_name, axis)], inset)
                frame = {
                    "uv": [((ru[i] - u_min) / span_u, (rv[i] - v_min) / span_v)
                           for i in range(count)],
                    "rect": (u0, v0, u1 - u0, v1 - v0),
                }
                frame_cache[axis] = frame
            u0, v0, du, dv = frame["rect"]
            local = frame["uv"]
            for index in face:
                su, sv = local[index]
                assignments[index] = (u0 + su * du, v0 + sv * dv)
        for index, value in enumerate(assignments):
            if value is not None:
                uvs[index] = value
    return MeshData(
        positions=list(positions),
        normals=list(mesh.normals),
        uvs=uvs,
        faces=list(mesh.faces),
        colors=list(mesh.colors) if mesh.colors else None,
        source_version=mesh.source_version,
    )


def region_for_face(part_name: str, normal: Sequence[float]) -> Rect:
    """Panel-Rechteck fuer eine Flaeche (fuer Diagnose/Summary)."""
    regions = PART_REGIONS.get(part_name) or TORSO
    return regions[side_region(part_name, _axis_name(normal))]
