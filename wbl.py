#!/usr/bin/env python3
"""
WayBackLog Machine (wbl) — a minimal, screenshot-driven ticket tracker that hands work to Claude Code.

The whole input surface is a folder of screenshots:

    backlog/                      <- the "root"; holds wbl.toml
      wbl.toml                    <- maps each project folder to a git repo
      skrutable/                  <- one folder per project
        meter-crash-on-anustubh.png      <- an untriaged screenshot (the "inbox")
        .wbl/                     <- wbl's own state for this project (hidden in Finder)
          SKR-7.md                <- one ticket = one markdown file
          counter                 <- last ID handed out, so IDs are never reused
          prepping/               <- a marker per screenshot whose Prep session is running

Lifecycle of a ticket (UI button names in quotes; "Prep" = triage, "Do" = resolve):

    screenshot dropped in   ->  inbox (no ticket file yet)
    "Prep"                  ->  pending      a Claude Code session in a Terminal window reads the input,
                                             the code and anything else it needs (web, packages), and
                                             ends with a short ticket; wbl files it from that message
    "Do"                    ->  in_progress  a Claude Code session in a Terminal window edits the repo
                                             where it is, on whatever branch you have checked out.
                                             One ticket at a time per repo, since they share the checkout.
                            ->  review       Claude finished its turn; its report is on the ticket and its
                                             changes sit uncommitted in your checkout
                            ->  failed       the session ended without a report
    "Done"                  ->  (gone)       screenshot + ticket file deleted (branches and commits are
                                             yours to manage)

Agents are ordinary Claude Code sessions, so they use whatever auth and settings your Claude Code
already has, and the session is the only record of a run: wbl keeps no logs. Claude Code tells
wbl when the agent finishes a turn through hooks (`wbl hook`), so results land on the board while
the session is still open. Agents never commit or switch branches.

Standard library only; Python 3.11+ (for tomllib). Start it with `make run`.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import tomllib
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# ----------------------------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------------------------

CONFIG_NAME = "wbl.toml"
WBL_DIR = ".wbl"  # per-project state folder; the leading dot keeps it out of Finder
# What can go in a project folder as an inbox item. Claude Code's Read tool takes all of these.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
TEXT_EXTS = {".txt", ".md"}
PDF_EXTS = {".pdf"}
INPUT_EXTS = IMAGE_EXTS | TEXT_EXTS | PDF_EXTS


def input_kind(name: str) -> str:
    """"image", "text" or "pdf": decides how the UI previews an input and how Prep describes it."""
    ext = Path(name).suffix.lower()
    return "text" if ext in TEXT_EXTS else "pdf" if ext in PDF_EXTS else "image"


def text_excerpt(path: Path, limit: int = 600) -> str:
    """The start of a text input, for its card on the board."""
    try:
        with open(path, "rb") as fh:
            return fh.read(limit * 4).decode("utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def pdf_thumbnail(state_dir: Path, pdf: Path) -> Path | None:
    """A PNG of page 1, made with macOS's built-in `sips` and cached in .wbl/thumbs/.
    Re-made when the PDF is newer than the cached image. None if it can't be made."""
    thumb = state_dir / "thumbs" / f"{pdf.name}.png"
    try:
        if thumb.exists() and thumb.stat().st_mtime >= pdf.stat().st_mtime:
            return thumb
        thumb.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["sips", "-s", "format", "png", "-Z", "480", str(pdf), "--out", str(thumb)],
                           capture_output=True, timeout=30)
        return thumb if r.returncode == 0 and thumb.exists() else None
    except (OSError, subprocess.TimeoutExpired):
        return None

PRIORITIES = ("P0", "P1", "P2")
# Fibonacci points, as in most Jira setups. The rubric below tells the agent what each means.
POINTS = (1, 2, 3, 5, 8, 13)
STATUSES = ("pending", "in_progress", "review", "failed")

HERE = Path(__file__).resolve().parent


def nfc(s: str) -> str:
    """Names compared as strings go through this. macOS returns file names decomposed ("a" +
    combining macron for "ā") while text typed elsewhere, e.g. wbl.toml, is usually composed.
    The filesystem treats both as the same file, but Python string comparison does not."""
    return unicodedata.normalize("NFC", s)


class WblError(Exception):
    """An expected, user-facing failure (bad ID, dirty worktree, ...). Printed without a traceback."""


# ----------------------------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------------------------

@dataclass
class Component:
    """One git repo belonging to a project. A project with several repos (say a library and its
    front end) has one component per repo; triage decides which one a ticket belongs to."""

    name: str        # e.g. "skrutable_front_end" (stored on tickets, so keep it stable)
    repo: Path       # local clone the agents work in, on whatever branch is checked out
    label: str = ""  # how the UI shows it, e.g. "Front End"
    venv: Path | None = None  # Python virtualenv the agents use for this repo


ACRONYMS = {"js", "ts", "ui", "api", "css", "db", "cli", "nlp", "v1", "v2", "v3"}


def derive_labels(project: str, names: list[str]) -> dict[str, str]:
    """Readable labels from repo names: drop what the names share with the project (or with
    each other), then title-case. "skrutable_front_end" -> "Front End", "hansel-app" -> "App"."""
    seps = "-_ "

    def strip(n: str, prefix: str) -> str:
        return n[len(prefix):].lstrip(seps) if prefix and n.lower().startswith(prefix.lower()) else n

    shared = os.path.commonprefix(names) if len(names) > 1 else ""
    shared = shared[: max(shared.rfind(c) for c in seps) + 1] if shared else ""  # whole words only
    out = {}
    for n in names:
        rest = strip(n, project) if strip(n, project) != n else strip(n, shared)
        words = rest.replace("-", " ").replace("_", " ").split()
        out[n] = " ".join(w.upper() if w.lower() in ACRONYMS else w.capitalize() for w in words) or "Core"
    return out


@dataclass
class Project:
    """One subfolder of the backlog root, tied to one or more local git repos (components)."""

    name: str        # folder name, e.g. "skrutable"
    key: str         # ticket prefix, e.g. "SKR"
    components: dict[str, Component]
    folder: Path     # <root>/<name>

    @property
    def state_dir(self) -> Path:
        return self.folder / WBL_DIR

    def component(self, name: str) -> Component:
        """Look up a component; an empty name is fine when the project has only one."""
        if not self.components:
            raise WblError(f"project {self.name} has no repo, so there is nothing for an agent to work in")
        if not name and len(self.components) == 1:
            return next(iter(self.components.values()))
        try:
            return self.components[name]
        except KeyError:
            raise WblError(f"project {self.name} has no component {name!r}; "
                           f"known: {', '.join(self.components)}")


@dataclass
class Config:
    root: Path
    projects: dict[str, Project]
    claude_bin: str = "claude"
    triage_model: str = "opus"
    resolve_model: str = "opus"
    # The terminal app that sessions open in: "Terminal", "iTerm", or an argv list in
    # which "{cmd}" stands for the shell command to run, e.g. ["kitty", "sh", "-c", "{cmd}"].
    terminal: str | list[str] = "Terminal"

    def project(self, name: str) -> Project:
        try:
            return self.projects[nfc(name)]
        except KeyError:
            raise WblError(f"unknown project {name!r}; known: {', '.join(self.projects) or '(none)'}")


def find_root(explicit: str | None) -> Path:
    """The backlog root is --root, else $WBL_ROOT, else the nearest ancestor holding wbl.toml."""
    if explicit or os.environ.get("WBL_ROOT"):
        root = Path(explicit or os.environ["WBL_ROOT"]).expanduser().resolve()
        if not (root / CONFIG_NAME).exists():
            raise WblError(f"no {CONFIG_NAME} in {root}")
        return root
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / CONFIG_NAME).exists():
            return d
    raise WblError(f"no {CONFIG_NAME} found here or above; pass --root or set WBL_ROOT")


def load_config(root: Path) -> Config:
    raw = tomllib.loads((root / CONFIG_NAME).read_text())
    d = raw.get("defaults", {})
    projects: dict[str, Project] = {}
    for name, p in raw.get("projects", {}).items():
        # Either one `repo = ...` (a single component named after the project), or
        # `components = { name = "path", other = { repo = "path", label = "Front End" } }`.
        # A component's `venv` (or the project's, for components without one) is the Python
        # virtualenv agents use there.
        if "components" in p:
            spec = p["components"]
        elif "repo" in p:
            spec = {name: {"repo": p["repo"]}}
        else:
            spec = {}  # no code at all (a "misc" folder): Prep still works, Do doesn't
        components = {}
        derived = derive_labels(name, list(spec))
        for cname, c in spec.items():
            c = {"repo": c} if isinstance(c, str) else c
            venv = c.get("venv", p.get("venv"))
            components[cname] = Component(name=cname, repo=Path(c["repo"]).expanduser().resolve(),
                                          label=c.get("label", derived[cname]),
                                          venv=Path(venv).expanduser() if venv else None)
        name = nfc(name)
        projects[name] = Project(
            name=name,
            key=p.get("key", name[:3].upper()),
            components=components,
            folder=root / name,
        )
    cfg = Config(root=root, projects=projects)
    # Copy over any defaults that were set; unknown keys are ignored rather than fatal.
    for key in ("claude_bin", "triage_model", "resolve_model", "terminal"):
        if key in d:
            setattr(cfg, key, d[key])
    cfg.claude_bin = os.environ.get("WBL_CLAUDE", cfg.claude_bin)  # lets tests swap in a stub
    return cfg


# ----------------------------------------------------------------------------------------------
# Tickets: one markdown file each, with a tiny flat frontmatter block
# ----------------------------------------------------------------------------------------------

# Frontmatter keys, in the order they are written. All values are single-line scalars, so a
# hand-rolled "key: value" parser is enough and we avoid a YAML dependency.
FIELDS = ["id", "project", "component", "title", "status", "priority", "points", "image", "created",
          "branch", "worktree", "session", "tokens", "cost_usd", "prep_session", "prep_tokens",
          "prep_cost_usd"]
INT_FIELDS = {"points", "tokens", "prep_tokens"}
FLOAT_FIELDS = {"cost_usd", "prep_cost_usd"}


@dataclass
class Ticket:
    id: str
    project: str
    title: str
    status: str
    priority: str
    points: int
    image: str               # file name of the input (screenshot, note or PDF) in the project folder
    created: str
    component: str = ""      # which of the project's repos the work happens in ("" = the only one)
    branch: str = ""         # only on tickets from before Do worked in your checkout: their
    worktree: str = ""       #   own branch wbl/<ID> in a separate worktree, cleaned up by Done
    session: str = ""        # Claude Code session id of the last Do session (for `claude --resume`)
    tokens: int | None = None    # total tokens that session has used
    cost_usd: float | None = None    # only on old tickets: Claude Code doesn't log cost for sessions
    prep_session: str = ""   # the session that prepped it
    prep_tokens: int | None = None      # what Prep used, so the UI can show what runs typically take
    prep_cost_usd: float | None = None
    body: str = ""           # the markdown spec, plus appended resolution notes
    path: Path | None = None  # where the ticket file lives (not serialized)

    # -- serialization -------------------------------------------------------------------------

    def dump(self) -> str:
        lines = ["---"]
        for k in FIELDS:
            v = getattr(self, k)
            v = "" if v is None else str(v).replace("\n", " ")
            lines.append(f"{k}: {v}")
        lines.append("---")
        return "\n".join(lines) + "\n\n" + self.body.strip() + "\n"

    @classmethod
    def load(cls, path: Path) -> "Ticket":
        text = path.read_text()
        if not text.startswith("---\n"):
            raise WblError(f"{path} has no frontmatter")
        head, _, body = text[4:].partition("\n---\n")
        values: dict[str, object] = {}
        for line in head.splitlines():
            k, sep, v = line.partition(":")
            if not sep or k.strip() not in FIELDS:
                continue
            k, v = k.strip(), v.strip()
            if k in INT_FIELDS:
                values[k] = int(v) if v else None
            elif k in FLOAT_FIELDS:
                values[k] = float(v) if v else None
            else:
                values[k] = v
        values.setdefault("points", 3)
        return cls(**values, body=body.strip(), path=path)  # type: ignore[arg-type]

    def save(self) -> None:
        """Atomic write (temp file + rename) so the server never reads a half-written ticket."""
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".md.tmp")
        tmp.write_text(self.dump())
        os.replace(tmp, self.path)

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in FIELDS}
        d["body"] = self.body
        d["image_url"] = f"/img/{self.project}/{self.image}"
        d["kind"] = input_kind(self.image)
        d["thumb_url"] = f"/thumb/{self.project}/{self.image}"
        return d


def load_tickets(cfg: Config, project: str | None = None) -> list[Ticket]:
    projects = [cfg.project(project)] if project else list(cfg.projects.values())
    out = []
    for p in projects:
        if p.state_dir.is_dir():
            out.extend(Ticket.load(f) for f in sorted(p.state_dir.glob("*.md")))
    return out


def get_ticket(cfg: Config, ticket_id: str) -> Ticket:
    ticket_id = ticket_id.upper()
    for p in cfg.projects.values():
        f = p.state_dir / f"{ticket_id}.md"
        if f.exists():
            return Ticket.load(f)
    raise WblError(f"no ticket {ticket_id}")


def ticket_component(cfg: Config, t: Ticket) -> Component:
    return cfg.project(t.project).component(t.component)


def inbox(cfg: Config, project: str | None = None) -> list[tuple[Project, Path]]:
    """Inputs (screenshots, notes, PDFs) sitting in a project folder that no ticket points at yet."""
    projects = [cfg.project(project)] if project else list(cfg.projects.values())
    out = []
    for p in projects:
        if not p.folder.is_dir():
            continue
        claimed = {nfc(t.image) for t in load_tickets(cfg, p.name)}
        for f in sorted(p.folder.iterdir()):
            if f.is_file() and f.suffix.lower() in INPUT_EXTS and nfc(f.name) not in claimed:
                out.append((p, f))
    return out


def allocate_id(project: Project) -> str:
    """Hand out the next ID. An flock on the counter file keeps parallel triage runs (or the CLI
    and the web UI at once) from handing out the same number twice. IDs are never reused, even
    after a ticket is deleted, so an ID in a commit message or a log always means one thing."""
    project.state_dir.mkdir(parents=True, exist_ok=True)
    counter = project.state_dir / "counter"
    with open(counter, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        n = int(fh.read().strip() or 0) + 1
        fh.seek(0)
        fh.truncate()
        fh.write(str(n))
    return f"{project.key}-{n}"


# ----------------------------------------------------------------------------------------------
# Running Claude Code. Every agent run is an ordinary Claude Code session, and that session is
# the only record of it: wbl keeps no logs. Claude Code calls back into wbl through hooks
# (`wbl hook`) each time the agent finishes a turn, so the ticket is written while the session is
# still open. The wbl process around each session repeats that check when `claude` exits, in case
# a hook never ran (say, the window was closed mid-turn).
# ----------------------------------------------------------------------------------------------

def claude_flags(*, model: str, add_dirs: list[Path] = ()) -> list[str]:
    # No permission flags or tool lists: a session is a normal Claude Code session, with the
    # user's own settings and permission mode.
    flags = ["--model", model]
    for d in add_dirs:
        flags += ["--add-dir", str(d)]
    return flags


def hook_settings(cfg: Config, kind: str, target: str) -> str:
    """`--settings` JSON that makes Claude Code call `wbl hook KIND TARGET` (see cmd_hook)."""
    cmd = shlex.join([sys.executable, str(HERE / "wbl.py"), "--root", str(cfg.root), "hook", kind, target])
    events = ["Stop", "SessionEnd", *(["UserPromptSubmit"] if kind == "do" else [])]
    return json.dumps({"hooks": {e: [{"hooks": [{"type": "command", "command": cmd}]}] for e in events}})


def agent_command(cfg: Config, kind: str, target: str, *, session: str,
                  resume: bool = False) -> tuple[list[str], Path, Path | None]:
    """The `claude` command line for one agent run, the directory to run it in, and the Python
    venv to activate for it. `kind` is "prep" (target "project/file") or "do" (target a ticket ID)."""
    if kind == "prep":
        project, image = inbox_file(cfg, target)
        comps = [c for c in project.components.values() if c.repo.is_dir()]
        if project.components and not comps:
            raise WblError(f"none of {project.name}'s repos exist")
        prompt = triage_prompt(project, image, comps, draft_path(project, session))
        cwd = comps[0].repo if comps else project.folder
        venv = comps[0].venv if comps else None
        flags = claude_flags(
            model=cfg.triage_model,
            # The screenshot lives outside the repos, and the other components are siblings.
            add_dirs=[*([image.parent] if comps else []), *(c.repo for c in comps[1:])])
    else:
        t = get_ticket(cfg, target)
        prompt, cwd, venv = resolve_prompt(cfg, t), workdir(cfg, t), ticket_component(cfg, t).venv
        flags = claude_flags(
            model=cfg.resolve_model,
            add_dirs=[cfg.project(t.project).folder])
    # The prompt goes first: --add-dir takes several values and would swallow a trailing
    # positional argument.
    cmd = [cfg.claude_bin, *(["--resume", session] if resume else [prompt, "--session-id", session]),
           *flags, "--settings", hook_settings(cfg, kind, target)]
    return cmd, cwd, venv


def venv_env(venv: Path | None) -> dict[str, str] | None:
    """The environment with `venv` activated, so `python`/`pip` in the session are its own."""
    if not venv:
        return None
    env = {k: v for k, v in os.environ.items() if k != "PYTHONHOME"}
    return {**env, "VIRTUAL_ENV": str(venv), "PATH": f"{venv / 'bin'}{os.pathsep}{env.get('PATH', '')}"}


def find_transcript(session: str) -> Path | None:
    """Where Claude Code keeps a session's transcript: <config dir>/projects/<cwd, mangled>/<id>.jsonl."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    return next(base.glob(f"*/{session}.jsonl"), None)


def read_transcript(session: str) -> tuple[int, str]:
    """(tokens the session has used, the agent's last message). Claude Code writes one line per
    content block, each repeating its message's usage, so usage is counted once per message id."""
    path = find_transcript(session)
    usage: dict[str, dict] = {}
    last_text = ""
    for line in open(path) if path else []:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") != "assistant":
            continue
        msg = e.get("message") or {}
        if msg.get("id") and msg.get("usage"):
            usage[msg["id"]] = msg["usage"]
        text = "\n\n".join(b.get("text", "") for b in msg.get("content") or []
                           if isinstance(b, dict) and b.get("type") == "text").strip()
        if text and not e.get("isSidechain"):
            last_text = text
    tokens = sum(u.get(k, 0) or 0 for u in usage.values() for k in
                 ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return tokens, last_text


def run_session(cfg: Config, kind: str, target: str, session: str, *, resume: bool = False) -> None:
    """Run one agent session to its end, then make sure its outcome is on the board."""
    cmd, cwd, venv = agent_command(cfg, kind, target, session=session, resume=resume)
    try:
        code = subprocess.run(cmd, cwd=cwd, env=venv_env(venv)).returncode
    except OSError as e:
        code = f"could not start {cfg.claude_bin}: {e}"
    if kind == "prep":
        prep_ended(cfg, target, session)
    else:
        do_ended(cfg, target, session, code)


# ----------------------------------------------------------------------------------------------
# Layer 1: prep / triage (screenshot -> spec, priority, points)
# ----------------------------------------------------------------------------------------------

def inbox_file(cfg: Config, item: str) -> tuple[Project, Path]:
    """"project/file name" -> (project, path), for a file directly inside a project folder."""
    name, _, file = item.partition("/")
    project = cfg.project(name)
    if not file or "/" in file or Path(file).suffix.lower() not in INPUT_EXTS:
        raise WblError(f"not a project input: {item}")
    return project, project.folder / file


def prepping(cfg: Config) -> set[str]:
    """The "project/file" items being prepped right now: one marker file each in .wbl/prepping/,
    holding the session ID, made when Prep starts and removed when it ends."""
    return {nfc(f"{p.name}/{f.name}") for p in cfg.projects.values()
            for f in (p.state_dir / "prepping").glob("*") if f.is_file()}


def claim_prep(cfg: Config, items: list[str]) -> dict[str, str]:
    """Mark inbox items as being prepped, so two runs never ticket the same screenshot, and give
    each its session ID. Returns {item: session}."""
    waiting = {nfc(f"{p.name}/{f.name}") for p, f in inbox(cfg)}
    claimed: dict[str, str] = {}
    try:
        for item in items:
            project, image = inbox_file(cfg, item)
            if nfc(item) not in waiting:
                raise WblError(f"not in the inbox: {item}")
            marker = project.state_dir / "prepping" / image.name
            marker.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                raise WblError(f"{item} is already being prepped")
            claimed[item] = str(uuid.uuid4())
            with os.fdopen(fd, "w") as fh:
                fh.write(claimed[item])
    except WblError:
        for item in claimed:
            release_prep(cfg, item)
        raise
    return claimed


def release_prep(cfg: Config, item: str) -> None:
    project, image = inbox_file(cfg, item)
    (project.state_dir / "prepping" / image.name).unlink(missing_ok=True)


def prep_session_of(cfg: Config, item: str) -> str:
    project, image = inbox_file(cfg, item)
    marker = project.state_dir / "prepping" / image.name
    if marker.exists():
        return marker.read_text().strip()
    t = ticket_for_image(cfg, item)
    if t and t.prep_session:
        return t.prep_session
    raise WblError(f"{item} has no prep session")


def ticket_for_image(cfg: Config, item: str) -> Ticket | None:
    project, image = inbox_file(cfg, item)
    return next((t for t in load_tickets(cfg, project.name) if nfc(t.image) == nfc(image.name)), None)


# The parts of TRIAGE_PROMPT that depend on whether the project has code to look at.
TRIAGE_WITH_REPOS = dict(
    code_context="""The project consists of these git repositories ("components"):
{components}
The current directory is the first of them. Read, Glob and Grep work in all of them;
`git log` / `git show` only in the current directory.{venvs}""",
    explore="Then explore the repositories until you can point at the specific code involved.",
    component_line="""- component: the one repository where the change should be made. If it spans several, pick
  the main one and say in the spec what the others need.
""",
    where="file paths with line numbers",
)
TRIAGE_WITHOUT_REPOS = dict(
    code_context="""This project has no code repository: it collects miscellaneous tasks (accounts, hosting,
services, chores). Work from the input and the hint alone; there is no code to look at.""",
    explore="Then work out what has to be done, by whom or where, and in what order.",
    component_line="",
    where="the services, accounts or sites involved",
)

TRIAGE_PROMPT = """\
You are triaging one item in a personal backlog for the project "{project}".
{code_context}

The only input is one file and its context:
- Input: {image}   ({kind}; open it with the Read tool and go through it carefully)
- File name hint: "{hint}"   (the author's few-word description; treat it as the main clue to intent)
- Project folder: {project}

Work out what the author wants: a bug to fix, a feature, a UI change, a wording fix, etc.
{explore} Your job is the ticket, not the fix: leave the repositories as they are. Scratch
files and experiments in a temporary directory are fine.

If the work depends on something outside the code (an external API, SDK, library or service),
research it now rather than leaving it to the implementer: read the docs, inspect the installed
package or download it and read its source, and try it out. Put what you verified in the spec.

Do everything you can on your own. If you need something from the author (a decision, access, a
key, a file), stop and ask for the minimum: a short list of just the next things you need.

Work out:
- title: a short imperative title.
{component_line}- priority, using this rubric:
    P0  broken or wrong output, data loss, or blocks normal use
    P1  a real bug with a workaround, or a clearly valuable feature
    P2  polish, cosmetics, nice-to-have
- points, an estimate of implementation effort for a coding agent (roughly, how much it will
  need to read and change), using this rubric:
    1   one-line or trivially local change
    2   small change in one file
    3   a few related changes, maybe two or three files
    5   touches several modules, or needs new tests
    8   substantial feature or refactor
    13  too big for one pass; say how to split it
- spec: a short markdown ticket the author will read and edit, with these sections:
    ## Problem          (two or three sentences: what the input shows and what the author wants)
    ## Fix              (a few concrete steps, citing {where})
    ## Done when        (two to four checkable bullets)
    ## Notes            (only if needed: facts you verified outside the code, open questions)
  Be terse: no restating the input at length, no estimate rationale, no padding. Aim for
  under 200 words.

Write the ticket to {draft} (wbl files it on the board from there each time you finish a
reply), in exactly this form: these lines, a blank line, then the spec.
title: ...
{component_key}priority: P1
points: 3

## Problem
...
If the author asks for changes, update that file. Don't repeat the ticket in your messages:
end with one or two sentences on what it asks for, plus your short list of asks if you have any.
"""


def triage_prompt(project: Project, image: Path, comps: list[Component], draft: Path) -> str:
    if comps:
        listing = "\n".join(f"- {c.name}: {c.repo}" + (f" (Python venv: {c.venv})" if c.venv else "")
                            for c in comps)
        venvs = ("\nTo run Python for a repository, use its venv's interpreter (`<venv>/bin/python`), which\n"
                 "has that repository's packages installed." if any(c.venv for c in comps) else "")
        if comps[0].venv:
            venvs += " The first repository's venv is active, so plain `python` and `pip` are its."
        parts = {**TRIAGE_WITH_REPOS,
                 "code_context": TRIAGE_WITH_REPOS["code_context"].format(components=listing, venvs=venvs)}
    else:
        parts = TRIAGE_WITHOUT_REPOS
    return TRIAGE_PROMPT.format(
        project=project.name, image=image, hint=image.stem.replace("-", " ").replace("_", " "), **parts,
        draft=draft, component_key="component: ...\n" if comps else "",
        kind={"image": "a screenshot", "text": "a text note", "pdf": "a PDF"}[input_kind(image.name)])


def draft_path(project: Project, session: str) -> Path:
    """Where a Prep session writes its ticket (see TRIAGE_PROMPT)."""
    return project.state_dir / "drafts" / f"{session}.md"


def parse_draft(text: str, project: Project) -> dict:
    """The ticket fields from a Prep draft ("key: value" lines, a blank line, the spec), checked."""
    head, _, spec = text.strip().partition("\n\n")
    s = {k.strip().lower(): v.strip() for k, sep, v in (line.partition(":") for line in head.splitlines()) if sep}
    s["spec"] = spec
    s["points"] = int(s["points"]) if s.get("points", "").isdigit() else None
    comps = [c for c in project.components.values() if c.repo.is_dir()]
    problems = [f"{k} is missing" for k in ("title", "spec") if not str(s.get(k) or "").strip()]
    if s.get("priority") not in PRIORITIES:
        problems.append(f"priority must be one of {', '.join(PRIORITIES)}")
    if s.get("points") not in POINTS:
        problems.append(f"points must be one of {', '.join(map(str, POINTS))}")
    if comps and s.get("component") not in project.components:
        problems.append(f"component must be one of {', '.join(c.name for c in comps)}")
    if problems:
        raise WblError("; ".join(problems))
    return s


def file_ticket(cfg: Config, item: str, session: str) -> Ticket | None:
    """Create the ticket from a Prep session's draft, or update it if the draft changed since it
    was last filed (you asked for changes; an unchanged draft leaves your edits on the board
    alone). Returns None if there's nothing new; raises WblError if the draft isn't valid."""
    project, image = inbox_file(cfg, item)
    draft = draft_path(project, session)
    filed = draft.with_suffix(".filed")
    text = draft.read_text() if draft.exists() else ""
    if not text.strip() or (filed.exists() and filed.read_text() == text):
        return None
    s = parse_draft(text, project)
    tokens, _ = read_transcript(session)
    t = next((x for x in load_tickets(cfg, project.name) if x.prep_session == session), None)
    if t is None:
        t = Ticket(id=allocate_id(project), project=project.name, title="", status="pending",
                   priority="P1", points=3, image=image.name,
                   created=datetime.now().isoformat(timespec="minutes"), prep_session=session)
        t.path = project.state_dir / f"{t.id}.md"
    if not t.worktree and t.status == "pending":
        t.component = s.get("component", "")  # a later Do pins it, after which it can't move
    t.title, t.priority, t.points = s["title"].strip(), s["priority"], s["points"]
    # Replace the spec but keep any Do reports below it.
    reports = t.body.find("\n\n## Resolution")
    t.body = s["spec"].strip() + (t.body[reports:] if reports >= 0 else "")
    t.prep_tokens, t.prep_cost_usd = tokens, None
    t.save()
    filed.write_text(text)
    release_prep(cfg, item)
    return t


def prep_ended(cfg: Config, item: str, session: str) -> None:
    """After a Prep session: file the ticket if no hook did, then clear the draft and free the
    screenshot (without a ticket, it goes back to the inbox)."""
    project, _ = inbox_file(cfg, item)
    try:
        file_ticket(cfg, item, session)
    except WblError:
        pass
    draft = draft_path(project, session)
    draft.unlink(missing_ok=True)
    draft.with_suffix(".filed").unlink(missing_ok=True)
    release_prep(cfg, item)


# ----------------------------------------------------------------------------------------------
# Layer 2: do / resolve (spec -> uncommitted changes in your checkout)
# ----------------------------------------------------------------------------------------------

RESOLVE_PROMPT = """\
You are implementing ticket {id} in the project "{project}".
The current directory is the project's git repository, on the branch the author has checked
out. Work there: the author manages branches and commits, and reviews your changes
uncommitted. The checkout may already hold uncommitted changes of the author's; leave those
alone.

The original input (a screenshot, note or PDF) is at {image} if you want to look at it.
{venv}
# {title}

{spec}

Instructions:
1. Re-check the relevant code; the spec may be off.
2. If the change depends on an external API, SDK or library, verify it (docs, the installed
   package's source, a quick experiment) rather than writing code from guesses.
3. Make the change. Keep it focused on this ticket; match the surrounding code style and
   document what you add.
4. If the repo has tests, run the relevant ones, and add a test when the fix is testable.
5. Do everything you can on your own, then stop and check in. End with a short report in
   markdown: what you changed (files) and how you verified it, then, if anything is left, the
   minimum you need from the author next, as a short list. Your last message goes on the
   ticket verbatim.
"""


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


def workdir(cfg: Config, t: Ticket) -> Path:
    """Where Do runs for this ticket: the component's checkout (or, for a ticket from before Do
    worked in place, the separate worktree it still has)."""
    return Path(t.worktree) if t.worktree and Path(t.worktree).exists() else ticket_component(cfg, t).repo


def resolve_prompt(cfg: Config, t: Ticket) -> str:
    p = cfg.project(t.project)
    name = p.name if len(p.components) == 1 else f"{p.name}/{t.component}"
    spec = t.body.split("\n\n## Resolution")[0]  # earlier reports live in the session, if resumed
    venv = ticket_component(cfg, t).venv
    venv = (f"\nThe repository's Python venv, {venv}, is active: `python` and `pip` are its, and it has\n"
            "the project's packages installed. Use it; don't create another environment.\n") if venv else ""
    return RESOLVE_PROMPT.format(id=t.id, project=name, title=t.title, spec=spec,
                                 image=p.folder / t.image, venv=venv)


# The server handles requests on several threads; two quick clicks must not both pass the
# "one ticket per repo" check before either has marked its ticket in_progress.
_claim_lock = threading.Lock()


def claim_do(cfg: Config, ids: list[str], resume: bool = False) -> list[Ticket]:
    """Mark tickets in_progress before their sessions start, refusing any that would share a
    checkout with a running one (or with each other). Gives each a new session ID, unless
    `resume`. Pins each ticket's component, so a later config change can't point it elsewhere."""
    with _claim_lock:
        tickets = [get_ticket(cfg, i) for i in ids]
        busy = {str(workdir(cfg, o)): o.id for o in load_tickets(cfg) if o.status == "in_progress"}
        for t in tickets:
            if t.status == "in_progress":
                raise WblError(f"{t.id} is already in progress")
            t.component = ticket_component(cfg, t).name  # also fails for a project without a repo
            if resume and not (t.session and find_transcript(t.session)):
                raise WblError(f"{t.id} has no earlier session to continue")
            where = str(workdir(cfg, t))
            if where in busy:
                raise WblError(f"{busy[where]} is already being worked on in {where}; "
                               f"wait for it to finish before starting {t.id}")
            busy[where] = t.id
        for t in tickets:
            t.status = "in_progress"
            if not resume:
                t.session = str(uuid.uuid4())  # chosen up front, so the board can show it right away
            t.save()
        return tickets


def record_report(t: Ticket, session: str, text: str, ok: bool = True) -> Ticket:
    """Put a Do session's latest outcome on the ticket. Each session has one report section,
    replaced each time the agent finishes a turn. Re-reads the ticket first: you may have
    changed priority/points in the UI meanwhile."""
    fresh = Ticket.load(t.path)
    tokens, _ = read_transcript(session)
    fresh.status = "review" if ok else "failed"
    fresh.tokens, fresh.cost_usd = tokens, None
    mark = f"session {session[:8]}"
    m = re.search(rf"\n\n## Resolution[^\n]*{mark}", fresh.body)
    if m:  # drop this session's previous report
        nxt = fresh.body.find("\n\n## Resolution", m.end())
        fresh.body = fresh.body[:m.start()] + (fresh.body[nxt:] if nxt >= 0 else "")
    stamp = datetime.now().isoformat(timespec="minutes")
    fresh.body += f"\n\n## Resolution — {stamp} ({fresh.status}, {tokens:,} tokens, {mark})\n\n{text.strip()}"
    fresh.save()
    return fresh


def do_ended(cfg: Config, ticket_id: str, session: str, code) -> None:
    """After a Do session: if no hook recorded an outcome, record one from the transcript."""
    t = get_ticket(cfg, ticket_id)
    if t.status != "in_progress" or t.session != session:
        return
    _, text = read_transcript(session)
    record_report(t, session, text or f"(the session ended before Claude reported; exit status {code})",
                  ok=bool(text))


# ----------------------------------------------------------------------------------------------
# Sessions in a terminal window, and the hooks Claude Code calls
# ----------------------------------------------------------------------------------------------

def open_terminal(cfg: Config, command: list[str], cwd: Path) -> None:
    """Run `command` in a new terminal window (cfg.terminal) and return straight away. The
    window's process is not ours: it lives on after the server stops."""
    shell_cmd = f"cd {shlex.quote(str(cwd))} && {shlex.join(command)}"
    if isinstance(cfg.terminal, list):
        argv = [a.replace("{cmd}", shell_cmd) for a in cfg.terminal]
        subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    quoted = '"' + shell_cmd.replace("\\", "\\\\").replace('"', '\\"') + '"'  # an AppleScript string
    if cfg.terminal == "Terminal":
        script = f'tell application "Terminal"\nactivate\ndo script {quoted}\nend tell'
    elif cfg.terminal == "iTerm":
        script = ('tell application "iTerm"\nactivate\nset w to (create window with default profile)\n'
                  f'tell current session of w to write text {quoted}\nend tell')
    else:
        raise WblError(f'terminal must be "Terminal", "iTerm" or an argv list with "{{cmd}}", not {cfg.terminal!r}')
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WblError(f"could not open {cfg.terminal}: {e}")
    if r.returncode:
        # The usual cause: macOS hasn't been allowed to let this process control the terminal app
        # (System Settings > Privacy & Security > Automation).
        raise WblError(f"could not open {cfg.terminal}: {r.stderr.strip()}")


def open_session(cfg: Config, kind: str, target: str, resume: bool = False) -> None:
    """Open a terminal window running `wbl session KIND TARGET`: an interactive agent session."""
    cmd = [sys.executable, str(HERE / "wbl.py"), "--root", str(cfg.root), "session", kind, target,
           *(["--resume"] if resume else [])]
    open_terminal(cfg, cmd, cfg.root)


def start_prep(cfg: Config, items: list[str]) -> None:
    """Prep in terminal windows, one per item."""
    claim_prep(cfg, items)
    for n, item in enumerate(items):
        try:
            open_session(cfg, "prep", item)
        except WblError:
            for rest in items[n:]:
                release_prep(cfg, rest)
            raise


def start_do(cfg: Config, ids: list[str], resume: bool = False) -> None:
    """Do in terminal windows, one per ticket. `resume` continues each ticket's last session."""
    tickets = claim_do(cfg, ids, resume=resume)
    for n, t in enumerate(tickets):
        try:
            open_session(cfg, "do", t.id, resume=resume)
        except WblError:
            for rest in tickets[n:]:
                rest = get_ticket(cfg, rest.id)
                rest.status = "review" if rest.tokens else "pending"
                rest.save()
            raise


def cmd_session(cfg: Config, args: argparse.Namespace) -> None:
    """The process inside a terminal window: one interactive agent session."""
    if args.kind == "prep":
        session = prep_session_of(cfg, args.target)
        title, what = args.target, "When Claude has written the ticket, it shows up on the board"
    else:
        t = get_ticket(cfg, args.target)
        if t.status != "in_progress" or not t.session:
            raise WblError(f"{t.id} wasn't started from the board (status {t.status}); use Do there")
        session, title = t.session, f"{t.id} {t.title}"
        here = workdir(cfg, t)
        branch = git(here, "branch", "--show-current", check=False).stdout.strip() or "detached HEAD"
        what = f"Working in {here} (on {branch}; nothing gets committed). Claude's report shows up on the board"
    print(f"\033]0;wbl: {title}\007", end="")  # window title
    print(f"{title}\n{what} each time it finishes. Stay to follow up, or quit (/exit) whenever.\n", flush=True)
    # Closing the window hangs up (SIGHUP) on claude and on us. We catch it, rather than ignore
    # it, so that claude (which execs with default handlers) still exits while we stay long
    # enough to record the outcome. Ctrl-C belongs to claude (it interrupts the agent), not us.
    for sig in (signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, lambda *_: None)
    run_session(cfg, args.kind, args.target, session, resume=args.resume)
    try:
        print("\nRecorded on the board. You can close this window.", flush=True)
    except OSError:  # the window is already gone
        pass


def cmd_hook(cfg: Config, args: argparse.Namespace) -> None:
    """Called by Claude Code (hook_settings) with the event as JSON on stdin.

    Stop (the agent finished a turn): Prep files or updates the ticket from its draft;
    Do records the report and moves the ticket to Review. UserPromptSubmit (you asked a Do
    session for more): back to In progress. SessionEnd: Prep frees the screenshot, Do records
    whatever it has if nothing has yet."""
    event = json.load(sys.stdin)
    name, session = event.get("hook_event_name"), event.get("session_id", "")
    if args.kind == "prep":
        if name == "Stop":
            try:
                file_ticket(cfg, args.target, session)
            except WblError:
                pass  # a half-written draft (say, Claude asked you something first); try next turn
        elif name == "SessionEnd":
            prep_ended(cfg, args.target, session)
        return
    # Claude Code hands Stop hooks the final message; the transcript may not have it yet.
    text = event.get("last_assistant_message") or read_transcript(session)[1]
    t = get_ticket(cfg, args.target)
    if t.session != session:
        return  # not the ticket's current session (say, an old one reopened by hand)
    if name == "Stop":
        record_report(t, session, text or "(Claude finished without a message)", ok=bool(text))
    elif name == "UserPromptSubmit" and t.status != "in_progress":
        t.status = "in_progress"
        t.save()
    elif name == "SessionEnd":
        do_ended(cfg, t.id, session, "session ended")


# ----------------------------------------------------------------------------------------------
# Finishing: cleanup
# ----------------------------------------------------------------------------------------------

def finish_ticket(cfg: Config, t: Ticket, force: bool = False, keep_image: bool = False) -> list[str]:
    """Remove every trace of a ticket: screenshot, ticket, and for a ticket from before Do
    worked in your checkout, its worktree and branch (if merged). Your checkout is not touched.

    `keep_image` is "discard": the ticket goes, but the screenshot stays and so drops back into
    the inbox, ready to be prepped again (say, after renaming it).

    Safety rails, which `force` overrides (use it for won't-fix / throw-away):
      * a ticket whose agent is still running is left alone;
      * an old-style worktree with uncommitted changes is not removed, since that work would be lost;
      * a branch that is not merged is kept (`git branch -d` refuses) and we say so.
    """
    p = cfg.project(t.project)
    notes: list[str] = []
    if t.status == "in_progress" and not force:
        raise WblError(f"{t.id}: an agent is still working on it; cancel that run first")
    # Only looked up when there is git state to clean up; tickets never resolved don't need it.
    repo = ticket_component(cfg, t).repo if t.branch else None

    if t.worktree and Path(t.worktree).exists():
        dirty = git(Path(t.worktree), "status", "--porcelain", check=False).stdout.strip()
        if dirty and not force:
            raise WblError(f"{t.id}: worktree {t.worktree} has uncommitted changes. Commit and "
                            "merge them, or finish with force to discard them.")
        r = git(repo, "worktree", "remove", *(["--force"] if force else []), t.worktree, check=False)
        if r.returncode:
            raise WblError(f"{t.id}: could not remove worktree: {r.stderr.strip()}")
        notes.append(f"removed worktree {t.worktree}")

    if t.branch:
        r = git(repo, "branch", "-D" if force else "-d", t.branch, check=False)
        if r.returncode == 0:
            notes.append(f"deleted branch {t.branch}")
        elif "not found" not in r.stderr:
            notes.append(f"kept branch {t.branch} (not merged; delete it yourself if unwanted)")

    doomed = [t.path, *p.state_dir.glob(f"logs/{t.id}-*")]  # logs: only old tickets have them
    if not keep_image:
        doomed += [p.folder / t.image, p.state_dir / "thumbs" / f"{t.image}.png"]
    for f in doomed:
        if f and f.exists():
            f.unlink()
    notes.append("deleted the ticket file" + ("" if keep_image else f" and {t.image}"))
    return notes


def set_component(cfg: Config, t: Ticket, name: str) -> None:
    """Move a ticket to another of its project's repos (triage guessed wrong). Not while an
    agent works on it, nor for an old-style ticket whose branch lives in the old repo."""
    c = cfg.project(t.project).component(name)
    if c.name != t.component and (t.worktree or t.status == "in_progress"):
        raise WblError(f"{t.id} is being or was worked on in {t.component}; finish it first")
    t.component = c.name


# ----------------------------------------------------------------------------------------------
# Entry point. Everything is driven from the browser UI (`make run`). `session` is what the server
# runs in a terminal window, so sessions outlive a server restart; `hook` is what Claude Code calls.
# ----------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="wbl", description=__doc__.strip().splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="backlog folder holding wbl.toml (default: search upward, or $WBL_ROOT)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="the board, in the browser")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="open a browser tab")

    s = sub.add_parser("session", help="(used by the server, in a terminal window) one agent session")
    s.add_argument("kind", choices=["prep", "do"])
    s.add_argument("target", help="PROJECT/FILE for prep, a ticket ID for do")
    s.add_argument("--resume", action="store_true", help="continue the ticket's last session")

    s = sub.add_parser("hook", help="(used by Claude Code) record a session's progress")
    s.add_argument("kind", choices=["prep", "do"])
    s.add_argument("target")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(find_root(args.root))
        if args.cmd == "serve":
            # Run as a script, this file is `__main__`; without this, wbl_server's `import wbl`
            # would load it a second time, and its `except wbl.WblError` would not catch errors
            # raised by objects made here (a different WblError class).
            sys.modules.setdefault("wbl", sys.modules[__name__])
            import wbl_server  # kept in its own module so this one stays readable
            wbl_server.serve(cfg, args.port, args.open)
        else:
            {"session": cmd_session, "hook": cmd_hook}[args.cmd](cfg, args)
    except WblError as e:
        print(f"wbl: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
