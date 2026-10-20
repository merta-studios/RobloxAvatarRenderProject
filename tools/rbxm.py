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
_T_VECTOR3 = 0x0E
_T_CFRAME = 0x10
_T_SHARED_STRING = 0x1C
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


def _read_cframe_array(reader: _Reader, count: int) -> List[Tuple[Vec3, Optional[Matrix3]]]:
    """CFrames lesen: (Position, Rotationsmatrix | None).

    Pro Wert: 1 Byte Rotations-ID; nur bei ID 0x00 folgen 9 Float32
    (Rotationsmatrix, zeilenweise R00..R22). Danach kommt ein Vector3-Array
    mit den Positionen.

    Die Rotation wird gebraucht, damit Arme/Beine/Huete aus Bundle-Modellen
    nicht "verrutschen": Roblox speichert jedes Segment mit seiner eigenen
    Drehung (z. B. Fuss 90 Grad nach vorne), wer sie ignoriert, setzt die
    Teile schief bzw. an der falschen Stelle zusammen.
    """
    rotations: List[Optional[Matrix3]] = []
    for _ in range(count):
        if reader.u8() == 0x00:
            values = struct.unpack("<9f", reader.read(36))
            rotations.append(tuple(tuple(values[r * 3:r * 3 + 3]) for r in range(3)))  # type: ignore[arg-type]
        else:
            rotations.append(None)
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
                  "shirtTemplate", "pantstemplate", "graphic"}
_TEXTURE_OVERLAY_PROPS = {"overlaytextureid"}


def _new_record(cls: str) -> Dict[str, Any]:
    return {
        "class": cls, "name": "", "mesh_url": "", "texture_url": "",
        "overlay_url": "", "size": None, "position": None, "rotation": None,
        "scale": None, "offset": None, "Parent": None,
    }


def _apply_property(record: Dict[str, Any], prop_name: str, value: Any) -> None:
    key = prop_name.lower()
    if key == "name" and isinstance(value, str):
        record["name"] = value
    elif key == "meshid" and isinstance(value, str):
        record["mesh_url"] = value
    elif key in _TEXTURE_PROPS and isinstance(value, str):
        if value and not record["texture_url"]:
            record["texture_url"] = value
    elif key in _TEXTURE_OVERLAY_PROPS and isinstance(value, str):
        if value and not record["overlay_url"]:
            record["overlay_url"] = value
    elif key == "size":
        record["size"] = value
    elif key == "cframe":
        # Neuer Parser liefert (Position, Rotation); die XML-Variante und
        # aeltere Aufrufer gaben nur die Position.
        if (isinstance(value, tuple) and len(value) == 2
                and isinstance(value[0], tuple) and len(value[0]) == 3
                and (value[1] is None or isinstance(value[1], tuple))):
            record["position"], record["rotation"] = value
        else:
            record["position"] = value
    elif key == "scale":
        record["scale"] = value
    elif key == "offset":
        record["offset"] = value


def _assemble(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aus Instanz-Records die Asset-Eintraege bauen.

    SpecialMeshes erben Groesse/Position vom Parent-Part (skaliert mit der
    SpecialMesh-Scale), SurfaceAppearance-/Decal-Texturen werden dem
    Parent-Eintrag zugeordnet, wenn dieser selbst eine Mesh-ID hat.
    """
    by_record: Dict[int, Dict[str, Any]] = {}

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

    def effective_rotation(rec: Dict[str, Any]) -> Optional[Matrix3]:
        if rec.get("rotation") is not None:
            return rec["rotation"]
        Parent = rec.get("Parent")
        if Parent is not None:
            return Parent.get("rotation")
        return None

    def effective_position(rec: Dict[str, Any]) -> Optional[Vec3]:
        Parent = rec.get("Parent")
        pos = rec["position"]
        if pos is None and Parent is not None:
            pos = Parent.get("position")
        if pos is not None and rec.get("offset"):
            pos = tuple(p + o for p, o in zip(pos, rec["offset"]))  # type: ignore[assignment]
        return pos

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

_STRING_PROPS = ({"name", "meshid"} | _TEXTURE_PROPS | _TEXTURE_OVERLAY_PROPS)
_VECTOR3_PROPS = {"size", "scale", "offset"}


def _parse_binary(data: bytes) -> List[Dict[str, Any]]:
    if len(data) < 32 or not data.startswith(MAGIC):
        raise RbxmError("Keine RBXM-Datei (Magic '<roblox!' fehlt oder Datei zu kurz)")

    pos = 32  # 14 Magic + 2 Version + 4 Klassen + 4 Instanzen + 8 reserviert
    classes: Dict[int, Dict[str, Any]] = {}
    records: List[Dict[str, Any]] = []
    by_referent: Dict[int, Dict[str, Any]] = {}
    shared_strings: List[str] = []
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
            for _ in range(count):
                reader.skip(16)  # MD5-Hash (wird von Roblox ignoriert)
                shared_strings.append(reader.string())
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
            values: Optional[List[Any]] = None
            if key in _STRING_PROPS:
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
            elif key == "cframe" and type_id == _T_CFRAME:
                values = _read_cframe_array(reader, len(recs))  # type: ignore[assignment]
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

    entries = _assemble(records)
    if not entries:
        entries = _scan_for_asset_ids(payloads)
    return entries


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
         "texture_id": None, "size": None, "position": None}
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
                elif prop.tag == "Vector3":
                    value = tuple(_xml_float(prop.findtext(axis)) for axis in ("X", "Y", "Z"))
                elif prop.tag == "CoordinateFrame":
                    position = tuple(_xml_float(prop.findtext(axis))
                                     for axis in ("X", "Y", "Z"))
                    rows = []
                    for row in range(3):
                        cells = tuple(_xml_float(prop.findtext(f"R{row}{col}"))
                                      for col in range(3))
                        rows.append(cells)
                    rotation = None
                    if any(cell for line in rows for cell in line):
                        # Nur echte Drehungen uebernehmen (Identity weglassen)
                        identity = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
                        if any(abs(rows[r][c] - identity[r][c]) > 1e-6
                               for r in range(3) for c in range(3)):
                            rotation = tuple(rows)  # type: ignore[assignment]
                    value = (position, rotation)
                else:
                    continue
                _apply_property(rec, name, value)
        for child in item.findall("Item"):
            walk(child, rec)

    for top in root.findall("Item"):
        walk(top, None)

    entries = _assemble(records)
    if not entries:
        entries = _scan_for_asset_ids([data])
    return entries


# ---------------------------------------------------------------------------
# Oeffentliche API
# ---------------------------------------------------------------------------


def extract_assets(data: bytes) -> List[Dict[str, Any]]:
    """Alle Mesh-/Textur-Verweise aus RBXM- (binaer) oder RBXMX-Daten (XML).

    Liefert eine Liste von Dicts mit den Schluesseln ``class``, ``name``,
    ``mesh_id`` (int | None), ``texture_id`` (int | None), ``size``
    (x, y, z in Studs | None), ``position`` (x, y, z | None) und
    ``rotation`` (3x3-Matrix | None).
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
