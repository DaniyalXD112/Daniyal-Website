# GAMEVAULT

GAMEVAULT is a premium game discovery site. RAWG supplies game metadata, the local SQLite database serves normal browsing, and CheapShark supplies on-demand PC prices and deal links. The app does not depend on the Steam API.

## Run locally

Python 3.10+ is enough; the API uses the standard library and SQLite.

From this folder in PowerShell, set the credentials for the current terminal session and start the server:

```powershell
$env:RAWG_API_KEY = "paste your RAWG key here"
$env:GAMEVAULT_ADMIN_TOKEN = "a long private admin token"
python .\server.py
```

Then open `http://127.0.0.1:4174/`. If the design preview is served from port 4173, it calls the API on port 4174. The RAWG key and admin token are server-only; never put them in HTML, JavaScript, or a committed file.

When `RAWG_API_KEY` is configured, the server automatically starts a bounded RAWG refresh in the background each time it starts. It imports up to ten pages (40 games per page) for the main catalog and the upcoming-release catalog, so games appear in the site as soon as the first page is stored. Later server starts continue a paused import or refresh stale data. The admin page remains available for an immediate manual import. Without the key, already imported games remain available and the dashboard reports that RAWG is not configured.

## Catalog and admin sync

Open `/admin.html` to monitor imports or run one manually. The main catalog and upcoming releases each use a bounded batch of up to 10 pages (40 games per page) and store a continuation cursor in SQLite. Later server starts continue paused imports. **Incremental sync** checks a smaller recent window; failed detail fetches can be retried, and individual game details can be refreshed. This avoids sending the entire catalog to the browser or attempting one unbounded import.

The site uses separate identifiers for its internal database rows, RAWG IDs, CheapShark IDs, and Steam App IDs. RAWG's actual Steam store link is shown only when RAWG supplies it. CheapShark matching prefers the Steam App ID and otherwise requires an exact, unique normalized title; ambiguous matches are not shown.

## Prices and deals

Prices load when a user opens a game's price section or the deals page. Cached prices last up to two hours, deals pages one hour, and store data one day. Repeated requests for the same game's prices are deduplicated while a lookup is in progress. CheapShark rate limits are respected, and its deals use the official redirect URL. Prices are in USD, as returned by the service.

## Accounts and wishlist

The existing device wishlist remains usable without signing in. Registration and login use the local API, PBKDF2-HMAC-SHA256 password hashes, and HTTP-only SameSite session cookies. Signed-in wishlists are stored against GAMEVAULT database IDs. For public hosting, use HTTPS and configure the appropriate origin and deployment controls before opening registration publicly.

## Data attribution

Game catalog pages link to [RAWG](https://rawg.io/) as the metadata source. The footer also links to [CheapShark](https://www.cheapshark.com/) for deal data. See the providers' [RAWG API documentation](https://rawg.io/apidocs) and [CheapShark API documentation](https://apidocs.cheapshark.com/) for current terms and usage limits.

## Service settings

| Variable | Purpose | Default |
| --- | --- | --- |
| `RAWG_API_KEY` | Server-only RAWG credential | Not configured |
| `GAMEVAULT_ADMIN_TOKEN` | Protects catalog administration routes (accepts `PLAYSCAPE_ADMIN_TOKEN`) | Not configured |
| `GAMEVAULT_DB` | SQLite database file | `%LOCALAPPDATA%/GAMEVAULT/gamevault.sqlite3` (outside public web folder) |
| `GAMEVAULT_HOST` | Bind address | `0.0.0.0` |
| `GAMEVAULT_PORT` | API and preview port | `4174` |
| `GAMEVAULT_CORS_ORIGIN` | Additional allowed browser origin | Not configured |

CheapShark does not need an API key. No Steam API key is used.
