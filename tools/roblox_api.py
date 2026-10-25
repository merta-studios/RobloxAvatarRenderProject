"""Roblox-API-Zugriff ohne zusaetzliche Abhaengigkeiten (nur Standardbibliothek).

Zwei Klassen von Endpunkten werden benutzt:

1. **Oeffentliche Web-APIs** (kein Key noetig):
   * `https://users.roblox.com/v1/usernames/users`  - Username -> UserId
   * `https://avatar.roblox.com/v1/users/{id}/avatar` - kompletter Avatar
     (Assets, Body-Colors, Scales, Avatar-Typ)
   * `https://avatar.roblox.com/v1/users/{id}/currently-wearing` - Fallback,
     nur Asset-IDs ohne Farben
   * `https://economy.roblox.com/v2/assets/{id}/details` - Asset-Typ

2. **Asset-Delivery** fuer die Binaerdateien (Mesh/Textur). Seit April 2025
   verlangt Roblox hier Authentifizierung. Wir probieren der Reihe nach:
   * Open Cloud: `https://apis.roblox.com/asset-delivery-api/v1/assetId/{id}`
     mit Header `x-api-key` (Secret `ROBLOX_OPEN_CLOUD_API_KEY`).
     Wichtig: Dieser Endpunkt braucht den Legacy-Scope `legacy-asset:manage`,
     `asset:read` alleine genuegt NICHT (Stand 2025/2026).
   * Legacy: `https://assetdelivery.roblox.com/v2/assetId/{id}` (klappt bei
     manchen Assets noch ohne Auth bzw. mit Key)
   * Legacy v1: `https://assetdelivery.roblox.com/v1/asset/?id={id}`

Faellt alles aus, wird das Asset uebersprungen (Warnung im Log) - der Workflow
rendert dann trotzdem einen Avatar aus den Teilen, die verfuegbar waren.
"""

from __future__ import annotations

import gzip
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

USER_AGENT = (
    "RobloxAvatarRenderProject/1.0 "
    "(+https://github.com/merta-studios/RobloxAvatarRenderProject)"
)

OPEN_CLOUD_KEY_ENV = "ROBLOX_OPEN_CLOUD_API_KEY"

# AssetType-IDs von Roblox (https://robloxapi.github.io/ref/enum/AssetType.html)
ASSET_TYPE_NAMES = {
    1: "Image", 2: "TShirt", 3: "Audio", 4: "Mesh", 5: "Lua", 8: "Hat",
    9: "Place", 10: "Model", 11: "Shirt", 12: "Pants", 13: "Decal",
    17: "Head", 18: "Face", 19: "Gear", 21: "Badge", 24: "Animation",
    27: "Torso", 28: "RightArm", 29: "LeftArm", 30: "LeftLeg", 31: "RightLeg",
    32: "Package", 34: "GamePass", 38: "Plugin", 40: "MeshPart",
    41: "HairAccessory", 42: "FaceAccessory", 43: "NeckAccessory",
    44: "ShoulderAccessory", 45: "FrontAccessory", 46: "BackAccessory",
    47: "WaistAccessory", 48: "ClimbAnimation",
    50: "FallAnimation", 51: "IdleAnimation", 52: "JumpAnimation",
    53: "RunAnimation", 54: "SwimAnimation", 55: "WalkAnimation",
    61: "EmoteAnimation",
    64: "TShirtAccessory", 65: "ShirtAccessory", 66: "PantsAccessory",
    67: "JacketAccessory", 68: "SweaterAccessory", 69: "ShortsAccessory",
    70: "LeftShoeAccessory", 71: "RightShoeAccessory", 72: "DressSkirtAccessory",
    76: "EyebrowAccessory", 77: "EyelashAccessory", 78: "MoodAnimation",
    79: "DynamicHead",
}

TEXTURE_ASSET_TYPES = {1, 2, 11, 12, 13, 18, 64, 65, 66, 67, 68, 69, 70, 71, 72}
# Alles, was als Mesh am Koerper landet
MESH_ASSET_TYPES = {4, 8, 19, 41, 42, 43, 44, 45, 46, 47, 17, 27, 28, 29, 30, 31,
                    76, 77, 79, 40}
# Audio (3) und alle Animations-/Emote-Typen: werden nie heruntergeladen,
# nur gezaehlt (siehe stats["ignored_animations"] in build_avatar.py).
IGNORED_ASSET_TYPES = {3, 24, 48, 49, 50, 51, 52, 53, 54, 55, 56, 61, 78}
# Accessoire-Kategorien (deutsch) -> AssetTypeIds. "Ausrüstung" (Gear, 19)
# wird bewusst NICHT geladen: Schwert & Co. gehoeren nicht auf ein
# GFX-Portraet und wuerden sonst in der rechten Hand landen.
GEAR_ASSET_TYPE = 19
EXCLUDED_ASSET_TYPES = {GEAR_ASSET_TYPE}
ACCESSORY_CATEGORIES = {
    8: "Kopf", 41: "Kopf", 79: "Kopf",
    43: "Hals",
    42: "Gesicht", 76: "Gesicht", 77: "Gesicht",
    44: "Schultern",
    45: "Vorderseite",
    46: "Rücken",
    47: "Taille",
    19: "Ausrüstung",
}


#: Animationstypen, die eine *Pose* beschreiben (nicht nur eine Bewegung).
#:   56 = PoseAnimation (die "Pose" aus dem Avatar-Editor),
#:   51 = IdleAnimation (Standpose eines Animationspakets),
#:   48-55 = uebrige Clip-Typen eines Animationspakets.
POSE_ANIMATION_TYPES = {56, 51}
ANIMATION_PACK_TYPES = {48, 49, 50, 51, 52, 53, 54, 55, 56}
EMOTE_ANIMATION_TYPE = 61


class RobloxError(RuntimeError):
    """Fehler, der den Lauf beenden sollte (z. B. unbekannter Username)."""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


@dataclass
class Response:
    status: int
    body: bytes
    headers: Dict[str, str] = field(default_factory=dict)
    url: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8", errors="replace"))


class HttpClient:
    """Minimaler HTTP-Client mit Retries, Timeout und optionalem API-Key."""

    def __init__(self, api_key: Optional[str] = None, timeout: float = 45.0,
                 retries: int = 3, retry_delay: float = 1.5, verbose: bool = True):
        self.api_key = api_key if api_key is not None else os.environ.get(OPEN_CLOUD_KEY_ENV, "")
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self.verbose = verbose

    # -- interne Helfer -----------------------------------------------------

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _open(self, url: str, headers: Dict[str, str], method: str = "GET",
              data: Optional[bytes] = None) -> Response:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return Response(resp.status, resp.read(), dict(resp.headers), url)

    def get(self, url: str, headers: Optional[Dict[str, str]] = None,
            allow_status: Iterable[int] = ()) -> Response:
        all_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, */*",
            "Accept-Encoding": "identity",
        }
        if headers:
            all_headers.update(headers)
        last_error: Optional[str] = None
        for attempt in range(1, self.retries + 1):
            try:
                return self._open(url, all_headers)
            except urllib.error.HTTPError as exc:
                body = b""
                try:
                    body = exc.read()
                except Exception:  # pragma: no cover
                    pass
                if exc.code in allow_status:
                    return Response(exc.code, body, dict(exc.headers or {}), url)
                last_error = f"HTTP {exc.code}: {_short(body)}"
                # 4xx (ausser 429) nicht wiederholen
                if exc.code not in (408, 425, 429) and exc.code < 500:
                    return Response(exc.code, body, dict(exc.headers or {}), url)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self.retries:
                delay = self.retry_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.4)
                self._log(f"    ... Netzwerkfehler ({last_error}), neuer Versuch in {delay:.1f}s")
                time.sleep(delay)
        raise RobloxError(f"Anfrage fehlgeschlagen: {url} ({last_error})")

    def post_json(self, url: str, payload: Dict[str, Any],
                  headers: Optional[Dict[str, str]] = None) -> Response:
        all_headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if headers:
            all_headers.update(headers)
        data = json.dumps(payload).encode("utf-8")
        return self._open(url, all_headers, method="POST", data=data)


def _short(body: bytes, limit: int = 160) -> str:
    text = body.decode("utf-8", errors="replace").strip().replace("\n", " ")
    return text[:limit]


# ---------------------------------------------------------------------------
# Username -> UserId
# ---------------------------------------------------------------------------


def normalize_username(raw: str) -> str:
    """Entfernt Whitespace/Newlines und validiert Roblox-Username-Regeln."""
    name = (raw or "").strip().strip("\ufeff").strip()
    # Manche Leute schreiben die URL in die Datei
    m = re.search(r"roblox\.com/users/(\d+)", name)
    if m:
        return m.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", name or ""):
        raise RobloxError(
            f"Ungueltiger Roblox-Username: {raw!r} "
            "(erlaubt sind 3-20 Zeichen: Buchstaben, Ziffern, Unterstrich)"
        )
    return name


def resolve_user_id(client: HttpClient, username: str) -> Tuple[int, str]:
    """Liefert (userId, exakt geschriebener Name). Gross-/Kleinschreibung egal."""
    if username.isdigit():
        return int(username), username

    # 1) Offizielle Users-API
    try:
        resp = client.post_json(
            "https://users.roblox.com/v1/usernames/users",
            {"usernames": [username], "excludeBannedUsers": False},
        )
        if resp.ok:
            data = resp.json().get("data") or []
            if data:
                return int(data[0]["id"]), data[0].get("name", username)
    except RobloxError as exc:
        print(f"  ! users.roblox.com nicht erreichbar: {exc}", flush=True)

    # 2) Legacy-Endpunkt (Fallback)
    url = "https://api.roblox.com/users/get-by-username?username=" + urllib.parse.quote(username)
    resp = client.get(url, allow_status=(404,))
    if resp.ok:
        try:
            data = resp.json()
            if data.get("Id"):
                return int(data["Id"]), data.get("Username", username)
        except json.JSONDecodeError:
            pass

    raise RobloxError(f"Roblox-Username {username!r} wurde nicht gefunden.")


# ---------------------------------------------------------------------------
# Avatar-Daten
# ---------------------------------------------------------------------------


DEFAULT_BODY_COLORS = {
    "headColorId": 226,
    "torsoColorId": 23,
    "rightArmColorId": 226,
    "leftArmColorId": 226,
    "rightLegColorId": 9,
    "leftLegColorId": 9,
}

DEFAULT_SCALES = {"height": 1.0, "width": 1.0, "head": 1.0, "depth": 1.0,
                  "proportion": 0.0, "bodyType": 0.0}


def fetch_avatar(client: HttpClient, user_id: int) -> Dict[str, Any]:
    """Holt die komplette Avatar-Definition (Assets + Farben + Scales)."""
    candidates = [
        f"https://avatar.roblox.com/v1/users/{user_id}/avatar",
        f"https://avatar.roblox.com/v2/avatar/users/{user_id}/avatar",
    ]
    last_status = None
    for url in candidates:
        resp = client.get(url, allow_status=(400, 403, 404, 429))
        if resp.ok:
            data = resp.json()
            data.setdefault("_source", url)
            return data
        last_status = resp.status
        print(f"  ! {url} -> HTTP {resp.status}", flush=True)

    # Letzter Ausweg: nur die getragenen Asset-IDs, ohne Farben/Scales
    url = f"https://avatar.roblox.com/v1/users/{user_id}/currently-wearing"
    resp = client.get(url, allow_status=(400, 403, 404, 429))
    if resp.ok:
        ids = resp.json().get("assetIds") or []
        print("  ! Nutze currently-wearing-Fallback (ohne Farben/Scales)", flush=True)
        return {
            "_source": url,
            "_degraded": True,
            "playerAvatarType": "R15",
            "scales": dict(DEFAULT_SCALES),
            "bodyColors": dict(DEFAULT_BODY_COLORS),
            "assets": [{"id": aid, "assetType": {}} for aid in ids],
        }

    raise RobloxError(
        f"Avatar-Daten fuer UserId {user_id} konnten nicht geladen werden "
        f"(letzter HTTP-Status: {last_status})."
    )


def fetch_asset_details(client: HttpClient, asset_id: int) -> Dict[str, Any]:
    """Asset-Typ/Name, damit wir Mesh und Textur unterscheiden koennen."""
    resp = client.get(f"https://economy.roblox.com/v2/assets/{asset_id}/details",
                      allow_status=(400, 403, 404, 429))
    if resp.ok:
        try:
            data = resp.json()
            return {
                "assetTypeId": int(data.get("AssetTypeId") or 0),
                "name": data.get("Name") or "",
            }
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return {"assetTypeId": 0, "name": ""}


# ---------------------------------------------------------------------------
# Fehlende Koerperteile aus demselben Koerperpaket
# ---------------------------------------------------------------------------

#: AssetTypeIds der R15-Koerperteile (siehe BODY_PART_TYPES in build_avatar).
BODY_PART_TYPE_IDS = (27, 28, 29, 30, 31)
BODY_PART_TYPE_NAMES = {
    27: "Torso", 28: "RightArm", 29: "LeftArm", 30: "LeftLeg", 31: "RightLeg",
}


def _asset_type_id(asset: Dict[str, Any]) -> int:
    """AssetTypeId aus dem Avatar-Eintrag (``assetType.id``)."""
    at = asset.get("assetType") or {}
    if isinstance(at, dict) and at.get("id"):
        try:
            return int(at["id"])
        except (TypeError, ValueError):
            return 0
    return 0


def _json_or_none(resp: Any) -> Any:
    if not getattr(resp, "ok", False):
        return None
    try:
        return resp.json()
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _catalog_json(client: HttpClient, url: str, log: List[str]) -> Any:
    """JSON aus dem Katalog holen; Status wird fuer die Diagnose protokolliert."""
    resp = client.get(url, allow_status=(400, 401, 403, 404, 429))
    status = getattr(resp, "status", 0)
    log.append(f"{url} -> HTTP {status}")
    if not resp.ok:
        return None
    try:
        return resp.json()
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def asset_bundles(client: HttpClient, asset_id: int,
                  log: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Koerperpakete (Bundles), die dieses Asset enthalten."""
    log = log if log is not None else []
    for url in (f"https://catalog.roblox.com/v1/assets/{asset_id}/bundles",
                f"https://catalog.roblox.com/v2/assets/{asset_id}/bundles"):
        data = _catalog_json(client, url, log)
        entries = (data or {}).get("data") if isinstance(data, dict) else data
        if isinstance(entries, list) and entries:
            return [e for e in entries if isinstance(e, dict) and e.get("id")]
    return []


def bundle_items(client: HttpClient, bundle_id: int,
                 log: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Teile eines Koerperpakets (Assets mit Typ und Namen)."""
    log = log if log is not None else []
    data = _catalog_json(
        client, f"https://catalog.roblox.com/v1/bundles/{bundle_id}/details", log)
    items = (data or {}).get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            item_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if item_id:
            out.append({"id": item_id, "name": item.get("name") or "",
                        "type": str(item.get("type") or "")})
    return out


def missing_body_parts(client: HttpClient, assets: List[Dict[str, Any]],
                       warnings: Optional[List[str]] = None,
                       log: Optional[List[str]] = None
                       ) -> List[Dict[str, Any]]:
    """Fehlende Koerperteile aus dem getragenen Koerperpaket nachladen.

    Viele Avatare tragen nur einzelne Teile eines Pakets (z. B. Torso + Arme
    aus "Roblox 2.0", aber keine Beine). Roblox ergaenzt die fehlenden Teile
    beim Rendern aus demselben Paket - sonst muessten wir sie als Quader
    erfinden ("Beine sind einfach nur eckige Quader").

    Reihenfolge: Paket des Torso-Assets (``/assets/{id}/bundles``), dann
    Katalogsuche nach dem Namen des Torso-Assets. Schlaegt beides fehl, bleibt
    es bei den R15-Quadern (Hinweis, kein Fehler).

    Rueckgabe: Liste ``{"id", "name", "asset_type_id"}``.
    """
    log = log if log is not None else []
    present = set()
    torso_asset = None
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        try:
            aid = int(asset.get("id") or 0)
        except (TypeError, ValueError):
            continue
        type_id = _asset_type_id(asset)
        if type_id in BODY_PART_TYPE_IDS:
            present.add(type_id)
            if type_id == 27:
                torso_asset = aid
    fehlend = [t for t in BODY_PART_TYPE_IDS if t not in present]
    if not fehlend or torso_asset is None:
        return []

    # Teile-Namen statt Typ-Abfragen: der Katalog nennt keine AssetTypeIds,
    # und die Typ-Abfrage je Teil kostet Requests (429-Ratelimit). Der Name
    # eines R15-Koerperteils endet immer auf Torso/Arm/Leg/Head.
    NAME_TO_TYPE = (
        ("Left Leg", 30), ("Right Leg", 31), ("Torso", 27),
        ("Left Arm", 29), ("Right Arm", 28), ("Head", 17),
    )

    def _type_from_name(name: str) -> int:
        text = (name or "").strip()
        for suffix, type_id in NAME_TO_TYPE:
            if text.endswith(suffix):
                return type_id
        return 0

    found: Dict[int, Dict[str, Any]] = {}
    seen: set = set()
    # 1) Teile des Koerperpakets, zu dem der Torso gehoert.
    for bundle in asset_bundles(client, torso_asset, log)[:4]:
        try:
            bundle_id = int(bundle.get("id"))
        except (TypeError, ValueError):
            continue
        items = bundle_items(client, bundle_id, log)
        log.append(f"Paket {bundle_id}: {len(items)} Teile "
                   + ", ".join(f"{i['name']}[{i['type']}]" for i in items[:12]))
        for item in items:
            if not item["id"] or item["id"] in seen:
                continue
            type_id = _type_from_name(item["name"])
            if not type_id:
                type_id = int(fetch_asset_details(client, item["id"])
                              .get("assetTypeId") or 0)
            seen.add(item["id"])
            if type_id in fehlend and type_id not in found:
                found[type_id] = {"id": item["id"], "name": item["name"],
                                  "asset_type_id": type_id}
        if len(found) >= len(fehlend):
            break

    result = [found[t] for t in BODY_PART_TYPE_IDS if t in found]
    if warnings is not None:
        if result:
            warnings.append(
                "Fehlende Koerperteile aus dem getragenen Koerperpaket "
                "nachgeladen: "
                + ", ".join(BODY_PART_TYPE_NAMES.get(f["asset_type_id"], "?")
                            for f in result) + ".")
            rest = [t for t in fehlend if t not in found]
            if rest:
                warnings.append(
                    "Nicht ermittelbar (bleiben R15-Quader): "
                    + ", ".join(BODY_PART_TYPE_NAMES.get(t, str(t)) for t in rest)
                    + ".")
        else:
            warnings.append(
                "Avatar traegt unvollstaendige Koerperteile ("
                + ", ".join(BODY_PART_TYPE_NAMES.get(t, str(t)) for t in fehlend)
                + ") - Paket nicht ermittelbar, die Teile werden als "
                "R15-Quader erzeugt.")
    return result


# ---------------------------------------------------------------------------
# Ausgeruestete Pose des Accounts
# ---------------------------------------------------------------------------


def equipped_pose_candidates(avatar: Dict[str, Any],
                             warnings: Optional[List[str]] = None
                             ) -> List[Dict[str, Any]]:
    """Kandidaten fuer die **ausgeruestete Pose** aus den Avatar-Daten.

    Reihenfolge (erster Treffer gewinnt):

    1. ``PoseAnimation`` (56) bzw. ``IdleAnimation`` (51) aus den
       ausgeruesteten Assets - das ist die "Pose", die der Avatar-Editor
       unter *Posen* fuehrt und die Roblox auf dem Profilbild zeigt.
    2. das ausgeruestete Emote (Slot 1, danach die uebrigen Slots) -
       Roblox nutzt genau dieses Emote fuer das Profilbild, wenn keine
       eigene Pose gewaehlt wurde.

    Rueckgabe: Liste aus ``{"asset_id", "kind", "name", "source"}`` mit
    ``kind`` = ``"pose"`` | ``"idle"`` | ``"emote"``.
    """
    avatar = avatar or {}
    found: List[Dict[str, Any]] = []

    def push(asset_id: Any, kind: str, source: str, name: str = "") -> None:
        try:
            aid = int(asset_id or 0)
        except (TypeError, ValueError):
            return
        if aid <= 0:
            return
        if any(item["asset_id"] == aid for item in found):
            return
        found.append({"asset_id": aid, "kind": kind, "source": source,
                      "name": name or ""})

    assets = [a for a in (avatar.get("assets") or []) if isinstance(a, dict)]
    typed: List[tuple] = []
    for asset in assets:
        at = asset.get("assetType") or {}
        tid = 0
        if isinstance(at, dict) and at.get("id"):
            tid = int(at.get("id") or 0)
        typed.append((tid, asset.get("id"), asset.get("name") or ""))

    for wanted, kind in ((56, "pose"), (51, "idle")):
        for tid, aid, name in typed:
            if tid == wanted:
                push(aid, kind, ASSET_TYPE_NAMES.get(wanted, "Animation"), name)
    # Reine Bewegungs-Clips (Walk/Run/Jump ...) sind keine *Pose* und werden
    # bewusst nicht verwendet - sie kosten nur Downloadzeit und liefern eine
    # Lauf-/Sprung-Haltung statt der Standpose.
    emotes = [e for e in (avatar.get("emotes") or []) if isinstance(e, dict)]
    def slot(item: Dict[str, Any]) -> int:
        try:
            return int(item.get("position") or 99)
        except (TypeError, ValueError):
            return 99
    for emote in sorted(emotes, key=slot):
        push(emote.get("assetId"), "emote",
             f"Emote-Slot {slot(emote)}" if slot(emote) != 99 else "Emote",
             emote.get("assetName") or "")

    if not found and warnings is not None:
        warnings.append(
            "Ausgeruestete Pose: die Avatar-API liefert weder eine Pose-/Idle-"
            "Animation noch ein ausgeruestetes Emote - es wird die Stand-Pose "
            "gerendert.")
    return found


def fetch_equipped_pose_candidates(client: HttpClient,
                                   avatar: Dict[str, Any],
                                   user_id: Optional[int] = None,
                                   warnings: Optional[List[str]] = None,
                                   max_checks: int = 12
                                   ) -> List[Dict[str, Any]]:
    """Wie :func:`equipped_pose_candidates`, mit Nachladen bei mageren Daten.

    Liefert ``/v1/users/{id}/avatar`` keine Pose/Emotes (z. B. weil nur der
    ``currently-wearing``-Fallback verfuegbar war), werden die getragenen
    Asset-IDs aufgeloest und auf Animations-/Emote-Typen geprueft.
    """
    candidates = equipped_pose_candidates(avatar, warnings)
    if candidates or client is None or user_id is None:
        return candidates

    resp = client.get(
        f"https://avatar.roblox.com/v1/users/{user_id}/currently-wearing",
        allow_status=(400, 403, 404, 429))
    if not resp.ok:
        return candidates
    try:
        ids = [int(v) for v in (resp.json().get("assetIds") or [])]
    except (json.JSONDecodeError, TypeError, ValueError):
        return candidates

    checked = 0
    for asset_id in ids:
        if checked >= max_checks:
            break
        checked += 1
        details = fetch_asset_details(client, asset_id)
        tid = int(details.get("assetTypeId") or 0)
        if tid in POSE_ANIMATION_TYPES or tid == EMOTE_ANIMATION_TYPE:
            kind = {56: "pose", 51: "idle"}.get(tid, "emote")
            candidates.append({
                "asset_id": asset_id, "kind": kind,
                "source": f"getragen ({ASSET_TYPE_NAMES.get(tid, tid)})",
                "name": details.get("name") or "",
            })
    return candidates


# ---------------------------------------------------------------------------
# Asset-Download
# ---------------------------------------------------------------------------


@dataclass
class DownloadedAsset:
    asset_id: int
    data: Optional[bytes]
    content_type: str = ""
    source: str = ""
    note: str = ""


def _is_image_bytes(data: bytes) -> bool:
    return data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff" or data[:4] == b"RIFF"


def _is_roblox_binary(data: bytes) -> bool:
    return data[:8] == b"<roblox!" or data[:8].startswith(b"<roblox")


def maybe_decompress(data: bytes) -> bytes:
    """Entpackt gzip-/zlib-gepackte Asset-Daten transparent.

    Die Asset Delivery (v. a. der Legacy-Endpunkt) liefert Modell-Dateien
    (RBXM) haeufig gzip-komprimiert aus - erkennbar an der Magic
    ``1F 8B``. Ohne Entpacken wuerde classify_asset() sie faelschlich
    fuer ein Mesh halten. Unbekannte/kaputte Daten bleiben unveraendert.
    """
    if data[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(data)
        except (OSError, EOFError, zlib.error):
            return data
    if data[:1] == b"\x78":  # zlib-Header (z. B. 78 9C)
        try:
            return zlib.decompress(data)
        except zlib.error:
            return data
    return data


def download_asset(client: HttpClient, asset_id: int) -> DownloadedAsset:
    """Laedt ein Asset-Binaerfile ueber alle verfuegbaren Endpunkte."""
    attempts: List[Tuple[str, Dict[str, str]]] = []

    if client.api_key:
        attempts.append((
            f"https://apis.roblox.com/asset-delivery-api/v1/assetId/{asset_id}",
            {"x-api-key": client.api_key},
        ))
    attempts.append((f"https://assetdelivery.roblox.com/v2/assetId/{asset_id}",
                     {"x-api-key": client.api_key} if client.api_key else {}))
    attempts.append((f"https://assetdelivery.roblox.com/v1/asset/?id={asset_id}",
                     {"x-api-key": client.api_key} if client.api_key else {}))

    notes: List[str] = []
    for url, headers in attempts:
        try:
            resp = client.get(url, headers=headers, allow_status=(400, 401, 403, 404, 409, 429))
        except RobloxError as exc:
            notes.append(f"{_host(url)}: {exc}")
            continue

        if not resp.ok:
            notes.append(f"{_host(url)}: HTTP {resp.status} {_short(resp.body, 80)}")
            continue

        body = resp.body
        ctype = resp.headers.get("Content-Type", "")

        # Manche Endpunkte antworten mit JSON, das eine CDN-URL enthaelt
        if body[:1] == b"{" and not _is_image_bytes(body):
            try:
                meta = json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                meta = {}
            locations = meta.get("locations") or []
            if isinstance(meta.get("location"), str):
                locations = [{"location": meta["location"]}]
            for loc in locations:
                cdn_url = loc.get("location") if isinstance(loc, dict) else loc
                if not cdn_url:
                    continue
                try:
                    cdn = client.get(cdn_url, allow_status=(400, 401, 403, 404))
                except RobloxError as exc:
                    notes.append(f"CDN: {exc}")
                    continue
                if cdn.ok:
                    return DownloadedAsset(asset_id, maybe_decompress(cdn.body),
                                           cdn.headers.get("Content-Type", ctype),
                                           "cdn", "; ".join(notes))
            notes.append(f"{_host(url)}: JSON ohne CDN-URL")
            continue

        if not body:
            notes.append(f"{_host(url)}: leere Antwort")
            continue

        return DownloadedAsset(asset_id, maybe_decompress(body), ctype,
                               _host(url), "; ".join(notes))

    return DownloadedAsset(asset_id, None, "", "", "; ".join(notes))


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or url


def classify_asset(data: bytes, declared_type_id: int = 0) -> str:
    """Bestimmt anhand der Magic-Bytes, was in dem Buffer steckt."""
    from roblox_mesh import is_filemesh  # lokal, damit Import-Zyklen harmlos bleiben

    if is_filemesh(data):
        return "mesh"
    if _is_image_bytes(data):
        return "texture"
    if _is_roblox_binary(data):
        return "rbxm"
    head = data[:512].lstrip().lower()
    if head.startswith(b"<roblox"):
        return "rbxm"
    if declared_type_id in TEXTURE_ASSET_TYPES:
        return "texture"
    if declared_type_id in MESH_ASSET_TYPES:
        return "mesh"
    return "unknown"


# ---------------------------------------------------------------------------
# BrickColor-Tabelle
# ---------------------------------------------------------------------------

_BRICK_COLORS: Optional[Dict[str, List[Any]]] = None


def brick_colors() -> Dict[str, List[Any]]:
    global _BRICK_COLORS
    if _BRICK_COLORS is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brick_colors.json")
        with open(path, "r", encoding="utf-8") as fh:
            _BRICK_COLORS = json.load(fh)
    return _BRICK_COLORS


def brick_color_hex(color_id: Any) -> str:
    """BrickColor-ID -> Hex-Farbe. Unbekannte IDs bekommen eine stabile Ersatzfarbe."""
    table = brick_colors()
    try:
        entry = table.get(str(int(color_id)))
    except (TypeError, ValueError):
        entry = None
    if entry:
        _name, r, g, b = entry[0], entry[1], entry[2], entry[3]
        return "#%02x%02x%02x" % (r, g, b)
    # Deterministischer Fallback im Pastellbereich
    try:
        seed = int(color_id) or 0
    except (TypeError, ValueError):
        seed = 0
    rnd = random.Random(seed)
    r = rnd.randint(130, 245)
    g = rnd.randint(140, 245)
    b = rnd.randint(160, 250)
    return "#%02x%02x%02x" % (r, g, b)


def hex_to_rgb(value: str) -> Tuple[float, float, float]:
    value = (value or "#cccccc").lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    try:
        r = int(value[0:2], 16) / 255.0
        g = int(value[2:4], 16) / 255.0
        b = int(value[4:6], 16) / 255.0
    except (ValueError, IndexError):
        r = g = b = 0.8
    return (r, g, b)
