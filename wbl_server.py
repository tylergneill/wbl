"""
wbl_server — the local browser front end for the WayBackLog Machine (`wbl serve`).

A small JSON API over the same ticket files the CLI uses, plus one static page (ui.html).
The page polls /api/state every few seconds, so tickets changed by agents (through their hooks)
or edits in another tab show up on their own.

Agents never run inside the server: Prep and Do open a terminal window running `wbl session …`,
an ordinary Claude Code session. That keeps one code path, and a server restart never kills an
agent mid-run.

Security: it binds to 127.0.0.1 only. Write endpoints insist on `Content-Type: application/json`,
which a web page on another origin cannot send to us without a CORS preflight that this server
never approves, so a random website open in your browser cannot drive it.
"""

from __future__ import annotations

import json
import mimetypes
import shlex
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import wbl

UI_FILE = Path(__file__).resolve().parent / "ui.html"


def export_markdown(tickets: list[wbl.Ticket]) -> str:
    """Tickets as plain markdown, for pasting or saving elsewhere: no frontmatter."""
    return "\n\n---\n\n".join(
        f"# {t.title}\n\n{t.id} · {t.project} · {t.priority} · {t.points} pt\n\n{t.body.strip()}\n"
        for t in tickets)


def make_handler(cfg: wbl.Config):
    def describe_ticket(t: wbl.Ticket) -> dict:
        f = cfg.project(t.project).folder / t.image
        d = t.to_dict()
        if t.component or len(cfg.project(t.project).components) == 1:
            where = wbl.workdir(cfg, t)
            d["workdir"] = str(where)
            d["resume_cmd"] = f"cd {shlex.quote(str(where))} && claude --resume {t.session}" if t.session else ""
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
            url = urlparse(self.path)
            parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
            try:
                self.dispatch(method, parts, parse_qs(url.query))
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

        def dispatch(self, method: str, parts: list[str], query: dict) -> None:
            if method == "GET" and not parts:
                return self.send_file(UI_FILE, "text/html; charset=utf-8")

            if method == "GET" and parts == ["api", "state"]:
                tickets = wbl.load_tickets(cfg)
                busy = wbl.prepping(cfg)
                return self.send_json({
                    "root": str(cfg.root),
                    "projects": [{"name": p.name, "key": p.key, "components": list(p.components),
                                  "labels": {c.name: c.label for c in p.components.values()}}
                                 for p in cfg.projects.values()],
                    # image_missing: the screenshot was renamed or deleted after Prep.
                    "tickets": [describe_ticket(t) for t in tickets],
                    "inbox": [{"project": p.name, "image": f.name, "image_url": f"/img/{p.name}/{f.name}",
                               "thumb_url": f"/thumb/{p.name}/{f.name}", "kind": wbl.input_kind(f.name),
                               "excerpt": wbl.text_excerpt(f) if wbl.input_kind(f.name) == "text" else "",
                               "prepping": wbl.nfc(f"{p.name}/{f.name}") in busy}
                              for p, f in wbl.inbox(cfg)],
                    "priorities": wbl.PRIORITIES, "points": wbl.POINTS, "statuses": wbl.STATUSES,
                    # So the UI can say what a click may spend before it starts any agents: the
                    # token counts past sessions on current tickets actually used.
                    "triage_tokens": [t.prep_tokens for t in tickets if t.prep_tokens],
                    "resolve_tokens": [t.tokens for t in tickets if t.tokens],
                    "terminal": cfg.terminal if isinstance(cfg.terminal, str) else "your terminal",
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

            if method == "GET" and parts == ["api", "export"]:
                # ?ids=SKR-1,SKR-2 -> one markdown file, downloaded.
                ids = [i for v in query.get("ids", []) for i in v.split(",") if i]
                if not ids:
                    raise wbl.WblError("no tickets selected")
                data = export_markdown([wbl.get_ticket(cfg, i) for i in ids]).encode()
                name = f"{ids[0].upper()}.md" if len(ids) == 1 else "tickets.md"
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

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
                if str(body.get("title", "")).strip():
                    t.title = str(body["title"]).strip()
                if "body" in body:
                    t.body = str(body["body"])
                t.save()
                return self.send_json(t.to_dict())

            if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "tickets"] and parts[3] in ("done", "discard"):
                # "done" deletes the screenshot too; "discard" keeps it, so it returns to the inbox.
                body = self.read_json()
                notes = wbl.finish_ticket(cfg, wbl.get_ticket(cfg, parts[2]), force=bool(body.get("force")),
                                          keep_image=parts[3] == "discard")
                return self.send_json({"notes": notes})

            if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "tickets"] and parts[3] == "continue":
                # Reopen the ticket's last Do session in a terminal window.
                self.read_json()  # content-type check: this starts an agent
                wbl.start_do(cfg, [parts[2]], resume=True)
                return self.send_json({"opened": [parts[2].upper()]})

            if method == "POST" and parts == ["api", "triage"]:
                # {"images": ["project/file", ...]}: one Prep session window each.
                images = [str(i) for i in self.read_json().get("images", [])]
                if not images:
                    raise wbl.WblError("no screenshots selected")
                wbl.start_prep(cfg, images)
                return self.send_json({"opened": images})

            if method == "POST" and parts == ["api", "unprep"]:
                # {"image": "project/file"}: forget a Prep whose window died without telling us.
                wbl.release_prep(cfg, str(self.read_json().get("image", "")))
                return self.send_json({})

            if method == "POST" and parts == ["api", "resolve"]:
                # {"ids": [...]}: one Do session window each.
                ids = [str(i) for i in self.read_json().get("ids", [])]
                if not ids:
                    raise wbl.WblError("no tickets selected")
                wbl.start_do(cfg, ids)
                return self.send_json({"opened": [i.upper() for i in ids]})

            raise FileNotFoundError

    return Handler


def serve(cfg: wbl.Config, port: int = 8765, open_browser: bool = False) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg))
    url = f"http://127.0.0.1:{port}/"
    print(f"wbl: serving {cfg.root} at {url}  (Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
