# /// script
# dependencies = ["markdown", "pygments"]
# ///
"""Serve big-plan markdown files as commented HTML, intended for Tailscale.

Usage:
    uv run server.py [ROOT_DIR] [--port 8765] [--host 0.0.0.0]

Endpoints:
    GET  /                                  index of *.md files under ROOT_DIR
    GET  /<rel/path.md>                     render that markdown as HTML
    GET  /raw/<rel/path.md>                 raw markdown
    GET  /pdf/<rel/path.md>                 render and download a PDF
    GET  /api/comments/<rel/path.md>        list comments for a file
    POST /api/promote/<rel/path.md>         add the file to the index
    POST /api/comments/<rel/path.md>        add a typed comment (see ALLOWED_TYPES)
    POST /api/comments/<rel/path.md>/<id>/delete   delete a comment
    POST /api/comments/<rel/path.md>/<id>/resolve  mark resolved (legacy; UI uses /delete)
    POST /api/comments/<rel/path.md>/<id>/reply    reply in a comment's thread
                                            body: {"text": str, "role": "agent"|"reviewer",
                                                   "author": str}
    POST /api/task/<rel/path.md>            flip a task checkbox by line number
                                            body: {"line": int (1-based), "checked": bool}
    GET  /api/submit/<rel/path.md>          preview where a submit would go (no side effects)
    POST /api/submit/<rel/path.md>          send open feedback to the authoring session
                                            body: {"prompt": str}
    POST /api/session/<rel/path.md>         record which session authored the plan
                                            body: {engine: claude|codex|grok, sessionId, name, cwd}
    GET  /<rel/path.md>?view=diff           collapsed unified diff (baseline -> current)
    GET  /<rel/path.md>?view=raw            the plan source as preformatted markdown
    POST /api/snapshot/<rel/path.md>        set the diff baseline (copy current .md -> .md.snapshot)
    POST /api/snapshot/<rel/path.md>/clear  delete the baseline (doc renders clean again)
    POST /api/snapshot/<rel/path.md>/accept accept one hunk into the baseline (splice its new lines)
                                            body: {old_start, old_end, old_b64, new_b64}
    GET  /assets/{style.css,app.js}         static

There is no auth. Bind only to a trusted network (Tailscale on 0.0.0.0 is the intent).
Do not expose to the public internet.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

import dispatch
import render


_TASK_LINE_RE = re.compile(r"^(?P<lead>[ ]*[-*+]\s+)\[(?P<state>[ xX])\](?P<rest>\s+.*)$")
INDEX_PAGE_SIZE = 20


def flip_task_line(md_path: Path, line_no: int, checked: bool) -> bool:
    """Flip `- [ ]` <-> `- [x]` on line_no (1-based). Returns True on success.

    Returns False if line_no is out of range or the line is not a task line.
    Atomic write via tmp file.
    """
    text = md_path.read_text()
    # splitlines(keepends=True) preserves the original newline characters per
    # line, so we can rewrite a single line without touching the others'
    # endings (CRLF vs LF).
    lines = text.splitlines(keepends=True)
    if line_no < 1 or line_no > len(lines):
        return False
    raw = lines[line_no - 1]
    # Strip the trailing newline for the regex, remember it for rewrite.
    if raw.endswith("\r\n"):
        body, eol = raw[:-2], "\r\n"
    elif raw.endswith("\n"):
        body, eol = raw[:-1], "\n"
    else:
        body, eol = raw, ""
    m = _TASK_LINE_RE.match(body)
    if not m:
        return False
    new_marker = "[x]" if checked else "[ ]"
    lines[line_no - 1] = f"{m.group('lead')}{new_marker}{m.group('rest')}{eol}"
    tmp = md_path.with_suffix(md_path.suffix + ".tmp")
    tmp.write_text("".join(lines))
    tmp.replace(md_path)
    return True

ROOT_DIR: Path = Path(".").resolve()
ASSETS_DIR: Path = Path(__file__).parent / "assets"
INDEX_FILTER: str = "plans"  # plans | readmes | all

ALLOWED_ASSETS: frozenset[str] = frozenset({"style.css", "app.js", "diff.js", "mermaid.min.js"})
ALLOWED_TYPES: frozenset[str] = frozenset({"text", "reaction", "decision", "status"})
# A reply is not a comment type: it nests under its parent comment, so every
# existing filter (the rail, the unresolved badge, the decision/status
# replacement passes) keeps working untouched.
ALLOWED_REPLY_ROLES: frozenset[str] = frozenset({"agent", "reviewer"})
# Bounds one reply so a runaway agent cannot grow the sidecar without limit.
# A real answer is a paragraph or two.
MAX_REPLY_BYTES = 32 * 1024
ALLOWED_REACTIONS: frozenset[str] = frozenset({"\U0001f44d", "\U0001f44e", "\U0001f914"})  # 👍 👎 🤔


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# --- Host allowlist (DNS-rebinding defence for state-changing requests) -------
#
# Comparing Origin to Host is not enough on its own. An attacker who serves a
# page from their own :8765 and then rebinds their hostname to this machine gets
# matching Origin and Host and a Sec-Fetch-Site of same-origin, so the page ends
# up talking to us as if it were ours -- and /api/submit spawns an agent. The
# standard fix is to trust only hostnames that are actually ours, which is why
# dev servers ship an allowedHosts setting. Applied to POST only: reading a plan
# from an unexpected hostname or a bare IP should keep working.
_ALLOWED_HOSTS: set[str] = set()
_ALLOWED_HOSTS_AT: float = 0.0
_ALLOWED_HOSTS_LOCK = threading.Lock()
_REFRESH_EVERY = 60.0


def _split_host(host_header: str) -> str:
    """Authority -> bare hostname, lowercased. Handles [::1]:8765 and host:port."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host.lstrip("[")
    head, sep, tail = host.rpartition(":")
    return head if sep and tail.isdigit() else host


def _static_hosts() -> set[str]:
    hosts = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    try:
        name = socket.gethostname().lower()
        hosts |= {name, f"{name}.local"}
    except OSError:
        pass
    for extra in os.environ.get("BIG_PLAN_ALLOWED_HOSTS", "").split(","):
        if extra.strip():
            hosts.add(extra.strip().lower())
    return hosts


def _tailscale_hosts() -> set[str]:
    """This node's MagicDNS name and tailnet IPs, or empty if tailscale is down."""
    try:
        import subprocess
        out = subprocess.run(
            ["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5
        )
        if out.returncode != 0:
            return set()
        self_node = json.loads(out.stdout).get("Self", {})
    except Exception:
        return set()
    hosts = set()
    dns = str(self_node.get("DNSName", "")).rstrip(".").lower()
    if dns:
        hosts.add(dns)
        hosts.add(dns.split(".", 1)[0])  # bare MagicDNS short name
    for ip in self_node.get("TailscaleIPs") or []:
        hosts.add(str(ip).lower())
    return hosts


def refresh_allowed_hosts() -> set[str]:
    global _ALLOWED_HOSTS, _ALLOWED_HOSTS_AT
    with _ALLOWED_HOSTS_LOCK:
        _ALLOWED_HOSTS = _static_hosts() | _tailscale_hosts()
        _ALLOWED_HOSTS_AT = time.monotonic()
        return set(_ALLOWED_HOSTS)


def host_allowed(host_header: str) -> bool:
    """True if this Host is one of ours.

    A miss re-reads tailscale once a minute before rejecting, so a service that
    started before tailscaled heals on first use instead of 403ing until the
    next restart.
    """
    host = _split_host(host_header)
    if not host:
        return False
    if host in _ALLOWED_HOSTS:
        return True
    if time.monotonic() - _ALLOWED_HOSTS_AT < _REFRESH_EVERY:
        return False
    return host in refresh_allowed_hosts()


def safe_join(root: Path, rel: str) -> Path | None:
    """Join rel under root, refusing escape via .. or absolute paths."""
    rel = rel.lstrip("/")
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def is_plan_file(p: Path) -> bool:
    """Heuristic: plans live under .projects/ OR end in .plan.md OR are named PLAN.md."""
    parts = p.parts
    if ".projects" in parts:
        return True
    name_lower = p.name.lower()
    if name_lower.endswith(".plan.md"):
        return True
    if name_lower == "plan.md":
        return True
    return False


def promotion_path(md_path: Path) -> Path:
    """Sidecar whose presence explicitly publishes a Markdown file to the index."""
    return md_path.with_suffix(md_path.suffix + ".big-plan")


def is_promoted(md_path: Path) -> bool:
    # Existing provenance registrations were already an explicit authoring
    # handoff, so retain those plans during the marker rollout.
    provenance = md_path.with_suffix(md_path.suffix + ".session")
    return promotion_path(md_path).is_file() or provenance.is_file()


def promote(md_path: Path) -> None:
    """Atomically mark a Markdown file as available in the big-plan index."""
    marker = promotion_path(md_path)
    tmp = marker.with_suffix(marker.suffix + ".tmp")
    tmp.write_text("published by big-plan\n")
    tmp.replace(marker)


def is_readme(p: Path) -> bool:
    return p.name.lower() in {"readme.md", "readme.markdown"}


def _ignore_index_directory(name: str) -> bool:
    """Return whether an index walk must prune this directory entirely."""
    return (
        name.startswith(".git")
        or name in {"node_modules", ".claude", ".venv", "venv"}
    )


def list_markdown(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory, names, filenames in os.walk(root):
        # Prune ignored trees before descending. Filtering paths produced by
        # Path.rglob still traverses every node_modules and .git worktree first,
        # which made the index take more than ten seconds and retain hundreds
        # of megabytes on a large development checkout.
        names[:] = [name for name in names if not _ignore_index_directory(name)]
        parent = Path(directory)
        for filename in filenames:
            if not filename.endswith(".md"):
                continue
            p = parent / filename
            # A .projects directory is a workspace, not an automatic
            # publication channel. Keep drafts out of the shared index until
            # their author has explicitly promoted them.
            if not is_promoted(p):
                continue

            # Filter mode
            if INDEX_FILTER == "all":
                files.append(p)
            elif INDEX_FILTER == "plans":
                if is_plan_file(p):
                    files.append(p)
            elif INDEX_FILTER == "readmes":
                if is_plan_file(p) or is_readme(p):
                    files.append(p)
            else:
                files.append(p)
    # The index is a recent-work queue, rather than a filesystem browser.
    # Newest first makes the handful of plans the reviewer is actively reviewing
    # immediately reachable; pagination keeps the rendered page bounded.
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return files


def render_index(root: Path, page: int = 1) -> bytes:
    files = list_markdown(root)
    total_files = len(files)
    total_pages = max(1, (total_files + INDEX_PAGE_SIZE - 1) // INDEX_PAGE_SIZE)
    page = min(max(page, 1), total_pages)
    first = (page - 1) * INDEX_PAGE_SIZE
    page_files = files[first:first + INDEX_PAGE_SIZE]
    rows = []
    for f in page_files:
        rel = f.relative_to(root)
        sidecar = f.with_suffix(f.suffix + ".comments.json")
        n_comments = 0
        n_answered = 0
        if sidecar.exists():
            try:
                data = json.loads(sidecar.read_text())
                open_comments = [
                    c for c in data.get("comments", []) if not c.get("resolved")
                ]
                n_comments = len(open_comments)
                n_answered = sum(1 for c in open_comments if render.is_answered(c))
            except json.JSONDecodeError:
                pass
        stat = f.stat()
        size_kb = max(1, stat.st_size // 1024)
        updated = dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        badge = f'<span class="badge">{n_comments}</span>' if n_comments else ""
        # Split out because "3 open" and "3 open, 2 of them already answered and
        # waiting on you" are different asks from the index.
        if n_answered:
            badge += f'<span class="badge answered">{n_answered} answered</span>'
        rows.append(
            f'<li><a href="/{html.escape(str(rel))}">{html.escape(str(rel))}</a>'
            f' <span class="size">{size_kb}K</span>'
            f' <time class="updated" datetime="{dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc).isoformat()}">{updated}</time> {badge}</li>'
        )

    range_label = "(no markdown files found)" if not total_files else f"Showing {first + 1}\u2013{first + len(page_files)} of {total_files} newest first"
    previous = f'<a class="page-link" href="/?page={page - 1}">Previous</a>' if page > 1 else '<span class="page-link disabled">Previous</span>'
    next_page = f'<a class="page-link" href="/?page={page + 1}">Next</a>' if page < total_pages else '<span class="page-link disabled">Next</span>'

    body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>big-plan: {html.escape(str(root))}</title>
<link rel="stylesheet" href="/assets/style.css?v={render._asset_version()}">
</head><body class="index">
<header class="topbar"><h1 class="page-title">Plans</h1>
<span class="root-path">{html.escape(str(root))}</span></header>
<main class="content"><p class="index-summary">{range_label}</p>
<ul class="plan-list">{''.join(rows) if rows else '<li>(no markdown files found)</li>'}</ul>
<nav class="pagination" aria-label="Plan pages">{previous}<span>Page {page} of {total_pages}</span>{next_page}</nav></main>
</body></html>"""
    return body.encode("utf-8")


def load_sidecar(md_path: Path) -> dict:
    sidecar = md_path.with_suffix(md_path.suffix + ".comments.json")
    if not sidecar.exists():
        return {"comments": []}
    try:
        return json.loads(sidecar.read_text())
    except json.JSONDecodeError:
        return {"comments": []}


def save_sidecar(md_path: Path, data: dict) -> None:
    sidecar = md_path.with_suffix(md_path.suffix + ".comments.json")
    tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(sidecar)


class Handler(BaseHTTPRequestHandler):
    server_version = "big-plan/0.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _send_pdf(self, md_path: Path, rel: str) -> None:
        chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        if not chrome:
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, b"PDF rendering is unavailable", "text/plain")
            return

        with tempfile.TemporaryDirectory(prefix="big-plan-pdf-") as tmp_dir:
            pdf_path = Path(tmp_dir) / "plan.pdf"
            url = f"http://127.0.0.1:{self.server.server_port}/{quote(rel, safe='/')}"
            try:
                result = subprocess.run(
                    [
                        chrome,
                        "--headless=new",
                        "--disable-gpu",
                        "--no-pdf-header-footer",
                        "--run-all-compositor-stages-before-draw",
                        "--virtual-time-budget=1000",
                        f"--print-to-pdf={pdf_path}",
                        url,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                self._send(HTTPStatus.GATEWAY_TIMEOUT, b"PDF rendering timed out", "text/plain")
                return
            if result.returncode != 0 or not pdf_path.is_file():
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, b"PDF rendering failed", "text/plain")
                return
            filename = f"{md_path.stem}.pdf".replace('"', "")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(pdf_path.stat().st_size))
            self.end_headers()
            self.wfile.write(pdf_path.read_bytes())

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    def _cross_origin(self) -> bool:
        """True if this looks like a browser request from another origin.

        There is no auth, so every POST here is CSRF-reachable from any page the
        user happens to have open: localhost:8765 is in their browser's reach
        whether or not the attacker is on the tailnet. `/api/submit` makes that
        acute because it spawns an agent, but comments, task flips and snapshot
        writes are the same class of forgeable side effect.

        Absent both headers we allow: curl sends neither (the README's
        registration flow depends on that), while a browser making a cross-origin
        POST always sends at least one.
        """
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return True
        origin = self.headers.get("Origin")
        if origin:
            host = self.headers.get("Host", "")
            # Compare host:port, which is what Origin carries after the scheme.
            return origin.split("//", 1)[-1] != host
        return False

    def _requires_json(self) -> bool:
        """Force a JSON content type so a cross-origin POST needs a preflight.

        A `text/plain` POST is a CORS "simple request" and fires without one; we
        answer no preflight, so demanding JSON is what makes the browser refuse
        to send it at all. Only enforced on the two endpoints that spawn agents,
        because four older bodyless POSTs in the client send no content type.
        """
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self._send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "Content-Type: application/json required"},
            )
            return False
        return True

    def _accept_hunk(self, snap_path, rel: str) -> None:
        """Accept one diff hunk into the baseline: splice its new lines into the
        snapshot so that hunk stops showing, leaving other changes intact.

        Body: {old_start, old_end, old_b64, new_b64} where the *_b64 fields are
        base64(JSON(list[str])). old_lines must still match the snapshot at
        [old_start:old_end] or we 409 (the baseline moved under the client).
        """
        if not snap_path.exists():
            self._send_json(HTTPStatus.CONFLICT, {"error": "no baseline; reload"})
            return
        try:
            payload = json.loads(self._read_body() or b"{}")
            old_start = int(payload["old_start"])
            old_end = int(payload["old_end"])
            old_lines = json.loads(base64.b64decode(payload["old_b64"]))
            new_lines = json.loads(base64.b64decode(payload["new_b64"]))
            # The lines get slice-assigned into the snapshot and "\n".join-ed, so
            # they must be a list of newline-free strings. A bare string would
            # splice character-by-character; a non-str would 500 on join; an
            # embedded "\n" would inject phantom lines (the only attack surface
            # for pure-insert hunks, where the old_lines==[] guard is a no-op).
            if not (isinstance(old_lines, list) and all(isinstance(x, str) for x in old_lines)):
                raise ValueError("old_lines must be list[str]")
            if not (isinstance(new_lines, list) and all(isinstance(x, str) for x in new_lines)):
                raise ValueError("new_lines must be list[str]")
            if any("\n" in x or "\r" in x for x in new_lines):
                raise ValueError("new_lines must not contain newlines")
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad accept payload"})
            return

        text = snap_path.read_text()
        ended_nl = text.endswith("\n")
        lines = text.splitlines()
        if old_start < 0 or old_start > old_end or old_end > len(lines):
            self._send_json(HTTPStatus.CONFLICT, {"error": "stale range; reload"})
            return
        if lines[old_start:old_end] != old_lines:
            self._send_json(HTTPStatus.CONFLICT, {"error": "baseline changed; reload"})
            return

        lines[old_start:old_end] = new_lines
        out = "\n".join(lines) + ("\n" if ended_nl else "")
        tmp = snap_path.with_suffix(snap_path.suffix + ".tmp")
        tmp.write_text(out)
        tmp.replace(snap_path)
        self._send_json(HTTPStatus.OK, {"accepted": rel})

    def do_GET(self) -> None:  # noqa: N802
        raw_path, _, query = self.path.partition("?")
        path = unquote(raw_path)
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)

        if path == "/":
            try:
                page = int(params.get("page", "1"))
            except ValueError:
                page = 1
            self._send(HTTPStatus.OK, render_index(ROOT_DIR, page))
            return

        if path.startswith("/assets/"):
            rel = path[len("/assets/"):]
            if rel not in ALLOWED_ASSETS:
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return
            asset = ASSETS_DIR / rel
            if not asset.is_file():
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return
            ctype = (
                "text/css" if asset.suffix == ".css"
                else "application/javascript" if asset.suffix == ".js"
                else "application/octet-stream"
            )
            self._send(HTTPStatus.OK, asset.read_bytes(), ctype)
            return

        if path.startswith("/raw/"):
            rel = path[len("/raw/"):]
            if not rel.endswith(".md"):
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return
            md_path = safe_join(ROOT_DIR, rel)
            if not md_path or not md_path.is_file():
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return
            self._send(HTTPStatus.OK, md_path.read_bytes(), "text/markdown; charset=utf-8")
            return

        if path.startswith("/pdf/"):
            rel = path[len("/pdf/"):]
            if not rel.endswith(".md"):
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return
            md_path = safe_join(ROOT_DIR, rel)
            if not md_path or not md_path.is_file():
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return
            self._send_pdf(md_path, rel)
            return

        if path.startswith("/api/comments/"):
            rel = path[len("/api/comments/"):]
            md_path = safe_join(ROOT_DIR, rel)
            if not md_path or not md_path.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            self._send_json(HTTPStatus.OK, load_sidecar(md_path))
            return

        if path.startswith("/api/submit/"):
            rel = path[len("/api/submit/"):]
            md_path = safe_join(ROOT_DIR, rel)
            if not md_path or not md_path.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            # Preview only: the button labels itself with the target it would
            # actually reach, so nobody presses "send" not knowing where it goes.
            self._send_json(HTTPStatus.OK, dispatch.route(md_path, ROOT_DIR))
            return

        if path.endswith(".md"):
            md_path = safe_join(ROOT_DIR, path)
            if not md_path or not md_path.is_file():
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return
            md_text = md_path.read_text()
            title = md_path.stem
            for line in md_text.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            comments = load_sidecar(md_path)
            snap_path = md_path.with_suffix(md_path.suffix + ".snapshot")
            snapshot_md = snap_path.read_text() if snap_path.exists() else None
            rel = str(md_path.relative_to(ROOT_DIR))
            if params.get("view") == "diff":
                html_out = render.render_diff_html(
                    snapshot_md if snapshot_md is not None else md_text,
                    md_text, title, relative_path=rel,
                )
            elif params.get("view") == "raw":
                html_out = render.render_raw_html(
                    md_text, title, relative_path=rel,
                )
            else:
                html_out = render.render_html(
                    md_text, title, comments,
                    relative_path=rel, snapshot_md=snapshot_md,
                )
            self._send(HTTPStatus.OK, html_out.encode("utf-8"))
            return

        self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(self.path.split("?", 1)[0])

        if not host_allowed(self.headers.get("Host", "")):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "unrecognised Host; POST refused"})
            return

        if self._cross_origin():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "cross-origin POST refused"})
            return

        if path.startswith("/api/task/"):
            rel = path[len("/api/task/"):]
            md_path = safe_join(ROOT_DIR, rel)
            if not md_path or not md_path.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                payload = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad json"})
                return
            line_no = payload.get("line")
            checked = payload.get("checked")
            if not isinstance(line_no, int) or not isinstance(checked, bool):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "line (int, 1-based) and checked (bool) required"},
                )
                return
            if not flip_task_line(md_path, line_no, checked):
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"error": "line is not a task line; reload and try again"},
                )
                return
            self._send_json(HTTPStatus.OK, {"line": line_no, "checked": checked})
            return

        if path.startswith("/api/submit/"):
            rel = path[len("/api/submit/"):]
            md_path = safe_join(ROOT_DIR, rel)
            if not md_path or not md_path.is_file() or not rel.endswith(".md"):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if not self._requires_json():
                return
            try:
                payload = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad json"})
                return
            # The button labels itself from a GET preview, but routing is
            # recomputed here. If it moved in between (provenance deleted, the
            # roster gone unreadable), refuse rather than quietly starting a
            # fresh session under a label that promised the authoring one.
            expect_mode = payload.get("expectMode")
            decision = None
            if expect_mode:
                decision = dispatch.route(md_path, ROOT_DIR)
                if (
                    decision["mode"] != expect_mode
                    or decision["sessionId"] != payload.get("expectSessionId")
                ):
                    self._send_json(
                        HTTPStatus.CONFLICT,
                        {"error": "route changed since the page loaded", "route": decision},
                    )
                    return
            try:
                # Hand the validated decision through: letting submit() re-route
                # would make the check and the spawn independent, so a roster
                # change in between could spawn a mode nobody agreed to.
                result = dispatch.submit(md_path, rel, ROOT_DIR, payload.get("prompt"), decision)
            except ValueError as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
                return
            except dispatch.DispatchFailed as e:
                # The agent died on startup (a busy Codex thread, a rejected
                # session id). Say so rather than reporting a send that did not
                # happen and leaving the reason in a log nobody reads.
                self.log_message("submit %s FAILED: %s", rel, e)
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(e)})
                return
            except OSError as e:
                # A missing CLI or an unwritable outbox is the operator's
                # problem, not the reviewer's: name it instead of a bare 500.
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"could not dispatch: {e}"}
                )
                return
            self.log_message("submit %s -> %s (%s)", rel, result["mode"], result["target"])
            self._send_json(HTTPStatus.OK, result)
            return

        if path.startswith("/api/promote/"):
            rel = path[len("/api/promote/"):]
            md_path = safe_join(ROOT_DIR, rel)
            if not md_path or not md_path.is_file() or not rel.endswith(".md"):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            promote(md_path)
            self._send_json(HTTPStatus.OK, {"promoted": rel})
            return

        if path.startswith("/api/session/"):
            rel = path[len("/api/session/"):]
            md_path = safe_join(ROOT_DIR, rel)
            if not md_path or not md_path.is_file() or not rel.endswith(".md"):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if not self._requires_json():
                return
            try:
                payload = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad json"})
                return
            try:
                record = dispatch.save_provenance(
                    md_path,
                    engine=str(payload.get("engine") or ""),
                    session_id=str(payload.get("sessionId") or ""),
                    name=str(payload.get("name") or ""),
                    cwd=str(payload.get("cwd") or ""),
                )
            except ValueError as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
                return
            # Registration is the normal authoring handoff, so it is also the
            # standard explicit promotion step. A separate /api/promote route
            # remains available for plans without feedback provenance.
            promote(md_path)
            self._send_json(HTTPStatus.OK, record)
            return

        if path.startswith("/api/snapshot/"):
            rest = path[len("/api/snapshot/"):]
            action = "set"
            for suffix in ("/clear", "/accept"):
                if rest.endswith(suffix):
                    action = suffix[1:]
                    rest = rest[: -len(suffix)]
                    break
            rel = rest
            md_path = safe_join(ROOT_DIR, rel)
            if not md_path or not md_path.is_file() or not rel.endswith(".md"):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            snap_path = md_path.with_suffix(md_path.suffix + ".snapshot")

            if action == "clear":
                snap_path.unlink(missing_ok=True)
                self._send_json(HTTPStatus.OK, {"cleared": rel})
                return

            if action == "accept":
                self._accept_hunk(snap_path, rel)
                return

            # Set baseline: copy the current source so the next render diffs against it.
            tmp = snap_path.with_suffix(snap_path.suffix + ".tmp")
            tmp.write_bytes(md_path.read_bytes())
            tmp.replace(snap_path)
            self._send_json(HTTPStatus.OK, {"snapshot": rel})
            return

        if not path.startswith("/api/comments/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        rest = path[len("/api/comments/"):]
        action = None
        cid = ""
        if rest.endswith("/resolve"):
            action = "resolve"
            rest = rest[: -len("/resolve")]
            rel, _, cid = rest.rpartition("/")
        elif rest.endswith("/delete"):
            action = "delete"
            rest = rest[: -len("/delete")]
            rel, _, cid = rest.rpartition("/")
        elif rest.endswith("/reply"):
            action = "reply"
            rest = rest[: -len("/reply")]
            rel, _, cid = rest.rpartition("/")
        else:
            rel = rest

        md_path = safe_join(ROOT_DIR, rel)
        if not md_path or not md_path.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        data = load_sidecar(md_path)
        comments = data.setdefault("comments", [])

        if action == "resolve":
            for c in comments:
                if c.get("id") == cid:
                    c["resolved"] = True
                    c["resolved_at"] = now_iso()
                    save_sidecar(md_path, data)
                    self._send_json(HTTPStatus.OK, c)
                    return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "comment not found"})
            return

        if action == "reply":
            try:
                payload = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad json"})
                return
            text = (payload.get("text") or "").strip()
            if not text:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "text required"})
                return
            if len(text.encode("utf-8")) > MAX_REPLY_BYTES:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "reply too large"})
                return
            role = (payload.get("role") or "agent").strip()
            if role not in ALLOWED_REPLY_ROLES:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"bad role: {role}"})
                return
            author = (payload.get("author") or "").strip() or (
                "reviewer" if role == "reviewer" else "agent"
            )
            for c in comments:
                if c.get("id") != cid:
                    continue
                # Refused rather than silently written: a resolved comment does
                # not render, so an answer posted into one is invisible. The
                # caller should know its answer went nowhere.
                if c.get("resolved"):
                    self._send_json(
                        HTTPStatus.CONFLICT,
                        {"error": "comment is resolved; nothing would render"},
                    )
                    return
                # Same reason: reactions render as bare chips and status entries
                # do not render at all, so neither has anywhere to show a
                # thread. Allowing it would leave the "N answered" chip counting
                # an answer with nothing to navigate to.
                ctype = c.get("type", "text")
                if ctype in ("reaction", "status"):
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": f"cannot reply to a {ctype}"},
                    )
                    return
                reply = {
                    "id": uuid.uuid4().hex[:12],
                    "role": role,
                    "author": author,
                    "text": text,
                    "timestamp": now_iso(),
                }
                replies = c.get("replies")
                if not isinstance(replies, list):
                    replies = []
                replies.append(reply)
                c["replies"] = replies
                save_sidecar(md_path, data)
                self._send_json(HTTPStatus.CREATED, {"comment": cid, "reply": reply})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "comment not found"})
            return

        if action == "delete":
            before = len(comments)
            data["comments"] = [c for c in comments if c.get("id") != cid]
            if len(data["comments"]) == before:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "comment not found"})
                return
            save_sidecar(md_path, data)
            self._send_json(HTTPStatus.OK, {"deleted": cid})
            return

        try:
            payload = json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad json"})
            return

        anchor = (payload.get("anchor") or "").strip()
        ctype = (payload.get("type") or "text").strip()
        author = (payload.get("author") or "anon").strip()
        if not anchor:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "anchor required"})
            return
        if ctype not in ALLOWED_TYPES:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"bad type: {ctype}"})
            return

        new_comment: dict = {
            "id": uuid.uuid4().hex[:12],
            "anchor": anchor,
            "type": ctype,
            "author": author,
            "timestamp": now_iso(),
            "resolved": False,
        }

        if ctype == "text":
            text = (payload.get("text") or "").strip()
            if not text:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "text required"})
                return
            new_comment["text"] = text
            # Optional span-comment fields: a text comment carrying a non-empty
            # quote highlights just that selection within its anchored block.
            quote = payload.get("quote")
            if isinstance(quote, str) and quote.strip():
                new_comment["quote"] = quote.strip()
                quote_occurrence = payload.get("quoteOccurrence")
                if isinstance(quote_occurrence, int) and quote_occurrence >= 0:
                    new_comment["quoteOccurrence"] = quote_occurrence
                else:
                    new_comment["quoteOccurrence"] = 0
        elif ctype == "reaction":
            emoji = (payload.get("emoji") or "").strip()
            if emoji not in ALLOWED_REACTIONS:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad emoji"})
                return
            new_comment["emoji"] = emoji
        elif ctype == "decision":
            raw_choices = payload.get("choices")
            if not isinstance(raw_choices, list):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "choices must be a list"})
                return
            choices = [str(c).strip() for c in raw_choices if str(c).strip()]
            # Empty choices = clear any prior decision for this anchor without adding a new one.
            data["comments"] = [
                c for c in comments
                if not (
                    c.get("type") == "decision"
                    and c.get("anchor") == anchor
                    and not c.get("resolved")
                )
            ]
            if not choices:
                save_sidecar(md_path, data)
                self._send_json(HTTPStatus.OK, {"cleared": anchor})
                return
            new_comment["choices"] = choices
            q = (payload.get("question") or "").strip()
            if q:
                new_comment["question"] = q
            data["comments"].append(new_comment)
            save_sidecar(md_path, data)
            self._send_json(HTTPStatus.CREATED, new_comment)
            return
        elif ctype == "status":
            checked = payload.get("checked")
            if not isinstance(checked, bool):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "checked must be boolean"})
                return
            new_comment["checked"] = checked
            text = (payload.get("text") or "").strip()
            if text:
                new_comment["text"] = text
            # One open status per anchor: replace prior.
            data["comments"] = [
                c for c in comments
                if not (
                    c.get("type") == "status"
                    and c.get("anchor") == anchor
                    and not c.get("resolved")
                )
            ]
            data["comments"].append(new_comment)
            save_sidecar(md_path, data)
            self._send_json(HTTPStatus.CREATED, new_comment)
            return

        comments.append(new_comment)
        save_sidecar(md_path, data)
        self._send_json(HTTPStatus.CREATED, new_comment)


def get_tailscale_url(port: int) -> str | None:
    try:
        import subprocess
        out = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
        dns = data.get("Self", {}).get("DNSName", "").rstrip(".")
        if dns:
            return f"http://{dns}:{port}/"
    except Exception:
        return None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve big-plan markdown over HTTP.")
    parser.add_argument("root", nargs="?", default=".", help="Directory to serve")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--filter",
        dest="index_filter",
        choices=("plans", "readmes", "all"),
        default="plans",
        help="Which .md files to show in the index. Direct URLs still serve any file.",
    )
    args = parser.parse_args()

    global ROOT_DIR, INDEX_FILTER
    ROOT_DIR = Path(args.root).resolve()
    INDEX_FILTER = args.index_filter
    if not ROOT_DIR.is_dir():
        print(f"ERROR: {ROOT_DIR} is not a directory", file=sys.stderr)
        return 2

    # The reply-endpoint curl that dispatch puts in front of the receiving
    # agent has to name a port that is actually listening, and ad-hoc runs
    # override the default.
    dispatch.LOCAL_BASE_URL = f"http://localhost:{args.port}"

    allowed = refresh_allowed_hosts()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"big-plan: serving {ROOT_DIR} (filter={INDEX_FILTER})")
    # Printed because a POST 403ing on an unexpected hostname is otherwise a
    # mystery; this is the list to check first.
    print(f"  POST hosts: {', '.join(sorted(allowed))}")
    print(f"  local:     http://{args.host}:{args.port}/")
    ts_url = get_tailscale_url(args.port)
    if ts_url:
        print(f"  tailscale: {ts_url}")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down...")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
