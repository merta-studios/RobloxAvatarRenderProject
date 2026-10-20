#!/usr/bin/env python3
"""Laedt einen Roblox-Avatar und baut daraus eine Blender-Szene (Manifest + OBJs).

Beispiel:
    python3 tools/build_avatar.py --username Builderman --out build
    python3 tools/build_avatar.py --dry-run --out build   # ohne Netzwerk

Ergebnis (im Ausgabeordner):
    manifest.json    Beschreibung der Szene (Parts, Farben, Texturen)
    parts/*.obj      Geometrie (Koerper + echte Roblox-Accessoire-Meshes)
    textures/*       heruntergeladene Texturen (PNG/JPG)
    avatar.json      Rohdaten der Avatar-API (zum Debuggen)
    summary.md       Kurzbericht (fuer GitHub Step Summary)

Das Skript braucht nur die Python-Standardbibliothek.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import avatar_rig  # noqa: E402
import roblox_api as api  # noqa: E402
import roblox_mesh  # noqa: E402

# Diese Koerperteile sind rund und werden weich schattiert, der Rest bleibt flach
# (glatte Normalen auf Quadern wirken sonst wie "Plattenpanzer").
SMOOTH_BODY_PARTS = {"kopf", "kopf_mesh"}

CLOTHING_TARGETS = {
    2: ["torso"],                                  # TShirt
    11: ["torso"],                                 # Shirt
    12: ["links_bein", "rechts_bein"],             # Pants
    18: ["kopf", "kopf_mesh"],                     # Face
    64: ["torso"],                                 # TShirtAccessory
    65: ["torso"],                                 # ShirtAccessory
    66: ["links_bein", "rechts_bein"],             # PantsAccessory
    67: ["torso"],                                 # JacketAccessory
    68: ["torso"],                                 # SweaterAccessory
    69: ["links_bein", "rechts_bein"],             # ShortsAccessory
    70: ["links_bein"],                            # LeftShoeAccessory
    71: ["rechts_bein"],                           # RightShoeAccessory
    72: ["torso"],                                 # DressSkirtAccessory
}


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def asset_type_id(asset: Dict[str, Any], details: Dict[str, Any]) -> int:
    """AssetTypeId aus der Avatar-API oder aus den Asset-Details."""
    at = asset.get("assetType") or {}
    if isinstance(at, dict) and at.get("id"):
        return int(at["id"])
    if details.get("assetTypeId"):
        return int(details["assetTypeId"])
    return 0


def texture_filename(asset_id: int, data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return f"{asset_id}.png"
    if data[:3] == b"\xff\xd8\xff":
        return f"{asset_id}.jpg"
    return f"{asset_id}.bin"


def write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def effective_scales(raw: Any) -> Dict[str, float]:
    """Normalisiert die Scales (fehlende Werte = 1.0 / 0.0)."""
    scales = dict(api.DEFAULT_SCALES)
    if isinstance(raw, dict):
        for key in ("height", "width", "head", "depth", "proportion", "bodyType"):
            if raw.get(key) is not None:
                try:
                    scales[key] = float(raw[key])
                except (TypeError, ValueError):
                    pass
    return scales


def effective_body_colors(raw: Any) -> Dict[str, Any]:
    colors = dict(api.DEFAULT_BODY_COLORS)
    if isinstance(raw, dict):
        for key in list(colors.keys()):
            if raw.get(key) is not None:
                colors[key] = raw[key]
    return colors


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------


def build(username_raw: str, out_dir: str, dry_run: bool = False,
          max_assets: int = 80, verbose: bool = True) -> Dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    parts_dir = os.path.join(out_dir, "parts")
    textures_dir = os.path.join(out_dir, "textures")
    os.makedirs(parts_dir, exist_ok=True)
    os.makedirs(textures_dir, exist_ok=True)

    warnings: List[str] = []
    parts: List[Dict[str, Any]] = []
    textures: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    avatar_raw: Dict[str, Any] = {}
    mesh_parts: List[Tuple[int, int, roblox_mesh.MeshData, str, str]] = []
    pending_textures: List[Tuple[int, int, str]] = []

    # ---- Avatar-Daten -----------------------------------------------------
    if dry_run:
        username = api.normalize_username(username_raw) if username_raw else "Builderman"
        user_id = 0
        avatar_raw = {
            "_source": "dry-run",
            "playerAvatarType": "R15",
            "scales": dict(api.DEFAULT_SCALES),
            "bodyColors": dict(api.DEFAULT_BODY_COLORS),
            "assets": [],
        }
        log("[1/4] Dry-Run: keine Netzwerk-Anfragen, Koerper mit Standardfarben")
    else:
        username = api.normalize_username(username_raw)
        client = api.HttpClient(verbose=verbose)
        if not client.api_key:
            warnings.append(
                "Kein Secret ROBLOX_OPEN_CLOUD_API_KEY gesetzt - Asset-Downloads "
                "laufen nur ueber die oeffentlichen Fallback-Endpunkte und koennen "
                "fehlschlagen (401)."
            )
            log("  ! ROBLOX_OPEN_CLOUD_API_KEY ist nicht gesetzt (Fallback aktiv)")
        else:
            log("  i Open-Cloud-Key gefunden (x-api-key wird mitgeschickt)")

        log(f"[1/4] Roblox-User-ID fuer '{username}' ermitteln ...")
        user_id, username = api.resolve_user_id(client, username)
        log(f"      -> UserId {user_id} ({username})")

        log("[2/4] Avatar-Definition laden ...")
        avatar_raw = api.fetch_avatar(client, user_id)
        if avatar_raw.get("_degraded"):
            warnings.append(
                "Nur 'currently-wearing' verfuegbar - Farben/Scales sind Standardwerte."
            )

        # ---- Assets ---------------------------------------------------------
        assets = avatar_raw.get("assets") or []
        log(f"[3/4] {len(assets)} Assets verarbeiten ...")

        for index, asset in enumerate(assets[:max_assets]):
            try:
                aid = int(asset.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if not aid:
                continue
            label = asset.get("name") or ""
            declared = asset_type_id(asset, {})
            if declared in api.IGNORED_ASSET_TYPES:
                skipped.append({"asset_id": aid, "reason": "Audio/Animation wird nicht gerendert"})
                continue

            details = api.fetch_asset_details(client, aid)
            type_id = asset_type_id(asset, details)
            type_name = api.ASSET_TYPE_NAMES.get(type_id, "Unbekannt")
            if not label:
                label = details.get("name") or type_name

            dl = api.download_asset(client, aid)
            if dl.data is None:
                skipped.append({
                    "asset_id": aid,
                    "name": label,
                    "asset_type": type_name,
                    "reason": "Download fehlgeschlagen",
                    "detail": dl.note[:400],
                })
                log(f"      - {aid} {type_name}: Download fehlgeschlagen ({dl.note[:120]})")
                continue

            kind = api.classify_asset(dl.data, type_id)
            log(f"      - {aid} {type_name:<22} {kind:<8} {len(dl.data)/1024:8.1f} KB via {dl.source}")

            if kind == "mesh":
                try:
                    mesh = roblox_mesh.parse(dl.data)
                except roblox_mesh.MeshParseError as exc:
                    skipped.append({"asset_id": aid, "name": label, "asset_type": type_name,
                                    "reason": f"Mesh nicht lesbar: {exc}"})
                    continue
                if mesh.is_empty:
                    skipped.append({"asset_id": aid, "name": label, "asset_type": type_name,
                                    "reason": "Mesh ohne Faces"})
                    continue
                mesh_parts.append((aid, type_id, mesh, label, type_name))
            elif kind == "texture":
                fname = texture_filename(aid, dl.data)
                with open(os.path.join(textures_dir, fname), "wb") as fh:
                    fh.write(dl.data)
                pending_textures.append((aid, type_id, fname))
                textures.append({"asset_id": aid, "name": label, "asset_type": type_name,
                                 "file": f"textures/{fname}", "type_id": type_id})
            else:
                skipped.append({"asset_id": aid, "name": label, "asset_type": type_name,
                                "reason": f"Dateityp '{kind}' wird nicht unterstuetzt"})

    # ---- Koerper bauen ----------------------------------------------------
    log("[4/4] Rig bauen und OBJ-Dateien schreiben ...")
    scales = effective_scales(avatar_raw.get("scales"))
    body_colors = effective_body_colors(avatar_raw.get("bodyColors"))
    body_parts, metrics = avatar_rig.build_body(body_colors, scales)

    color_lookup = {
        "kopf": body_colors.get("headColorId"),
        "hals": body_colors.get("headColorId"),
    }

    # Texturen den Kleidungs-Parts zuordnen
    texture_for_part: Dict[str, str] = {}
    for tex in textures:
        for target in CLOTHING_TARGETS.get(int(tex["type_id"]), []):
            texture_for_part.setdefault(target, tex["file"])

    for part in body_parts:
        fname = f"{part.name}.obj"
        part.mesh.write_obj(os.path.join(parts_dir, fname))
        entry = {
            "name": part.name,
            "obj": f"parts/{fname}",
            "color": part.color,
            "kind": "body",
            "smooth": part.name in SMOOTH_BODY_PARTS,
            "texture": texture_for_part.get(part.name),
        }
        parts.append(entry)

    # Kopf durch echtes Head-Mesh ersetzen, falls vorhanden
    head_mesh_replacement: Optional[roblox_mesh.MeshData] = None
    for aid, type_id, mesh, label, type_name in mesh_parts:
        if type_id == 17:  # Head
            head_mesh_replacement = mesh
            break

    if head_mesh_replacement is not None:
        size = head_mesh_replacement.size()
        max_dim = max(size) or 1.0
        target = metrics.head_radius * 2.15
        placed = head_mesh_replacement.transform(target / max_dim)
        c = placed.center()
        placed = placed.transform(1.0, (0.0 - c[0], metrics.head_center_y - c[1], 0.0 - c[2]))
        fname = "kopf_mesh.obj"
        placed.write_obj(os.path.join(parts_dir, fname))
        parts = [p for p in parts if p["name"] != "kopf"]
        parts.append({
            "name": "kopf_mesh",
            "obj": f"parts/{fname}",
            "color": api.brick_color_hex(body_colors.get("headColorId", 226)),
            "kind": "body",
            "smooth": True,
            "texture": None,
        })
        warnings.append("Kopf wurde durch das echte Head-Mesh des Avatars ersetzt.")

    # Accessoires platzieren
    accessory_assets = 0
    for aid, type_id, mesh, label, type_name in mesh_parts:
        if type_id == 17:
            continue
        if type_id in (27, 28, 29, 30, 31):
            skipped.append({"asset_id": aid, "name": label, "asset_type": type_name,
                            "reason": "R6-Koerperteil - der Rig bringt eigene Koerperteile mit"})
            continue
        placed, note = avatar_rig.place_accessory(mesh, type_id, metrics)
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (label or type_name))[:28]
        fname = f"accessoire_{aid}_{safe or 'teil'}.obj"
        placed.write_obj(os.path.join(parts_dir, fname))
        parts.append({
            "name": f"{safe or type_name} ({aid})",
            "obj": f"parts/{fname}",
            "color": "#dff4ff",
            "kind": "accessory",
            "smooth": True,
            "asset_id": aid,
            "asset_type": type_name,
            "note": note,
            "texture": None,
        })
        accessory_assets += 1

    # ---- Manifest ---------------------------------------------------------
    bounds_lo = [1e9, 1e9, 1e9]
    bounds_hi = [-1e9, -1e9, -1e9]
    for part in parts:
        path = os.path.join(out_dir, part["obj"])
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("v "):
                        _, x, y, z = line.split()
                        for i, val in enumerate((float(x), float(y), float(z))):
                            bounds_lo[i] = min(bounds_lo[i], val)
                            bounds_hi[i] = max(bounds_hi[i], val)
        except OSError:
            continue
    if bounds_lo[0] > bounds_hi[0]:
        bounds_lo, bounds_hi = [-1, 0, -1], [1, 5, 1]

    manifest = {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "username": username,
        "user_id": user_id,
        "avatar_type": avatar_raw.get("playerAvatarType") or "R15",
        "avatar_source": avatar_raw.get("_source", ""),
        "dry_run": dry_run,
        "scales": scales,
        "body_colors": body_colors,
        "metrics": {
            "height": round(metrics.height, 3),
            "torso_width": round(metrics.torso_width, 3),
            "head_radius": round(metrics.head_radius, 3),
        },
        "bbox": {"min": [round(v, 3) for v in bounds_lo],
                 "max": [round(v, 3) for v in bounds_hi]},
        "parts": parts,
        "textures": textures,
        "skipped": skipped,
        "warnings": warnings,
        "stats": {
            "parts": len(parts),
            "body_parts": len(body_parts),
            "accessories": accessory_assets,
            "textures": len(textures),
            "skipped": len(skipped),
            "assets_seen": len(avatar_raw.get("assets") or []),
        },
    }
    write_json(os.path.join(out_dir, "manifest.json"), manifest)
    write_json(os.path.join(out_dir, "avatar.json"), avatar_raw)
    write_summary(os.path.join(out_dir, "summary.md"), manifest)

    log("")
    log(f"Fertig: {manifest['stats']['parts']} Teile "
        f"({accessory_assets} Accessoires, {len(textures)} Texturen), "
        f"{len(skipped)} Assets uebersprungen")
    log(f"Hoehe: {manifest['metrics']['height']} Studs, "
        f"Bounding-Box: {manifest['bbox']['min']} .. {manifest['bbox']['max']}")
    if warnings:
        for w in warnings:
            log(f"  ! {w}")
    return manifest


def write_summary(path: str, manifest: Dict[str, Any]) -> None:
    s = manifest["stats"]
    lines = [
        f"# Avatar-Render: `{manifest['username']}`",
        "",
        f"* UserId: **{manifest['user_id']}**",
        f"* Avatar-Typ: **{manifest['avatar_type']}**",
        f"* Quelle: `{manifest['avatar_source']}`",
        f"* Teile: **{s['parts']}** (Accessoires: {s['accessories']}, "
        f"Texturen: {s['textures']}, uebersprungen: {s['skipped']})",
        f"* Koerperhoehe: {manifest['metrics']['height']} Studs",
        "",
        "## Teile",
        "",
        "| Teil | Typ | Farbe | Notiz |",
        "| --- | --- | --- | --- |",
    ]
    for part in manifest["parts"]:
        note = (part.get("note") or "").replace("|", "/")
        lines.append(f"| {part['name']} | {part['kind']} | `{part['color']}` | {note} |")
    if manifest["skipped"]:
        lines += ["", "## Uebersprungen", "", "| Asset | Typ | Grund |", "| --- | --- | --- |"]
        for item in manifest["skipped"]:
            lines.append(f"| {item.get('asset_id')} | {item.get('asset_type', '')} | "
                         f"{item.get('reason', '')} |")
    if manifest["warnings"]:
        lines += ["", "## Hinweise", ""]
        lines += [f"* {w}" for w in manifest["warnings"]]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def read_username_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Roblox-Avatar -> Blender-Szene")
    parser.add_argument("--username", default=None,
                        help="Roblox-Username (sonst username.txt)")
    parser.add_argument("--username-file", default="username.txt")
    parser.add_argument("--out", default="build", help="Ausgabeordner (Default: build)")
    parser.add_argument("--dry-run", action="store_true",
                        help="ohne Netzwerk: nur Koerper mit Standardfarben bauen")
    parser.add_argument("--max-assets", type=int, default=80)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    username = args.username or read_username_file(args.username_file)
    if not username and not args.dry_run:
        print("FEHLER: kein Username uebergeben und username.txt ist leer.", file=sys.stderr)
        return 2

    try:
        build(username, args.out, dry_run=args.dry_run, max_assets=args.max_assets,
              verbose=not args.quiet)
    except api.RobloxError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
