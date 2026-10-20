#!/usr/bin/env python3
"""Temporäres Debug-Werkzeug: Rohdaten eines Avatars dumpen.

Schreibt JSON mit allen RBXM-Einträgen (Klasse, Name, size, position,
Mesh-ID, Textur-ID) sowie den tatsächlichen Mesh-Ausdehnungen pro Asset.
Damit lässt sich prüfen, in welchem Maßstab die Roblox-Bundles ihre
Positionen/Größen liefern.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rbxm  # noqa: E402
import roblox_api as api  # noqa: E402
import roblox_mesh  # noqa: E402


def main() -> int:
    username = sys.argv[1] if len(sys.argv) > 1 else "MertaStudios"
    out = sys.argv[2] if len(sys.argv) > 2 else "debugdump/avatar_debug.json"
    client = api.HttpClient(verbose=True)
    user_id, username = api.resolve_user_id(client, username)
    avatar = api.fetch_avatar(client, user_id)
    result = {"username": username, "user_id": user_id,
              "scales": avatar.get("scales"),
              "body_colors": avatar.get("bodyColors"),
              "assets": []}
    cache = {}

    def download(aid):
        if aid not in cache:
            res = api.download_asset(client, aid)
            data = api.maybe_decompress(res.data) if res.data else None
            cache[aid] = data
        return cache[aid]

    for asset in avatar.get("assets") or []:
        aid = int(asset.get("id") or 0)
        type_id = int((asset.get("assetType") or {}).get("id") or 0)
        info = {"id": aid, "name": asset.get("name"), "type": type_id}
        data = download(aid)
        if data is None:
            info["error"] = "download failed"
            result["assets"].append(info)
            continue
        kind = api.classify_asset(data, type_id)
        info["kind"] = kind
        info["bytes"] = len(data)
        if kind != "rbxm":
            result["assets"].append(info)
            continue
        try:
            entries = rbxm.extract_assets(data)
        except Exception as exc:  # pragma: no cover
            info["error"] = f"rbxm: {exc}"
            result["assets"].append(info)
            continue
        info["entries"] = []
        for entry in entries:
            e = {k: entry.get(k) for k in
                 ("class", "name", "mesh_id", "texture_id", "size", "position")}
            if entry.get("mesh_id"):
                raw = download(int(entry["mesh_id"]))
                if raw:
                    try:
                        mesh = roblox_mesh.parse(raw)
                        e["mesh_size"] = [round(v, 4) for v in mesh.size()]
                        e["mesh_center"] = [round(v, 4) for v in mesh.center()]
                        e["mesh_faces"] = len(mesh.faces)
                    except Exception as exc:
                        e["mesh_error"] = str(exc)
            if entry.get("texture_id"):
                raw = download(int(entry["texture_id"]))
                if raw:
                    e["texture_kind"] = api.classify_asset(raw, 0)
                    e["texture_bytes"] = len(raw)
            info["entries"].append(e)
        result["assets"].append(info)

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)
    print(f"geschrieben: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
