#!/usr/bin/env python3
"""Lad den kompletten Avatar (Roh-Bytes) fuer Offline-Analysen.

Speichert nach ``debug_dump/``:
    avatar.json          Rohdaten der Avatar-API
    raw/<asset_id>       Rohe Bytes jedes Downloads (RBXM/Mesh/Textur)
    entries.json         Ergebnis von rbxm.extract_assets pro RBXM-Asset
    report.md            Kurzbericht

Der Dump wird vom Workflow "Rig-Debug-Dump" in den Branch
``rig-debug-data`` gepusht, damit die Pipeline offline gegen echte
Roblox-Daten replizierbar ist (kein Netzwerk lokal noetig).
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "tools"))

import rbxm  # noqa: E402
import roblox_api as api  # noqa: E402

OUT_DIR = "debug_dump"
RAW_DIR = os.path.join(OUT_DIR, "raw")
MAX_FILE = 40 * 1024 * 1024  # harte Obergrenze pro Datei


def main() -> int:
    os.makedirs(RAW_DIR, exist_ok=True)
    username = ""
    try:
        with open("username.txt", encoding="utf-8-sig") as fh:
            username = fh.read().strip()
    except OSError:
        pass
    username = api.normalize_username(username or "SunsetSiya")

    client = api.HttpClient(verbose=True)
    user_id, username = api.resolve_user_id(client, username)
    avatar = api.fetch_avatar(client, user_id)
    with open(os.path.join(OUT_DIR, "avatar.json"), "w", encoding="utf-8") as fh:
        json.dump(avatar, fh, indent=2, ensure_ascii=False)

    downloaded: set = set()
    report: list = []

    def fetch(asset_id: int, depth: int = 0, parent: int = 0) -> None:
        if asset_id in downloaded or depth > 3:
            return
        downloaded.add(asset_id)
        dl = api.download_asset(client, asset_id)
        if dl.data is None:
            report.append(f"- {asset_id}: Download fehlgeschlagen ({dl.note[:120]})")
            return
        data = api.maybe_decompress(dl.data)
        if len(data) > MAX_FILE:
            report.append(f"- {asset_id}: zu gross ({len(data)} B), uebersprungen")
            return
        kind = api.classify_asset(data, 0)
        ext = {"rbxm": ".rbxm", "mesh": ".mesh", "texture": ".png"}.get(kind, ".bin")
        path = os.path.join(RAW_DIR, f"{asset_id}{ext}")
        with open(path, "wb") as fh:
            fh.write(data)
        report.append(f"- {asset_id}: {kind}, {len(data)} B"
                      + (f" (von {parent})" if parent else ""))
        if kind == "rbxm":
            try:
                entries = rbxm.extract_assets(data)
            except rbxm.RbxmError as exc:
                report.append(f"  ! RBXM nicht lesbar: {exc}")
                return
            with open(os.path.join(OUT_DIR, f"entries_{asset_id}.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(entries, fh, indent=2, ensure_ascii=False)
            for entry in entries:
                for key in ("mesh_id", "texture_id"):
                    if entry.get(key):
                        fetch(int(entry[key]), depth + 1, asset_id)

    for asset in avatar.get("assets") or []:
        try:
            aid = int(asset.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if not aid:
            continue
        at = (asset.get("assetType") or {}).get("id")
        if at in api.IGNORED_ASSET_TYPES:
            continue
        fetch(aid)

    with open(os.path.join(OUT_DIR, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# Rig-Debug-Dump: {username} (UserId {user_id})\n\n"
                 f"* {len(avatar.get('assets') or [])} Assets in der Avatar-Definition\n"
                 f"* {len(downloaded)} Downloads versucht\n\n"
                 + "\n".join(report) + "\n")
    print(f"OK: {len(downloaded)} Downloads -> {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
