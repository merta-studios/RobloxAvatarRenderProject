"""Erzeugt die Referenzgeometrie aus Roblox' offiziellen FBX-Dateien.

Die Referenzdatei kommt aus der offiziellen Doku (Project Files):

    https://prod.docsiteassets.roblox.com/avatar/BlockyCharacter.fbx
    https://prod.docsiteassets.roblox.com/avatar/ClassicMannequin.fbx

``tools/probe_reference.py`` laedt beide nach ``reference/official/``. Dieses
Skript liest sie (``tools/fbx.py``) und schreibt zwei kompakte JSON-Dateien,
die die Pipeline zur Renderzeit ohne FBX-Parser laedt:

* ``reference/standard_r15.json`` - R15-Blockkoerper (Standard-Avatar),
* ``reference/classic_body.json`` - klassischer Koerper (Abgleich der Maße).

    python tools/build_mannequin.py                     # Standardpfade

Enthalten sind je Datei alle 15 Teile (Mesh in Teil-Koordinaten und Maße);
der klassische Koerper traegt zusaetzlich die abgeleiteten Verhaeltnisse
(``metrics``) zum Mass-Abgleich.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mannequin  # noqa: E402


OFFICIAL = os.path.join(mannequin.ROOT, "reference", "official")


def _build(fbx: str, out: str, standard: bool) -> int:
    if not os.path.exists(fbx):
        print(f"FBX fehlt: {fbx} - zuerst tools/probe_reference.py laufen "
              "lassen.", file=sys.stderr)
        return 1
    if standard:
        body = mannequin.build_standard_from_fbx(fbx)
    else:
        body = mannequin.build_from_fbx(fbx)
    if len(body.meshes) != len(mannequin.PART_NAMES):
        print(f"Nur {len(body.meshes)} von {len(mannequin.PART_NAMES)} Teilen "
              f"in {os.path.basename(fbx)} gelesen - Format pruefen.",
              file=sys.stderr)
        return 1
    mannequin.dump_json(body, out)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--classic-fbx",
                        default=os.path.join(OFFICIAL, "ClassicMannequin.fbx"))
    parser.add_argument("--standard-fbx",
                        default=os.path.join(OFFICIAL, "BlockyCharacter.fbx"))
    parser.add_argument("--classic-out", default=mannequin.DEFAULT_PATH)
    parser.add_argument("--standard-out", default=mannequin.STANDARD_PATH)
    args = parser.parse_args(argv)

    rc = _build(args.standard_fbx, args.standard_out, standard=True)
    if rc == 0:
        body = mannequin.cached(args.standard_out)
        sizes = body.sizes if body else {}
        if "UpperTorso" in sizes:
            w, h, d = sizes["UpperTorso"]
            print(f"R15-Blockkoerper: Torso {w:.3f} x {h:.3f} x {d:.3f} Studs, "
                  f"{len(sizes)} Teile")
    rc = _build(args.classic_fbx, args.classic_out, standard=False) or rc
    if rc == 0:
        m = mannequin.cached(args.classic_out).metrics
        print(f"Classic-Koerper: Torso {m['torso_width']:.3f} x "
              f"{m['torso_height']:.3f} x {m['torso_depth']:.3f} Studs, "
              f"Armoberkante {m['arm_top_below_torso'] * 100:.1f} % unter der "
              "Torso-Oberkante")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
