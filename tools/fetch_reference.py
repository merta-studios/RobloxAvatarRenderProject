"""Laedt die **Original-Geometrie** von Roblox als Referenz herunter.

Zwei Quellen:

1. **R15-Block-Meshes** (``R15BlockLeftUpperLeg`` usw.) - die Meshes, aus denen
   die Engine den Standard-Avatar baut. Ohne diese Dateien ist jede
   Standard-Koerper-Nachbildung nur eine Naeherung.
2. **Roblox' eigener 3D-Avatar-Render** (``thumbnails.roblox.com``,
   ``avatar-3d``): Roblox baut den Avatar dort selbst zusammen - inklusive der
   Standard-Teile fuer nicht ausgeruestete Koerperteile - und liefert OBJ, MTL
   und Texturen. Das ist die Ground Truth fuer alle Masse: Torso-Oberkante,
   Schultergelenk, Armlaenge, Beinlaenge.

Das Skript schreibt alles nach ``reference/`` (JSON-Bericht + optional OBJ) und
gibt eine Zusammenfassung aus. Es laeuft im CI (dort ist das Netz offen),
damit die Werte nicht geraten werden muessen::

    python tools/fetch_reference.py --user-id 1234 --out reference

Im Sandbox-Setup ist nur GitHub erreichbar - deshalb der Umweg ueber den
Workflow ``reference.yml``, der das Ergebnis in den Branch committet.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import roblox_api  # noqa: E402
import roblox_mesh  # noqa: E402
import avatar_rig  # noqa: E402

CDN_HOSTS = [f"https://t{i}.rbxcdn.com" for i in range(8)]


# ---------------------------------------------------------------------------
# HTTP (bewusst ohne Abhaengigkeit von requests)
# ---------------------------------------------------------------------------

def _http(url: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None,
          timeout: float = 60.0) -> Tuple[int, bytes]:
    import urllib.error
    import urllib.request

    data = None
    headers = {"User-Agent": "Mozilla/5.0 (reference-fetch)",
               "Accept": "*/*"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # pragma: no cover - Netz
        return exc.code, exc.read() if exc.fp else b""
    except Exception as exc:  # pragma: no cover - Netz
        return 0, str(exc).encode("utf-8")


def _json(url: str, method: str = "GET",
          payload: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
    status, raw = _http(url, method, payload)
    try:
        return status, json.loads(raw.decode("utf-8"))
    except Exception:
        return status, None


# ---------------------------------------------------------------------------
# 1) R15-Block-Meshes des Standard-Avatars
# ---------------------------------------------------------------------------

def fetch_standard_meshes(out_dir: str, log: List[str]) -> Dict[str, Any]:
    mesh_dir = os.path.join(out_dir, "meshes")
    os.makedirs(mesh_dir, exist_ok=True)
    report: Dict[str, Any] = {}
    for name, mesh_id in avatar_rig.STANDARD_BODY_MESH_IDS.items():
        status, raw = _http(f"https://assetdelivery.roblox.com/v1/asset/?id={mesh_id}")
        entry: Dict[str, Any] = {"asset_id": mesh_id, "http": status,
                                 "bytes": len(raw)}
        if status != 200 or not raw:
            report[name] = entry
            log.append(f"  ! {name}: HTTP {status}")
            continue
        path = os.path.join(mesh_dir, f"{name}.mesh")
        with open(path, "wb") as fh:
            fh.write(raw)
        try:
            mesh = roblox_mesh.parse(roblox_api.maybe_decompress(raw))
            lo, hi = mesh.bbox()
            size = [hi[i] - lo[i] for i in range(3)]
            entry.update({
                "file": f"meshes/{name}.mesh",
                "faces": len(mesh.faces),
                "vertices": len(mesh.positions),
                "version": mesh.source_version,
                "bbox": [list(lo), list(hi)],
                "size": size,
                "center": mesh.center(),
            })
            log.append(f"  + {name}: {len(mesh.faces)} Faces, "
                       f"{size[0]:.3f} x {size[1]:.3f} x {size[2]:.3f}")
        except Exception as exc:
            entry["error"] = str(exc)
            log.append(f"  ! {name}: {exc}")
        report[name] = entry
    return report


# ---------------------------------------------------------------------------
# 2) Roblox' eigener 3D-Avatar-Render (OBJ + MTL + Texturen)
# ---------------------------------------------------------------------------

def _decode_model_info(payload: Any) -> Optional[Dict[str, Any]]:
    """3D-Thumbnail-Antwort in {obj, mtl, textures, aabb} uebersetzen."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return None
    if not isinstance(payload, dict):
        return None
    if "obj" in payload:
        return payload
    for key in ("imageUrl", "obj", "model", "data"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip().startswith("{"):
            try:
                inner = json.loads(value)
            except Exception:
                continue
            if isinstance(inner, dict) and "obj" in inner:
                return inner
        if isinstance(value, dict) and "obj" in value:
            return value
    return None


def fetch_avatar_3d(user_id: int, out_dir: str, log: List[str],
                    max_obj_bytes: int = 12_000_000) -> Dict[str, Any]:
    info: Dict[str, Any] = {"user_id": user_id, "attempts": []}
    model: Optional[Dict[str, Any]] = None
    for url in (f"https://thumbnails.roblox.com/v1/users/avatar-3d?userIds={user_id}",
                f"https://thumbnails.roblox.com/v1/users/avatar?userIds={user_id}"
                "&size=720x720&format=Png&isCircular=false",
                f"https://thumbnails.roblox.com/v1/users/avatar?userId={user_id}"
                "&size=420x420&format=Png&isCircular=false"):
        status, payload = _json(url)
        info["attempts"].append({"url": url, "http": status})
        model = _decode_model_info(payload)
        log.append(f"  . {url.split('?')[0]} -> HTTP {status}"
                   f"{' (3D-Modell)' if model else ''}")
        if model:
            break
        # 2D-Thumbnail: kein Modell, aber ein Bild als Vergleichsvorlage.
        if isinstance(payload, dict) and payload.get("data"):
            png_url = payload["data"][0].get("imageUrl")
            if png_url:
                status_png, raw_png = _http(png_url)
                if status_png == 200 and raw_png:
                    os.makedirs(os.path.join(out_dir, "avatar2d"), exist_ok=True)
                    with open(os.path.join(out_dir, "avatar2d", "avatar.png"),
                              "wb") as fh:
                        fh.write(raw_png)
                    info["avatar2d"] = "avatar2d/avatar.png"
                    log.append("  + 2D-Avatar-Thumbnail gespeichert")
    if not model:
        return info

    info["model"] = {k: model.get(k) for k in ("obj", "mtl", "textures", "aabb")}
    os.makedirs(os.path.join(out_dir, "avatar3d"), exist_ok=True)

    def _download(hash_str: str, filename: str) -> Optional[bytes]:
        for host in CDN_HOSTS:
            status, raw = _http(f"{host}/{hash_str}")
            if status == 200 and raw:
                with open(os.path.join(out_dir, "avatar3d", filename), "wb") as fh:
                    fh.write(raw)
                return raw
        return None

    obj_name = str(model.get("obj") or "")
    if obj_name:
        raw = _download(obj_name, f"{obj_name}.obj")
        if raw is not None:
            info["obj_bytes"] = len(raw)
            if len(raw) <= max_obj_bytes:
                info["obj_file"] = f"avatar3d/{obj_name}.obj"
                info["components"] = summarize_obj(raw.decode("utf-8", "replace"))
            log.append(f"  + OBJ {len(raw)} Bytes, "
                       f"{len(info.get('components') or [])} Komponenten")
    mtl_name = str(model.get("mtl") or "")
    if mtl_name:
        raw = _download(mtl_name, f"{mtl_name}.mtl")
        if raw is not None:
            info["mtl_file"] = f"avatar3d/{mtl_name}.mtl"
    for tex in (model.get("textures") or [])[:8]:
        if isinstance(tex, str):
            _download(tex, f"{tex}.png")
    return info


# ---------------------------------------------------------------------------
# OBJ-Analyse: zusammenhaengende Komponenten = Koerperteile
# ---------------------------------------------------------------------------

def summarize_obj(text: str) -> List[Dict[str, Any]]:
    """Zerlegt ein OBJ in zusammenhaengende Komponenten (je Koerperteil eine).

    Liefert pro Komponente Bounding-Box, Groesse, Mittelpunkt und Anzahl
    Vertices - damit laesst sich exakt ablesen, wo Roblox selbst z. B. die
    Standard-Beine platziert und wie hoch die Schulter sitzt.
    """
    verts: List[Tuple[float, float, float]] = []
    faces: List[Tuple[int, int, int]] = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "v" and len(parts) >= 4:
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif parts[0] == "f" and len(parts) >= 4:
            idx = []
            for token in parts[1:4]:
                idx.append(int(token.split("/")[0]) - 1)
            if all(0 <= i < len(verts) for i in idx):
                faces.append((idx[0], idx[1], idx[2]))
    if not faces:
        return []

    parent = list(range(len(verts)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for a, b, c in faces:
        union(a, b)
        union(a, c)

    groups: Dict[int, List[int]] = {}
    for i in range(len(verts)):
        groups.setdefault(find(i), []).append(i)

    out: List[Dict[str, Any]] = []
    used = {root: False for root in groups}
    for a, _b, _c in faces:
        used[find(a)] = True
    for root, ids in groups.items():
        if not used.get(root):
            continue
        pts = [verts[i] for i in ids]
        lo = [min(p[k] for p in pts) for k in range(3)]
        hi = [max(p[k] for p in pts) for k in range(3)]
        out.append({
            "vertices": len(pts),
            "bbox": [lo, hi],
            "size": [round(hi[k] - lo[k], 4) for k in range(3)],
            "center": [round((hi[k] + lo[k]) / 2.0, 4) for k in range(3)],
        })
    out.sort(key=lambda c: (-c["size"][1], c["center"][1]))
    return out


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", type=int, default=0)
    ap.add_argument("--out", default="reference")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    log: List[str] = []
    report: Dict[str, Any] = {}

    log.append("[1/2] R15-Block-Meshes des Standard-Avatars ...")
    report["standard_meshes"] = fetch_standard_meshes(args.out, log)

    if args.user_id:
        log.append("[2/2] Roblox-3D-Render des Avatars (Ground Truth) ...")
        report["avatar_3d"] = fetch_avatar_3d(args.user_id, args.out, log)
    else:
        log.append("[2/2] uebersprungen (keine --user-id)")

    with open(os.path.join(args.out, "report.json"), "w") as fh:
        json.dump(report, fh, indent=1, sort_keys=True)
    print("\n".join(log))
    print(f"\nBericht: {args.out}/report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
