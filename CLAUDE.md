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

- **`cli.py`** — Thin entry point. Defines Click group, imports `commands/` package.
- **`commands/`** — Command modules, each with a `register(cli)` function:
  - `_common.py` — Shared helpers: `_get_client`, `_handle_api_error`, `cfg`, `should_json`, `_parse_schedule_time`, `_format_size`, `VALID_REACTIONS`.
  - `auth.py` — `login` (with `--with-token` for CI/CD), `whoami`
  - `chat.py` — `chats`, `chat`, `read`, `unread`
  - `send.py` — `chat-send`, `reply`, `send`, `send-file`
  - `search.py` — `search`, `user-search`
  - `reactions.py` — `react`, `unreact` (multi-ID)
  - `message_manage.py` — `edit`, `delete`
  - `group_chat.py` — `group-chat`, `forward`
  - `mark_read.py` — `mark-read` (supports `--unread`)
  - `schedule.py` — `schedule`, `schedule-list`, `schedule-cancel`, `schedule-run`
  - `presence.py` — `status`, `set-status`
  - `attachments.py` — `attachments`
- **`exceptions.py`** — Structured hierarchy: `TeamsCliError` → `TokenExpiredError`, `RateLimitError`, `ResourceNotFoundError`, `AuthRequiredError`, `ApiError`.
- **`client.py`** — `TeamsClient` wraps multi-token routing (IC3/Graph/MT/UPS). Two-level ID mapping. HTTP helpers (`_ic3_get`, `_ic3_post`, `_ic3_put`, `_ic3_delete`, `_graph_get`, `_ups_put`, etc.) handle jitter/auth/response parsing.
- **`auth.py`** — Playwright-based login + `login_with_token()` for CI/CD (stdin). Opens Teams web, extracts MSAL tokens from localStorage via JS evaluation. Classifies by audience (`ic3`, `graph`, `presence`, `csa`, `substrate`). Detects region from GTM localStorage key.
- **`anti_detection.py`** — `BrowserSession`: request jitter, full browser headers (Sec-Fetch-*, sec-ch-ua-*), proxy support, configurable timeout.
- **`models.py`** — Dataclasses (User, Chat, Message, Reaction, Attachment) with `from_api()` class methods that normalize various API response formats.
- **`formatter.py`** — Rich tables output. `Console(stderr=True)` so JSON piping to stdout stays clean.
- **`serialization.py`** — JSON envelope format `{ok, schema_version, data}` with `to_json()` and `to_json_error()`. Auto-JSON when stdout is piped (`is_piped()`).
- **`scheduler.py`** — Local scheduled message tracking (Teams has no native scheduled send).
- **`config.py`** — YAML config with deep-merge defaults. Includes `timeout` setting.
- **`constants.py`** — URLs, paths, headers, client ID. All API base URLs use `{region}` template.

### Key patterns

- **Two-level ID mapping**: Chats get `#1, #2...`, messages also get `#1, #2...` (globally, not per-chat). Stored in `id_map.json` with `chats` and `messages` sections. Max 500 entries per section (LRU eviction).
- **Token routing**: `_ic3_get`/`_ic3_post`/`_ic3_put`/`_ic3_delete` for chat service, `_graph_get`/`_graph_post` for Graph, `_ups_post`/`_ups_put` for presence. All HTTP methods go through `_request_with_retry` which handles 429 rate limiting with automatic exponential backoff (up to 3 retries).
- **Multi-token auth**: MSAL stores multiple tokens in localStorage. We extract `ic3`, `graph`, `presence`, `csa`, `substrate` by audience and keep polling briefly after IC3 appears so secondary tokens are cached too. Token flow: env var `TEAMS_IC3_TOKEN` → cached `tokens.json` → `--with-token` stdin → Playwright login.
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
- **Mark unread**: Uses `consumptionHorizonBookmark` property (not `consumptionhorizon`). Reverse-engineered from Teams web client.
- **Presence fallback**: `get_presence()` tries Graph first, then falls back to Teams UPS using the presence token when Graph `/me/presence` returns 403.
- **1:1 chat resolution**: `_find_existing_1on1` checks the OTHER party in conv_id (not substring match, which would match own ID in every chat). Self-sends use `48:notes`.
- **Output convention**: Every list command supports `--json` flag. Rich tables go to stderr (`Console(stderr=True)`), JSON goes to stdout via `click.echo()`. Piped stdout auto-triggers JSON envelope.
- **Send confirmation**: `send`, `chat-send`, `reply`, `react`, `unreact`, `edit`, `delete`, `forward`, `group-chat`, `schedule`, `send-file`, `set-status`, `mark-read` show details and require `-y` to skip.
- **Anti-detection**: `BrowserSession.jitter()` adds random delay (0.3s reads, 2.0s writes). `browser_headers()` adds Sec-Fetch-*, sec-ch-ua-* headers.

### Cache & config locations

- Cache: `~/.cache/teams-cli/` (tokens.json, browser-state.json, id_map.json, scheduled.json, user_profile.json)
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
