# SCHEMA — the integration contract

The SQLite database is the firewall between the stable parsing core and everything
that reads it (CLI, dashboard, external tools). This document is the contract: the
tables, the read-API, and the rules for evolving them. If you only read one section,
read **Evolving the schema** at the bottom.

The profiler's DB lives at `~/.whytokens/whytokens.db` (separate from
claude-usage's `usage.db` so both can run side by side — see `config.py`). It runs
in **WAL mode**, so readers never block a concurrent `scan`.

## Tables

### `turns` — one row per API response (source of truth)

Tokens and per-model attribution live here. Cost is computed per turn (each turn
knows its own model) then summed.

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | autoincrement |
| `session_id` | TEXT | groups turns into a session |
| `timestamp` | TEXT | ISO-8601 UTC, e.g. `2026-05-01T10:00:05Z` |
| `model` | TEXT | exact model id of *this* turn |
| `input_tokens` | INTEGER | raw prompt tokens |
| `output_tokens` | INTEGER | generated tokens |
| `cache_read_tokens` | INTEGER | from `usage.cache_read_input_tokens` |
| `cache_creation_tokens` | INTEGER | from `usage.cache_creation_input_tokens` |
| `cache_creation_5m_tokens` | INTEGER | ephemeral 5-minute cache write |
| `cache_creation_1h_tokens` | INTEGER | ephemeral 1-hour cache write — **bills at 2x input, not the 5m 1.25x**; `pricing.calc_cost` takes it as `cache_creation_1h` and prices that portion at `cache_write_1h` |
| `tool_name` | TEXT | first tool_use in the turn (or NULL) |
| `tool_calls` | TEXT | JSON array of `{name, input}` for *all* tool_use blocks; `input` is allowlisted metadata (see docs/DATA_POLICY.md), raw only with `WHYTOKENS_STORE_RAW_INPUTS=1` |
| `cwd` | TEXT | working directory of the turn |
| `message_id` | TEXT | `message.id`; dedup key (see invariants). Advisor turns use the synthetic `advisor:<message_id>:<idx>` |
| `duration_ms` | INTEGER | turn latency if present |
| `stop_reason` | TEXT | |
| `service_tier` | TEXT | from `usage.service_tier` |
| `inference_geo` | TEXT | from `usage.inference_geo` |
| `is_sidechain` | INTEGER | 1 if a subagent turn |
| `is_compact_summary` | INTEGER | 1 on the turn that answers a compaction summary (Claude Code flags the USER record; the scanner carries it to the next assistant message) |
| `agent` | TEXT | source coding agent: `claude` (default), `codex`, or `coordinator` (partial; see below) |
| `reasoning_tokens` | INTEGER | informational; reasoning is already inside `output_tokens` and never billed separately (Codex) |
| `agent_id` | TEXT | dispatched-subagent id (matches `agents.agent_id`); NULL on main-chain turns |
| `effort` | TEXT | reasoning effort of the turn (`high`/`medium`/`low`/`xhigh`); `''` when the source didn't report one — Claude Code only writes it from 2.1.215 |
| `skill` | TEXT | skill active when the turn was produced (`next-task`, `local-delegate`, …), from the record's top-level `attributionSkill`; `''` outside any skill |
| `mcp_server` | TEXT | MCP server whose tool result this turn consumed (`github`, `Claude Browser`, or a claude.ai connector UUID), from `attributionMcpServer`; `''` otherwise. Orthogonal to the agent/skill/main split — do not sum across both |
| `mcp_tool` | TEXT | that MCP tool (`workitem`, `get_equity_quotes`, …), from `attributionMcpTool`; `''` otherwise |
| `plugin` | TEXT | plugin the turn is attributed to, from `attributionPlugin`; `''` otherwise |
| `estimated` | INTEGER | 1 = some token count on the row is an estimate (see below); default 0 |
| `prompt_seq` | INTEGER | genuine user prompts seen so far in the session's main transcript (`scanner.is_user_prompt`; slash commands count, tool_result / isMeta / compaction summaries / task-notification, peer and desktop-app event records do not); 0 before the first prompt. **NULL** on subagent turns and on rows whose transcript was pruned before per-call ingestion (unknown, never 0). Advisor rows inherit the parent's. No prompt text is stored |

**Estimated rows** (`scanner._apply_estimates`, constants at the top of `scanner.py`). Subagent transcripts under-record what Claude Code bills; each gap is estimated, whole file re-read on every (re)parse:

1. *Truncated output*: a message whose final record has `stop_reason: null` (subagents only; mid-stream write) keeps a stream-start `output_tokens`. Set to `max(recorded, round(payload_chars / 2.0))` over text/thinking/tool_use chars of all the message's lines.
2. *Missing advisor*: a message with an advisor `server_tool_use` block but no `advisor_message` iteration on any of its records gets an extra `advisor:<id>:est` turn (6158 output tokens, input = parent context); it is deleted if a real iteration for that id is later written.
3. *AgentSummary*: Claude Code forks a running subagent's context every 30 s (if its transcript changed), billed but never written. One aggregated `agent_summary:<session_id>:<file stem>` turn per subagent file (`is_sidechain=1`, `tool_name` NULL, counted as a turn like advisor rows): a fire counts at tick t0+30k up to the agent's last activity when any record landed in that window; input 300 / output 60 per fire, `cache_read` = summed context of the latest assistant message at each tick.

Upsert precedence: a real row always replaces an estimated one (clearing `estimated`); an estimate never replaces a real row except one still mid-stream (empty `stop_reason`); estimate over estimate updates when values move.

Indexes: `idx_turns_session(session_id)`, `idx_turns_timestamp(timestamp)`, and a
**partial unique index** `idx_turns_message_id(message_id) WHERE message_id IS NOT
NULL AND message_id != ''` — this enforces the dedup invariant in the DB.

### `sessions` — denormalized per-session rollup

| column | type | notes |
|---|---|---|
| `session_id` | TEXT PK | |
| `project_name` | TEXT | last two path components of `cwd` |
| `first_timestamp` / `last_timestamp` | TEXT | session span |
| `git_branch` | TEXT | |
| `total_input_tokens` / `total_output_tokens` | INTEGER | recomputed from `turns` |
| `total_cache_read` / `total_cache_creation` | INTEGER | recomputed from `turns` |
| `model` | TEXT | the session's **primary** model (Claude: priority opus > sonnet > haiku; otherwise most-common per-turn model) |
| `turn_count` | INTEGER | recomputed from `turns` |
| `agent` | TEXT | source coding agent: `claude` (default), `codex`, or `coordinator` (partial; see below) |
| `topic` | TEXT | session title, or NULL |
| `topic_source` | TEXT | where `topic` came from. Higher rank wins: `custom` (user rename) > `thread` (Claude Projects thread title) > `ai` (ai-title record) > `prompt` (the Projects thread's trigger message, used when the title is no longer cached) |
| `shape` | TEXT | work shape from the tool-call mix (see Work shape): `edit`, `read_search`, `shell`, `web`, `connector`, `subagent`, `mixed` or `chat`; NULL until derived |
| `shape_detail` | TEXT | the top connector server for a `connector` session, else NULL |
| `shape_version` | INTEGER | `shape.SHAPE_VERSION` of the rule that derived `shape`; a different value re-derives it |
| `shape_turns` | INTEGER | signature (turn count + stored tool-call bytes) when the shape was derived; a session that gained turns or had tool calls backfilled is re-derived |

Index: `idx_sessions_first(first_timestamp)`.

Projects threads have no title in their transcript. The cloud coordinator names a
thread when it spawns it, and the only local copy is the desktop app's claude.ai
IndexedDB cache (`%APPDATA%\Claude\IndexedDB\https_claude.ai_0.indexeddb.blob`).
`project_threads.py` decodes that cache (snappy + V8, stdlib only) on every scan and
joins on three keys: the spawn note, the trigger `cmsg_` id and the `bridge-cse_`
worktree. The cache is a rolling window, so a title is kept once it has been
captured.

The coordinators' own spend is in the same cache and is ingested as the **partial**
`coordinator` source (`sources/coordinator.py`): one session per coordinator
(`session_id` = its `cse_` id, `project_name` "Projects coordinator (partial: cached
window)"). Tokens come from each query's closing `result` event, because the cached
assistant messages are mid-stream snapshots whose output counts are far below
final. The main model takes the result's `usage`, which includes the 1h/5m
cache-write split. Side models (e.g. Haiku) take the delta of the cumulative
`modelUsage` from the previous result. `message_id` is the result's event uuid,
suffixed `:<model>` for side models. The main model is the one whose delta equals
the result's `usage`. When no base proves it (the first result in a window), the
largest delta is a guess. That row is `estimated = 1` and has no side-model rows,
and a later blob that confirms the model replaces it. The cache holds only about 200 events per
coordinator, so the totals are a lower bound: whatever was cached while scans
ran.

### `tool_results` — one row per tool call (Claude only)

`(call_id PK, session_id, message_id, tool_name, result_bytes)`. `call_id` is the
`tool_use` id; `message_id` joins to `turns.message_id` (collected from every
streamed record of a message, so parallel calls are all there). `result_bytes` is
the UTF-8 length of the `tool_result` content as the transcript holds it: a string
as-is, in a block list each text block's text plus any other block's compact JSON.
It is the observed figure; convert to tokens only where a detector needs it (`waste.py`
`RESULT_BYTES_PER_TOKEN`, one estimate: `resident_output` claims at most this many tokens of the
next cache write, and a NULL or missing size claims nothing and is counted in the finding's detail).

Either side can land first (a result often arrives in the next scan), so writes are
a COALESCE upsert, after which each call takes `session_id` from its stored turn, so a
branch copy re-using the ids never moves a call to the copy. `result_bytes` is **NULL until the result is seen** — a real `0`
means an empty result. Sessions whose transcripts were pruned before per-call ingestion have no
rows at all and can never be backfilled; Codex sizes are **not measurable** (no rows,
`sessions.agent = 'codex'`). `scanner.session_result_bytes(conn)` /
`SESSION_RESULT_BYTES_SQL` gives each session's total, or NULL when any of its calls
has no result yet, any of its turns has more `tool_calls` than measured results
(pruned before per-call ingestion), or it is not a Claude session.
Size-based $ must skip NULL sessions, never read them as 0.

ToolSearch ingestion adds two additive columns for ToolSearch results: `loaded_tools` (JSON list of the tool names
whose schemas the result loaded, from its `tool_reference` content entries; never the query text) and
`total_deferred_tools` (INTEGER, from `toolUseResult`, when the record holds one result). The result
appears twice in the transcript (with and without `toolUseResult`); rows are keyed by `call_id`, so the
names are deduped by tool_use id. NULL = not a ToolSearch result or its transcript is gone (unknown,
never an empty list). `ClaudeSource.backfill_deferred_loads` fills old rows from transcripts still on
disk, once. Consumed by the `deferred_tool_load` waste cause (`waste.py`; Claude only, per `agent_id` lane).

`is_error` (INTEGER, additive): the `tool_result` block's `is_error` flag, 1 or 0 (an absent flag is 0).
NULL = the result was not seen by a scanner that records it. Bash calls in `turns.tool_calls` also carry
`command_hash` (docs/DATA_POLICY.md) from the same version. Rows scanned earlier are filled once, on the next
scan, by `ClaudeSource.backfill_retry_inputs` (marker `retry_inputs_backfill_done_claude`): it re-reads
transcripts still on disk and UPDATEs only NULL `is_error` / absent `command_hash`, matched by call id; no
row is inserted, deleted or re-priced, and an interrupted run repeats. Sessions whose transcript is gone stay
NULL: **unmeasured**, never read as success. Consumed by the `retry_loop` waste cause;
`waste.measurement()` reports it as `partial` for Claude with the measured / unmeasured session counts.

#### Waste causes from per-turn context (no new columns)

- `large_context` (observation): main-lane turns whose context (`input + cache_read + cache_creation`)
  passes `waste.LARGE_CONTEXT_TOKENS` (250K), priced as the cache-read cost of the tokens above it; a
  compaction starts a new crossing.
- `fixed_prefix` (observation): the main lane's opening prefix (`cache_read + cache_creation` of its
  first cached request, so a prefix warm from another session still counts) times each later main-lane
  turn's `min(prefix, cache_read)` at the cache-read rate, plus the opening request. Gated at
  `waste.FIXED_PREFIX_TOKENS` (50K). Subagent lanes stay in `subagent_preamble`.

Observation findings carry `in_total: false`: they overlap the attributed causes, so `total_usd`, the
overhead headline, the Sessions Overhead column and the Overview "where to act" strip skip them.
`uncached_input`, also an observation, is still summed in (`in_total: true`), as before.

### `processed_files` — incremental-scan bookkeeping

`(path PK, mtime REAL, lines INTEGER)`. Lets a rescan skip unchanged files and
re-parse only the appended tail of a grown file.

### `measures` — engine `session.measure` readings (cost-band sidecar)

One row per Claude Code `session.measure` event, the only record of **rate-limit
windows** (transcripts never carry them). Written by the `measures` extension, created by
its own `init_measures` (not `scanner.init_db`), from the sidecar the **cost-band**
mod (a Claude Code mod outside this repo) writes: one file per session,
`~/.claude/usage-measure/<sessionId>.jsonl`. One file per
session because `$.fs` has no append: the mod rewrites the whole file, and a shared
file would lose lines across concurrent sessions.

| Column | Type | Meaning |
|---|---|---|
| `session_id` | TEXT | Claude session id (joins `sessions`) — PK part |
| `ts` | TEXT | ISO-8601 UTC when the mod saw the event — PK part |
| `cwd` | TEXT | session working directory |
| `cost_usd` | REAL | engine's `/cost` ledger, **session total incl. subagents**; NULL when absent, never 0 |
| `context_tokens` | INTEGER | live main-thread context fill; NULL when unknown |
| `five_hour_pct` / `seven_day_pct` | REAL | `percentUsed` of that window, NULL when not reported |
| `rate_limits` | TEXT | the raw `rateLimits` array as JSON (all kinds, `resetsAt`) |

A changed file (by mtime, `processed_files.source = 'measure'`) is re-parsed whole;
`INSERT OR IGNORE` on `(session_id, ts)` makes that idempotent, and a torn line is
skipped until the next rewrite; an unreadable file (locked mid-write) is not
recorded, so the next scan retries it. Runs from the extension's `on_scan` hook, so only a scan with the extension loaded reads it;
a caller passing its own `sources` opts in with `scan_all(extensions=[...])`.

**Window percentages are account-wide**, not per session: concurrent sessions report
the same window, so attribute burn to a session only when it ran alone. Quota burn
over time: `SELECT ts, five_hour_pct, seven_day_pct FROM measures ORDER BY ts`.

The engine's own `/cost` can differ from `pricing.py`: Claude Code 2.1.294 bills Sonnet 5.5
cache reads at $0.20 (`tier_2_10`) vs the published $0.10. `pricing.py` uses the published rate.

## Scanner invariants

1. **Dedup by `message.id`** — Claude Code writes many JSONL records per API
   response; only the last carries final usage tallies. Keep last-per-id; never sum
   across records of one id.
2. **Session totals are recomputed from `turns`** at the end of a scan (turn inserts
   are an upsert on `message_id` that only overwrites when the new record's output is larger — a scan landing mid-stream is corrected by the next one; session rollups add additively — the reconcile prevents
   drift on rescans).
3. **Primary-model priority opus > sonnet > haiku** at the session level only;
   per-turn `model` is always honored.

## Sources — multi-agent ingestion

Ingestion is pluggable: a **Source** (`sources/`) owns where its files live and how
its records parse; the engine (`sources.scan_all`) owns the shared machinery
(`processed_files` walk, dedup, reconcile) and writes every source into the **same**
`turns`/`sessions` tables, tagged by `agent`. Today: `claude` (a thin wrapper over the
vendored `scanner.py`), `codex` (OpenAI Codex CLI, `~/.codex/sessions`) and
`coordinator` (Claude Projects coordinators, partial; see the `sessions` notes).

Codex is normalised into the column semantics above so one cost formula serves both
agents:

- Codex `input_tokens` *includes* cached tokens, but the `input_tokens` column means
  **uncached** input (Claude's convention). So we store `input_tokens = codex.input −
  codex.cached_input` and `cache_read_tokens = codex.cached_input`;
  `cache_creation_tokens` is 0 (Codex has no cache-creation concept).
- Codex `token_count` records carry no message id, and branched/resumed sessions copy
  parent history verbatim into the child file. Codex turns therefore get a
  **synthetic** `message_id` (`codex:` + content hash, excluding session_id) routed
  through the same partial-unique index — so branch copies collapse and rescans are
  idempotent.
- Codex detail enrichment uses per-file `(mtime_ns, size)` receipts in
  `schema_meta.detail_backfill_files_codex`. Missing files and incomplete or
  changing reads remain retryable; existing metadata and token IDs/totals are
  preserved. Tool call IDs suppress replayed calls; namespaced calls retain
  their qualified name. Tool aggregates attribute the whole turn to its first
  tool, not marginal cost, while session details list every call.
- Codex usage is plan-included for ChatGPT-plan users; any dollar figure is an
  **API-equivalent estimate** (see `pricing.py`), not a bill.

## Satellite tables — external writers, untouched by rescan

The core tables (`turns`, `sessions`, `processed_files`) are owned by ingestion and
updated incrementally by `POST /api/rescan` (same `sources.scan_all` engine as
`python cli.py scan` — no DB unlink, no rebuild). Anything else in the DB is a
**satellite**: a table some *other* tool writes, joined to the core by `session_id`.
whytokens is agnostic about what a satellite *is* — it never writes or migrates
one, and rescan never deletes the DB file, so satellites survive by construction. A
satellite never needs an additive migration here because whytokens doesn't own its
schema — a read path discovers columns at query time via `PRAGMA table_info`.

### `experiments` — example satellite (an external eval harness)

Written by an external eval harness (not by the scanner), one row per
eval run, joined to `sessions`/`turns` by `session_id`. whytokens reads it
**read-only** (via an extension); it never writes or migrates it. Columns
are discovered at query time via `PRAGMA table_info` (the harness owns the shape), but
the read path uses: `task`, `arm`, `backend`, `model` (filter dimensions),
`dry_run` (excluded unless `include_dry`), `ts` (ordering), and `session_id` (join).
A missing table is **"no experiments yet"**, not an error — the tab renders empty.

## Read-API (`api.py`)

All endpoints read the contract above; only `POST /api/rescan` writes (an incremental
`sources.scan_all`, same as `scan` — satellites are untouched since nothing is
deleted). Every query parameter is bound as a SQL `?` placeholder.

| endpoint | params | returns |
|---|---|---|
| `GET /api/data` | `tz` | full-history snapshot the dashboard filters client-side |
| `GET /api/session` | `session_id` | one session + its turns |
| `GET /api/session-breakdown` | `session_id` | per-actor token split of one session: rows of `(kind, label, model)` + token sums |
| `GET /api/sessions` | `project`, `model`, `start`, `end`, `limit`, `offset`, `sort`, `tz` | **paginated**, SQL-side-filtered session list + `total` |
| `GET /api/daily` | `start`, `end`, `model`, `project`, `tz` | **SQL-side** daily `(day, model)` aggregation with **server-computed cost** |
| `GET /api/experiments` (extension) | `task`, `arm`, `backend`, `model`, `include_dry` | eval-harness `experiments` satellite (read-only): rows + aggregated `groups`; empty when the table is absent |
| `GET /api/live` | `session_id` \| `cwd` | one session read **straight from its transcript** — no DB, no scan — with server-computed `cost` |
| `GET /api/pricing` | — | the single-source price table (`pricing.PRICING`) |

Notes on the parameterized endpoints:

- **`/api/data`** — `tz` picks the calendar **every** day-shaped field is bucketed
  into: `daily_by_model.day`, `hourly_by_model.day`, `tool_by_model.day`,
  `effort_by_model.day`, `subagent_dispatches.day` and `sessions_all.last_date`.
  Default (and anything but `utc`) is the **server machine's local** calendar
  (`date(timestamp, 'localtime')`, DST-aware); `tz=utc` is the plain UTC slice.
  They move together on purpose — the dashboard derives its range bounds in the
  same calendar, and a mixed pair puts a session on the far side of a range edge
  from its own turns. `hourly_by_model.hour` stays UTC either way; the client
  shifts it (see `aggregateHourly`).
- **`/api/sessions`** — `start`/`end` are inclusive `YYYY-MM-DD`, matched against
  `last_timestamp` in the **`tz` calendar**, which is also the calendar
  `last_date` is reported in — the two agree by construction. Default is local,
  matching `/api/data`; `tz=utc` filters and reports on the UTC slice. Filtering and
  reporting in one calendar keeps a session within an hour of local midnight from
  carrying a `last_date` outside the range that returned it. `limit` is clamped to
  `[1, 500]` (default 50); `offset` ≥ 0.
  `sort` is one of an **allowlist** — `recent` (default), `oldest`, `tokens`,
  `turns` — never raw SQL. `total` is the full filtered count (pre-pagination) so
  callers can build pagers.
- **`/api/daily`** — aggregation happens in SQLite (`GROUP BY day, model`), not in
  the client. Cost is added per bucket via `pricing.calc_cost`, so the single
  pricing source is honored server-side. `day` is UTC (`substr(timestamp, 1, 10)`)
  by default — it's a documented external contract, kept stable — unless
  `tz=local` is passed, which buckets by the server machine's local calendar day
  (`date(timestamp, 'localtime')`, DST-aware) to match the dashboard's own range
  bounds. `start`/`end` stay UTC-compared either way.
  Note the **`tz` default differs by endpoint**: `/api/daily` defaults to UTC (a
  frozen external contract), `/api/data` and `/api/sessions` default to local
  (the dashboard's pre-toggle behaviour). Both are deliberate; pass `tz`
  explicitly rather than relying on either default.

- **`/api/session-breakdown`** — answers "who spent this session": the main thread,
  each dispatched subagent **type**, and each **skill**. Buckets are disjoint and
  resolved in priority order **agent > skill > main** (a turn produced inside a
  subagent belongs to that agent even when a skill was active in the parent), so the
  rows re-sum to the session's own totals — a property the tests assert. Rows are
  per `(bucket, model)` because one agent type can span models and each slice must
  be priced with its own. **Tokens only, no cost**: the dashboard prices them
  client-side so the editable pricing table keeps working. An agent whose type was
  never recorded falls into `main thread` rather than vanishing.

  This is *not* the same decomposition as Claude Code's own `/usage` panel. That
  panel's per-model split does reproduce here (it is cost share), but its per-agent
  percentages do not reconcile under any weighting tried — cost, cost-excluding-cache,
  raw tokens, or output-only. Read these rows as cost share at *this* pricing table,
  not as Anthropic's limit accounting.

- **`/api/live`** — the one endpoint that does **not** read the DB. It answers "what
  is the session I am sitting in costing right now", which the DB cannot: that
  session has not been scanned yet. Resolution is by `session_id` (validated as a
  UUID before it reaches a filesystem glob) or by `cwd` (mangled to a project-dir
  slug the way Claude Code does, then the newest transcript in it). The parse set is
  the main `<id>.jsonl` **plus `<id>/subagents/*.jsonl`** — subagent records carry
  the *parent* `sessionId`, and `reconcile_session_totals` rolls them into the same
  session row, so omitting them undercounts every agent-heavy session. Turns are
  parsed with the ordinary `scanner.parse_jsonl_file` (same dedup, same advisor
  emit) and priced **per turn** with `pricing.calc_cost`, so the total equals what
  the dashboard row shows once scanned — asserted against real sessions and guarded
  by `tests/test_live.py` + `tests/falsify_live.py`. Keys mirror `/api/session`'s
  `session` object, plus `cost`, `cost_per_turn` and `context_tokens` (the newest
  **main-chain** turn's input side — a subagent's context is its own, not this
  session's replay cost). Also available offline as `python cli.py live [--json]`.
  Consumers: the `session-cost` skill and the `ctxband-hook.mjs` spend band, which
  fetch the dollar figure rather than reimplementing the price table.

`/api/live` has **no client mirror** in `static/modules/api.js` — it is a
server/tooling endpoint (skill + hook), and the dashboard has the scanned row.
`static/modules/api.js` mirrors the rest as `fetchData / fetchSession /
fetchSessionBreakdown / fetchSessions / fetchDaily / fetchPricing / postRescan`.

## Export document (`whytokens export --json`)

One JSON document on stdout, also `api.get_export()`. Top-level keys:
`schema_version` (int, currently `1`; bumped on any breaking change, pinned by
`tests/test_export.py`), `totals` (`sessions`, `turns`, `input`, `output`, `cache_read`,
`cache_creation`, `waste_usd`), `sessions` (every session, same objects as `/api/sessions`)
and `waste` (the `/api/waste` payload). Additive keys do not bump the version.

## Schema-drift armor (`schema.py` + `scan --doctor`)

The transcript envelope is undocumented and drifts between Claude Code versions, so
the real risk is silently *missing* a new field. `schema.py` owns the **known
surface** — allowlisted keys at each record level, the known record `type`s, and the
Claude Code `version`s seen when the allowlist was last refreshed. It is the single
source for:

- the **schema-surface canary** (`tests/test_schema.py`) over the golden corpus
  (`tests/fixtures/golden.jsonl`) — a refreshed fixture with a new key fails the
  build; and
- **`python cli.py scan --doctor`** (alias `doctor`) — reads the *real* logs and
  reports unknown keys / types, unseen versions, and **shape drift** (a nested field
  such as `message` / `usage` whose *type* changed, which the scanner assumes is a
  dict), exiting non-zero on drift. Writes nothing.

When `--doctor` flags a new key, decide whether the scanner should capture it (add a
column via the additive migration pattern) and then refresh the allowlists +
`KNOWN_VERSIONS` in `schema.py`.

**Codex** has a parallel track: `codex_doctor_scan` over `~/.codex/sessions` with its
own allowlists (`KNOWN_CODEX_*` — envelope keys/types, `payload.type`s, and the
`token_count.info` usage keys, the money canary) and golden corpus
(`tests/fixtures/codex/golden-codex.jsonl`). Structural drift gates the result;
Codex CLI versions churn fast (frequent alphas) so they are reported informationally
and do **not** gate. `cli.py doctor` runs both tracks and exits non-zero if either
drifts.

## Budget file

Not a table: the monthly budget is a small JSON file `budget.json` next to the DB
(`config.DB_PATH.with_name("budget.json")`, so `WHYTOKENS_DB` / `WHYTOKENS_HOME` move it
too). `{"monthly_usd": 500.0}`, written atomically (tmp + replace); the value must be a
finite number > 0. An absent file means no budget: the API reports `budget: null`, never 0.
Dropping and rescanning the DB leaves it alone.

`GET /api/budget` (`budget.month_status`) covers the current calendar month, whatever range
the dashboard is filtered to, priced per day exactly like `/api/daily` (API list price):
`{month, spent, budget, projected, gap, needed_daily, daily_rate, method, days_elapsed,
days_remaining}`. Data runs through today inclusive; `days_remaining = days in month - day of
month`; `daily_rate` is the average of the last 7 calendar days (sum / 7) once the month is
7 days old (`method: "trailing_7d"`), else month-to-date spend per elapsed day
(`"month_to_date"`); `projected = spent + daily_rate * days_remaining`; `gap = projected -
budget` (positive = over; null without a budget); `needed_daily = (budget - spent) /
days_remaining` (null without a budget or on the last day). `POST /api/budget
{"monthly_usd": number|null}` sets or clears it (token-guarded like `/api/fixes`; invalid
values are 400). CLI: `whytokens budget [AMOUNT|--clear]`.

## Work shape

A session's shape is derived from `turns.tool_calls` alone (`shape.py`; no model, no transcript
read, so a rule change re-derives from the DB: `shape.sync_shapes` runs at the end of every scan
and fills any session whose `shape` is NULL, whose `shape_version` differs, or whose
`turn_count` moved). Tool names fall in categories: edit (Edit, Write, NotebookEdit, MultiEdit,
apply_patch), read/search (Read, Grep, Glob), shell (Bash, PowerShell, shell, exec_command,
local_shell), web (WebFetch, WebSearch), connector (`mcp__<server>__<tool>`, the top server is
kept in `shape_detail`), subagent (Agent, Task). Other tools count in the total but in no
category. The shape is the category holding at least `DOMINANT_SHARE` (0.40) of the tool calls
(ties go to the earlier category in that order), else `mixed`; a session with fewer than
`MIN_TOOL_CALLS` (5) calls is `chat`. Image input is not recorded by ingestion, so it is not a
category. The human labels live in `shape.LABELS` and reach the dashboard through the API.

Peers: sessions of the same project and shape, itself included. The baseline is the median cost
per user prompt (`MAX(prompt_seq)` of the main thread; sessions without prompts or spend have no
multiple and are not peers). Under `MIN_PEERS` (5) the project's median across shapes is used and
the payload says `basis: "project"`; with neither there is no multiple. A multiple of
`OUTLIER_X` (2.0) or more is an outlier: counted and listed, never added to attributed overhead
or any total.

`GET /api/shapes`: `{labels, constants, tags, rows, sessions, outliers}` where `rows` is the
by-shape table (sessions, usd, share of cost, overhead_usd counting only findings in the total,
overhead_share of that shape's cost) and `sessions` maps each session id to
`{shape, detail, x, basis, n, median}`. Cached per DB state like the waste payload.

On a recorded change, `GET /api/changes` rows carry `mix`: the session-share per shape before and
after, a `warning` when any shape's share moved by `MIX_SHIFT` (0.20) or more on a decided change,
and a `same_shape` before/after for each shape with at least 10 sessions on both sides. It is
informational: `fixes.verify` (the 10-session gate, the materiality thresholds, the verdict) does
not read it.

Project tags: `budget.json` also holds `"project_tags": {project: text}` (at most 40 plain
characters each; read-modify-write under a lock, so setting a budget or a tag never drops the
other). `POST /api/project-tag {project, tag|null}` (token-guarded; the project must exist) and
`whytokens project tag PROJECT [TAG|--clear]`.

## Model what-if

`GET /api/whatif?to=<model id|next_tier>&start&end&model&project&actor&session_id`
(`whatif.reprice`) prices the same turns on another model through `pricing.reprice_turn`
(`calc_cost` plus the fast-mode / US-only / long-prompt multiplier, so repricing a turn onto
its own model equals its recorded cost). `to=next_tier` reprices each model onto
`pricing.next_tier_down` (`pricing.TIER_ORDER`); a model with no cheaper tier has `to: null`
and is left out of the totals. `actor` is `main`, `subagent` or an agent type, resolved as
in `/api/session-breakdown`. Returns `{to, actual_usd, repriced_usd, delta_usd, by_model[],
wording}`; an unknown `to` is a 400. Observation only: these figures never enter attributed
overhead, the waste totals, Est. Cost or any other total.

## Evolving the schema

- **Additive only.** New columns go through `COLUMN_MIGRATIONS` in `scanner.py`
  (applied by `init_db` as `ALTER TABLE … ADD COLUMN`), with a default unless NULL
  must mean *unknown*. New tables and indexes are added to `CORE_TABLES` / `CORE_INDEXES` too. Never break the
  ability to **drop the DB and rescan**.
- **Backup first.** When `init_db` would create a table or index or add a column on an
  existing DB, it first copies the DB to `<db>.bak-YYYYMMDD` via the sqlite backup
  API (a same-day backup is kept, never overwritten; a failed backup stops the
  migration). The DB is the only copy of sessions whose transcripts were pruned.
  `scripts/falsify_tool_results.py` breaks this guard and the NULL-not-zero rule
  and checks `tests/test_tool_results.py` goes red.
- **The schema is the contract.** Ingestion writes it; the CLI, dashboard, and any
  external tool only read it. Keep that split.
- **Satellite tables** join the core by `session_id` (e.g. `experiments`). They are
  written by *external* tools, preserved across rescan, and read-only to whytokens —
  see **Satellite tables** above.


## Advisor turns

An `advisor` tool call is a **separate inference on a different model**, carried
inside the parent assistant message as a `usage.iterations[]` entry of type
`advisor_message`. The model is named by the record's top-level `advisorModel`
(observed: `claude-fable-5`).

The envelope's top-level `input_tokens` / `output_tokens` count **only** the
`message` iterations — advisor tokens are *not* in them. (`cache_read` /
`cache_creation` are unaffected: advisor iterations carry none.) Reading only the
top level therefore silently drops the entire advisor spend.

The scanner emits each `advisor_message` iteration as its **own turn row**, with:

- `model` = the record's `advisorModel` (falls back to the parent's model),
- `message_id` = `advisor:<parent message_id>:<iteration index>` — synthetic and
  stable, so re-scans and branch copies stay idempotent (same rationale as the
  Codex content-hash id),
- `tool_name` = `advisor`,
- `effort`, `is_sidechain` and `skill` **inherited from the parent turn** — advisor
  cost is induced by the parent's effort level, shares its main-chain/subagent
  status, and belongs to whatever skill the parent was running under. Advisor rows have a
  synthetic id the message_id skill backfill can't reach, so
  `scanner.propagate_advisor_column` copies it down from the parent —
  otherwise the advisor half of a call, which is the expensive half, pools into the
  "main thread" bucket while its parent sits under a skill.

This keeps the "each turn knows its own model" pricing invariant, so fable-5 rates
flow through every existing cost query with no per-callsite change.

**Consequence:** turn counts for advisor-using sessions include these rows. A view
that wants only model-authored turns should exclude `tool_name = 'advisor'`.

`schema.py` carries a value-level canary (`KNOWN_ITERATION_TYPES`) that fails
`scan --doctor` on any iteration `type` outside `{message, advisor_message}` — a
new type would be another billable class going unrecorded.
