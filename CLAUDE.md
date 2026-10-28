# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

wbl ("WayBackLog Machine") is a screenshot-driven backlog. Screenshots dropped into a project folder get **prepped** (triaged) into short ticket files, then **done** (resolved) in the component's checkout, on whatever branch the user has checked out (the user manages branches; wbl never creates or switches them). Both are interactive Claude Code sessions in a terminal window the server opens. There is no headless mode and wbl keeps no logs: the user wants every agent visible, and the session is the record. The README is the user-facing spec. Keep it in sync when behavior changes.

## Constraints

- **Standard library only, Python 3.11+** (`tomllib`, `fcntl`, so Unix only). Don't add dependencies, a build step, or a package layout. The frontend is one static `ui.html` with inline JS and no build.
- There is no test suite, linter, or formatter config yet. `WBL_CLAUDE` overrides `claude_bin` so that a stub can stand in for the real `claude` CLI. Use it for any testing that shouldn't spend tokens.

## Running it

```sh
make run                                         # the user's real backlog (path in Makefile), http://127.0.0.1:8765
make run BACKLOG=/tmp/bl PORT=8799               # a scratch backlog: wbl.toml + one folder per project
WBL_CLAUDE=/path/to/stub make run BACKLOG=/tmp/bl PORT=8799   # same, with a stub instead of real agents
```

The user drives everything from the board. The CLI is only `serve`, plus `session prep|do TARGET [--resume]` (what the server runs in a terminal window) and `hook prep|do TARGET` (what Claude Code calls); don't add user-facing subcommands. The UI says **Prep** for triage and **Do** for resolve, while the code keeps the triage/resolve names. Every UI action that starts agents carries the token-coin icon, and acting on more than one item asks for confirmation first. Its tooltip shows observed usage (median tokens from `prep_tokens` / `tokens` on current tickets) and says sessions have no spending limit. Don't present anything as a cost estimate: the user found that misleading. Keep it that way: the user doesn't want clicks that spend tokens without warning. Don't run real Prep/Do against the user's backlog without asking. Use a stub `WBL_CLAUDE` to test.

## Architecture

- **`wbl.py` holds all the logic.** `wbl_server.py` is a thin JSON API that imports `wbl` and calls its functions (`load_tickets`, `inbox`, `get_ticket`, `finish_ticket`, `prepping`, `start_prep`, `start_do`, `release_prep`). New behavior goes in `wbl.py` first and gets exposed through the server afterward.
- **The server never runs agents.** `start_prep` / `start_do` claim the work, then `open_terminal` (AppleScript for Terminal/iTerm, or the `terminal` argv template) runs `wbl.py session …` in a new window. A server restart doesn't kill sessions. Keep it that way.
- **Sessions and hooks.** `agent_command` builds the `claude` command line: the prompt as the *first* positional argument (variadic flags like `--add-dir` would swallow a trailing one), `--session-id` chosen up front (or `--resume`), and `--settings` with hooks (`hook_settings`) for Stop, SessionEnd and (Do) UserPromptSubmit that call `wbl.py hook`. On Stop, Prep reads the draft the agent wrote to `.wbl/drafts/<session>.md` (`parse_draft`: `key: value` lines, a blank line, the spec) and, if it changed since last filed (a `.filed` copy, so UI edits aren't clobbered), creates or updates the ticket whose `prep_session` matches (`file_ticket`); the user doesn't want the ticket dumped in the session. Do replaces that session's report section and sets Review (`record_report`). UserPromptSubmit sets a Do ticket back to In progress. `run_session` (the process in the window) catches SIGHUP/SIGINT (not SIG_IGN, so `claude` keeps default handlers) and after `claude` exits re-checks from the transcript (`prep_ended` / `do_ended`), since hooks passed via `--settings` and SessionEnd on window close are not guaranteed by the docs. Tokens come from the transcript (`read_transcript`: usage deduped by message id). Cost is unknown for interactive sessions.
- **The files on disk are the only state.** Tickets are `<project>/.wbl/<ID>.md`. Their frontmatter is flat, single-line `key: value`, parsed by hand (no YAML). The field order lives in `FIELDS`, and a new field must be added there plus to `INT_FIELDS`/`FLOAT_FIELDS` if numeric. A running Prep is a marker file `.wbl/prepping/<file name>` holding its session ID; a running Do is `status: in_progress`. The server re-reads the files on every request, and the UI polls `/api/state` every 4 s, so there is no cache to invalidate.
- **Concurrency rules:**
  - `Ticket.save()` writes atomically (temp file + `os.replace`).
  - `allocate_id` uses `flock` on `.wbl/counter`. IDs are never reused.
  - `claim_prep` creates markers with `O_EXCL`, so one screenshot is never prepped twice.
  - Sessions share the user's checkout, so **at most one ticket per repo is `in_progress`** (`claim_do`, under a lock since the server is threaded; the user explicitly doesn't want more yet).
  - `record_report` re-loads the ticket from disk before writing, so UI edits made during a run survive.
- **Project → components.** A project (a backlog folder, with one ID prefix) has one or more `Component`s, each a git repo (Do works on whatever branch it has checked out). A single `repo =` in the config becomes one component named after the project. Prep runs from the first component's repo, gets the others via `--add-dir`, and names the ticket's `component`. Everything git-related (the working directory via `workdir()`, old-ticket cleanup) goes through `ticket_component()`, never through the project. An empty `component` on a ticket means "the only one". A component's `venv` (or the project's) is activated for the session (`venv_env`: `VIRTUAL_ENV` + `PATH`) and named in the prompts.
- **Inbox = input files in a project folder that no ticket's `image` field names.** Renaming a screenshot after Prep makes it reappear in the inbox.
- **Prompts and rubrics** (priority P0–P2, Fibonacci points 1–13, the short spec sections) are in `TRIAGE_PROMPT` / `RESOLVE_PROMPT` in `wbl.py`. The user wants tickets concise and editable; keep the spec format short. `PRIORITIES`, `POINTS`, and `STATUSES` are served to the UI through `/api/state`, so the UI doesn't hardcode them.
- **No restrictions.** Sessions are normal Claude Code sessions: no `--permission-mode`, `--allowedTools` or `--disallowedTools`, so the user's own settings apply. The user explicitly doesn't want wbl limiting what agents can do. Guidance lives in the prompts only: Prep leaves the repos alone (scratch files are fine), Do leaves branches, commits and the user's uncommitted work alone, and both do what they can, then stop and ask for the minimal next things they need.
- **`finish_ticket` (`done`)** never touches the user's checkout. Tickets from before in-place Do still carry `branch`/`worktree`; for those it refuses to remove a dirty worktree and keeps unmerged branches (`git branch -d`), and `force` overrides both. Don't weaken these.
- **Server security model:** it binds to 127.0.0.1 only. POSTs require `Content-Type: application/json` (the CSRF defense), and `/img/` only serves image files directly inside a configured project folder. Items like "project/file" go through `inbox_file`, which rejects anything but a single file name with an input extension, and every terminal command is built as an argv list and quoted with `shlex`, never pasted together from request text. Preserve all of these when adding endpoints.
