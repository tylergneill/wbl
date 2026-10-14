"""
wbl_server — the local browser front end for the WayBackLog Machine (`wbl serve`).

A small JSON API over the same ticket files the CLI uses, plus one static page (ui.html).
The page polls /api/state every few seconds, so tickets moved by CLI runs, agents, or edits
in another tab show up on their own.

Long-running work (triage, resolve) is not done inside the server: the API starts the ordinary
CLI as a background subprocess and logs its output. That keeps one code path for everything,
and a server restart never kills an agent mid-run.

Security: it binds to 127.0.0.1 only. Write endpoints insist on `Content-Type: application/json`,
which a web page on another origin cannot send to us without a CORS preflight that this server
never approves, so a random website open in your browser cannot drive it.
"""

from __future__ import annotations

import json
import mimetypes
import os
import signal
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import wbl

UI_FILE = Path(__file__).resolve().parent / "ui.html"


class Jobs:
    """Background CLI runs started from the UI ("triage inbox", "resolve selected")."""

    def __init__(self, cfg: wbl.Config):
        self.cfg = cfg
        self.log_dir = cfg.worktree_root.parent / "jobs"
        self.items: list[dict] = []
        self.next_id = 1
        self.lock = threading.Lock()

    def start(self, kind: str, cli_args: list[str], claims: set[str] = frozenset()) -> dict:
        """`claims` (triage only) is the set of "project/image" items the run will take.
        Two runs claiming the same screenshot would both ticket it."""
        with self.lock:
            for j in self.items:
                if j["proc"].poll() is None and claims & j["claims"]:
                    raise wbl.WblError("some of those are already being worked on")
            self.log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            log = self.log_dir / f"{stamp}-{kind}-{self.next_id}.log"
            cmd = [sys.executable, str(Path(wbl.__file__).resolve()),
                   "--root", str(self.cfg.root), kind, *cli_args]
            # Its own session (and process group), so it survives a server restart and can be
            # cancelled as a group, agents included.
            proc = subprocess.Popen(cmd, stdout=open(log, "w"), stderr=subprocess.STDOUT,
                                    start_new_session=True)
            job = {"id": self.next_id, "kind": kind, "args": cli_args, "started": stamp,
                   "log": log, "proc": proc, "claims": claims, "cancelled": False}
            self.next_id += 1
            self.items = (self.items + [job])[-10:]
            return self.describe(job)

    def cancel(self, job_id: int) -> dict:
        with self.lock:
            job = next((j for j in self.items if j["id"] == job_id), None)
            if job is None:
                raise wbl.WblError(f"no job {job_id}")
            if job["proc"].poll() is None:
                try:
                    os.killpg(job["proc"].pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                job["proc"].wait(timeout=10)
                job["cancelled"] = True
        if job["kind"] == "resolve":
            # The killed run never got to record a result; don't leave its tickets stuck.
            for i in job["args"]:
                try:
                    t = wbl.get_ticket(self.cfg, i)
                except wbl.WblError:
                    continue
                if t.status == "in_progress":
                    t.status = "failed"
                    t.body += "\n\n## Resolution attempt cancelled from the UI"
                    t.save()
        return self.describe(job)

    @staticmethod
    def describe(job: dict) -> dict:
        code = job["proc"].poll()
        tail = job["log"].read_text()[-1500:] if job["log"].exists() else ""
        return {"id": job["id"], "kind": job["kind"], "args": job["args"], "started": job["started"],
                "running": code is None, "exit_code": code, "cancelled": job["cancelled"], "tail": tail}

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [self.describe(j) for j in self.items]


def make_handler(cfg: wbl.Config, jobs: Jobs):
    def describe_ticket(t: wbl.Ticket) -> dict:
        f = cfg.project(t.project).folder / t.image
        d = t.to_dict()
        d["image_missing"] = not f.exists()  # renamed or deleted after Prep
        d["excerpt"] = wbl.text_excerpt(f) if d["kind"] == "text" and f.exists() else ""
        return d

    class Handler(BaseHTTPRequestHandler):
        # --- plumbing ---------------------------------------------------------------------

        def log_message(self, fmt, *args):  # keep the terminal quiet; polling is noisy
            pass

        def send_json(self, obj, status: int = 200) -> None:
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def send_file(self, path: Path, ctype: str | None = None) -> None:
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def read_json(self) -> dict:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise PermissionError("expected application/json")
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def route(self, method: str) -> None:
            parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/") if p]
            try:
                self.dispatch(method, parts)
            except wbl.WblError as e:
                self.send_json({"error": str(e)}, 400)
            except PermissionError as e:
                self.send_json({"error": str(e)}, 403)
            except FileNotFoundError:
                self.send_json({"error": "not found"}, 404)

        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

        # --- endpoints --------------------------------------------------------------------

        def dispatch(self, method: str, parts: list[str]) -> None:
            if method == "GET" and not parts:
                return self.send_file(UI_FILE, "text/html; charset=utf-8")

            if method == "GET" and parts == ["api", "state"]:
                tickets = wbl.load_tickets(cfg)
                return self.send_json({
                    "root": str(cfg.root),
                    "projects": [{"name": p.name, "key": p.key, "components": list(p.components),
                                  "labels": {c.name: c.label for c in p.components.values()}}
                                 for p in cfg.projects.values()],
                    # image_missing: the screenshot was renamed or deleted after Prep.
                    "tickets": [describe_ticket(t) for t in tickets],
                    "inbox": [{"project": p.name, "image": f.name, "image_url": f"/img/{p.name}/{f.name}",
                               "thumb_url": f"/thumb/{p.name}/{f.name}", "kind": wbl.input_kind(f.name),
                               "excerpt": wbl.text_excerpt(f) if wbl.input_kind(f.name) == "text" else ""}
                              for p, f in wbl.inbox(cfg)],
                    "jobs": jobs.snapshot(),
                    "priorities": wbl.PRIORITIES, "points": wbl.POINTS, "statuses": wbl.STATUSES,
                    # So the UI can say what a click may spend before it starts any agents: the
                    # per-run limit, plus the token counts past runs on current tickets actually used.
                    "triage_budget_usd": cfg.triage_budget_usd, "resolve_budget_usd": cfg.resolve_budget_usd,
                    "triage_tokens": [t.prep_tokens for t in tickets if t.prep_tokens],
                    "resolve_tokens": [t.tokens for t in tickets if t.tokens],
                    "max_parallel": cfg.max_parallel,
                })

            if method == "GET" and len(parts) == 3 and parts[0] in ("img", "thumb"):
                # /img/ serves an input file as is; /thumb/ serves its preview image (a PDF's
                # first page; an image is its own preview). Both refuse anything that isn't an
                # input file directly inside a configured project folder (no ../ tricks).
                p = cfg.project(parts[1])
                f = (p.folder / parts[2]).resolve()
                if f.parent != p.folder.resolve() or f.suffix.lower() not in wbl.INPUT_EXTS:
                    raise PermissionError("not a project input")
                kind = wbl.input_kind(f.name)
                if parts[0] == "img":
                    return self.send_file(f, "text/plain; charset=utf-8" if kind == "text" else None)
                if kind == "image":
                    return self.send_file(f)
                thumb = wbl.pdf_thumbnail(p.state_dir, f) if kind == "pdf" and f.exists() else None
                if thumb is None:
                    raise FileNotFoundError
                return self.send_file(thumb, "image/png")

            if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "tickets"] and parts[3] == "diff":
                return self.send_json({"diff": wbl.worktree_changes(cfg, wbl.get_ticket(cfg, parts[2]))})

            if method == "POST" and len(parts) == 3 and parts[:2] == ["api", "tickets"]:
                body = self.read_json()
                t = wbl.get_ticket(cfg, parts[2])
                if "component" in body:
                    wbl.set_component(cfg, t, str(body["component"]))
                if "priority" in body:
                    if body["priority"] not in wbl.PRIORITIES:
                        raise wbl.WblError("bad priority")
                    t.priority = body["priority"]
                if "points" in body:
                    if int(body["points"]) not in wbl.POINTS:
                        raise wbl.WblError("bad points")
                    t.points = int(body["points"])
                if "status" in body:
                    if body["status"] not in wbl.STATUSES:
                        raise wbl.WblError("bad status")
                    t.status = body["status"]
                if body.get("title", "").strip():
                    t.title = body["title"].strip()
                t.save()
                return self.send_json(t.to_dict())

            if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "tickets"] and parts[3] in ("done", "discard"):
                # "done" deletes the screenshot too; "discard" keeps it, so it returns to the inbox.
                body = self.read_json()
                notes = wbl.finish_ticket(cfg, wbl.get_ticket(cfg, parts[2]), force=bool(body.get("force")),
                                          keep_image=parts[3] == "discard")
                return self.send_json({"notes": notes})

            if method == "POST" and parts == ["api", "triage"]:
                # {"images": ["project/file", ...]}
                images = [str(i) for i in self.read_json().get("images", [])]
                if not images:
                    raise wbl.WblError("no screenshots selected")
                return self.send_json(jobs.start("triage", images, claims={wbl.nfc(i) for i in images}))

            if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
                self.read_json()  # content-type check: this is a write
                if not parts[2].isdigit():
                    raise wbl.WblError("bad job id")
                return self.send_json(jobs.cancel(int(parts[2])))

            if method == "POST" and parts == ["api", "resolve"]:
                ids = [str(i) for i in self.read_json().get("ids", [])]
                if not ids:
                    raise wbl.WblError("no tickets selected")
                for i in ids:
                    t = wbl.get_ticket(cfg, i)  # fail fast on a bad ID instead of in the background
                    if t.status == "in_progress":
                        raise wbl.WblError(f"{t.id} is already in progress")
                    wbl.ticket_component(cfg, t)  # a project without a repo has nothing to Do
                return self.send_json(jobs.start("resolve", ids, claims={i.upper() for i in ids}))

            raise FileNotFoundError

    return Handler


def serve(cfg: wbl.Config, port: int = 8765, open_browser: bool = False) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg, Jobs(cfg)))
    url = f"http://127.0.0.1:{port}/"
    print(f"wbl: serving {cfg.root} at {url}  (Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
