"""Write-time idempotency contract (Stream B / AC2).

The observable demand of AC2 is narrow and hard: a learning write that
near-duplicates an existing entry must provably UPDATE the existing file rather
than create a new sibling, and the test proving it must FAIL if the dedupe is
removed. Three duplicate files landed in 11 days under a prose instruction to
the generator agent, so enforcement lives in `scripts/dedupe-on-write.py`, wired
into the deterministic new-file loop of `hooks/extract-learnings.sh`.

Two constraints on that script are decisions, not implementation detail, and
each has a test here:

  * it must stay CHEAP -- one vector query, no LLM call, no network -- because
    it runs once per generated file inside a hook;
  * it must FAIL OPEN -- a dedupe that crashes or prints garbage must degrade to
    exactly today's behaviour (index the file), never to a lost learning.

The script is driven as its own CLI, in-process, by executing the file under the
name `__main__`. That exercises argument parsing, the JSON contract the hook
parses, and the exit code, without binding these tests to any function name --
and unlike a subprocess it can be monkeypatched, which the no-network test
requires.
"""

import contextlib
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import lib.db as db  # noqa: E402

DEDUPE_SCRIPT = PLUGIN_ROOT / "scripts" / "dedupe-on-write.py"
EXTRACT_HOOK = PLUGIN_ROOT / "hooks" / "extract-learnings.sh"


# --- fixture content --------------------------------------------------------
#
# EXISTING and NEAR_DUPLICATE describe the same defect in near-identical prose:
# the shape of the same-day global/repo pair and of the 11-day re-derivation
# that motivated AC2. DISTINCT is a different subsystem entirely and must
# survive as its own file.

EXISTING = """# npm overrides cannot fix bundled dependencies

**Type:** gotcha
**Topic:** dependency-management
**Tags:** npm, lockfile, vulnerability

## Problem
An `overrides` block in package.json is silently ignored for a transitive
dependency that the parent package ships bundled inside its own published
tarball. The lockfile shows the pinned, patched version while node_modules still
contains the vulnerable bundled copy, so the audit stays red after the override
lands. EXISTING-BODY-MARKER

## Solution
Patch or fork the parent package, or wait for the maintainer to unbundle the
dependency. An override alone cannot reach inside a bundled tarball.
"""

NEAR_DUPLICATE = """# npm overrides are a no-op on bundled dependencies

**Type:** gotcha
**Topic:** dependency-management
**Tags:** npm, lockfile, vulnerability

## Problem
Adding an `overrides` entry to package.json does nothing for a transitive
dependency that its parent package bundles into the published tarball. The
lockfile reports the pinned version while the vulnerable bundled copy is still
what ends up in node_modules, so npm audit keeps reporting the advisory.
NEWFILE-BODY-MARKER

## Solution
Fork or patch the parent package so the bundled copy is replaced. Overrides
cannot rewrite the contents of a bundled tarball.
"""

DISTINCT = """# Postgres autovacuum stalls behind a long-running transaction

**Type:** gotcha
**Topic:** database
**Tags:** postgres, vacuum, transactions

## Problem
Autovacuum cannot reclaim dead tuples newer than the oldest running
transaction's snapshot, so one forgotten idle-in-transaction session pins the
xmin horizon and table bloat grows without bound while vacuum reports success.
NEWFILE-BODY-MARKER

## Solution
Alert on the age of the oldest transaction and set idle_in_transaction_session
timeout, rather than tuning autovacuum thresholds.
"""


# --- harness ----------------------------------------------------------------


@pytest.fixture()
def corpus(isolated_db, tmp_path, monkeypatch):
    """Isolated DB + learnings dir, exported so the script's own load_config finds them.

    The assertion is a hard safety gate: the shipped
    `.claude-plugin/config.json` points dbPath at the live 126MB corpus, so a
    missing env override here would have these tests rewriting real learnings.
    """
    config, conn = isolated_db
    learnings = tmp_path / "learnings"
    learnings.mkdir(exist_ok=True)

    monkeypatch.setenv("SQLITE_DB_PATH", config["sqlite"]["dbPath"])
    monkeypatch.setenv("LEARNINGS_GLOBAL_DIR", str(learnings))

    resolved = db.load_config()
    assert resolved["sqlite"]["dbPath"].startswith(str(tmp_path)), (
        "refusing to run: the resolved dbPath is not inside tmp_path"
    )
    return config, conn, learnings


def _index(conn, doc_id: str, path: Path, content: str, scope: str = "global",
           repo: str = "") -> None:
    db.upsert_document(
        conn, doc_id, content,
        {
            "scope": scope,
            "repo": repo,
            "file_path": str(path),
            "topic": "other",
            "keywords": "",
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _run_dedupe(args):
    """Run scripts/dedupe-on-write.py as its own CLI, in-process.

    Returns (exit_code, parsed_json_or_None, raw_stdout).
    """
    if not DEDUPE_SCRIPT.exists():
        pytest.fail(f"{DEDUPE_SCRIPT} does not exist yet (Stream B has not landed)")

    spec = importlib.util.spec_from_file_location("__main__", DEDUPE_SCRIPT)
    module = importlib.util.module_from_spec(spec)

    buf = io.StringIO()
    exit_code = 0
    saved_argv = sys.argv
    sys.argv = ["dedupe-on-write.py", *args]
    try:
        with contextlib.redirect_stdout(buf):
            try:
                spec.loader.exec_module(module)
            except SystemExit as exc:
                if exc.code is None:
                    exit_code = 0
                elif isinstance(exc.code, int):
                    exit_code = exc.code
                else:
                    exit_code = 1
    finally:
        sys.argv = saved_argv

    raw = buf.getvalue()
    payload = None
    for line in reversed([ln.strip() for ln in raw.splitlines() if ln.strip()]):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    return exit_code, payload, raw


# --- AC2's core -------------------------------------------------------------


def test_near_duplicate_is_absorbed_into_existing_file(corpus) -> None:
    """The new sibling must vanish and the existing file must carry both bodies.

    This is the test AC2 demands fail when the dedupe is removed: without it the
    new path simply stays on disk and gets indexed as row two.
    """
    _config, conn, learnings = corpus
    existing = learnings / "npm-overrides-bundled-deps-2026-07-01.md"
    existing.write_text(EXISTING, encoding="utf-8")
    _index(conn, "existing", existing, EXISTING)

    new = learnings / "npm-overrides-bundled-deps-2026-08-01.md"
    new.write_text(NEAR_DUPLICATE, encoding="utf-8")
    rows_before = db.count_documents(conn)

    exit_code, payload, raw = _run_dedupe([str(new), "--json"])

    assert exit_code == 0, raw
    assert payload is not None, f"expected a JSON verdict on stdout, got: {raw!r}"
    assert payload.get("action") == "absorbed", payload
    assert payload.get("into") == str(existing), payload

    assert not new.exists(), "the near-duplicate sibling must be removed"
    survivor = existing.read_text(encoding="utf-8")
    assert "EXISTING-BODY-MARKER" in survivor, "the original body was destroyed"
    assert "NEWFILE-BODY-MARKER" in survivor, "the new body was discarded instead of merged"
    assert db.count_documents(conn) == rows_before, (
        "absorbing must not add a corpus row"
    )


def test_distinct_learning_is_kept(corpus) -> None:
    """An unrelated learning survives as its own file.

    Without this, the test above passes trivially by absorbing everything -- and
    a cosine-direction inversion (distance vs similarity) fails here loudly
    instead of silently eating the corpus.
    """
    _config, conn, learnings = corpus
    existing = learnings / "npm-overrides-bundled-deps-2026-07-01.md"
    existing.write_text(EXISTING, encoding="utf-8")
    _index(conn, "existing", existing, EXISTING)

    new = learnings / "postgres-autovacuum-stall-2026-08-01.md"
    new.write_text(DISTINCT, encoding="utf-8")

    exit_code, payload, raw = _run_dedupe([str(new), "--json"])

    assert exit_code == 0, raw
    assert payload is not None, f"expected a JSON verdict on stdout, got: {raw!r}"
    assert payload.get("action") == "kept", payload
    assert new.exists(), "an unrelated learning must not be absorbed"
    assert new.read_text(encoding="utf-8") == DISTINCT
    assert existing.read_text(encoding="utf-8") == EXISTING, (
        "a kept learning must not be appended to anything"
    )


def test_same_day_global_and_repo_duplicate_is_not_merged_across_scopes(
    corpus, tmp_path
) -> None:
    """The same-day global/repo pair must NOT be collapsed across scopes.

    THIS IS A CONFIDENTIALITY TEST, not a dedupe test. Absorption appends the new
    body verbatim into the target and re-indexes the survivor under the TARGET's
    scope, so absorbing a repo-scoped learning into a global one promotes one
    client's repo names, service names and internal identifiers into the global
    corpus, which auto-peek injects into EVERY other client's session. `db.search`
    always ORs in `l.scope = 'global'`, so the global copy is offered as the
    nearest candidate on every repo-scoped write; this test exists to prove the
    write path refuses it.

    AC2's "stop writing the identical learning to both scopes on the same day" is
    satisfied by picking ONE scope at write time in the generation prompt, never
    by merging content across scopes afterwards.
    """
    _config, conn, learnings = corpus
    global_file = learnings / "npm-overrides-bundled-deps-2026-08-01.md"
    global_file.write_text(EXISTING, encoding="utf-8")
    _index(conn, "global-copy", global_file, EXISTING)

    repo_learnings = tmp_path / "myrepo" / ".projects" / "learnings"
    repo_learnings.mkdir(parents=True)
    repo_file = repo_learnings / "npm-overrides-bundled-deps-2026-08-01.md"
    repo_file.write_text(NEAR_DUPLICATE, encoding="utf-8")
    rows_before = db.count_documents(conn)

    exit_code, payload, raw = _run_dedupe([str(repo_file), "--json"])

    assert exit_code == 0, raw
    assert payload is not None, f"expected a JSON verdict on stdout, got: {raw!r}"
    assert payload.get("action") == "kept", payload
    assert "scope" in (payload.get("reason") or ""), (
        f"the refusal must name the boundary it enforced: {payload!r}"
    )
    assert repo_file.exists(), "the repo-scoped learning must survive as its own file"
    assert repo_file.read_text(encoding="utf-8") == NEAR_DUPLICATE
    survivor = global_file.read_text(encoding="utf-8")
    assert "NEWFILE-BODY-MARKER" not in survivor, (
        "repo-scoped content was promoted into the global corpus"
    )
    assert survivor == EXISTING
    assert db.count_documents(conn) == rows_before


def test_near_duplicate_in_the_same_repo_is_absorbed(corpus, tmp_path) -> None:
    """Positive control: the boundary blocks crossings, not same-scope dedupe.

    Without this, refusing every repo-scoped absorb would pass the test above.
    """
    _config, conn, _learnings = corpus
    repo_learnings = tmp_path / "myrepo" / ".projects" / "learnings"
    repo_learnings.mkdir(parents=True)

    existing = repo_learnings / "npm-overrides-bundled-deps-2026-07-01.md"
    existing.write_text(EXISTING, encoding="utf-8")
    _index(conn, "repo-existing", existing, EXISTING, scope="repo", repo="myrepo")

    new = repo_learnings / "npm-overrides-bundled-deps-2026-08-01.md"
    new.write_text(NEAR_DUPLICATE, encoding="utf-8")

    exit_code, payload, raw = _run_dedupe([str(new), "--json"])

    assert exit_code == 0, raw
    assert payload is not None, f"expected a JSON verdict on stdout, got: {raw!r}"
    assert payload.get("action") == "absorbed", payload
    assert payload.get("into") == str(existing), payload
    assert not new.exists()
    survivor = existing.read_text(encoding="utf-8")
    assert "EXISTING-BODY-MARKER" in survivor
    assert "NEWFILE-BODY-MARKER" in survivor



def _load_dedupe_module():
    """Import the script as an ordinary module, so its functions are callable.

    Loaded under its own name rather than `__main__`, so the `if __name__`
    guard leaves `main()` alone. The CLI harness above stays the default; this
    exists for the concurrency test, which needs two threads inside one process
    and cannot share `sys.argv` and `redirect_stdout` between them.
    """
    spec = importlib.util.spec_from_file_location("dedupe_on_write", DEDUPE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stray_temp_files(directory: Path):
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


def test_concurrent_absorbs_into_one_survivor_lose_neither(corpus, monkeypatch) -> None:
    """Two absorbs racing on one survivor must both land, or neither is safe.

    The hook runs this per generated file and several background jobs write
    learnings at once on this machine, so two sessions really do select the same
    survivor concurrently. Unserialized, both read the pre-merge body and the
    second rename overwrites the first merge -- while the first source file has
    already been deleted, making that learning unrecoverable from disk and from
    the corpus at once.

    The interleaving is forced rather than hoped for: the first absorb to reach
    `os.replace` stalls inside its critical section, which is exactly the window
    the second absorb has to be excluded from. Remove the lock and this test
    fails deterministically with one marker missing.
    """
    _config, conn, learnings = corpus
    existing = learnings / "npm-overrides-bundled-deps-2026-07-01.md"
    existing.write_text(EXISTING, encoding="utf-8")
    _index(conn, "existing", existing, EXISTING)

    first = learnings / "npm-overrides-bundled-deps-2026-08-01.md"
    first.write_text(
        NEAR_DUPLICATE.replace("NEWFILE-BODY-MARKER", "FIRST-BODY-MARKER"),
        encoding="utf-8",
    )
    second = learnings / "npm-overrides-bundled-deps-2026-08-02.md"
    second.write_text(
        NEAR_DUPLICATE.replace("NEWFILE-BODY-MARKER", "SECOND-BODY-MARKER"),
        encoding="utf-8",
    )
    rows_before = db.count_documents(conn)

    module = _load_dedupe_module()

    real_replace = os.replace
    stalled = threading.Lock()
    already_stalled = []

    def stalling_replace(src, dst):
        with stalled:
            mine = not already_stalled
            already_stalled.append(True)
        if mine:
            time.sleep(0.5)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", stalling_replace)

    start = threading.Barrier(2)
    verdicts = {}
    failures = {}

    def run(path: Path) -> None:
        try:
            start.wait(timeout=10)
            verdicts[path.name] = module.decide(str(path), 0.88, False)
        except BaseException as exc:  # a raise here is a lost learning, not a skip
            failures[path.name] = repr(exc)

    threads = [threading.Thread(target=run, args=(p,)) for p in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not failures, failures
    assert [v.get("action") for v in verdicts.values()] == ["absorbed", "absorbed"], (
        f"both racing writes must be absorbed, got {verdicts!r}"
    )

    survivor = existing.read_text(encoding="utf-8")
    assert "EXISTING-BODY-MARKER" in survivor, "the original body was destroyed"
    assert "FIRST-BODY-MARKER" in survivor, "one racing merge was overwritten by the other"
    assert "SECOND-BODY-MARKER" in survivor, "one racing merge was overwritten by the other"
    assert not first.exists() and not second.exists()
    assert _stray_temp_files(learnings) == [], (
        f"a temp file was left behind: {_stray_temp_files(learnings)}"
    )
    assert db.count_documents(conn) == rows_before
    assert "FIRST-BODY-MARKER" in conn.execute(
        "SELECT content FROM learnings WHERE id = 'existing'"
    ).fetchone()["content"], "the indexed survivor does not match what is on disk"


def test_a_wedged_lock_falls_open_instead_of_blocking_the_write(corpus) -> None:
    """A stuck holder costs this write its dedupe, never the hook behind it.

    Fail open is the whole failure policy of this script, and a lock is the one
    thing that can violate it by waiting forever instead of erroring.
    """
    _config, conn, learnings = corpus
    existing = learnings / "npm-overrides-bundled-deps-2026-07-01.md"
    existing.write_text(EXISTING, encoding="utf-8")
    _index(conn, "existing", existing, EXISTING)

    new = learnings / "npm-overrides-bundled-deps-2026-08-01.md"
    new.write_text(NEAR_DUPLICATE, encoding="utf-8")

    module = _load_dedupe_module()
    module.LOCK_TIMEOUT_SECONDS = 0.2

    with module.survivor_lock(str(existing)) as held:
        assert held, "harness precondition: the test must own the lock first"
        verdict = module.decide(str(new), 0.88, False)

    assert verdict.get("action") == "kept", verdict
    assert new.exists(), "a lock we could not take must never cost the file"
    assert new.read_text(encoding="utf-8") == NEAR_DUPLICATE
    assert existing.read_text(encoding="utf-8") == EXISTING


def test_source_survives_a_database_failure_after_the_merge_is_written(
    corpus, monkeypatch
) -> None:
    """The content must always exist in a file or a committed row, never neither.

    Removing the source before the reindex made a failing `upsert_document` fatal
    in a way the fail-open handler could not see: the verdict said `kept`, the
    hook then tried to index a path that was gone, and the survivor's row still
    held the pre-merge body -- so the absorbed learning was reachable from
    nothing.
    """
    _config, conn, learnings = corpus
    existing = learnings / "npm-overrides-bundled-deps-2026-07-01.md"
    existing.write_text(EXISTING, encoding="utf-8")
    _index(conn, "existing", existing, EXISTING)

    new = learnings / "npm-overrides-bundled-deps-2026-08-01.md"
    new.write_text(NEAR_DUPLICATE, encoding="utf-8")
    rows_before = db.count_documents(conn)

    def exploding_upsert(*_args, **_kwargs):
        raise RuntimeError("simulated reindex failure")

    monkeypatch.setattr(db, "upsert_document", exploding_upsert)

    exit_code, payload, raw = _run_dedupe([str(new), "--json"])

    assert exit_code == 0, raw
    assert payload is not None, f"expected a JSON verdict on stdout, got: {raw!r}"
    assert payload.get("action") == "kept", payload
    assert new.exists(), "the source was removed before the reindex succeeded"
    assert new.read_text(encoding="utf-8") == NEAR_DUPLICATE
    assert "NEWFILE-BODY-MARKER" in existing.read_text(encoding="utf-8")
    assert "EXISTING-BODY-MARKER" in existing.read_text(encoding="utf-8")
    assert _stray_temp_files(learnings) == []
    assert db.count_documents(conn) == rows_before


def test_global_dir_inside_a_repo_is_still_global_scope(
    corpus, tmp_path, monkeypatch
) -> None:
    """`learnings.globalDir` decides scope, exactly as the indexer decides it.

    With globalDir pointed at a `.projects/learnings` directory inside another
    Git repo -- a supported configuration -- the indexer stores those files as
    global while path-based inference called each new one repo scoped. Every
    indexed sibling was then refused as a cross-scope match and write-time
    dedupe silently stopped working for that entire corpus.
    """
    _config, conn, _learnings = corpus
    host_repo_learnings = tmp_path / "hostrepo" / ".projects" / "learnings"
    host_repo_learnings.mkdir(parents=True)
    monkeypatch.setenv("LEARNINGS_GLOBAL_DIR", str(host_repo_learnings))

    existing = host_repo_learnings / "npm-overrides-bundled-deps-2026-07-01.md"
    existing.write_text(EXISTING, encoding="utf-8")
    # Indexed the way index-learnings.py would index it: under globalDir, so global.
    _index(conn, "existing", existing, EXISTING, scope="global")

    new = host_repo_learnings / "npm-overrides-bundled-deps-2026-08-01.md"
    new.write_text(NEAR_DUPLICATE, encoding="utf-8")

    exit_code, payload, raw = _run_dedupe([str(new), "--json"])

    assert exit_code == 0, raw
    assert payload is not None, f"expected a JSON verdict on stdout, got: {raw!r}"
    assert payload.get("action") == "absorbed", payload
    assert payload.get("into") == str(existing), payload
    assert not new.exists()
    survivor = existing.read_text(encoding="utf-8")
    assert "EXISTING-BODY-MARKER" in survivor
    assert "NEWFILE-BODY-MARKER" in survivor


def test_dedupe_makes_no_network_call_and_no_llm_call(corpus, monkeypatch) -> None:
    """The write path stays cheap: one vector query, no LLM, no network.

    Any socket connect, urlopen, or child process is fatal. This is a decided
    constraint, not an optimization: the script runs once per generated file
    inside an async hook, and an LLM adjudication step there is the design that
    was explicitly rejected.
    """
    _config, conn, learnings = corpus
    existing = learnings / "npm-overrides-bundled-deps-2026-07-01.md"
    existing.write_text(EXISTING, encoding="utf-8")
    _index(conn, "existing", existing, EXISTING)

    new = learnings / "npm-overrides-bundled-deps-2026-08-01.md"
    new.write_text(NEAR_DUPLICATE, encoding="utf-8")

    def _no_network(*_args, **_kwargs):
        raise AssertionError("dedupe-on-write must not open a network connection")

    def _no_subprocess(*_args, **_kwargs):
        raise AssertionError("dedupe-on-write must not spawn a subprocess (no LLM call)")

    monkeypatch.setattr(socket.socket, "connect", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    monkeypatch.setattr(subprocess, "Popen", _no_subprocess)
    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    monkeypatch.setattr(os, "system", _no_subprocess)
    monkeypatch.setattr(os, "popen", _no_subprocess)

    exit_code, payload, raw = _run_dedupe([str(new), "--json"])

    assert exit_code == 0, raw
    assert payload is not None, f"expected a JSON verdict on stdout, got: {raw!r}"
    assert payload.get("action") == "absorbed", payload
    assert not new.exists()


# --- the hook loop ----------------------------------------------------------


def _write_transcript(path: Path) -> None:
    lines = []
    for i in range(14):
        lines.append(json.dumps({
            "type": "user",
            "message": {"content": f"User turn {i} about npm overrides and bundled deps"},
        }))
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"content": [{
                "type": "text",
                "text": f"Assistant turn {i} explaining the bundled tarball behaviour",
            }]},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_extract_hook(tmp_path, dedupe_stub_case: str):
    """Drive hooks/extract-learnings.sh with a stubbed generator and dedupe.

    Only the python3 dispatch and the generator are stubbed; the loop under test
    is the hook's own shell. Returns (result, generated_path, index_calls_path).
    """
    home = tmp_path / "home"
    (home / ".claude" / "plugins" / "compound-learning").mkdir(parents=True)
    global_dir = home / ".projects" / "learnings"
    global_dir.mkdir(parents=True)

    workdir = tmp_path / "work"
    workdir.mkdir()

    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript)

    generated = global_dir / "hook-generated-learning-2026-08-01.md"
    index_calls = tmp_path / "index-calls.txt"
    dedupe_calls = tmp_path / "dedupe-calls.txt"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    claude_stub = bin_dir / "claude"
    claude_stub.write_text(
        """#!/bin/sh
cat >/dev/null
printf '# Hook generated learning\\n\\nbody\\n' > "$GENERATED_FILE"
printf 'wrote one learning\\n'
""",
        encoding="utf-8",
    )
    claude_stub.chmod(0o755)

    python_stub = bin_dir / "python3"
    python_stub.write_text(
        f"""#!/bin/sh
case "$1" in
  */extract-transcript-messages.py)
    exec "$REAL_PYTHON" "$@"
    ;;
  */dedupe-on-write.py)
    printf '%s\\n' "$*" >> "$DEDUPE_CALLS_FILE"
{dedupe_stub_case}
    ;;
  */index-learnings.py)
    printf '%s\\n' "$*" >> "$INDEX_CALLS_FILE"
    exit 0
    ;;
esac
printf 'unexpected python3 call: %s\\n' "$*" >&2
exit 2
""",
        encoding="utf-8",
    )
    python_stub.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
        "REAL_PYTHON": sys.executable,
        "GENERATED_FILE": str(generated),
        "INDEX_CALLS_FILE": str(index_calls),
        "DEDUPE_CALLS_FILE": str(dedupe_calls),
    }
    env.pop("CLAUDE_SUBPROCESS", None)
    env.pop("COMPOUND_LEARNING_GENERATION_ENGINE", None)

    hook_input = {
        "session_id": "dedupe-hook-session",
        "transcript_path": str(transcript),
        "cwd": str(workdir),
    }

    result = subprocess.run(
        ["bash", str(EXTRACT_HOOK)],
        input=json.dumps(hook_input),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    return result, generated, index_calls, dedupe_calls


def test_hook_loop_skips_indexing_after_absorb(tmp_path) -> None:
    """An absorbed file must not be handed to the indexer -- it no longer exists.

    The dedupe script re-indexes the survivor itself, so indexing the vanished
    path would either fail or, worse, resurrect a row for a deleted file.
    """
    absorb_case = """    rm -f "$2"
    printf '%s\\n' '{"action":"absorbed","into":"/tmp/some-survivor.md"}'
    exit 0"""

    result, generated, index_calls, dedupe_calls = _run_extract_hook(tmp_path, absorb_case)

    assert result.returncode == 0, result.stderr
    assert dedupe_calls.exists(), (
        "the hook's new-file loop must invoke dedupe-on-write.py before indexing"
    )
    assert str(generated) in dedupe_calls.read_text(), (
        f"dedupe was not called for {generated}: {dedupe_calls.read_text()!r}"
    )
    assert not generated.exists(), (
        "harness precondition: the stubbed dedupe should have removed the generated file"
    )
    assert not index_calls.exists() or str(generated) not in index_calls.read_text(), (
        "indexing must be skipped for an absorbed file"
    )


def test_dedupe_failure_falls_open_to_indexing(tmp_path) -> None:
    """A broken dedupe degrades to today's behaviour, never to a lost learning.

    Also the positive control for the test above: it proves this harness reaches
    the hook's new-file loop and does invoke the indexer when nothing intercepts.
    """
    broken_case = """    printf 'Traceback (most recent call last):\\n' >&2
    printf 'this is not json\\n'
    exit 1"""

    result, generated, index_calls, dedupe_calls = _run_extract_hook(tmp_path, broken_case)

    assert result.returncode == 0, result.stderr
    assert dedupe_calls.exists() and str(generated) in dedupe_calls.read_text(), (
        "the dedupe must actually be attempted, or 'falls open' is vacuous: "
        f"{dedupe_calls.read_text() if dedupe_calls.exists() else '<never called>'}"
    )
    assert generated.exists(), "a failed dedupe must not remove the generated file"
    assert index_calls.exists(), "a failed dedupe must fall through to indexing"
    assert str(generated) in index_calls.read_text(), (
        f"indexer was not called for {generated}: {index_calls.read_text()!r}"
    )
