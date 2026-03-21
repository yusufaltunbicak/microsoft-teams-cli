---
name: teams-cli
description: CLI skill for Microsoft Teams to read chats, send messages, search, manage reactions, files, channels, and presence from the terminal without API keys or admin consent
author: yusufaltunbicak
version: "0.4.0"
tags:
  - teams
  - microsoft-teams
  - chat
  - messaging
  - office365
  - terminal
  - cli
---

# teams-cli Skill

Use this skill when the user wants to read, send, search, or manage Microsoft Teams chats and channels from the terminal. No Azure app registration, admin consent, or API keys required.

## Prerequisites

```bash
# Install (requires Python 3.10+)
cd ~/microsoft-teams-cli && pip install -e .
playwright install chromium
```

## Authentication

- First run: `teams login` opens Chromium, user logs in to Teams web, MSAL tokens are auto-extracted from localStorage.
- Extracts multiple tokens by audience: IC3 (chat service), Graph, Presence, Substrate (search).
- Region auto-detected from GTM localStorage key (emea/amer/apac).
- Tokens cached at `~/.cache/teams-cli/tokens.json` (auto-expires based on JWT exp).
- Auto re-login on 401 via cached browser SSO state.
- Or set `TEAMS_IC3_TOKEN` environment variable directly.

```bash
teams login              # Interactive browser login
teams login --force      # Force re-login, ignore saved session
teams login --debug      # Show debug info about token extraction
teams whoami             # Verify current user
```

## Command Reference

### Chats

```bash
teams chats                             # List recent chats
teams chats -n 50                       # Limit count
teams chats --unread                    # Unread only
teams chats --json                      # JSON output
```

### Unread

```bash
teams unread                            # Show chats with unread messages
teams unread -n 10                      # Limit count
teams unread --json                     # JSON output
```

### Read Chat Messages

```bash
teams chat 3                            # Read messages from chat #3
teams chat 3 -n 50                      # Limit message count
teams chat 3 --json                     # JSON output
```

### Read Single Message

```bash
teams read 5                            # Read full message detail by number
teams read 5 --raw                      # Show raw HTML content
teams read 5 --json                     # JSON output
```

### Send Messages

```bash
teams send "John Doe" "Hello!"                     # Send to person (resolves name/email)
teams send "john@company.com" "Hi there" -y         # Skip confirmation
teams send "Jane" "<b>Bold</b> message" --html      # Send as HTML

teams chat-send 3 "Hello team!"                     # Send to existing chat #3
teams chat-send 3 "Meeting at 3pm" -y               # Skip confirmation
```

### Send Files

```bash
teams send-file 3 ./report.pdf                      # Upload to OneDrive + send in chat
teams send-file 3 ./image.png -m "Here's the image" # With message
teams send-file 3 ./doc.xlsx -y                      # Skip confirmation
```

### Search

```bash
teams search "keyword"                              # Search across all chats
teams search "budget report" -n 10                   # Limit results
teams search "project" --chat 3                      # Search within chat #3
teams search "hello" --from "John"                   # Filter by sender
teams search "meeting" --after 2026-03-01            # After date
teams search "review" --before 2026-02-28            # Before date
teams search "quarterly" --json                      # JSON output
```

### Reactions

```bash
teams react 5 like                                   # Add reaction (shows confirmation)
teams react 5 heart -y                               # Skip confirmation
teams unreact 5 like                                 # Remove reaction
teams unreact 5 laugh -y                             # Skip confirmation
```

Available reactions: `like`, `heart`, `laugh`, `surprised`, `sad`, `angry`.

### Presence / Status

```bash
teams status                                         # Show current presence
teams set-status Available                           # Set status
teams set-status Busy -y                             # Skip confirmation
teams set-status DoNotDisturb --expiry +1h           # With expiry
teams set-status Away --expiry +30m -y               # Expiry + skip confirm
```

Available statuses: `Available`, `Busy`, `DoNotDisturb`, `BeRightBack`, `Away`, `Offline`.

### Scheduled Messages

```bash
teams schedule 3 "Reminder: standup" "+30m"           # Schedule in chat #3
teams schedule 3 "Good morning!" "tomorrow 09:00" -y  # Skip confirmation
teams schedule 3 "Done" "2026-03-15T10:00"            # Exact datetime

teams schedule-list                                    # List scheduled messages
teams schedule-cancel 1                                # Cancel by list number
teams schedule-run                                     # Send all due messages
```

**Time formats:** `+30m`, `+1h` (relative), `tomorrow 09:00` (day-relative), `2026-03-15T10:00` (absolute ISO).

**Note:** Teams has no native scheduled send. Messages are tracked locally and sent via `schedule-run`.

### Attachments

```bash
teams attachments 5                                  # List attachments on message #5
teams attachments 5 -d                               # Download all
teams attachments 5 -d --save-to ~/Downloads         # Custom download path
teams attachments 5 --json                           # JSON output
```

### Mark Read

```bash
teams mark-read 42 43 44                             # Mark messages as read
teams mark-read --chat 1 2 3 -y                      # Mark chats as read by chat number
teams mark-read 42 --unread                          # Mark as unread
```

### User Search

```bash
teams user-search "John"                             # Search by name
teams user-search "john@company.com"                 # Search by email
teams user-search "design team" --json               # JSON output
```

## JSON / Scripting

Rich output goes to stderr, stdout is pure JSON for clean piping:

```bash
teams chats --json | jq '.[0].topic'
teams chat 3 --json | jq '.[].sender'
teams search "keyword" --json | jq 'length'
teams chats --json | jq '.[] | select(.unread_count > 0)'
teams unread --json | jq '.[].display_title'
teams user-search "John" --json | jq '.[0].email'
```

## ID System

Chats get short display numbers (#1, #2, #3...) and messages get their own global sequence (#1, #2...). Numbers are assigned when listing and persist across commands. ID map is capped at 500 entries per section (LRU eviction).

```bash
teams chats               # Shows #1, #2, #3...
teams chat 3              # Read messages from chat #3
teams chat 3 -n 20        # Messages get #N numbers
teams read 15             # Read message #15 in detail
teams react 15 like       # React to message #15
teams reply 15 "Thanks"   # (if available)
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `TEAMS_IC3_TOKEN` | Override IC3 token (skip login) |
| `TEAMS_REGION` | Override region (default: auto-detected) |
| `TEAMS_PROXY` | HTTP proxy URL |
| `TEAMS_CLI_CACHE` | Cache directory (default: `~/.cache/teams-cli`) |
| `TEAMS_CLI_CONFIG` | Config directory (default: `~/.config/teams-cli`) |

## Common Patterns for AI Agents

```bash
# Quick check for new messages
teams unread

# Read latest messages from a specific chat
teams chat 3 -n 10

# Find a specific message across all chats
teams search "deployment failed" -n 5

# Find messages from a person
teams search "status update" --from "Makbule"

# Send a quick message to someone
teams send "john.doe@company.com" "Acknowledged, will review today." -y

# Reply in a group chat
teams chat-send 5 "Sounds good, let's proceed." -y

# Share a file in a chat
teams send-file 3 ./report.pdf -m "Weekly report attached" -y

# Check someone's presence before messaging
teams status

# Set DND for a meeting
teams set-status DoNotDisturb --expiry +1h -y

# Download attachments from a message
teams attachments 12 -d --save-to ~/Downloads

# Mark chats as read by chat number
teams mark-read --chat 1 2 3 -y

# Schedule a reminder
teams schedule 3 "Don't forget: review PR" "+2h" -y

# Search for a document shared in chats
teams search "Yapay Zeka Kullanım Politikası" --json
```

## Error Handling

- Token expired -> auto re-login attempted via cached browser SSO state.
- `Unknown chat #N` -> run `teams chats` first to populate the ID map.
- `Unknown message #N` -> run `teams chat N` first to see messages.
- `No results found` -> try broader search terms or check spelling.
- HTTP 401 -> auto re-login. If it fails: `teams login --force`.
- HTTP 429 -> automatic retry with backoff.

## Safety Notes

- Tokens are cached with `chmod 600` (owner-only read/write).
- Browser state saved for SSO — avoids repeated logins.
- `send`, `chat-send`, `react`, `unreact`, `schedule`, `send-file`, `set-status` ask for confirmation by default (use `-y` to skip).
- Anti-detection: random jitter between requests (0.3s reads, 2.0s writes), full browser headers.
- Do not share or log bearer tokens — they grant full Teams access.
- Prefer `teams login` over manually copying tokens.
