# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

wbl ("WayBackLog Machine") is a screenshot-driven backlog. Screenshots dropped into a project folder get **triaged** into ticket files by a read-only `claude -p` run, then **resolved** by `claude -p` agents running in parallel, each in its own git worktree on branch `wbl/<ID>`. The README is the user-facing spec. Keep it in sync when behavior changes.

## Constraints

- **Standard library only, Python 3.11+** (`tomllib`, `fcntl`, so Unix only). Don't add dependencies, a build step, or a package layout. The frontend is one static `ui.html` with inline JS and no build.
- There is no test suite, linter, or formatter config yet. `WBL_CLAUDE` overrides `claude_bin` so that a stub can stand in for the real `claude` CLI. Use it for any testing that shouldn't spend tokens.

## Running it

```sh
make run                                         # the user's real backlog (path in Makefile), http://127.0.0.1:8765
make run BACKLOG=/tmp/bl PORT=8799               # a scratch backlog: wbl.toml + one folder per project
WBL_CLAUDE=/path/to/stub make run BACKLOG=/tmp/bl PORT=8799   # same, with a stub instead of real agents
```

The user drives everything from the board. The CLI is only `serve` plus `triage IMAGES…` / `resolve IDS…`, which the server launches; don't add user-facing subcommands. The UI says **Prep** for triage and **Do** for resolve, while the code keeps the triage/resolve names. Every UI action that starts agents carries the token-coin icon, and acting on more than one item asks for confirmation first. Its tooltip shows observed usage (median tokens from `prep_tokens` / `tokens` on current tickets) and the `*_budget_usd` limit. Don't present the limit as an estimate: the user found that misleading. Keep it that way: the user doesn't want clicks that spend tokens without warning. Don't run real Prep/Do against the user's backlog without asking. Use a stub `WBL_CLAUDE` to test.

## Architecture

- **`wbl.py` holds all the logic.** `wbl_server.py` is a thin JSON API that imports `wbl` and calls its functions (`load_tickets`, `inbox`, `get_ticket`, `finish_ticket`, `worktree_changes`). New behavior goes in `wbl.py` first and gets exposed through the server afterward.
- **The server never runs agents in-process.** `/api/triage` and `/api/resolve` spawn the ordinary CLI (`python wbl.py --root … triage|resolve …`) with `start_new_session=True` and log to `~/.wbl/jobs/`. This keeps a single code path, and a server restart doesn't kill agents. Keep it that way.
- **The files on disk are the only state.** Tickets are `<project>/.wbl/<ID>.md`. Their frontmatter is flat, single-line `key: value`, parsed by hand (no YAML). The field order lives in `FIELDS`, and a new field must be added there plus to `INT_FIELDS`/`FLOAT_FIELDS` if numeric. The CLI and the server re-read the files on every request, and the UI polls `/api/state` every 4 s, so there is no cache to invalidate.
- **Concurrency rules:**
  - `Ticket.save()` writes atomically (temp file + `os.replace`).
  - `allocate_id` uses `flock` on `.wbl/counter`. IDs are never reused.
  - `resolve_one` re-loads the ticket from disk before writing results, so UI edits made during a run survive.
  - `git worktree add` runs serially, and only the agents run in parallel (`ThreadPoolExecutor`, `max_parallel`).
- **Project → components.** A project (a backlog folder, with one ID prefix) has one or more `Component`s, each a git repo with its own `base`. A single `repo =` in the config becomes one component named after the project. Triage runs from the first component's repo, gets the others via `--add-dir`, and picks the ticket's `component` through a schema enum. Everything git-related (worktree, diff base, branch cleanup) goes through `ticket_component()`, never through the project. An empty `component` on a ticket means "the only one".
- **Inbox = image files in a project folder that no ticket's `image` field names.** Renaming a screenshot after triage makes it reappear in the inbox.
- **`run_claude`** is the only place that invokes Claude Code. It sends the prompt on stdin (variadic flags like `--allowedTools` would swallow a positional prompt), uses `--output-format json`, and parses `structured_output`, `modelUsage` tokens, `total_cost_usd`, `session_id`, and `permission_denials`.
  - Triage is read-only and uses `--json-schema TRIAGE_SCHEMA`.
  - Resolve runs with `acceptEdits` plus the allow/deny lists from config. Agents must never commit; `DEFAULT_RESOLVE_DISALLOWED` blocks git write commands.
- **Prompts and rubrics** (priority P0–P2, Fibonacci points 1–13, required spec sections) are in `TRIAGE_PROMPT` / `RESOLVE_PROMPT` in `wbl.py`. `PRIORITIES`, `POINTS`, and `STATUSES` are served to the UI through `/api/state`, so the UI doesn't hardcode them.
- **`finish_ticket` (`done`) safety rails:** it refuses to remove a dirty worktree and keeps unmerged branches (`git branch -d`). `force` overrides both. Don't weaken these.
- **Server security model:** it binds to 127.0.0.1 only. POSTs require `Content-Type: application/json` (the CSRF defense), and `/img/` only serves image files directly inside a configured project folder. Preserve all of these when adding endpoints.
