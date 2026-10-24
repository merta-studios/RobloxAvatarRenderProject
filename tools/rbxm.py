"""RBXM-/RBXMX-Parser: extrahiert Mesh-/Textur-IDs aus Roblox-Modellen.

Roblox liefert Accessoires **und** die getragenen Koerperteile (AssetTypen
27-31) ueber die Asset Delivery nicht als `.mesh`-Datei, sondern als
RBXM-Modell (binaer) bzw. RBXMX (XML). Die eigentliche FileMesh-ID steckt
erst im Modell (`MeshPart.MeshId`, `SpecialMesh.MeshId`, ...), Texturen
analog (`TextureID`/`TextureId`, `SurfaceAppearance.ColorMap`,
`Decal.Texture`, ...). Dieses Modul liest nur genau diese Angaben aus -
es ist bewusst **kein** vollstaendiger DOM-Parser:

* `SSTR` (SharedStrings), `INST` (Klassen/Instanzen) und `PRNT`
  (Hierarchie) werden gelesen.
* Von den `PROP`-Chunks werden nur die fuer uns relevanten Properties
  dekodiert (`Name`, `MeshId`, Textur-Properties, `size`, `CFrame`,
  `Scale`/`Offset`). Alle anderen Chunks werden anhand ihrer Laenge im
  Chunk-Header uebersprungen - deshalb muss nicht jeder der ~35
  Roblox-Datentypen implementiert sein.
* String-Properties werden in allen drei gaengigen Kodierungen gelesen:
  `String` (0x01), `SharedString` (0x1C) und der neue `Content`-Typ (0x22).
* Chunk-Kompression: ZSTD (Magic `28 B5 2F FD`), LZ4-Block (4 Byte
  Laenge + Block, Roblox-Variante) und als Fallback rohes Deflate.

Format-Referenz: "Roblox Binary Model Format" (rbx-dom, dom.rojo.space).

Oeffentliche API:

    extract_assets(data) -> [{"class", "name", "mesh_id", "texture_id",
                              "size", "position"}, ...]
    parse_instances(data) -> [Wurzel-Instanz, ...]   # kompletter Baum (Emotes)

Abhaengigkeiten: `zstandard` und `lz4` sind optional. Fehlt das fuer eine
Datei noetige Modul, wird ein verstaendlicher `RbxmError` geworfen statt
eines Tracebacks. Kein Netzwerk noetig.
"""

from __future__ import annotations

import base64
import io
import re
import struct
import xml.etree.ElementTree as ET
import zlib
from typing import Any, Dict, List, Optional, Tuple

try:  # optionale Drittanbieter-Module (im Workflow per pip installiert)
    import lz4.block as _lz4_block
except ImportError:  # pragma: no cover - abhaengig von der Umgebung
    _lz4_block = None  # type: ignore[assignment]

try:
    import zstandard as _zstandard
except ImportError:  # pragma: no cover - abhaengig von der Umgebung
    _zstandard = None  # type: ignore[assignment]

MAGIC = b"<roblox!\x89\xff\r\n\x1a\n"
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# PROP-Datentyp-IDs (nur die, die wir dekodieren)
_T_STRING = 0x01
_T_BOOL = 0x02
_T_INT32 = 0x03
_T_FLOAT32 = 0x04
_T_FLOAT64 = 0x05
_T_VECTOR3 = 0x0E
_T_CFRAME = 0x10
_T_ENUM = 0x12
_T_SHARED_STRING = 0x1C
_T_PROTECTED_STRING = 0x1D
_T_CONTENT = 0x22

Vec3 = Tuple[float, float, float]
Matrix3 = Tuple[Vec3, Vec3, Vec3]


class RbxmError(Exception):
    """Sauberer Fehler fuer unleserliche RBXM-/RBXMX-Daten."""


# ---------------------------------------------------------------------------
# Asset-ID parsen
# ---------------------------------------------------------------------------

_ASSET_ID_PATTERNS = (
    re.compile(r"rbxassetid://(\d+)"),
    re.compile(r"[?&]id=(\d+)"),
)


def parse_asset_id(value: Any) -> Optional[int]:
    """`rbxassetid://123` / `http://...?id=123` / `123` -> 123, sonst None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        return number if number > 0 else None
    for pattern in _ASSET_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            number = int(match.group(1))
            return number if number > 0 else None
    return None


def is_rbxm(data: bytes) -> bool:
    """True, wenn die Bytes wie RBXM (binaer) oder RBXMX (XML) aussehen."""
    if data[:len(MAGIC)] == MAGIC:
        return True
    head = data[:512].lstrip(b"\xef\xbb\xbf \t\r\n")
    return head[:7].lower() == b"<roblox" or (
        head[:5] == b"<?xml" and b"<roblox" in data[:2048].lower()
    )


# ---------------------------------------------------------------------------
# Little-Endian-Reader mit Bounds-Checks (wirft RbxmError statt struct.error)
# ---------------------------------------------------------------------------


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def read(self, count: int) -> bytes:
        if count < 0 or self.pos + count > len(self.data):
            raise RbxmError("RBXM-Datei ist abgeschnitten (unerwartetes Dateiende)")
        chunk = self.data[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def skip(self, count: int) -> None:
        self.read(count)

    def u8(self) -> int:
        return self.read(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def string(self) -> str:
        length = self.u32()
        return self.read(length).decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Roblox-Transformationen (Byte-Interleaving, Zigzag, Float-Rotation)
# ---------------------------------------------------------------------------


def _deinterleave_u32(buf: bytes, count: int) -> List[int]:
    """Big-Endian-Byte-Planes wieder zu u32-Werten zusammensetzen."""
    if len(buf) < 4 * count:
        raise RbxmError("RBXM-Datei ist abgeschnitten (interleaved Array zu kurz)")
    return [
        (buf[i] << 24) | (buf[count + i] << 16) | (buf[2 * count + i] << 8) | buf[3 * count + i]
        for i in range(count)
    ]


def _untransform_i32(values: List[int]) -> List[int]:
    """Zigzag-Dekodierung: (x >> 1) ^ -(x & 1)."""
    return [((v >> 1) ^ -(v & 1)) for v in values]


def _read_i32_array(reader: _Reader, count: int) -> List[int]:
    """Transformiertes, interleaved Int32-Array (z. B. Referenten-Grundlage)."""
    return _untransform_i32(_deinterleave_u32(reader.read(4 * count), count))


def _read_referents(reader: _Reader, count: int) -> List[int]:
    """Referenten: Int32-Array, kumulativ (Deltas aufsummieren)."""
    out: List[int] = []
    total = 0
    for delta in _read_i32_array(reader, count):
        total += delta
        out.append(total)
    return out


def _read_f32_array(reader: _Reader, count: int) -> List[float]:
    """Interleaved Float32 im Roblox-Format (Bits um 1 nach links rotiert)."""
    floats: List[float] = []
    for bits in _deinterleave_u32(reader.read(4 * count), count):
        ieee = ((bits >> 1) | (bits << 31)) & 0xFFFFFFFF  # Rotation rueckgaengig
        floats.append(struct.unpack("<f", struct.pack("<I", ieee))[0])
    return floats


def _read_vector3_array(reader: _Reader, count: int) -> List[Vec3]:
    xs = _read_f32_array(reader, count)
    ys = _read_f32_array(reader, count)
    zs = _read_f32_array(reader, count)
    return list(zip(xs, ys, zs))


# 24 Sonder-Rotationen (Spec: "Roblox Binary Model Format", CFrame).
# ID 0x00 bedeutet: 9 rohe Float32 (Matrix R00..R22) folgen; alle anderen
# IDs sind vordefinierte Drehungen - KEINE Identitaet (nur 0x02 ist
# identisch). Wer diese Tabelle ignoriert, setzt Fuesse/Haende/Huete aus
# Bundle-Modellen an falscher Position und mit falscher Rotation zusammen.
# Quelle: rbx-dom (rbx_types::Matrix3::from_basic_rotation_id), zeilenweise.
_BASIC_ROTATIONS: Dict[int, Matrix3] = {
    0x02: ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    0x03: ((1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),
    0x05: ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0)),
    0x06: ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0)),
    0x07: ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
    0x09: ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    0x0A: ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    0x0C: ((0.0, 0.0, -1.0), (1.0, 0.0, 0.0), (0.0, -1.0, 0.0)),
    0x0D: ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
    0x0E: ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0), (1.0, 0.0, 0.0)),
    0x10: ((0.0, -1.0, 0.0), (0.0, 0.0, -1.0), (1.0, 0.0, 0.0)),
    0x11: ((0.0, 0.0, 1.0), (0.0, -1.0, 0.0), (1.0, 0.0, 0.0)),
    0x14: ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, -1.0)),
    0x15: ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
    0x17: ((-1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    0x18: ((-1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, -1.0, 0.0)),
    0x19: ((0.0, 1.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    0x1B: ((0.0, 0.0, -1.0), (-1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    0x1C: ((0.0, -1.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
    0x1E: ((0.0, 0.0, 1.0), (-1.0, 0.0, 0.0), (0.0, -1.0, 0.0)),
    0x1F: ((0.0, 1.0, 0.0), (0.0, 0.0, -1.0), (-1.0, 0.0, 0.0)),
    0x20: ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0)),
    0x22: ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0), (-1.0, 0.0, 0.0)),
    0x23: ((0.0, 0.0, -1.0), (0.0, -1.0, 0.0), (-1.0, 0.0, 0.0)),
}


def _read_cframe_array(reader: _Reader, count: int) -> List[Tuple[Vec3, Optional[Matrix3]]]:
    """CFrames lesen: (Position, Rotationsmatrix) je Wert.

    Pro Wert: 1 Byte Rotations-ID. ID 0x00 -> 9 rohe Float32 (Matrix
    zeilenweise R00..R22); andere ID -> Tabelle _BASIC_ROTATIONS.
    Danach folgt das Vector3-Array der Positionen.

    Rotationen sind fuer die Riggung entscheidend: Segmente aus
    Bundle-Modellen tragen ihre eigene Drehung (z. B. Fuss 90 Grad,
    Hut schraeg aufgesetzt). Sie bisher zu ueberspringen, war die
    Ursache fuer falsche Position und Rotation der Rig-Teile.
    """
    rotations: List[Optional[Matrix3]] = []
    for _ in range(count):
        rotation_id = reader.u8()
        if rotation_id == 0x00:
            values = struct.unpack("<9f", reader.read(9 * 4))
            rotations.append(tuple(  # type: ignore[arg-type]
                tuple(values[r * 3:r * 3 + 3]) for r in range(3)))
        else:
            # Unbekannte kuenftige ID -> None (Identitaet) statt Absturz.
            rotations.append(_BASIC_ROTATIONS.get(rotation_id))
    positions = _read_vector3_array(reader, count)
    return list(zip(positions, rotations))


def _read_content_array(reader: _Reader, count: int) -> List[str]:
    """Neuer Content-Typ (0x22): pro Instanz die URI (oder "") liefern."""
    source_types = _deinterleave_u32(reader.read(4 * count), count)  # Enum, untransformiert
    uris = [reader.string() for _ in range(reader.u32())]
    object_count = reader.u32()
    reader.skip(4 * object_count)  # Referenten auf Objekte: fuer uns irrelevant
    external_count = reader.u32()
    reader.skip(4 * external_count)
    values: List[str] = []
    uri_index = 0
    for source in source_types:
        if source == 1 and uri_index < len(uris):  # 1 == Uri
            values.append(uris[uri_index])
            uri_index += 1
        else:
            values.append("")
    return values


# ---------------------------------------------------------------------------
# Chunk-Dekompression (ZSTD / LZ4 / Deflate)
# ---------------------------------------------------------------------------


def _decompress_chunk(raw: bytes, expected_len: int) -> bytes:
    if raw[:4] == _ZSTD_MAGIC:
        if _zstandard is None:
            raise RbxmError(
                "RBXM-Chunk ist ZSTD-komprimiert, aber das Python-Modul "
                "'zstandard' fehlt (pip install zstandard)"
            )
        try:
            reader = _zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw))
            result = reader.read(expected_len if expected_len > 0 else -1)
        except Exception as exc:  # zstandard.ZstdError u. a.
            raise RbxmError(f"ZSTD-Dekompression fehlgeschlagen: {exc}") from exc
        if expected_len and len(result) != expected_len:
            raise RbxmError("ZSTD-Dekompression lieferte unerwartete Laenge")
        return result

    errors: List[str] = []

    # 1) LZ4 im Roblox-Format: 4 Byte unkomprimierte Laenge, dann LZ4-Block
    lz4_missing = _lz4_block is None
    looks_like_lz4 = (
        expected_len > 0 and len(raw) > 4
        and struct.unpack_from("<I", raw, 0)[0] == expected_len
    )
    if looks_like_lz4 and not lz4_missing:
        try:
            result = _lz4_block.decompress(raw[4:], uncompressed_size=expected_len)
            if len(result) == expected_len:
                return result
            errors.append("LZ4: unerwartete Laenge")
        except Exception as exc:
            errors.append(f"LZ4: {exc}")

    # 2) Deflate-Varianten (roh / mit zlib-Header, ggf. nach 4-Byte-Praefix)
    for candidate in (raw, raw[4:] if len(raw) > 4 else raw):
        for wbits in (-15, zlib.MAX_WBITS):
            try:
                result = zlib.decompress(candidate, wbits)
            except zlib.error:
                continue
            if not expected_len or len(result) == expected_len:
                return result
            errors.append("Deflate: unerwartete Laenge")

    # 3) LZ4-Block ohne Laengen-Praefix
    if not lz4_missing and expected_len > 0:
        try:
            result = _lz4_block.decompress(raw, uncompressed_size=expected_len)
            if len(result) == expected_len:
                return result
            errors.append("LZ4 (ohne Praefix): unerwartete Laenge")
        except Exception as exc:
            errors.append(f"LZ4 (ohne Praefix): {exc}")

    if lz4_missing and looks_like_lz4:
        raise RbxmError(
            "RBXM-Chunk ist LZ4-komprimiert, aber das Python-Modul 'lz4' "
            "fehlt (pip install lz4)"
        )
    detail = "; ".join(errors) if errors else "unbekanntes Format"
    raise RbxmError(f"Chunk konnte nicht dekomprimiert werden ({detail})")


# ---------------------------------------------------------------------------
# Gemeinsames Zwischenformat: Instanz-Records -> Asset-Eintraege
# ---------------------------------------------------------------------------

# Property-Namen (serialisiert, kleingeschrieben) -> Feld im Record.
# Klassische Kleidung (Shirt/Pants/TShirt) wird ebenfalls als RBXM
# ausgeliefert; die Bild-ID steht dort in ShirtTemplate/PantsTemplate/Graphic.
_TEXTURE_PROPS = {"textureid", "texture", "colormap", "basetextureid",
                  "shirttemplate", "pantstemplate", "graphic"}
_TEXTURE_OVERLAY_PROPS = {"overlaytextureid"}
_CAGE_MESH_PROPS = {"cagemeshid", "referencemeshid"}

def _new_record(cls: str) -> Dict[str, Any]:
    return {
        "class": cls, "name": "", "mesh_url": "", "texture_url": "",
        "overlay_url": "", "cage_mesh_url": "", "reference_mesh_url": "",
        "size": None, "position": None, "rotation": None,
        "attachment_point": None, "import_origin": None, "reference_origin": None,
        "scale": None, "offset": None, "transparency": None, "puffiness": None,
        "value_str": None, "value_vec3": None,
        # Emote-Posen (tools/emote_pose.py): Thumbnails-Metadaten,
        # Keyframes/Pose-CFrames und CurveAnimation-Kanaldaten.
        "value_num": None, "time": None, "weight": None,
        "rotation_order": None, "values_and_times": None,
        "animation_url": "", "loop": None,
        "Parent": None, "children": [],
    }


def _unpack_cframe(value: Any) -> Tuple[Optional[Vec3], Optional[Matrix3]]:
    if (isinstance(value, tuple) and len(value) == 2
            and isinstance(value[0], tuple) and len(value[0]) == 3):
        return value[0], value[1]
    if isinstance(value, tuple) and len(value) == 3:
        return value, None  # type: ignore[return-value]
    return None, None


def _apply_property(record: Dict[str, Any], prop_name: str, value: Any) -> None:
    key = prop_name.lower()
    if key == "name" and isinstance(value, str):
        record["name"] = value
    elif key == "meshid" and isinstance(value, str):
        record["mesh_url"] = value
    elif key == "cagemeshid" and isinstance(value, str):
        record["cage_mesh_url"] = value
    elif key == "referencemeshid" and isinstance(value, str):
        record["reference_mesh_url"] = value
    elif key in _TEXTURE_PROPS and isinstance(value, str):
        if value and not record["texture_url"]:
            record["texture_url"] = value
    elif key in _TEXTURE_OVERLAY_PROPS and isinstance(value, str):
        if value and not record["overlay_url"]:
            record["overlay_url"] = value
    elif key == "value":
        if isinstance(value, str):
            record["value_str"] = value
        elif isinstance(value, tuple) and len(value) == 3:
            record["value_vec3"] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            record["value_num"] = float(value)
    elif key == "animationid" and isinstance(value, str):
        record["animation_url"] = value
    elif key == "time" and isinstance(value, (int, float)) and not isinstance(value, bool):
        record["time"] = float(value)
    elif key == "weight" and isinstance(value, (int, float)) and not isinstance(value, bool):
        record["weight"] = float(value)
    elif key == "rotationorder" and isinstance(value, (int, float)) and not isinstance(value, bool):
        record["rotation_order"] = int(value)
    elif key == "attachmentpos" and isinstance(value, tuple) and len(value) == 3:
        record["hat_attachment_pos"] = value
    elif key == "attachmentforward" and isinstance(value, tuple) and len(value) == 3:
        record["hat_attachment_forward"] = value
    elif key == "attachmentup" and isinstance(value, tuple) and len(value) == 3:
        record["hat_attachment_up"] = value
    elif key == "valuesandtimes" and isinstance(value, (bytes, bytearray)):
        record["values_and_times"] = bytes(value)
    elif key == "loop" and isinstance(value, bool):
        record["loop"] = value
    elif key == "size":
        record["size"] = value
    elif key == "cframe":
        record["position"], record["rotation"] = _unpack_cframe(value)
    elif key == "attachmentpoint":
        pos, rot = _unpack_cframe(value)
        if pos is not None:
            record["attachment_point"] = {"position": pos, "rotation": rot}
    elif key == "importorigin":
        pos, rot = _unpack_cframe(value)
        if pos is not None:
            record["import_origin"] = {"position": pos, "rotation": rot}
    elif key == "referenceorigin":
        pos, rot = _unpack_cframe(value)
        if pos is not None:
            record["reference_origin"] = {"position": pos, "rotation": rot}
    elif key == "scale":
        record["scale"] = value
    elif key in ("offset", "vertex"):
        record["offset"] = value
    elif key == "transparency" and isinstance(value, (int, float)):
        record["transparency"] = float(value)
    elif key == "puffiness" and isinstance(value, (int, float)):
        record["puffiness"] = float(value)


def _assemble(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aus Instanz-Records die Asset-Eintraege bauen.

    SpecialMeshes erben Groesse/Position vom Parent-Part (skaliert mit der
    SpecialMesh-Scale), SurfaceAppearance-/Decal-Texturen werden dem
    Parent-Eintrag zugeordnet, wenn dieser selbst eine Mesh-ID hat.
    Zusaetzlich werden R15-Ordner, Attachments, OriginalSize, Transparenz
    und WrapLayer-/WrapTarget-Metadaten an den Eintrag gehaengt.
    """
    for rec in records:
        rec["children"] = []
    for rec in records:
        parent = rec.get("Parent")
        if parent is not None:
            parent["children"].append(rec)

    by_record: Dict[int, Dict[str, Any]] = {}

    def enclosing_folder(rec: Dict[str, Any]) -> Optional[str]:
        cur = rec.get("Parent")
        while cur is not None:
            if cur.get("class") == "Folder" and cur.get("name"):
                return cur["name"]
            cur = cur.get("Parent")
        return None

    def enclosing_attachment_point(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cur = rec
        while cur is not None:
            if cur.get("attachment_point") is not None:
                return cur["attachment_point"]
            cur = cur.get("Parent")
        return None

    def enclosing_hat_mount(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Legacy-Hut (Klasse Hat, vor der Accessoire-Aera): Mounting über
        AttachmentPos/AttachmentForward/AttachmentUp am Hat-Objekt, oft zwei
        Ebenen ueber dem Mesh (SpecialMesh -> Handle-Part -> Hat)."""
        cur = rec
        while cur is not None:
            if cur.get("hat_attachment_pos") is not None:
                return {
                    "pos": cur["hat_attachment_pos"],
                    "forward": cur.get("hat_attachment_forward"),
                    "up": cur.get("hat_attachment_up"),
                }
            cur = cur.get("Parent")
        return None

    def rbxm_ancestry(rec: Dict[str, Any]) -> str:
        names = [rec.get("class") or "?"]
        cur = rec.get("Parent")
        while cur is not None and len(names) < 5:
            names.append(cur.get("class") or "?")
            cur = cur.get("Parent")
        return "/".join(names)

    def collect_part_metadata(rec: Dict[str, Any]) -> Dict[str, Any]:
        """Attachments, OriginalSize, ScaleType und Wrap-Daten sammeln."""
        sources = [rec]
        parent = rec.get("Parent")
        if parent is not None and rec["class"] in ("SpecialMesh", "BlockMesh", "CylinderMesh", "FileMesh"):
            sources.insert(0, parent)

        attachments: Dict[str, Dict[str, Any]] = {}
        original_size: Optional[Vec3] = None
        scale_type: Optional[str] = None
        wrap_layer: Optional[Dict[str, Any]] = None
        wrap_target: Optional[Dict[str, Any]] = None

        for src in sources:
            for ch in src.get("children", ()):
                ccls = ch.get("class")
                cname = ch.get("name") or ""
                if ccls == "Attachment" and cname and ch.get("position") is not None:
                    attachments[cname] = {
                        "position": ch["position"],
                        "rotation": ch.get("rotation"),
                    }
                elif ccls == "Vector3Value":
                    if cname == "OriginalSize" and ch.get("value_vec3") is not None:
                        original_size = ch["value_vec3"]
                    elif cname.endswith("Attachment") and ch.get("value_vec3") is not None:
                        attachments.setdefault(cname, {
                            "position": ch["value_vec3"],
                            "rotation": None,
                        })
                elif ccls == "StringValue" and cname == "AvatarPartScaleType":
                    if ch.get("value_str"):
                        scale_type = ch["value_str"]
                elif ccls == "WrapLayer":
                    wrap_layer = {
                        "import_origin": ch.get("import_origin"),
                        "reference_origin": ch.get("reference_origin"),
                        "cage_mesh_id": parse_asset_id(ch.get("cage_mesh_url")),
                        "reference_mesh_id": parse_asset_id(ch.get("reference_mesh_url")),
                        "puffiness": ch.get("puffiness"),
                    }
                elif ccls == "WrapTarget":
                    wrap_target = {
                        "cage_mesh_id": parse_asset_id(ch.get("cage_mesh_url")),
                    }

        transparency = rec.get("transparency")
        if transparency is None and parent is not None:
            transparency = parent.get("transparency")

        return {
            "folder": enclosing_folder(rec),
            "transparency": float(transparency) if transparency is not None else 0.0,
            "mesh_scale": rec.get("scale"),
            "mesh_offset": rec.get("offset"),
            "original_size": original_size,
            "scale_type": scale_type,
            "attachments": attachments,
            "accessory_attachment_point": enclosing_attachment_point(rec),
            "hat_mount": enclosing_hat_mount(rec),
            "rbxm_ancestry": rbxm_ancestry(rec),
            "import_origin": rec.get("import_origin"),
            "wrap_layer": wrap_layer,
            "wrap_target": wrap_target,
        }

    def effective_size(rec: Dict[str, Any]) -> Optional[Vec3]:
        Parent = rec.get("Parent")
        if rec["size"] is not None:
            return rec["size"]
        if Parent is not None:
            base = Parent.get("size")
            if base is not None:
                if rec.get("scale"):
                    return tuple(b * s for b, s in zip(base, rec["scale"]))  # type: ignore[return-value]
                return base
        return rec.get("scale")

    def effective_position(rec: Dict[str, Any]) -> Optional[Vec3]:
        Parent = rec.get("Parent")
        pos = rec["position"]
        if pos is None and Parent is not None:
            pos = Parent.get("position")
        if pos is not None and rec.get("offset"):
            # Offset liegt im lokalen Part-Rahmen: bei gedrehtem Part
            # zuerst drehen, sonst verrutscht das Accessoire.
            offset = rec["offset"]
            rotation = effective_rotation(rec)
            if rotation is not None:
                offset = (
                    rotation[0][0] * offset[0] + rotation[0][1] * offset[1] + rotation[0][2] * offset[2],
                    rotation[1][0] * offset[0] + rotation[1][1] * offset[1] + rotation[1][2] * offset[2],
                    rotation[2][0] * offset[0] + rotation[2][1] * offset[1] + rotation[2][2] * offset[2],
                )
            pos = tuple(p + o for p, o in zip(pos, offset))  # type: ignore[assignment]
        return pos

    def effective_rotation(rec: Dict[str, Any]) -> Optional[Matrix3]:
        if rec.get("rotation") is not None:
            return rec["rotation"]
        Parent = rec.get("Parent")
        if Parent is not None:
            return Parent.get("rotation")
        return None

    for rec in records:
        texture_url = rec["texture_url"] or rec["overlay_url"]
        mesh_id = parse_asset_id(rec["mesh_url"])
        texture_id = parse_asset_id(texture_url)
        if mesh_id is None and texture_id is None:
            continue
        if mesh_id is None and rec.get("Parent") is not None:
            # Reine Textur (SurfaceAppearance/Decal): dem Parent zuordnen,
            # falls der Parent einen eigenen Eintrag bekommt.
            parent_entry = by_record.get(id(rec["Parent"]))
            if parent_entry is not None and parent_entry["texture_id"] is None:
                parent_entry["texture_id"] = texture_id
                continue
        entry = {
            "class": rec["class"],
            "name": (rec["Parent"]["name"] if rec.get("Parent") and rec["class"] == "SpecialMesh"
                     and rec["Parent"].get("name") else rec["name"]),
            "mesh_id": mesh_id,
            "texture_id": texture_id,
            "size": effective_size(rec),
            "position": effective_position(rec),
            "rotation": effective_rotation(rec),
        }
        entry.update(collect_part_metadata(rec))
        by_record[id(rec)] = entry

    # Texture-Kinder, deren Parent erst nach ihnen verarbeitet wurde,
    # in einem zweiten Durchlauf zuordnen (sonst doppelt vorhanden).
    result: List[Dict[str, Any]] = []
    for rec in records:
        entry = by_record.get(id(rec))
        if entry is None:
            continue
        if entry["mesh_id"] is None and entry["texture_id"] is not None and rec.get("Parent") is not None:
            parent_entry = by_record.get(id(rec["Parent"]))
            if parent_entry is not None and parent_entry["mesh_id"] is not None:
                if parent_entry["texture_id"] is None:
                    parent_entry["texture_id"] = entry["texture_id"]
                continue
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Binaer-Parser (RBXM)
# ---------------------------------------------------------------------------

_STRING_PROPS = ({"name", "meshid", "value", "animationid"} | _TEXTURE_PROPS
                 | _TEXTURE_OVERLAY_PROPS | _CAGE_MESH_PROPS)
_VECTOR3_PROPS = {"size", "scale", "offset", "vertex", "value",
                  "attachmentpos", "attachmentforward", "attachmentup"}
_CFRAME_PROPS = {"cframe", "attachmentpoint", "importorigin", "referenceorigin"}
_FLOAT_PROPS = {"transparency", "puffiness", "time", "weight", "value",
                "authoredhipheight"}
_BOOL_PROPS = {"archivable", "loop"}
# Int32-Werte (Zigzag wie Referenten) - z. B. IntValue.Value (ThumbnailBundleId).
_INT_PROPS = {"value"}
# Token/Enums: interleaved u32 OHNE Zigzag (PropertyType.Enum, ID 0x12) -
# z. B. EulerRotationCurve.RotationOrder und Pose.EasingStyle/Direction.
_ENUM_PROPS = {"rotationorder", "easingstyle", "easingdirection"}
# Binaer-Blobs (PropertyType.String mit Rohbytes): FloatCurve/RotationCurve-
# Keys ("ValuesAndTimes") in CurveAnimations (Emotes).
_BINARY_PROPS = {"valuesandtimes"}


def _parse_binary(data: bytes) -> List[Dict[str, Any]]:
    records, payloads = _parse_binary_records(data)
    entries = _assemble(records)
    if not entries:
        entries = _scan_for_asset_ids(payloads)
    return entries


def _parse_binary_records(data: bytes,
                          prop_log: Optional[List[str]] = None
                          ) -> Tuple[List[Dict[str, Any]], List[bytes]]:
    """Liest alle Instanz-Records (mit ``Parent``-Verweisen) aus binaerem RBXM.

    Rueckgabe: (Records, dekomprimierte Chunk-Payloads). Die Baum-Verknuepfung
    (``children``) macht :func:`parse_instances`. ``prop_log`` sammelt
    (Diagnose) eine Zeile je PROP-Chunk: Klasse, Property, Typ-ID, Anzahl.
    """
    if len(data) < 32 or not data.startswith(MAGIC):
        raise RbxmError("Keine RBXM-Datei (Magic '<roblox!' fehlt oder Datei zu kurz)")

    pos = 32  # 14 Magic + 2 Version + 4 Klassen + 4 Instanzen + 8 reserviert
    classes: Dict[int, Dict[str, Any]] = {}
    records: List[Dict[str, Any]] = []
    by_referent: Dict[int, Dict[str, Any]] = {}
    shared_strings: List[str] = []
    shared_string_bytes: List[bytes] = []
    parents: Dict[int, int] = {}
    payloads: List[bytes] = []
    saw_end = False

    while pos + 16 <= len(data):
        name_raw = data[pos:pos + 4]
        comp_len, uncomp_len = struct.unpack_from("<II", data, pos + 4)
        pos += 16
        body_len = comp_len if comp_len else uncomp_len
        if pos + body_len > len(data):
            raise RbxmError("RBXM-Datei ist abgeschnitten (Chunk laeuft ueber Dateiende)")
        raw = data[pos:pos + body_len]
        pos += body_len
        payload = _decompress_chunk(raw, uncomp_len) if comp_len else raw
        payloads.append(payload)
        chunk = name_raw.rstrip(b"\x00").decode("ascii", errors="replace")
        reader = _Reader(payload)

        if chunk == "END":
            break
        if chunk == "SSTR":
            reader.u32()  # Version
            count = reader.u32()
            shared_strings = []
            shared_string_bytes = []
            for _ in range(count):
                reader.skip(16)  # MD5-Hash (wird von Roblox ignoriert)
                length = reader.u32()
                raw = reader.read(length)
                # Binaer-Blobs (ValuesAndTimes) brauchen die rohen Bytes;
                # Text-Properties bekommen die dekodierte Variante.
                shared_string_bytes.append(raw)
                shared_strings.append(raw.decode("utf-8", errors="replace"))
        elif chunk == "INST":
            class_id = reader.u32()
            class_name = reader.string()
            reader.u8()  # Object Format (Service-Marker kommen erst nach den Referenten)
            count = reader.u32()
            referents = _read_referents(reader, count)
            recs = [_new_record(class_name) for _ in range(count)]
            for ref, rec in zip(referents, recs):
                by_referent[ref] = rec
            classes[class_id] = {"name": class_name, "records": recs}
            records.extend(recs)
        elif chunk == "PROP":
            class_id = reader.u32()
            prop_name = reader.string()
            type_id = reader.u8()
            cls = classes.get(class_id)
            if cls is None:
                continue
            recs = cls["records"]
            key = prop_name.lower()
            if prop_log is not None:
                prop_log.append("PROP %s.%s typ=0x%02x n=%d"
                                % (cls["name"], prop_name, type_id, len(recs)))
            values: Optional[List[Any]] = None
            try:
                if key in _STRING_PROPS and type_id in (_T_STRING, _T_SHARED_STRING, _T_CONTENT):
                    if type_id == _T_STRING:
                        values = [reader.string() for _ in recs]
                    elif type_id == _T_SHARED_STRING:
                        indices = _deinterleave_u32(reader.read(4 * len(recs)), len(recs))
                        values = [
                            shared_strings[i] if i < len(shared_strings) else "" for i in indices
                        ]
                    elif type_id == _T_CONTENT:
                        values = _read_content_array(reader, len(recs))
                elif key in _VECTOR3_PROPS and type_id == _T_VECTOR3:
                    values = _read_vector3_array(reader, len(recs))  # type: ignore[assignment]
                elif key in _CFRAME_PROPS and type_id == _T_CFRAME:
                    values = _read_cframe_array(reader, len(recs))  # type: ignore[assignment]
                elif key in _FLOAT_PROPS and type_id == _T_FLOAT32:
                    values = _read_f32_array(reader, len(recs))
                elif key in _FLOAT_PROPS and type_id == _T_FLOAT64:
                    # Float64: einfache Little-Endian-Doubles, nicht interleaved.
                    count = len(recs)
                    values = list(struct.unpack("<%dd" % count, reader.read(8 * count))) if count else []
                elif key in _INT_PROPS and type_id == _T_INT32:
                    values = _read_i32_array(reader, len(recs))
                elif key in _ENUM_PROPS and type_id == _T_ENUM:
                    # Token: interleaved u32 ohne Zigzag-Dekodierung.
                    values = list(_deinterleave_u32(reader.read(4 * len(recs)), len(recs)))
                elif key in _BINARY_PROPS and type_id in (_T_STRING, _T_PROTECTED_STRING):
                    values = [bytes(reader.read(reader.u32())) for _ in recs]
                elif key in _BINARY_PROPS and type_id == _T_SHARED_STRING:
                    indices = _deinterleave_u32(reader.read(4 * len(recs)), len(recs))
                    values = [
                        shared_string_bytes[i] if i < len(shared_string_bytes) else b""
                        for i in indices
                    ]
                elif key in _BOOL_PROPS and type_id == _T_BOOL:
                    values = [bool(b) for b in reader.read(len(recs))]
            except RbxmError:
                # Unbekannte kuenftige Kodierung dieses Properties: lieber
                # das Property weglassen (Werte fehlen dann) als den ganzen
                # Avatar-Load abbrechen zu lassen. Andere Properties und die
                # Chunk-Struktur bleiben unberuehrt (eigener Reader je Chunk).
                values = None
            if values is not None:
                for rec, value in zip(recs, values):
                    _apply_property(rec, prop_name, value)
        elif chunk == "PRNT":
            reader.u8()  # Version
            count = reader.u32()
            children = _read_referents(reader, count)
            parent_refs = _read_referents(reader, count)
            parents.update(zip(children, parent_refs))
        # META und unbekannte Chunks: per Chunk-Laenge uebersprungen

    if not saw_end and (pos < len(data) or not payloads):
        # Partieller Chunk-Header am Dateiende oder gar keine Chunks:
        # das ist eine abgeschnittene Datei, kein leeres Modell.
        raise RbxmError("RBXM-Datei ist abgeschnitten (END-Chunk fehlt)")

    for child_ref, parent_ref in parents.items():
        if parent_ref >= 0 and child_ref in by_referent and parent_ref in by_referent:
            by_referent[child_ref]["Parent"] = by_referent[parent_ref]

    return records, payloads


# Legacy-URL-Form: klassische Shirt-/Pants-Templates stehen oft als
# "http://www.roblox.com/asset/?id=123" in der Datei.
_URL_ID_RE = re.compile(rb"asset/\?[^\"'\x00]{0,40}?[?&]id=(\d+)|[?&]id=(\d+)")


def _scan_for_asset_ids(payloads: List[bytes]) -> List[Dict[str, Any]]:
    """Notnagel: dekomprimierte Chunks nach Asset-IDs im Klartext absuchen.

    Nur aktiv, wenn die strukturierte Auswertung gar keine IDs fand
    (z. B. unbekannte kuenftige Property-Kodierung). Gesucht wird
    ``rbxassetid://NNN`` und - falls das nichts ergibt - die
    Legacy-URL-Form ``.../asset/?id=NNN``. Ob eine gefundene ID ein
    Mesh oder eine Textur ist, entscheidet der Aufrufer nach dem
    Download anhand der Magic-Bytes.
    """
    found: List[int] = []
    seen = set()
    for payload in payloads:
        for match in re.finditer(rb"rbxassetid://(\d+)", payload):
            number = int(match.group(1))
            if number > 0 and number not in seen:
                seen.add(number)
                found.append(number)
    if not found:
        for payload in payloads:
            for match in _URL_ID_RE.finditer(payload):
                number = int(match.group(1) or match.group(2))
                if number > 100 and number not in seen:
                    seen.add(number)
                    found.append(number)
    return [
        {"class": "Unbekannt", "name": "", "mesh_id": number,
         "texture_id": None, "size": None, "position": None, "rotation": None}
        for number in found
    ]


# ---------------------------------------------------------------------------
# XML-Parser (RBXMX)
# ---------------------------------------------------------------------------


def _xml_float(text: Optional[str]) -> float:
    try:
        return float(text or 0.0)
    except ValueError:
        return 0.0


def _parse_xml(data: bytes) -> List[Dict[str, Any]]:
    records = _parse_xml_records(data)
    entries = _assemble(records)
    if not entries:
        entries = _scan_for_asset_ids([data])
    return entries


def _parse_xml_records(data: bytes) -> List[Dict[str, Any]]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise RbxmError(f"RBXMX-XML nicht lesbar: {exc}") from exc

    shared: Dict[str, str] = {}
    shared_root = root.find("SharedStrings")
    if shared_root is not None:
        for el in shared_root.findall("SharedString"):
            md5 = el.get("md5") or ""
            try:
                shared[md5] = base64.b64decode(el.text or "").decode("utf-8", errors="replace")
            except (ValueError, TypeError):
                pass

    records: List[Dict[str, Any]] = []

    def walk(item: ET.Element, Parent: Optional[Dict[str, Any]]) -> None:
        rec = _new_record(item.get("class") or "")
        rec["Parent"] = Parent
        records.append(rec)
        props = item.find("Properties")
        if props is not None:
            for prop in props:
                name = prop.get("name") or ""
                if prop.tag == "Content":
                    url = prop.find("url")
                    value: Any = url.text if url is not None and url.text else ""
                elif prop.tag in ("string", "ProtectedString"):
                    value = prop.text or ""
                elif prop.tag == "SharedString":
                    value = shared.get((prop.text or "").strip(), "")
                elif prop.tag in ("float", "double"):
                    value = _xml_float(prop.text)
                elif prop.tag in ("int", "token", "int64"):
                    try:
                        value = int((prop.text or "0").strip())
                    except ValueError:
                        value = 0
                elif prop.tag == "BinaryString":
                    try:
                        value = base64.b64decode((prop.text or "").strip())
                    except (ValueError, TypeError):
                        value = b""
                elif prop.tag == "bool":
                    value = (prop.text or "").strip().lower() in ("true", "1")
                elif prop.tag == "Vector3":
                    value = tuple(_xml_float(prop.findtext(axis)) for axis in ("X", "Y", "Z"))
                elif prop.tag == "CoordinateFrame":
                    position = tuple(_xml_float(prop.findtext(axis))
                                     for axis in ("X", "Y", "Z"))
                    rotation = None
                    if prop.find("R00") is not None:
                        flat = tuple(_xml_float(prop.findtext(f"R{i}{j}"))
                                     for i in range(3) for j in range(3))
                        rotation = (flat[0:3], flat[3:6], flat[6:9])  # type: ignore[assignment]
                    value = (position, rotation)
                else:
                    continue
                _apply_property(rec, name, value)
        for child in item.findall("Item"):
            walk(child, rec)

    for top in root.findall("Item"):
        walk(top, None)

    return records


# ---------------------------------------------------------------------------
# Oeffentliche API
# ---------------------------------------------------------------------------


def extract_assets(data: bytes) -> List[Dict[str, Any]]:
    """Alle Mesh-/Textur-Verweise aus RBXM- (binaer) oder RBXMX-Daten (XML).

    Liefert eine Liste von Dicts mit den Schluesseln ``class``, ``name``,
    ``mesh_id`` (int | None), ``texture_id`` (int | None), ``size``
    (x, y, z in Studs | None), ``position`` (x, y, z | None) und
    ``rotation`` (3x3-Matrix als drei Zeilen | None; Identitaet oder
    bundletypische Drehung aus der Sonder-Rotation-Tabelle).
    Unleserliche Daten werfen einen :class:`RbxmError`.
    """
    if not data:
        raise RbxmError("Leere RBXM-Daten")
    if data[:len(MAGIC)] == MAGIC:
        return _parse_binary(data)
    head = data[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"<roblox") or head.startswith(b"<?xml"):
        return _parse_xml(data)
    raise RbxmError("Weder RBXM (binaer) noch RBXMX (XML) erkannt")


def parse_instances(data: bytes,
                    prop_log: Optional[List[str]] = None
                    ) -> List[Dict[str, Any]]:
    """Komplette Instanz-Hierarchie aus RBXM/RBXMX - Wurzel-Instanzen.

    Fuer Emote-Posen (tools/emote_pose.py): im Gegensatz zu
    :func:`extract_assets` werden hier **alle** Instanzen zurueckgegeben
    (KeyframeSequence/Keyframe/Pose/SubPose, CurveAnimation/FloatCurve,
    Animation mit NumberValue-Metadaten ...), verknuepft ueber ``children``
    und ``Parent``. Jeder Record traegt ``class``, ``name`` und die in
    :func:`_apply_property` gefuellten Felder (u. a. ``position``/
    ``rotation`` aus CFrame, ``value_num``, ``time``, ``weight``,
    ``values_and_times``, ``animation_url``).
    """
    if not data:
        raise RbxmError("Leere RBXM-Daten")
    if data[:len(MAGIC)] == MAGIC:
        records, _payloads = _parse_binary_records(data, prop_log)
    else:
        head = data[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        if head.startswith(b"<roblox") or head.startswith(b"<?xml"):
            records = _parse_xml_records(data)
        else:
            raise RbxmError("Weder RBXM (binaer) noch RBXMX (XML) erkannt")

    for rec in records:
        rec["children"] = []
    roots: List[Dict[str, Any]] = []
    for rec in records:
        parent = rec.get("Parent")
        if parent is None:
            roots.append(rec)
        else:
            parent["children"].append(rec)
    if not roots and records:
        # Zyklen-Schutz: sollte bei echten Dateien nicht passieren.
        roots = records
    return roots
