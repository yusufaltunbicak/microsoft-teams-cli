# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Python CLI tool for Microsoft Teams that uses MSAL localStorage token extraction via Playwright — no Azure app registration, admin consent, or API keys required. Entry point: `teams` command.

## Build & Run

```sh
pip install -e .              # editable install (hatchling build system)
playwright install chromium   # required for auth
teams login                   # first-time: opens browser, extracts MSAL tokens
teams chats                   # verify it works
python -m pytest              # run mocked/unit suite
```

`pytest` test suite exists under `tests/` and includes unit, mocked integration, and opt-in live smoke coverage. No linter configured.

## Architecture

### Five API layers

1. **IC3 Chat Service** (`teams.cloud.microsoft/api/chatsvc/{region}/v1/`) — primary API for chats, messages, conversations, group chat creation, message edit/delete. Uses IC3 bearer token (aud: `ic3.teams.office.com`).
2. **Substrate Search** (`substrate.office.com/searchservice/api/v2/query`) — primary search API. Uses Substrate token (aud: `substrate.office.com`). Falls back to scanning recent chats if no substrate token.
3. **Graph API** (`graph.microsoft.com/v1.0/`) — user search, file uploads, 1:1 chat creation. Uses Graph token when available, IC3 fallback.
4. **UPS Presence** (`teams.cloud.microsoft/ups/{region}/v1/`) — presence read (`getpresence`) and write (`forceavailability`). Used for both get and set status (Graph presence endpoints are unreliable).

### Module responsibilities

- **`cli.py`** — Entry point with ASCII banner. Custom `TeamsGroup(click.Group)` shows banner on `--help`. Imports `commands/` package.
- **`commands/`** — Command modules, each with a `register(cli)` function:
  - `_common.py` — Shared helpers: `_get_client`, `_handle_api_error`, `cfg`, `should_json`, `_parse_schedule_time`, `_format_size`, `VALID_REACTIONS`.
  - `auth.py` — `login` (with `--with-token` for CI/CD), `whoami`
  - `chat.py` — `chats`, `chat`, `read`, `unread`
  - `send.py` — `chat-send`, `reply`, `send`, `send-file`
  - `search.py` — `search`, `user-search`
  - `reactions.py` — `react`, `unreact` (multi-ID)
  - `message_manage.py` — `edit`, `delete`
  - `group_chat.py` — `group-chat`, `forward`
  - `mark_read.py` — `mark-read` (supports `--unread`, `--chat`)
  - `schedule.py` — `schedule`, `schedule-list`, `schedule-cancel`, `schedule-run`
  - `presence.py` — `status`, `set-status`
  - `summary.py` — `summary` (parallel API dashboard: status + unreads + recent)
  - `attachments.py` — `attachments`
  - `cache.py` — `sync` (bounded read-only history import, optional foreground
    `--watch` loop), `cache status`, `cache clear` (local-only deletion)
- **`exceptions.py`** — Structured hierarchy: `TeamsCliError` → `TokenExpiredError`, `RateLimitError`, `ResourceNotFoundError`, `AuthRequiredError`, `ApiError`.
- **`client.py`** — `TeamsClient` wraps multi-token routing (IC3/Graph/MT/UPS). Two-level ID mapping. HTTP helpers (`_ic3_get`, `_ic3_post`, `_ic3_put`, `_ic3_delete`, `_graph_get`, `_ups_put`, etc.) handle jitter/auth/response parsing.
- **`auth.py`** — Playwright-based login + `login_with_token()` for CI/CD (stdin). Opens Teams web, extracts MSAL tokens from localStorage via JS evaluation. Classifies by audience (`ic3`, `graph`, `presence`, `csa`, `substrate`). Detects region from GTM localStorage key.
- **`msal_cache.py`** — Parses legacy and `msal.2|...` credential records, selects
  the same account/resource, and supplies saved scopes for silent renewal. Never
  print credential records or refresh-token responses.
- **`metadata_cache.py`** — Persistent account-scoped name/title cache in
  `metadata.json`. Whitelisted fields exclude message bodies/previews. User names
  expire after seven days; titles after one day. Atomic `0600` writes and a file
  lock protect concurrent CLI processes. Explicit Graph user 404 markers expire
  after five minutes and native names replace them. A Graph presence 403 denial
  with an available UPS token is cached for one hour, then probed again; 401,
  throttles and transport errors never create such a marker. Unchanged fresh
  metadata retains its expiry and avoids rewriting the file.
- **`history.py`** — Explicit opt-in SQLite FTS5 message index in
  `history.sqlite3`. `sync_history()` follows bounded same-origin IC3 pagination;
  `search --local` has no network/auth-renewal dependency. Retains plain message
  text plus sender/chat metadata with `0600` permissions and account isolation.
  Local SQL filters and word-prefix matching use Turkish normalization. Coverage
  records describe requested cutoff, last sync, completeness and timestamp bounds;
  `complete` applies to the requested window in selected chats, not all Teams.
  Incomplete coverage records `page_limit`, `access_denied`, `unavailable` or
  `cursor_repeat`; a permission-denied chat does not abort every other chat.
- **`context.py`** — Additive neighbor-count metadata for live/local context;
  anchors match conversation and message IDs normalized as strings.
- **`anti_detection.py`** — `BrowserSession`: request jitter, full browser headers (Sec-Fetch-*, sec-ch-ua-*), proxy support, configurable timeout.
- **`models.py`** — Dataclasses (User, Chat, Message, Reaction, Attachment) with `from_api()` class methods that normalize various API response formats.
- **`formatter.py`** — Rich tables with ROUNDED borders, chat type icons (`│` 1:1, `○` group, `□` meeting), colored unread badges, status dots (`STATUS_DOTS`/`STATUS_COLORS`), `print_status()`. `Console(stderr=True)` so JSON piping to stdout stays clean.
- **`serialization.py`** — JSON envelope format `{ok, schema_version, data}` with `to_json()` and `to_json_error()`. Auto-JSON when stdout is piped (`is_piped()`).
- **`scheduler.py`** — Local scheduled message tracking (Teams has no native scheduled send).
- **`config.py`** — YAML config with deep-merge defaults. Includes `timeout` setting.
- **`constants.py`** — URLs, paths, headers, client ID. All API base URLs use `{region}` template.

### Key patterns

- **Two-level ID mapping**: Chats get `#1, #2...`, messages also get `#1, #2...` (globally, not per-chat). Stored in `id_map.json` with `chats` and `messages` sections. Max 500 entries per section (LRU eviction).
- **Token routing**: `_ic3_get`/`_ic3_post`/`_ic3_put`/`_ic3_delete` for chat service, `_graph_get`/`_graph_post` for Graph, `_ups_post`/`_ups_put` for presence. All HTTP methods go through `_request_with_retry` which handles 429 rate limiting with automatic exponential backoff (up to 3 retries).
- **Multi-token auth**: MSAL stores multiple tokens in localStorage. Explicit login extracts `ic3`, `graph`, `presence`, `csa`, `substrate` by audience and keeps polling briefly after IC3 appears so secondary tokens are cached too. Ordinary commands use the environment override or saved credentials, then saved MSAL access/refresh grants; interactive Playwright login is only an explicit `teams login` action. `--with-token` imports credentials from stdin.
- **Non-interactive login**: `teams login --with-token` reads from stdin (plain IC3 token or JSON bundle). Supports `--region` flag. For CI/CD, cron jobs, and automation pipelines.
- **Region-specific endpoints**: All API URLs include region (emea/amer/apac), auto-detected from GTM localStorage during login.
- **HTML messages**: Teams content is always HTML (`<p>text</p>`). `send_message()` wraps plain text in `<p>` tags. `_strip_html()` uses BeautifulSoup for display.
- **JSON envelope**: All `--json` output uses `{ok: true, schema_version: "1.0", data: ...}` format. Errors return `{ok: false, error: "message"}`. Auto-JSON when stdout is piped (no `--json` flag needed).
- **Send safety**: `send` command re-ranks search results by name match (not API relevance). Refuses to send with `-y` when no exact match found. Self-messages route to `48:notes` thread.
- **Multi-ID operations**: `react`, `unreact`, `mark-read` accept multiple message numbers via `nargs=-1`.
- **Pagination**: All list commands support `--offset` to skip items.
- **Group chat creation**: Uses IC3 `/threads` API with minimal payload `{members, properties: {threadType: "chat"}}`. Topic set separately via `/threads/{id}/properties?name=topic`. Reverse-engineered from Teams web client.
- **Message edit/delete**: Edit uses IC3 PUT on message, delete uses IC3 DELETE with `{deletetime: unix_ms}`.
- **Set status**: Uses UPS `PUT /me/forceavailability/` (not Graph `setUserPreferredPresence` which is unreliable). Supports `desiredExpirationTime` for timed status.
- **Unread detection**: Compares `properties.consumptionhorizon` timestamp (read position) with `lastMessage.composetime`. Skips marking as unread when the last message sender is the current user (prevents false positives for self-sent messages). The `consumptionhorizon` is in the top-level `properties` dict, NOT in `threadProperties`.
- **Mark chat read**: `mark_chat_read()` sets `consumptionhorizon` to current time on a conversation. `mark-read --chat` flag accepts chat numbers directly (resolves via `_resolve_chat_id`).
- **Mark unread**: Uses `consumptionHorizonBookmark` property (not `consumptionhorizon`). Reverse-engineered from Teams web client.
- **Silent auth**: `get_tokens()` and `refresh_tokens()` use saved MSAL refresh
  grants per audience, with a 60-second expiry buffer, a bounded deadline and an
  account check. HTTP helpers refresh the actual resource token, including
  secondary search/Graph/presence tokens. A 401 permits one silent refresh/retry
  only for reads (including explicitly read-only POST envelopes); writes are not
  replayed after auth, transport or server failures.
  Commands never call interactive Playwright login implicitly. MFA/conditional
  access stops with `AuthRequiredError` (exit 4); the user runs `teams login`.
  `teams login --silent` explicitly renews saved credentials without Chromium.
  Never retry a whole mutation after auth failure: retry belongs to the HTTP helper.
- **Presence fallback**: `get_presence()` tries Graph first, then falls back to Teams UPS using the presence token when Graph `/me/presence` returns 401 or 403.
- **Summary dashboard**: `teams summary` uses `ThreadPoolExecutor(max_workers=3)` to fetch presence, recent chats, and unread chats in parallel.
- **1:1 chat resolution**: `_find_existing_1on1` checks the OTHER party in conv_id (not substring match, which would match own ID in every chat). Self-sends use `48:notes`.
- **Output convention**: Every list command supports `--json` flag. Rich tables go to stderr (`Console(stderr=True)`), JSON goes to stdout via `click.echo()`. Piped stdout auto-triggers JSON envelope.
- **Send confirmation**: `send`, `chat-send`, `reply`, `react`, `unreact`, `edit`, `delete`, `forward`, `group-chat`, `schedule`, `send-file`, `set-status`, `mark-read` show details and require `-y` to skip.
- **Anti-detection**: `BrowserSession.jitter()` keeps defaults of 0.3s reads and
  2.0s writes; values now come from config. `browser_headers()` adds Sec-Fetch-*,
  sec-ch-ua-* headers. A Graph `$batch` carrying only GET subrequests is a read;
  individual 429 responses must respect their own retry policy.
- **Name/chat resolution**: Reuse persisted user display names and chat titles;
  combine cold Graph lookups in batches of at most 20 GET subrequests. Resolve a
  chat selector by exact name before a unique partial match. Ambiguous selectors
  fail safely. Title lookup for server search must not trigger a new full chat
  listing when search results or metadata already supply it.
- **Search filters**: Push sender/chat/date constraints into Substrate's index
  query before its top-hit limit, then validate returned hits client-side.
  Date-only or timezone-free input is UTC; `search --before YYYY-MM-DD` includes
  that UTC day. ISO input with explicit offsets supports local-day boundaries.
- **Local research**: Message text enters the persistent index only through
  explicit `sync` (default 50 chats, 60 days, five 100-message pages/chat).
  `sync --watch N` repeats in the foreground with a minimum 60-second interval;
  no daemon is installed. `search --local --context N` uses indexed neighbors;
  live `search --context N` and `read --context N` use bounded message-anchor
  reads. Control-event-heavy windows use at most three window reads, capped at 200
  raw events per request; a missing anchor can need one direct-message read.
  The context fields are additive and requested explicitly
  (0-10 each side); `context_meta` reports actual neighbor counts and partial windows.
  Local JSON preserves `{ok, schema_version, data}` and adds
  `meta: {source: "local", coverage: ...}`. Never call indexed results exhaustive.
  Repeated sync updates edits/deletions it sees; untouched historical pages can
  remain stale. Preserve a live server-search path.
  `--days` bounds an import, not retention; earlier imported rows remain until
  cache deletion. Stop foreground watchers before deleting their cache.
- **Deleted messages**: Live reads and sync exclude nonzero `properties.deletetime`
  and `Control/MessageDelete` tombstones using the same policy. Zero markers are
  live. A deleted anchor fails with not-found without exposing retained text.

### Cache & config locations

- Cache: `~/.cache/teams-cli/` (tokens.json, browser-state.json, id_map.json,
  scheduled.json, user_profile.json, metadata.json, history.sqlite3). New
  caches/locks use `0600`.
  `metadata.json` contains names/titles/member IDs and finite short-lived
  missing-user/presence-capability markers, no message text. It can be
  removed without deleting auth or schedules. `cache.metadata: false` disables it.
- `teams cache clear --messages-only` deletes the SQLite history and companion
  files for all cached accounts; `teams cache clear` also deletes metadata.
  `--metadata-only` resets names/missing-user/capability markers while keeping
  message history. It cannot be combined with `--messages-only`.
  Confirmation/`--yes`, `--dry-run`, `--force` and `--no-input` apply to this local
  deletion. Tokens, browser state, ID maps and schedules must survive. No need to
  contact Teams to inspect or clear local caches.
- Config: `~/.config/teams-cli/config.yaml`
- Overridable via `TEAMS_CLI_CACHE` and `TEAMS_CLI_CONFIG` env vars

### Environment variables

| Variable | Description |
|----------|-------------|
| `TEAMS_IC3_TOKEN` | Override IC3 token (skip login) |
| `TEAMS_REGION` | Override region (default: auto-detected) |
| `TEAMS_PROXY` | HTTP proxy URL |
| `TEAMS_TIMEOUT` | HTTP request timeout in seconds (default: 30) |
| `TEAMS_CLI_CACHE` | Cache directory (default: `~/.cache/teams-cli`) |
| `TEAMS_CLI_CONFIG` | Config directory (default: `~/.config/teams-cli`) |

### Dependencies

click, rich, httpx, playwright, PyYAML, beautifulsoup4. Python >=3.10. Build: hatchling.

### Benchmark and privacy

`python3 scripts/benchmark_read.py --label current --runs 3 --cache-mode warm --skip-preflight`
runs a finite read-only command allowlist using captured subprocess output. Only
elapsed times, return codes, finite error labels and numeric counts are retained
in `~/.cache/teams-cli/benchmark-*.json` (`0600`). Cold mode backs up/restores only
`metadata.json`; tokens, browser state, ID maps and schedules are untouched. Use
`--skip-preflight` only with silent auth, never the legacy auto-browser version.
Run sequentially, with no other CLI processes during cold-cache sampling. Keep
account/message contents out of benchmark reports and repository commits.
`PERFORMANCE.md` records alternatives, source links and measurement limits.
