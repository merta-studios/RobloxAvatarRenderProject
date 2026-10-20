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

import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
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
                    return DownloadedAsset(asset_id, cdn.body,
                                           cdn.headers.get("Content-Type", ctype),
                                           "cdn", "; ".join(notes))
            notes.append(f"{_host(url)}: JSON ohne CDN-URL")
            continue

        if not body:
            notes.append(f"{_host(url)}: leere Antwort")
            continue

        return DownloadedAsset(asset_id, body, ctype, _host(url), "; ".join(notes))

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
