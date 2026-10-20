#!/usr/bin/env python3
"""Diagnose: prüft, ob die Roblox-Endpunkte erreichbar sind und ob der
Open-Cloud-Key die nötigen Rechte für Asset-Downloads hat.

Aufruf:
    python3 tools/diagnose.py --username Builderman
    python3 tools/diagnose.py --username Builderman --asset-id 1818

Ausgabe: Tabelle pro Endpunkt (HTTP-Status) + GitHub-Annotations
(::notice:: / ::warning:: / ::error::), damit man in den Actions-Logs und in
der Zusammenfassung sofort sieht, woran es hängt.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import roblox_api as api  # noqa: E402

IN_GITHUB = os.environ.get("GITHUB_ACTIONS") == "true"


def annotate(level: str, message: str) -> None:
    """GitHub-Annotation ausgeben (ausserhalb von Actions normale Ausgabe)."""
    if IN_GITHUB:
        print(f"::{level}::{message}", flush=True)
    else:
        print(f"[{level}] {message}", flush=True)


def status_of(url: str, headers=None, method: str = "GET", payload=None):
    """Fuehrt eine Anfrage aus und liefert (status, bytes, content_type)."""
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", api.USER_AGENT)
    req.add_header("Accept", "*/*")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
    req.data = data
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read(4096), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(400)
        except Exception:
            body = b""
        return exc.code, body, (exc.headers or {}).get("Content-Type", "")
    except Exception as exc:  # Netzwerkfehler
        return 0, str(exc).encode()[:200], ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Roblox-API-Diagnose")
    parser.add_argument("--username", default="Builderman")
    parser.add_argument("--asset-id", type=int, default=0,
                        help="Asset fuer den Download-Test (0 = erstes Avatar-Asset)")
    parser.add_argument("--summary-file", default=os.environ.get("GITHUB_STEP_SUMMARY", ""))
    args = parser.parse_args(argv)

    key = os.environ.get(api.OPEN_CLOUD_KEY_ENV, "").strip()
    rows = []
    problems = []

    def record(step: str, status: int, detail: str) -> None:
        rows.append((step, status, detail))
        print(f"  {step:<46} HTTP {status or '-'}  {detail}", flush=True)

    print("== Roblox-API-Diagnose ==", flush=True)
    print(f"   Open-Cloud-Key vorhanden: {'ja' if key else 'nein'}", flush=True)

    # 1) Username -> UserId
    status, body, _ = status_of("https://users.roblox.com/v1/usernames/users",
                                method="POST",
                                payload={"usernames": [args.username], "excludeBannedUsers": False})
    user_id = 0
    if status == 200:
        try:
            user_id = int(json.loads(body)["data"][0]["id"])
        except Exception:
            pass
    record("users.roblox.com (Username -> UserId)", status,
           f"UserId {user_id}" if user_id else body[:80].decode(errors="replace"))
    if not user_id:
        problems.append("Username konnte nicht aufgeloest werden.")

    # 2) Avatar-Definition
    assets = []
    if user_id:
        status, body, _ = status_of(f"https://avatar.roblox.com/v1/users/{user_id}/avatar")
        if status == 200:
            try:
                assets = json.loads(body).get("assets") or []
            except Exception:
                pass
        record("avatar.roblox.com (Avatar-Definition)", status,
               f"{len(assets)} Assets" if status == 200 else body[:80].decode(errors="replace"))
        if status != 200:
            problems.append("Avatar-Definition nicht abrufbar (Rate-Limit/Block?).")

    # 3) Asset-Details
    asset_id = args.asset_id or (int(assets[0]["id"]) if assets else 1818)
    status, body, _ = status_of(f"https://economy.roblox.com/v2/assets/{asset_id}/details")
    record(f"economy.roblox.com (Details {asset_id})", status,
           body[:80].decode(errors="replace") if status != 200 else "ok")

    # 4) Asset-Delivery: Open Cloud (mit Key)
    if key:
        status, body, ctype = status_of(
            f"https://apis.roblox.com/asset-delivery-api/v1/assetId/{asset_id}",
            headers={"x-api-key": key})
        kind = "leer"
        if status == 200 and body:
            kind = api.classify_asset(body) + f", {len(body)}B"
        record(f"apis.roblox.com asset-delivery ({asset_id})", status, kind)
        if status in (401, 403):
            problems.append(
                f"Open-Cloud-Key wird abgelehnt (HTTP {status}). Fehlt der Legacy-Scope "
                "legacy-asset:manage?"
            )
    else:
        rows.append(("apis.roblox.com asset-delivery", 0, "uebersprungen (kein Key)"))
        print("  apis.roblox.com asset-delivery                      uebersprungen (kein Key)")

    # 5) Asset-Delivery: Legacy-Fallback
    status, body, ctype = status_of(f"https://assetdelivery.roblox.com/v2/assetId/{asset_id}")
    record(f"assetdelivery.roblox.com v2 ({asset_id})", status,
           (api.classify_asset(body) if status == 200 and body else body[:60].decode(errors="replace")))
    if not key and status in (401, 403):
        problems.append("Ohne Key verweigert Asset Delivery den Download (erwartbar seit 2025).")

    # Zusammenfassung
    print("", flush=True)
    if problems:
        for problem in problems:
            annotate("warning", problem)
    else:
        annotate("notice", f"Alle Checks unauffaellig (Test-Asset {asset_id}).")

    if args.summary_file:
        lines = ["## Roblox-Diagnose", "",
                 f"* Open-Cloud-Key: **{'vorhanden' if key else 'fehlt'}**",
                 f"* Test-Asset: `{asset_id}`", "",
                 "| Check | HTTP | Detail |", "| --- | --- | --- |"]
        for step, status, detail in rows:
            lines.append(f"| {step} | {status or '-'} | {detail} |")
        if problems:
            lines += ["", "**Hinweise:**", ""] + [f"* {p}" for p in problems]
        try:
            with open(args.summary_file, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
