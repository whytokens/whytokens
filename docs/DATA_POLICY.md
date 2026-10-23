# Data policy

What whytokens stores, where, and how to delete it. Code: `policy.py`.

## Stored by default

`turns.tool_calls` keeps, per tool call, the tool name and an allowlisted `input`:

| Field | Tools | Meaning |
|---|---|---|
| `input_size` | all | length of the original input JSON |
| `file_path` | Read, Write, Edit, MultiEdit | the path (needed for repeated-read findings) |
| `notebook_path` | NotebookEdit | the path |
| `command_len` | Bash, Codex exec/shell/apply_patch | length of the command |
| `subagent_type` | Task, Agent | agent type |
| `skill` | Skill | skill name |

MCP tool inputs, search patterns, prompts, file contents and shell commands are **not** stored.
Token counts, timestamps, model names, project and branch names, and session titles are
stored (see `SCHEMA.md`).

## Raw inputs (opt-in)

`WHYTOKENS_STORE_RAW_INPUTS=1` stores full tool inputs. Off by default. It applies to
scans made while it is set; it does not recover inputs already removed.

## Existing databases

The first scan on a database holding unredacted rows rewrites every stored `tool_calls` row under the default policy
and runs `VACUUM`, so the removed text is not left in free pages. The migration is skipped
while the raw opt-in is on. Transcripts themselves are never modified.

## Desktop-app cache (opt-in)

Claude Projects thread titles and coordinator spend are read from the claude.ai cache of the
Claude desktop app (`config.DESKTOP_BLOB_DIR`, read-only). This is off unless
`WHYTOKENS_DESKTOP_CACHE=1`.

## Network

Scanning and analysis make no outbound connections (tested by patching `socket` and
`subprocess`). The dashboard listens on loopback only; `--unsafe-host` is required to bind
anything else. Requests need a loopback `Host`, a same-origin `Origin` when one is sent, and
POSTs need the per-run token embedded in the served page.

## Deleting your data

Delete the database file (`~/.whytokens/whytokens.db` plus its `-wal`/`-shm` files).
It is rebuilt from the transcripts that still exist; history already pruned from them is lost.
