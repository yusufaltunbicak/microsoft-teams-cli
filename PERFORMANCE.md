# Read performance and design decisions

Measurements use the same macOS machine, real EMEA account and sequential CLI
subprocesses. Response bodies are reduced in memory to result counts; reports
contain timings, return codes and finite error labels, never message text, names,
tokens, cookies or search queries. Private reports live under
`~/.cache/teams-cli/benchmark-*.json` with mode `0600`.

## Reproduce the benchmark

```sh
# Installed baseline, before replacing the uv tool installation:
python3 scripts/benchmark_read.py --label baseline --runs 3

# Updated installed CLI, one metadata-cache miss per sample:
python3 scripts/benchmark_read.py --label optimized --runs 3 --cache-mode cold --skip-preflight

# Updated installed CLI, untimed per-command priming followed by three samples:
python3 scripts/benchmark_read.py --label optimized --runs 3 --cache-mode warm --skip-preflight

# Also compare local indexed search once an index has been populated:
python3 scripts/benchmark_read.py --label indexed --runs 3 --cache-mode warm --skip-preflight --include-local
```

The default queries are `Yusuf` for user lookup and `toplantı` for message search;
they can be overridden without storing them in the report. The allowlist contains
only version, chats, chat, unread, summary and search commands. Every live command
uses `--no-input`; subprocess stdin is closed. No send, mark-read, status change,
reaction, group creation or scheduling command is available to the script.

Legacy v0.1.2 can ignore `--no-input` during authentication. The script therefore
privately checks cached token expiry and refuses expired or near-expiry credentials
by default. `--skip-preflight` is intended for the updated CLI, whose command auth
never opens an interactive browser. Stop other Teams CLI processes before using
cold mode: it backs up only `metadata.json`, deletes it before each measured command
and restores the original after the run. It leaves tokens, browser state, ID maps,
scheduled messages and the message index untouched. Warm mode performs an untimed
invocation of each command before measuring it. A process timeout, authentication
failure or rate-limit response stops the run, rather than repeatedly retrying.

## Measured before and after

The untouched installed v0.1.2 and updated editable v0.1.3.dev0 each completed
three runs per command on 2026-10-09. These are medians from the baseline and
`optimized-final` reports, with no overlapping live benchmarks. Both installed
CLI versions used Python 3.12; the stdlib benchmark driver used Python 3.14 on
macOS arm64. The report labels distinguish driver and CLI runtimes.

| Command | Before v0.1.2 | After, cold metadata | After, warm metadata | Before / warm |
| --- | ---: | ---: | ---: | ---: |
| `--version` | 0.079 s | 0.088 s | 0.092 s | 0.9× |
| `chats -n 25` | 5.167 s | 2.333 s | 0.641 s | 8.1× |
| `chat 1 -n 25` | 0.264 s | 0.286 s | 0.334 s | 0.8× |
| `unread` | 0.624 s | 0.622 s | 0.482 s | 1.3× |
| `summary` | 4.496 s | 3.160 s | 1.351 s | 3.3× |
| `user-search` | 0.355 s | 0.347 s | 0.355 s | 1.0× |
| `search "toplantı"` | 13.455 s | 2.763 s | 0.966 s | 13.9× |

For transparency, the three-run min–max ranges in seconds were:

| Command | Before range | After cold range | After warm range |
| --- | ---: | ---: | ---: |
| `--version` | 0.077–0.084 | 0.084–0.090 | 0.087–0.104 |
| `chats -n 25` | 3.536–6.426 | 2.163–2.443 | 0.636–1.340 |
| `chat 1 -n 25` | 0.239–0.266 | 0.280–0.401 | 0.323–1.013 |
| `unread` | 0.465–0.634 | 0.465–0.664 | 0.453–0.642 |
| `summary` | 3.376–4.658 | 2.720–3.863 | 1.076–1.618 |
| `user-search` | 0.313–0.388 | 0.335–0.449 | 0.335–0.397 |
| `search "toplantı"` | 12.581–13.960 | 2.745–3.065 | 0.561–1.179 |

Result counts matched before/after in every sample: 25 chats, 25 chat messages,
four unread chats, five summary recents, ten user matches and eight live search
hits. `chat 1` uses the ID map produced by the preceding chats listing, just as in
ordinary CLI use. Cold means only persisted metadata was removed before each
sample; auth, ID maps and the optional message index remained available. Warm
means an untimed same-command prime followed by fresh CLI processes, not reuse
of a running client or a cached live response.

The optional local search measured **0.100 s median** (0.100–0.103 s, three runs).
It returned the first 25 hits from an index containing **3,759 unique messages**
across 50 selected chats. Forty-two chats covered the requested 60-day window;
eight reported `access_denied`, so overall coverage is incomplete. This import
used a 20-page per-chat budget and took 34.032 s across 70 read pages. The normal
sync default remains five pages per chat. Local word-prefix matching and bounded
indexed coverage differ from server search; its 25 hits must not be treated as
the same eight-result workload or an exhaustive account archive.

Network conditions and service response times change; a tenfold target is most
relevant to repeated search and chat listing. Startup and already-fast message
reads have much less room to improve. Local indexed search is a separate workload:
its completeness depends on what has been synchronized and when, so its timing
must be reported alongside coverage rather than presented as an exhaustive server
search replacement.

Repeated server search and chat listing provide the largest measured gains.
Already-fast user lookup did not change materially, and the final message-read
sample was slower. The result is not a universal tenfold speedup: fresh network
reads and startup still dominate the commands that had little enrichment work.

## What the Teams web code showed

The public Teams worker bundles were examined on 2026-10-09 without opening the
authenticated Teams application. The
[shared worker](https://teams.cloud.microsoft/v2/worker/precompiled-shared-worker-5aa2f0ea14f06b23.js),
its [paired bundle](https://teams.cloud.microsoft/v2/worker/precompiled-shared-worker-8a12fc3b96291fca.js)
and the
[async entry](https://teams.public.onecdn.static.microsoft/teams-modular-packages/hashed-assets/async-entry-19cfccc26b84312b.js)
are primary application code; their content hashes can change. This was static
inspection, not an authenticated browser network recording.

The worker code contains a single-message GET and an anchored history window at
`messages/epochTimeStamp/{messageId}` with direction, page size and metadata
parameters. Separate live read-only GET probes confirmed the single-message and
anchor routes. The CLI uses those routes for `read` and context instead of pulling
the newest 50 messages and hoping an old result is still among them.

Old-hit validation showed that small raw windows can be filled with membership
and call events. Context therefore expands only when filtered-out events leave
requested neighbors missing and directional pagination indicates more history.
It is bounded to three window reads and 200 raw events per request, plus a direct
message read if the anchor is missing. At a real history
boundary fewer messages are expected; additive `context_meta` describes actual
neighbor counts and whether the requested window was only partly filled.
Deleted-message tombstones are excluded from live reads and synchronized text;
reading a deleted anchor returns the existing not-found exit code rather than
exposing any body retained in the provider response.

Publication review also reproduced deleted terms remaining in FTS shadow blobs
despite core `PRAGMA secure_delete`. The index now enables FTS5 secure-delete on
SQLite 3.42+, cleans existing obsolete segments once, and uses an optimize pass
after observed text updates/deletions on older SQLite. Ordinary local reads do
not repeat that cleanup. The old-runtime fallback can slow sync; an index using
the newer FTS format requires SQLite 3.42+ to reopen, with a clear/resync instruction
if the runtime is downgraded. These behavior and format limits are documented in
[SQLite's FTS5 secure-delete specification](https://sqlite.org/fts5.html#the_secure_delete_configuration_option)
and verified with synthetic message text, including inspection of FTS blobs and
database bytes. The local-search timings above precede this additional protection;
the migration is a one-time cost. The final protected warm local search measured
**0.106 s median** (0.104–0.107 s, three runs), with the same 25 hits and coverage,
recorded separately as `benchmark-publication-local-warm.json`. The old-runtime
cleanup was also verified with a separately compiled SQLite 3.41.2 and synthetic
deleted/updated terms; temporary sources and test databases were removed.

The code also carries `SyncState` and backward pagination links, with a
100-message historical-page pattern and a 30-minute delta-window pattern. Those
constants are evidence of web-client strategies, not a stable public protocol or
a guarantee that a CLI replica can recover every missed change. The chosen initial
index uses bounded history pagination and explicit coverage, with an optional
foreground full refresh. Reproducing the entire web synchronization/notification
service was deferred to keep operation and recovery simple.

Search data already carries native sender identity/name and conversation topic.
The optimized parser requests/uses those fields before falling back to Graph/name
metadata; chat-list enrichment no longer has to rediscover every title for each
search. Server sender/date/chat constraints are assembled before the top-hit
limit. JSON shape and safety behavior remain compatible; context and local-search
coverage are additive metadata.

Profiling found another warm-path cost: retrying a user ID that Graph explicitly
reported missing on every command, and retrying a Graph presence route already
known to be forbidden even though UPS could provide live presence. Only confirmed
user-not-found responses receive a five-minute negative-cache marker; newly
observed native names replace it immediately. Only Graph presence 403 responses
with an available UPS token receive a one-hour capability marker. Presence remains a
live UPS read. Expired auth, throttling and network failures never create either
capability denial. Permission changes may therefore take up to one hour to be
probed again unless metadata is cleared; this is the tradeoff for eliminating a
known failing request. Fresh unchanged metadata also avoids repeated file rewrites.

## Alternatives considered

A tested `endTime` history-anchor variant returned HTTP 400, so it was abandoned
in favor of the web client's native `epochTimeStamp` route. A refresh request that
mixed `.default` with delegated scopes returned OAuth `70011`; refresh now uses
the saved delegated resource scopes and removes that incompatible `.default`
combination. The bounded auth-only refresh grant worked without executing the
authenticated Teams application, so a headless Teams UI renewal was unnecessary
and was not used. This also avoids introducing web application read-state side
effects while renewing credentials.

The existing CLI sends a Substrate search, then enriches senders through Graph,
then fetches chats and resolves their names again. A lasting metadata cache and
bulk lookup remove repeated work without retaining message bodies. Graph documents
[`$batch`](https://learn.microsoft.com/en-us/graph/json-batching) as supporting up to
20 requests per call. This reduces round trips, but individual subrequests still
count toward throttling limits; each failed item needs its own backoff. That makes
bounded batches plus caching preferable to unconstrained thread pools.

Graph's documented
[Teams message search](https://learn.microsoft.com/en-us/graph/search-concept-chat-messages)
supports sender, recipient and sent-date query operators. It returns a subset of
message properties; the `total` field counts the current page, not every matching
message. It is an alternative server search route, not proof that a page of hits
covers an entire history. Substrate remains the current server-search path until
permission and live-account parity can be established without changing behavior.

A documented cross-chat
[Graph message delta API](https://learn.microsoft.com/en-us/graph/api/chatmessage-delta?view=graph-rest-1.0)
offers incremental synchronization, but does not support delegated work-account
authentication. It requires application permissions and returns a bounded historical
window. Making that the default would introduce Azure registration/admin consent,
contradicting this CLI's existing model. A local index can instead be populated from
bounded IC3 reads; the CLI must describe its coverage and retain a server-search
option. Deletion/edit convergence needs explicit design: a one-time import is not
a continuously accurate mirror.

MSAL's documented
[silent acquisition sequence](https://learn.microsoft.com/en-us/entra/msal/javascript/browser/acquire-token)
checks its token cache, refreshes per resource and attempts interactive auth only
after silent acquisition fails. The CLI should follow that ordering while making
interactive login an explicit command. MSAL documents browser access tokens as
typically lasting about an hour and SPA refresh tokens as a non-sliding window
usually limited to 24 hours. Rotation does not restart that window. A valid SSO
session may permit silent renewal after it, but MFA or conditional-access policy
can still require the user. See
[token lifetimes](https://learn.microsoft.com/en-us/entra/msal/javascript/browser/token-lifetimes).

Repeated polling and removing delays do not offer the same benefit as removing
redundant requests. Microsoft's
[throttling guidance](https://learn.microsoft.com/en-us/graph/throttling)
recommends caching/change tracking where available and respecting `Retry-After`.
The user's existing read/write jitter defaults remain a meaningful baseline;
reducing them should be an explicit configuration decision supported by measurement.
Success in a short benchmark cannot establish future account-detection or rate-limit
safety for undocumented APIs.

## Follow-up ideas by practical value

1. Incremental per-chat sync with periodic reconciliation: skip unchanged chats
   when a reliable server revision is available, then revisit old pages on a
   bounded schedule so edits/deletions converge. This reduces watch-loop traffic
   without pretending that a recent-message import is a full mirror.
2. Search pagination and explicit server coverage: sender/date/chat filters now
   reach the server before its top-hit limit; continue validating ranking/filter
   parity and page beyond first hits where necessary. Keep local indexed coverage
   visible separately from server search.
3. Silent SSO authorization after the saved SPA refresh token expires: evaluate a
   bounded auth-only cookie flow, with no Teams application execution or implicit
   UI. Any conditional-access/MFA requirement must still stop and request explicit
   login. This is distinct from refreshing an unexpired saved grant.
4. Permission-tested Graph search fallback: compare counts, filters, message IDs
   and detail availability before replacing the established Substrate route.
5. Read-only request metrics: aggregate endpoint families, round trips, throttling
   counts and elapsed time without URLs containing IDs, bodies or credentials.

An always-running service and live notification stream add lifecycle, protocol
and recovery complexity. The initial design uses explicit bounded sync and an
optional foreground repeat loop; the installed CLI remains sufficient to operate
it. Lower jitter is configurable but has not been selected as the default speedup.
