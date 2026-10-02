"""Server-side RAWG client. API keys are read only from the process environment."""
from __future__ import annotations

import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://api.rawg.io/api"
USER_AGENT = "PLAYSCAPE/2.0 game discovery"


class RawgError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def api_key() -> str:
    return os.environ.get("RAWG_API_KEY", "").strip()


def request_json(path: str, params: dict | None = None, timeout: int = 20, attempts: int = 2):
    key = api_key()
    if not key:
        raise RawgError("RAWG_API_KEY is not configured on the server.", 503)
    query = dict(params or {})
    query["key"] = key
    url = f"{BASE_URL}/{path.lstrip('/')}?{urlencode(query, doseq=True)}"
    for attempt in range(attempts):
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read(12 * 1024 * 1024 + 1)
                if len(body) > 12 * 1024 * 1024:
                    raise RawgError("RAWG response exceeded the response-size limit.", 502)
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, (dict, list)):
                    raise RawgError("RAWG returned an unexpected response.", 502)
                return payload
        except HTTPError as exc:
            retry_after = None
            try:
                retry_after = max(1, min(3600, int(exc.headers.get("Retry-After", "0")))) or None
            except (TypeError, ValueError):
                pass
            if exc.code >= 500 and attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise RawgError(f"RAWG request failed with HTTP {exc.code}.", exc.code, retry_after) from None
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            if attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise RawgError(f"RAWG is temporarily unavailable ({type(exc).__name__}).", 502) from None
    raise RawgError("RAWG request failed.", 502)


def list_games(page: int, page_size: int = 40, extra: dict | None = None) -> dict:
    params = {"page": max(1, int(page)), "page_size": max(1, min(40, int(page_size))), "ordering": "-added"}
    if extra:
        params.update(extra)
    result = request_json("games", params)
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        raise RawgError("RAWG returned an invalid game page.", 502)
    return result


def game_details(rawg_id: int) -> dict:
    result = request_json(f"games/{int(rawg_id)}")
    if not isinstance(result, dict) or not result.get("id"):
        raise RawgError("RAWG did not return a game record.", 502)
    return result


def game_resource(rawg_id: int, resource: str, page_size: int = 20) -> list[dict]:
    payload = request_json(f"games/{int(rawg_id)}/{resource}", {"page_size": page_size})
    if not isinstance(payload, dict):
        return []
    rows = payload.get("results", [])
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
