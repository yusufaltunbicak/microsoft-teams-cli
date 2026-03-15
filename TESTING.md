# Testing

This repo uses a three-layer test pyramid built around `pytest`.

## Setup

```sh
python -m pip install -e .[test]
python -m pytest
python -m pytest -m live -s
```

Default `pytest` runs exclude live smoke tests. Live tests only run when you explicitly select the `live` marker.

## Pyramid

1. Unit tests
   - Pure parsing, formatting, scheduler behavior, ID mapping, CLI wiring.
2. Mocked integration tests
   - `TeamsClient` HTTP flows with mocked IC3, Graph, CSA, and Substrate responses.
   - Command-to-client behavior with fixtures, monkeypatching, and `respx`.
3. Live smoke tests
   - Read-only auth and chat sanity checks.
   - Opt-in self-targeted send, search, read, react, unreact, file send, and attachment download smoke flow.
   - CLI surface checks for `teams`, `channels`, `status`, and `channel`, including documented tenant-specific `403` limits.

## Command Coverage

- `login`, `whoami`: auth cache/verify behavior plus CLI output.
- `chats`, `chat`, `read`, `unread`: JSON/table/empty states and date filter forwarding.
- `search`, `user-search`, `attachments`: filter forwarding, attachment rendering, download flow.
- `chat-send`, `reply`, `send`, `send-file`, `react`, `unreact`, `status`, `set-status`: confirmation bypass, user resolution, payload wiring.
- `teams`, `channels`, `channel`: listing, empty-state behavior, and CSA fallback for channel reads when Graph is denied.
- `schedule`, `schedule-list`, `schedule-cancel`, `schedule-run`: local scheduler flow and due-message execution.

## Live Smoke Guardrails

- Live tests are marked with `@pytest.mark.live`.
- They are intended to be run manually with an already authenticated local Teams session.
- They only attempt to target the currently authenticated user.
- The write-path uses the built-in self-notes conversation (`48:notes`) instead of a normal DM.
- If self-lookup does not resolve back to the current user, the send/reaction smoke test aborts with `skip`.
- If the self-notes conversation cannot be read safely, no real message is sent.
- Live test messages are prefixed with `[teams-cli live test]` so they are easy to spot.

## Notes

- No CI workflow is configured on purpose.
- `send-file` and attachment download are also covered in live smoke against `48:notes`.
- `status` falls back to the Teams UPS presence endpoint when Graph `/me/presence` is denied.
- `channel` falls back to the Teams CSA channel-thread endpoint when Graph channel messages are denied.
