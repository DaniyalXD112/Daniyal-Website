"""Small, on-demand CheapShark client. Never crawls the provider's catalog."""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://www.cheapshark.com/api/1.0"
USER_AGENT = "PLAYSCAPE/2.0 (+https://www.cheapshark.com/) game discovery"
_rate_lock = threading.Lock()
_blocked_until = 0


class CheapSharkError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def retry_after_seconds() -> int:
    with _rate_lock:
        return max(0, _blocked_until - int(time.time()))


def request_json(path: str, params: dict | None = None, timeout: int = 18, with_headers: bool = False):
    global _blocked_until
    retry = retry_after_seconds()
    if retry:
        raise CheapSharkError("CheapShark rate limit is active; try again later.", 429, retry)
    url = f"{BASE_URL}/{path.lstrip('/')}?{urlencode(params or {}, doseq=True)}"
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024:
                raise CheapSharkError("CheapShark response exceeded the response-size limit.", 502)
            payload = json.loads(body.decode("utf-8"))
            return (payload, dict(response.headers.items())) if with_headers else payload
    except HTTPError as exc:
        if exc.code == 429:
            try:
                delay = max(30, min(86400, int(exc.headers.get("Retry-After", "60"))))
            except (TypeError, ValueError):
                delay = 60
            with _rate_lock:
                _blocked_until = max(_blocked_until, int(time.time()) + delay)
            raise CheapSharkError("CheapShark temporarily rate-limited requests.", 429, delay) from None
        raise CheapSharkError(f"CheapShark request failed with HTTP {exc.code}.", exc.code) from None
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise CheapSharkError(f"CheapShark is temporarily unavailable ({type(exc).__name__}).", 502) from None


def games_by_steam_id(steam_app_id: str) -> list[dict]:
    if not str(steam_app_id).isdigit():
        return []
    result = request_json("games", {"steamAppID": str(steam_app_id), "limit": 60})
    return [row for row in result if isinstance(row, dict)] if isinstance(result, list) else []


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").casefold()
    return " ".join(re.findall(r"[a-z0-9]+", value))


def match_game(name: str, steam_app_id: str | None = None) -> dict:
    """Prefer a matching App ID; otherwise require a unique exact normalized title."""
    target = normalize_title(name)
    if not target:
        return {"state": "not_found", "game": None}
    if steam_app_id:
        rows = games_by_steam_id(steam_app_id)
        exact = [row for row in rows if normalize_title(str(row.get("external", ""))) == target]
        if len(exact) == 1:
            return {"state": "matched_app_id", "game": exact[0]}
        if len(rows) == 1 and str((rows[0].get("steamAppID") or "")) == str(steam_app_id):
            # App ID is authoritative, but avoid a contradictory product name.
            candidate = normalize_title(str(rows[0].get("external", "")))
            if candidate == target:
                return {"state": "matched_app_id", "game": rows[0]}
        if len(rows) > 1:
            return {"state": "ambiguous", "game": None}
    rows = request_json("games", {"title": name[:120], "exact": 1, "limit": 60})
    candidates = [row for row in rows if isinstance(row, dict) and normalize_title(str(row.get("external", ""))) == target] if isinstance(rows, list) else []
    # Same-title remasters/editions are ambiguous without a trustworthy Steam app ID.
    unique = {str(item.get("gameID")): item for item in candidates if item.get("gameID")}
    if len(unique) == 1:
        return {"state": "matched_exact_title", "game": next(iter(unique.values()))}
    return {"state": "ambiguous" if unique else "not_found", "game": None}


def game_lookup(game_id: str | int) -> dict:
    result = request_json("games", {"id": str(game_id)})
    if not isinstance(result, dict) or not isinstance(result.get("info"), dict):
        raise CheapSharkError("CheapShark returned an invalid game record.", 502)
    return result


def deal_pages(params: dict) -> tuple[list[dict], int]:
    result, headers = request_json("deals", params, with_headers=True)
    if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
        raise CheapSharkError("CheapShark returned an invalid deals page.", 502)
    try:
        raw_page_count = next((value for key, value in headers.items() if key.casefold() == "x-total-page-count"), "0")
        pages = max(0, int(raw_page_count))
    except (TypeError, ValueError):
        pages = 0
    return result, pages


def stores() -> list[dict]:
    result = request_json("stores")
    return [row for row in result if isinstance(row, dict) and row.get("storeID")] if isinstance(result, list) else []


def deal_redirect(deal_id: str) -> str | None:
    # CheapShark IDs can themselves contain percent-escapes; preserve those and
    # reject anything that could alter the redirect URL's query string.
    value = str(deal_id or "")
    if not re.fullmatch(r"[A-Za-z0-9%/+_=.-]{1,240}", value):
        return None
    return "https://www.cheapshark.com/redirect?dealID=" + quote(value, safe="%/+_=.-")

