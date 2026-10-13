"""Deliver plan feedback through T3 using the installed t3-thread transport.

The session sidecar records a T3 threadId and repository cwd. Provider session
ids do not identify T3 threads. Feedback travels on stdin, never argv or a shell.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib.parse import quote

MAX_PROMPT_BYTES = 256 * 1024
OUTBOX = Path(os.environ.get("BIG_PLAN_OUTBOX", Path.home() / ".local/state/big-plan/outbox"))
LOCAL_BASE_URL = os.environ.get("BIG_PLAN_BASE_URL", "http://localhost:8765")
T3_SKILL = Path.home() / ".claude/skills/t3-thread"

# Reuse the supported launcher's authentication, version checks and RPC client.
# queue_after_active is T3 thread-send's queue mode: an idle thread starts now;
# a busy thread receives a separate follow-up without interrupting its author.
_T3_API_SCRIPT = r"""
import { pathToFileURL } from 'node:url';
import { randomUUID } from 'node:crypto';
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
const load = (name) => import(pathToFileURL(`${input.skill}/lib/${name}.mjs`));
const { resolveContext } = await load('context');
const { withT3Session } = await load('t3cli');
const { connect } = await load('rpc');
const { ensureCompatibleServer } = await load('version');
const { buildMessageDispatch } = await load('commands');
const ctx = resolveContext({});
const timer = setTimeout(() => process.emit('SIGTERM', 'SIGTERM'), 30000);
try {
  const result = await withT3Session({
    bin: ctx.bin, baseDir: ctx.baseDir, ttlMs: 600000, label: 'big-plan'
  }, async (token) => {
    const client = await connect(ctx.origin, token);
    try {
      await ensureCompatibleServer(client, { label: 'big-plan' });
      const snapshot = await client.shellSnapshot();
      const thread = snapshot.threads.find(t => t.id === input.threadId);
      if (!thread || thread.deletedAt != null || thread.archivedAt != null) {
        if (input.action === 'inspect') return { usable: false };
        throw new Error('Authoring T3 thread is no longer available; reload the plan');
      }
      if (input.action === 'inspect') return { usable: true, title: thread.title };
      if (input.action === 'check') {
        await new Promise(resolve => setTimeout(resolve, 2500));
        const projection = await client.call('orchestration.getThreadProjection', { threadId: input.threadId });
        const run = projection.runs.at(-1);
        if (!run || ['failed', 'cancelled', 'interrupted', 'rolled_back'].includes(run.status)) {
          throw new Error('T3 feedback run failed during startup');
        }
        return { threadId: input.threadId, runId: run.id };
      }
      const messageId = randomUUID();
      const command = buildMessageDispatch({
        threadId: input.threadId, messageId, text: input.payload,
        modelSelection: thread.modelSelection
      });
      command.dispatchMode = { type: 'queue_after_active' };
      await client.call('orchestration.dispatchCommand', command);
      await new Promise(resolve => setTimeout(resolve, 2500));
      const projection = await client.call('orchestration.getThreadProjection', {
        threadId: input.threadId
      });
      const run = projection.runs.find(r => r.userMessageId === messageId);
      if (!run || ['failed', 'cancelled', 'interrupted', 'rolled_back'].includes(run.status)) {
        throw new Error('T3 did not accept the feedback run');
      }
      return { threadId: input.threadId, messageId, runId: run.id, delivery: run.status === 'queued' ? 'queued' : 'started' };
    } finally { client.close(); }
  });
  process.stdout.write(JSON.stringify(result) + '\n');
} catch (error) {
  process.stderr.write(`big-plan: ${error.message}\n`);
  process.exitCode = 1;
} finally { clearTimeout(timer); }
"""


class DispatchFailed(RuntimeError):
    """T3 failed to accept the feedback."""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def provenance_path(md_path: Path) -> Path:
    return md_path.with_suffix(md_path.suffix + ".session")


def _valid_thread_id(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and not any(
        c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value
    )


def load_provenance(md_path: Path) -> dict | None:
    try:
        data = json.loads(provenance_path(md_path).read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    thread_id = data.get("threadId")
    return {
        "threadId": thread_id if _valid_thread_id(thread_id) else None,
        "name": str(data.get("name") or "").strip(),
        "cwd": str(data.get("cwd") or "").strip(),
        "recordedAt": str(data.get("recordedAt") or "").strip(),
    }


def save_provenance(md_path: Path, thread_id: str | None, name: str, cwd: str) -> dict:
    if thread_id is not None and not _valid_thread_id(thread_id):
        raise ValueError("threadId must be a nonempty T3 thread ID without whitespace (up to 256 characters)")
    if not cwd or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise ValueError("cwd must be an existing absolute repository directory")
    record = {"threadId": thread_id, "name": name.strip(), "cwd": cwd.strip(), "recordedAt": now_iso()}
    path = provenance_path(md_path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
    return record


def _fallback_dir(md_path: Path, root: Path) -> str:
    state_root = (Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "big-plan/plans").resolve()
    if md_path.resolve().is_relative_to(state_root):
        raise ValueError("External plans require registration with the repository cwd")
    for parent in md_path.parents:
        if (parent / ".git").exists():
            return str(parent)
        if parent == root:
            break
    return str(root)


def _node_bin() -> str:
    candidates = [shutil.which("node")]
    candidates.extend(str(p) for p in sorted(
        (Path.home() / ".nvm/versions/node").glob("v*/bin/node"),
        key=lambda p: tuple(int(n) for n in p.parts[-3][1:].split(".")), reverse=True,
    ))
    for candidate in candidates:
        if candidate:
            result = subprocess.run([candidate, "-p", "process.versions.node.split('.')[0]"],
                                    check=False, capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and int(result.stdout.strip()) >= 22:
                return candidate
    raise DispatchFailed("t3-thread requires Node >= 22 on PATH or under ~/.nvm/versions/node")


def _run(argv: list[str], stdin_text: str) -> dict:
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    except OSError as error:
        raise DispatchFailed(f"Could not start T3 transport: {error}") from error
    try:
        stdout, stderr = proc.communicate(stdin_text, timeout=40)
    except subprocess.TimeoutExpired as error:
        # The supported transport revokes its short-lived token on SIGTERM.
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        raise DispatchFailed("T3 delivery timed out; check T3 before retrying") from error
    if proc.returncode != 0:
        detail = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", stderr).strip()
        raise DispatchFailed(detail[-1000:] or f"T3 transport exited {proc.returncode}")
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise DispatchFailed("T3 transport returned invalid JSON; check T3 before retrying") from error
    if not isinstance(result, dict):
        raise DispatchFailed("T3 transport returned no delivery result")
    return result


def _t3_api(action: str, thread_id: str, payload: str = "") -> dict:
    return _run([_node_bin(), "--input-type=module", "-e", _T3_API_SCRIPT],
                json.dumps({"skill": str(T3_SKILL), "action": action,
                            "threadId": thread_id, "payload": payload}))


def route(md_path: Path, root: Path) -> dict:
    prov = load_provenance(md_path) or {}
    cwd = prov.get("cwd") or _fallback_dir(md_path, root)
    thread_id = prov.get("threadId")
    if thread_id:
        thread = _t3_api("inspect", thread_id)
        if thread.get("usable"):
            return {"mode": "thread", "target": thread.get("title") or prov.get("name") or thread_id,
                    "sessionId": thread_id, "cwd": cwd,
                    "reason": "send to the authoring T3 thread"}
    return {"mode": "new_thread", "target": None, "sessionId": None, "cwd": cwd,
            "reason": "no usable authoring T3 thread recorded"}


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
        "Big Plan as a T3 thread message.\n"
        f"\nPlan file:       {md_path}\n"
        f"Served path:     /{rel}\n"
        f"Comment sidecar: {sidecar}\n"
        "\n---\n\n"
        f"{prompt.rstrip()}\n"
        "\n---\n\n"
        "Before you edit the plan, set the diff baseline so your edits highlight "
        "for the reviewer:\n    cp "
        # Quoted because the receiving agent runs this verbatim, and plan paths
        # in persistent plan storage routinely contain spaces.
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


def submit(md_path: Path, rel: str, root: Path, prompt: str, decision: dict | None = None) -> dict:
    """Deliver through the route already checked against the client's preview."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt required")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("prompt too large")
    decision = decision if decision is not None else route(md_path, root)
    payload = build_payload(md_path, rel, prompt)
    payload_path = _write_payload(payload)
    if decision["mode"] == "thread":
        result = _t3_api("send", decision["sessionId"], payload)
    elif decision["mode"] == "new_thread":
        result = _run([str(T3_SKILL / "t3-thread"), "--project", decision["cwd"],
                       "--prompt", "-", "--title", f"Review feedback: {md_path.stem}", "--json"], payload)
        if _valid_thread_id(result.get("threadId")):
            result.update(_t3_api("check", result["threadId"]))
    else:
        raise ValueError("unknown T3 delivery mode")
    if not _valid_thread_id(result.get("threadId")):
        raise DispatchFailed("T3 transport returned no thread ID; check T3 before retrying")
    return {**decision, **result, "status": "sent", "payload": str(payload_path)}
