"""Roblox FileMesh (`.mesh`) Parser und einfache Geometrie-Helfer.

Roblox liefert Mesh-Assets ueber die Asset-Delivery-API **nicht** als OBJ/GLB,
sondern im proprietären FileMesh-Format aus (Version 1.00 bis 7.00, siehe
https://devforum.roblox.com/t/roblox-filemesh-format-specification/326114).
Dieses Modul liest die wichtigsten Versionen und schreibt daraus OBJ-Dateien,
die Blender importieren kann.

Unterstuetzt:
  * version 1.00 / 1.01  (ASCII)
  * version 2.00         (binaer, optional Vertex-Farben)
  * version 3.00 / 3.01  (binaer + LODs)
  * version 4.00 / 4.01  (binaer + Bones/Skinning)
  * version 5.00         (binaer + FACS-Daten am Dateiende)
  * version 6.00 / 7.00  (Chunk-Container; 7.00 zusaetzlich
                          Draco-komprimiert, braucht Paket "DracoPy")

Koordinatensystem: Roblox ist Y-up, rechtshaendig, "vorne" ist -Z. OBJ ist
ebenfalls Y-up, daher werden Positionen und Normalen unveraendert uebernommen.
UV-Koordinaten werden gespiegelt (v -> 1-v), weil Roblox den Ursprung links
oben und OBJ/Blender ihn links unten hat.
"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

Vec3 = Tuple[float, float, float]


class MeshParseError(Exception):
    """Wird geworfen, wenn eine Datei kein lesbares FileMesh ist."""


# ---------------------------------------------------------------------------
# Datenmodell
# ---------------------------------------------------------------------------


@dataclass
class SkinBone:
    """Ein Bone aus der FileMesh-Skinning-Tabelle (Bind-Pose in Mesh-Raum)."""

    name: str
    parent: int
    position: Vec3
    rotation: Tuple[Vec3, Vec3, Vec3]


@dataclass
class SkinningData:
    """Aufgeloeste Linear-Blend-Skinning-Gewichte pro Vertex."""

    bones: List[SkinBone] = field(default_factory=list)
    influences: List[Tuple[Tuple[int, int, int, int], Tuple[float, float, float, float]]] = field(
        default_factory=list
    )


def _scale_skinning(
    skin: Optional[SkinningData],
    sx: float,
    sy: float,
    sz: float,
    tx: float,
    ty: float,
    tz: float,
) -> Optional[SkinningData]:
    """Transformiert die Bind-Posen der Bones wie die Mesh-Vertices."""
    if skin is None:
        return None
    bones: List[SkinBone] = []
    for bone in skin.bones:
        px, py, pz = bone.position
        axes = []
        for axis in bone.rotation:
            ax, ay, az = axis
            scaled = (ax * sx, ay * sy, az * sz)
            length = (scaled[0] ** 2 + scaled[1] ** 2 + scaled[2] ** 2) ** 0.5
            axes.append(
                (1.0, 0.0, 0.0) if length < 1e-9
                else (scaled[0] / length, scaled[1] / length, scaled[2] / length)
            )
        bones.append(SkinBone(
            name=bone.name,
            parent=bone.parent,
            position=(px * sx + tx, py * sy + ty, pz * sz + tz),
            rotation=(axes[0], axes[1], axes[2]),
        ))
    return SkinningData(bones=bones, influences=skin.influences)


def _rotate_skinning(
    skin: Optional[SkinningData],
    apply,
) -> Optional[SkinningData]:
    """Dreht die Bind-Posen der Bones mit derselben Matrix wie das Mesh."""
    if skin is None:
        return None
    bones: List[SkinBone] = []
    for bone in skin.bones:
        bones.append(SkinBone(
            name=bone.name,
            parent=bone.parent,
            position=apply(bone.position),
            rotation=tuple(apply(axis) for axis in bone.rotation),
        ))
    return SkinningData(bones=bones, influences=skin.influences)


@dataclass
class MeshData:
    """Einfache Dreiecks-Geometrie in Roblox-Koordinaten (Y-up)."""

    positions: List[Vec3] = field(default_factory=list)
    normals: List[Vec3] = field(default_factory=list)
    uvs: List[Tuple[float, float]] = field(default_factory=list)
    faces: List[Tuple[int, int, int]] = field(default_factory=list)
    colors: Optional[List[Tuple[int, int, int, int]]] = None
    source_version: str = ""
    skinning: Optional[SkinningData] = None

    # -- Basis-Operationen ---------------------------------------------------

    def __len__(self) -> int:
        return len(self.faces)

    @property
    def is_empty(self) -> bool:
        return not self.faces or not self.positions

    def bbox(self) -> Tuple[Vec3, Vec3]:
        """Achs-aligned Bounding Box als (min, max)."""
        if not self.positions:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        xs = [p[0] for p in self.positions]
        ys = [p[1] for p in self.positions]
        zs = [p[2] for p in self.positions]
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))

    def size(self) -> Vec3:
        lo, hi = self.bbox()
        return (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])

    def center(self) -> Vec3:
        lo, hi = self.bbox()
        return ((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2)

    def transform(
        self,
        scale: Sequence[float] | float = 1.0,
        translate: Sequence[float] = (0.0, 0.0, 0.0),
    ) -> "MeshData":
        """Skaliert (uniform oder pro Achse) und verschiebt das Mesh.

        Skin-Daten (Layered Clothing) werden mit transformiert - sonst
        verliert ein per ``sanity_check`` nachskaliertes Teil seine
        Gewichte und wird starr statt weich an das Rig gelegt.
        """
        if isinstance(scale, (int, float)):
            sx = sy = sz = float(scale)
        else:
            sx, sy, sz = (float(s) for s in scale)
        tx, ty, tz = (float(t) for t in translate)
        return MeshData(
            positions=[(p[0] * sx + tx, p[1] * sy + ty, p[2] * sz + tz) for p in self.positions],
            normals=list(self.normals),
            uvs=list(self.uvs),
            faces=list(self.faces),
            colors=list(self.colors) if self.colors else None,
            source_version=self.source_version,
            skinning=_scale_skinning(self.skinning, sx, sy, sz, tx, ty, tz),
        )

    def rotate(self, matrix) -> "MeshData":
        """Dreht Positionen und Normalen um den Ursprung.

        ``matrix``: 3x3-Rotationsmatrix als Tupel von drei Zeilen (so, wie
        Roblox CFrames speichert: Zeile 0 = R00 R01 R02). Anwendung auf
        Spaltenvektoren: v' = R * v. Wird fuer die CFrame-Rotationen aus
        RBXM-Modellen benutzt (z. B. gedrehte Accessoires).
        """
        if matrix is None:
            return self.transform(1.0)
        try:
            rows = [[float(value) for value in row] for row in matrix]
        except (TypeError, ValueError):
            return self.transform(1.0)
        if len(rows) != 3 or any(len(row) != 3 for row in rows):
            return self.transform(1.0)

        def apply(v):
            return (rows[0][0] * v[0] + rows[0][1] * v[1] + rows[0][2] * v[2],
                    rows[1][0] * v[0] + rows[1][1] * v[1] + rows[1][2] * v[2],
                    rows[2][0] * v[0] + rows[2][1] * v[1] + rows[2][2] * v[2])

        return MeshData(
            positions=[apply(p) for p in self.positions],
            normals=[apply(n) for n in self.normals] if self.normals else [],
            uvs=list(self.uvs),
            faces=list(self.faces),
            colors=list(self.colors) if self.colors else None,
            source_version=self.source_version,
            skinning=_rotate_skinning(self.skinning, apply),
        )

    def flip_v(self) -> "MeshData":
        """Spiegelt die UV-V-Achse (Roblox oben-links -> Blender unten-links)."""
        return MeshData(
            positions=list(self.positions),
            normals=list(self.normals),
            uvs=[(u, 1.0 - v) for (u, v) in self.uvs],
            faces=list(self.faces),
            colors=list(self.colors) if self.colors else None,
            source_version=self.source_version,
            skinning=self.skinning,
        )

    def write_obj(self, path) -> None:
        """Schreibt das Mesh als OBJ (v/vt/vn + f)."""
        has_uv = len(self.uvs) == len(self.positions)
        has_n = len(self.normals) == len(self.positions)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"# Roblox FileMesh {self.source_version} -> OBJ\n")
            fh.write(f"# {len(self.positions)} vertices, {len(self.faces)} faces\n")
            for p in self.positions:
                fh.write(f"v {p[0]:.6f} {p[1]:.6f} {p[2]:.6f}\n")
            if has_uv:
                for uv in self.uvs:
                    fh.write(f"vt {uv[0]:.6f} {uv[1]:.6f}\n")
            if has_n:
                for n in self.normals:
                    fh.write(f"vn {n[0]:.6f} {n[1]:.6f} {n[2]:.6f}\n")
            for a, b, c in self.faces:
                if has_uv and has_n:
                    fh.write(f"f {a+1}/{a+1}/{a+1} {b+1}/{b+1}/{b+1} {c+1}/{c+1}/{c+1}\n")
                elif has_uv:
                    fh.write(f"f {a+1}/{a+1} {b+1}/{b+1} {c+1}/{c+1}\n")
                else:
                    fh.write(f"f {a+1} {b+1} {c+1}\n")


# ---------------------------------------------------------------------------
# Geometrie-Primitive (fuer den Koerper-Rig, siehe avatar_rig.py)
# ---------------------------------------------------------------------------


def make_box(width: float, height: float, depth: float, center: Vec3 = (0, 0, 0)) -> MeshData:
    """Achsen-paralleler Quader in Roblox-Einheiten (Studs)."""
    hw, hh, hd = width / 2.0, height / 2.0, depth / 2.0
    cx, cy, cz = center
    corners = [
        (-hw, -hh, -hd), (hw, -hh, -hd), (hw, hh, -hd), (-hw, hh, -hd),
        (-hw, -hh, hd), (hw, -hh, hd), (hw, hh, hd), (-hw, hh, hd),
    ]
    positions = [(cx + x, cy + y, cz + z) for (x, y, z) in corners]
    # Flach schattierte Normalen: pro Flaeche eigene Vertices
    quads = [
        (4, 5, 6, 7),  # +Z
        (1, 0, 3, 2),  # -Z
        (5, 1, 2, 6),  # +X
        (0, 4, 7, 3),  # -X
        (3, 7, 6, 2),  # +Y
        (0, 1, 5, 4),  # -Y
    ]
    out_pos: List[Vec3] = []
    out_norm: List[Vec3] = []
    out_uv: List[Tuple[float, float]] = []
    faces: List[Tuple[int, int, int]] = []
    quad_uv = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    for idx, quad in enumerate(quads):
        base = len(out_pos)
        for k, vi in enumerate(quad):
            out_pos.append(positions[vi])
            out_uv.append(quad_uv[k])
            out_norm.append(_face_normal(*[positions[quad[j]] for j in range(4)]))
        faces.append((base, base + 1, base + 2))
        faces.append((base, base + 2, base + 3))
        _ = idx
    return MeshData(positions=out_pos, normals=out_norm, uvs=out_uv, faces=faces,
                    source_version="generated-box")


def make_sphere(radius: float = 0.5, segments: int = 24, rings: int = 16,
                center: Vec3 = (0, 0, 0)) -> MeshData:
    """Kugel (UV-Sphaere) - fuer Kopf-Ersatz, Augen, Kugel-Accessoires."""
    cx, cy, cz = center
    positions: List[Vec3] = []
    normals: List[Vec3] = []
    uvs: List[Tuple[float, float]] = []
    faces: List[Tuple[int, int, int]] = []

    for i in range(rings + 1):
        phi = math.pi * i / rings            # 0 .. pi
        for j in range(segments + 1):
            theta = 2 * math.pi * j / segments
            nx = math.sin(phi) * math.cos(theta)
            ny = math.cos(phi)
            nz = math.sin(phi) * math.sin(theta)
            positions.append((cx + nx * radius * 1.0,
                              cy + ny * radius * 1.0,
                              cz + nz * radius * 0.99))
            normals.append((nx, ny, nz))
            uvs.append((j / segments, i / rings))

    for i in range(rings):
        for j in range(segments):
            a = i * (segments + 1) + j
            b = a + segments + 1
            faces.append((a, a + 1, b))
            faces.append((a + 1, b + 1, b))
    return MeshData(positions=positions, normals=normals, uvs=uvs, faces=faces,
                    source_version="generated-sphere")


def _face_normal(a: Vec3, b: Vec3, c: Vec3, d: Vec3 | None = None) -> Vec3:
    if d is not None:
        u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        v = (d[0] - a[0], d[1] - a[1], d[2] - a[2])
    else:
        u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    length = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2) or 1.0
    return (n[0] / length, n[1] / length, n[2] / length)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(rb"^version\s+(\d+)\.(\d+)")


def sniff_version(data: bytes) -> Optional[str]:
    """Gibt die FileMesh-Version zurueck ("1.00", "2.00", ...) oder None."""
    m = _VERSION_RE.match(data[:32])
    if not m:
        return None
    return f"{int(m.group(1))}.{int(m.group(2)):02d}"


def parse(data: bytes) -> MeshData:
    """Parst einen FileMesh-Buffer in MeshData."""
    version = sniff_version(data)
    if version is None:
        raise MeshParseError("Kein FileMesh-Header ('version X.YY') gefunden")
    try:
        mesh = _parse_by_version(data, version)
    except struct.error as exc:
        raise MeshParseError(f"version {version}: Datei ist abgeschnitten ({exc})") from exc
    if not mesh.colors:
        mesh.colors = None
    return mesh.flip_v()


def _parse_by_version(data: bytes, version: str) -> MeshData:
    if version in ("1.00", "1.01"):
        mesh = _parse_v1(data, version)
    elif version == "2.00":
        mesh = _parse_v2(data, version)
    elif version in ("3.00", "3.01"):
        mesh = _parse_v3(data, version)
    elif version in ("4.00", "4.01"):
        mesh = _parse_v4(data, version)
    elif version == "5.00":
        mesh = _parse_v5(data, version)
    elif version in ("6.00", "7.00"):
        mesh = _parse_chunked(data, version)
    else:
        raise MeshParseError(f"Nicht unterstuetzte FileMesh-Version {version}")
    return mesh


def is_filemesh(data: bytes) -> bool:
    return sniff_version(data) is not None


def _parse_v1(data: bytes, version: str) -> MeshData:
    text = data.decode("ascii", errors="ignore")
    lines = text.split("\n")
    if len(lines) < 3:
        raise MeshParseError("version %s: Datei zu kurz" % version)
    try:
        num_faces = int(lines[1].strip())
    except ValueError as exc:
        raise MeshParseError("version %s: num_faces nicht lesbar" % version) from exc
    tuples = re.findall(r"\[([^\]]*)\]", "\n".join(lines[2:]))
    tuples = [tuple(float(x) for x in t.split(",")[:3]) for t in tuples]
    if len(tuples) < num_faces * 9:
        raise MeshParseError("version %s: zu wenige Vertex-Tupel" % version)

    scale = 0.5 if version == "1.00" else 1.0
    positions: List[Vec3] = []
    normals: List[Vec3] = []
    uvs: List[Tuple[float, float]] = []
    faces: List[Tuple[int, int, int]] = []
    for f in range(num_faces):
        base = len(positions)
        for v in range(3):
            pos, nrm, uv = tuples[f * 9 + v * 3: f * 9 + v * 3 + 3]
            positions.append((pos[0] * scale, pos[1] * scale, pos[2] * scale))
            normals.append(nrm)
            uvs.append((uv[0], uv[1]))
        faces.append((base, base + 1, base + 2))
    return MeshData(positions, normals, uvs, faces, None, version)


def _unpack_vertices(buf: bytes, offset: int, count: int, stride: int) -> Tuple[List[Vec3], List[Vec3], List[Tuple[float, float]], Optional[List[Tuple[int, int, int, int]]]]:
    if stride < 36:
        raise MeshParseError(f"Vertex-Stride {stride} ist zu klein")
    need = offset + count * stride
    if len(buf) < need:
        raise MeshParseError("Datei endet mitten in den Vertex-Daten")
    positions: List[Vec3] = []
    normals: List[Vec3] = []
    uvs: List[Tuple[float, float]] = []
    colors: List[Tuple[int, int, int, int]] = []
    has_color = stride >= 40
    for i in range(count):
        base = offset + i * stride
        px, py, pz, nx, ny, nz, tu, tv = struct.unpack_from("<8f", buf, base)
        positions.append((px, py, pz))
        normals.append((nx, ny, nz))
        uvs.append((tu, tv))
        if has_color:
            # Layout: pos(12) + normal(12) + uv(8) + tangent(4) + rgba(4) = 40
            color_off = base + 40 if stride >= 44 else base + 36
            colors.append(struct.unpack_from("<4B", buf, color_off))
        else:
            colors.append((255, 255, 255, 255))
    return positions, normals, uvs, colors


def _read_faces(buf: bytes, offset: int, count: int, nverts: int) -> List[Tuple[int, int, int]]:
    end = offset + count * 12
    if len(buf) < end:
        raise MeshParseError("Datei endet mitten in den Face-Daten")
    raw = struct.unpack_from("<%dI" % (count * 3), buf, offset)
    faces: List[Tuple[int, int, int]] = []
    for i in range(count):
        a, b, c = raw[i * 3: i * 3 + 3]
        if max(a, b, c) >= nverts:  # defekter Index -> Face verwerfen
            continue
        faces.append((a, b, c))
    return faces


def _read_lods(buf: bytes, offset: int, count: int) -> List[int]:
    if count <= 0 or len(buf) < offset + count * 4:
        return []
    return list(struct.unpack_from("<%dI" % count, buf, offset))


def _main_lod_face_count(lods: List[int]) -> Optional[int]:
    """Erstes LOD endet beim zweiten Offset -> Anzahl Faces des Haupt-Meshes."""
    if len(lods) >= 2 and lods[1] > lods[0]:
        return lods[1]
    if lods:
        return lods[0]
    return None


def _parse_v2(data: bytes, version: str) -> MeshData:
    # version 2.00\n = 13 Byte Header-Zeile
    off = 13
    hdr_size, stride, face_size, num_verts, num_faces = struct.unpack_from("<HBBII", data, off)
    if hdr_size != 12:
        raise MeshParseError(f"version 2.00: unerwartete Header-Groesse {hdr_size}")
    off += hdr_size
    positions, normals, uvs, colors = _unpack_vertices(data, off, num_verts, stride or 36)
    off += num_verts * (stride or 36)
    faces = _read_faces(data, off, num_faces, num_verts)
    return MeshData(positions, normals, uvs, faces, colors, version)


def _parse_v3(data: bytes, version: str) -> MeshData:
    off = 13
    hdr_size, stride, face_size, _lod_size, num_lods, num_verts, num_faces = struct.unpack_from(
        "<HBBHHII", data, off)
    off += hdr_size
    positions, normals, uvs, colors = _unpack_vertices(data, off, num_verts, stride or 36)
    off += num_verts * (stride or 36)
    faces = _read_faces(data, off, num_faces, num_verts)
    off += num_faces * 12
    lods = _read_lods(data, off, num_lods)
    main = _main_lod_face_count(lods)
    if main:
        faces = faces[:main]
    return MeshData(positions, normals, uvs, faces, colors, version)


def _parse_skinning_tables(
    skin_bytes: bytes,
    num_verts: int,
    bones_bytes: bytes,
    num_bones: int,
    name_table: bytes,
    subsets_bytes: bytes,
    num_subsets: int,
) -> Optional[SkinningData]:
    """Loest Bone-Tabelle und Subset-Paletten in globale Vertex-Gewichte auf."""
    if num_bones <= 0 or num_verts <= 0:
        return None
    if len(skin_bytes) < num_verts * 8 or len(bones_bytes) < num_bones * 60:
        return None

    bones: List[SkinBone] = []
    for i in range(num_bones):
        base = i * 60
        name_off, parent, _lod_parent, _cull = struct.unpack_from("<Ihhf", bones_bytes, base)
        rot_flat = struct.unpack_from("<9f", bones_bytes, base + 12)
        px, py, pz = struct.unpack_from("<3f", bones_bytes, base + 48)
        end = name_table.find(b"\x00", name_off)
        raw_name = name_table[name_off:end] if end >= 0 else name_table[name_off:]
        bname = raw_name.decode("utf-8", errors="replace")
        rot = (
            (rot_flat[0], rot_flat[1], rot_flat[2]),
            (rot_flat[3], rot_flat[4], rot_flat[5]),
            (rot_flat[6], rot_flat[7], rot_flat[8]),
        )
        bones.append(SkinBone(name=bname, parent=int(parent), position=(px, py, pz), rotation=rot))

    # Standard-Palette (identisch), falls ein Vertex keinem Subset angehoert
    default_palette = tuple(min(k, num_bones - 1) for k in range(26))
    vert_palette: List[Tuple[int, ...]] = [default_palette] * num_verts
    if num_subsets > 0 and len(subsets_bytes) >= num_subsets * 72:
        for s in range(num_subsets):
            sbase = s * 72
            _fb, _fl, vb, vl, nb = struct.unpack_from("<5I", subsets_bytes, sbase)
            palette = struct.unpack_from("<26H", subsets_bytes, sbase + 20)
            pal = tuple(int(idx) if idx < num_bones else 0 for idx in palette)
            end_v = min(num_verts, vb + vl)
            for vi in range(min(num_verts, vb), end_v):
                vert_palette[vi] = pal
            _ = nb

    influences: List[Tuple[Tuple[int, int, int, int], Tuple[float, float, float, float]]] = []
    for vi in range(num_verts):
        off = vi * 8
        si0, si1, si2, si3, w0, w1, w2, w3 = struct.unpack_from("<8B", skin_bytes, off)
        pal = vert_palette[vi]
        bi = (
            pal[si0] if si0 < 26 else 0,
            pal[si1] if si1 < 26 else 0,
            pal[si2] if si2 < 26 else 0,
            pal[si3] if si3 < 26 else 0,
        )
        wsum = float(w0 + w1 + w2 + w3)
        if wsum > 1e-6:
            wf = (w0 / wsum, w1 / wsum, w2 / wsum, w3 / wsum)
        else:
            wf = (1.0, 0.0, 0.0, 0.0)
        influences.append((bi, wf))

    return SkinningData(bones=bones, influences=influences)


def _parse_v4_or_v5(data: bytes, version: str) -> MeshData:
    off = 13
    hdr_size = struct.unpack_from("<H", data, off)[0]
    if hdr_size < 24:
        raise MeshParseError(f"version {version}: Header-Groesse {hdr_size} zu klein")
    (_hdr, _lod_type, num_verts, num_faces, num_lods, num_bones,
     size_bone_names, num_subsets, _hq, _pad) = struct.unpack_from("<HHIIHHIHBB", data, off)
    off += hdr_size

    stride = _guess_stride(data, off, num_verts, num_faces, num_lods, num_bones)
    positions, normals, uvs, colors = _unpack_vertices(data, off, num_verts, stride)
    off += num_verts * stride

    skin_bytes = b""
    if num_bones > 0:
        skin_bytes = data[off:off + num_verts * 8]
        off += num_verts * 8

    faces = _read_faces(data, off, num_faces, num_verts)
    off += num_faces * 12
    lods = _read_lods(data, off, num_lods)
    off += max(num_lods, 0) * 4

    skinning: Optional[SkinningData] = None
    if num_bones > 0:
        bones_end = off + num_bones * 60
        names_end = bones_end + size_bone_names
        subsets_end = names_end + num_subsets * 72
        if subsets_end <= len(data):
            skinning = _parse_skinning_tables(
                skin_bytes,
                num_verts,
                data[off:bones_end],
                num_bones,
                data[bones_end:names_end],
                data[names_end:subsets_end],
                num_subsets,
            )

    main = _main_lod_face_count(lods)
    if main:
        faces = faces[:main]
    return MeshData(positions, normals, uvs, faces, colors, version, skinning=skinning)


def _parse_v4(data: bytes, version: str) -> MeshData:
    """Gilt fuer v4.00 und v4.01 (v6/v7 siehe _parse_chunked)."""
    return _parse_v4_or_v5(data, version)


def _parse_v5(data: bytes, version: str) -> MeshData:
    return _parse_v4_or_v5(data, version)


def _parse_skinning_chunk(payload: bytes) -> Optional[SkinningData]:
    """SKINNING v1 (FileMesh 6.00/7.00) parsen."""
    pos = 0
    if len(payload) < 4:
        return None
    num_verts = struct.unpack_from("<I", payload, pos)[0]
    pos += 4
    if pos + num_verts * 8 + 4 > len(payload):
        return None
    skin_bytes = payload[pos:pos + num_verts * 8]
    pos += num_verts * 8
    num_bones = struct.unpack_from("<I", payload, pos)[0]
    pos += 4
    if pos + num_bones * 60 + 4 > len(payload):
        return None
    bones_bytes = payload[pos:pos + num_bones * 60]
    pos += num_bones * 60
    name_size = struct.unpack_from("<I", payload, pos)[0]
    pos += 4
    if pos + name_size + 4 > len(payload):
        return None
    name_table = payload[pos:pos + name_size]
    pos += name_size
    num_subsets = struct.unpack_from("<I", payload, pos)[0]
    pos += 4
    if pos + num_subsets * 72 > len(payload):
        return None
    subsets_bytes = payload[pos:pos + num_subsets * 72]
    return _parse_skinning_tables(
        skin_bytes, num_verts, bones_bytes, num_bones, name_table, subsets_bytes, num_subsets
    )


def _parse_chunked(data: bytes, version: str) -> MeshData:
    """FileMesh 6.00/7.00: Chunk-Container statt festem Header.

    Nach der Versionszeile folgen Chunks
    ``char name[8] | uint32 chunkVersion | uint32 size | byte data[size]``
    bis zum Dateiende (Spec: devforum "Roblox FileMesh Format
    Specification", Revision 3/30/2025). "COREMESH" (Geometrie), "LODS"
    (Face-Bereiche) und "SKINNING" (Linear Blend Skinning) werden ausgewertet.
    """
    off = 13  # "version 6.00\n" / "version 7.00\n"
    coremesh = None
    coremesh_version = 0
    lod_offsets: List[int] = []
    skinning_payload: Optional[bytes] = None
    while off + 16 <= len(data):
        raw_name = data[off:off + 8]
        name = raw_name.rstrip(b"\x00 ").decode("ascii", "replace")
        chunk_version, size = struct.unpack_from("<II", data, off + 8)
        off += 16
        payload = data[off:off + size]
        if len(payload) < size:
            raise MeshParseError(
                f"version {version}: Chunk '{name}' ist abgeschnitten")
        off += size
        if name == "COREMESH":
            coremesh, coremesh_version = payload, chunk_version
        elif name == "LODS":
            lod_offsets = _parse_lods_chunk(payload)
        elif name == "SKINNING":
            skinning_payload = payload
    if coremesh is None:
        raise MeshParseError(f"version {version}: kein COREMESH-Chunk")

    if coremesh_version >= 2:
        mesh = _parse_coremesh_draco(coremesh, version)
    else:
        mesh = _parse_coremesh_v1(coremesh, version)
    if skinning_payload is not None:
        skin = _parse_skinning_chunk(skinning_payload)
        if skin is not None and len(skin.influences) == len(mesh.positions):
            mesh.skinning = skin
    main = _main_lod_face_count(lod_offsets)
    if main:
        mesh.faces = mesh.faces[:main]
    return mesh


def _parse_lods_chunk(payload: bytes) -> List[int]:
    """LODS v1: ushort lodType | byte numHQ | uint count | uint offsets[]."""
    for header in (7, 8):  # 8 = mit Alignment-Byte vor dem count
        if len(payload) < header:
            continue
        count = struct.unpack_from("<I", payload, header - 4)[0]
        if header + count * 4 == len(payload) and count < 10000:
            return list(struct.unpack_from("<%dI" % count, payload, header))
    return []


def _parse_coremesh_v1(payload: bytes, version: str) -> MeshData:
    """COREMESH v1 (FileMesh 6.00): uint numVerts, Verts, uint numFaces, Faces."""
    if len(payload) < 8:
        raise MeshParseError(f"version {version}: COREMESH zu kurz")
    num_verts = struct.unpack_from("<I", payload, 0)[0]
    if num_verts <= 0:
        raise MeshParseError(f"version {version}: Mesh ohne Vertices")
    for stride in (40, 36):
        faces_off = 4 + num_verts * stride
        if faces_off + 4 > len(payload):
            continue
        num_faces = struct.unpack_from("<I", payload, faces_off)[0]
        if faces_off + 4 + num_faces * 12 == len(payload):
            positions, normals, uvs, colors = _unpack_vertices(
                payload, 4, num_verts, stride)
            faces = _read_faces(payload, faces_off + 4, num_faces, num_verts)
            return MeshData(positions, normals, uvs, faces, colors, version)
    raise MeshParseError(
        f"version {version}: COREMESH-Groesse passt zu keiner Vertex-Stride "
        f"(36/40) bei {num_verts} Vertices")


def _parse_coremesh_draco(payload: bytes, version: str) -> MeshData:
    """COREMESH v2 (FileMesh 7.00): uint dracoSize + Draco-Bitstrom."""
    if len(payload) < 4:
        raise MeshParseError(f"version {version}: COREMESH zu kurz")
    size = struct.unpack_from("<I", payload, 0)[0]
    stream = payload[4:4 + size]
    if len(stream) < size:
        raise MeshParseError(f"version {version}: Draco-Strom abgeschnitten")
    try:
        import DracoPy
    except ImportError as exc:
        raise MeshParseError(
            "FileMesh 7.00 ist Draco-komprimiert - bitte Paket 'DracoPy' "
            "installieren (pip install DracoPy)") from exc
    try:
        decoded = DracoPy.decode_buffer_to_mesh(bytes(stream))
    except Exception as exc:  # noqa: BLE001 - fremde Exception-Typen kapseln
        raise MeshParseError(f"Draco-Dekompression fehlgeschlagen: {exc}") from exc

    positions = [tuple(float(v) for v in p) for p in decoded.points]
    faces = [tuple(int(i) for i in f) for f in decoded.faces]
    raw_normals = getattr(decoded, "normals", None)
    if raw_normals is not None and len(raw_normals) == len(positions):
        normals = [tuple(float(v) for v in n) for n in raw_normals]
    else:
        normals = []
    raw_uv = getattr(decoded, "tex_coord", None)
    if raw_uv is not None and len(raw_uv) == len(positions):
        uvs = [(float(u), float(v)) for u, v in raw_uv]
    else:
        uvs = [(0.0, 0.0)] * len(positions)
    raw_colors = getattr(decoded, "colors", None)
    colors = None
    if raw_colors is not None and len(raw_colors) == len(positions):
        colors = []
        for c in raw_colors:
            rgba = tuple(int(v) for v in c)
            colors.append(rgba if len(rgba) == 4 else rgba + (255,))
    if not normals:
        normals = _smooth_normals(positions, faces)
    return MeshData(positions, normals, uvs, faces, colors, version)


def _smooth_normals(positions, faces):
    """Glatte Vertex-Normalen aus den Face-Normalen mitteln."""
    acc = [[0.0, 0.0, 0.0] for _ in positions]
    for a, b, c in faces:
        if max(a, b, c) >= len(positions):
            continue
        n = _face_normal(positions[a], positions[b], positions[c])
        for index in (a, b, c):
            acc[index][0] += n[0]
            acc[index][1] += n[1]
            acc[index][2] += n[2]
    normals = []
    for x, y, z in acc:
        length = math.sqrt(x * x + y * y + z * z)
        if length < 1e-12:
            normals.append((0.0, 1.0, 0.0))
        else:
            normals.append((x / length, y / length, z / length))
    return normals


def smooth_normals_by_position(mesh: MeshData,
                               tolerance: float = 1e-5) -> MeshData:
    """Berechnet geglaettete Normalen ueber positionsgleiche Vertices hinweg.

    Roblox-Meshes duplizieren Vertices haeufig an UV- und Materialnahten.
    Werden nur die Normalen pro Index gemittelt, bleiben diese Kanten trotz
    ``shade_smooth`` sichtbar facettiert. Hier werden Face-Normalen daher
    zunaechst fuer alle Vertices an derselben Position aufsummiert (gewichtet
    mit der Dreiecksflaeche) und anschliessend auf jeden Duplikat-Vertex
    zurueckgeschrieben. UVs, Faces und Farben bleiben unveraendert.
    """
    if mesh.is_empty:
        return mesh
    quant = max(1e-9, float(tolerance))
    keys = [tuple(round(float(value) / quant) for value in point)
            for point in mesh.positions]
    accumulated: Dict[Tuple[int, int, int], List[float]] = {}

    for a, b, c in mesh.faces:
        if min(a, b, c) < 0 or max(a, b, c) >= len(mesh.positions):
            continue
        pa, pb, pc = mesh.positions[a], mesh.positions[b], mesh.positions[c]
        ab = (pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2])
        ac = (pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2])
        face_normal = (ab[1] * ac[2] - ab[2] * ac[1],
                       ab[2] * ac[0] - ab[0] * ac[2],
                       ab[0] * ac[1] - ab[1] * ac[0])
        for index in (a, b, c):
            target = accumulated.setdefault(keys[index], [0.0, 0.0, 0.0])
            for axis in range(3):
                target[axis] += face_normal[axis]

    normals: List[Vec3] = []
    for key in keys:
        nx, ny, nz = accumulated.get(key, (0.0, 1.0, 0.0))
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        normals.append((nx / length, ny / length, nz / length)
                       if length > 1e-12 else (0.0, 1.0, 0.0))
    return MeshData(
        positions=list(mesh.positions),
        normals=normals,
        uvs=list(mesh.uvs),
        faces=list(mesh.faces),
        colors=list(mesh.colors) if mesh.colors else None,
        source_version=mesh.source_version,
        skinning=mesh.skinning,
    )


def stack_segments(items, source_version: str = "stacked",
                   gap: float = 0.012) -> MeshData:
    """Setzt R15-Segmente lueckenlos und senkrecht zu einem Glied zusammen.

    ``items``: Folge aus ``(MeshData, size|None)`` - oder
    ``(MeshData, size|None, position|None[, rotation])`` wie bei
    :func:`merge_meshes`; Position und Rotation werden hier **bewusst
    ignoriert**. Reihenfolge: von oben nach unten (Obersegment zuerst,
    z. B. UpperTorso, LowerTorso bzw. UpperArm, LowerArm, Hand).

    Warum nicht die RBXM-Positionen? Die CFrames der Bundles beschreiben
    die *Bind-Pose* (A-Pose: Arme schraeg nach aussen, Diagonalstufen wie
    0,85 -> 1,20 -> 1,48 Studs). Wer diese Positionen uebernimmt und das
    Ergebnis in die senkrechte Rig-Box presst, bekommt verrenkte,
    auseinanderdriftende Teile - genau der Position/Rotation-Bug. In der
    Standard-Stand-Pose stehen alle Segmente achsenparallell übereinander;
    dafuer wird hier gestapelt: jede Segment-Box exakt auf ihre
    RBXM-Groesse skaliert, Oberkante des naechsten an Unterkante des
    vorherigen (``gap`` = minimale Luft, damit im Glasrender keine
    Flaechen exakt aufeinander liegen - wie in build_body).

    Danach reicht :func:`avatar_rig.fit_mesh_to_part`, um den Stapel in
    die Rig-Box des Koerperteils einzuordnen (Endposition und Gesamt-
    groesse stimmen dann exakt).
    """
    positions: List[Vec3] = []
    normals: List[Vec3] = []
    uvs: List[Tuple[float, float]] = []
    faces: List[Tuple[int, int, int]] = []
    colors: List[Tuple[int, int, int, int]] = []
    all_colors = True
    top = 0.0  # Unterkante des zuletzt gesetzten Segments (Start: 0)
    first = True
    for item in items:
        mesh = item[0]
        size = item[1] if len(item) > 1 else None
        part = mesh
        if size:
            # Gleichmaessig auf die RBXM-Hoehe skalieren (nicht achsweise!):
            # Mesh-Einheiten und Part-Groesse stimmen bei manchen Bundles
            # nicht exakt ueberein; eine Pro-Achsen-Skalierung hat die
            # Segmente sichtbar in die Laenge/Breite gezogen.
            current = mesh.size()
            if current[1] > 1e-9 and float(size[1]) > 0:
                factor = float(size[1]) / current[1]
            else:
                factor = max(float(v) for v in size) / (max(current) or 1.0)
            part = mesh.transform(factor)
        height = part.size()[1]
        center = part.center()
        if first:
            target_top = 0.0
            first = False
        else:
            target_top = top - gap
        # Zentrum so setzen: Oberkante liegt auf target_top, x/z mittig.
        part = part.transform(
            1.0, (-center[0], target_top - (center[1] + height / 2.0),
                  -center[2]))
        top = target_top - height
        base = len(positions)
        positions.extend(part.positions)
        normals.extend(part.normals or [(0.0, 1.0, 0.0)] * len(part.positions))
        uvs.extend(part.uvs or [(0.0, 0.0)] * len(part.positions))
        faces.extend((a + base, b + base, c_ + base) for a, b, c_ in part.faces)
        if part.colors:
            colors.extend(part.colors)
        else:
            all_colors = False
    return MeshData(positions, normals, uvs, faces,
                    colors if all_colors and colors else None, source_version)


def merge_meshes(items, source_version: str = "merged") -> MeshData:
    """Mehrere Meshes (z. B. R15-Segmente eines RBXM) zu einem verbinden.

    ``items``: Liste aus ``(MeshData, size|None, position|None)``. Jedes
    Mesh wird pro Achse auf ``size`` gebracht (falls gegeben) und so
    verschoben, dass sein Box-Zentrum auf ``position`` liegt (RBXM:
    CFrame-Position; Drehungen werden ignoriert - Naeherung fuer die
    aufrechte Standardpose).
    """
    positions: List[Vec3] = []
    normals: List[Vec3] = []
    uvs: List[Tuple[float, float]] = []
    faces: List[Tuple[int, int, int]] = []
    colors: List[Tuple[int, int, int, int]] = []
    all_colors = True
    for mesh, size, position in items:
        part = mesh
        if size:
            current = mesh.size()
            factors = tuple(
                float(size[i]) / current[i] if current[i] > 1e-9 else 1.0
                for i in range(3))
            part = mesh.transform(factors)
        if position is not None:
            c = part.center()
            part = part.transform(
                1.0, (position[0] - c[0], position[1] - c[1],
                      position[2] - c[2]))
        base = len(positions)
        positions.extend(part.positions)
        normals.extend(part.normals or [(0.0, 1.0, 0.0)] * len(part.positions))
        uvs.extend(part.uvs or [(0.0, 0.0)] * len(part.positions))
        faces.extend((a + base, b + base, c_ + base) for a, b, c_ in part.faces)
        if part.colors:
            colors.extend(part.colors)
        else:
            all_colors = False
    return MeshData(positions, normals, uvs, faces,
                    colors if all_colors and colors else None, source_version)


def _guess_stride(data: bytes, off: int, num_verts: int, num_faces: int,
                  num_lods: int, num_bones: int) -> int:
    """v4+ speichert keine Vertex-Groesse -> aus der Dateigroesse ableiten."""
    if num_verts <= 0:
        raise MeshParseError("Mesh ohne Vertices")
    skin = num_verts * 8 if num_bones > 0 else 0
    tail = num_faces * 12 + max(num_lods, 0) * 4
    remaining = len(data) - off - skin - tail
    if remaining < num_verts * 36:
        raise MeshParseError("Datei zu kurz fuer die angegebene Vertex-Anzahl")
    stride = remaining // num_verts
    # Auf bekannte Werte runden (36 = ohne, 40 = mit Farben, 44 = extra)
    for candidate in (36, 40, 44, 48):
        if abs(stride - candidate) <= 1:
            return candidate
    return 36 if stride < 38 else 40


_PRIMARY_CHILD_BONE = {
    "LowerTorso": "UpperTorso",
    "UpperTorso": "Head",
    "RightUpperArm": "RightLowerArm",
    "RightLowerArm": "RightHand",
    "LeftUpperArm": "LeftLowerArm",
    "LeftLowerArm": "LeftHand",
    "RightUpperLeg": "RightLowerLeg",
    "RightLowerLeg": "RightFoot",
    "LeftUpperLeg": "LeftLowerLeg",
    "LeftLowerLeg": "LeftFoot",
}


def rotation_between(vec_from: Vec3, vec_to: Vec3) -> Tuple[Vec3, Vec3, Vec3]:
    """Kuerzester-Bogen-Rotationsmatrix R in SO(3) mit R * norm(vec_from) = norm(vec_to)."""
    ax, ay, az = vec_from
    bx, by, bz = vec_to
    la = math.sqrt(ax * ax + ay * ay + az * az)
    lb = math.sqrt(bx * bx + by * by + bz * bz)
    if la < 1e-9 or lb < 1e-9:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    ax, ay, az = ax / la, ay / la, az / la
    bx, by, bz = bx / lb, by / lb, bz / lb
    dot = max(-1.0, min(1.0, ax * bx + ay * by + az * bz))
    if dot > 1.0 - 1e-8:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    if dot < -1.0 + 1e-8:
        ux, uy, uz = (-ay, ax, 0.0) if (abs(ax) > 0.5 or abs(ay) > 0.5) else (0.0, -az, ay)
        ul = math.sqrt(ux * ux + uy * uy + uz * uz) or 1.0
        ux, uy, uz = ux / ul, uy / ul, uz / ul
        return (
            (2 * ux * ux - 1, 2 * ux * uy, 2 * ux * uz),
            (2 * uy * ux, 2 * uy * uy - 1, 2 * uy * uz),
            (2 * uz * ux, 2 * uz * uy, 2 * uz * uz - 1),
        )
    # Rodrigues: v = a x b, R = I + [v]_x + [v]_x^2 / (1 + dot)
    vx = ay * bz - az * by
    vy = az * bx - ax * bz
    vz = ax * by - ay * bx
    k = 1.0 / (1.0 + dot)
    return (
        (1.0 - (vy * vy + vz * vz) * k, -vz + vx * vy * k, vy + vx * vz * k),
        (vz + vx * vy * k, 1.0 - (vx * vx + vz * vz) * k, -vx + vy * vz * k),
        (-vy + vx * vz * k, vx + vy * vz * k, 1.0 - (vx * vx + vy * vy) * k),
    )


def _mat3_vec(mat: Tuple[Vec3, Vec3, Vec3], v: Vec3) -> Vec3:
    return (
        mat[0][0] * v[0] + mat[0][1] * v[1] + mat[0][2] * v[2],
        mat[1][0] * v[0] + mat[1][1] * v[1] + mat[1][2] * v[2],
        mat[2][0] * v[0] + mat[2][1] * v[1] + mat[2][2] * v[2],
    )


def skin_mesh_to_rig(
    mesh: MeshData,
    rig_joints: dict[str, Vec3],
    normal_offset: float = 0.008,
) -> Optional[MeshData]:
    """Verformt ein geskinntes Layered-Clothing-Mesh per Linear Blend Skinning auf das Ziel-Rig.

    ``rig_joints``: Welt-Positionen der R15-Gelenke (z. B. ``RightUpperArm`` -> Schultergelenk,
    ``RightLowerArm`` -> Ellbogengelenk, ``RightHand`` -> Handgelenk, ``LowerTorso``, ``UpperTorso``,
    ``Head`` usw.). Gibt ``None`` zurueck, wenn das Mesh keine nutzbaren Skinning-Gewichte besitzt.
    """
    skin = mesh.skinning
    if skin is None or not skin.bones or len(skin.influences) != len(mesh.positions):
        return None

    bind_by_name = {b.name: b for b in skin.bones}
    matched_r15 = [b.name for b in skin.bones if b.name in rig_joints]
    if not matched_r15:
        return None

    identity: Tuple[Vec3, Vec3, Vec3] = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    bone_rot: List[Optional[Tuple[Vec3, Vec3, Vec3]]] = [None] * len(skin.bones)
    bone_scale: List[float] = [1.0] * len(skin.bones)
    bone_world: List[Optional[Vec3]] = [None] * len(skin.bones)

    def resolve_bone(idx: int, depth: int = 0) -> None:
        if bone_world[idx] is not None or depth > 32:
            return
        b = skin.bones[idx]
        p_idx = b.parent if 0 <= b.parent < len(skin.bones) and b.parent != idx else -1
        if p_idx >= 0:
            resolve_bone(p_idx, depth + 1)

        if b.name in rig_joints:
            pw = rig_joints[b.name]
            child_name = _PRIMARY_CHILD_BONE.get(b.name)
            if child_name and child_name in bind_by_name and child_name in rig_joints:
                cb = bind_by_name[child_name].position
                cw = rig_joints[child_name]
                db = (cb[0] - b.position[0], cb[1] - b.position[1], cb[2] - b.position[2])
                dw = (cw[0] - pw[0], cw[1] - pw[1], cw[2] - pw[2])
                lb = math.sqrt(db[0] ** 2 + db[1] ** 2 + db[2] ** 2)
                lw = math.sqrt(dw[0] ** 2 + dw[1] ** 2 + dw[2] ** 2)
                if lb > 1e-4 and lw > 1e-4:
                    bone_rot[idx] = rotation_between(db, dw)
                    bone_scale[idx] = max(0.4, min(2.5, lw / lb))
            if bone_rot[idx] is None:
                if p_idx >= 0 and bone_rot[p_idx] is not None:
                    bone_rot[idx] = bone_rot[p_idx]
                    bone_scale[idx] = bone_scale[p_idx]
                else:
                    bone_rot[idx] = identity
                    bone_scale[idx] = 1.0
            bone_world[idx] = pw
        elif p_idx >= 0 and bone_world[p_idx] is not None and bone_rot[p_idx] is not None:
            pr = bone_rot[p_idx]
            ps = bone_scale[p_idx]
            pp = skin.bones[p_idx].position
            pw_parent = bone_world[p_idx]
            assert pr is not None and pw_parent is not None
            rel = (b.position[0] - pp[0], b.position[1] - pp[1], b.position[2] - pp[2])
            rrel = _mat3_vec(pr, rel)
            bone_rot[idx] = pr
            bone_scale[idx] = ps
            bone_world[idx] = (
                pw_parent[0] + ps * rrel[0],
                pw_parent[1] + ps * rrel[1],
                pw_parent[2] + ps * rrel[2],
            )
        else:
            bone_rot[idx] = identity
            bone_scale[idx] = 1.0
            bone_world[idx] = b.position

    for i in range(len(skin.bones)):
        resolve_bone(i)

    out_pos: List[Vec3] = []
    out_nrm: List[Vec3] = []
    has_normals = len(mesh.normals) == len(mesh.positions)

    for vi, pos in enumerate(mesh.positions):
        bi, wf = skin.influences[vi]
        px = py = pz = 0.0
        nx = ny = nz = 0.0
        nrm = mesh.normals[vi] if has_normals else (0.0, 1.0, 0.0)
        for k in range(4):
            w = wf[k]
            if w <= 1e-6:
                continue
            b_i = bi[k]
            bp = skin.bones[b_i].position
            rot = bone_rot[b_i] or identity
            sc = bone_scale[b_i]
            bw = bone_world[b_i] or bp
            rel = (pos[0] - bp[0], pos[1] - bp[1], pos[2] - bp[2])
            rrel = _mat3_vec(rot, rel)
            px += w * (bw[0] + sc * rrel[0])
            py += w * (bw[1] + sc * rrel[1])
            pz += w * (bw[2] + sc * rrel[2])
            if has_normals:
                rn = _mat3_vec(rot, nrm)
                nx += w * rn[0]
                ny += w * rn[1]
                nz += w * rn[2]
        if has_normals:
            nl = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            nx, ny, nz = nx / nl, ny / nl, nz / nl
            if normal_offset:
                px += normal_offset * nx
                py += normal_offset * ny
                pz += normal_offset * nz
            out_nrm.append((nx, ny, nz))
        out_pos.append((px, py, pz))

    return MeshData(
        positions=out_pos,
        normals=out_nrm if has_normals else [],
        uvs=list(mesh.uvs),
        faces=list(mesh.faces),
        colors=list(mesh.colors) if mesh.colors else None,
        source_version=mesh.source_version,
        skinning=None,
    )

if __name__ == "__main__":  # pragma: no cover
    import sys

    if len(sys.argv) < 3:
        print("Aufruf: python3 tools/roblox_mesh.py <input.mesh> <output.obj>")
        raise SystemExit(2)
    raw = open(sys.argv[1], "rb").read()
    m = parse(raw)
    m.write_obj(sys.argv[2])
    print(f"OK: {sys.argv[1]} ({m.source_version}): {len(m.positions)} Vertices, "
          f"{len(m.faces)} Faces, Groesse {tuple(round(v, 3) for v in m.size())} Studs")
