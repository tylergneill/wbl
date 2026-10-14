# WayBackLog Machine

A small, screenshot-driven backlog that hands its tickets to Claude Code.
Its files are named `wbl` too (`wbl.toml`, `.wbl/`, branches `wbl/<ID>`).

You drop a screenshot into a project folder, with a few words of description in its file name.
A text note (`.txt`, `.md`) or a PDF works too. Cards show a preview: the first lines of a
note, or page 1 of a PDF (rendered with macOS's `sips` and cached in `.wbl/thumbs/`).
"Screenshot" below means any of these.
On the board, **Prep** turns it into a ticket: a spec grounded in the actual code, the repo it
belongs to, a priority from P0 to P2, and an effort estimate in points. **Do** sends an agent at
a prepped ticket. Each one gets its own git branch and worktree, and they run in parallel. You
review the result, merge what you like, and mark the ticket **Done**. That deletes the ticket
and its screenshot.

There are three files and no dependencies beyond Python 3.11+ and the `claude` CLI:

| file | what it is |
|---|---|
| `wbl.py` | All the logic: config, ticket files, prep (triage), do (resolve), cleanup. |
| `wbl_server.py` | A JSON API over the same files, for the board. |
| `ui.html` | The board. It is one static page with no build step. |

## Running it

```sh
make run                               # the board, at http://127.0.0.1:8765
make run BACKLOG=~/other PORT=8800     # another backlog folder or port
```

`BACKLOG` is the folder holding `wbl.toml`. It defaults to the path in the `Makefile`.

## Configuration

Add one table per project to `wbl.toml`. The table name is the subfolder you drop screenshots
into. A name with diacritics needs quotes, e.g. `[projects."vātāyana"]`. If you rename a
folder, rename its table to match. A table with no repo at all (say `[projects.misc]` with
only a `key`) is for non-code tasks. Prep writes the ticket from the screenshot alone, and
there is no Do.

```toml
[projects.skrutable]
key  = "SKR"                 # ticket IDs: SKR-1, SKR-2, …
repo = "~/code/skrutable"
base = "main"                # Do branches start here (default: whatever is checked out)
```

A project that spans several repos lists them as **components** instead of a single `repo`.
Prep looks at all of them and records which one each ticket belongs to, and Do then branches
in that repo. You can change the component in the ticket's panel until Do has run. The board
labels components from their repo names, dropping the part shared with the project or with
each other: `skrutable_front_end` becomes "Front End" and `hansel-app` becomes "App". To set
your own label, write the component as a table, e.g.
`skrutable = { repo = "~/Git/skrutable/skrutable", label = "Python Library" }`.

```toml
[projects.hansel]
key = "HAN"
components = { hansel-app = "~/Git/hansel/hansel-app", hansel-data = { repo = "~/Git/hansel/hansel-data", base = "main" } }
```

## Using the board

* **Select** tiles with their checkbox, or by ⌘/Shift-clicking them. "select all" sits on
  the Inbox column header. A plain click opens a tile's detail panel.
* **Prep** inbox screenshots from the header ("Prep selected"), by right-clicking a tile, or
  from its panel.
* **Do** prepped tickets the same way: "Do selected", right-click, or the panel.
* **Discard** deletes a ticket but keeps its screenshot, which goes back to the inbox. You can
  discard from the right-click menu or the panel, or press Delete/⌫ to discard the selected
  tickets (or the open one). **Re-prep** discards a ticket and preps its screenshot again, which
  gives it a new ID. If you rename a screenshot after Prep, its ticket shows the image as
  missing: discard that ticket, then prep the screenshot under its new name.
* Buttons that start agents carry a gold token coin. wbl doesn't predict cost. Hovering the
  button shows what past runs of that kind actually used (median, in millions of tokens), along
  with the per-run dollar limit, which is a ceiling and not an estimate. Acting on one item
  starts right away. Acting on several asks first, and the prompt says the same things.
* A running job appears in the header with a **cancel** button, which also stops its agents.

## How it works

### Files on disk

```
backlog/
  wbl.toml
  skrutable/
    meter-wrong-on-vipula.png     ← inbox: a screenshot no ticket points at yet
    .wbl/                         ← hidden in Finder
      SKR-4.md                    ← the ticket
      counter                     ← last ID handed out; IDs are never reused
      logs/SKR-4-triage.json      ← raw agent output, for debugging
~/.wbl/worktrees/skrutable/SKR-4 ← the agent's checkout, on branch wbl/SKR-4
```

A ticket is a markdown file with a flat frontmatter block (id, component, title, status, priority,
points, image, branch, worktree, session, tokens, cost) and the spec as its body. You can
read and edit these by hand; the CLI and UI re-read them on every request. The screenshot
stays where you dropped it. **Don't rename a screenshot after Prep:** the ticket refers to
it by name, so a renamed file would show up in the inbox again as a new item.

### Lifecycle

```
screenshot ──triage──▶ pending ──resolve──▶ in_progress ──▶ review ──done──▶ (deleted)
                                                    └──▶ failed ──resolve again──▶ …
```

**Prep** (triage in the code) runs `claude -p` with its working directory set to the
project's first repo, and the other components added as extra directories. The agent gets the
image path, the file name as a hint ("meter wrong on vipula"), and the project's repos. It may
only Read, Glob, Grep and look at `git log`/`git show`, so it cannot change anything. It
returns structured output, validated by `--json-schema`: a title, the component, a priority,
points, and a spec. The spec has fixed sections: what the input shows,
interpretation, relevant code with line numbers, proposed fix, acceptance criteria, and the
reasoning behind the estimate. The rubrics for priority and points are in `TRIAGE_PROMPT` in
`wbl.py`, so that's the place to tune them.

**Do** (resolve in the code) first creates the worktrees, one at a time, because `git worktree add` doesn't
like running concurrently on one repo. It then runs up to `max_parallel` agents at once. Each
agent works only in its own directory, so two tickets on the same repo never collide, and
your own checkout is never touched. Agents run in `acceptEdits` mode with a shell allowlist
(`resolve_allowed_tools`), and anything outside the list is refused automatically. They are
told not to commit, and `git commit/push/reset/checkout/branch/...` are explicitly
disallowed as well. When an agent finishes, wbl appends its report to the ticket, along with
tokens and cost. Any blocked tool calls are listed too: if the same one keeps showing up,
add it to the allowlist. It also stores the session ID, so you can pick up where the agent
stopped:

```sh
cd ~/.wbl/worktrees/skrutable/SKR-4 && claude --resume <session>   # shown in the UI, with a copy button
```

Doing a ticket again reuses its worktree, so a second attempt builds on the first.

**Review** is normal git. The changes sit uncommitted in the worktree, so you can look at
them in the ticket's panel ("Show changes") or in your editor. Then commit and
merge as usual. Your commit hooks run as they always do, because wbl never commits for you.

**Done** removes everything belonging to the ticket: the worktree, the `wbl/<ID>` branch if
it has been merged, the screenshot, the ticket file and its logs. The card disappears from the
board. Two safety rails stop it from losing work:

* If the worktree has **uncommitted** changes, done refuses to run. The UI then offers to discard them.
* If the branch is committed but **not merged**, the branch is kept, and you're told about it.

When Done refuses, the board offers to discard everything, which overrides both rails. Use it for won't-fix tickets.

### Points and cost

Points use the usual Fibonacci scale, and each value is defined in terms of agent effort:
1 is a one-liner, 2 is one file, 3 is a few files, 5 means several modules or new tests, 8 is
a substantial feature, and 13 means "split this". After each run, the ticket records the
real token count and cost, so after a few weeks you can check how points line up with actual
spend and adjust the rubric. In testing, a 2-point ticket used roughly 170–250k tokens with
Sonnet. Most of that is cached context, which costs about $0.10.

Each ticket records its Prep and its last Do run (tokens and dollars), and the board's
tooltips are built from those numbers. Spending caps apply per agent run, and a run that hits
its cap is stopped: `triage_budget_usd` (default $1) and `resolve_budget_usd`
(default $5). Set either one to `0` to remove the cap.

### The server

The server binds to 127.0.0.1 only. The page polls `/api/state` every 4 s, so changes made by
agents or a hand-edited ticket file show up on their own. Prep and Do don't run inside the
server: each one starts `wbl.py triage …` / `wbl.py resolve …` as a separate background
process, with its log in `~/.wbl/jobs/`, so restarting the server never kills a running agent.
Write requests must be `application/json`, and other websites open in your browser cannot
send that cross-origin, so they can't drive the API.

The server can live on your always-on home machine and still be used from the work machine,
through an SSH tunnel:

```sh
ssh -L 8765:127.0.0.1:8765 home-machine    # then open http://127.0.0.1:8765 locally
```

## Configuration reference (`[defaults]`)

| key | default | |
|---|---|---|
| `triage_model`, `resolve_model` | `"sonnet"` | any `claude --model` value |
| `max_parallel` | `3` | agents at once, for triage and for resolve |
| `triage_budget_usd`, `resolve_budget_usd` | `1.0`, `5.0` | per-run cap; `0` = none |
| `worktree_root` | `~/.wbl/worktrees` | |
| `resolve_allowed_tools` | Read/Edit/Write/Glob/Grep + git read commands, `python`, `pytest`, `uv run` | Claude Code permission-rule syntax, e.g. `"Bash(npm test:*)"` |
| `resolve_disallowed_tools` | git commit/push/reset/checkout/switch/branch/worktree/rebase, `rm -rf` | |
| `claude_bin` | `"claude"` | can also be set with `$WBL_CLAUDE` |
