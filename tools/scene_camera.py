"""Kamera-Mathematik fuer den Blender-Render (ohne Blender testbar).

Koordinaten
-----------
Roblox und OBJ sind beide Y-up mit -Z als Blickrichtung ("vorne").
Blender ist Z-up; der OBJ-Import (``forward_axis="NEGATIVE_Z",
up_axis="Y"``) dreht deshalb

    (x, y, z)_roblox  ->  (x, -z, y)_blender

Damit landet die Roblox-Front (-Z) bei **+Y** in Blender, oben (+Y) bei
+Z und rechts (+X) bleibt +X. Die Kamera muss also von der +Y-Seite
kommen - stand sie bei -Y, sieht man den Ruecken des Avatars.

Azimut 0 = frontal, positive Werte drehen die Kamera zur rechten
Koerperhaelfte (+X) des Charakters; Elevation 0 = auf Modellmitte.
"""

from __future__ import annotations

import math
from typing import Sequence, Tuple

Vec3 = Tuple[float, float, float]


def camera_direction(azimuth_deg: float, elevation_deg: float) -> Vec3:
    """Einheitsvektor vom Modell zur Kamera (Blender-Koordinaten)."""
    az = math.radians(azimuth_deg)
    el = math.radians(elevation_deg)
    x = math.sin(az) * math.cos(el)
    y = math.cos(az) * math.cos(el)
    z = math.sin(el)
    length = math.sqrt(x * x + y * y + z * z) or 1.0
    return (x / length, y / length, z / length)


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(a[i] * b[i] for i in range(3))


def _cross(a: Sequence[float], b: Sequence[float]) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _normalized(v: Sequence[float]) -> Vec3:
    length = math.sqrt(_dot(v, v)) or 1.0
    return (v[0] / length, v[1] / length, v[2] / length)


def frame_distance(lo: Sequence[float], hi: Sequence[float],
                   azimuth_deg: float = 24.0, elevation_deg: float = 9.0,
                   lens: float = 55.0, sensor_width: float = 36.0,
                   res_x: int = 1280, res_y: int = 720,
                   margin: float = 1.12) -> Tuple[Vec3, Vec3, float]:
    """Kameraziel, Kameraposition und Distanz aus der Bounding-Box.

    Die Ecken der Box werden auf die Kamera-Achsen (rechts/oben/Tiefe)
    projiziert; daraus folgt die noetige Entfernung fuer den Bildausschnitt.
    Rueckgabe: (center, location, distance).
    """
    center = tuple((float(lo[i]) + float(hi[i])) / 2.0 for i in range(3))
    direction = camera_direction(azimuth_deg, elevation_deg)
    right = _cross(direction, (0.0, 0.0, 1.0))
    if _dot(right, right) < 1e-12:
        right = (1.0, 0.0, 0.0)
    right = _normalized(right)
    up = _normalized(_cross(right, direction))

    corners = [(x, y, z) for x in (lo[0], hi[0])
               for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
    rel = [tuple(c[i] - center[i] for i in range(3)) for c in corners]
    half_w = max(abs(_dot(r, right)) for r in rel)
    half_h = max(abs(_dot(r, up)) for r in rel)
    half_d = max(abs(_dot(r, direction)) for r in rel)

    h_fov = 2.0 * math.atan(sensor_width / 2.0 / lens)
    v_fov = 2.0 * math.atan(math.tan(h_fov / 2.0) * (res_y / res_x))
    dist_v = half_h / math.tan(v_fov / 2.0)
    dist_h = half_w / math.tan(h_fov / 2.0)
    distance = (max(dist_v, dist_h) + half_d * 0.9) * margin
    location = tuple(center[i] + direction[i] * distance for i in range(3))
    return center, location, distance


def project(point: Sequence[float], location: Sequence[float],
            res_x: int, res_y: int, lens: float = 55.0,
            sensor_width: float = 36.0):
    """Weltpunkt -> Bildkoordinaten (Pixel) und Tiefe.

    Hilfsfunktion fuer Vorschau/Tests; liefert ``(x, y, depth)`` mit
    y von oben. ``depth <= 0`` bedeutet: hinter der Kamera.
    """
    forward = _normalized(tuple(-location[i] for i in range(3)))
    right = _normalized(_cross(forward, (0.0, 0.0, 1.0)))
    up = _cross(right, forward)
    rel = tuple(point[i] - location[i] for i in range(3))
    depth = _dot(rel, forward)
    if depth <= 1e-9:
        return (None, None, depth)
    half_w = depth * (sensor_width / 2.0 / lens)
    half_h = half_w * (res_y / res_x)
    sx = _dot(rel, right) / half_w
    sy = _dot(rel, up) / half_h
    return ((sx + 1.0) * 0.5 * res_x, (1.0 - sy) * 0.5 * res_y, depth)
