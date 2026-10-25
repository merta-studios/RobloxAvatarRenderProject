"""Erzeugt ``reference/classic_body.json`` aus Roblox' ``ClassicMannequin.fbx``.

Die Referenzdatei kommt aus der offiziellen Doku (Project Files):

    https://prod.docsiteassets.roblox.com/avatar/ClassicMannequin.fbx

``tools/probe_reference.py`` laedt sie nach ``reference/official/``. Dieses
Skript liest die FBX-Datei (``tools/fbx.py``) und schreibt den Standard-Koerper
als kompaktes JSON, das die Pipeline zur Renderzeit ohne FBX-Parser laedt:

    python tools/build_mannequin.py                     # Standardpfade
    python tools/build_mannequin.py --fbx X --out Y

Enthalten sind alle 15 Teile (Mesh in Teil-Koordinaten, Maße, Lage im
Mannequin) sowie die abgeleiteten Verhaeltnisse (``metrics``), die im
Rig-Aufbau verwendet werden.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mannequin  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fbx", default=os.path.join(
        mannequin.ROOT, "reference", "official", "ClassicMannequin.fbx"))
    parser.add_argument("--out", default=mannequin.DEFAULT_PATH)
    args = parser.parse_args(argv)

    if not os.path.exists(args.fbx):
        print(f"FBX fehlt: {args.fbx} - zuerst tools/probe_reference.py laufen "
              "lassen.", file=sys.stderr)
        return 1
    body = mannequin.build_from_fbx(args.fbx)
    if len(body.meshes) != len(mannequin.PART_NAMES):
        print(f"Nur {len(body.meshes)} von {len(mannequin.PART_NAMES)} Teilen "
              "gelesen - Format der FBX-Datei pruefen.", file=sys.stderr)
        return 1
    mannequin.dump_json(body, args.out)
    m = body.metrics
    print(f"Torso {m['torso_width']:.3f} x {m['torso_height']:.3f} x "
          f"{m['torso_depth']:.3f} Studs")
    print(f"Armoberkante {m['arm_top_below_torso'] * 100:.1f} % unter der "
          f"Torso-Oberkante, Beinlaenge {m['leg_length'] * 100:.1f} % der "
          "Torso-Hoehe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
