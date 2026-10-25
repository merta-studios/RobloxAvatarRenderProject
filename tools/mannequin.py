"""Roblox' offizielle Standard-Koerper als Referenzgeometrie.

Zwei Referenzdateien, beide aus der offiziellen Doku:

* ``BlockyCharacter.fbx`` - der **R15-Blockkoerper** (Standard-Avatar, 15
  Blockteile). Daraus kommt die Geometrie fuer nicht getragene Koerperteile
  (``reference/standard_r15.json``).
* ``ClassicMannequin.fbx`` - der **klassische** Koerper (R6-Optik). Er dient
  dem Abgleich der Koerpermaße (``reference/classic_body.json``).

Ist eine Referenzdatei nicht vorhanden, liefert :func:`cached` ``None`` und
die Pipeline faellt auf ihre Quader-Notloesung zurueck.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from roblox_mesh import MeshData

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_PATH = os.path.join(ROOT, "reference", "classic_body.json")
#: R15-Blockkoerper (Standard-Avatar) als Referenzgeometrie.
STANDARD_PATH = os.path.join(ROOT, "reference", "standard_r15.json")

#: Reihenfolge der Standard-Teile (wie im FBX benannt).
PART_NAMES = (
    "UpperTorso", "LowerTorso", "Head",
    "LeftUpperArm", "LeftLowerArm", "LeftHand",
    "RightUpperArm", "RightLowerArm", "RightHand",
    "LeftUpperLeg", "LeftLowerLeg", "LeftFoot",
    "RightUpperLeg", "RightLowerLeg", "RightFoot",
)

#: Namen der Koerperteile, wie die Pipeline sie fuehrt -> Mannequin-Teile.
PART_ALIASES: Dict[str, str] = {
    "links_arm": "LeftArm",
    "rechts_arm": "RightArm",
    "links_bein": "LeftLeg",
    "rechts_bein": "RightLeg",
}

#: Segmente je Koerperteil (R15-Gruppierung von Roblox).
SEGMENTS_BY_PART: Dict[str, Tuple[str, ...]] = {
    "LeftArm": ("LeftUpperArm", "LeftLowerArm", "LeftHand"),
    "RightArm": ("RightUpperArm", "RightLowerArm", "RightHand"),
    "LeftLeg": ("LeftUpperLeg", "LeftLowerLeg", "LeftFoot"),
    "RightLeg": ("RightUpperLeg", "RightLowerLeg", "RightFoot"),
}


@dataclass
class ClassicBody:
    """Standard-Koerper mit exakten Maßen aus Roblox' Mannequin."""

    #: Teilname -> Mesh in *Teil*-Koordinaten (Ursprung = Teil-Mittelpunkt).
    meshes: Dict[str, MeshData] = field(default_factory=dict)
    #: Teilname -> Groesse (Breite, Hoehe, Tiefe) in Studs bei Skala 1.
    sizes: Dict[str, Tuple[float, float, float]] = field(default_factory=dict)
    #: Teilname -> Mittelpunkt/Position im Standard-Koerper (Weltkoordinaten).
    positions: Dict[str, Tuple[float, float, float]] = field(default_factory=dict)
    #: Abgeleitete Verhaeltnisse (alle relativ zur Torso-Hoehe des Standards).
    metrics: Dict[str, float] = field(default_factory=dict)
    source: str = ""

    # -- Zugriff ---------------------------------------------------------

    def mesh(self, name: str) -> Optional[MeshData]:
        return self.meshes.get(name)

    def size(self, name: str) -> Optional[Tuple[float, float, float]]:
        return self.sizes.get(name)

    def segment_names(self, part: str) -> Tuple[str, ...]:
        return SEGMENTS_BY_PART.get(part, ())

    # -- Verhaeltnisse ---------------------------------------------------

    def arm_proportions(self) -> Dict[str, float]:
        """Anteile der *Armlaenge* (Schulter -> Fingerspitze), Standard-Koerper."""
        return {k: self.metrics[k] for k in self.metrics if k.startswith("arm_")}

    def leg_proportions(self) -> Dict[str, float]:
        """Anteile der *Beinlaenge* (Huefte -> Sohle), Standard-Koerper."""
        return {k: self.metrics[k] for k in self.metrics if k.startswith("leg_")}


# ---------------------------------------------------------------------------
# Erzeugen aus der FBX-Datei
# ---------------------------------------------------------------------------

def build_from_fbx(fbx_path: str) -> ClassicBody:
    """Liest die FBX-Datei und baut den Standard-Koerper (Teil-Koordinaten)."""
    import fbx  # lokal, damit das Modul ohne FBX-Abhaengigkeit importierbar bleibt

    doc = fbx.read_document(fbx_path)
    by_name = {p.name: p for p in doc.parts}

    body = ClassicBody(source=os.path.basename(fbx_path))
    world: Dict[str, Tuple[Tuple[float, float, float],
                            Tuple[float, float, float]]] = {}
    for name in PART_NAMES:
        part = by_name.get(f"{name}_Geo") or by_name.get(name)
        if part is None or not part.positions:
            continue
        lo, hi = part.bbox()
        center = tuple((lo[i] + hi[i]) / 2.0 for i in range(3))
        size = tuple(hi[i] - lo[i] for i in range(3))
        # Mesh in Teil-Koordinaten: Ursprung = Mittelpunkt der Bounding-Box.
        positions = [(p[0] - center[0], p[1] - center[1], p[2] - center[2])
                     for p in part.positions]
        body.meshes[name] = MeshData(
            positions=positions,
            normals=[],
            uvs=list(part.uvs) if len(part.uvs) == len(positions) else [],
            faces=list(part.faces),
            source_version="roblox-classic-mannequin",
        )
        body.sizes[name] = size
        body.positions[name] = center
        world[name] = (lo, hi)

    body.metrics = _derive_metrics(world)
    return body


def _derive_metrics(world: Dict[str, Tuple[Tuple[float, float, float],
                                            Tuple[float, float, float]]]) -> Dict[str, float]:
    """Alle Verhaeltnisse aus den echten Teil-Positionen ableiten.

    Bezugsgroesse ist die **Torso-Hoehe** des Standard-Koerpers
    (Unterkante LowerTorso bis Oberkante UpperTorso). Alles andere wird als
    Anteil davon ausgedrueckt - so skaliert es mit jedem Avatar mit.
    """
    if "UpperTorso" not in world:
        return {}
    ut_lo, ut_hi = world["UpperTorso"]
    lt_lo, _lt_hi = world.get("LowerTorso", (ut_lo, ut_hi))
    torso_top = ut_hi[1]
    torso_bottom = lt_lo[1]
    torso_h = torso_top - torso_bottom
    torso_w = ut_hi[0] - ut_lo[0]
    if torso_h <= 1e-6:
        return {}
    m: Dict[str, float] = {
        "torso_height": torso_h,
        "torso_width": torso_w,
        "torso_depth": ut_hi[2] - ut_lo[2],
    }
    for side in ("Left", "Right"):
        # -- Arm ---------------------------------------------------------
        ua = world.get(f"{side}UpperArm")
        la = world.get(f"{side}LowerArm")
        hand = world.get(f"{side}Hand")
        if ua and la and hand:
            arm_top = ua[1][1]
            elbow = la[1][1]          # Oberkante Unterarm = Ellbogen
            wrist = hand[1][1]        # Oberkante Hand = Handgelenk
            tip = hand[0][1]
            length = arm_top - tip
            m[f"arm_{side.lower()}_top_below_torso"] = (torso_top - arm_top) / torso_h
            m[f"arm_{side.lower()}_upper"] = (arm_top - elbow) / length
            m[f"arm_{side.lower()}_lower"] = (elbow - wrist) / length
            m[f"arm_{side.lower()}_hand"] = (wrist - tip) / length
            m[f"arm_{side.lower()}_length"] = length / torso_h
            m[f"arm_{side.lower()}_width"] = (ua[1][0] - ua[0][0]) / torso_w
            m[f"arm_{side.lower()}_inner_x"] = ua[0][0] / torso_w
        # -- Bein --------------------------------------------------------
        ul = world.get(f"{side}UpperLeg")
        ll = world.get(f"{side}LowerLeg")
        foot = world.get(f"{side}Foot")
        if ul and ll and foot:
            hip = ul[1][1]
            knee = ll[1][1]
            ankle = foot[1][1]
            sole = foot[0][1]
            length = hip - sole
            m[f"leg_{side.lower()}_upper"] = (hip - knee) / length
            m[f"leg_{side.lower()}_lower"] = (knee - ankle) / length
            m[f"leg_{side.lower()}_foot"] = (ankle - sole) / length
            m[f"leg_{side.lower()}_length"] = length / torso_h
            m[f"leg_{side.lower()}_width"] = (ul[1][0] - ul[0][0]) / torso_w
            m[f"leg_{side.lower()}_depth"] = (ul[1][2] - ul[0][2]) / torso_h
            m[f"leg_{side.lower()}_hip_below_torso"] = (torso_top - hip) / torso_h
    # Mittelwerte (links/rechts sind beim Standard-Koerper symmetrisch).
    for prefix, keys in (("arm", ("upper", "lower", "hand", "length", "width",
                                  "top_below_torso")),
                         ("leg", ("upper", "lower", "foot", "length", "width",
                                  "depth", "hip_below_torso"))):
        for key in keys:
            vals = [m[f"{prefix}_{side}_{key}"] for side in ("left", "right")
                    if f"{prefix}_{side}_{key}" in m]
            if vals:
                m[f"{prefix}_{key}"] = sum(vals) / len(vals)
    return m


# ---------------------------------------------------------------------------
# JSON-Zwischenspeicher (damit zur Renderzeit kein FBX-Parser laufen muss)
# ---------------------------------------------------------------------------

def build_standard_from_fbx(fbx_path: str) -> ClassicBody:
    """Liest den R15-Blockkoerper (``BlockyCharacter.fbx``).

    Die Datei legt die 15 Blockteile als Einzelteile nebeneinander ab (kein
    aufgebautes Rig). Fuer die Pipeline zaehlen nur Form und Groesse der
    Teile: jedes Mesh wird auf seine Mitte zentriert, ``sizes`` haelt die
    natuerlichen Maße. Die Lage im Koerper ergibt sich beim Einbau aus den
    kanonischen R15-Anteilen (``avatar_rig``).
    """
    import fbx  # lokal, wie in build_from_fbx

    doc = fbx.read_document(fbx_path)
    by_name = {p.name: p for p in doc.parts}
    body = ClassicBody(source=os.path.basename(fbx_path))
    for name in PART_NAMES:
        part = by_name.get(f"{name}_Geo") or by_name.get(name)
        if part is None or not part.positions:
            continue
        lo, hi = part.bbox()
        center = tuple((lo[i] + hi[i]) / 2.0 for i in range(3))
        size = tuple(hi[i] - lo[i] for i in range(3))
        positions = [(p[0] - center[0], p[1] - center[1], p[2] - center[2])
                     for p in part.positions]
        body.meshes[name] = MeshData(
            positions=positions,
            normals=[],
            uvs=list(part.uvs) if len(part.uvs) == len(positions) else [],
            faces=list(part.faces),
            source_version="roblox-r15-blocky",
        )
        body.sizes[name] = size
        body.positions[name] = (0.0, 0.0, 0.0)
    return body


def dump_json(body: ClassicBody, path: str) -> None:
    payload: Dict[str, Any] = {
        "source": body.source,
        "metrics": {k: round(v, 6) for k, v in body.metrics.items()},
        "parts": {},
    }
    for name, mesh in body.meshes.items():
        payload["parts"][name] = {
            "size": [round(v, 5) for v in body.sizes[name]],
            "position": [round(v, 5) for v in body.positions[name]],
            "vertices": [round(c, 4) for p in mesh.positions for c in p],
            "faces": [i for f in mesh.faces for i in f],
            "uvs": [round(c, 5) for uv in mesh.uvs for c in uv],
        }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    size = os.path.getsize(path)
    print(f"  -> {path} ({size / 1024:.0f} KB)")


def load(path: str = DEFAULT_PATH) -> Optional[ClassicBody]:
    """Laedt den zwischengespeicherten Standard-Koerper (oder ``None``)."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    body = ClassicBody(source=str(payload.get("source") or path))
    body.metrics = {k: float(v) for k, v in (payload.get("metrics") or {}).items()}
    for name, part in (payload.get("parts") or {}).items():
        flat_v = part.get("vertices") or []
        flat_f = part.get("faces") or []
        flat_uv = part.get("uvs") or []
        positions = [(flat_v[i], flat_v[i + 1], flat_v[i + 2])
                     for i in range(0, len(flat_v) - 2, 3)]
        faces = [(flat_f[i], flat_f[i + 1], flat_f[i + 2])
                 for i in range(0, len(flat_f) - 2, 3)]
        uvs = [(flat_uv[i], flat_uv[i + 1]) for i in range(0, len(flat_uv) - 1, 2)]
        body.meshes[name] = MeshData(
            positions=positions, normals=[], uvs=uvs, faces=faces,
            source_version="roblox-classic-mannequin")
        body.sizes[name] = tuple(float(v) for v in (part.get("size") or (1, 1, 1)))
        body.positions[name] = tuple(float(v) for v in (part.get("position") or (0, 0, 0)))
    return body


_CACHE: Dict[str, Optional[ClassicBody]] = {}


def cached(path: str = DEFAULT_PATH) -> Optional[ClassicBody]:
    if path not in _CACHE:
        _CACHE[path] = load(path)
    return _CACHE[path]


def standard_meshes(path: str = STANDARD_PATH) -> Dict[str, MeshData]:
    """R15-Blockteile des Standard-Avatars (Segmentname -> Mesh).

    Leeres Dict, wenn die Referenzdatei fehlt - dann baut die Pipeline die
    fehlenden Teile als Quader gleicher Maße.
    """
    body = cached(path)
    return dict(body.meshes) if body is not None else {}
