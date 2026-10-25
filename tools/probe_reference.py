"""Sondiert die Roblox-Endpunkte, die Original-Geometrie liefern.

Laeuft im CI (dort ist das Netz offen) und schreibt ``reference/probe.json``:
welcher Endpunkt liefert was, mit welchem Status und welchem Inhaltstyp.
Damit muss nichts geraten werden - die Pipeline nimmt danach die Quelle, die
tatsaechlich echte Roblox-Geometrie liefert.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_reference import _http, _json  # noqa: E402

OFFICIAL_FILES = {
    "ClassicMannequin.fbx":
        "https://prod.docsiteassets.roblox.com/assets/art/reference-files/ClassicMannequin.fbx",
    "ClassicMannequin_With-Cages.fbx":
        "https://prod.docsiteassets.roblox.com/assets/art/reference-files/ClassicMannequin_With-Cages.fbx",
    "BlockyCharacter.fbx":
        "https://prod.docsiteassets.roblox.com/assets/avatar/dynamic-heads/reference-files/BlockyCharacter.fbx",
}

THUMB_URLS = [
    "https://thumbnails.roblox.com/v1/users/avatar-3d?userIds=1",
    "https://thumbnails.roblox.com/v1/users/avatar-3d?userIds=1&size=420x420&format=Obj",
    "https://thumbnails.roblox.com/v1/users/avatar-3d?userIds=9767435332&size=420x420&format=Obj",
    "https://thumbnails.roblox.com/v1/users/avatar?userIds=1&size=420x420&format=Obj",
]


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "reference"
    os.makedirs(os.path.join(out, "official"), exist_ok=True)
    report = {"files": {}, "thumbnails": {}}

    for name, url in OFFICIAL_FILES.items():
        status, raw = _http(url)
        entry = {"url": url, "http": status, "bytes": len(raw),
                 "head": raw[:40].decode("latin-1")}
        if status == 200 and raw:
            path = os.path.join(out, "official", name)
            with open(path, "wb") as fh:
                fh.write(raw)
            entry["file"] = f"official/{name}"
            entry["is_fbx_binary"] = raw.startswith(b"Kaydara FBX Binary")
            entry["is_zip"] = raw[:2] == b"PK"
            entry["is_ascii_fbx"] = raw[:20].lstrip().startswith(b"; FBX")
            print(f"+ {name}: {len(raw)} Bytes, binary={entry['is_fbx_binary']}, "
                  f"zip={entry['is_zip']}, ascii={entry['is_ascii_fbx']}")
        else:
            print(f"! {name}: HTTP {status}")
        report["files"][name] = entry

    for url in THUMB_URLS:
        status, payload = _json(url)
        text = json.dumps(payload)[:300] if payload is not None else ""
        report["thumbnails"][url] = {"http": status, "body": text}
        print(f"? {url} -> HTTP {status}: {text[:160]}")

    with open(os.path.join(out, "probe.json"), "w") as fh:
        json.dump(report, fh, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
