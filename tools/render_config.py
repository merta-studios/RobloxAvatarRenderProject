#!/usr/bin/env python3
"""Liest und prueft die zentrale ``Configs.toml`` (frueher: ``username.txt``).

Die Datei ist absichtlich TOML: ``#``-Kommentare bleiben erhalten, Zahlen und
Schalter (``true``/``false``) sind eindeutig, und Python bringt den Parser seit
3.11 in der Standardbibliothek mit (``tomllib``). Fuer aeltere Pythons gibt es
einen kleinen Fallback-Parser fuer den hier benutzten TOML-Teil (Tabellen,
Texte, Zahlen, Schalter, Zahlenlisten).

Beispiel::

    python3 tools/render_config.py --config Configs.toml
    python3 tools/render_config.py --config Configs.toml --github-output
    python3 tools/render_config.py --config Configs.toml --reset-render-flag
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

CONFIG_FILENAME = "Configs.toml"
POSES_FILENAME = "Posen.toml"

# Standardwerte, falls ein Schluessel in der Datei fehlt (die Datei im Repo
# enthaelt immer alle Schluessel - das hier ist die sichere Rueckfallebene).
DEFAULTS: Dict[str, Any] = {
    "username": "",
    "drehung_grad": 0.0,
    "pose": "None",
    "render_bei_aenderung": False,
    "hintergrund_url": "hdri",
    "hintergrund_glossy": False,
    "posen_datei": POSES_FILENAME,
}

KNOWN_KEYS = tuple(DEFAULTS)

# Sonderwerte fuer hintergrund_url:
#   "transparent" -> Film wird durchsichtig (PNG mit Alpha)
#   "hdri"        -> wie frueher: World-HDRI (sunset_jhbcentral_4k.exr) als Hintergrund
TRANSPARENT_WORDS = ("transparent",)
HDRI_WORDS = ("hdri", "hdr", "environment", "env", "")

# Grenzen: seitliche Drehung laut Vorgabe -90 .. 90 Grad.
MIN_DREHUNG = -90.0
MAX_DREHUNG = 90.0
# Bildausschnitt (sichtbar oben/unten), Kamera-Abstand und Avatar-Hoehe
# stehen nicht mehr hier, sondern in jeder Pose in Posen.toml
# (siehe tools/avatar_pose.py: POSE_SETTING_DEFAULTS).


class ConfigError(ValueError):
    """Ungueltige oder fehlende Configs.toml."""


# ---------------------------------------------------------------------------
# TOML laden (tomllib, mit kleinem Fallback fuer Python < 3.11)
# ---------------------------------------------------------------------------

def _strip_comment(line: str) -> str:
    """Entfernt einen ``#``-Kommentar, ohne ``#`` in Texten zu zerstoeren."""
    out = []
    quote = ""
    escaped = False
    for ch in line:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\" and quote:
            out.append(ch)
            escaped = True
            continue
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).strip()


def _parse_scalar(text: str, where: str) -> Any:
    value = text.strip()
    if not value:
        raise ConfigError(f"{where}: Wert fehlt.")
    if value[0] in ('"', "'"):
        if len(value) < 2 or value[-1] != value[0]:
            raise ConfigError(f"{where}: Text ist nicht geschlossen ({value}).")
        return value[1:-1]
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        items: List[Any] = []
        for chunk in inner.split(","):
            items.append(_parse_scalar(chunk, where))
        return items
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        raise ConfigError(
            f"{where}: Wert '{value}' ist weder Text (\"...\"), Zahl noch true/false."
        )


def _parse_simple_toml(text: str) -> Dict[str, Any]:
    """Minimaler TOML-Parser (nur was Configs.toml / Posen.toml brauchen)."""
    root: Dict[str, Any] = {}
    current = root
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw)
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if not name:
                raise ConfigError(f"Zeile {lineno}: leerer Tabellenname [].")
            node = root
            for part in name.split("."):
                part = part.strip().strip('"').strip("'")
                node = node.setdefault(part, {})
                if not isinstance(node, dict):
                    raise ConfigError(
                        f"Zeile {lineno}: '{part}' ist bereits ein Wert, keine Tabelle."
                    )
            current = node
            continue
        if "=" not in line:
            raise ConfigError(f"Zeile {lineno}: erwartet 'schluessel = wert'.")
        key, value = line.split("=", 1)
        key = key.strip().strip('"').strip("'")
        if not key:
            raise ConfigError(f"Zeile {lineno}: Schluessel fehlt.")
        current[key] = _parse_scalar(value, f"Zeile {lineno} ('{key}')")
    return root


def _read_toml(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError as exc:
        raise ConfigError(f"{path} konnte nicht gelesen werden: {exc}") from exc

    try:
        import tomllib  # Python >= 3.11
    except ImportError:  # pragma: no cover - Fallback fuer alte Pythons
        return _parse_simple_toml(text)

    try:
        return tomllib.loads(text)
    except Exception as exc:  # tomllib.TOMLDecodeError
        raise ConfigError(
            f"{path} ist kein gueltiges TOML: {exc}\n"
            "Tipp: Texte in \"Anfuehrungszeichen\", Schalter als true/false."
        ) from exc


# ---------------------------------------------------------------------------
# Typ- und Wertepruefung
# ---------------------------------------------------------------------------

def _number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if isinstance(value, str):
            try:
                return float(value.replace(",", "."))
            except ValueError:
                pass
        raise ConfigError(f"'{name}' muss eine Zahl sein (z. B. 0 oder -40), "
                          f"gefunden: {value!r}.")
    return float(value)


def _flag(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ConfigError(f"'{name}' muss true oder false sein, gefunden: {value!r}.")


def _text(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"'{name}' muss ein Text in Anfuehrungszeichen sein, "
                          f"gefunden: {value!r}.")
    return value.strip()


def _is_transparent(url: str) -> bool:
    return url.strip().lower() in TRANSPARENT_WORDS


def is_hdri(url: str) -> bool:
    return url.strip().lower() in HDRI_WORDS


def load_config(path: str = CONFIG_FILENAME,
                require_username: bool = True) -> Dict[str, Any]:
    """Laedt, normalisiert und prueft die Configs.toml.

    Rueckgabe enthaelt alle Werte aus :data:`DEFAULTS` (typisiert) sowie

    * ``_pfad``        – absoluter Pfad der Config-Datei
    * ``_posen_pfad``  – absoluter Pfad der Posen-Datei
    * ``_warnungen``   – Liste von Hinweisen (z. B. unbekannte Schluessel)
    * ``_transparent`` – True, wenn der Hintergrund transparent sein soll
    * ``_hdri``        – True, wenn das World-HDRI den Hintergrund bildet
    """
    if not os.path.isfile(path):
        raise ConfigError(
            f"{path} fehlt. Bitte die Datei im Repository-Root anlegen "
            "(oder --config auf eine andere Datei zeigen lassen)."
        )

    raw = _read_toml(path)
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: Inhalt muss eine Tabelle sein.")

    warnings: List[str] = []
    for key in raw:
        if key in ("freiraum_oben_prozent", "freiraum_unten_prozent"):
            warnings.append(
                f"Configs.toml: '{key}' gibt es nicht mehr - der "
                "Bildausschnitt steht jetzt in jeder Pose in Posen.toml "
                "(sichtbar_oben_prozent / sichtbar_unten_prozent).")
            continue
        if key not in KNOWN_KEYS:
            warnings.append(
                f"Configs.toml: unbekannter Schluessel '{key}' wird ignoriert "
                f"(bekannt: {', '.join(KNOWN_KEYS)})."
            )

    config: Dict[str, Any] = dict(DEFAULTS)
    for key in KNOWN_KEYS:
        if key in raw:
            config[key] = raw[key]

    config["username"] = _text("username", config["username"]).lstrip("@").strip()
    if require_username and not config["username"]:
        raise ConfigError(
            f"{path}: 'username' ist leer. Bitte einen Roblox-Usernamen eintragen."
        )

    drehung = _number("drehung_grad", config["drehung_grad"])
    if drehung < MIN_DREHUNG or drehung > MAX_DREHUNG:
        raise ConfigError(
            f"'drehung_grad' muss zwischen {MIN_DREHUNG:.0f} und "
            f"{MAX_DREHUNG:.0f} liegen (0 = frontal, 90 = rechte Seite, "
            f"-90 = linke Seite), gefunden: {drehung:g}."
        )
    config["drehung_grad"] = drehung

    pose = _text("pose", config["pose"])
    config["pose"] = "None" if pose.strip().lower() in ("", "none") else pose

    config["render_bei_aenderung"] = _flag(
        "render_bei_aenderung", config["render_bei_aenderung"])
    config["hintergrund_glossy"] = _flag(
        "hintergrund_glossy", config["hintergrund_glossy"])

    url = _text("hintergrund_url", config["hintergrund_url"])
    config["hintergrund_url"] = url
    config["_transparent"] = _is_transparent(url)
    config["_hdri"] = is_hdri(url)
    if not config["_transparent"] and not config["_hdri"]:
        lowered = url.lower()
        looks_like_url = lowered.startswith(("http://", "https://"))
        if not looks_like_url and not os.path.isfile(url):
            warnings.append(
                "Configs.toml: 'hintergrund_url' ist keine http(s)-URL und keine "
                f"vorhandene Datei ({url!r}). Der Render faellt auf das HDRI "
                "zurueck, sofern die Datei nicht heruntergeladen werden kann."
            )

    posen_name = _text("posen_datei", config["posen_datei"]) or POSES_FILENAME
    config_dir = os.path.dirname(os.path.abspath(path))
    config["posen_datei"] = posen_name
    config["_posen_pfad"] = (
        posen_name if os.path.isabs(posen_name)
        else os.path.join(config_dir, posen_name)
    )
    config["_pfad"] = os.path.abspath(path)
    config["_warnungen"] = warnings
    return config


def _replace_render_flag(path: str, value: bool) -> bool:
    """Setzt ``render_bei_aenderung`` in der Datei, ohne Kommentare zu verlieren."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    pattern = re.compile(r"(?m)^(\s*render_bei_aenderung\s*=\s*)(true|false)\b")
    match = pattern.search(text)
    if match is None:
        raise ConfigError(
            f"{path}: 'render_bei_aenderung' wurde nicht gefunden."
        )
    current = match.group(2) == "true"
    if current == value:
        return False
    new_text = text[:match.start()] + match.group(1) + ("true" if value else "false") + text[match.end():]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    return True


def reset_render_flag(path: str = CONFIG_FILENAME) -> bool:
    """Setzt den Render-Schalter nach einem Lauf auf ``false``. True = geaendert."""
    return _replace_render_flag(path, False)


def as_github_output(config: Dict[str, Any]) -> str:
    """Zeilen fuer ``$GITHUB_OUTPUT`` (username + render_jetzt)."""
    # Username unveraendert durchreichen (auch Profil-URLs werden spaeter
    # erkannt), nur Zeilenumbrueche entfernen.
    username = " ".join(str(config.get("username", "")).split()) or "Builderman"
    flag = "true" if config.get("render_bei_aenderung") else "false"
    return f"username={username}\nrender_jetzt={flag}\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Configs.toml lesen/pruefen (Avatar-Render)")
    parser.add_argument("--config", default=CONFIG_FILENAME,
                        help=f"Pfad zur Config-Datei (Default: {CONFIG_FILENAME})")
    parser.add_argument("--github-output", action="store_true",
                        help="username=... und render_jetzt=... fuer $GITHUB_OUTPUT ausgeben")
    parser.add_argument("--print", dest="print_values", action="store_true",
                        help="alle Werte als lesbare Liste ausgeben")
    parser.add_argument("--get", default=None,
                        help="einen einzelnen Wert ausgeben (z. B. username)")
    parser.add_argument("--reset-render-flag", action="store_true",
                        help="render_bei_aenderung in der Datei auf false setzen")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.reset_render_flag:
        try:
            changed = reset_render_flag(args.config)
        except (ConfigError, OSError) as exc:
            print(f"FEHLER: {exc}", file=sys.stderr)
            return 1
        if not args.quiet:
            print("render_bei_aenderung steht jetzt auf false."
                  if changed else "render_bei_aenderung war bereits false.")
        return 0

    try:
        config = load_config(args.config, require_username=False)
    except ConfigError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1

    for warning in config["_warnungen"]:
        print(f"::warning::{warning}", file=sys.stderr)

    if args.github_output:
        sys.stdout.write(as_github_output(config))
        return 0

    if args.get:
        key = args.get
        if key not in config:
            print(f"FEHLER: unbekannter Schluessel '{key}' "
                  f"(bekannt: {', '.join(KNOWN_KEYS)}).", file=sys.stderr)
            return 1
        value = config[key]
        if isinstance(value, bool):
            print("true" if value else "false")
        else:
            print(value)
        return 0

    if args.print_values:
        for key in KNOWN_KEYS:
            print(f"{key} = {config[key]!r}")
        return 0

    print(f"Config OK: {config['username']} | Drehung {config['drehung_grad']:g}° | "
          f"Pose {config['pose']} (Ausschnitt/Kamera stehen in der Pose) | "
          f"Render bei Aenderung: {config['render_bei_aenderung']} | "
          f"Hintergrund: {'transparent' if config['_transparent'] else ('HDRI' if config['_hdri'] else config['hintergrund_url'])}"
          f"{'' if config['_hdri'] or config['_transparent'] else (' (glossy)' if config['hintergrund_glossy'] else ' (matt)')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
