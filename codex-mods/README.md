# Codex Mods

A Codex command-hook adapter for the four Nate Herk Claude mods. The upstream Claude packages are unchanged. Code is MIT licensed; see LICENSE. Tested runtime: Codex CLI 0.159.3; Python 3.12. The runtime uses Python's standard library and SQLite; Windows execution has not been tested.

| Original mod | Codex behavior | Limit |
|---|---|---|
| Collision Guard | Atomically reserves every apply_patch target before an edit; blocks another session's reservation or recent edit | Applies to sessions using this adapter; arbitrary shell writes and the original Claude mods' ledgers are not tracked |
| Recording Mode | Blocks non-control shell/read/image/MCP tools, private patch targets; adds a privacy instruction to prompts | No native screen masking, output redaction, or secret-free video guarantee; this is not an OS sandbox |
| Goal Meter | Weighted tasks, a printed progress bar, start/done/clear commands | No native UI bar or ETA; tasks must be updated by the agent |
| Cache Keeper | Session board, measured input/cached/output tokens, handoff skill | No cache TTL, price estimate, keep-warm or automatic clear |

## Install

```bash
codex plugin marketplace add nicksonnenberg/claude-code-mods --ref main
codex plugin add codex-mods@nateherk-mods-codex
```

Open a fresh Codex session. Review and trust this plugin's five hooks in `/hooks`; installation alone does not trust them. Invoke `$mods` and ask for status, a session board, a goal task list, recording safeguards, or a handoff. The skill resolves the CLI path from the installed package. Do not run hooks from unreviewed upstream versions.

Recording safeguards start off. Collision protection starts on. Recording off removes its flag. State is local under ~/.codex/mods-data/mods.sqlite3 (or MODS_DATA for an isolated test). Hook writes are serialized in one SQLite transaction. Reservations expire after five minutes; successful-edit history expires after 30 minutes. Unknown outcomes retain only the five-minute reservation. SessionEnd removes a session's ledger and goal; hook calls prune session/goal rows older than 24 hours. Retention expiry is not a claim that a user process is disposable. No processes are stopped.

Record tokens only from token_count entries in a bounded transcript tail. No prompt or reply contents are printed by the board. It lists adapter-registered sessions, not all Codex or Claude sessions. Recording status is shared only across these adapters, not the original Claude plugins.

Remove with `codex plugin remove codex-mods@nateherk-mods-codex`. Local user data remains; back it up before deleting ~/.codex/mods-data when no adapter sessions are open.

## Verify

```bash
python3 -m unittest discover -s tests -v
ruff check codex-mods/scripts tests
```

See the repository tests for competing reservations, patch moves, failed edits, resumed identity, recording controls, traversal and usage reporting. These tests are not proof that arbitrary scripts cannot bypass hooks. The native runtime smoke test must separately verify hook loading and trust.
