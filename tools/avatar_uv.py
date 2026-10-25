"""Humanoid-Textur-Kompositor und R15-UV-Helfer fuer Roblox-Avatare.

Klassische Roblox-Kleidung (`Shirt`, `Pants`) liefert ein `585x559`-Template-Bild
mit sechs aufgeklappten Quader-Flaechen pro Koerperteil und Beschriftungs- bzw.
Wasserzeichen-Flächen im Hintergrund ("Front", "Back", "L", "R", Designer-Logos).
Wird dieses Rohbild direkt auf R15-Meshes oder Quader gelegt, erscheinen die
Template-Hilfsbuchstaben ("L", "X") auf Armen, Beinen und Torso.

Dieses Modul bildet den offiziellen Roblox-R15-Humanoid-Kompositor nach:
  * Torso-Textur (`388x272` RGBA): `Pants` -> `Shirt` -> `TShirt` (`Front`)
  * Gliedmassen-Textur (`264x284` RGBA): `Shirt` auf Arme, `Pants` auf Beine
  * Passende R15-UVs fuer prozedurale Quader-Fallbacks (`apply_torso_box_uvs`,
    `apply_limb_box_uvs`), sodass auch Blocky-Fallbacks ohne `.mesh`-Asset
    exakt dieselben 6 Seiten zeigen wie echte R15-Bundle-Meshes.
"""

from __future__ import annotations

import io
from typing import Any, List, Optional, Sequence, Tuple

try:
    import numpy as np
    from PIL import Image
except ImportError:  # pragma: no cover - optionale Bildbibliotheken
    np = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]

from roblox_mesh import MeshData, _face_normal

ColorRGB = Tuple[float, float, float]

TORSO_MAP_SIZE = (388, 272)
LIMB_MAP_SIZE = (264, 284)
TEMPLATE_SIZE = (585, 559)

# Quell-Rechtecke (x0, y0, x1, y1) im offiziellen 585x559-Template -> Ziel (x, y) in 388x272
_TORSO_PANELS = (
    ((231, 8, 359, 72), (2, 10)),     # Top    (128x64,  y=10..74)
    ((231, 74, 359, 202), (2, 74)),   # Front  (128x128, x=2..130,   y=74..202)
    ((361, 74, 425, 202), (130, 74)), # Left   (64x128,  x=130..194, y=74..202)
    ((427, 74, 555, 202), (194, 74)), # Back   (128x128, x=194..322, y=74..202)
    ((165, 74, 229, 202), (322, 74)), # Right  (64x128,  x=322..386, y=74..202)
    ((231, 204, 359, 268), (2, 202)), # Bottom (128x64,  x=2..130,   y=202..266)
)

# Spalten im 264x284-Gliedmassen-Atlas: exakt x = 2 + 64*k (2, 66, 130, 194 -> 2..258)
# col0=+X, col1=-Z(Front), col2=-X, col3=+Z(Back)
_RIGHT_LIMB_COLS = (
    ((19, 355, 83, 483), 2),     # +X = aussen rechts
    ((217, 355, 281, 483), 66),  # -Z = vorne rechts
    ((151, 355, 215, 483), 130), # -X = innen rechts
    ((85, 355, 149, 483), 194),  # +Z = hinten rechts
)
_RIGHT_LIMB_TOP = (217, 289, 281, 353)
_RIGHT_LIMB_BOT = (217, 485, 281, 549)

_LEFT_LIMB_COLS = (
    ((374, 355, 438, 483), 2),   # +X = innen links
    ((308, 355, 372, 483), 66),  # -Z = vorne links
    ((506, 355, 570, 483), 130), # -X = aussen links
    ((440, 355, 504, 483), 194), # +Z = hinten links
)
_LEFT_LIMB_TOP = (308, 289, 372, 353)
_LEFT_LIMB_BOT = (308, 485, 372, 549)


def open_rgba(raw: Optional[bytes]) -> Optional[Any]:
    """Dekodiert Bild-Bytes als RGBA-PIL-Image oder gibt None zurueck."""
    if not raw or Image is None or np is None:
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        return img.convert("RGBA")
    except Exception:  # noqa: BLE001
        return None


def is_classic_template(img: Optional[Any]) -> bool:
    """Erkennt ein klassisches 585x559-Shirt/Pants-Template (inkl. HD-Vielfache)."""
    if img is None:
        return False
    w, h = img.size
    if (w, h) == TEMPLATE_SIZE:
        return True
    return w >= 290 and h >= 275 and abs((w / float(h)) - (585.0 / 559.0)) < 0.012


def _normalize_template(img: Image.Image) -> Image.Image:
    if img.size == TEMPLATE_SIZE:
        return img
    return img.resize(TEMPLATE_SIZE, Image.Resampling.LANCZOS)


def _skin_canvas(size: Tuple[int, int], skin_rgb: ColorRGB) -> Image.Image:
    """Erzeugt eine Basis-Textur in Hautfarbe mit Alpha=0 (unbedeckte Haut = Glas)."""
    r = max(0, min(255, int(round(skin_rgb[0] * 255.0))))
    g = max(0, min(255, int(round(skin_rgb[1] * 255.0))))
    b = max(0, min(255, int(round(skin_rgb[2] * 255.0))))
    arr = np.zeros((size[1], size[0], 4), dtype=np.uint8)
    arr[:, :, 0] = r
    arr[:, :, 1] = g
    arr[:, :, 2] = b
    arr[:, :, 3] = 0
    return Image.fromarray(arr, mode="RGBA")


def _alpha_paste_with_bleed(
    canvas: np.ndarray,
    patch_img: Image.Image,
    dst_x: int,
    dst_y: int,
    bleed: int = 2,
) -> None:
    """Blendet ``patch_img`` inkl. ``bleed`` Pixel Kanten-Padding per Alpha-Over ein.

    Wichtig fuer Blender/Cycles (`CHANNEL_PACKED`): RGB speichert bereits die
    fertig ueber der Hautfarbe gemischte Farbe `(1-a)*dst_rgb + a*src_rgb`,
    waehrend der Alpha-Kanal die Gesamt-Kleidungsdeckung `a_out` traegt.
    So entstehen an halbtransparenten Kanten (Ausschnitt, Belly-Schattierung,
    UV-Bevel-Naehten) keinerlei schwarze oder weisse Saum-Artefakte.
    """
    src = np.asarray(patch_img, dtype=np.float32) / 255.0
    if bleed > 0:
        src = np.pad(src, ((bleed, bleed), (bleed, bleed), (0, 0)), mode="edge")
        dst_x -= bleed
        dst_y -= bleed

    ch, cw = canvas.shape[:2]
    sh, sw = src.shape[:2]
    x0 = max(0, dst_x)
    y0 = max(0, dst_y)
    x1 = min(cw, dst_x + sw)
    y1 = min(ch, dst_y + sh)
    if x1 <= x0 or y1 <= y0:
        return

    sub = src[y0 - dst_y:y1 - dst_y, x0 - dst_x:x1 - dst_x]
    dst = canvas[y0:y1, x0:x1]
    sa = sub[:, :, 3:4]
    dst[:, :, :3] = dst[:, :, :3] * (1.0 - sa) + sub[:, :, :3] * sa
    dst[:, :, 3:4] = dst[:, :, 3:4] * (1.0 - sa) + sa


def _to_png_bytes(canvas_f32: np.ndarray) -> bytes:
    arr = np.clip(np.rint(canvas_f32 * 255.0), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr, mode="RGBA").save(buf, format="PNG")
    return buf.getvalue()


def _dilate_into_gutter(canvas: np.ndarray, used: np.ndarray, steps: int = 2) -> None:
    """Weitet Panel-Raender nur in unbenutzte Gutter-Pixel (`~used`) aus."""
    filled = used.copy()
    for _ in range(steps):
        empty = ~filled
        if not empty.any():
            break
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            s_fill = np.roll(np.roll(filled, dy, axis=0), dx, axis=1)
            take = empty & s_fill
            if take.any():
                s_canvas = np.roll(np.roll(canvas, dy, axis=0), dx, axis=1)
                canvas[take] = s_canvas[take]
                filled[take] = True
                empty = ~filled


def composite_torso_texture(
    skin_rgb: ColorRGB,
    pants_bytes: Optional[bytes] = None,
    shirt_bytes: Optional[bytes] = None,
    tshirt_bytes: Optional[bytes] = None,
) -> Optional[bytes]:
    """Baut die `388x272`-Torso-Textur aus Pants -> Shirt -> TShirt (oder None)."""
    pants_img = open_rgba(pants_bytes)
    shirt_img = open_rgba(shirt_bytes)
    tshirt_img = open_rgba(tshirt_bytes)

    has_pants = is_classic_template(pants_img)
    has_shirt = is_classic_template(shirt_img)
    has_tshirt = tshirt_img is not None and tshirt_img.size[0] >= 16 and tshirt_img.size[1] >= 16

    if not (has_pants or has_shirt or has_tshirt):
        return None

    canvas = np.asarray(_skin_canvas(TORSO_MAP_SIZE, skin_rgb), dtype=np.float32) / 255.0
    used = np.zeros(canvas.shape[:2], dtype=bool)
    for src_box, (dx, dy) in _TORSO_PANELS:
        pw, ph = src_box[2] - src_box[0], src_box[3] - src_box[1]
        used[dy:dy + ph, dx:dx + pw] = True

    for tmpl_img, active in ((pants_img, has_pants), (shirt_img, has_shirt)):
        if not active or tmpl_img is None:
            continue
        norm = _normalize_template(tmpl_img)
        for src_box, (dx, dy) in _TORSO_PANELS:
            _alpha_paste_with_bleed(canvas, norm.crop(src_box), dx, dy, bleed=0)
        # Interne R15-Taillenkugel von UpperTorso (x=134..198, y=38..70) mit
        # dem unteren Shirt-/Pants-Saum fuellen, damit keine Haut am Gelenk blitzt.
        waist_cap = norm.crop((231, 164, 359, 202)).resize((66, 36), Image.Resampling.BILINEAR)
        _alpha_paste_with_bleed(canvas, waist_cap, 132, 36, bleed=0)
        used[36:72, 132:198] = True

    if has_tshirt and tshirt_img is not None:
        graphic = tshirt_img.resize((128, 128), Image.Resampling.LANCZOS)
        _alpha_paste_with_bleed(canvas, graphic, 2, 74, bleed=0)
        used[74:202, 2:130] = True

    _dilate_into_gutter(canvas, used, steps=2)
    return _to_png_bytes(canvas)


def composite_limb_texture(
    skin_rgb: ColorRGB,
    template_bytes: Optional[bytes],
    side: str,
) -> Optional[bytes]:
    """Baut die `264x284`-Arm/Bein-Textur (`side` = 'left' oder 'right') oder None."""
    tmpl_img = open_rgba(template_bytes)
    if not is_classic_template(tmpl_img) or tmpl_img is None:
        return None

    norm = _normalize_template(tmpl_img)
    cols = _RIGHT_LIMB_COLS if side == "right" else _LEFT_LIMB_COLS
    top_box = _RIGHT_LIMB_TOP if side == "right" else _LEFT_LIMB_TOP
    bot_box = _RIGHT_LIMB_BOT if side == "right" else _LEFT_LIMB_BOT

    canvas = np.asarray(_skin_canvas(LIMB_MAP_SIZE, skin_rgb), dtype=np.float32) / 255.0
    used = np.zeros(canvas.shape[:2], dtype=bool)

    top_patch = norm.crop(top_box)
    _alpha_paste_with_bleed(canvas, top_patch, 2, 2, bleed=0)
    used[2:66, 2:66] = True

    # Interne R15-Ellenbogen-/Kniekugel von UpperArm/UpperLeg (x=68..130, y=28..68)
    # mit der mittleren Vorderseiten-Zeile fuellen, damit gebeugte/rotierte Arme
    # am Ellenbogengelenk keinen Hautstreifen zeigen.
    front_patch = norm.crop(cols[1][0])
    joint_cap = front_patch.crop((0, 44, 64, 80)).resize((64, 42), Image.Resampling.BILINEAR)
    _alpha_paste_with_bleed(canvas, joint_cap, 66, 26, bleed=0)
    used[26:68, 66:130] = True

    for src_box, dx in cols:
        col_patch = norm.crop(src_box)  # 64 x 128
        shaft = col_patch.crop((0, 0, 64, 112)).resize((64, 146), Image.Resampling.BILINEAR)
        cuff = col_patch.crop((0, 112, 64, 128)).resize((64, 24), Image.Resampling.BILINEAR)
        _alpha_paste_with_bleed(canvas, shaft, dx, 68, bleed=0)
        _alpha_paste_with_bleed(canvas, cuff, dx, 214, bleed=0)
        used[68:238, dx:dx + 64] = True

    sole = norm.crop(bot_box).resize((68, 42), Image.Resampling.BILINEAR)
    _alpha_paste_with_bleed(canvas, sole, 88, 240, bleed=0)
    used[240:282, 88:156] = True

    _dilate_into_gutter(canvas, used, steps=2)
    return _to_png_bytes(canvas)


def prepare_opaque_mesh_texture(raw: Optional[bytes]) -> Optional[bytes]:
    """Bereitet eine Bundle-/MeshPart-Textur (z. B. Korblox, Haare, Krone) auf.

    Alte Roblox-PNGs enthalten haeufig `Alpha = 0` auf UV-Saum-Raendern oder
    Fuss-/Hand-Inseln, obwohl die Oberflaeche im Spiel opak ist (`Transparency=0`).
    In Blender wuerde `Alpha=0` sonst die Hautfarbe durchscheinen lassen oder
    RGB ausnullen. Wir weiten benachbarte opake RGB-Pixel in leere `(0,0,0,0)`-
    Saumpixel aus und setzen Alpha auf 255.
    """
    img = open_rgba(raw)
    if img is None or img.size[0] < 4 or img.size[1] < 4:
        return raw
    arr = np.asarray(img, dtype=np.uint8).copy()
    alpha = arr[:, :, 3]
    if int(alpha.min()) >= 250:
        return raw

    # Wo RGB bereits vorhanden ist, bleibt es erhalten; nur reine (0,0,0,0)-Luecken
    # erhalten per 4-Nachbarschafts-Dilatation die Nachbarfarbe gegen schwarze Saeume.
    valid = (alpha > 8) | (arr[:, :, :3].max(axis=2) > 4)
    rgb = arr[:, :, :3]
    if valid.any() and not valid.all():
        for _ in range(6):
            missing = ~valid
            if not missing.any():
                break
            for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                shifted_valid = np.roll(np.roll(valid, dy, axis=0), dx, axis=1)
                fill = missing & shifted_valid
                if fill.any():
                    shifted_rgb = np.roll(np.roll(rgb, dy, axis=0), dx, axis=1)
                    rgb[fill] = shifted_rgb[fill]
                    valid[fill] = True
                    missing = ~valid

    arr[:, :, :3] = rgb
    arr[:, :, 3] = 255
    buf = io.BytesIO()
    Image.fromarray(arr, mode="RGBA").save(buf, format="PNG")
    return buf.getvalue()


def composite_face_texture(skin_rgb: ColorRGB, face_bytes: Optional[bytes]) -> Optional[bytes]:
    """Legt eine transparente Gesichts-Textur (Augen/Mund) ueber die Kopffarbe."""
    face_img = open_rgba(face_bytes)
    if face_img is None or face_img.size[0] < 8 or face_img.size[1] < 8:
        return None
    canvas = np.asarray(_skin_canvas(face_img.size, skin_rgb), dtype=np.float32) / 255.0
    _alpha_paste_with_bleed(canvas, face_img, 0, 0, bleed=0)
    return _to_png_bytes(canvas)


# ---------------------------------------------------------------------------
# Passende R15-UV-Koordinaten fuer prozedurale Quader (`make_box`)
# Reihenfolge der 6 Quads in `roblox_mesh.make_box` (je 4 Vertices):
#   0: +Z (Back), 1: -Z (Front), 2: +X (Right), 3: -X (Left), 4: +Y (Top), 5: -Y (Bottom)
# ---------------------------------------------------------------------------


def _quad_uv_from_px(
    tex_w: float,
    tex_h: float,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    flip_x: bool = False,
) -> List[Tuple[float, float]]:
    """Rechnet ein Pixel-Rechteck (Ursprung oben links) in 4 OBJ-UVs um.

    `make_box` baut jedes Seiten-Quad in Reihenfolge:
    unten-rechts/links -> unten-links/rechts -> oben-... -> oben-...:
      +Z (Back):  (-x,-y,+z), (+x,-y,+z), (+x,+y,+z), (-x,+y,+z)
      -Z (Front): (+x,-y,-z), (-x,-y,-z), (-x,+y,-z), (+x,+y,-z)
      +X (Right): (+x,-y,-z), (+x,-y,+z), (+x,+y,+z), (+x,+y,-z)
      -X (Left):  (-x,-y,+z), (-x,-y,-z), (-x,+y,-z), (-x,+y,+z)
    Deshalb entspricht Vertex 0..1 immer `y1` (unten) und Vertex 2..3 `y0` (oben)!
    """
    u_left = x0 / tex_w
    u_right = x1 / tex_w
    if flip_x:
        u_left, u_right = u_right, u_left
    v_bot = 1.0 - (y1 / tex_h)
    v_top = 1.0 - (y0 / tex_h)
    return [
        (u_left, v_bot),
        (u_right, v_bot),
        (u_right, v_top),
        (u_left, v_top),
    ]


def apply_torso_box_uvs(box: MeshData, part_slice: str = "full") -> MeshData:
    """Weist einem `make_box`-Quader exakte `388x272`-R15-Torso-UVs zu."""
    if len(box.uvs) != 24:
        return box
    w, h = float(TORSO_MAP_SIZE[0]), float(TORSO_MAP_SIZE[1])
    if part_slice == "upper":
        y0, y1 = 74.0, 178.0
    elif part_slice == "lower":
        y0, y1 = 178.0, 202.0
    else:
        y0, y1 = 74.0, 202.0

    uvs: List[Tuple[float, float]] = []
    uvs.extend(_quad_uv_from_px(w, h, 194, y0, 322, y1))  # 0: +Z Back
    uvs.extend(_quad_uv_from_px(w, h, 2, y0, 130, y1))    # 1: -Z Front
    uvs.extend(_quad_uv_from_px(w, h, 322, y0, 386, y1))  # 2: +X Right
    uvs.extend(_quad_uv_from_px(w, h, 130, y0, 194, y1))  # 3: -X Left
    uvs.extend(_quad_uv_from_px(w, h, 2, 10, 130, 74))    # 4: +Y Top
    uvs.extend(_quad_uv_from_px(w, h, 2, 202, 130, 266))  # 5: -Y Bottom
    return MeshData(
        positions=list(box.positions),
        normals=list(box.normals),
        uvs=uvs,
        faces=list(box.faces),
        colors=list(box.colors) if box.colors else None,
        source_version=box.source_version,
    )


def apply_limb_box_uvs(box: MeshData, part_slice: str = "full") -> MeshData:
    """Weist einem `make_box`-Quader exakte `264x284`-R15-Gliedmassen-UVs zu."""
    if len(box.uvs) != 24:
        return box
    w, h = float(LIMB_MAP_SIZE[0]), float(LIMB_MAP_SIZE[1])
    if part_slice == "upper":
        y0, y1 = 68.0, 141.0
    elif part_slice == "lower":
        y0, y1 = 141.0, 214.0
    elif part_slice == "end":
        y0, y1 = 214.0, 238.0
    else:
        y0, y1 = 68.0, 238.0

    uvs: List[Tuple[float, float]] = []
    uvs.extend(_quad_uv_from_px(w, h, 194, y0, 258, y1))  # 0: +Z Back
    uvs.extend(_quad_uv_from_px(w, h, 66, y0, 130, y1))   # 1: -Z Front
    uvs.extend(_quad_uv_from_px(w, h, 2, y0, 66, y1))     # 2: +X
    uvs.extend(_quad_uv_from_px(w, h, 130, y0, 194, y1))  # 3: -X
    uvs.extend(_quad_uv_from_px(w, h, 2, 2, 66, 66))      # 4: +Y Top
    uvs.extend(_quad_uv_from_px(w, h, 90, 240, 154, 282)) # 5: -Y Bottom
    return MeshData(
        positions=list(box.positions),
        normals=list(box.normals),
        uvs=uvs,
        faces=list(box.faces),
        colors=list(box.colors) if box.colors else None,
        source_version=box.source_version,
    )


#: UV-Rechtecke (in Pixeln der 264x284-Gliedmassen-Vorlage) je Flaeche.
_LIMB_RECTS = {
    "+Z": (194, 258),   # Rueckseite
    "-Z": (66, 130),    # Vorderseite
    "+X": (2, 66),
    "-X": (130, 194),
}


#: Rand, der beim Projizieren ausgespart wird. Die Gliedmassen-Vorlage hat
#: zwischen den Abschnitten weisse Trennzeilen; ohne Rand ziehen die
#: abgerundeten Kanten der Standard-Meshes diese Zeilen als weisse Streifen
#: ueber die Naht.
_LIMB_UV_MARGIN = 3.0


def _limb_slice_rows(part_slice: str) -> Tuple[float, float, float, float]:
    """Pixel-Zeilen fuer einen Gliedmassen-Abschnitt (wie `apply_limb_box_uvs`)."""
    w, h = float(LIMB_MAP_SIZE[0]), float(LIMB_MAP_SIZE[1])
    if part_slice == "upper":
        y0, y1 = 68.0, 141.0
    elif part_slice == "lower":
        y0, y1 = 141.0, 214.0
    elif part_slice == "end":
        y0, y1 = 214.0, 238.0
    else:
        y0, y1 = 68.0, 238.0
    m = _LIMB_UV_MARGIN
    if y1 - y0 > 4 * m:
        y0, y1 = y0 + m, y1 - m
    return w, h, y0, y1


def apply_limb_uvs_projected(mesh: MeshData, part_slice: str = "full") -> MeshData:
    """Projiziert die R15-Gliedmassen-UVs auf ein *beliebiges* Mesh.

    Fuer Quader gibt es `apply_limb_box_uvs` (feste 24 Vertices). Der
    Standard-Avatar besteht aber aus echten Roblox-Meshes (R15-Block) mit
    abgerundeten Kanten und eigener Topologie. Damit Shirt/Hose auch dort
    landen, wird hier jede Dreiecksflaeche ueber ihre Normale einer der sechs
    Quaderseiten zugeordnet und ihre Vertices in das passende UV-Rechteck der
    `264x284`-Vorlage projiziert. Fuer einen Quader kommt exakt dasselbe
    Ergebnis heraus wie bei `apply_limb_box_uvs`.
    """
    if mesh.is_empty or not mesh.positions:
        return mesh
    tex_w, tex_h, y0, y1 = _limb_slice_rows(part_slice)
    lo, hi = mesh.bbox()
    span_x = max(1e-6, hi[0] - lo[0])
    span_y = max(1e-6, hi[1] - lo[1])
    span_z = max(1e-6, hi[2] - lo[2])

    def _quad_uv(rect, s: float, t: float) -> Tuple[float, float]:
        x0, x1 = rect
        m = _LIMB_UV_MARGIN
        corners = _quad_uv_from_px(tex_w, tex_h, x0 + m, y0, x1 - m, y1)
        (ul, vb), (ur, _vb2), (_ur2, vt) = corners[0], corners[1], corners[2]
        return (ul + s * (ur - ul), vb + t * (vt - vb))

    # Kanten-UVs fuer Deckflaechen (Top/Bottom) - gleiche Rechtecke wie im Quader.
    top_rect = (2, 66)
    bottom_rect = (90, 154)
    top_rows = (2.0, 66.0)
    bottom_rows = (240.0, 282.0)

    positions: List[Tuple[float, float, float]] = []
    normals: List[Tuple[float, float, float]] = []
    uvs: List[Tuple[float, float]] = []
    faces: List[Tuple[int, int, int]] = []
    for face in mesh.faces:
        if len(face) < 3:
            continue
        idx = list(face)[:3]
        p = [mesh.positions[i] for i in idx]
        n = _face_normal(p[0], p[1], p[2])
        ax = max(range(3), key=lambda k: abs(n[k]))
        sign = 1.0 if n[ax] >= 0 else -1.0
        if ax == 2:      # +/-Z
            rect = _LIMB_RECTS["+Z" if sign > 0 else "-Z"]
            for q in p:
                s = ((q[0] - lo[0]) / span_x if sign > 0
                     else (hi[0] - q[0]) / span_x)
                uvs.append(_quad_uv(rect, s, (q[1] - lo[1]) / span_y))
        elif ax == 0:    # +/-X
            rect = _LIMB_RECTS["+X" if sign > 0 else "-X"]
            for q in p:
                s = ((hi[2] - q[2]) / span_z if sign > 0
                     else (q[2] - lo[2]) / span_z)
                uvs.append(_quad_uv(rect, s, (q[1] - lo[1]) / span_y))
        else:            # +/-Y
            x0, x1 = top_rect if sign > 0 else bottom_rect
            py0, py1 = top_rows if sign > 0 else bottom_rows
            m = _LIMB_UV_MARGIN
            for q in p:
                corners = _quad_uv_from_px(tex_w, tex_h, x0 + m, py0 + m,
                                           x1 - m, py1 - m)
                s = (q[0] - lo[0]) / span_x
                t = (q[2] - lo[2]) / span_z
                (ul, vb) = corners[0]
                (ur, _b) = corners[1]
                (_u, vt) = corners[2]
                uvs.append((ul + s * (ur - ul), vb + t * (vt - vb)))
        base = len(positions)
        for k, q in enumerate(p):
            positions.append(q)
            normals.append(mesh.normals[idx[k]] if idx[k] < len(mesh.normals) else n)
        faces.append((base, base + 1, base + 2))
    return MeshData(
        positions=positions, normals=normals, uvs=uvs, faces=faces,
        colors=None, source_version=mesh.source_version,
    )


def build_multi_segment_atlas(
    segments: Sequence[Tuple[MeshData, Optional[bytes]]],
) -> Tuple[List[MeshData], Optional[bytes]]:
    """Packt mehrere Segmente mit unterschiedlichen Texturen in einen horizontalen Atlas."""
    unique_tex: List[bytes] = []
    seg_indices: List[int] = []
    for _m, raw in segments:
        if raw is None:
            seg_indices.append(-1)
            continue
        found = -1
        for i, existing in enumerate(unique_tex):
            if existing == raw:
                found = i
                break
        if found < 0:
            found = len(unique_tex)
            unique_tex.append(raw)
        seg_indices.append(found)

    if len(unique_tex) <= 1:
        return [m for m, _ in segments], (unique_tex[0] if unique_tex else None)

    imgs = [open_rgba(raw) for raw in unique_tex]
    if any(im is None for im in imgs):
        return [m for m, _ in segments], unique_tex[0]

    tile_w = max(im.size[0] for im in imgs if im is not None)
    tile_h = max(im.size[1] for im in imgs if im is not None)
    n = len(imgs)
    atlas = Image.new("RGBA", (tile_w * n, tile_h), (255, 255, 255, 255))
    for i, im in enumerate(imgs):
        assert im is not None
        resized = im if im.size == (tile_w, tile_h) else im.resize(
            (tile_w, tile_h), Image.Resampling.LANCZOS
        )
        atlas.paste(resized, (i * tile_w, 0))

    remapped: List[MeshData] = []
    for (m, _raw), idx in zip(segments, seg_indices):
        slot = max(0, idx)
        new_uvs = [
            ((slot + max(0.002, min(0.998, u % 1.0 if not (0.0 <= u <= 1.0) else u))) / n, v)
            for (u, v) in m.uvs
        ]
        remapped.append(
            MeshData(
                positions=list(m.positions),
                normals=list(m.normals),
                uvs=new_uvs,
                faces=list(m.faces),
                colors=list(m.colors) if m.colors else None,
                source_version=m.source_version,
                skinning=m.skinning,
            )
        )

    buf = io.BytesIO()
    atlas.save(buf, format="PNG")
    return remapped, buf.getvalue()
