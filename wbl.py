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
          logs/SKR-7-resolve.json <- raw agent output, for debugging

Lifecycle of a ticket (UI button names in quotes; "Prep" = triage, "Do" = resolve):

    screenshot dropped in   ->  inbox (no ticket file yet)
    "Prep"                  ->  pending      agent reads image + filename + repos, writes spec,
                                             picks the component, estimates priority (P0-P2)
                                             and points (1-13)
    "Do"                    ->  in_progress  one git worktree + branch per ticket, agents run in parallel
                            ->  review       agent finished; changes sit uncommitted in the worktree
                            ->  failed       agent errored or hit its budget
    "Done"                  ->  (gone)       worktree removed, merged branch deleted,
                                             screenshot + ticket file + logs deleted

Both agent layers shell out to Claude Code in headless mode (`claude -p`), so they use whatever
auth and settings your Claude Code already has. Triage is read-only; resolve may edit files and
run a whitelisted set of shell commands, but only inside its own worktree, and never commits.

Standard library only; Python 3.11+ (for tomllib). Start it with `make run`.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tomllib
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
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
    repo: Path       # local clone the agents work against
    base: str        # branch/commit that resolve branches start from ("HEAD" = whatever is checked out)
    label: str = ""  # how the UI shows it, e.g. "Front End"


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


# Commands the resolve agent may run without asking. Everything else is denied automatically,
# because nobody is there to approve a prompt in headless mode. Edit to taste in wbl.toml.
DEFAULT_RESOLVE_ALLOWED = [
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(ls:*)", "Bash(python:*)", "Bash(python3:*)", "Bash(pytest:*)", "Bash(uv run:*)",
]
# Belt and braces: never let an agent publish or rewrite history, even if a rule above is widened.
DEFAULT_RESOLVE_DISALLOWED = [
    "Bash(git commit:*)", "Bash(git push:*)", "Bash(git reset:*)", "Bash(git checkout:*)",
    "Bash(git switch:*)", "Bash(git branch:*)", "Bash(git worktree:*)", "Bash(git rebase:*)",
    "Bash(rm -rf:*)",
]


@dataclass
class Config:
    root: Path
    projects: dict[str, Project]
    claude_bin: str = "claude"
    triage_model: str = "sonnet"
    resolve_model: str = "sonnet"
    worktree_root: Path = field(default_factory=lambda: Path.home() / ".wbl" / "worktrees")
    max_parallel: int = 3
    triage_budget_usd: float | None = 1.0
    resolve_budget_usd: float | None = 5.0
    resolve_allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_RESOLVE_ALLOWED))
    resolve_disallowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_RESOLVE_DISALLOWED))

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
        # `components = { name = "path", other = { repo = "path", base = "main" } }`.
        if "components" in p:
            spec = p["components"]
        elif "repo" in p:
            spec = {name: {"repo": p["repo"], "base": p.get("base", "HEAD")}}
        else:
            spec = {}  # no code at all (a "misc" folder): Prep still works, Do doesn't
        components = {}
        derived = derive_labels(name, list(spec))
        for cname, c in spec.items():
            c = {"repo": c} if isinstance(c, str) else c
            components[cname] = Component(name=cname, repo=Path(c["repo"]).expanduser().resolve(),
                                          base=c.get("base", p.get("base", "HEAD")),
                                          label=c.get("label", derived[cname]))
        name = nfc(name)
        projects[name] = Project(
            name=name,
            key=p.get("key", name[:3].upper()),
            components=components,
            folder=root / name,
        )
    cfg = Config(root=root, projects=projects)
    # Copy over any defaults that were set; unknown keys are ignored rather than fatal.
    for key in ("claude_bin", "triage_model", "resolve_model", "max_parallel",
                "triage_budget_usd", "resolve_budget_usd",
                "resolve_allowed_tools", "resolve_disallowed_tools"):
        if key in d:
            setattr(cfg, key, d[key])
    if "worktree_root" in d:
        cfg.worktree_root = Path(d["worktree_root"]).expanduser().resolve()
    # Budget 0 in the TOML means "no cap" (TOML has no null).
    for key in ("triage_budget_usd", "resolve_budget_usd"):
        if getattr(cfg, key) in (0, 0.0):
            setattr(cfg, key, None)
    cfg.claude_bin = os.environ.get("WBL_CLAUDE", cfg.claude_bin)  # lets tests swap in a stub
    return cfg


# ----------------------------------------------------------------------------------------------
# Tickets: one markdown file each, with a tiny flat frontmatter block
# ----------------------------------------------------------------------------------------------

# Frontmatter keys, in the order they are written. All values are single-line scalars, so a
# hand-rolled "key: value" parser is enough and we avoid a YAML dependency.
FIELDS = ["id", "project", "component", "title", "status", "priority", "points", "image", "created",
          "branch", "worktree", "session", "tokens", "cost_usd", "prep_tokens", "prep_cost_usd"]
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
    branch: str = ""
    worktree: str = ""
    session: str = ""        # Claude Code session id of the last resolve run (for `claude --resume`)
    tokens: int | None = None    # total tokens used by the last resolve run
    cost_usd: float | None = None
    prep_tokens: int | None = None      # what triage used, so the UI can show what runs typically take
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
        d["resume_cmd"] = (f"cd {self.worktree} && claude --resume {self.session}"
                           if self.worktree and self.session else "")
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
    after a ticket is deleted, so a branch name like wbl/SKR-7 always means one thing."""
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
# Running Claude Code headless
# ----------------------------------------------------------------------------------------------

@dataclass
class AgentResult:
    ok: bool
    text: str                    # the agent's final message
    structured: dict | None      # parsed --json-schema output, when a schema was given
    session: str
    tokens: int
    cost_usd: float
    denied: list[str] = field(default_factory=list)  # tool calls the allowlist blocked


def run_claude(cfg: Config, prompt: str, *, cwd: Path, model: str, allowed: list[str],
               disallowed: list[str] = (), permission_mode: str = "dontAsk",
               add_dirs: list[Path] = (), json_schema: dict | None = None,
               budget_usd: float | None = None, log_path: Path | None = None) -> AgentResult:
    """Run one `claude -p` session and return its parsed result.

    The prompt goes in on stdin rather than as an argument: several Claude Code flags
    (--add-dir, --allowedTools) take a variable number of values and would swallow a trailing
    positional prompt. `--output-format json` gives one JSON object at the end with the final
    text, cost, token usage and session id.
    """
    cmd = [cfg.claude_bin, "-p", "--output-format", "json", "--model", model,
           "--permission-mode", permission_mode,
           "--allowedTools", ",".join(allowed)]
    if disallowed:
        cmd += ["--disallowedTools", ",".join(disallowed)]
    for d in add_dirs:
        cmd += ["--add-dir", str(d)]
    if json_schema is not None:
        cmd += ["--json-schema", json.dumps(json_schema)]
    if budget_usd:
        cmd += ["--max-budget-usd", str(budget_usd)]

    proc = subprocess.run(cmd, input=prompt, cwd=cwd, capture_output=True, text=True)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else ""))
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        msg = (proc.stderr or proc.stdout).strip()[-2000:]
        return AgentResult(False, f"claude exited {proc.returncode}: {msg}", None, "", 0, 0.0)

    # Count every model the session touched (Claude Code may use a small model for side tasks).
    tokens = 0
    for usage in (out.get("modelUsage") or {}).values():
        tokens += sum(usage.get(k, 0) for k in
                      ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"))
    # Calls the agent tried but the allowlist refused. Worth surfacing: a recurring one means
    # resolve_allowed_tools in wbl.toml should probably include it.
    denied = []
    for d in out.get("permission_denials") or []:
        inp = d.get("tool_input") or {}
        detail = inp.get("command") or inp.get("file_path") or ""
        denied.append(f"{d.get('tool_name', '?')}: {detail.splitlines()[0][:120] if detail else ''}")
    return AgentResult(
        denied=denied,
        ok=not out.get("is_error") and out.get("subtype") == "success",
        text=out.get("result") or out.get("subtype", ""),
        structured=out.get("structured_output"),
        session=out.get("session_id", ""),
        tokens=tokens,
        cost_usd=round(float(out.get("total_cost_usd") or 0), 4),
    )


# ----------------------------------------------------------------------------------------------
# Layer 1: triage (screenshot -> spec, priority, points)
# ----------------------------------------------------------------------------------------------

def triage_schema(components: list[str]) -> dict:
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short imperative ticket title, under 80 chars"},
            "component": {"type": "string", "enum": components,
                          "description": "The repo where the fix belongs"},
            "priority": {"type": "string", "enum": list(PRIORITIES)},
            "points": {"type": "integer", "enum": list(POINTS)},
            "spec": {"type": "string", "description": "The ticket body in markdown"},
        },
        "required": ["title", "component", "priority", "points", "spec"],
    }
    if not components:  # a project with no repo
        del schema["properties"]["component"]
        schema["required"].remove("component")
    return schema

# The parts of TRIAGE_PROMPT that depend on whether the project has code to look at.
TRIAGE_WITH_REPOS = dict(
    code_context="""The project consists of these git repositories ("components"):
{components}
The current directory is the first of them. Read, Glob and Grep work in all of them;
`git log` / `git show` only in the current directory.""",
    explore="Then explore the repositories until you can point at the specific code involved.",
    component_line="""- component: the one repository where the change should be made. If it spans several, pick
  the main one and say in the spec what the others need.
""",
    relevant="## Relevant code           (file paths with line numbers, and what each does)",
)
TRIAGE_WITHOUT_REPOS = dict(
    code_context="""This project has no code repository: it collects miscellaneous tasks (accounts, hosting,
services, chores). Work from the input and the hint alone; there is no code to look at.""",
    explore="Then work out what has to be done, by whom or where, and in what order.",
    component_line="",
    relevant="## Relevant context        (services, accounts, tools or sites involved)",
)

TRIAGE_PROMPT = """\
You are triaging one item in a personal backlog for the project "{project}".
{code_context}

The only input is one file and its context:
- Input: {image}   ({kind}; open it with the Read tool and go through it carefully)
- File name hint: "{hint}"   (the author's few-word description; treat it as the main clue to intent)
- Project folder: {project}

Work out what the author wants: a bug to fix, a feature, a UI change, a wording fix, etc.
{explore} Do not change anything; you are only writing the ticket.

Return:
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
- spec: markdown with exactly these sections:
    ## What the input shows
    ## Interpretation          (what the author is asking for, and how sure you are)
    {relevant}
    ## Proposed fix            (concrete steps an agent could follow)
    ## Acceptance criteria     (checkable bullets)
    ## Estimate rationale      (one or two lines justifying priority and points)
"""


def triage_one(cfg: Config, project: Project, image: Path) -> Ticket | None:
    hint = image.stem.replace("-", " ").replace("_", " ")
    comps = [c for c in project.components.values() if c.repo.is_dir()]
    if project.components and not comps:
        print(f"  ! {project.name}/{image.name}: none of the project's repos exist; skipping")
        return None
    if comps:
        listing = "\n".join(f"- {c.name}: {c.repo}" for c in comps)
        parts = {**TRIAGE_WITH_REPOS,
                 "code_context": TRIAGE_WITH_REPOS["code_context"].format(components=listing)}
    else:
        parts = TRIAGE_WITHOUT_REPOS
    res = run_claude(
        cfg,
        TRIAGE_PROMPT.format(project=project.name, image=image, hint=hint, **parts,
                             kind={"image": "a screenshot", "text": "a text note",
                                   "pdf": "a PDF"}[input_kind(image.name)]),
        # Without a repo, the agent runs in the project folder, next to the screenshot.
        cwd=comps[0].repo if comps else project.folder,
        model=cfg.triage_model,
        # Read-only: look at the image and the code, peek at git history, nothing else.
        allowed=["Read", "Glob", "Grep", "Bash(git log:*)", "Bash(git show:*)", "Bash(ls:*)"],
        disallowed=["Edit", "Write", "NotebookEdit"],
        # The screenshot lives outside the repos, and the other components are siblings.
        add_dirs=[*([image.parent] if comps else []), *(c.repo for c in comps[1:])],
        json_schema=triage_schema([c.name for c in comps]),
        budget_usd=cfg.triage_budget_usd,
        log_path=project.state_dir / "logs" / f"triage-{image.stem}.json",
    )
    if not res.ok or not res.structured:
        print(f"  ! {project.name}/{image.name}: triage failed: {res.text[:300]}")
        return None

    s = res.structured
    t = Ticket(
        id=allocate_id(project),
        project=project.name,
        component=(s["component"] if s.get("component") in project.components else comps[0].name) if comps else "",
        title=s["title"].strip(),
        status="pending",
        priority=s["priority"] if s["priority"] in PRIORITIES else "P1",
        points=s["points"] if s["points"] in POINTS else 3,
        image=image.name,
        created=datetime.now().isoformat(timespec="minutes"),
        prep_tokens=res.tokens,
        prep_cost_usd=res.cost_usd,
        body=s["spec"],
    )
    t.path = project.state_dir / f"{t.id}.md"
    t.save()
    # Rename the triage log to the ticket ID so `done` can find and remove it.
    log = project.state_dir / "logs" / f"triage-{image.stem}.json"
    if log.exists():
        log.rename(project.state_dir / "logs" / f"{t.id}-triage.json")
    where = f" ({t.component})" if len(project.components) > 1 else ""
    print(f"  + {t.id}{where} [{t.priority} · {t.points}pt] {t.title}  (${res.cost_usd:.2f})")
    return t


def cmd_triage(cfg: Config, args: argparse.Namespace) -> None:
    """Triage specific screenshots, given as "<project>/<file name>". Launched by the server."""
    wanted = {nfc(i) for i in args.images}
    items = [(p, f) for p, f in inbox(cfg) if nfc(f"{p.name}/{f.name}") in wanted]
    missing = wanted - {nfc(f"{p.name}/{f.name}") for p, f in items}
    if missing:
        raise WblError(f"not in the inbox: {', '.join(sorted(missing))}")
    print(f"triaging {len(items)} screenshot(s), {cfg.max_parallel} at a time")
    with ThreadPoolExecutor(max_workers=cfg.max_parallel) as pool:
        list(pool.map(lambda pf: triage_one(cfg, *pf), items))


# ----------------------------------------------------------------------------------------------
# Layer 2: resolve (spec -> branch with a proposed fix)
# ----------------------------------------------------------------------------------------------

RESOLVE_PROMPT = """\
You are implementing ticket {id} in the project "{project}".
The current directory is a git worktree on branch {branch}, made just for this ticket.
Stay inside it. Do not commit, push, or switch branches: the author reviews your changes
uncommitted and commits them himself.

The original input (a screenshot, note or PDF) is at {image} if you want to look at it.

# {title}

{spec}

Instructions:
1. Re-check the relevant code; the spec was written by a quick read-only pass and may be off.
2. Make the change. Keep it focused on this ticket; match the surrounding code style and
   document what you add.
3. If the repo has tests, run the relevant ones, and add a test when the fix is testable.
4. Finish with a short report in markdown: what you changed (files), how you verified it, and
   anything left undone or uncertain. That report is appended to the ticket verbatim.
"""


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


def prepare_worktree(cfg: Config, t: Ticket) -> None:
    """Give the ticket its own branch wbl/<ID> checked out in its own worktree.

    Worktrees (rather than branches in the main checkout) are what make parallel agents safe:
    each one has a separate directory, so two agents on the same repo never see each other's
    half-done edits, and your own checkout is never touched. Re-running resolve on a ticket
    reuses its existing worktree, so a second attempt builds on the first.
    """
    c = ticket_component(cfg, t)
    branch = f"wbl/{t.id}"
    wt = cfg.worktree_root / t.project / t.id
    if not wt.exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        has_branch = git(c.repo, "rev-parse", "--verify", "--quiet", branch, check=False).returncode == 0
        args = ["worktree", "add", str(wt), branch] if has_branch else \
               ["worktree", "add", "-b", branch, str(wt), c.base]
        r = git(c.repo, *args, check=False)
        if r.returncode:
            raise WblError(f"{t.id}: git worktree add failed: {r.stderr.strip()}")
    t.branch, t.worktree = branch, str(wt)
    t.component = c.name  # pin it, so a later config change can't point the ticket elsewhere


def resolve_one(cfg: Config, t: Ticket) -> Ticket:
    p = cfg.project(t.project)
    name = p.name if len(p.components) == 1 else f"{p.name}/{t.component}"
    res = run_claude(
        cfg,
        RESOLVE_PROMPT.format(id=t.id, project=name, branch=t.branch, title=t.title,
                              spec=t.body, image=p.folder / t.image),
        cwd=Path(t.worktree),
        model=cfg.resolve_model,
        allowed=cfg.resolve_allowed_tools,
        disallowed=cfg.resolve_disallowed_tools,
        permission_mode="acceptEdits",  # file edits inside the worktree go through without prompting
        add_dirs=[p.folder],
        budget_usd=cfg.resolve_budget_usd,
        log_path=p.state_dir / "logs" / f"{t.id}-resolve.json",
    )
    # Re-read from disk before writing: you may have changed priority/points in the UI meanwhile.
    fresh = Ticket.load(t.path)
    fresh.branch, fresh.worktree = t.branch, t.worktree
    fresh.status = "review" if res.ok else "failed"
    fresh.session = res.session or fresh.session
    fresh.tokens, fresh.cost_usd = res.tokens, res.cost_usd
    stamp = datetime.now().isoformat(timespec="minutes")
    fresh.body += (f"\n\n## Resolution attempt — {stamp} ({fresh.status}, "
                   f"{res.tokens:,} tokens, ${res.cost_usd:.2f})\n\n{res.text.strip()}")
    if res.denied:
        fresh.body += "\n\n**Blocked tool calls** (consider widening `resolve_allowed_tools`):\n" + \
                      "\n".join(f"- `{d}`" for d in res.denied)
    fresh.save()
    print(f"  {'✓' if res.ok else '✗'} {t.id} -> {fresh.status}  ({res.tokens:,} tok, ${res.cost_usd:.2f})")
    return fresh


def cmd_resolve(cfg: Config, args: argparse.Namespace) -> None:
    """Resolve the given tickets in parallel. Launched by the server."""
    tickets = [get_ticket(cfg, i) for i in args.ids]
    for t in tickets:
        if t.status == "in_progress":
            raise WblError(f"{t.id} is already in progress")

    # Worktree creation is done one at a time: concurrent `git worktree add` on the same repo
    # can trip over git's lock files. The agents themselves then run in parallel.
    for t in tickets:
        prepare_worktree(cfg, t)
        t.status = "in_progress"
        t.save()
    print(f"resolving {len(tickets)} ticket(s), {cfg.max_parallel} at a time")
    with ThreadPoolExecutor(max_workers=cfg.max_parallel) as pool:
        list(pool.map(lambda t: resolve_one(cfg, t), tickets))


# ----------------------------------------------------------------------------------------------
# Finishing: cleanup
# ----------------------------------------------------------------------------------------------

def worktree_changes(cfg: Config, t: Ticket) -> str:
    """Everything the ticket's branch adds on top of its base: commits (if you made any) plus
    the uncommitted edits the agent left, as `git status` and a diff. Used for review."""
    wt = Path(t.worktree) if t.worktree else None
    if not wt or not wt.exists():
        return ""
    c = ticket_component(cfg, t)
    base = git(c.repo, "rev-parse", c.base, check=False).stdout.strip()
    status = git(wt, "status", "--short", check=False).stdout
    commits = git(wt, "log", "--oneline", f"{base}..HEAD", check=False).stdout if base else ""
    diff = git(wt, "diff", base or "HEAD", check=False).stdout  # base -> working tree (tracked files)
    # New files the agent created don't appear in `git diff` until added. Rather than touch the
    # worktree's index, diff each one against /dev/null (respecting .gitignore).
    untracked = git(wt, "ls-files", "--others", "--exclude-standard", check=False).stdout.split("\n")
    for f in filter(None, untracked):
        diff += git(wt, "diff", "--no-index", "--", "/dev/null", f, check=False).stdout
    out = status
    if commits.strip():
        out += "\n# commits on branch\n" + commits
    return (out + "\n" + diff)[:300_000]


def finish_ticket(cfg: Config, t: Ticket, force: bool = False, keep_image: bool = False) -> list[str]:
    """Remove every trace of a ticket: worktree, branch (if merged), screenshot, ticket, logs.

    `keep_image` is "discard": the ticket goes, but the screenshot stays and so drops back into
    the inbox, ready to be prepped again (say, after renaming it).

    Safety rails, which `force` overrides (use it for won't-fix / throw-away):
      * a ticket whose agent is still running is left alone;
      * a worktree with uncommitted changes is not removed, since that work would be lost;
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

    doomed = [t.path, *p.state_dir.glob(f"logs/{t.id}-*")]
    if not keep_image:
        doomed += [p.folder / t.image, p.state_dir / "thumbs" / f"{t.image}.png"]
    for f in doomed:
        if f and f.exists():
            f.unlink()
    notes.append("deleted the ticket file" + ("" if keep_image else f" and {t.image}"))
    return notes


def set_component(cfg: Config, t: Ticket, name: str) -> None:
    """Move a ticket to another of its project's repos (triage guessed wrong). Only before
    resolve: once a worktree exists, its branch lives in the old repo."""
    c = cfg.project(t.project).component(name)
    if t.worktree and c.name != t.component:
        raise WblError(f"{t.id} already has a worktree in {t.component}; finish it first")
    t.component = c.name


# ----------------------------------------------------------------------------------------------
# Entry point. Everything is driven from the browser UI (`make run`); `triage` and `resolve`
# are what the server launches in the background, so agents outlive a server restart.
# ----------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="wbl", description=__doc__.strip().splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="backlog folder holding wbl.toml (default: search upward, or $WBL_ROOT)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="the board, in the browser")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="open a browser tab")

    s = sub.add_parser("triage", help="(used by the server) turn screenshots into tickets")
    s.add_argument("images", nargs="+", help="PROJECT/FILE")

    s = sub.add_parser("resolve", help="(used by the server) send agents at tickets")
    s.add_argument("ids", nargs="+")
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
            {"triage": cmd_triage, "resolve": cmd_resolve}[args.cmd](cfg, args)
    except WblError as e:
        print(f"wbl: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
