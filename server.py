"""PLAYSCAPE API: RAWG-backed catalog, on-demand CheapShark pricing and SQLite."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

import cheapshark_service as cheapshark
import rawg_service as rawg


ROOT = Path(__file__).resolve().parent
LOCAL_DB = ROOT / "playscape.sqlite3"
WORK_DB = ROOT.parent.parent / "work" / "playscape.sqlite3"
DEFAULT_DATA_DIR = Path(os.environ.get("GAMEVAULT_DATA_DIR", os.environ.get("LOCALAPPDATA", ROOT.parent.parent / ".gamevault-data"))) / "GAMEVAULT"
FALLBACK_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", ROOT.parent.parent / ".playscape-data")) / "PLAYSCAPE"
DEFAULT_DB = (
    LOCAL_DB
    if LOCAL_DB.exists()
    else (
        WORK_DB
        if WORK_DB.exists()
        else (DEFAULT_DATA_DIR / "gamevault.sqlite3" if not (FALLBACK_DATA_DIR / "playscape.sqlite3").exists() else FALLBACK_DATA_DIR / "playscape.sqlite3")
    )
)
DB_PATH = Path(os.environ.get("GAMEVAULT_DB", os.environ.get("PLAYSCAPE_DB", DEFAULT_DB)))
HOST = os.environ.get("GAMEVAULT_HOST", os.environ.get("HOST", "0.0.0.0"))
PORT = int(os.environ.get("PORT") or os.environ.get("GAMEVAULT_PORT") or os.environ.get("PLAYSCAPE_PORT") or 4174)
ADMIN_TOKEN = os.environ.get("GAMEVAULT_ADMIN_TOKEN", os.environ.get("PLAYSCAPE_ADMIN_TOKEN", "")).strip()
if not ADMIN_TOKEN:
    ADMIN_TOKEN = secrets.token_urlsafe(32)
    os.environ["GAMEVAULT_ADMIN_TOKEN"] = ADMIN_TOKEN
SESSION_COOKIE = "gamevault_session"
SESSION_DAYS = 30
RAWG_PAGE_SIZE = 40
RAWG_PAGES_PER_RUN = 10
AUTO_SYNC_RETRY_SECONDS = 15 * 60
AUTO_SYNC_INCREMENTAL_SECONDS = 24 * 60 * 60
PRICE_CACHE_SECONDS = 2 * 60 * 60
DEALS_CACHE_SECONDS = 60 * 60
STORES_CACHE_SECONDS = 24 * 60 * 60
sync_lock = threading.Lock()
auto_sync_guard = threading.Lock()
auto_sync_scheduled = False
details_lock = threading.Lock()
details_locks_guard = threading.Lock()
details_locks: dict[int, threading.Lock] = {}
price_locks_guard = threading.Lock()
price_locks: dict[int, threading.Lock] = {}
metadata_job_lock = threading.Lock()
SESSION_USER_AGENT = "GAMEVAULT/2.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists() and LOCAL_DB.exists():
        try:
            if DB_PATH.resolve() != LOCAL_DB.resolve():
                import shutil
                shutil.copy2(LOCAL_DB, DB_PATH)
        except Exception:
            pass
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def migration_from_steam(db: sqlite3.Connection) -> tuple[list[dict], list[dict], list[dict]]:
    """Move the previous empty/Steam-keyed schema to distinct RAWG and Steam IDs."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "games" not in tables:
        return [], [], []
    columns = {r[1] for r in db.execute("PRAGMA table_info(games)")}
    if "rawg_id" in columns:
        return [], [], []
    old_games = [dict(r) for r in db.execute("SELECT * FROM games")]
    old_genres = [dict(r) for r in db.execute("SELECT * FROM genres")] if "genres" in tables else []
    old_wishlist = [dict(r) for r in db.execute("SELECT * FROM wishlist")] if "wishlist" in tables else []
    for trigger in ("games_fts_insert", "games_fts_delete", "games_fts_update"):
        db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    db.execute("DROP TABLE IF EXISTS games_fts")
    db.execute("DROP TABLE IF EXISTS wishlist")
    db.execute("DROP TABLE IF EXISTS genres")
    db.execute("ALTER TABLE games RENAME TO games_legacy_steam")
    return old_games, old_genres, old_wishlist


def initialize() -> None:
    with connect() as db:
        old_games, old_genres, old_wishlist = migration_from_steam(db)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS games (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          rawg_id INTEGER UNIQUE,
          cheapshark_game_id TEXT,
          steam_app_id TEXT,
          name TEXT NOT NULL,
          slug TEXT NOT NULL DEFAULT '',
          short_description TEXT,
          description TEXT,
          released TEXT,
          release_sort TEXT,
          background_image TEXT,
          cover_image TEXT,
          website TEXT,
          rating REAL,
          rating_top INTEGER,
          ratings_count INTEGER,
          metacritic INTEGER,
          metacritic_platforms_json TEXT,
          playtime REAL,
          tba INTEGER,
          platforms_json TEXT,
          system_requirements_json TEXT,
          esrb_rating TEXT,
          rawg_updated TEXT,
          rawg_synced_at TEXT,
          detail_status TEXT NOT NULL DEFAULT 'catalog',
          detail_attempt_at TEXT,
          detail_error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS genres (
          game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
          rawg_id INTEGER,
          name TEXT NOT NULL,
          slug TEXT,
          PRIMARY KEY(game_id,name)
        );
        CREATE TABLE IF NOT EXISTS game_tags (
          game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
          rawg_id INTEGER,
          name TEXT NOT NULL,
          slug TEXT,
          PRIMARY KEY(game_id,name)
        );
        CREATE TABLE IF NOT EXISTS game_people (
          game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
          role TEXT NOT NULL,
          name TEXT NOT NULL,
          PRIMARY KEY(game_id,role,name)
        );
        CREATE TABLE IF NOT EXISTS game_platforms (
          game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
          rawg_id INTEGER,
          name TEXT NOT NULL,
          slug TEXT,
          requirements_json TEXT,
          PRIMARY KEY(game_id,name)
        );
        CREATE TABLE IF NOT EXISTS game_stores (
          game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
          store_id INTEGER,
          store_name TEXT NOT NULL,
          slug TEXT,
          url TEXT,
          PRIMARY KEY(game_id,store_name,url)
        );
        CREATE TABLE IF NOT EXISTS game_media (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
          kind TEXT NOT NULL CHECK(kind IN ('screenshot','trailer')),
          title TEXT,
          url TEXT NOT NULL,
          preview_url TEXT,
          source TEXT NOT NULL DEFAULT 'RAWG',
          UNIQUE(game_id,kind,url)
        );
        CREATE TABLE IF NOT EXISTS related_games (
          game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
          rawg_id INTEGER,
          name TEXT NOT NULL,
          slug TEXT,
          background_image TEXT,
          rating REAL,
          relation TEXT NOT NULL DEFAULT 'similar',
          PRIMARY KEY(game_id,rawg_id,relation)
        );
        CREATE TABLE IF NOT EXISTS cheapshark_stores (
          store_id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          logo_url TEXT,
          is_active INTEGER,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS game_prices (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
          store_id TEXT NOT NULL,
          store_name TEXT NOT NULL,
          deal_id TEXT NOT NULL,
          price REAL NOT NULL,
          retail_price REAL,
          savings REAL,
          deal_rating REAL,
          currency TEXT NOT NULL DEFAULT 'USD',
          deal_url TEXT,
          checked_at TEXT NOT NULL,
          UNIQUE(game_id,store_id,deal_id)
        );
        CREATE TABLE IF NOT EXISTS price_cache (
          game_id INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
          cheapshark_game_id TEXT,
          payload_json TEXT,
          state TEXT NOT NULL,
          checked_at TEXT,
          expires_at INTEGER,
          cheapest_ever REAL,
          cheapest_ever_at INTEGER,
          last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS deals_cache (
          query_key TEXT PRIMARY KEY,
          payload_json TEXT NOT NULL,
          page_count INTEGER NOT NULL DEFAULT 0,
          checked_at TEXT NOT NULL,
          expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS app_cache (
          cache_key TEXT PRIMARY KEY,
          payload_json TEXT NOT NULL,
          checked_at INTEGER NOT NULL,
          expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at INTEGER NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS wishlist (
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
          created_at TEXT NOT NULL,
          PRIMARY KEY(user_id,game_id)
        );
        CREATE TABLE IF NOT EXISTS sync_state (
          id INTEGER PRIMARY KEY CHECK(id=1),
          status TEXT NOT NULL DEFAULT 'idle',
          mode TEXT NOT NULL DEFAULT 'initial',
          started_at TEXT,
          finished_at TEXT,
          last_sync_at TEXT,
          updated_at TEXT,
          total_seen INTEGER NOT NULL DEFAULT 0,
          added INTEGER NOT NULL DEFAULT 0,
          updated INTEGER NOT NULL DEFAULT 0,
          failed INTEGER NOT NULL DEFAULT 0,
          pages INTEGER NOT NULL DEFAULT 0,
          cursor INTEGER NOT NULL DEFAULT 1,
          has_more INTEGER NOT NULL DEFAULT 0,
          last_error TEXT,
          message TEXT,
          upcoming_sync_status TEXT NOT NULL DEFAULT 'idle',
          upcoming_sync_started_at TEXT,
          upcoming_sync_finished_at TEXT,
          upcoming_last_sync_at TEXT,
          upcoming_cursor INTEGER NOT NULL DEFAULT 1,
          upcoming_has_more INTEGER NOT NULL DEFAULT 0,
          upcoming_total_seen INTEGER NOT NULL DEFAULT 0,
          upcoming_added INTEGER NOT NULL DEFAULT 0,
          upcoming_updated INTEGER NOT NULL DEFAULT 0,
          upcoming_failed INTEGER NOT NULL DEFAULT 0,
          upcoming_last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS metadata_state (
          id INTEGER PRIMARY KEY CHECK(id=1),
          status TEXT NOT NULL DEFAULT 'idle',
          started_at TEXT,
          finished_at TEXT,
          total_queued INTEGER NOT NULL DEFAULT 0,
          processed INTEGER NOT NULL DEFAULT 0,
          failed INTEGER NOT NULL DEFAULT 0,
          message TEXT
        );
        CREATE TABLE IF NOT EXISTS service_metrics (
          service TEXT PRIMARY KEY,
          last_request_at TEXT,
          request_count INTEGER NOT NULL DEFAULT 0,
          error_count INTEGER NOT NULL DEFAULT 0,
          rate_limit_errors INTEGER NOT NULL DEFAULT 0,
          last_error TEXT
        );
        INSERT OR IGNORE INTO sync_state(id,status,message) VALUES(1,'idle','RAWG catalog sync has not run yet.');
        INSERT OR IGNORE INTO metadata_state(id,status,message) VALUES(1,'idle','No RAWG game details have been refreshed yet.');
        CREATE INDEX IF NOT EXISTS idx_games_release ON games(release_sort);
        CREATE INDEX IF NOT EXISTS idx_games_rating ON games(rating DESC,ratings_count DESC);
        CREATE INDEX IF NOT EXISTS idx_games_metacritic ON games(metacritic DESC);
        CREATE INDEX IF NOT EXISTS idx_games_rawg_sync ON games(rawg_synced_at);
        CREATE INDEX IF NOT EXISTS idx_genres_name ON genres(name,game_id);
        CREATE INDEX IF NOT EXISTS idx_tags_name ON game_tags(name,game_id);
        CREATE INDEX IF NOT EXISTS idx_platform_name ON game_platforms(name,game_id);
        CREATE INDEX IF NOT EXISTS idx_people_name ON game_people(role,name,game_id);
        CREATE INDEX IF NOT EXISTS idx_prices_game ON game_prices(game_id,price);
        CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
        """)
        # Older PLAYSCAPE databases used the same sync tables with fewer columns.
        # CREATE TABLE IF NOT EXISTS does not upgrade those tables, so add the
        # current fields in place and preserve their existing progress records.
        compatibility_columns = {
            "sync_state": {
                "mode": "TEXT NOT NULL DEFAULT 'initial'",
                "last_sync_at": "TEXT",
                "has_more": "INTEGER NOT NULL DEFAULT 0",
                "last_error": "TEXT",
                "upcoming_sync_status": "TEXT NOT NULL DEFAULT 'idle'",
                "upcoming_sync_started_at": "TEXT",
                "upcoming_sync_finished_at": "TEXT",
                "upcoming_last_sync_at": "TEXT",
                "upcoming_cursor": "INTEGER NOT NULL DEFAULT 1",
                "upcoming_has_more": "INTEGER NOT NULL DEFAULT 0",
                "upcoming_total_seen": "INTEGER NOT NULL DEFAULT 0",
                "upcoming_added": "INTEGER NOT NULL DEFAULT 0",
                "upcoming_updated": "INTEGER NOT NULL DEFAULT 0",
                "upcoming_failed": "INTEGER NOT NULL DEFAULT 0",
                "upcoming_last_error": "TEXT",
            },
        }
        for table, additions in compatibility_columns.items():
            existing_columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            for column, definition in additions.items():
                if column not in existing_columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        if old_games:
            old_by_app: dict[int, int] = {}
            now = utc_now()
            for item in old_games:
                name = str(item.get("name") or "Unknown game")
                cursor = db.execute("""INSERT INTO games(steam_app_id,name,slug,short_description,description,released,release_sort,
                    background_image,cover_image,platforms_json,system_requirements_json,detail_status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(item.get("app_id") or "") or None, name, str(item.get("slug") or ""), item.get("short_description"),
                     item.get("description"), item.get("release_date"), item.get("release_sort"), item.get("background_image") or item.get("header_image"),
                     item.get("capsule_image"), item.get("platforms_json"), json.dumps({"minimum": item.get("min_requirements"), "recommended": item.get("rec_requirements")}),
                     "legacy", item.get("created_at") or now, now))
                if item.get("app_id"):
                    old_by_app[int(item["app_id"])] = int(cursor.lastrowid)
            for item in old_genres:
                target = old_by_app.get(int(item.get("app_id") or 0))
                if target and item.get("name"):
                    db.execute("INSERT OR IGNORE INTO genres(game_id,name) VALUES(?,?)", (target, item["name"]))
            for item in old_wishlist:
                target = old_by_app.get(int(item.get("app_id") or 0))
                if target and item.get("user_id"):
                    db.execute("INSERT OR IGNORE INTO wishlist(user_id,game_id,created_at) VALUES(?,?,?)", (item["user_id"], target, item.get("created_at") or now))
        legacy_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='games_legacy_steam'").fetchone()
        legacy_count = db.execute("SELECT count(*) FROM games_legacy_steam").fetchone()[0] if legacy_exists else 0
        if legacy_exists and (old_games or legacy_count == 0):
            # An empty legacy catalog needs no row migration, but SQLite still
            # retains the renamed table and its old indexes unless it is dropped.
            db.execute("DROP TABLE games_legacy_steam")
        try:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS games_fts USING fts5(name, short_description, description, content='games', content_rowid='id', tokenize='unicode61 remove_diacritics 2')")
            db.executescript("""
            CREATE TRIGGER IF NOT EXISTS games_fts_insert AFTER INSERT ON games BEGIN
              INSERT INTO games_fts(rowid,name,short_description,description) VALUES(new.id,new.name,coalesce(new.short_description,''),coalesce(new.description,''));
            END;
            CREATE TRIGGER IF NOT EXISTS games_fts_delete AFTER DELETE ON games BEGIN
              INSERT INTO games_fts(games_fts,rowid,name,short_description,description) VALUES('delete',old.id,old.name,coalesce(old.short_description,''),coalesce(old.description,''));
            END;
            CREATE TRIGGER IF NOT EXISTS games_fts_update AFTER UPDATE OF name,short_description,description ON games BEGIN
              INSERT INTO games_fts(games_fts,rowid,name,short_description,description) VALUES('delete',old.id,old.name,coalesce(old.short_description,''),coalesce(old.description,''));
              INSERT INTO games_fts(rowid,name,short_description,description) VALUES(new.id,new.name,coalesce(new.short_description,''),coalesce(new.description,''));
            END;
            """)
            db.execute("INSERT INTO games_fts(games_fts) VALUES('rebuild')")
        except sqlite3.OperationalError:
            pass


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if value:
            self.parts.append(value)


def clean_text(value: object, max_len: int = 12000) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parser = PlainText()
    try:
        parser.feed(html.unescape(value))
        result = " ".join(parser.parts)
    except Exception:
        result = re.sub(r"<[^>]*>", " ", html.unescape(value))
    result = re.sub(r"\s+", " ", result).strip()
    return result[:max_len] or None


def json_text(value: object) -> str | None:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) if value is not None else None


def parse_json(value: str | None, default: object) -> object:
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def safe_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value.strip())
    return value.strip()[:2000] if parsed.scheme in {"http", "https"} and parsed.hostname else None


def names(rows: object) -> list[str]:
    if not isinstance(rows, list):
        return []
    return [str(row.get("name")).strip() for row in rows if isinstance(row, dict) and row.get("name")]


def map_rawg_game(item: dict) -> dict:
    platforms = []
    for entry in item.get("platforms") or []:
        if not isinstance(entry, dict):
            continue
        platform = entry.get("platform") if isinstance(entry.get("platform"), dict) else entry
        if platform.get("name"):
            platforms.append({"id": platform.get("id"), "name": str(platform["name"])[:100], "slug": platform.get("slug"),
                              "requirements": entry.get("requirements") if isinstance(entry.get("requirements"), dict) else {}})
    genres = [row for row in (item.get("genres") or []) if isinstance(row, dict) and row.get("name")]
    tags = [row for row in (item.get("tags") or []) if isinstance(row, dict) and row.get("name")]
    developers = names(item.get("developers"))
    publishers = names(item.get("publishers"))
    description = clean_text(item.get("description_raw") or item.get("description"), 12000)
    short_description = clean_text(item.get("description_raw") or item.get("description"), 260)
    return {
        "rawg_id": int(item["id"]), "name": str(item.get("name") or "Untitled game")[:300],
        "slug": str(item.get("slug") or "")[:300], "short_description": short_description,
        "description": description, "released": str(item.get("released") or "")[:30] or None,
        "background_image": safe_url(item.get("background_image")),
        "cover_image": safe_url(item.get("background_image")), "website": safe_url(item.get("website")),
        "rating": item.get("rating") if isinstance(item.get("rating"), (int, float)) else None,
        "rating_top": item.get("rating_top") if isinstance(item.get("rating_top"), int) else None,
        "ratings_count": item.get("ratings_count") if isinstance(item.get("ratings_count"), int) else None,
        "metacritic": item.get("metacritic") if isinstance(item.get("metacritic"), int) else None,
        "metacritic_platforms": item.get("metacritic_platforms") if isinstance(item.get("metacritic_platforms"), list) else [],
        "playtime": item.get("playtime") if isinstance(item.get("playtime"), (int, float)) else None,
        "tba": bool(item.get("tba")), "platforms": platforms,
        "system_requirements": [dict(platform=p["name"], **p.get("requirements", {})) for p in platforms if p.get("requirements")],
        "esrb_rating": (item.get("esrb_rating") or {}).get("name") if isinstance(item.get("esrb_rating"), dict) else None,
        "rawg_updated": str(item.get("updated") or "")[:50] or None,
        "genres": genres, "tags": tags, "developers": developers, "publishers": publishers,
    }


def steam_id_from_links(links: list[dict]) -> str | None:
    for store in links:
        link = safe_url(store.get("url"))
        if not link:
            continue
        parsed = urlparse(link)
        match = re.search(r"/(?:app|sub)/(\d+)(?:/|$)", parsed.path)
        if parsed.hostname and (parsed.hostname == "store.steampowered.com" or parsed.hostname.endswith(".steampowered.com")) and match:
            return match.group(1)
    return None


def upsert_rawg_game(item: dict, detail: bool = False) -> tuple[int, bool]:
    if not isinstance(item, dict) or not item.get("id"):
        raise ValueError("RAWG game record is missing its ID.")
    mapped = map_rawg_game(item)
    now = utc_now()
    values = [mapped.get(k) for k in ("rawg_id", "name", "slug", "short_description", "description", "released", "background_image", "cover_image", "website", "rating", "rating_top", "ratings_count", "metacritic", "metacritic_platforms", "playtime", "tba", "platforms", "system_requirements", "esrb_rating", "rawg_updated")]
    values[13] = json_text(values[13])
    values[16] = json_text(values[16])
    values[17] = json_text(values[17])
    with connect() as db:
        existing = db.execute("SELECT id FROM games WHERE rawg_id=?", (mapped["rawg_id"],)).fetchone()
        if not existing:
            inserted = db.execute("""INSERT INTO games(rawg_id,name,slug,short_description,description,released,background_image,cover_image,website,rating,rating_top,ratings_count,metacritic,metacritic_platforms_json,playtime,tba,platforms_json,system_requirements_json,esrb_rating,rawg_updated,rawg_synced_at,detail_status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                values[:13] + [values[13], values[14], int(bool(values[15])), values[16], values[17], values[18], values[19], now, "complete" if detail else "catalog", now, now])
            game_id = int(inserted.lastrowid)
            added = True
        else:
            game_id = int(existing["id"])
            added = False
            db.execute("""UPDATE games SET name=?,slug=?,short_description=coalesce(?,short_description),description=coalesce(?,description),released=coalesce(?,released),background_image=coalesce(?,background_image),cover_image=coalesce(?,cover_image),website=coalesce(?,website),rating=coalesce(?,rating),rating_top=coalesce(?,rating_top),ratings_count=coalesce(?,ratings_count),metacritic=coalesce(?,metacritic),metacritic_platforms_json=coalesce(?,metacritic_platforms_json),playtime=coalesce(?,playtime),tba=?,platforms_json=coalesce(?,platforms_json),system_requirements_json=coalesce(?,system_requirements_json),esrb_rating=coalesce(?,esrb_rating),rawg_updated=coalesce(?,rawg_updated),rawg_synced_at=?,detail_status=CASE WHEN ? THEN 'complete' WHEN detail_status='failed' THEN 'catalog' ELSE detail_status END,detail_error=CASE WHEN ? THEN NULL ELSE detail_error END,updated_at=? WHERE id=?""",
                values[1:13] + [values[13], values[14], int(bool(values[15])), values[16], values[17], values[18], values[19], now, int(detail), int(detail), now, game_id])
        if mapped["genres"]:
            db.execute("DELETE FROM genres WHERE game_id=?", (game_id,))
            db.executemany("INSERT OR IGNORE INTO genres(game_id,rawg_id,name,slug) VALUES(?,?,?,?)", [(game_id, g.get("id"), str(g["name"])[:120], g.get("slug")) for g in mapped["genres"]])
        if mapped["tags"]:
            db.execute("DELETE FROM game_tags WHERE game_id=?", (game_id,))
            db.executemany("INSERT OR IGNORE INTO game_tags(game_id,rawg_id,name,slug) VALUES(?,?,?,?)", [(game_id, g.get("id"), str(g["name"])[:160], g.get("slug")) for g in mapped["tags"]])
        if mapped["developers"] or mapped["publishers"]:
            db.execute("DELETE FROM game_people WHERE game_id=?", (game_id,))
            db.executemany("INSERT OR IGNORE INTO game_people(game_id,role,name) VALUES(?,?,?)", [(game_id, role, person[:200]) for role, group in (("developer", mapped["developers"]), ("publisher", mapped["publishers"])) for person in group])
        if mapped["platforms"]:
            db.execute("DELETE FROM game_platforms WHERE game_id=?", (game_id,))
            db.executemany("INSERT OR REPLACE INTO game_platforms(game_id,rawg_id,name,slug,requirements_json) VALUES(?,?,?,?,?)", [(game_id, p.get("id"), p["name"], p.get("slug"), json_text(p.get("requirements"))) for p in mapped["platforms"]])
    return game_id, added


def update_sync(**fields: object) -> None:
    allowed = {"status", "mode", "started_at", "finished_at", "last_sync_at", "updated_at", "total_seen", "added", "updated", "failed", "pages", "cursor", "has_more", "last_error", "message", "upcoming_sync_status", "upcoming_sync_started_at", "upcoming_sync_finished_at", "upcoming_last_sync_at", "upcoming_cursor", "upcoming_has_more", "upcoming_total_seen", "upcoming_added", "upcoming_updated", "upcoming_failed", "upcoming_last_error"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if fields:
        with connect() as db:
            db.execute("UPDATE sync_state SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=1", tuple(fields.values()))


def _resource_rows(rawg_id: int, resource: str) -> list[dict]:
    try:
        return rawg.game_resource(rawg_id, resource)
    except rawg.RawgError as exc:
        # Optional resource routes can be absent for a title or unavailable on an API plan.
        if exc.status in {400, 401, 403, 404, 405}:
            return []
        raise


def enrich_game(game_id: int, force: bool = False) -> None:
    with connect() as db:
        row = db.execute("SELECT rawg_id,steam_app_id,detail_status,rawg_synced_at,detail_attempt_at FROM games WHERE id=?", (game_id,)).fetchone()
    if not row or not row["rawg_id"]:
        return
    now = datetime.now(timezone.utc)
    if not force and row["detail_status"] == "complete" and row["rawg_synced_at"]:
        try:
            if (now - datetime.fromisoformat(row["rawg_synced_at"])).total_seconds() < 7 * 86400:
                return
        except ValueError:
            pass
    if not force and row["detail_status"] == "failed" and row["detail_attempt_at"]:
        try:
            if (now - datetime.fromisoformat(row["detail_attempt_at"])).total_seconds() < 3600:
                return
        except ValueError:
            pass
    with details_locks_guard:
        lock = details_locks.setdefault(game_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return
    rawg_id = int(row["rawg_id"])
    try:
        with connect() as db:
            db.execute("UPDATE games SET detail_attempt_at=?,detail_status='loading',detail_error=NULL WHERE id=?", (utc_now(), game_id))
        detail = rawg.game_details(rawg_id)
        mapped_item = dict(detail)
        for key in ("genres", "tags", "developers", "publishers", "platforms"):
            if not mapped_item.get(key):
                with connect() as db:
                    if key == "genres": mapped_item[key] = [dict(name=r[0]) for r in db.execute("SELECT name FROM genres WHERE game_id=?", (game_id,))]
                    elif key == "tags": mapped_item[key] = [dict(name=r[0]) for r in db.execute("SELECT name FROM game_tags WHERE game_id=?", (game_id,))]
                    elif key in {"developers", "publishers"}: mapped_item[key] = [dict(name=r[0]) for r in db.execute("SELECT name FROM game_people WHERE game_id=? AND role=?", (game_id, key[:-1]))]
        mapped_item["id"] = rawg_id
        game_id, _ = upsert_rawg_game(mapped_item, detail=True)
        screenshots = _resource_rows(rawg_id, "screenshots")
        movies = _resource_rows(rawg_id, "movies")
        store_rows = _resource_rows(rawg_id, "stores")
        suggested = _resource_rows(rawg_id, "suggested")
        series = _resource_rows(rawg_id, "game-series")
        clean_screens = [(safe_url(s.get("image")), safe_url(s.get("image")), "Screenshot") for s in screenshots if not s.get("hidden") and safe_url(s.get("image"))]
        clean_movies = []
        for movie in movies:
            data = movie.get("data") if isinstance(movie.get("data"), dict) else {}
            video = next((safe_url(data.get(k)) for k in ("max", "720", "480") if safe_url(data.get(k))), None)
            youtube = str(data.get("youtube") or "").strip()
            if not video and re.fullmatch(r"[A-Za-z0-9_-]{6,20}", youtube):
                video = f"https://www.youtube.com/embed/{youtube}"
            preview = safe_url(movie.get("preview"))
            if video:
                clean_movies.append((video, preview, str(movie.get("name") or "Game video")[:200]))
        stores = []
        for row_data in store_rows:
            store = row_data.get("store") if isinstance(row_data.get("store"), dict) else {}
            url = safe_url(row_data.get("url"))
            if store.get("name") and url:
                stores.append((store.get("id"), str(store["name"])[:160], store.get("slug"), url))
        similar_items = [(g.get("id"), str(g.get("name"))[:300], str(g.get("slug") or "")[:300], safe_url(g.get("background_image")), g.get("rating"), "similar")
                         for g in suggested if g.get("id") and g.get("name")]
        series_items = [(g.get("id"), str(g.get("name"))[:300], str(g.get("slug") or "")[:300], safe_url(g.get("background_image")), g.get("rating"), "series")
                        for g in series if g.get("id") and g.get("name")]
        links = stores
        steam_app = steam_id_from_links([{"url": link[3]} for link in links])
        with connect() as db:
            db.execute("DELETE FROM game_media WHERE game_id=?", (game_id,))
            db.executemany("INSERT OR IGNORE INTO game_media(game_id,kind,title,url,preview_url,source) VALUES(?,?,?,?,?,'RAWG')",
                           [(game_id, "screenshot", title, url, preview) for url, preview, title in clean_screens] +
                           [(game_id, "trailer", title, url, preview) for url, preview, title in clean_movies])
            db.execute("DELETE FROM game_stores WHERE game_id=?", (game_id,))
            db.executemany("INSERT OR IGNORE INTO game_stores(game_id,store_id,store_name,slug,url) VALUES(?,?,?,?,?)", [(game_id, sid, name, slug, url) for sid, name, slug, url in stores])
            db.execute("DELETE FROM related_games WHERE game_id=?", (game_id,))
            db.executemany("INSERT OR IGNORE INTO related_games(game_id,rawg_id,name,slug,background_image,rating,relation) VALUES(?,?,?,?,?,?,?)", [(game_id, *item) for item in similar_items + series_items])
            if steam_app and steam_app != row["steam_app_id"]:
                db.execute("UPDATE games SET steam_app_id=? WHERE id=?", (steam_app, game_id))
                # A newly discovered Steam ID changes the safest CheapShark match key.
                # Discard any earlier title-only result so the next price view rematches.
                db.execute("DELETE FROM price_cache WHERE game_id=?", (game_id,))
                db.execute("DELETE FROM game_prices WHERE game_id=?", (game_id,))
            db.execute("UPDATE games SET detail_status='complete',rawg_synced_at=?,detail_error=NULL,updated_at=? WHERE id=?", (utc_now(), utc_now(), game_id))
    except rawg.RawgError as exc:
        with connect() as db:
            db.execute("UPDATE games SET detail_status='failed',detail_attempt_at=?,detail_error=?,updated_at=? WHERE id=?", (utc_now(), str(exc)[:300], utc_now(), game_id))
        raise
    finally:
        lock.release()


def run_rawg_sync(mode: str = "continue", page_limit: int = RAWG_PAGES_PER_RUN) -> None:
    if not sync_lock.acquire(blocking=False):
        return
    mode = mode if mode in {"initial", "continue", "incremental"} else "continue"
    with connect() as db:
        prior = db.execute("SELECT cursor,has_more,status FROM sync_state WHERE id=1").fetchone()
    page = int(prior["cursor"] or 1) if mode == "continue" and prior["has_more"] else 1
    limit = max(1, min(RAWG_PAGES_PER_RUN, int(page_limit)))
    if mode == "incremental":
        limit = min(limit, 5)
    started = utc_now()
    totals = {"seen": 0, "added": 0, "updated": 0, "failed": 0, "pages": 0}
    update_sync(status="running", mode=mode, started_at=started, finished_at=None, updated_at=started,
                total_seen=0, added=0, updated=0, failed=0, pages=0, cursor=page, has_more=0,
                last_error=None, message=f"RAWG import is reading page {page}.")
    has_more = False
    try:
        for _ in range(limit):
            payload = rawg.list_games(page, RAWG_PAGE_SIZE)
            results = payload["results"]
            if not results:
                has_more = False
                break
            for item in results:
                try:
                    _, added = upsert_rawg_game(item)
                    totals["added" if added else "updated"] += 1
                except (ValueError, TypeError, sqlite3.Error):
                    totals["failed"] += 1
            totals["seen"] += len(results)
            totals["pages"] += 1
            has_more = bool(payload.get("next"))
            page += 1
            update_sync(status="running", updated_at=utc_now(), total_seen=totals["seen"], added=totals["added"],
                        updated=totals["updated"], failed=totals["failed"], pages=totals["pages"], cursor=page,
                        has_more=int(has_more), message=f"Imported {totals['seen']:,} RAWG records across {totals['pages']} pages. Next page: {page}.")
            if not has_more:
                break
            time.sleep(0.15)
        finished = utc_now()
        state = "paused" if has_more else "complete"
        message = (f"Imported {totals['seen']:,} games. Continue sync to index the next RAWG page." if has_more
                   else f"RAWG page sync finished after {totals['seen']:,} game records.")
        update_sync(status=state, finished_at=finished, last_sync_at=finished, updated_at=finished,
                    total_seen=totals["seen"], added=totals["added"], updated=totals["updated"], failed=totals["failed"],
                    pages=totals["pages"], cursor=page, has_more=int(has_more), last_error=None, message=message)
    except rawg.RawgError as exc:
        finished = utc_now()
        update_sync(status="failed", finished_at=finished, updated_at=finished, total_seen=totals["seen"],
                    added=totals["added"], updated=totals["updated"], failed=totals["failed"] + 1,
                    pages=totals["pages"], cursor=page, has_more=1, last_error=str(exc)[:400], message=str(exc)[:400])
    except Exception as exc:
        finished = utc_now()
        update_sync(status="failed", finished_at=finished, updated_at=finished, total_seen=totals["seen"],
                    added=totals["added"], updated=totals["updated"], failed=totals["failed"] + 1,
                    pages=totals["pages"], cursor=page, has_more=1, last_error=f"{type(exc).__name__}: {exc}"[:400],
                    message=f"RAWG import stopped: {type(exc).__name__}.")
    finally:
        sync_lock.release()


def run_rawg_upcoming_sync(page_limit: int = RAWG_PAGES_PER_RUN) -> None:
    """Import a bounded, independent RAWG release-date window for upcoming titles."""
    if not sync_lock.acquire(blocking=False):
        return
    with connect() as db:
        prior = db.execute("SELECT upcoming_cursor,upcoming_has_more FROM sync_state WHERE id=1").fetchone()
    page = int(prior["upcoming_cursor"] or 1) if prior["upcoming_has_more"] else 1
    limit = max(1, min(RAWG_PAGES_PER_RUN, int(page_limit)))
    today = datetime.now(timezone.utc).date()
    end_date = (today + timedelta(days=3652)).isoformat()
    date_window = f"{today.isoformat()},{end_date}"
    started = utc_now()
    totals = {"seen": 0, "added": 0, "updated": 0, "failed": 0, "pages": 0}
    update_sync(upcoming_sync_status="running", upcoming_sync_started_at=started,
                upcoming_sync_finished_at=None, upcoming_last_error=None,
                upcoming_total_seen=0, upcoming_added=0, upcoming_updated=0,
                upcoming_failed=0, upcoming_cursor=page, upcoming_has_more=0)
    has_more = False
    try:
        for _ in range(limit):
            payload = rawg.list_games(page, RAWG_PAGE_SIZE, {"dates": date_window, "ordering": "released"})
            results = payload["results"]
            if not results:
                has_more = False
                break
            for item in results:
                try:
                    _, added = upsert_rawg_game(item)
                    totals["added" if added else "updated"] += 1
                except (ValueError, TypeError, sqlite3.Error):
                    totals["failed"] += 1
            totals["seen"] += len(results)
            totals["pages"] += 1
            has_more = bool(payload.get("next"))
            page += 1
            update_sync(upcoming_total_seen=totals["seen"], upcoming_added=totals["added"],
                        upcoming_updated=totals["updated"], upcoming_failed=totals["failed"],
                        upcoming_cursor=page, upcoming_has_more=int(has_more))
            if not has_more:
                break
            time.sleep(0.15)
        finished = utc_now()
        update_sync(upcoming_sync_status="paused" if has_more else "complete",
                    upcoming_sync_finished_at=finished, upcoming_last_sync_at=finished,
                    upcoming_total_seen=totals["seen"], upcoming_added=totals["added"],
                    upcoming_updated=totals["updated"], upcoming_failed=totals["failed"],
                    upcoming_cursor=page, upcoming_has_more=int(has_more), upcoming_last_error=None)
    except rawg.RawgError as exc:
        update_sync(upcoming_sync_status="failed", upcoming_sync_finished_at=utc_now(),
                    upcoming_total_seen=totals["seen"], upcoming_added=totals["added"],
                    upcoming_updated=totals["updated"], upcoming_failed=totals["failed"] + 1,
                    upcoming_cursor=page, upcoming_has_more=1, upcoming_last_error=str(exc)[:400])
    except Exception as exc:
        update_sync(upcoming_sync_status="failed", upcoming_sync_finished_at=utc_now(),
                    upcoming_total_seen=totals["seen"], upcoming_added=totals["added"],
                    upcoming_updated=totals["updated"], upcoming_failed=totals["failed"] + 1,
                    upcoming_cursor=page, upcoming_has_more=1,
                    upcoming_last_error=f"{type(exc).__name__}: {exc}"[:400])
    finally:
        sync_lock.release()


def timestamp_age_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def auto_sync_worker() -> None:
    """Refresh a bounded RAWG batch automatically after the API server starts."""
    global auto_sync_scheduled
    try:
        if not rawg.api_key():
            return
        state = status_dict()
        if state["status"] != "running":
            recent_failure = state["status"] == "failed" and (timestamp_age_seconds(state.get("finished_at")) or 0) < AUTO_SYNC_RETRY_SECONDS
            if not recent_failure:
                if not state["catalog_games"]:
                    mode = "initial"
                elif state["has_more"]:
                    mode = "continue"
                elif (timestamp_age_seconds(state.get("last_sync_at")) or AUTO_SYNC_INCREMENTAL_SECONDS) >= AUTO_SYNC_INCREMENTAL_SECONDS:
                    mode = "incremental"
                else:
                    mode = None
                if mode:
                    run_rawg_sync(mode)
        state = status_dict()
        if state["status"] == "failed" or state["status"] == "running":
            return
        upcoming_age = timestamp_age_seconds(state.get("upcoming_last_sync_at"))
        upcoming_failed_recently = state.get("upcoming_sync_status") == "failed" and (timestamp_age_seconds(state.get("upcoming_sync_finished_at")) or 0) < AUTO_SYNC_RETRY_SECONDS
        if not upcoming_failed_recently and (state.get("upcoming_has_more") or upcoming_age is None or upcoming_age >= AUTO_SYNC_INCREMENTAL_SECONDS):
            run_rawg_upcoming_sync()
    finally:
        with auto_sync_guard:
            auto_sync_scheduled = False


def start_auto_sync() -> None:
    """Start at most one bounded background catalog refresh for this server run."""
    global auto_sync_scheduled
    if not rawg.api_key():
        return
    with auto_sync_guard:
        if auto_sync_scheduled:
            return
        auto_sync_scheduled = True
    threading.Thread(target=auto_sync_worker, name="playscape-rawg-auto-sync", daemon=True).start()


def run_failed_retry(limit: int = 10) -> None:
    if not metadata_job_lock.acquire(blocking=False):
        return
    try:
        with connect() as db:
            ids = [int(r[0]) for r in db.execute("SELECT id FROM games WHERE rawg_id IS NOT NULL AND detail_status='failed' ORDER BY detail_attempt_at LIMIT ?", (max(1, min(20, limit)),))]
            db.execute("UPDATE metadata_state SET status='running',started_at=?,finished_at=NULL,total_queued=?,processed=0,failed=0,message=? WHERE id=1", (utc_now(), len(ids), f"Retrying {len(ids)} failed RAWG game detail requests."))
        failed = 0
        for index, game_id in enumerate(ids, 1):
            try:
                enrich_game(game_id, force=True)
            except rawg.RawgError:
                failed += 1
            with connect() as db:
                db.execute("UPDATE metadata_state SET processed=?,failed=?,message=? WHERE id=1", (index, failed, f"Retried {index} of {len(ids)} game detail records."))
            time.sleep(0.2)
        with connect() as db:
            db.execute("UPDATE metadata_state SET status='complete',finished_at=?,message=? WHERE id=1", (utc_now(), f"Retry finished: {len(ids) - failed} refreshed, {failed} failed."))
    finally:
        metadata_job_lock.release()


def game_dict(row: sqlite3.Row, db: sqlite3.Connection | None = None) -> dict:
    if db is None:
        with connect() as owned:
            return game_dict(row, owned)
    result = dict(row)
    game_id = int(result["id"])
    genres = [dict(r) for r in db.execute("SELECT rawg_id,name,slug FROM genres WHERE game_id=? ORDER BY name COLLATE NOCASE", (game_id,))]
    tags = [dict(r) for r in db.execute("SELECT rawg_id,name,slug FROM game_tags WHERE game_id=? ORDER BY name COLLATE NOCASE LIMIT 80", (game_id,))]
    people = {"developers": [], "publishers": []}
    for r in db.execute("SELECT role,name FROM game_people WHERE game_id=? ORDER BY role,name COLLATE NOCASE", (game_id,)):
        people["developers" if r["role"] == "developer" else "publishers"].append(r["name"])
    platforms = [dict(r) for r in db.execute("SELECT rawg_id,name,slug,requirements_json FROM game_platforms WHERE game_id=? ORDER BY name COLLATE NOCASE", (game_id,))]
    for item in platforms:
        item["requirements"] = parse_json(item.pop("requirements_json"), {})
    stores = [dict(r) for r in db.execute("SELECT store_id,store_name,slug,url FROM game_stores WHERE game_id=? ORDER BY store_name COLLATE NOCASE", (game_id,))]
    media_rows = [dict(r) for r in db.execute("SELECT kind,title,url,preview_url,source FROM game_media WHERE game_id=? ORDER BY id", (game_id,))]
    similar = [dict(r) for r in db.execute("SELECT rawg_id,name,slug,background_image,rating,relation FROM related_games WHERE game_id=? ORDER BY relation,name COLLATE NOCASE LIMIT 16", (game_id,))]
    requirements = parse_json(result.pop("system_requirements_json", None), [])
    platforms_json = parse_json(result.pop("platforms_json", None), [])
    result["metacritic_platforms"] = parse_json(result.pop("metacritic_platforms_json", None), [])
    result["genres"] = [g["name"] for g in genres]
    result["genre_details"] = genres
    result["tags"] = [t["name"] for t in tags]
    result.update(people)
    result["platforms"] = platforms or platforms_json
    result["system_requirements"] = requirements
    result["screenshots"] = [{"url": m["url"], "thumbnail": m["preview_url"] or m["url"], "source": m["source"]} for m in media_rows if m["kind"] == "screenshot"]
    result["trailers"] = [{"name": m["title"], "url": m["url"], "preview": m["preview_url"], "source": m["source"]} for m in media_rows if m["kind"] == "trailer"]
    result["stores"] = stores
    result["similar_games"] = similar
    result["id"] = game_id
    result["app_id"] = game_id
    result["appId"] = game_id
    result["rawgGameId"] = result.get("rawg_id")
    result["steamAppId"] = result.get("steam_app_id")
    result["cheapsharkGameId"] = result.get("cheapshark_game_id")
    result["title"] = result.get("name")
    result["release_date"] = result.get("released")
    result["header_image"] = result.get("background_image")
    result["capsule_image"] = result.get("cover_image")
    result["steam_url"] = next((s["url"] for s in stores if urlparse(s["url"]).hostname and urlparse(s["url"]).hostname.endswith("steampowered.com")), None)
    result["review_count"] = result.get("ratings_count")
    result["review_desc"] = "RAWG rating" if result.get("rating") is not None else None
    result["review_score"] = round(float(result["rating"]) * 20) if result.get("rating") is not None else None
    result["min_requirements"] = next((r.get("minimum") for r in requirements if r.get("minimum")), None)
    result["rec_requirements"] = next((r.get("recommended") for r in requirements if r.get("recommended")), None)
    result["coming_soon"] = bool(result.get("tba") or (result.get("released") and result["released"] > datetime.now(timezone.utc).date().isoformat()))
    result["detail_complete"] = result.get("detail_status") == "complete"
    result["source"] = "RAWG"
    return result


SORT_SQL = {
    "popular": "coalesce(ratings_count,0) DESC, coalesce(rating,0) DESC, name COLLATE NOCASE",
    "trending": "coalesce(ratings_count,0) DESC, coalesce(rating,0) DESC, name COLLATE NOCASE",
    "recently-released": "(release_sort IS NULL) ASC, release_sort DESC, name COLLATE NOCASE",
    "newest": "(release_sort IS NULL) ASC, release_sort DESC, name COLLATE NOCASE",
    "highest-rated": "(rating IS NULL) ASC, rating DESC, coalesce(ratings_count,0) DESC",
    "most-reviewed": "coalesce(ratings_count,0) DESC, name COLLATE NOCASE",
    "metacritic": "(metacritic IS NULL) ASC, metacritic DESC, name COLLATE NOCASE",
    "az": "name COLLATE NOCASE ASC",
    "za": "name COLLATE NOCASE DESC",
}


def search_catalog(params: dict[str, list[str]]) -> dict:
    q = (params.get("q", [""])[0] or "").strip()[:120]
    genre = (params.get("genre", [""])[0] or "").strip()[:100]
    tag = (params.get("tag", [""])[0] or "").strip()[:120]
    developer = (params.get("developer", [""])[0] or "").strip()[:160]
    publisher = (params.get("publisher", [""])[0] or "").strip()[:160]
    platform = (params.get("platform", [""])[0] or "").strip()[:100]
    sort = (params.get("sort", ["popular"])[0] or "popular").lower()
    order = SORT_SQL.get(sort, SORT_SQL["popular"])
    try:
        page = max(1, min(1_000_000, int(params.get("page", ["1"])[0])))
        limit = max(1, min(60, int(params.get("limit", ["24"])[0])))
        rating_min = max(0, min(5, float(params.get("rating_min", ["0"])[0] or 0)))
        metacritic_min = max(0, min(100, int(params.get("metacritic_min", ["0"])[0] or 0)))
    except (TypeError, ValueError):
        page, limit, rating_min, metacritic_min = 1, 24, 0, 0
    clauses = ["g.rawg_id IS NOT NULL"]
    values: list[object] = []
    for join_table, alias, text in (("genres", "ge", genre), ("game_tags", "tg", tag)):
        if text:
            clauses.append(f"EXISTS(SELECT 1 FROM {join_table} {alias} WHERE {alias}.game_id=g.id AND {alias}.name=?)")
            values.append(text)
    if developer:
        clauses.append("EXISTS(SELECT 1 FROM game_people dp WHERE dp.game_id=g.id AND dp.role='developer' AND dp.name LIKE ?)")
        values.append(f"%{developer}%")
    if publisher:
        clauses.append("EXISTS(SELECT 1 FROM game_people pp WHERE pp.game_id=g.id AND pp.role='publisher' AND pp.name LIKE ?)")
        values.append(f"%{publisher}%")
    if platform:
        clauses.append("EXISTS(SELECT 1 FROM game_platforms pl WHERE pl.game_id=g.id AND lower(pl.name) LIKE ?)")
        values.append(f"%{platform.lower()}%")
    if rating_min:
        clauses.append("g.rating>=?")
        values.append(rating_min)
    if metacritic_min:
        clauses.append("g.metacritic>=?")
        values.append(metacritic_min)
    release = (params.get("release", [""])[0] or "").lower()
    today = datetime.now(timezone.utc).date().isoformat()
    if release == "upcoming":
        clauses.append("(g.tba=1 OR g.released>?)")
        values.append(today)
    elif release == "released":
        clauses.append("(g.released<=? OR g.released IS NULL)")
        values.append(today)
    where = " AND ".join(clauses)
    search_values: list[object] = []
    join_fts = ""
    search_clause = ""
    if q:
        tokens = re.findall(r"[\w-]+", q, flags=re.UNICODE)
        fts = " AND ".join('"' + token.replace('"', '""') + '"*' for token in tokens)
        if fts:
            try:
                with connect() as probe:
                    probe.execute("SELECT rowid FROM games_fts WHERE games_fts MATCH ? LIMIT 1", (fts,)).fetchone()
                join_fts = " JOIN games_fts ON games_fts.rowid=g.id"
                search_clause = " AND games_fts MATCH ?"
                search_values = [fts]
            except sqlite3.OperationalError:
                search_clause = " AND (g.name LIKE ? OR g.short_description LIKE ? OR g.description LIKE ?)"
                search_values = [f"%{q}%"] * 3
    with connect() as db:
        total = int(db.execute(f"SELECT count(DISTINCT g.id) FROM games g{join_fts} WHERE {where}{search_clause}", values + search_values).fetchone()[0])
        rows = db.execute(f"SELECT DISTINCT g.* FROM games g{join_fts} WHERE {where}{search_clause} ORDER BY {order} LIMIT ? OFFSET ?", values + search_values + [limit, (page - 1) * limit]).fetchall()
        results = [game_dict(row, db) for row in rows]
    return {"results": results, "page": page, "limit": limit, "total": total, "pages": (total + limit - 1) // limit, "sort": sort, "source": "RAWG local catalog"}


def query_shelf(where: str, params: tuple = (), order: str = SORT_SQL["popular"], limit: int = 15) -> list[dict]:
    with connect() as db:
        rows = db.execute(f"SELECT g.* FROM games g WHERE g.rawg_id IS NOT NULL AND {where} ORDER BY {order} LIMIT ?", params + (max(1, min(15, limit)),)).fetchall()
        return [game_dict(row, db) for row in rows]


def home_shelves() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    shelves = {
        "trending": query_shelf("coalesce(g.ratings_count,0)>0", order=SORT_SQL["trending"]),
        "recentlyReleased": query_shelf("g.released IS NOT NULL AND g.released<=?", (today,), "(release_sort IS NULL) ASC,release_sort DESC,name COLLATE NOCASE"),
        "mostRated": query_shelf("g.rating IS NOT NULL", order=SORT_SQL["highest-rated"]),
    }
    with connect() as db:
        genres = [r[0] for r in db.execute("SELECT name FROM genres GROUP BY name ORDER BY count(*) DESC,name COLLATE NOCASE LIMIT 12")]
    shelves["genres"] = [{"name": name, "games": query_shelf("EXISTS(SELECT 1 FROM genres ge WHERE ge.game_id=g.id AND ge.name=?)", (name,), SORT_SQL["trending"])} for name in genres]
    return {"shelves": shelves, "source": "PLAYSCAPE SQLite catalog", "games": sum(len(v) for v in shelves.values() if isinstance(v, list))}


def genres_dict() -> dict:
    with connect() as db:
        rows = db.execute("SELECT ge.name,count(*) AS game_count FROM genres ge JOIN games g ON g.id=ge.game_id WHERE g.rawg_id IS NOT NULL GROUP BY ge.name ORDER BY ge.name COLLATE NOCASE").fetchall()
    return {"results": [dict(row) for row in rows], "source": "RAWG local catalog"}


def service_metric(service: str, error: str | None = None, rate_limited: bool = False) -> None:
    with connect() as db:
        db.execute("""INSERT INTO service_metrics(service,last_request_at,request_count,error_count,rate_limit_errors,last_error)
            VALUES(?,?,1,?,?,?) ON CONFLICT(service) DO UPDATE SET last_request_at=excluded.last_request_at,
            request_count=service_metrics.request_count+1,error_count=service_metrics.error_count+excluded.error_count,
            rate_limit_errors=service_metrics.rate_limit_errors+excluded.rate_limit_errors,
            last_error=coalesce(excluded.last_error,service_metrics.last_error)""",
            (service, utc_now(), int(bool(error)), int(rate_limited), error[:300] if error else None))


def stores_cached(force: bool = False) -> list[dict]:
    now = int(time.time())
    with connect() as db:
        cached = db.execute("SELECT payload_json,expires_at FROM app_cache WHERE cache_key='cheapshark:stores'").fetchone()
    if cached and int(cached["expires_at"] or 0) > now and not force:
        return parse_json(cached["payload_json"], [])
    try:
        rows = cheapshark.stores()
        service_metric("cheapshark")
        normalized = []
        stamp = utc_now()
        with connect() as db:
            db.execute("DELETE FROM cheapshark_stores")
            for row in rows:
                store_id = str(row.get("storeID") or "")
                name = str(row.get("storeName") or "").strip()[:180]
                if not store_id.isdigit() or not name:
                    continue
                logo = row.get("images", {}).get("logo") if isinstance(row.get("images"), dict) else None
                logo_url = safe_url(logo)
                if not logo_url and isinstance(logo, str) and logo.startswith("/"):
                    logo_url = "https://www.cheapshark.com" + logo[:500]
                active = row.get("isActive")
                db.execute("INSERT OR REPLACE INTO cheapshark_stores(store_id,name,logo_url,is_active,updated_at) VALUES(?,?,?,?,?)", (store_id, name, logo_url, active, stamp))
                normalized.append({"storeID": store_id, "name": name, "logo": logo_url, "isActive": active})
            db.execute("INSERT OR REPLACE INTO app_cache(cache_key,payload_json,checked_at,expires_at) VALUES('cheapshark:stores',?,?,?)", (json_text(normalized), now, now + STORES_CACHE_SECONDS))
        return normalized
    except cheapshark.CheapSharkError as exc:
        service_metric("cheapshark", str(exc), exc.status == 429)
        return parse_json(cached["payload_json"], []) if cached else []


def price_payload(game_id: int, force: bool = False) -> dict:
    now = int(time.time())
    with connect() as db:
        initial = db.execute("SELECT rawg_id,steam_app_id,detail_status FROM games WHERE id=? AND rawg_id IS NOT NULL", (game_id,)).fetchone()
    if initial and not initial["steam_app_id"] and rawg.api_key() and initial["detail_status"] != "complete":
        try:
            enrich_game(game_id)
        except rawg.RawgError:
            pass
    with connect() as db:
        row = db.execute("SELECT id,name,steam_app_id,cheapshark_game_id FROM games WHERE id=? AND rawg_id IS NOT NULL", (game_id,)).fetchone()
        cache = db.execute("SELECT * FROM price_cache WHERE game_id=?", (game_id,)).fetchone()
    if not row:
        raise KeyError("Game not found in the RAWG catalog.")
    if cache and int(cache["expires_at"] or 0) > now and not force:
        payload = parse_json(cache["payload_json"], {})
        if isinstance(payload, dict):
            payload["fresh"] = True
            return payload
    with price_locks_guard:
        lock = price_locks.setdefault(game_id, threading.Lock())
    if not lock.acquire(blocking=False):
        stale = parse_json(cache["payload_json"], {}) if cache else {}
        if isinstance(stale, dict) and stale:
            stale.update({"fresh": False, "state": "refreshing"})
            return stale
        return {"state": "loading", "fresh": False, "offers": [], "currency": "USD"}
    stale_payload = parse_json(cache["payload_json"], {}) if cache else None
    try:
        if cache and cache["cheapshark_game_id"]:
            match = {"state": "cached_match", "game": {"gameID": cache["cheapshark_game_id"]}}
        else:
            match = cheapshark.match_game(row["name"], row["steam_app_id"])
            service_metric("cheapshark")
        if not match.get("game"):
            state = "ambiguous" if match.get("state") == "ambiguous" else "not_available"
            payload = {"state": state, "fresh": True, "offers": [], "currency": "USD", "checkedAt": utc_now(), "message": "No unique exact product match was returned by CheapShark."}
            expires = now + PRICE_CACHE_SECONDS
            with connect() as db:
                db.execute("INSERT OR REPLACE INTO price_cache(game_id,cheapshark_game_id,payload_json,state,checked_at,expires_at,last_error) VALUES(?,NULL,?,?,?,?,NULL)", (game_id, json_text(payload), state, utc_now(), expires))
                db.execute("DELETE FROM game_prices WHERE game_id=?", (game_id,))
            return payload
        cheap_id = str(match["game"].get("gameID") or cache["cheapshark_game_id"] if cache else match["game"].get("gameID") or "")
        if not cheap_id.isdigit():
            raise cheapshark.CheapSharkError("CheapShark match did not include a valid game ID.", 502)
        lookup = cheapshark.game_lookup(cheap_id)
        service_metric("cheapshark")
        store_rows = stores_cached()
        store_map = {str(store["storeID"]): store for store in store_rows}
        info = lookup.get("info") or {}
        offers = []
        for deal in lookup.get("deals") or []:
            if not isinstance(deal, dict):
                continue
            try:
                price = float(deal.get("price"))
                retail = float(deal.get("retailPrice")) if deal.get("retailPrice") not in (None, "") else None
            except (TypeError, ValueError):
                continue
            store_id = str(deal.get("storeID") or "")
            store = store_map.get(store_id, {})
            deal_id = str(deal.get("dealID") or "")
            redirect = cheapshark.deal_redirect(deal_id)
            if not store_id.isdigit() or not redirect:
                continue
            savings = deal.get("savings")
            try:
                savings = float(savings) if savings not in (None, "") else (max(0, (retail - price) / retail * 100) if retail else None)
            except (TypeError, ValueError):
                savings = None
            try:
                deal_rating = float(deal.get("dealRating")) if deal.get("dealRating") not in (None, "") else None
            except (TypeError, ValueError):
                deal_rating = None
            offers.append({"storeID": store_id, "storeName": store.get("name") or "Store", "storeLogo": store.get("logo"),
                           "dealID": deal_id, "price": price, "retailPrice": retail, "savings": savings,
                           "dealRating": deal_rating, "currency": "USD", "dealURL": redirect,
                           "isOnSale": bool(retail is not None and price < retail)})
        offers.sort(key=lambda item: (item["price"], item["storeName"].casefold()))
        cheapest = lookup.get("cheapestPriceEver") if isinstance(lookup.get("cheapestPriceEver"), dict) else {}
        try:
            cheapest_price = float(cheapest.get("price")) if cheapest.get("price") not in (None, "") else None
            cheapest_date = int(cheapest.get("date")) if cheapest.get("date") not in (None, "") else None
        except (TypeError, ValueError):
            cheapest_price, cheapest_date = None, None
        payload = {"state": "available" if offers else "no_offers", "fresh": True, "currency": "USD", "cheapsharkGameId": cheap_id,
                   "gameTitle": str(info.get("title") or row["name"]), "offers": offers,
                   "currentLowest": min((offer["price"] for offer in offers), default=None),
                   "cheapestPriceEver": cheapest_price, "cheapestPriceEverDate": cheapest_date, "checkedAt": utc_now(),
                   "match": match.get("state")}
        expires = now + PRICE_CACHE_SECONDS
        with connect() as db:
            db.execute("UPDATE games SET cheapshark_game_id=?,updated_at=? WHERE id=?", (cheap_id, utc_now(), game_id))
            db.execute("DELETE FROM game_prices WHERE game_id=?", (game_id,))
            for offer in offers:
                db.execute("INSERT OR IGNORE INTO game_prices(game_id,store_id,store_name,deal_id,price,retail_price,savings,deal_rating,currency,deal_url,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (game_id, offer["storeID"], offer["storeName"], offer["dealID"], offer["price"], offer["retailPrice"], offer["savings"], offer["dealRating"], "USD", offer["dealURL"], payload["checkedAt"]))
            db.execute("INSERT OR REPLACE INTO price_cache(game_id,cheapshark_game_id,payload_json,state,checked_at,expires_at,cheapest_ever,cheapest_ever_at,last_error) VALUES(?,?,?,?,?,?,?,?,NULL)",
                       (game_id, cheap_id, json_text(payload), payload["state"], payload["checkedAt"], expires, cheapest_price, cheapest_date))
        return payload
    except cheapshark.CheapSharkError as exc:
        service_metric("cheapshark", str(exc), exc.status == 429)
        if cache:
            old = stale_payload if isinstance(stale_payload, dict) else {}
            old.update({"fresh": False, "state": "stale", "error": str(exc), "checkedAt": cache["checked_at"]})
            with connect() as db:
                db.execute("UPDATE price_cache SET state='stale',last_error=? WHERE game_id=?", (str(exc)[:300], game_id))
            return old
        payload = {"state": "unavailable", "fresh": False, "offers": [], "currency": "USD", "error": str(exc)}
        with connect() as db:
            db.execute("INSERT OR REPLACE INTO price_cache(game_id,payload_json,state,checked_at,expires_at,last_error) VALUES(?,?,?,NULL,?,?)", (game_id, json_text(payload), "unavailable", now + min(300, exc.retry_after or 120), str(exc)[:300]))
        return payload
    finally:
        lock.release()


DEAL_SORTS = {"DealRating", "Title", "Savings", "Price", "Metacritic", "Reviews", "ReviewCount", "Release", "Store", "Recent"}


def deals_payload(params: dict[str, list[str]]) -> dict:
    try:
        page = max(1, min(100000, int(params.get("page", ["1"])[0])))
        page_size = max(1, min(60, int(params.get("pageSize", ["24"])[0])))
    except (TypeError, ValueError):
        page, page_size = 1, 24
    query: dict[str, object] = {"pageNumber": page - 1, "pageSize": page_size}
    store_id = (params.get("storeID", [""])[0] or "").strip()
    if store_id and all(piece.isdigit() for piece in store_id.split(",")):
        query["storeID"] = store_id[:80]
    sort = (params.get("sortBy", ["DealRating"])[0] or "DealRating").strip()
    query["sortBy"] = sort if sort in DEAL_SORTS else "DealRating"
    query["desc"] = "1" if params.get("desc", ["1"])[0] in {"1", "true"} else "0"
    for key, minimum, maximum in (("lowerPrice", 0, 500), ("upperPrice", 0, 500), ("metacritic", 0, 100), ("steamRating", 0, 100), ("minimumReviewCount", 0, 10000000), ("maxAge", 1, 2500)):
        value = (params.get(key, [""])[0] or "").strip()
        if value.isdigit() and minimum <= int(value) <= maximum:
            query[key] = value
    if params.get("onSale", [""])[0] in {"1", "true"}:
        query["onSale"] = 1
    title = (params.get("title", [""])[0] or "").strip()[:100]
    if title:
        query["title"] = title
        query["exact"] = 1 if params.get("exact", [""])[0] in {"1", "true"} else 0
    cache_key = "v2:" + json.dumps(query, sort_keys=True, separators=(",", ":"))
    now = int(time.time())
    with connect() as db:
        cache = db.execute("SELECT payload_json,page_count,expires_at,checked_at FROM deals_cache WHERE query_key=?", (cache_key,)).fetchone()
    if cache and int(cache["expires_at"]) > now:
        data = parse_json(cache["payload_json"], [])
        return {"results": data, "page": page, "pageSize": page_size, "pages": cache["page_count"], "cached": True, "checkedAt": cache["checked_at"], "state": "available", "source": "CheapShark"}
    try:
        deals, pages = cheapshark.deal_pages(query)
        service_metric("cheapshark")
        stores = stores_cached()
        store_map = {str(row["storeID"]): row for row in stores}
        results = []
        with connect() as db:
            for deal in deals:
                deal_id = str(deal.get("dealID") or "")
                redirect = cheapshark.deal_redirect(deal_id)
                if not redirect:
                    continue
                store = store_map.get(str(deal.get("storeID") or ""), {})
                item = dict(deal)
                item["dealURL"] = redirect
                item["storeName"] = store.get("name") or "Store"
                item["storeLogo"] = store.get("logo")
                item["currency"] = "USD"
                item["thumb"] = safe_url(item.get("thumb"))
                try:
                    item["gameTitle"] = str(item.get("title") or "")[:300]
                    item["savings"] = float(item.get("savings")) if item.get("savings") not in (None, "") else None
                    item["price"] = float(item.get("salePrice"))
                    item["retailPrice"] = float(item.get("normalPrice"))
                except (TypeError, ValueError):
                    continue
                # Attach RAWG facts only on a strict normalized title match.
                candidate = db.execute("SELECT * FROM games WHERE rawg_id IS NOT NULL AND lower(name)=lower(?) LIMIT 1", (item["gameTitle"],)).fetchone()
                candidate_dict = game_dict(candidate, db) if candidate else None
                item["gamevaultGame"] = candidate_dict
                item["playscapeGame"] = candidate_dict
                results.append(item)
        with connect() as db:
            db.execute("INSERT OR REPLACE INTO deals_cache(query_key,payload_json,page_count,checked_at,expires_at) VALUES(?,?,?,?,?)", (cache_key, json_text(results), pages, utc_now(), now + DEALS_CACHE_SECONDS))
        return {"results": results, "page": page, "pageSize": page_size, "pages": pages, "cached": False, "checkedAt": utc_now(), "state": "available", "source": "CheapShark"}
    except cheapshark.CheapSharkError as exc:
        service_metric("cheapshark", str(exc), exc.status == 429)
        if cache:
            data = parse_json(cache["payload_json"], [])
            return {"results": data, "page": page, "pageSize": page_size, "pages": cache["page_count"], "cached": True, "fresh": False, "checkedAt": cache["checked_at"], "state": "stale", "error": str(exc), "source": "CheapShark"}
        return {"results": [], "page": page, "pageSize": page_size, "pages": 0, "cached": False, "state": "unavailable", "error": str(exc), "source": "CheapShark"}


def status_dict() -> dict:
    with connect() as db:
        sync = dict(db.execute("SELECT * FROM sync_state WHERE id=1").fetchone())
        metadata = dict(db.execute("SELECT * FROM metadata_state WHERE id=1").fetchone())
        count = db.execute("SELECT count(*) FROM games WHERE rawg_id IS NOT NULL").fetchone()[0]
        upcoming_count = db.execute("SELECT count(*) FROM games WHERE rawg_id IS NOT NULL AND (tba=1 OR released>?)", (datetime.now(timezone.utc).date().isoformat(),)).fetchone()[0]
        detailed = db.execute("SELECT count(*) FROM games WHERE rawg_id IS NOT NULL AND detail_status='complete'").fetchone()[0]
        price_count = db.execute("SELECT count(*) FROM price_cache WHERE payload_json IS NOT NULL").fetchone()[0]
        metrics = {row["service"]: dict(row) for row in db.execute("SELECT * FROM service_metrics")}
    for key in ("id",):
        sync.pop(key, None)
        metadata.pop(key, None)
    cheap_metric = metrics.get("cheapshark", {})
    return {**sync, "catalog_games": count, "upcoming_games": upcoming_count, "detailed_games": detailed,
            "rawg_key_configured": bool(rawg.api_key()), "admin_protected": bool(ADMIN_TOKEN),
            "metadata": metadata, "cheapshark": {"last_price_request": cheap_metric.get("last_request_at"),
                "cached_price_results": price_count, "api_errors": cheap_metric.get("error_count", 0),
                "rate_limit_errors": cheap_metric.get("rate_limit_errors", 0),
                "retry_after_seconds": cheapshark.retry_after_seconds(), "stores_cached": len(stores_cached_readonly())}}


def stores_cached_readonly() -> list:
    with connect() as db:
        row = db.execute("SELECT payload_json FROM app_cache WHERE cache_key='cheapshark:stores'").fetchone()
    return parse_json(row[0], []) if row else []


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310000)
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)).hex()
        return secrets.compare_digest(candidate, digest_hex)
    except (ValueError, TypeError):
        return False


class Handler(SimpleHTTPRequestHandler):
    server_version = "PLAYSCAPE/2.0"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        static_media = {".avif", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".svg", ".webp", ".woff", ".woff2"}
        cache_policy = "public, max-age=300" if Path(urlparse(self.path).path).suffix.lower() in static_media else "no-store"
        self.send_header("Cache-Control", cache_policy)
        if self.path.startswith("/api/"):
            origin = self.headers.get("Origin", "")
            host_header = self.headers.get("Host", "")
            allowed = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}", "http://127.0.0.1:4173", "http://localhost:4173"}
            if host_header:
                host_only = host_header.split(":")[0]
                allowed.add(f"http://{host_header}")
                allowed.add(f"https://{host_header}")
                allowed.add(f"http://{host_only}:{PORT}")
                allowed.add(f"https://{host_only}")
                allowed.add(f"http://{host_only}:4173")
            if origin:
                try:
                    parsed_origin = urlparse(origin)
                    if (
                        parsed_origin.port in {PORT, 4173}
                        or parsed_origin.hostname in {"127.0.0.1", "localhost", "192.168.3.108"}
                        or (host_header and parsed_origin.hostname == host_header.split(":")[0])
                    ):
                        allowed.add(origin)
                except Exception:
                    pass
            configured = os.environ.get("GAMEVAULT_CORS_ORIGIN", os.environ.get("PLAYSCAPE_CORS_ORIGIN", "")).strip()
            if configured:
                if configured == "*":
                    allowed.add(origin)
                else:
                    for item in configured.split(","):
                        cleaned = item.strip().rstrip("/")
                        if cleaned:
                            allowed.add(cleaned)
                            if not cleaned.startswith("http://") and not cleaned.startswith("https://"):
                                allowed.add(f"https://{cleaned}")
                                allowed.add(f"http://{cleaned}")
            if origin in allowed:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Credentials", "true")
                self.send_header("Vary", "Origin")
        super().end_headers()

    def json_response(self, data: object, status: int = 200, headers: dict[str, str] | None = None) -> None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def read_body(self, max_bytes: int = 16384) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid request body length.")
        if length < 0 or length > max_bytes:
            raise ValueError("Request body is too large.")
        body = self.rfile.read(length) if length else b"{}"
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object.")
        return value

    def session_user(self) -> sqlite3.Row | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        if not morsel:
            return None
        digest = hashlib.sha256(morsel.value.encode()).hexdigest()
        with connect() as db:
            return db.execute("SELECT u.id,u.email FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?", (digest, int(time.time()))).fetchone()

    def session_cookie(self, token: str, max_age: int = SESSION_DAYS * 86400) -> str:
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"

    def check_admin(self) -> bool:
        supplied = self.headers.get("X-Admin-Key", "")
        return bool(ADMIN_TOKEN and secrets.compare_digest(supplied, ADMIN_TOKEN))

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Admin-Key")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path, params = parsed.path, parse_qs(parsed.query)
        if path in {"/deals", "/deals/", "/games", "/games/"}:
            self.path = "/index.html" + (f"?{parsed.query}" if parsed.query else "")
            return super().do_GET()
        try:
            if path == "/api/health":
                return self.json_response({"ok": True, "service": "PLAYSCAPE API", "time": utc_now()})
            if path == "/api/catalog/status":
                return self.json_response(status_dict())
            if path == "/api/home":
                return self.json_response(home_shelves())
            if path == "/api/genres":
                return self.json_response(genres_dict())
            if path == "/api/filters":
                with connect() as db:
                    platforms = [r[0] for r in db.execute("SELECT name FROM game_platforms GROUP BY name ORDER BY name COLLATE NOCASE")]
                    genres = [r[0] for r in db.execute("SELECT name FROM genres GROUP BY name ORDER BY name COLLATE NOCASE")]
                    tags = [r[0] for r in db.execute("SELECT name FROM game_tags GROUP BY name ORDER BY count(*) DESC,name COLLATE NOCASE LIMIT 100")]
                return self.json_response({"platforms": platforms, "genres": genres, "tags": tags, "source": "RAWG local catalog"})
            if path in {"/api/games", "/api/search"}:
                return self.json_response(search_catalog(params))
            if path == "/api/home":
                return self.json_response(home_shelves())
            if path == "/api/deals":
                return self.json_response(deals_payload(params))
            if path == "/api/stores":
                return self.json_response({"results": stores_cached(), "source": "CheapShark"})
            if path.startswith("/api/games/"):
                tail = path[len("/api/games/"):].split("/")
                if len(tail) == 2 and tail[1] in {"prices", "deals"}:
                    if not tail[0].isdigit():
                        return self.json_response({"error": "Use the PLAYSCAPE database game ID."}, 400)
                    result = price_payload(int(tail[0]), force=params.get("refresh", [""])[0] == "1")
                    return self.json_response(result)
                if len(tail) != 1 or not tail[0].isdigit():
                    return self.json_response({"error": "Use the numeric PLAYSCAPE database game ID."}, 400)
                game_id = int(tail[0])
                with connect() as db:
                    row = db.execute("SELECT * FROM games WHERE id=? AND rawg_id IS NOT NULL", (game_id,)).fetchone()
                if not row:
                    return self.json_response({"error": "Game not found in the synchronized RAWG catalog."}, 404)
                refresh = params.get("refresh", [""])[0] == "1"
                should_load = rawg.api_key() and (refresh or row["detail_status"] in {"catalog", "failed"})
                if should_load:
                    try:
                        enrich_game(game_id, force=refresh)
                    except rawg.RawgError:
                        pass
                    with connect() as db:
                        row = db.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
                with connect() as db:
                    return self.json_response(game_dict(row, db))
            if path == "/api/auth/session":
                user = self.session_user()
                return self.json_response({"authenticated": bool(user), "user": dict(user) if user else None})
            if path == "/api/wishlist":
                user = self.session_user()
                if not user:
                    return self.json_response({"error": "Sign in to sync a wishlist.", "requiresAuth": True}, 401)
                with connect() as db:
                    ids = [int(row[0]) for row in db.execute("SELECT game_id FROM wishlist WHERE user_id=? ORDER BY created_at DESC", (user["id"],))]
                    rows = db.execute("SELECT g.* FROM games g JOIN wishlist w ON w.game_id=g.id WHERE w.user_id=? AND g.rawg_id IS NOT NULL ORDER BY w.created_at DESC LIMIT 2000", (user["id"],)).fetchall()
                    games = [game_dict(row, db) for row in rows]
                return self.json_response({"appIds": ids, "games": games, "source": "PLAYSCAPE account"})
            if path in {"/api/admin/sync", "/api/admin/status"}:
                if not self.check_admin():
                    return self.json_response({"error": "Admin access denied."}, 401)
                return self.json_response(status_dict())
            return super().do_GET()
        except KeyError as exc:
            return self.json_response({"error": str(exc).strip("'")}, 404)
        except rawg.RawgError as exc:
            return self.json_response({"error": str(exc), "retryAfter": exc.retry_after}, exc.status or 502)
        except cheapshark.CheapSharkError as exc:
            return self.json_response({"error": str(exc), "retryAfter": exc.retry_after}, exc.status or 502)
        except (sqlite3.Error, ValueError, TypeError) as exc:
            return self.json_response({"error": str(exc)[:300]}, 400)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self.read_body()
            if path in {"/api/auth/register", "/api/auth/login"}:
                email = str(body.get("email", "")).strip().casefold()
                password = str(body.get("password", ""))
                if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                    return self.json_response({"error": "Enter a valid email address."}, 400)
                if len(password) > 128 or len(password) < 8:
                    return self.json_response({"error": "Password must be at least 8 characters."}, 400)
                with connect() as db:
                    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                    if path.endswith("register"):
                        if user:
                            return self.json_response({"error": "An account with this email already exists."}, 409)
                        cursor = db.execute("INSERT INTO users(email,password_hash,created_at) VALUES(?,?,?)", (email, password_hash(password), utc_now()))
                        user_id = cursor.lastrowid
                    else:
                        if not user or not verify_password(password, user["password_hash"]):
                            return self.json_response({"error": "Email or password is incorrect."}, 401)
                        user_id = user["id"]
                    token = secrets.token_urlsafe(32)
                    db.execute("DELETE FROM sessions WHERE expires_at<?", (int(time.time()),))
                    db.execute("INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user_id, int(time.time()) + SESSION_DAYS * 86400, utc_now()))
                    user_result = db.execute("SELECT id,email FROM users WHERE id=?", (user_id,)).fetchone()
                return self.json_response({"authenticated": True, "user": dict(user_result)}, 201 if path.endswith("register") else 200, {"Set-Cookie": self.session_cookie(token)})
            if path == "/api/auth/logout":
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                morsel = cookie.get(SESSION_COOKIE)
                if morsel:
                    with connect() as db:
                        db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(morsel.value.encode()).hexdigest(),))
                return self.json_response({"authenticated": False}, headers={"Set-Cookie": self.session_cookie("", 0)})
            if path == "/api/wishlist":
                user = self.session_user()
                if not user:
                    return self.json_response({"error": "Sign in to sync a wishlist.", "requiresAuth": True}, 401)
                raw_ids = body.get("appIds")
                if not isinstance(raw_ids, list) or len(raw_ids) > 2000:
                    return self.json_response({"error": "Wishlist must contain at most 2,000 PLAYSCAPE game IDs."}, 400)
                ids = sorted(set(int(value) for value in raw_ids if str(value).isdigit() and 0 < int(value) < 2**32))
                with connect() as db:
                    db.execute("DELETE FROM wishlist WHERE user_id=?", (user["id"],))
                    now = utc_now()
                    db.executemany("INSERT OR IGNORE INTO wishlist(user_id,game_id,created_at) SELECT ?,id,? FROM games WHERE id=? AND rawg_id IS NOT NULL", [(user["id"], now, game_id) for game_id in ids])
                    saved = [int(row[0]) for row in db.execute("SELECT game_id FROM wishlist WHERE user_id=? ORDER BY game_id", (user["id"],))]
                return self.json_response({"appIds": saved, "source": "PLAYSCAPE account"})
            if path.startswith("/api/admin/"):
                if not self.check_admin():
                    return self.json_response({"error": "Admin access denied."}, 401)
                if path == "/api/admin/sync":
                    if not rawg.api_key():
                        return self.json_response({"error": "Set RAWG_API_KEY in the server environment and restart PLAYSCAPE before syncing."}, 409)
                    mode = str(body.get("mode", "continue"))
                    page_limit = body.get("pageLimit", RAWG_PAGES_PER_RUN)
                    if sync_lock.locked():
                        return self.json_response(status_dict(), 202)
                    threading.Thread(target=run_rawg_sync, args=(mode, page_limit), name="playscape-rawg-sync", daemon=True).start()
                    return self.json_response({"status": "queued", "mode": mode, "pageLimit": max(1, min(RAWG_PAGES_PER_RUN, int(page_limit))), "message": "Bounded RAWG page sync queued."}, 202)
                if path == "/api/admin/sync-upcoming":
                    if not rawg.api_key():
                        return self.json_response({"error": "Set RAWG_API_KEY in the server environment and restart PLAYSCAPE before syncing."}, 409)
                    page_limit = body.get("pageLimit", RAWG_PAGES_PER_RUN)
                    if sync_lock.locked():
                        return self.json_response({"error": "A RAWG catalog sync is already running."}, 409)
                    threading.Thread(target=run_rawg_upcoming_sync, args=(page_limit,), name="playscape-rawg-upcoming", daemon=True).start()
                    return self.json_response({"status": "queued", "pageLimit": max(1, min(RAWG_PAGES_PER_RUN, int(page_limit))), "message": "Bounded RAWG upcoming-release sync queued."}, 202)
                if path == "/api/admin/retry-failed":
                    if not rawg.api_key():
                        return self.json_response({"error": "Set RAWG_API_KEY in the server environment first."}, 409)
                    if metadata_job_lock.locked():
                        return self.json_response({"error": "A metadata retry is already running."}, 409)
                    threading.Thread(target=run_failed_retry, name="playscape-rawg-retry", daemon=True).start()
                    return self.json_response({"status": "queued", "message": "Retrying a bounded batch of failed RAWG details."}, 202)
                if path.startswith("/api/admin/games/") and path.endswith("/refresh"):
                    pieces = path.strip("/").split("/")
                    if len(pieces) != 5 or not pieces[3].isdigit():
                        return self.json_response({"error": "Use /api/admin/games/{database-id}/refresh."}, 400)
                    game_id = int(pieces[3])
                    if not rawg.api_key():
                        return self.json_response({"error": "Set RAWG_API_KEY in the server environment first."}, 409)
                    threading.Thread(target=refresh_one_game, args=(game_id,), name=f"playscape-rawg-game-{game_id}", daemon=True).start()
                    return self.json_response({"status": "queued", "gameId": game_id})
                if path == "/api/admin/metadata-refresh":
                    if not rawg.api_key():
                        return self.json_response({"error": "Set RAWG_API_KEY in the server environment first."}, 409)
                    if metadata_job_lock.locked():
                        return self.json_response({"error": "A metadata retry is already running."}, 409)
                    threading.Thread(target=run_failed_retry, name="playscape-rawg-retry", daemon=True).start()
                    return self.json_response({"status": "queued", "message": "Bounded failed-detail retry queued."}, 202)
                if path == "/api/admin/clear-stale-cache":
                    now = int(time.time())
                    with connect() as db:
                        prices = db.execute("DELETE FROM price_cache WHERE coalesce(expires_at,0)<?", (now,)).rowcount
                        deals = db.execute("DELETE FROM deals_cache WHERE expires_at<?", (now,)).rowcount
                        app_cache = db.execute("DELETE FROM app_cache WHERE expires_at<?", (now,)).rowcount
                        db.execute("DELETE FROM game_prices WHERE game_id NOT IN (SELECT game_id FROM price_cache)")
                    return self.json_response({"status": "complete", "cleared": {"priceCaches": prices, "dealPages": deals, "serviceCaches": app_cache}})
                if path == "/api/admin/refresh-stores":
                    result = stores_cached(force=True)
                    return self.json_response({"status": "complete", "stores": len(result)})
                return self.json_response({"error": "Admin route not found."}, 404)
            return self.json_response({"error": "Route not found."}, 404)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, sqlite3.IntegrityError) as exc:
            return self.json_response({"error": str(exc)[:300]}, 400)
        except sqlite3.Error:
            return self.json_response({"error": "Database operation failed."}, 500)

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        try:
            if path != "/api/wishlist":
                return self.json_response({"error": "Route not found."}, 404)
            body = self.read_body()
            user = self.session_user()
            if not user:
                return self.json_response({"error": "Sign in to sync a wishlist.", "requiresAuth": True}, 401)
            raw_ids = body.get("appIds")
            if not isinstance(raw_ids, list) or len(raw_ids) > 2000:
                return self.json_response({"error": "Wishlist must contain at most 2,000 PLAYSCAPE game IDs."}, 400)
            ids = sorted(set(int(value) for value in raw_ids if str(value).isdigit() and 0 < int(value) < 2**32))
            with connect() as db:
                db.execute("DELETE FROM wishlist WHERE user_id=?", (user["id"],))
                now = utc_now()
                db.executemany("INSERT OR IGNORE INTO wishlist(user_id,game_id,created_at) SELECT ?,id,? FROM games WHERE id=? AND rawg_id IS NOT NULL", [(user["id"], now, game_id) for game_id in ids])
                saved = [int(row[0]) for row in db.execute("SELECT game_id FROM wishlist WHERE user_id=? ORDER BY game_id", (user["id"],))]
            return self.json_response({"appIds": saved, "source": "PLAYSCAPE account"})
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            return self.json_response({"error": str(exc)[:300]}, 400)


def refresh_one_game(game_id: int) -> None:
    try:
        enrich_game(game_id, force=True)
    except rawg.RawgError:
        pass


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        import sys
        exc = sys.exception()
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main() -> None:
    initialize()
    server = QuietServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"GAMEVAULT API and preview: http://{HOST}:{PORT}", flush=True)
    print(f"SQLite database: {DB_PATH}", flush=True)
    if not rawg.api_key():
        print("RAWG catalog import is paused until RAWG_API_KEY is set in the server environment.", flush=True)
    if not ADMIN_TOKEN:
        print("Admin actions are disabled until GAMEVAULT_ADMIN_TOKEN (or PLAYSCAPE_ADMIN_TOKEN) is set.", flush=True)
    start_auto_sync()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()


