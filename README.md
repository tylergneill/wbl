# WayBackLog Machine

A small, screenshot-driven backlog that hands its tickets to Claude Code.
Its files are named `wbl` too (`wbl.toml`, `.wbl/`).

You drop a screenshot into a project folder, with a few words of description in its file name.
A text note (`.txt`, `.md`) or a PDF works too. Cards show a preview: the first lines of a
note, or page 1 of a PDF (rendered with macOS's `sips` and cached in `.wbl/thumbs/`).
"Screenshot" below means any of these.
On the board, **Prep** turns it into a ticket: a spec grounded in the actual code, the repo it
belongs to, a priority from P0 to P2, and an effort estimate in points. **Do** sends an agent at
a prepped ticket. Both open a Claude Code session in a Terminal window, which you can watch,
answer and steer. Do edits the repo where it is, on whatever branch you have checked out, and
never commits. You review the changes, commit what
you like, and mark the ticket **Done**. That deletes the ticket and its screenshot.

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
repo = "~/code/skrutable"   # Do edits this checkout, on whatever branch is checked out
```

A project that spans several repos lists them as **components** instead of a single `repo`.
Prep looks at all of them and records which one each ticket belongs to, and Do then works
in that repo. You can change the component in the ticket's panel while no agent is on it. The board
labels components from their repo names, dropping the part shared with the project or with
each other: `skrutable_front_end` becomes "Front End" and `hansel-app` becomes "App". To set
your own label, write the component as a table, e.g.
`skrutable = { repo = "~/Git/skrutable/skrutable", label = "Python Library" }`.

```toml
[projects.hansel]
key = "HAN"
components = { hansel-app = "~/Git/hansel/hansel-app", hansel-data = { repo = "~/Git/hansel/hansel-data", label = "Data" } }
```

For Python repos, give agents the right virtualenv with `venv = "~/venvs/hansel-311"`, either on
the project (for all its components) or in a component's table (for that one). Do sessions run
with that venv active, so `python` and `pip` are its. Prep sessions have the first component's
venv active, and the prompt lists the venv of every component.

## Using the board

* **Select** tiles with their checkbox, or by ⌘/Shift-clicking them. "select all" sits on
  the Inbox column header. A plain click opens a tile's detail panel.
* **Prep** inbox screenshots from the header ("Prep selected"), by right-clicking a tile, or
  from its panel. Each one opens a Claude Code session in a Terminal window. The ticket appears
  on the board as soon as Claude has written it. You don't need to close the session: stay to
  ask for changes (the ticket is updated each time Claude replies), or close it whenever.
* **Do** prepped tickets the same way: "Do selected", right-click, or the panel. Each opens a
  session in a Terminal window that works in your checkout. Its report lands on the ticket (and
  the ticket moves to Review) each time Claude finishes; ask for more in the same window and the
  ticket goes back to In progress until it's done again.
* Only **one ticket per repo** can be in progress at a time, because sessions share your
  checkout. Do refuses a second one.
* **Continue in terminal** (in a Review or Failed ticket's panel) reopens its Do session, with
  its history.
* **Edit** in a ticket's panel lets you rewrite its title and text. **Export .md** downloads
  the ticket as plain markdown; "Export selected" in the header puts several in one file.
* **Discard** deletes a ticket but keeps its screenshot, which goes back to the inbox. You can
  discard from the right-click menu or the panel, or press Delete/⌫ to discard the selected
  tickets (or the open one). If you rename a screenshot after Prep, its ticket shows the image
  as missing: discard that ticket, then prep the screenshot under its new name.
* Buttons that start sessions carry a gold token coin. wbl doesn't predict cost: hovering the
  button shows what past sessions of that kind used (median, in millions of tokens). Sessions
  have no spending limit; you end them yourself. Acting on one item starts right away. Acting
  on several asks first.

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
      prepping/                   ← a marker per screenshot whose Prep session is open
```

A ticket is a markdown file with a flat frontmatter block (id, component, title, status, priority,
points, image, session, tokens, prep_session, prep_tokens) and the spec as its body. You can
read and edit these by hand; the UI re-reads them on every request. The screenshot
stays where you dropped it. **Don't rename a screenshot after Prep:** the ticket refers to
it by name, so a renamed file would show up in the inbox again as a new item.

wbl keeps no logs. Every agent run is an ordinary Claude Code session, and that session is the
record: the ticket stores its ID, and `claude --resume <id>` (shown in the panel) reopens it.

### Lifecycle

```
screenshot ──Prep──▶ pending ──Do──▶ in_progress ⇄ review ──Done──▶ (deleted)
                                          └──▶ failed ──Do again──▶ …
```

The server opens each session in a terminal window (`terminal`: Terminal.app by default)
running `wbl.py session prep|do …`, which starts an interactive `claude`. The first time, macOS
asks whether the app running the server may control Terminal: allow it. Claude Code reports
back through hooks that wbl passes with `--settings`: each time Claude finishes a turn, it runs
`wbl.py hook …`, which files the ticket (Prep) or records the report (Do). When the session
ends, wbl checks once more, in case a hook never ran.

**Prep** (triage in the code) starts in the project's first repo, with the other components
added as extra directories. Claude gets the image path, the file name as a hint ("meter wrong
on vipula"), and the project's repos. It is told to leave the repos alone (scratch files in a
temporary directory are fine), to research external APIs and SDKs before writing the ticket,
and, if it needs something from you, to stop and ask for just the next things. It writes the ticket (title,
component, priority, points, spec) to a draft file that wbl checks and files each time Claude
finishes a reply, so the session itself just ends with a sentence or two.
The spec is short: Problem, Fix, Done when, and Notes when needed. The rubrics for priority and
points are in `TRIAGE_PROMPT` in `wbl.py`, so that's the place to tune them.

**Do** (resolve in the code) runs in the component's own checkout, on whatever branch you have
checked out, next to any uncommitted work of yours. It is told to leave that alone and to
leave branches and commits to you. It does what it can, then stops and checks in with a short
list of what it needs from you next, if anything.

Both are normal Claude Code sessions: wbl sets no permission mode and no tool lists, so your
own Claude Code settings apply. Each runs with its repo's `venv` active.
Claude's last message becomes the ticket's report, along with the session's token count.

**Review** is normal git. The changes sit uncommitted in your checkout. Branch, commit and
merge as you like. Your commit hooks run as they always do, because wbl never commits for you.

**Done** removes the screenshot and the ticket file; your checkout is left as it is. (Tickets
from before Do worked in place still have their own worktree and `wbl/<ID>` branch. Done
removes those too, but refuses if the worktree has uncommitted changes, and keeps an unmerged
branch. When it refuses, the board offers to discard everything, which overrides both rails.)

If a session's window dies without the session ending cleanly, a screenshot can stay
"prepping" (its panel has a **Not prepping** button) or a ticket can stay "in progress" (set
its status back in the panel).

### Points and tokens

Points use the usual Fibonacci scale, and each value is defined in terms of agent effort:
1 is a one-liner, 2 is one file, 3 is a few files, 5 means several modules or new tests, 8 is
a substantial feature, and 13 means "split this". Each ticket records the tokens its Prep and
Do sessions used, so after a few weeks you can check how points line up with actual spend and
adjust the rubric. (Claude Code doesn't record dollar cost for interactive sessions.)

### The server

The server binds to 127.0.0.1 only. The page polls `/api/state` every 4 s, so changes made by
sessions or a hand-edited ticket file show up on their own. Sessions don't run inside the
server, so restarting it never kills one. Write requests must be `application/json`, and other
websites open in your browser cannot send that cross-origin, so they can't drive the API.

Session windows open on the machine running the server.

## Configuration reference (`[defaults]`)

| key | default | |
|---|---|---|
| `triage_model`, `resolve_model` | `"opus"` | any `claude --model` value |
| `terminal` | `"Terminal"` | `"Terminal"`, `"iTerm"`, or an argv list where `"{cmd}"` is the shell command, e.g. `["kitty", "sh", "-c", "{cmd}"]` |
| `claude_bin` | `"claude"` | can also be set with `$WBL_CLAUDE` |
