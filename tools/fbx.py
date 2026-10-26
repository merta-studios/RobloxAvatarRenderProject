"""Minimaler Leser fuer binaere FBX-Dateien (Version 7.x).

Roblox stellt seine Referenz-Koerper als FBX bereit (z. B. ``ClassicMannequin``
- der klassische Standard-Koerper). Statt die Standard-Teile nachzubauen,
liest die Pipeline sie hier aus der Originaldatei: Modell-Hierarchie,
Transformationen, Vertices, Faces und UVs.

Unterstuetzt wird genau das, was diese Dateien brauchen:

* Knoten-Records mit Properties (``Y C I F D L S R``) und Arrays
  (``f d l i b``, optional zlib-komprimiert),
* ``Objects`` (Model, Geometry, Deformer ...) und ``Connections``,
* lokale Transformationen (``Lcl Translation/Rotation/Scaling``,
  ``PreRotation``, ``RotationOrder``).

Oeffentliche API::

    doc = parse_file("ClassicMannequin.fbx")
    for part in doc.models:            # Weltkoordinaten, Y-up, Studs
        part.name, part.mesh.positions, part.mesh.faces, part.mesh.uvs
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

FBX_MAGIC = b"Kaydara FBX Binary  \x00"

#: Property-Typen mit fester Groesse (ohne Array-Typen).
_SCALAR_SIZES = {"Y": 2, "C": 1, "I": 4, "F": 4, "D": 8, "L": 8}
_ARRAY_TYPES = {"f": ("f", 4), "d": ("d", 8), "l": ("q", 8), "i": ("i", 4), "b": ("B", 1)}


class FbxError(Exception):
    """Datei ist kein lesbares binaeres FBX."""


@dataclass
class Node:
    """Ein Knoten der FBX-Datei."""

    name: str
    props: List[Any] = field(default_factory=list)
    children: List["Node"] = field(default_factory=list)

    def find(self, name: str) -> Optional["Node"]:
        for child in self.children:
            if child.name == name:
                return child
        return None

    def all(self, name: str) -> List["Node"]:
        return [c for c in self.children if c.name == name]

    def path(self) -> str:
        return "/".join(self.name.split("\x00")[0].split("::"))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _read_properties(buf: bytes, offset: int, count: int) -> Tuple[List[Any], int]:
    props: List[Any] = []
    for _ in range(count):
        kind = chr(buf[offset])
        offset += 1
        if kind in _SCALAR_SIZES:
            size = _SCALAR_SIZES[kind]
            raw = buf[offset:offset + size]
            offset += size
            if kind == "Y":
                props.append(struct.unpack("<h", raw)[0])
            elif kind == "C":
                props.append(bool(raw[0]))
            elif kind == "I":
                props.append(struct.unpack("<i", raw)[0])
            elif kind == "F":
                props.append(struct.unpack("<f", raw)[0])
            elif kind == "D":
                props.append(struct.unpack("<d", raw)[0])
            else:
                props.append(struct.unpack("<q", raw)[0])
        elif kind in _ARRAY_TYPES:
            fmt, size = _ARRAY_TYPES[kind]
            length, encoding, comp_len = struct.unpack("<III", buf[offset:offset + 12])
            offset += 12
            if encoding == 1:
                data = zlib.decompress(buf[offset:offset + comp_len])
                offset += comp_len
            else:
                data = buf[offset:offset + length * size]
                offset += length * size
            values = list(struct.unpack(f"<{length}{fmt}", data[:length * size]))
            props.append(values)
        elif kind in ("S", "R"):
            length = struct.unpack("<I", buf[offset:offset + 4])[0]
            offset += 4
            raw = buf[offset:offset + length]
            offset += length
            props.append(raw.decode("utf-8", "replace") if kind == "S" else raw)
        else:  # unbekannt - Datei ist nicht lesbar
            raise FbxError(f"Unbekannter Property-Typ '{kind}' bei Offset {offset}")
    return props, offset


def _parse_node(buf: bytes, offset: int, version: int) -> Tuple[Optional[Node], int]:
    if version >= 7500:
        end_offset, num_props, _props_len, name_len = struct.unpack(
            "<QQQB", buf[offset:offset + 25])
        header = 25
    else:
        end_offset, num_props, _props_len, name_len = struct.unpack(
            "<IIIB", buf[offset:offset + 13])
        header = 13
    if end_offset == 0:
        return None, offset + header
    name = buf[offset + header:offset + header + name_len].decode("utf-8", "replace")
    props, cursor = _read_properties(buf, offset + header + name_len, num_props)
    node = Node(name=name, props=props)
    while cursor < end_offset:
        child, cursor = _parse_node(buf, cursor, version)
        if child is None:
            break
        node.children.append(child)
    return node, end_offset


def parse_bytes(data: bytes) -> Node:
    if not data.startswith(FBX_MAGIC):
        raise FbxError("Kein binaeres FBX (Header fehlt)")
    version = struct.unpack("<I", data[23:27])[0]
    root = Node(name="__root__")
    cursor = 27
    while cursor < len(data):
        node, cursor = _parse_node(data, cursor, version)
        if node is None:
            break
        root.children.append(node)
    root.props = [version]
    return root


def parse_file(path: str) -> Node:
    with open(path, "rb") as fh:
        return parse_bytes(fh.read())


# ---------------------------------------------------------------------------
# Transformationen
# ---------------------------------------------------------------------------

Matrix4 = List[List[float]]
Matrix3 = Tuple[Tuple[float, float, float], Tuple[float, float, float],
                Tuple[float, float, float]]


def _identity() -> Matrix4:
    return [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def _matmul(a: Matrix4, b: Matrix4) -> Matrix4:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)]
            for i in range(4)]


def _rot_x(deg: float) -> Matrix4:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return [[1, 0, 0, 0], [0, c, -s, 0], [0, s, c, 0], [0, 0, 0, 1]]


def _rot_y(deg: float) -> Matrix4:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return [[c, 0, s, 0], [0, 1, 0, 0], [-s, 0, c, 0], [0, 0, 0, 1]]


def _rot_z(deg: float) -> Matrix4:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return [[c, -s, 0, 0], [s, c, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def _euler(order: int, x: float, y: float, z: float) -> Matrix4:
    """Euler-Rotation nach FBX-Konvention (RotationOrder 0..5, XYZ-Standard)."""
    rx, ry, rz = _rot_x(x), _rot_y(y), _rot_z(z)
    if order == 0:      # XYZ
        return _matmul(rx, _matmul(ry, rz))
    if order == 1:      # XZY
        return _matmul(rx, _matmul(rz, ry))
    if order == 2:      # YZX
        return _matmul(ry, _matmul(rz, rx))
    if order == 3:      # YXZ
        return _matmul(ry, _matmul(rx, rz))
    if order == 4:      # ZXY
        return _matmul(rz, _matmul(rx, ry))
    return _matmul(rz, _matmul(ry, rx))  # ZYX


def _property(model: Node, name: str) -> Optional[List[Any]]:
    """Wert einer ``Properties70/P``-Eigenschaft eines Model-Knotens.

    FBX speichert ``Lcl Translation`` & Co. NICHT als direkte Kindknoten,
    sondern unter ``Properties70`` als ``P``-Knoten mit der Werteliste
    ``[Name, Label, Typ, Flags, Werte...]``. Ohne diese Lesart blieben alle
    Model-Transformationen Identity (Marker scheinbar alle auf dem Ursprung).
    """
    props70 = model.find("Properties70")
    if props70 is None:
        return None
    for p in props70.all("P"):
        if p.props and p.props[0] == name:
            return list(p.props)
    return None


def _prop_vec(model: Node, name: str, default: Sequence[float]) -> List[float]:
    raw = _property(model, name)
    if raw is None or len(raw) < 7:
        return list(default)
    try:
        return [float(raw[4]), float(raw[5]), float(raw[6])]
    except (TypeError, ValueError):
        return list(default)


def _prop_scalar(model: Node, name: str, default: float) -> float:
    raw = _property(model, name)
    if raw is None or len(raw) < 5:
        return default
    try:
        return float(raw[4])
    except (TypeError, ValueError):
        return default


def _local_matrix(model: Node) -> Matrix4:
    t = _prop_vec(model, "Lcl Translation", (0.0, 0.0, 0.0))
    r = _prop_vec(model, "Lcl Rotation", (0.0, 0.0, 0.0))
    s = _prop_vec(model, "Lcl Scaling", (1.0, 1.0, 1.0))
    pre = _prop_vec(model, "PreRotation", (0.0, 0.0, 0.0))
    order = int(_prop_scalar(model, "RotationOrder", 0.0))

    # FBX-SDK-Formel (Ausschnitt):  Local = T * Rp * R * S  mit
    # R = Euler(Lcl Rotation), Rp = PreRotation (z. B. Maya-JointOrient).
    m = _matmul(_euler(0, pre[0], pre[1], pre[2]),
                _euler(order, r[0], r[1], r[2]))
    for i in range(3):
        for j in range(3):
            m[i][j] *= s[j]
    m[0][3], m[1][3], m[2][3] = t
    return m


# ---------------------------------------------------------------------------
# Modelle und Meshes
# ---------------------------------------------------------------------------

@dataclass
class Part:
    """Ein Koerperteil in Weltkoordinaten."""

    name: str
    positions: List[Tuple[float, float, float]] = field(default_factory=list)
    faces: List[Tuple[int, int, int]] = field(default_factory=list)
    uvs: List[Tuple[float, float]] = field(default_factory=list)
    #: Positionen der ``*_Att``-Marker (Name -> (Teil-lokale Position, Rotation)).
    attachments: Dict[str, Tuple[Tuple[float, float, float], "Matrix3"]] = \
        field(default_factory=dict)

    def bbox(self) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
        if not self.positions:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        lo = tuple(min(p[i] for p in self.positions) for i in range(3))
        hi = tuple(max(p[i] for p in self.positions) for i in range(3))
        return lo, hi

    def size(self) -> Tuple[float, float, float]:
        lo, hi = self.bbox()
        return tuple(hi[i] - lo[i] for i in range(3))


@dataclass
class Document:
    version: int
    parts: List[Part] = field(default_factory=list)
    #: Alle Model-Knoten (auch leere Marker/Attachments) mit Weltposition.
    markers: Dict[str, Tuple[float, float, float]] = field(default_factory=dict)
    #: Marker mit Welt-Orientierung ((Position, 3x3-Rotation)).
    marker_frames: Dict[str, Tuple[Tuple[float, float, float], Matrix3]] = \
        field(default_factory=dict)

    def part(self, name: str) -> Optional[Part]:
        for p in self.parts:
            if p.name == name:
                return p
        return None


def _clean(name: str) -> str:
    return name.split("\x00")[0].split("::")[0]


def _decode_polygon_index(values: Sequence[int]) -> List[Tuple[int, int, int]]:
    """FBX-Polygon-Indizes (negativer Wert = Ende eines Polygons) triangulieren."""
    faces: List[Tuple[int, int, int]] = []
    poly: List[int] = []
    for raw in values:
        if raw < 0:
            poly.append(-raw - 1)
            if len(poly) >= 3:
                for k in range(1, len(poly) - 1):
                    faces.append((poly[0], poly[k], poly[k + 1]))
            poly = []
        else:
            poly.append(raw)
    if len(poly) >= 3:
        for k in range(1, len(poly) - 1):
            faces.append((poly[0], poly[k], poly[k + 1]))
    return faces


def _uv_per_vertex(faces: List[Tuple[int, int, int]],
                   polygon_uv: Sequence[int], uv_values: Sequence[float],
                   vertex_count: int) -> List[Tuple[float, float]]:
    """UVs je Vertex aufbauen (FBX indiziert UVs je Polygon-Ecke)."""
    uvs: List[Tuple[float, float]] = [(0.0, 0.0)] * vertex_count
    ci = 0
    for fi in range(len(polygon_uv) // 3):
        base = fi * 3
        idx = polygon_uv[base:base + 3]
        for k in range(3):
            if ci >= len(faces):
                break
            vi = faces[ci][k]
            ui = int(idx[k])
            if 0 <= ui * 2 + 1 < len(uv_values) and 0 <= vi < vertex_count:
                uvs[vi] = (float(uv_values[ui * 2]), float(uv_values[ui * 2 + 1]))
        ci += 1
    return uvs


def read_document(path: str) -> Document:
    root = parse_bytes(open(path, "rb").read())
    version = int(root.props[0]) if root.props else 0
    doc = Document(version=version)

    objects = root.find("Objects")
    if objects is None:
        raise FbxError("Keine 'Objects'-Sektion")

    models: Dict[int, Node] = {}
    geometries: Dict[int, Node] = {}
    parents: Dict[int, int] = {}
    for node in objects.children:
        oid = int(node.props[0]) if node.props and isinstance(node.props[0], int) else 0
        if node.name == "Model":
            models[oid] = node
        elif node.name == "Geometry":
            geometries[oid] = node

    connections = root.find("Connections")
    geometry_of: Dict[int, int] = {}
    if connections is not None:
        for conn in connections.children:
            if len(conn.props) >= 3 and conn.props[0] == "OO":
                child, parent = int(conn.props[1]), int(conn.props[2])
                # Wichtig: OO-Connections verbinden auch Skin-Cluster
                # (Deformer) und Geometrien mit Modellen. Fuer die
                # Szenenhierarchie zaehlen nur Model -> Model (oder Root 0) -
                # sonst haengen Gelenke an Deformer-Ketten und alle
                # Welttransformationen stimmen nicht.
                if child in models and (parent == 0 or parent in models):
                    parents[child] = parent
                elif child in geometries and parent in models:
                    geometry_of[parent] = child

    def world_matrix(oid: int) -> Matrix4:
        chain: List[int] = []
        cursor = oid
        seen = set()
        while cursor and cursor not in seen:
            seen.add(cursor)
            chain.append(cursor)
            cursor = parents.get(cursor, 0)
        m = _identity()
        for mid in reversed(chain):
            node = models.get(mid)
            if node is not None:
                m = _matmul(m, _local_matrix(node))
        return m

    def apply(m: Matrix4, p: Sequence[float]) -> Tuple[float, float, float]:
        return (m[0][0] * p[0] + m[0][1] * p[1] + m[0][2] * p[2] + m[0][3],
                m[1][0] * p[0] + m[1][1] * p[1] + m[1][2] * p[2] + m[1][3],
                m[2][0] * p[0] + m[2][1] * p[1] + m[2][2] * p[2] + m[2][3])

    # Geometrien wurden oben ihren Modellen zugeordnet.

    matrices: Dict[int, Matrix4] = {}
    part_by_model: Dict[int, Part] = {}

    def _rot3(m: Matrix4) -> Matrix3:
        return ((m[0][0], m[0][1], m[0][2]),
                (m[1][0], m[1][1], m[1][2]),
                (m[2][0], m[2][1], m[2][2]))

    def _mat3_mul(a: Matrix3, b: Matrix3) -> Matrix3:
        return tuple(
            tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
            for i in range(3))  # type: ignore[return-value]

    def _mat3_t(m: Matrix3) -> Matrix3:
        return ((m[0][0], m[1][0], m[2][0]),
                (m[0][1], m[1][1], m[2][1]),
                (m[0][2], m[1][2], m[2][2]))

    def _mat3_vec(m: Matrix3, v: Tuple[float, float, float]) -> Tuple[float, float, float]:
        return (m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
                m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
                m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2])

    for oid, node in models.items():
        name = _clean(str(node.props[1])) if len(node.props) > 1 else f"Model{oid}"
        matrix = world_matrix(oid)
        matrices[oid] = matrix
        # Welt-Frame fuer ALLE Modelle - auch Marker-Diamanten haben
        # Geometrie und waeren sonst nicht als Marker erkennbar.
        doc.marker_frames[name] = (apply(matrix, (0.0, 0.0, 0.0)), _rot3(matrix))
        geo_id = geometry_of.get(oid)
        if geo_id is None:
            doc.markers[name] = doc.marker_frames[name][0]
            continue
        geo = geometries[geo_id]
        verts_node = geo.find("Vertices")
        index_node = geo.find("PolygonVertexIndex")
        if verts_node is None or index_node is None:
            continue
        raw_verts = list(verts_node.props[0]) if verts_node.props else []
        positions = [apply(matrix, (raw_verts[i], raw_verts[i + 1], raw_verts[i + 2]))
                     for i in range(0, len(raw_verts) - 2, 3)]
        faces = _decode_polygon_index(list(index_node.props[0]) if index_node.props else [])
        uvs: List[Tuple[float, float]] = []
        uv_node = geo.find("LayerElementUV")
        if uv_node is not None:
            uv_values_node = uv_node.find("UV")
            uv_index_node = uv_node.find("UVIndex")
            if uv_values_node is not None and uv_values_node.props:
                uvs = _uv_per_vertex(faces,
                                     list(uv_index_node.props[0]) if uv_index_node and uv_index_node.props else [],
                                     list(uv_values_node.props[0]), len(positions))
        part = Part(name=name, positions=positions, faces=faces, uvs=uvs)
        doc.parts.append(part)
        part_by_model[oid] = part
        doc.markers[name] = apply(matrix, (0.0, 0.0, 0.0))

    # Marker (`*_Att`) dem naechsten geometrischen Vorfahren zuordnen und in
    # Teil-Koordinaten speichern (Position + Orientierung) - so liegen die
    # Attachments exakt dort, wo Roblox sie im zugehoerigen Body-Part annimmt.
    for oid, node in models.items():
        if oid in part_by_model:
            continue
        name = _clean(str(node.props[1])) if len(node.props) > 1 else f"Model{oid}"
        cursor = parents.get(oid, 0)
        seen = set()
        while cursor and cursor not in part_by_model and cursor not in seen:
            seen.add(cursor)
            cursor = parents.get(cursor, 0)
        part = part_by_model.get(cursor)
        if part is None:
            continue
        m_part = matrices[cursor]
        m_mark = matrices[oid]
        rp, rm = _rot3(m_part), _rot3(m_mark)
        local_rot = _mat3_mul(_mat3_t(rp), rm)
        delta = (m_mark[0][3] - m_part[0][3],
                 m_mark[1][3] - m_part[1][3],
                 m_mark[2][3] - m_part[2][3])
        local_pos = _mat3_vec(_mat3_t(rp), delta)
        part.attachments[name] = (local_pos, local_rot)

    return doc
