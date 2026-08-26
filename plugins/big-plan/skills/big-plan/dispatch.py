"""Route plan feedback back to the agent session that authored the plan.

Provenance lives beside the plan in a `<plan>.md.session` sidecar. The name
deliberately does not end in `.md`, so the Markdown index scan, agentic
markdown search, and direct GETs all ignore it -- the same trick `.md.snapshot`
already uses.

    {
      "engine": "claude" | "codex" | "grok",
      "sessionId": "<uuid>",
      "name": "Home Directory Spring Cleaning Audit",
      "cwd": "/home/example/project",
      "recordedAt": "2026-08-14T12:00:00+00:00"
    }

Five delivery modes, picked by what the provenance points at:

  message   The Claude session is live in the daemon roster. A throwaway
            headless courier (`claude -p`) calls SendMessage, which wakes the
            idle session in place with its whole authoring context intact.
            There is no CLI verb for this; the courier exists purely because
            SendMessage is a tool, not a command.
  resume    The Claude session is recorded but no longer live -> `claude --bg
            --resume`, which replays its transcript into a fresh worker. Only
            ever chosen when nothing is live on that id: two workers on one
            transcript interleave writes into the same JSONL.
  codex     A Codex thread -> joins the local app-server thread and starts a
            turn, or steers its active turn. This is the only way to reach an
            open Codex Desktop session without a second writer conflict.
  grok      A Grok thread -> joins the local Grok leader and prompts that
            session, loading it first when it is not already resident. This
            is the only way to reach an open Grok TUI session without a
            second writer. With no leader at all, falls back to
            `grok --resume --prompt-file` the same way Claude `resume` does.
  dispatch  No usable provenance -> agent-router starts a fresh session and
            owns the engine choice. The plan plus its comments sidecar carry
            most of the context, so this degrades softly rather than failing.

`claude agents --json` failing is treated as "cannot tell live from dead", which
routes to `dispatch` rather than `resume` -- guessing wrong in that direction
puts a second worker on a live transcript.

The feedback text never touches argv or a shell. It is written to an outbox file
and the commands only ever carry that path, so a comment containing shell
metacharacters is inert. Nothing here uses shell=True.
"""
from __future__ import annotations

import datetime as dt
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.parse import quote

# Cap the payload so a runaway client cannot fill the outbox disk. Real
# aggregated feedback for one plan is a few KB.
MAX_PROMPT_BYTES = 256 * 1024

# How long to watch a freshly spawned agent before calling the send successful.
# Long enough for a startup refusal to surface, short enough that a real send
# (which runs for many seconds) is never waited out.
EARLY_FAILURE_WINDOW = float(os.environ.get("BIG_PLAN_EARLY_FAILURE_WINDOW", "2.5"))

CODEX_APP_SERVER_SOCKET = Path(os.environ.get(
    "BIG_PLAN_CODEX_APP_SERVER_SOCKET",
    Path.home() / ".codex/app-server-control/app-server-control.sock",
))

# Official Grok leader frame cap, matching agent-viewer.
GROK_MAX_FRAME_BYTES = 64 * 1024 * 1024
GROK_LEADER_TIMEOUT = 15
# Linux SO_PEERCRED: {pid, uid, gid} as three ints.
_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)
_UCRED_STRUCT = struct.Struct("3i")

VALID_ENGINES = frozenset({"claude", "codex", "grok"})
# Session ids are UUIDs on every engine. Kept strict because this value is the
# one piece of sidecar data that reaches a subprocess argv.
_SESSION_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")

OUTBOX = Path(
    os.environ.get("BIG_PLAN_OUTBOX", Path.home() / ".local/state/big-plan/outbox")
)

# Where the receiving agent posts replies back to. Overwritten by the server at
# startup with its real port; the default matches the systemd unit.
LOCAL_BASE_URL = os.environ.get("BIG_PLAN_BASE_URL", "http://localhost:8765")


def _resolve_bin(env_name: str, name: str) -> str:
    """Absolute path to a CLI, env-overridable for tests.

    Never returns a bare name: the service runs under systemd --user, whose PATH
    does not include ~/.local/bin, so a bare `claude` silently fails to exec.
    """
    override = os.environ.get(env_name)
    if override:
        return override
    found = shutil.which(name)
    if found:
        return found
    return str(Path.home() / ".local" / "bin" / name)


def claude_bin() -> str:
    return _resolve_bin("BIG_PLAN_CLAUDE_BIN", "claude")


def router_bin() -> str:
    return _resolve_bin("BIG_PLAN_ROUTER_BIN", "agent-router")


def grok_bin() -> str:
    return _resolve_bin("BIG_PLAN_GROK_BIN", "grok")


def grok_home() -> Path:
    override = os.environ.get("GROK_HOME") or os.environ.get("BIG_PLAN_GROK_HOME")
    if override:
        return Path(override)
    return Path.home() / ".grok"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def provenance_path(md_path: Path) -> Path:
    return md_path.with_suffix(md_path.suffix + ".session")


def load_provenance(md_path: Path) -> dict | None:
    """Read and validate the provenance sidecar. None if absent or unusable."""
    p = provenance_path(md_path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    engine = str(data.get("engine") or "").strip().lower()
    session_id = str(data.get("sessionId") or "").strip()
    if engine not in VALID_ENGINES or not _SESSION_ID_RE.match(session_id):
        return None
    return {
        "engine": engine,
        "sessionId": session_id,
        "name": str(data.get("name") or "").strip(),
        "cwd": str(data.get("cwd") or "").strip(),
        "recordedAt": str(data.get("recordedAt") or "").strip(),
    }


def save_provenance(md_path: Path, engine: str, session_id: str, name: str, cwd: str) -> dict:
    """Write the provenance sidecar. Raises ValueError on a bad engine/id."""
    engine = engine.strip().lower()
    session_id = session_id.strip()
    if engine not in VALID_ENGINES:
        raise ValueError(f"engine must be one of {sorted(VALID_ENGINES)}")
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError("sessionId must look like a session UUID")
    record = {
        "engine": engine,
        "sessionId": session_id,
        "name": name.strip(),
        "cwd": cwd.strip(),
        "recordedAt": now_iso(),
    }
    path = provenance_path(md_path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
    return record


def live_claude_sessions() -> dict[str, dict] | None:
    """Map sessionId -> roster entry for live Claude sessions.

    None (not {}) when the roster could not be read at all, so callers can tell
    "nothing is live" apart from "we do not know what is live".
    """
    try:
        out = subprocess.run(
            [claude_bin(), "agents", "--json"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        rows = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(rows, list):
        return None
    return {
        str(r.get("sessionId")): r
        for r in rows
        if isinstance(r, dict) and r.get("sessionId")
    }


def _fallback_dir(md_path: Path, root: Path) -> str:
    """Where a fresh dispatch should start: the plan's repo, else the serve root.

    Walks up looking for a .git so the dispatched session lands in the repo that
    owns the plan rather than the whole ~/git tree.
    """
    for parent in md_path.parents:
        if (parent / ".git").exists():
            return str(parent)
        if parent == root:
            break
    return str(root)


def _is_grok_session(session_id: str, cwd: str = "") -> bool:
    """True when this id is a Grok session on disk.

    Grok agents used to register as `codex` because grok was not a valid
    engine. Checking the session store is how those sidecars still reach the
    Grok thread instead of minting a new Codex one.
    """
    if not _SESSION_ID_RE.match(session_id):
        return False
    sessions = grok_home() / "sessions"
    if not sessions.is_dir():
        return False
    if cwd:
        encoded = quote(cwd, safe="")
        if (sessions / encoded / session_id / "summary.json").is_file():
            return True
    try:
        for cwd_dir in sessions.iterdir():
            if (cwd_dir / session_id / "summary.json").is_file():
                return True
    except OSError:
        return False
    return False


def route(md_path: Path, root: Path) -> dict:
    """Decide how a submit on this plan would be delivered. No side effects."""
    prov = load_provenance(md_path)
    if prov is None:
        return {
            "mode": "dispatch",
            "engine": None,
            "target": None,
            "sessionId": None,
            "cwd": _fallback_dir(md_path, root),
            "reason": "no authoring session recorded for this plan",
        }

    cwd = prov["cwd"] or _fallback_dir(md_path, root)
    # Explicit grok, or a Codex-labelled sidecar whose id is actually a Grok
    # session: join the Grok leader. The mis-label happens because grok was not
    # a valid engine when those plans were registered.
    if prov["engine"] == "grok" or (
        prov["engine"] == "codex" and _is_grok_session(prov["sessionId"], cwd)
    ):
        return {
            "mode": "grok",
            "engine": "grok",
            "target": prov["name"] or prov["sessionId"][:8],
            "sessionId": prov["sessionId"],
            "cwd": cwd,
            "reason": "joins the Grok thread that wrote the plan",
        }

    if prov["engine"] == "codex":
        return {
            "mode": "codex",
            "engine": "codex",
            "target": prov["name"] or prov["sessionId"][:8],
            "sessionId": prov["sessionId"],
            "cwd": cwd,
            "reason": "resumes the Codex thread that wrote the plan",
        }

    live = live_claude_sessions()
    if live is None:
        return {
            "mode": "dispatch",
            "engine": None,
            "target": None,
            "sessionId": None,
            "cwd": prov["cwd"] or _fallback_dir(md_path, root),
            "reason": "could not read the session roster; starting fresh is the safe guess",
        }

    entry = live.get(prov["sessionId"])
    if entry:
        name = str(entry.get("name") or prov["name"] or prov["sessionId"][:8])
        return {
            "mode": "message",
            "engine": "claude",
            "target": name,
            "sessionId": prov["sessionId"],
            "cwd": str(entry.get("cwd") or prov["cwd"] or _fallback_dir(md_path, root)),
            "reason": f"session is live ({entry.get('status', 'unknown')})",
        }

    return {
        "mode": "resume",
        "engine": "claude",
        "target": prov["name"] or prov["sessionId"][:8],
        "sessionId": prov["sessionId"],
        "cwd": prov["cwd"] or _fallback_dir(md_path, root),
        "reason": "session is no longer live; replaying its transcript",
    }


def build_payload(md_path: Path, rel: str, prompt: str) -> str:
    """Wrap the client's aggregated feedback with the context the target needs.

    The snapshot instruction is here rather than in the README because the
    session receiving this is mid-context and will not go re-read the service
    docs before editing.
    """
    sidecar = md_path.with_suffix(md_path.suffix + ".comments.json")
    reply_url_template = (
        f"{LOCAL_BASE_URL.rstrip('/')}/api/comments/"
        f"{quote(rel.lstrip('/'))}/<COMMENT_ID>/reply"
    )
    return (
        "Reviewer feedback just came in on a plan you wrote. It arrived through "
        "big-plan (the commentable HTML view), not as a new chat turn.\n"
        f"\nPlan file:       {md_path}\n"
        f"Served path:     /{rel}\n"
        f"Comment sidecar: {sidecar}\n"
        "\n---\n\n"
        f"{prompt.rstrip()}\n"
        "\n---\n\n"
        "Before you edit the plan, set the diff baseline so your edits highlight "
        "for the reviewer:\n    cp "
        # Quoted because the receiving agent runs this verbatim, and plan paths
        # under .projects/ routinely contain spaces.
        f"{shlex.quote(str(md_path))} {shlex.quote(str(md_path) + '.snapshot')}\n"
        "\nThen triage every comment into exactly one of three outcomes. Picking "
        "the right one matters, because two of them are invisible if you get it "
        "wrong:\n"
        "\n1. It asks for a change to the plan -> make the edit, then delete "
        "that comment from the sidecar.\n"
        "\n2. It is a question for you, or an argument to answer rather than a "
        "plan edit -> reply in the thread and LEAVE THE COMMENT OPEN:\n"
        f"     jq -n --arg t 'your answer' '{{text:$t}}' | curl -sX POST \\\n"
        f"       {shlex.quote(reply_url_template)} \\\n"
        "       -H 'Content-Type: application/json' -d @-\n"
        "   Substitute the comment's own id for <COMMENT_ID> (the `id` field in "
        "the sidecar). Do NOT resolve or delete a comment you answered this "
        "way: resolved comments stop rendering, so you would be deleting your "
        "own answer before the reviewer ever saw it. Dismissing an answered "
        "thread is their press, not yours.\n"
        "\n3. It needs a decision only the reviewer can make -> leave it open "
        "and say so as a reply, so the ask is visible on the page instead of "
        "only in your chat transcript.\n"
        "\nA thread that already shows a reply from you with no reviewer reply "
        "after it has been answered; do not answer it a second time.\n"
    )


def _write_payload(payload: str) -> Path:
    OUTBOX.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = OUTBOX / f"{stamp}-{uuid.uuid4().hex[:8]}.md"
    path.write_text(payload)
    return path


def _courier_prompt(target_name: str, session_id: str, payload_path: Path) -> str:
    return (
        "You are a courier. Do exactly one delivery, then stop.\n\n"
        f"1. Read the file {payload_path}\n"
        "2. Call ListAgents and find the peer whose name is exactly:\n"
        f"     {target_name}\n"
        f"   That peer's session id is {session_id}. Address it by the [ref] the "
        "listing prints for it; a ref you did not just read from the listing "
        "will not resolve.\n"
        "3. Call SendMessage to send that peer the ENTIRE contents of the file "
        "verbatim. Do not summarize it, do not paraphrase it, do not add a "
        "preamble or any commentary of your own.\n\n"
        "Do not do any other work. Do not edit any file. Reply with only the "
        "word sent, or the error you hit.\n"
    )


def _pointer_prompt(md_path: Path, payload_path: Path) -> str:
    return (
        "New reviewer feedback on a plan you wrote: "
        f"{md_path}\n"
        f"The full set of open comments is at {payload_path} -- read that file "
        "first, then follow it.\n"
    )


class DispatchFailed(RuntimeError):
    """The spawned agent died immediately; the send did not happen."""


def _spawn(argv: list[str], cwd: str, stdin_text: str | None, log_path: Path) -> None:
    """Launch detached, watch briefly for an immediate death, then reap.

    The HTTP response must not wait on a full agent turn, but returning "sent"
    for a process that died on startup is worse than being slow: the failure
    lands in a log nobody reads while the UI reports success. The failures that
    matter here are all instant -- `codex exec resume` refuses a thread that
    "already has an active writer", a missing binary is ENOENT, a bad session id
    is rejected before any model call -- so a short watch catches them while
    every real send is still running when the window closes.

    ThreadingHTTPServer does no child reaping of its own, hence the thread.
    """
    log = open(log_path, "ab")  # noqa: SIM115 -- closed by the reaper thread
    proc = subprocess.Popen(
        argv,
        cwd=cwd if cwd and Path(cwd).is_dir() else None,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
        finally:
            proc.stdin.close()

    try:
        code = proc.wait(timeout=EARLY_FAILURE_WINDOW)
    except subprocess.TimeoutExpired:
        code = None  # still running, which is what a real send looks like

    if code is not None and code != 0:
        log.close()
        raise DispatchFailed(_log_tail(log_path) or f"exited {code}")

    if code == 0:
        log.close()
        return

    def _reap() -> None:
        try:
            proc.wait()
        finally:
            log.close()

    threading.Thread(target=_reap, daemon=True).start()


def _log_tail(log_path: Path, limit: int = 400) -> str:
    """Last of the failed process's output, for an error the user can act on."""
    try:
        text = log_path.read_text(errors="replace").strip()
    except OSError:
        return ""
    # Strip ANSI so the message reads cleanly in an alert box.
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return " / ".join(lines[-3:])[-limit:]


def _read_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise DispatchFailed("peer closed the connection")
        data.extend(chunk)
    return bytes(data)


def _websocket_send(sock: socket.socket, message: dict) -> None:
    """Send one masked JSON text frame as required for a websocket client."""
    body = json.dumps(message).encode("utf-8")
    mask = os.urandom(4)
    size = len(body)
    if size < 126:
        header = bytes((0x81, 0x80 | size))
    elif size <= 0xFFFF:
        header = bytes((0x81, 0x80 | 126)) + struct.pack("!H", size)
    else:
        header = bytes((0x81, 0x80 | 127)) + struct.pack("!Q", size)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(body))
    sock.sendall(header + mask + masked)


def _websocket_receive(sock: socket.socket) -> dict:
    """Receive one JSON websocket frame from the local Codex app server."""
    first, second = _read_exact(sock, 2)
    opcode = first & 0x0F
    size = second & 0x7F
    if size == 126:
        size = struct.unpack("!H", _read_exact(sock, 2))[0]
    elif size == 127:
        size = struct.unpack("!Q", _read_exact(sock, 8))[0]
    mask = _read_exact(sock, 4) if second & 0x80 else None
    body = _read_exact(sock, size)
    if mask:
        body = bytes(byte ^ mask[index % 4] for index, byte in enumerate(body))
    if opcode == 0x8:
        raise DispatchFailed("Codex app server closed the websocket")
    if opcode == 0x9:
        sock.sendall(b"\x8a" + bytes((len(body),)) + body)
        return _websocket_receive(sock)
    if opcode != 0x1:
        return _websocket_receive(sock)
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise DispatchFailed("Codex app server returned invalid JSON") from e


def _app_server_request(sock: socket.socket, request_id: int, method: str, params: dict | None = None) -> dict:
    request = {"id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    _websocket_send(sock, request)
    while True:
        response = _websocket_receive(sock)
        if response.get("id") != request_id:
            continue
        if "error" in response:
            raise DispatchFailed(f"Codex app server {method}: {response['error'].get('message', response['error'])}")
        return response["result"]


def _codex_app_server_send(session_id: str, payload: str) -> str:
    """Add feedback to the app-server thread that already owns this session.

    `codex exec resume` opens a second writer and cannot target a thread shown
    in Codex Desktop. Joining its app-server thread instead works whether the
    thread is idle or has a regular turn in progress.
    """
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    expected_accept = base64.b64encode(hashlib.sha1(
        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
    ).digest()).decode("ascii")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(15)
        sock.connect(str(CODEX_APP_SERVER_SOCKET))
        sock.sendall(
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n".encode("ascii")
        )
        headers = _read_until(sock, b"\r\n\r\n")
        if b" 101 " not in headers.split(b"\r\n", 1)[0] or expected_accept.encode() not in headers:
            raise DispatchFailed("Codex app server rejected the websocket connection")
        _app_server_request(sock, 1, "initialize", {
            "clientInfo": {"name": "big_plan_feedback", "version": "1.0"},
        })
        _websocket_send(sock, {"method": "initialized"})
        thread = _app_server_request(sock, 2, "thread/resume", {"threadId": session_id})["thread"]
        if thread.get("status", {}).get("type") == "idle":
            turn = _app_server_request(sock, 3, "turn/start", {
                "threadId": session_id,
                "clientUserMessageId": str(uuid.uuid4()),
                "input": [{"type": "text", "text": payload}],
            })["turn"]
            return str(turn["id"])
        active_turn = next((turn for turn in reversed(thread.get("turns", [])) if turn.get("status") == "inProgress"), None)
        if active_turn is None:
            raise DispatchFailed("Codex thread is not idle and has no steerable turn")
        result = _app_server_request(sock, 3, "turn/steer", {
            "threadId": session_id,
            "expectedTurnId": active_turn["id"],
            "clientUserMessageId": str(uuid.uuid4()),
            "input": [{"type": "text", "text": payload}],
        })
        return str(result["turnId"])


def _read_until(sock: socket.socket, delimiter: bytes, limit: int = 16 * 1024) -> bytes:
    data = bytearray()
    while delimiter not in data:
        if len(data) >= limit:
            raise DispatchFailed("Codex app server sent oversized HTTP headers")
        data.extend(_read_exact(sock, 1))
    return bytes(data)


def _grok_leader_sockets() -> list[Path]:
    """Leader sockets big-plan may join.

    `BIG_PLAN_GROK_LEADER_SOCKET` is the test override and skips the lock/uid
    filter. Otherwise this is the same discovery agent-viewer uses: a
    `leader*.sock` next to a matching `leader*.lock`, both owned by us.
    """
    override = os.environ.get("BIG_PLAN_GROK_LEADER_SOCKET")
    if override:
        return [Path(override)]
    home = grok_home()
    try:
        names = [p.name for p in home.iterdir()]
    except OSError:
        return []
    uid = os.geteuid()
    sockets: list[Path] = []
    for name in names:
        if not name.startswith("leader") or not name.endswith(".sock"):
            continue
        suffix = name[len("leader"):-len(".sock")]
        sock = home / name
        lock = home / f"leader{suffix}.lock"
        try:
            sock_stat = sock.lstat()
            lock_stat = lock.lstat()
        except OSError:
            continue
        if sock_stat.st_uid != uid or lock_stat.st_uid != uid:
            continue
        sockets.append(sock)
    return sockets


def _verify_unix_peer(sock: socket.socket) -> None:
    try:
        cred = sock.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, _UCRED_STRUCT.size)
    except OSError as e:
        raise DispatchFailed("Grok leader peer credentials unavailable") from e
    _pid, uid, _gid = _UCRED_STRUCT.unpack(cred)
    if uid != os.geteuid():
        raise DispatchFailed("Grok leader peer is not owned by the current user")


def _grok_write_frame(sock: socket.socket, message: dict) -> None:
    body = json.dumps(message).encode("utf-8")
    if len(body) > GROK_MAX_FRAME_BYTES:
        raise DispatchFailed("Grok leader frame exceeds the 64 MiB limit")
    sock.sendall(struct.pack("!I", len(body)) + body)


def _grok_read_frame(sock: socket.socket) -> dict:
    prefix = _read_exact(sock, 4)
    size = struct.unpack("!I", prefix)[0]
    if size > GROK_MAX_FRAME_BYTES:
        raise DispatchFailed("Grok leader frame exceeds the 64 MiB limit")
    body = _read_exact(sock, size)
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise DispatchFailed("Grok leader returned invalid JSON") from e


def _sessions_from_list_result(result: object) -> list[dict]:
    if not isinstance(result, dict):
        return []
    inner = result.get("result") if isinstance(result.get("result"), dict) else result
    sessions = inner.get("sessions") if isinstance(inner, dict) else None
    if not isinstance(sessions, list):
        return []
    return [s for s in sessions if isinstance(s, dict)]


class _GrokLeaderClient:
    """Length-prefixed ACP client for the official Grok leader socket."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._next_id = 1

    @classmethod
    def connect(cls, socket_path: Path) -> _GrokLeaderClient:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(GROK_LEADER_TIMEOUT)
        try:
            sock.connect(str(socket_path))
            _verify_unix_peer(sock)
            client = cls(sock)
            client._register()
            return client
        except OSError as e:
            sock.close()
            raise DispatchFailed(f"Grok leader is not reachable: {e}") from e
        except DispatchFailed:
            sock.close()
            raise

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _register(self) -> None:
        _grok_write_frame(self.sock, {
            "type": "register",
            "client_type": "big_plan_feedback",
            "mode": "stdio",
            "capabilities": {"yolo_mode": True},
        })
        registered = _grok_read_frame(self.sock)
        if registered.get("type") != "registered":
            raise DispatchFailed("Grok leader registration failed")
        protocol = registered.get("leader_protocol_version")
        control = bool((registered.get("leader_capabilities") or {}).get("control_v1"))
        if not isinstance(protocol, int) or protocol < 1 or not control:
            raise DispatchFailed("Grok leader does not support control protocol version 1")
        if registered.get("ready") is False:
            ready = _grok_read_frame(self.sock)
            if ready.get("type") != "leader_ready":
                raise DispatchFailed("Grok leader did not become ready")

    def _next_request_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    def _write_acp(self, message: dict) -> None:
        _grok_write_frame(self.sock, {"type": "acp", "payload": json.dumps(message)})

    def _read_acp(self) -> dict:
        for _ in range(4096):
            outer = _grok_read_frame(self.sock)
            if outer.get("type") != "acp":
                continue
            payload = outer.get("payload")
            if not isinstance(payload, str):
                raise DispatchFailed("Grok leader returned malformed ACP")
            try:
                message = json.loads(payload)
            except json.JSONDecodeError as e:
                raise DispatchFailed("Grok leader returned invalid ACP JSON") from e
            if message.get("method") == "session/request_permission" and "id" in message:
                self._write_acp({
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"outcome": {"outcome": "cancelled"}},
                })
                continue
            return message
        raise DispatchFailed("Grok leader sent too many unrelated messages")

    def request(self, method: str, params: dict | None = None) -> dict:
        request_id = self.send_request(method, params)
        return self.wait_response(request_id, method)

    def send_request(self, method: str, params: dict | None = None) -> int:
        request_id = self._next_request_id()
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._write_acp(message)
        return request_id

    def wait_response(
        self,
        request_id: int,
        method: str,
        pending_error: tuple[int, str] | None = None,
    ) -> dict:
        for _ in range(4096):
            message = self._read_acp()
            if pending_error is not None and message.get("id") == pending_error[0]:
                if message.get("error"):
                    raise DispatchFailed(_grok_rpc_error(pending_error[1], message["error"]))
                continue
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise DispatchFailed(_grok_rpc_error(method, message["error"]))
            result = message.get("result")
            if result is None:
                return {}
            if not isinstance(result, dict):
                raise DispatchFailed(f"Grok {method} returned no result")
            return result
        raise DispatchFailed(f"Grok {method} received too many unrelated messages")

    def initialize(self) -> None:
        self.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "_meta": {"clientType": "big_plan_feedback", "clientVersion": "1.0"},
        })

    def list_sessions(self, pending_error: tuple[int, str] | None = None) -> list[dict]:
        request_id = self.send_request("_x.ai/sessions/list", {})
        result = self.wait_response(request_id, "_x.ai/sessions/list", pending_error)
        return _sessions_from_list_result(result)


def _grok_rpc_error(method: str, error: object) -> str:
    detail = ""
    if isinstance(error, dict):
        detail = str(error.get("message") or "").strip()
    return f"Grok {method} request failed: {detail}" if detail else f"Grok {method} request failed"


def _session_is_ready(entry: dict | None) -> bool:
    if not entry:
        return False
    if entry.get("resident") is True:
        return True
    return str(entry.get("activity") or "") in {"working", "idle", "needs_input"}


def _find_session(sessions: list[dict], session_id: str) -> dict | None:
    for entry in sessions:
        if entry.get("sessionId") == session_id:
            return entry
    return None


def _grok_prompt_on_client(
    client: _GrokLeaderClient,
    session_id: str,
    payload: str,
    cwd: str,
    already_resident: bool,
) -> None:
    if not already_resident:
        client.request("session/load", {
            "sessionId": session_id,
            "cwd": cwd,
            "mcpServers": [],
        })
    prompt_id = client.send_request("session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": payload}],
    })
    deadline = dt.datetime.now(dt.timezone.utc).timestamp() + GROK_LEADER_TIMEOUT
    while True:
        roster = client.list_sessions(pending_error=(prompt_id, "session/prompt"))
        entry = _find_session(roster, session_id)
        if already_resident and _session_is_ready(entry):
            return
        if entry and entry.get("resident") is True and entry.get("activity") == "working":
            return
        if dt.datetime.now(dt.timezone.utc).timestamp() >= deadline:
            if already_resident:
                return
            raise DispatchFailed(
                "Grok roster did not confirm the session accepted the feedback"
            )


def _grok_leader_send(session_id: str, payload: str, cwd: str) -> None:
    """Deliver feedback through the Grok leader that already owns this session.

    `grok -p --resume` opens a second writer against a live TUI session. Joining
    the leader and prompting the existing session is the Grok equivalent of the
    Codex app-server path: it works whether the thread is idle or mid-turn
    (follow-ups queue by default).
    """
    sockets = _grok_leader_sockets()
    if not sockets:
        raise DispatchFailed("no Grok leader socket")

    load_target: Path | None = None
    handshake_error: DispatchFailed | None = None
    for socket_path in sockets:
        client = None
        owned = False
        try:
            client = _GrokLeaderClient.connect(socket_path)
            client.initialize()
            load_target = socket_path
            owned = _find_session(client.list_sessions(), session_id) is not None
            if not owned:
                continue
            _grok_prompt_on_client(
                client, session_id, payload, cwd, already_resident=True
            )
            return
        except DispatchFailed as e:
            # Handshake/list failures try the next socket. Once this leader
            # has the session, a prompt failure is terminal: loading it onto
            # another leader would be a second writer.
            if owned:
                raise
            handshake_error = e
        except OSError as e:
            handshake_error = DispatchFailed(f"Grok leader is not reachable: {e}")
        finally:
            if client is not None:
                client.close()

    target = load_target or sockets[0]
    client = None
    try:
        client = _GrokLeaderClient.connect(target)
        client.initialize()
        _grok_prompt_on_client(client, session_id, payload, cwd, already_resident=False)
    except OSError as e:
        raise handshake_error or DispatchFailed(f"Grok leader is not reachable: {e}") from e
    finally:
        if client is not None:
            client.close()


def _grok_cli_resume(session_id: str, cwd: str, payload_path: Path, log_path: Path) -> None:
    """Headless resume when no leader exists at all. Same shape as Claude `resume`."""
    argv = [
        grok_bin(),
        "--prompt-file", str(payload_path),
        "--resume", session_id,
        "--always-approve",
        "--verbatim",
        "--no-auto-update",
    ]
    if cwd:
        argv.extend(["--cwd", cwd])
    _spawn(argv, cwd, None, log_path)


def submit(md_path: Path, rel: str, root: Path, prompt: str, decision: dict | None = None) -> dict:
    """Deliver aggregated feedback to the plan's authoring session.

    `decision` lets the caller hand in a route it has already validated against
    the client's expectation. Without it this would re-route independently, and
    a roster change in between could spawn a different mode than the one the
    caller checked and the button promised.

    Returns the routing summary. Raises ValueError on a bad prompt.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt required")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("prompt too large")

    decision = decision if decision is not None else route(md_path, root)
    payload_path = _write_payload(build_payload(md_path, rel, prompt))
    log_path = payload_path.with_suffix(".log")
    mode = decision["mode"]
    cwd = decision["cwd"]

    if mode == "codex":
        turn_id = _codex_app_server_send(decision["sessionId"], payload_path.read_text())
        return {
            **decision,
            "payload": str(payload_path),
            "log": str(log_path),
            "status": "sent",
            "turnId": turn_id,
        }

    if mode == "grok":
        try:
            _grok_leader_send(decision["sessionId"], payload_path.read_text(), cwd)
        except DispatchFailed as e:
            if "no Grok leader socket" not in str(e):
                raise
            _grok_cli_resume(decision["sessionId"], cwd, payload_path, log_path)
        return {
            **decision,
            "payload": str(payload_path),
            "log": str(log_path),
            "status": "sent",
        }

    if mode == "message":
        argv = [claude_bin(), "-p", "--model", "sonnet"]
        stdin_text = _courier_prompt(decision["target"], decision["sessionId"], payload_path)
    elif mode == "resume":
        argv = [
            claude_bin(), "--bg", "--model", "opus[1m]",
            "--resume", decision["sessionId"],
            _pointer_prompt(md_path, payload_path),
        ]
        stdin_text = None
    else:
        argv = [
            router_bin(), "run", "--dir", cwd,
            _pointer_prompt(md_path, payload_path),
        ]
        stdin_text = None

    _spawn(argv, cwd, stdin_text, log_path)

    return {
        **decision,
        "payload": str(payload_path),
        "log": str(log_path),
        "status": "sent",
    }
