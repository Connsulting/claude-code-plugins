"""Tests for big-plan feedback routing, including Grok leader delivery."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path


DISPATCH_PATH = Path(__file__).with_name("dispatch.py")
SPEC = importlib.util.spec_from_file_location("big_plan_dispatch", DISPATCH_PATH)
assert SPEC and SPEC.loader
dispatch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dispatch)


def _frame(message: dict) -> bytes:
    body = json.dumps(message).encode("utf-8")
    return struct.pack("!I", len(body)) + body


def _read_frame(sock: socket.socket) -> dict | None:
    prefix = b""
    while len(prefix) < 4:
        chunk = sock.recv(4 - len(prefix))
        if not chunk:
            return None
        prefix += chunk
    size = struct.unpack("!I", prefix)[0]
    body = b""
    while len(body) < size:
        chunk = sock.recv(size - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body)


def _acp(payload: dict) -> dict:
    return {"type": "acp", "payload": json.dumps(payload)}


class FakeGrokLeader(threading.Thread):
    """Minimal Grok leader: register, ACP initialize/list/load/prompt."""

    def __init__(
        self,
        socket_path: Path,
        *,
        sessions: list[dict] | None = None,
        prompt_error: str | None = None,
        load_error: str | None = None,
    ) -> None:
        super().__init__(daemon=True)
        self.socket_path = socket_path
        self.sessions = list(sessions or [])
        self.prompt_error = prompt_error
        self.load_error = load_error
        self.prompts: list[dict] = []
        self.loads: list[dict] = []
        self._ready = threading.Event()
        self._halt = threading.Event()
        self.exception: BaseException | None = None
        self._listener: socket.socket | None = None

    def start_ready(self) -> None:
        self.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("fake Grok leader did not start")

    def stop(self) -> None:
        self._halt.set()
        if self._listener is not None:
            try:
                poke = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                poke.connect(str(self.socket_path))
                poke.close()
            except OSError:
                pass
            try:
                self._listener.close()
            except OSError:
                pass
        self.join(timeout=5)

    def run(self) -> None:
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._listener = listener
            listener.bind(str(self.socket_path))
            listener.listen(4)
            listener.settimeout(0.2)
            self._ready.set()
            while not self._halt.is_set():
                try:
                    conn, _addr = listener.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        except BaseException as e:
            self.exception = e
            self._ready.set()

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(2)
        try:
            while not self._halt.is_set():
                message = _read_frame(conn)
                if message is None:
                    return
                kind = message.get("type")
                if kind == "register":
                    conn.sendall(_frame({
                        "type": "registered",
                        "leader_protocol_version": 1,
                        "leader_capabilities": {"control_v1": True},
                        "ready": True,
                    }))
                    continue
                if kind == "control":
                    conn.sendall(_frame({
                        "type": "control_result",
                        "request_id": message.get("request_id"),
                        "result": {"Ok": {"type": "leader_info"}},
                    }))
                    continue
                if kind != "acp":
                    continue
                payload = json.loads(message["payload"])
                method = payload.get("method")
                req_id = payload.get("id")
                params = payload.get("params") or {}
                if method == "initialize":
                    conn.sendall(_frame(_acp({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"protocolVersion": 1},
                    })))
                elif method == "_x.ai/sessions/list":
                    conn.sendall(_frame(_acp({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"result": {"sessions": self.sessions}},
                    })))
                elif method == "session/load":
                    self.loads.append(params)
                    if self.load_error:
                        conn.sendall(_frame(_acp({
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {"message": self.load_error},
                        })))
                    else:
                        session_id = params.get("sessionId")
                        self.sessions = [s for s in self.sessions if s.get("sessionId") != session_id]
                        self.sessions.append({
                            "sessionId": session_id,
                            "resident": True,
                            "activity": "idle",
                            "cwd": params.get("cwd"),
                        })
                        conn.sendall(_frame(_acp({
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"sessionId": session_id},
                        })))
                elif method == "session/prompt":
                    self.prompts.append(params)
                    if self.prompt_error:
                        conn.sendall(_frame(_acp({
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {"message": self.prompt_error},
                        })))
                    else:
                        session_id = params.get("sessionId")
                        for entry in self.sessions:
                            if entry.get("sessionId") == session_id:
                                entry["resident"] = True
                                entry["activity"] = "working"
                        # Leave the prompt request in-flight so the client
                        # must confirm via the roster, like a live turn.
                elif method == "session/request_permission":
                    continue
        except OSError:
            return
        finally:
            conn.close()


class DispatchGrokTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "grok-home"
        self.home.mkdir()
        self.outbox = self.root / "outbox"
        self.outbox.mkdir()
        self._env = {
            "GROK_HOME": str(self.home),
            "BIG_PLAN_GROK_HOME": str(self.home),
            "BIG_PLAN_OUTBOX": str(self.outbox),
            "BIG_PLAN_EARLY_FAILURE_WINDOW": "0.4",
        }
        self._old_env = {key: os.environ.get(key) for key in self._env}
        os.environ.update(self._env)
        self._old_outbox = dispatch.OUTBOX
        dispatch.OUTBOX = self.outbox
        self.plan = self.root / "plan.md"
        self.plan.write_text("# Plan\n")

    def tearDown(self) -> None:
        dispatch.OUTBOX = self._old_outbox
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        os.environ.pop("BIG_PLAN_GROK_LEADER_SOCKET", None)
        os.environ.pop("BIG_PLAN_GROK_BIN", None)
        self.tmp.cleanup()

    def _write_sidecar(self, engine: str, session_id: str, name: str = "Plan Thread") -> None:
        dispatch.save_provenance(self.plan, engine, session_id, name, str(self.root))

    def _put_grok_session(self, session_id: str, cwd: str) -> None:
        from urllib.parse import quote
        path = self.home / "sessions" / quote(cwd, safe="") / session_id
        path.mkdir(parents=True)
        (path / "summary.json").write_text(json.dumps({
            "info": {"id": session_id, "cwd": cwd},
            "session_summary": "test",
            "created_at": "2026-08-23T00:00:00Z",
            "updated_at": "2026-08-23T00:00:00Z",
        }))

    def test_save_and_load_accepts_grok(self) -> None:
        session_id = "01a02ffa-b079-77e0-8c66-97f9d2e39598"
        record = dispatch.save_provenance(self.plan, "grok", session_id, "Grok Plan", str(self.root))
        self.assertEqual(record["engine"], "grok")
        loaded = dispatch.load_provenance(self.plan)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded["engine"], "grok")
        self.assertEqual(loaded["sessionId"], session_id)

    def test_save_rejects_unknown_engine(self) -> None:
        with self.assertRaises(ValueError):
            dispatch.save_provenance(self.plan, "opencode", "01a02ffa-b079-77e0-8c66-97f9d2e39598", "x", str(self.root))

    def test_route_grok_provenance(self) -> None:
        session_id = "01a02ffa-b079-77e0-8c66-97f9d2e39598"
        self._write_sidecar("grok", session_id)
        decision = dispatch.route(self.plan, self.root)
        self.assertEqual(decision["mode"], "grok")
        self.assertEqual(decision["engine"], "grok")
        self.assertEqual(decision["sessionId"], session_id)
        self.assertIn("Grok", decision["reason"])

    def test_codex_sidecar_for_a_grok_session_routes_to_grok(self) -> None:
        session_id = "01a02ffa-b079-77e0-8c66-97f9d2e39598"
        self._write_sidecar("codex", session_id)
        self._put_grok_session(session_id, str(self.root))
        decision = dispatch.route(self.plan, self.root)
        self.assertEqual(decision["mode"], "grok")
        self.assertEqual(decision["engine"], "grok")

    def test_real_codex_sidecar_stays_codex(self) -> None:
        session_id = "0193b8c0-1234-4000-8000-00000000abcd"
        self._write_sidecar("codex", session_id)
        decision = dispatch.route(self.plan, self.root)
        self.assertEqual(decision["mode"], "codex")
        self.assertEqual(decision["engine"], "codex")

    def test_leader_prompts_a_resident_session(self) -> None:
        session_id = "01a02ffa-b079-77e0-8c66-97f9d2e39598"
        sock = self.home / "leader.sock"
        leader = FakeGrokLeader(sock, sessions=[{
            "sessionId": session_id,
            "resident": True,
            "activity": "idle",
            "title": "Grok Plan",
        }])
        leader.start_ready()
        os.environ["BIG_PLAN_GROK_LEADER_SOCKET"] = str(sock)
        try:
            dispatch._grok_leader_send(session_id, "please triage the comments", str(self.root))
            self.assertEqual(len(leader.prompts), 1)
            self.assertEqual(leader.prompts[0]["sessionId"], session_id)
            text = leader.prompts[0]["prompt"][0]["text"]
            self.assertEqual(text, "please triage the comments")
            self.assertEqual(leader.loads, [])
        finally:
            leader.stop()

    def test_leader_loads_a_session_that_is_not_resident(self) -> None:
        session_id = "01a02ffa-b079-77e0-8c66-97f9d2e39598"
        sock = self.home / "leader.sock"
        leader = FakeGrokLeader(sock, sessions=[])
        leader.start_ready()
        os.environ["BIG_PLAN_GROK_LEADER_SOCKET"] = str(sock)
        try:
            dispatch._grok_leader_send(session_id, "new comments", str(self.root))
            self.assertEqual(len(leader.loads), 1)
            self.assertEqual(leader.loads[0]["sessionId"], session_id)
            self.assertEqual(leader.loads[0]["cwd"], str(self.root))
            self.assertEqual(len(leader.prompts), 1)
        finally:
            leader.stop()

    def test_leader_surfaces_a_prompt_error(self) -> None:
        session_id = "01a02ffa-b079-77e0-8c66-97f9d2e39598"
        sock = self.home / "leader.sock"
        leader = FakeGrokLeader(
            sock,
            sessions=[{"sessionId": session_id, "resident": True, "activity": "working"}],
            prompt_error="detached prompt rejected",
        )
        leader.start_ready()
        os.environ["BIG_PLAN_GROK_LEADER_SOCKET"] = str(sock)
        try:
            with self.assertRaises(dispatch.DispatchFailed) as ctx:
                dispatch._grok_leader_send(session_id, "nope", str(self.root))
            self.assertIn("session/prompt", str(ctx.exception))
            self.assertIn("detached prompt rejected", str(ctx.exception))
        finally:
            leader.stop()

    def test_submit_grok_uses_the_leader(self) -> None:
        session_id = "01a02ffa-b079-77e0-8c66-97f9d2e39598"
        self._write_sidecar("grok", session_id, "Grok Plan")
        sock = self.home / "leader.sock"
        leader = FakeGrokLeader(sock, sessions=[{
            "sessionId": session_id,
            "resident": True,
            "activity": "idle",
        }])
        leader.start_ready()
        os.environ["BIG_PLAN_GROK_LEADER_SOCKET"] = str(sock)
        try:
            result = dispatch.submit(self.plan, "plan.md", self.root, "open comments here")
            self.assertEqual(result["mode"], "grok")
            self.assertEqual(result["status"], "sent")
            self.assertEqual(len(leader.prompts), 1)
            self.assertIn("open comments here", leader.prompts[0]["prompt"][0]["text"])
        finally:
            leader.stop()

    def test_submit_grok_resumes_via_cli_when_no_leader_exists(self) -> None:
        session_id = "01a02ffa-b079-77e0-8c66-97f9d2e39598"
        self._write_sidecar("grok", session_id)
        grok_cli = self.root / "fake-grok"
        log = self.root / "grok-argv.log"
        grok_cli.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$@\" > {log}\n"
            "exit 0\n"
        )
        grok_cli.chmod(0o755)
        os.environ["BIG_PLAN_GROK_BIN"] = str(grok_cli)
        os.environ.pop("BIG_PLAN_GROK_LEADER_SOCKET", None)
        result = dispatch.submit(self.plan, "plan.md", self.root, "please handle these")
        self.assertEqual(result["mode"], "grok")
        self.assertEqual(result["status"], "sent")
        deadline = time.time() + 2
        while time.time() < deadline and not log.exists():
            time.sleep(0.05)
        self.assertTrue(log.exists(), "grok CLI was not invoked")
        argv = log.read_text().splitlines()
        self.assertIn("--resume", argv)
        self.assertIn(session_id, argv)
        self.assertIn("--prompt-file", argv)
        self.assertIn("--always-approve", argv)


if __name__ == "__main__":
    unittest.main()
