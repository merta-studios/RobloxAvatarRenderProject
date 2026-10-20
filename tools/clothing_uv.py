"""Klassische Roblox-Kleidung (Shirt/Pants) korrekt auf ein Mesh legen.

Das Roblox-Template ist **keine** normale Textur, sondern eine UV-Karte:
Jede Flaeche jedes Koerperteils hat ein eigenes Rechteck im 585x559-Bild.
Bisher wurde das komplette Bild auf jede Flaeche gelegt - dabei landet
z. B. das Hemden-Muster des Oberkoerpers auch auf den Armen und auf den
Fuessen, und weil das Bild zum Grossteil transparent/schwarz ist, kam
am Ende ein dunkelgrauer Farbbrei heraus ("Textur falsch, kaum sichtbar,
immer dunkelgrau").

Dieses Modul schneidet deshalb pro Flaeche genau das passende Rechteck
aus dem Template und normiert die Flaeche darauf:

    Flaeche (Normale)   Template-Rechteck
    front  (-Z)         "Front"-Feld
    back   (+Z)         "Back"-Feld
    left   (-X)         "Left"-Feld
    right  (+X)         "Right"-Feld
    top    (+Y)         "Top"-Feld
    bottom (-Y)         "Bottom"-Feld

Koordinaten: Roblox ist Y-up und schaut nach -Z, die Charakter-Rechte
Seite ist +X (deshalb liegt im Template das "Right"-Feld links vom
"Front"-Feld - man sieht den Avatar ja von vorne).

R15: Der Koerper besteht aus mehreren Segmenten (UpperTorso/LowerTorso,
*UpperArm/*LowerArm/*Hand, *UpperLeg/*LowerLeg/*Foot). Roblox legt jedes
Segment auf einen festen Anteil des Feldes (siehe SEGMENT_SLICES):
Oberkoerper 96 px, Unterkoerper 32 px; Oberarm 64 px, Unterarm 48 px,
Hand 16 px. `assign_template_uvs(..., slice_name="LeftUpperArm")` bildet
das nach - wichtig fuer die Feinheiten (Aermel-/Hosennaehte sitzen sonst
ein paar Studs zu hoch oder zu tief).
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Sequence, Tuple

from roblox_mesh import MeshData

# ---------------------------------------------------------------------------
# Template-Geometrie (Pixel, y von oben gezaehlt)
# ---------------------------------------------------------------------------

TEMPLATE_W = 585
TEMPLATE_H = 559

Rect = Tuple[float, float, float, float]  # x, y, breite, hoehe

# Torso-Block: 128x128 vorne/hinten, 64x128 seitlich, 128x64 oben/unten.
TORSO_PANELS: Dict[str, Rect] = {
    "front": (231, 74, 128, 128),
    "back": (427, 74, 128, 128),
    "left": (361, 74, 64, 128),
    "right": (165, 74, 64, 128),
    "top": (231, 8, 128, 64),
    "bottom": (231, 204, 128, 64),
}

# Zwei Gliedmassen-Bloecke: links im Bild = rechte Koerperseite (+X).
LIMB_PANELS_RIGHT: Dict[str, Rect] = {
    "front": (217, 355, 64, 128),
    "back": (85, 355, 64, 128),
    "left": (19, 355, 64, 128),
    "right": (151, 355, 64, 128),
    "top": (217, 289, 64, 64),
    "bottom": (217, 485, 64, 64),
}
LIMB_PANELS_LEFT: Dict[str, Rect] = {
    "front": (308, 355, 64, 128),
    "back": (440, 355, 64, 128),
    "left": (374, 355, 64, 128),
    "right": (506, 355, 64, 128),
    "top": (308, 289, 64, 64),
    "bottom": (308, 485, 64, 64),
}

# Hosen: der Torso-Anteil ist nur der untere Streifen des Torso-Blocks
# (R15: LowerTorso = untere 32 von 128 Pixeln).
PANTS_TORSO_HEIGHT = 32

STYLE_SHIRT = "shirt"
STYLE_PANTS = "pants"

# R15-Segment -> vertikaler Anteil des jeweiligen Template-Feldes
# (0.0 = Oberkante des Feldes, 1.0 = Unterkante).
SEGMENT_SLICES: Dict[str, Tuple[float, float]] = {
    "UpperTorso": (0.0, 0.75),
    "LowerTorso": (0.75, 1.0),
    "UpperArm": (0.0, 0.5),
    "LowerArm": (0.5, 0.875),
    "Hand": (0.875, 1.0),
    "UpperLeg": (0.0, 0.5),
    "LowerLeg": (0.5, 0.875),
    "Foot": (0.875, 1.0),
}

# Welche Achse zeigt bei welcher Flaeche nach rechts (u) bzw. oben (v,
# Roblox +Y = Bild oben). Werte sind Einheitsvektoren in Roblox-Koordinaten.
_FACE_AXES: Dict[str, Tuple[Tuple[float, float, float], Tuple[float, float, float]]] = {
    "front": ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),   # Blick von -Z
    "back": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),     # Blick von +Z
    "right": ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),   # Blick von +X
    "left": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),     # Blick von -X
    "top": ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),     # Blick von +Y
    "bottom": ((-1.0, 0.0, 0.0), (0.0, 0.0, -1.0)),  # Blick von -Y
}


def _face_of_normal(normal: Sequence[float]) -> str:
    """Welche Wuerfelflaeche gehoert zu dieser Normale?"""
    ax = max(range(3), key=lambda i: abs(normal[i]))
    if ax == 1:
        return "top" if normal[1] >= 0 else "bottom"
    if ax == 2:
        return "front" if normal[2] <= 0 else "back"  # Roblox "vorne" = -Z
    return "right" if normal[0] >= 0 else "left"


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _panel_rects(style: str, limb: str) -> Dict[str, Rect]:
    """Rechtecke des Templates, das dieser Koerperteil benutzt."""
    if style == STYLE_PANTS:
        if limb in ("right", "left"):
            return LIMB_PANELS_RIGHT if limb == "right" else LIMB_PANELS_LEFT
        # Torso: nur der untere Streifen (Huefte)
        out = {}
        for key, (x, y, w, h) in TORSO_PANELS.items():
            out[key] = (x, y + h - PANTS_TORSO_HEIGHT, w, min(PANTS_TORSO_HEIGHT, h))
        return out
    if limb in ("right", "left"):
        return LIMB_PANELS_RIGHT if limb == "right" else LIMB_PANELS_LEFT
    return TORSO_PANELS


def segment_slice(slice_name: Optional[str]) -> Optional[Tuple[float, float]]:
    """Vertikaler Anteil eines R15-Segments (z. B. "LeftUpperArm")."""
    if not slice_name:
        return None
    name = str(slice_name)
    for suffix, bounds in SEGMENT_SLICES.items():
        if name.endswith(suffix):
            return bounds
    return None


def assign_template_uvs(
    mesh: MeshData,
    style: str,
    limb: str = "torso",
    slice_name: Optional[str] = None,
    region: Optional[Tuple[float, float]] = None,
    front_only: bool = False,
) -> MeshData:
    """Schreibt Template-UVs in ein Mesh (gleiche Vertices, neue UVs).

    style       STYLE_SHIRT / STYLE_PANTS
    limb        "torso" | "right" | "left" (Gliedmassen-Block im Template)
    slice_name  R15-Segmentname; schraenkt den vertikalen Anteil ein
    region      expliziter vertikaler Anteil (0..1) statt `slice_name`
    front_only  nur Front-Flaechen bekommen ein UV, alle anderen (0, 0)

    Rueckgabe: neues MeshData (Original bleibt unveraendert).
    """
    if not mesh.positions:
        return mesh
    lo, hi = mesh.bbox()
    size = [hi[i] - lo[i] for i in range(3)]
    rects = _panel_rects(style, limb)
    bounds = region if region is not None else segment_slice(slice_name)
    v_start, v_end = bounds if bounds else (0.0, 1.0)

    normals = list(mesh.normals)
    if len(normals) < len(mesh.positions):
        normals.extend(_vertex_normals(mesh, len(mesh.positions) - len(normals)))

    uvs = []
    for index, position in enumerate(mesh.positions):
        face = _face_of_normal(normals[index])
        if front_only and face != "front":
            uvs.append((0.0, 0.0))
            continue
        rect = rects.get(face) or rects["front"]
        x, y, w, h = rect
        u_axis, v_axis = _FACE_AXES[face]
        proj_u = [_dot((p[0] - lo[0], p[1] - lo[1], p[2] - lo[2]), u_axis)
                  for p in (lo, hi)]
        proj_v = [_dot((p[0] - lo[0], p[1] - lo[1], p[2] - lo[2]), v_axis)
                  for p in (lo, hi)]
        u_lo, u_hi = min(proj_u), max(proj_u)
        v_lo, v_hi = min(proj_v), max(proj_v)
        u_here = _dot((position[0] - lo[0], position[1] - lo[1],
                       position[2] - lo[2]), u_axis)
        v_here = _dot((position[0] - lo[0], position[1] - lo[1],
                       position[2] - lo[2]), v_axis)
        u_norm = (u_here - u_lo) / (u_hi - u_lo) if u_hi - u_lo > 1e-9 else 0.5
        v_norm = (v_here - v_lo) / (v_hi - v_lo) if v_hi - v_lo > 1e-9 else 0.5
        v_norm = min(1.0, max(0.0, v_norm))
        u_norm = min(1.0, max(0.0, u_norm))

        # Anteil des Segments am Feld (R15-Naehte)
        v_frac = v_start + (1.0 - v_norm) * (v_end - v_start)  # 0 = Feld oben
        u = (x + u_norm * w) / TEMPLATE_W
        # OBJ/Blender: v=0 ist die UNTERKANTE des Bildes (Robot-Flip in
        # roblox_mesh.flip_v() ist bereits beruecksichtigt, siehe Modul-Doku).
        v = 1.0 - (y + v_frac * h) / TEMPLATE_H
        uvs.append((round(u, 6), round(v, 6)))

    return MeshData(
        positions=list(mesh.positions),
        normals=list(mesh.normals),
        uvs=uvs,
        faces=list(mesh.faces),
        colors=list(mesh.colors) if mesh.colors else None,
        source_version=mesh.source_version,
    )


def _vertex_normals(mesh: MeshData, count: int) -> Iterable[Tuple[float, float, float]]:
    """Ersatz-Normalen aus den Faces (falls die Datei keine mitbringt)."""
    acc = [[0.0, 0.0, 0.0] for _ in mesh.positions]
    for a, b, c in mesh.faces:
        try:
            pa, pb, pc = mesh.positions[a], mesh.positions[b], mesh.positions[c]
        except IndexError:
            continue
        n = _cross((pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]),
                   (pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]))
        for idx in (a, b, c):
            for i in range(3):
                acc[idx][i] += n[i]
    out = []
    for vec in acc[:count]:
        length = (vec[0] ** 2 + vec[1] ** 2 + vec[2] ** 2) ** 0.5
        if length < 1e-12:
            out.append((0.0, 0.0, -1.0))
        else:
            out.append((vec[0] / length, vec[1] / length, vec[2] / length))
    return out


def _cross(a: Sequence[float], b: Sequence[float]) -> Tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def part_style(asset_type_id: int) -> Optional[str]:
    """Welcher Template-Stil gehoert zu diesem Kleidungs-Asset-Typ?

    Shirt/TShirt-Accessoires decken Oberkoerper + Arme ab, Pants/Hosen
    die Beine (und die Huefte).
    """
    tops = {2, 11, 64, 65, 67, 68}
    bottoms = {12, 66, 69, 70, 71}
    if asset_type_id in tops:
        return STYLE_SHIRT
    if asset_type_id in bottoms:
        return STYLE_PANTS
    return None
