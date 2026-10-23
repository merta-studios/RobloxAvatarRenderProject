"""Tests fuer die Kamera-Nachjustierung in blender/render_glass.py.

Hintergrund (CI-Ausfall 2026-10, Run 40037473691): Bei breiten Posen (Emote
mit ausgebreiteten Armen) lief der Aufzieh-Loop der Kamera-Nachjustierung
viele Solver-Durchlaeufe; die Projektion las dabei ``obj.matrix_world`` und
loeste im Notnagel pro Aufruf ein ``view_layer.update()`` (komplette
Szenen-Auswertung) aus - auf den Software-Renderern (llvmpipe/lavapipe)
frass das die komplette Render-Zeit, der Render startete nie (Exit 124).

Der Fix: Die Projektion rechnet mit der explizit gesetzten Matrix
(``_project_points``) statt ``obj.matrix_world`` zu lesen, und der Solver
hat ein hartes Zeitlimit samt Bestwert-Sicherung.

Diese Tests laufen ohne Blender: bpy/mathutils werden durch kleine
Pure-Python-Attrappen ersetzt, die exakt die noetige Mathematik
implementieren (inkl. ``view_frame``-Eckenreihenfolge wie im
Blender-Quellcode: [oben-rechts, unten-rechts, unten-links, oben-links]).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SCRIPT = os.path.join(ROOT, "blender", "render_glass.py")


# ---------------------------------------------------------------------------
# Mini-mathutils (nur was render_glass fuer Kamera-Solver braucht)
# ---------------------------------------------------------------------------

def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm3(v):
    length = (_dot(v, v)) ** 0.5
    if length < 1e-12:
        return (0.0, 0.0, 0.0)
    return (v[0] / length, v[1] / length, v[2] / length)


class FakeVector(tuple):
    def __new__(cls, values):
        return super().__new__(cls, (float(v) for v in values))

    @property
    def x(self):
        return self[0]

    @property
    def y(self):
        return self[1]

    @property
    def z(self):
        return self[2]

    @property
    def length(self):
        return _dot(self, self) ** 0.5

    def normalized(self):
        return FakeVector(_norm3(tuple(self)))

    def __add__(self, other):
        return FakeVector((self[0] + other[0], self[1] + other[1], self[2] + other[2]))

    def __sub__(self, other):
        return FakeVector((self[0] - other[0], self[1] - other[1], self[2] - other[2]))

    def __mul__(self, k):
        if isinstance(k, (int, float)):
            return FakeVector((self[0] * k, self[1] * k, self[2] * k))
        return NotImplemented

    __rmul__ = __mul__

    def __truediv__(self, k):
        return FakeVector((self[0] / k, self[1] / k, self[2] / k))

    def __neg__(self):
        return FakeVector((-self[0], -self[1], -self[2]))

    def __matmul__(self, other):
        if isinstance(other, FakeVector):
            return _dot(self, other)
        return NotImplemented

    def to_track_quat(self, axis, up):
        """Look-At-Rotation: lokale -Z zeigt entlang dieses Vektors."""
        forward = _norm3(tuple(self))
        z_axis = (-forward[0], -forward[1], -forward[2])
        world_up = (0.0, 1.0, 0.0)
        if abs(_dot(world_up, z_axis)) > 0.999:
            world_up = (0.0, 0.0, 1.0)
        x_axis = _norm3(_cross(world_up, z_axis))
        y_axis = _cross(z_axis, x_axis)
        return FakeQuat((x_axis, y_axis, z_axis))


class FakeQuat:
    def __init__(self, columns):
        self.columns = columns

    def to_euler(self):
        return (0.0, 0.0, 0.0)

    def to_matrix(self):
        return self.columns


class FakeMatrix(tuple):
    """4x4-Matrix als Tupel von Zeilen-Tupeln."""

    def __new__(cls, rows):
        return super().__new__(cls, tuple(tuple(float(v) for v in row) for row in rows))

    @staticmethod
    def LocRotScale(loc, rot, scale):
        cols = rot.to_matrix() if isinstance(rot, FakeQuat) else (
            ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
        sx, sy, sz = (scale[0], scale[1], scale[2])
        r = [[cols[j][i] for j in range(3)] for i in range(3)]
        rows = [
            [r[0][0] * sx, r[0][1] * sy, r[0][2] * sz, loc[0]],
            [r[1][0] * sx, r[1][1] * sy, r[1][2] * sz, loc[1]],
            [r[2][0] * sx, r[2][1] * sy, r[2][2] * sz, loc[2]],
            [0.0, 0.0, 0.0, 1.0],
        ]
        return FakeMatrix(rows)

    def copy(self):
        return FakeMatrix(self)

    def normalized(self):
        """Rotation orthonormalisieren (Gram-Schmidt auf den Spalten)."""
        cols = [[self[i][j] for i in range(3)] for j in range(3)]
        x = _norm3(cols[0])
        y_raw = cols[1]
        y = _norm3((y_raw[0] - _dot(y_raw, x) * x[0],
                    y_raw[1] - _dot(y_raw, x) * x[1],
                    y_raw[2] - _dot(y_raw, x) * x[2]))
        z = _cross(x, y)
        rows = [[x[0], y[0], z[0], self[0][3]],
                [x[1], y[1], z[1], self[1][3]],
                [x[2], y[2], z[2], self[2][3]],
                [0.0, 0.0, 0.0, 1.0]]
        return FakeMatrix(rows)

    def inverted(self):
        """Generalisierte 4x4-Inverse (Gauss-Jordan)."""
        n = 4
        a = [list(row) + [1.0 if i == j else 0.0 for j in range(n)]
             for i, row in enumerate(self)]
        for col in range(n):
            pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
            if abs(a[pivot][col]) < 1e-12:
                raise ValueError("singulaere Matrix")
            a[col], a[pivot] = a[pivot], a[col]
            p = a[col][col]
            a[col] = [v / p for v in a[col]]
            for r in range(n):
                if r != col and a[r][col] != 0.0:
                    f = a[r][col]
                    a[r] = [v - f * w for v, w in zip(a[r], a[col])]
        return FakeMatrix([row[n:] for row in a])

    def __matmul__(self, other):
        if isinstance(other, FakeVector):
            w = self[3][3]
            return FakeVector(tuple(
                self[i][0] * other[0] + self[i][1] * other[1]
                + self[i][2] * other[2] + self[i][3] * w
                for i in range(3)))
        return NotImplemented


# ---------------------------------------------------------------------------
# Fake-bpy (Kamera + Szene)
# ---------------------------------------------------------------------------

class FakeCameraData:
    """lens/sensor wie im Render: 55 mm auf 36 mm Sensor, 2000x2000."""

    type = 'PERSP'
    lens = 55.0
    sensor_width = 36.0
    sensor_fit = 'AUTO'
    shift_x = 0.0
    shift_y = 0.0

    def __init__(self, res_x=2000, res_y=2000):
        self.res_x = res_x
        self.res_y = res_y

    def view_frame(self, scene=None):
        # Reihenfolge wie BKE_camera_view_frame (camera.cc):
        # [oben-rechts, unten-rechts, unten-links, oben-links],
        # Perspektive bei drawsize 0.5: fac = 0.5, Tiefe = -0.5*lens/half_sensor.
        asp_x, asp_y = 1.0, 1.0
        if scene is not None and self.sensor_fit == 'AUTO':
            if self.res_x < self.res_y:
                asp_x = self.res_x / self.res_y
            else:
                asp_y = self.res_y / self.res_x
        half_sensor = 0.5 * self.sensor_width
        depth = 0.5 * self.lens / (-half_sensor)
        facx, facy = 0.5 * asp_x, 0.5 * asp_y
        return (
            FakeVector((self.shift_x + facx, self.shift_y + facy, depth)),
            FakeVector((self.shift_x + facx, self.shift_y - facy, depth)),
            FakeVector((self.shift_x - facx, self.shift_y - facy, depth)),
            FakeVector((self.shift_x - facx, self.shift_y + facy, depth)),
        )


class FakeCamera:
    def __init__(self, res_x=2000, res_y=2000):
        self.location = FakeVector((0.0, -10.0, 2.0))
        self.rotation_euler = (0.0, 0.0, 0.0)
        self.matrix_world = FakeMatrix(((1, 0, 0, 0), (0, 1, 0, 0),
                                         (0, 0, 1, 0), (0, 0, 0, 1)))
        self.data = FakeCameraData(res_x, res_y)


def _load_module():
    """render_glass.py importieren, ohne echtes Blender."""
    bpy = types.ModuleType("bpy")
    bpy.app = types.SimpleNamespace(version_string="5.0.1")
    bpy.data = types.SimpleNamespace(materials=[], images=[], objects=[],
                                     meshes=[], cameras=[], worlds=[], lights=[])
    bpy.ops = types.SimpleNamespace(
        wm=types.SimpleNamespace(read_factory_settings=lambda **kw: None))

    scene = types.SimpleNamespace(
        render=types.SimpleNamespace(resolution_x=2000, resolution_y=2000),
        world=None,
    )
    bpy.context = types.SimpleNamespace(
        scene=scene,
        view_layer=types.SimpleNamespace(update=lambda: None))

    mathutils = types.ModuleType("mathutils")
    mathutils.Vector = FakeVector
    mathutils.Matrix = FakeMatrix

    saved = {name: sys.modules.get(name) for name in ("bpy", "mathutils")}
    sys.modules["bpy"] = bpy
    sys.modules["mathutils"] = mathutils
    try:
        spec = importlib.util.spec_from_file_location("render_glass_solver_test",
                                                      SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture(scope="module")
def rg():
    return _load_module()


# ---------------------------------------------------------------------------
# _project_points: Projektion mit expliziter Matrix
# ---------------------------------------------------------------------------

def test_project_points_mittelpunkt_und_ecken(rg):
    """Kamera im Ursprung, Blick -Z: Mitte auf (0.5, 0.5), Ecken auf 0/1."""
    matrix = FakeMatrix(((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0),
                         (0, 0, 0, 1)))
    cam_data = FakeCameraData()
    scene = types.SimpleNamespace(render=types.SimpleNamespace(
        resolution_x=2000, resolution_y=2000))

    # Punkt direkt vor der Kamera -> Bildmitte.
    (x, y) = rg._project_points(matrix, cam_data, scene, [(0.0, 0.0, -10.0)])[0]
    assert x == pytest.approx(0.5)
    assert y == pytest.approx(0.5)

    # Frustum-Ecken bei Tiefe 10 in X-Richtung -> x == 0 und x == 1.
    # Halbbreite bei Tiefe t: t * tan(fov_x/2) mit
    # tan(fov_x/2) = (sensor/2)/lens = 18/55.
    half = 10.0 * 18.0 / 55.0
    points = [(-half, 0.0, -10.0), (half, 0.0, -10.0),
              (0.0, -half, -10.0), (0.0, half, -10.0)]
    got = rg._project_points(matrix, cam_data, scene, points)
    assert got[0][0] == pytest.approx(0.0)
    assert got[1][0] == pytest.approx(1.0)
    assert got[2][1] == pytest.approx(0.0)
    assert got[3][1] == pytest.approx(1.0)


def test_project_points_verschobene_kamera(rg):
    """Kamera auf (0, -20, 3) Blick Richtung +Y: Punkt vor ihr bleibt Mitte."""
    cam = FakeCamera()
    cam.location = FakeVector((0.0, -20.0, 3.0))
    quat = FakeVector((0.0, 1.0, 0.0)).to_track_quat("-Z", "Y")
    matrix = FakeMatrix.LocRotScale(cam.location, quat, FakeVector((1, 1, 1)))
    scene = types.SimpleNamespace(render=types.SimpleNamespace(
        resolution_x=2000, resolution_y=2000))
    (x, y) = rg._project_points(matrix, cam.data, scene,
                                [(0.0, -10.0, 3.0)])[0]
    assert x == pytest.approx(0.5)
    assert y == pytest.approx(0.5)


def test_project_points_vertrag_sich_mit_objekt_matrix(rg):
    """Gleiche Matrix wie obj.matrix_world -> keine Exception, endliche Werte."""
    cam = FakeCamera()
    quat = FakeVector((0.0, 1.0, 0.0)).to_track_quat("-Z", "Y")
    cam.matrix_world = FakeMatrix.LocRotScale(
        FakeVector((0.0, -20.0, 3.0)), quat, FakeVector((1, 1, 1)))
    scene = types.SimpleNamespace(render=types.SimpleNamespace(
        resolution_x=2000, resolution_y=2000))
    got = rg._project_points(cam.matrix_world, cam.data, scene,
                             [(0.0, -10.0, 3.0), (50.0, -10.0, 3.0)])
    assert all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in got[:1])
    assert got[1][0] > 1.0  # weit rechts ausserhalb des Bildes


# ---------------------------------------------------------------------------
# fit_camera_with_blender: Konvergenz, Aufziehen, Zeitlimit
# ---------------------------------------------------------------------------

def _fit(rg, cam, content_box, top=6.0, bottom=6.0, deadline_s=30.0,
         focus_xy=(0.0, 0.0), head_z=5.0, feet_z=0.0):
    lo, hi = content_box
    corners = [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1])
               for z in (lo[2], hi[2])]
    return rg.fit_camera_with_blender(
        cam, focus_xy, head_z, feet_z, corners,
        yaw_deg=0.0, top_percent=top, bottom_percent=bottom,
        deadline_s=deadline_s)


def test_fit_konvergiert_schmale_pose(rg):
    """Schmale Pose (wie vorherige Render): Kopf/Fuesse exakt auf 6 %/6 %.

    Die Content-Box schliesst oben mit der Kopfoberkante ab - nur dann ist
    beides gleichzeitig erfuellbar (Inhalt komplett im Bild UND Kopf auf
    6 %). Ragt die Box hoeher (Arme ueber dem Kopf), zieht der Aufzieh-Loop
    die Kamera zurueck und der Kopf landet tiefer als 6 %.
    """
    cam = FakeCamera()
    result = _fit(rg, cam, ((-1, -1, 0), (1, 1, 5.0)))
    assert result["kopf_prozent"] == pytest.approx(6.0, abs=0.2)
    assert result["fuss_prozent"] == pytest.approx(6.0, abs=0.2)
    assert result["distance"] > 0.0
    assert result["projektionen"] > 0
    assert result["dauer_s"] >= 0.0


def test_fit_breite_pose_terminiert_schnell(rg):
    """DER Regressionsfall: breite Emote-Pose (Arme ausgestreckt).

    Frueher loeste der Aufzieh-Loop pro Projektion ein komplettes
    view_layer.update() aus und frass im CI die Render-Zeit (Exit 124).
    Jetzt: reine Mathe-Multiplikationen - der ganze Solver muss in
    deutlich unter einer Sekunde durchlaufen.
    """
    cam = FakeCamera()
    t0 = time.monotonic()
    result = _fit(rg, cam, ((-4.2, -1.5, 0.0), (4.2, 1.5, 4.8)))
    dauer = time.monotonic() - t0
    assert dauer < 2.0, f"Solver zu langsam: {dauer:.2f}s"
    # Kopf und Fuesse bleiben im Bild, Werte sind endlich und plausibel.
    assert math_finite(result["kopf_prozent"])
    assert math_finite(result["fuss_prozent"])
    assert -5.0 <= result["kopf_prozent"] <= 105.0
    assert -5.0 <= result["fuss_prozent"] <= 105.0
    assert result["distance"] > 1.0
    # Aufziehen ist begrenzt: hoechstens 8 Zueh-Iterationen (1.03^9 > 1.25)
    # und dann der definierte Abbruch mit "abgeschnitten" statt Endlosschleife.
    assert result["abgeschnitten"] is True or result["aufgezogen"] is True
    assert result["projektionen"] < 600


def math_finite(value):
    return value == value and abs(value) != float("inf")


def test_fit_sehr_breite_pose_bleibt_endlich(rg):
    """Noch extremer (Liege-/Grätsch-Pose): kein Absturz, keine NaNs."""
    cam = FakeCamera()
    result = _fit(rg, cam, ((-9.0, -1.0, 0.0), (9.0, 1.0, 3.0)))
    assert math_finite(result["kopf_prozent"])
    assert math_finite(result["fuss_prozent"])
    assert math_finite(result["distance"])


def test_fit_zeitlimit_liefert_bestwert(rg):
    """deadline_s=0: sofortiger Abbruch, trotzdem endliche, nutzbare Werte."""
    cam = FakeCamera()
    result = _fit(rg, cam, ((-4.2, -1.5, 0.0), (4.2, 1.5, 4.8)),
                  deadline_s=0.0)
    assert math_finite(result["kopf_prozent"])
    assert math_finite(result["fuss_prozent"])
    assert math_finite(result["distance"])
    assert result["distance"] > 0.0


def test_fit_rueckgabe_enthält_diagnosefelder(rg):
    cam = FakeCamera()
    result = _fit(rg, cam, ((-1, -1, 0), (1, 1, 5.4)))
    assert "projektionen" in result and "dauer_s" in result
    assert result["projektionen"] >= 1


def test_solve_bestwert_bei_singulaerem_jakobi(rg):
    """content_points extrem asymmetrisch: trotzdem kein Schlechtwert."""
    cam = FakeCamera()
    # Kopf == Fuesse (body_span faellt auf den Minimum-Fallback) und
    # Inhalt, der nie in max_scale passt - der Solver muss trotzdem
    # terminieren und sinnvolle Werte liefern.
    result = _fit(rg, cam, ((-6.0, -1.0, 2.0), (6.0, 1.0, 2.4)),
                  head_z=2.2, feet_z=2.2)
    assert math_finite(result["kopf_prozent"])
    assert math_finite(result["distance"])
