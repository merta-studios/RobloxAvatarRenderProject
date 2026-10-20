"""Synthetische RBXM-Dateien fuer Tests erzeugen (Spec-Gegenstueck zu rbxm.py).

Die Encoder hier bilden die dokumentierten Roblox-Transformationen nach
(Big-Endian-Byte-Planes, Zigzag-Integers, rotierte Floats, Chunk-Framing
mit optionaler LZ4-/ZSTD-/Deflate-Kompression), damit der Parser gegen
realitaetsnahe Bytes getestet wird - ohne Netzwerk und ohne echte
Roblox-Dateien im Repo.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any, Dict, List, Sequence, Tuple

MAGIC = b"<roblox!\x89\xff\r\n\x1a\n"


def enc_string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def interleave_u32(values: Sequence[int]) -> bytes:
    out = bytearray()
    for shift in (24, 16, 8, 0):
        for value in values:
            out.append((value >> shift) & 0xFF)
    return bytes(out)


def _zigzag(value: int) -> int:
    return ((value << 1) ^ (value >> 31)) & 0xFFFFFFFF


def enc_i32_array(values: Sequence[int]) -> bytes:
    return interleave_u32([_zigzag(v) for v in values])


def enc_referents(refs: Sequence[int]) -> bytes:
    deltas: List[int] = []
    previous = 0
    for ref in refs:
        deltas.append(ref - previous)
        previous = ref
    return enc_i32_array(deltas)


def enc_f32_array(values: Sequence[float]) -> bytes:
    bits_list = []
    for value in values:
        bits = struct.unpack("<I", struct.pack("<f", value))[0]
        bits_list.append(((bits << 1) | (bits >> 31)) & 0xFFFFFFFF)  # links rotieren
    return interleave_u32(bits_list)


def enc_vector3_array(vecs: Sequence[Tuple[float, float, float]]) -> bytes:
    return (
        enc_f32_array([v[0] for v in vecs])
        + enc_f32_array([v[1] for v in vecs])
        + enc_f32_array([v[2] for v in vecs])
    )


def enc_cframe_array(items: Sequence[Any]) -> bytes:
    """items: (x,y,z) = Identitaets-Rotation (ID 0x02) oder ("M", (x,y,z))
    fuer eine explizite Rotationsmatrix (ID 0x00 + 9 Floats)."""
    out = bytearray()
    positions: List[Tuple[float, float, float]] = []
    for item in items:
        if isinstance(item, tuple) and item and item[0] == "M":
            out.append(0x00)
            out += struct.pack("<9f", 1, 0, 0, 0, 1, 0, 0, 0, 1)
            positions.append(tuple(item[1]))  # type: ignore[arg-type]
        else:
            out.append(0x02)
            positions.append(tuple(item))  # type: ignore[arg-type]
    out += enc_vector3_array(positions)
    return bytes(out)


def enc_content_array(values: Sequence[str]) -> bytes:
    """Neuer Content-Typ (0x22): alle Werte als Uri (oder None bei "")."""
    source_types = [1 if v else 0 for v in values]
    uris = [v for v in values if v]
    out = bytearray(interleave_u32(source_types))
    out += struct.pack("<I", len(uris))
    for uri in uris:
        out += enc_string(uri)
    out += struct.pack("<I", 0)  # ObjectCount
    out += struct.pack("<I", 0)  # ExternalObjectCount
    return bytes(out)


def make_chunk(name: bytes, payload: bytes, compression: str = "none") -> bytes:
    name = (name + b"\x00\x00\x00\x00")[:4]
    if compression == "none":
        return name + struct.pack("<III", 0, len(payload), 0) + payload
    if compression == "zlibraw":
        compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
        body = compressor.compress(payload) + compressor.flush()
    elif compression == "lz4":
        import lz4.block

        body = struct.pack("<I", len(payload)) + lz4.block.compress(payload, store_size=False)
    elif compression == "zstd":
        import zstandard

        body = zstandard.ZstdCompressor().compress(payload)
    else:  # pragma: no cover
        raise ValueError(compression)
    return name + struct.pack("<III", len(body), len(payload), 0) + body


def make_rbxm(instances: List[Dict[str, Any]], compression: str = "none",
              meshid_as_content: bool = False,
              name_as_shared: bool = False) -> bytes:
    """instances: [{"class": str, "props": {...}, "Parent": Index | None}].

    String-Props werden als Typ 0x01 kodiert (MeshId optional als Content
    0x22, Name optional als SharedString 0x1C), 3-Tupel als Vector3 (0x0E),
    "CFrame" als Typ 0x10 (Wert: (x,y,z) oder ("M", (x,y,z))).
    """
    referents = list(range(len(instances)))
    classes: Dict[str, List[int]] = {}
    for idx, inst in enumerate(instances):
        classes.setdefault(inst["class"], []).append(idx)

    chunks: List[bytes] = []

    shared_strings: List[str] = []
    if name_as_shared:
        for inst in instances:
            name = (inst.get("props") or {}).get("Name", "")
            if name not in shared_strings:
                shared_strings.append(name)
        payload = struct.pack("<II", 0, len(shared_strings))
        for text in shared_strings:
            payload += b"\x00" * 16 + enc_string(text)
        chunks.append(make_chunk(b"SSTR", payload, compression))

    class_ids: Dict[str, int] = {}
    for class_id, (class_name, idxs) in enumerate(classes.items()):
        class_ids[class_name] = class_id
        payload = struct.pack("<I", class_id) + enc_string(class_name)
        payload += b"\x00" + struct.pack("<I", len(idxs))
        payload += enc_referents([referents[i] for i in idxs])
        chunks.append(make_chunk(b"INST", payload, compression))

    for class_name, idxs in classes.items():
        prop_names: List[str] = []
        for i in idxs:
            for key in (instances[i].get("props") or {}):
                if key not in prop_names:
                    prop_names.append(key)
        for prop_name in prop_names:
            values = [(instances[i].get("props") or {}).get(prop_name) for i in idxs]
            if prop_name == "CFrame":
                filled = [v if v is not None else (0.0, 0.0, 0.0) for v in values]
                type_id, body = 0x10, enc_cframe_array(filled)
            elif all(isinstance(v, tuple) for v in values if v is not None):
                filled = [v if v is not None else (0.0, 0.0, 0.0) for v in values]
                type_id, body = 0x0E, enc_vector3_array(filled)  # type: ignore[arg-type]
            elif prop_name == "MeshId" and meshid_as_content:
                filled = [v if isinstance(v, str) else "" for v in values]
                type_id, body = 0x22, enc_content_array(filled)
            elif prop_name == "Name" and name_as_shared:
                indices = [shared_strings.index(v if isinstance(v, str) else "") for v in values]
                type_id, body = 0x1C, interleave_u32(indices)
            else:
                filled = [v if isinstance(v, str) else "" for v in values]
                type_id = 0x01
                body = b"".join(enc_string(v) for v in filled)
            payload = struct.pack("<I", class_ids[class_name]) + enc_string(prop_name)
            payload += bytes([type_id]) + body
            chunks.append(make_chunk(b"PROP", payload, compression))

    children = list(range(len(instances)))
    parents = []
    for inst in instances:
        Parent = inst.get("Parent")
        parents.append(-1 if Parent is None else referents[Parent])
    payload = b"\x00" + struct.pack("<I", len(instances))
    payload += enc_referents(children) + enc_referents(parents)
    chunks.append(make_chunk(b"PRNT", payload, compression))
    chunks.append(make_chunk(b"END", b"</roblox>", "none"))

    header = MAGIC + struct.pack("<Hii", 0, len(classes), len(instances)) + b"\x00" * 8
    return header + b"".join(chunks)
