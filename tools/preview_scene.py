#!/usr/bin/env python3
"""Schnelle Software-Vorschau der gebauten Szene - ohne Blender.

Das Glas-Rendering macht Blender (`blender/render_glass.py`). Wer nur
prüfen will, ob **Rig, Kamera und Kleidungs-UVs** stimmen, kann diese
Vorschau nutzen: sie liest `manifest.json` + `parts/*.obj`, rastert die
Dreiecke in reinem Python und färbt mit denselben Regeln wie das
Blender-Skript (Textur mit Alpha gegen die Teilfarbe, Kamera aus
`scene_camera.py`).

Kein Ersatz für den finalen Render - aber in Sekunden fertig und ohne
GPU/Blender installierbar:

    python3 tools/build_avatar.py --dry-run --out build
    python3 tools/preview_scene.py --manifest build/manifest.json \
        --out preview.png

Abhängigkeiten: `numpy` und `Pillow` (nur für dieses Vorschau-Tool; die
Pipeline selbst braucht sie nicht).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scene_camera  # noqa: E402

# Zielauflösung der Vorschau
DEFAULT_RESOLUTION = "900x700"
SUPERSAMPLE = 2

BACKGROUND_TOP = (0.94, 0.96, 0.98)
BACKGROUND_BOTTOM = (0.80, 0.84, 0.89)
GLASS_TINT = (0.54, 0.80, 0.96)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Software-Vorschau einer Avatar-Szene")
    parser.add_argument("--manifest", default="build/manifest.json")
    parser.add_argument("--out", default="preview.png")
    parser.add_argument("--resolution", default=DEFAULT_RESOLUTION)
    parser.add_argument("--azimuth", type=float, default=24.0)
    parser.add_argument("--elevation", type=float, default=9.0)
    parser.add_argument("--texture-strength", type=float, default=0.8)
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# OBJ + Farben
# ---------------------------------------------------------------------------


def load_obj(path):
    """OBJ laden und in Blender-Koordinaten bringen (forward=-Z, up=Y)."""
    import numpy as np

    verts, uvs, normals, faces = [], [], [], []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("v "):
                verts.append(tuple(float(x) for x in line.split()[1:4]))
            elif line.startswith("vt "):
                uvs.append(tuple(float(x) for x in line.split()[1:3]))
            elif line.startswith("vn "):
                normals.append(tuple(float(x) for x in line.split()[1:4]))
            elif line.startswith("f "):
                corners = []
                for token in line.split()[1:]:
                    parts = (token.split("/") + ["", ""])[:3]
                    corners.append((int(parts[0]) - 1,
                                    int(parts[1]) - 1 if parts[1] else -1,
                                    int(parts[2]) - 1 if parts[2] else -1))
                for k in range(1, len(corners) - 1):
                    faces.append((corners[0], corners[k], corners[k + 1]))

    V = np.array(verts, dtype=float).reshape(-1, 3)
    # Roblox (Y-up, -Z vorne) -> Blender (Z-up): (x, y, z) -> (x, -z, y)
    V = np.stack([V[:, 0], -V[:, 2], V[:, 1]], axis=1)
    VT = np.array(uvs, dtype=float).reshape(-1, 2) if uvs else None
    VN = np.array(normals, dtype=float).reshape(-1, 3) if normals else None
    if VN is not None:
        VN = np.stack([VN[:, 0], -VN[:, 2], VN[:, 1]], axis=1)
    return V, VT, VN, faces


def hex_rgb(value):
    import numpy as np
    value = (value or "#cccccc").lstrip("#")
    return np.array([int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4)])


def srgb_to_linear(color):
    import numpy as np
    return np.where(color <= 0.04045, color / 12.92,
                    ((color + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(color):
    import numpy as np
    color = np.clip(color, 0.0, 1.0)
    return np.where(color <= 0.0031308, color * 12.92,
                    1.055 * color ** (1 / 2.4) - 0.055)


def load_texture(path):
    from PIL import Image
    import numpy as np
    image = Image.open(path).convert("RGBA")
    return np.asarray(image, dtype=float) / 255.0


# ---------------------------------------------------------------------------
# Rasterung
# ---------------------------------------------------------------------------


def render(manifest_path: str, out_path: str, resolution=DEFAULT_RESOLUTION,
           azimuth: float = 24.0, elevation: float = 9.0,
           texture_strength: float = 0.8) -> dict:
    import numpy as np
    from PIL import Image

    width, height = (int(v) for v in resolution.lower().split("x"))
    ss = SUPERSAMPLE
    W, H = width * ss, height * ss

    base = os.path.dirname(os.path.abspath(manifest_path))
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    parts = []
    textures = {}
    for part in manifest.get("parts", []):
        obj_path = os.path.join(base, part.get("obj", ""))
        if not os.path.exists(obj_path):
            continue
        V, VT, VN, faces = load_obj(obj_path)
        if not faces:
            continue
        tex = None
        if part.get("texture"):
            tex_path = os.path.join(base, part["texture"])
            if os.path.exists(tex_path):
                if tex_path not in textures:
                    textures[tex_path] = load_texture(tex_path)
                tex = textures[tex_path]
        tint = part.get("tint") or part.get("color")
        parts.append({"part": part, "V": V, "VT": VT, "VN": VN,
                      "faces": faces, "tex": tex,
                      "color": hex_rgb(part.get("color")),
                      "tint": hex_rgb(tint)})
    if not parts:
        raise SystemExit("Keine Geometrie im Manifest gefunden.")

    lo = np.array([1e9] * 3)
    hi = np.array([-1e9] * 3)
    for entry in parts:
        lo = np.minimum(lo, entry["V"].min(axis=0))
        hi = np.maximum(hi, entry["V"].max(axis=0))
    center, location, distance = scene_camera.frame_distance(
        lo, hi, azimuth, elevation, res_x=W, res_y=H)

    location = np.array(location)
    forward = np.array(center) - location
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    half_w = 18.0 / 55.0          # sensor/2 / Brennweite (wie render_glass.py)

    # Hintergrund: sanfter Verlauf wie im Blender-Render
    top = np.array(BACKGROUND_TOP)
    bottom = np.array(BACKGROUND_BOTTOM)
    ramp = np.linspace(0.0, 1.0, H).reshape(H, 1)
    column = top.reshape(1, 3) * (1.0 - ramp) + bottom.reshape(1, 3) * ramp
    image = np.repeat(column[:, None, :], W, axis=1)

    zbuf = np.full((H, W), np.inf)
    light = np.array([0.45, 0.75, 0.55])
    light /= np.linalg.norm(light)
    glass = np.array(GLASS_TINT)
    strength = max(0.0, min(1.0, texture_strength))

    for entry in parts:
        V, VT, VN, faces = entry["V"], entry["VT"], entry["VN"], entry["faces"]
        color = srgb_to_linear(entry["color"])
        # Kleidungsfarbe (Durchschnitt der Atlas-Panels) - wie im
        # Blender-Material, das damit das Glas einfaerbt.
        cloth = srgb_to_linear(entry["tint"])
        kind = entry["part"].get("kind")
        tex = entry["tex"]
        for face in faces:
            P = V[[f[0] for f in face]]
            rel = P - location
            z = rel @ forward
            if np.any(z <= 0.05):
                continue
            hw = z * half_w
            hh = hw * (H / W)
            px = ((rel @ right) / hw + 1.0) * 0.5 * W
            py = (1.0 - (rel @ up) / hh) * 0.5 * H
            x0, x1 = int(max(0, math.floor(px.min()))), int(min(W - 1, math.ceil(px.max())))
            y0, y1 = int(max(0, math.floor(py.min()))), int(min(H - 1, math.ceil(py.max())))
            if x1 < x0 or y1 < y0:
                continue
            det = ((py[1] - py[2]) * (px[0] - px[2])
                   + (px[2] - px[1]) * (py[0] - py[2]))
            if abs(det) < 1e-12:
                continue
            xs, ys = np.meshgrid(np.arange(x0, x1 + 1) + 0.5,
                                 np.arange(y0, y1 + 1) + 0.5)
            w0 = ((py[1] - py[2]) * (xs - px[2])
                  + (px[2] - px[1]) * (ys - py[2])) / det
            w1 = ((py[2] - py[0]) * (xs - px[2])
                  + (px[0] - px[2]) * (ys - py[2])) / det
            w2 = 1.0 - w0 - w1
            mask = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
            if not mask.any():
                continue
            depth = w0 * z[0] + w1 * z[1] + w2 * z[2]
            sub = zbuf[y0:y1 + 1, x0:x1 + 1]
            mask &= depth < sub
            if not mask.any():
                continue
            rows, cols = np.where(mask)
            weights = np.stack([w0[mask], w1[mask], w2[mask]], axis=1)
            sub[rows, cols] = depth[mask]

            if VN is not None:
                normals = np.stack([VN[f[2]] for f in face])
            else:
                edge = np.cross(P[1] - P[0], P[2] - P[0])
                normals = np.stack([edge] * 3)
            normal = weights @ normals
            normal /= (np.linalg.norm(normal, axis=1, keepdims=True) + 1e-9)
            shade = 0.35 + 0.65 * np.maximum(0.0, normal @ light)
            fresnel = 0.25 + 0.75 * (1.0 - np.abs(normal @ (-forward))) ** 2

            rgb = np.tile(color, (len(rows), 1))
            if tex is not None and VT is not None:
                uv = np.stack([VT[f[1]] for f in face])
                tuv = weights @ uv
                th, tw = tex.shape[:2]
                tx = np.clip((tuv[:, 0] % 1.0) * (tw - 1), 0, tw - 1).astype(int)
                ty = np.clip((1.0 - (tuv[:, 1] % 1.0)) * (th - 1), 0, th - 1).astype(int)
                sample = tex[ty, tx]
                # Gamma-Aufhellung wie im Blender-Material (dunkle
                # Kleidung bleibt lesbar)
                tex_rgb = srgb_to_linear(sample[:, :3]) ** 0.72
                # Textur fest mit der Teilfarbe mischen (wie im Blender-Material)
                lit = tex_rgb * strength + cloth * (1.0 - strength)
                rgb = lit * sample[:, 3:4] + color * (1.0 - sample[:, 3:4])

            if kind == "decal":
                out = linear_to_srgb(rgb * shade[:, None])
                if tex is not None:
                    alpha = sample[:, 3]
                    out = out * alpha[:, None] + image[rows + y0, cols + x0] * (1.0 - alpha[:, None])
            elif kind == "accessory":
                out = linear_to_srgb(rgb * shade[:, None])
            else:
                # Glas: Grundfarbe + heller Fresnel-Rand
                body = linear_to_srgb(rgb * shade[:, None])
                out = body * (0.72 + 0.28 * fresnel[:, None]) \
                    + glass * (0.18 * fresnel[:, None])
            image[rows + y0, cols + x0] = np.clip(out, 0.0, 1.0)

    if ss > 1:
        image = image.reshape(height, ss, width, ss, 3).mean(axis=(1, 3))
    rgba = np.concatenate([image, np.ones((height, width, 1))], axis=2)
    Image.fromarray((np.clip(rgba, 0.0, 1.0) * 255).astype("uint8")).save(out_path)
    return {"parts": len(parts), "textures": len(textures),
            "bbox": (lo.round(2).tolist(), hi.round(2).tolist()),
            "camera": np.round(location, 2).tolist(),
            "distance": round(float(distance), 2), "out": out_path}


def main(argv=None) -> int:
    args = parse_args(argv)
    info = render(args.manifest, args.out, args.resolution, args.azimuth,
                  args.elevation, args.texture_strength)
    print(f"Vorschau: {info['out']} ({info['parts']} Teile, "
          f"{info['textures']} Texturen, Kamera {info['camera']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
