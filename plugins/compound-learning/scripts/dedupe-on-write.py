#!/usr/bin/env python3
"""
Absorb a freshly written learning into an existing near-duplicate, at write time.

Why this exists: three files describing the same npm fact landed in 11 days, three
describing the same teammate fact landed in 2 days, and the same learning was
written to both the global and the repo scope on the same day. The only guidance
against that was prose in `agents/learning-writer.md` telling the generator not to
duplicate, which is exactly the design that failed. Enforcement therefore lives
here, in a script the shell loop of `hooks/extract-learnings.sh` runs for every
newly created file before it is indexed, where the generating model cannot skip it.

The check is deliberately CHEAP: one embedding of the new file, one KNN query
against `vec_learnings`, no LLM adjudication and no network call. It runs once per
generated file inside an async hook, so an expensive verdict here would be paid on
every write.

It is also deliberately FAIL OPEN. Every outcome, including an internal error,
exits 0 and prints a verdict. Losing a learning to a broken dedupe is worse than
keeping a duplicate, so anything unexpected degrades to "kept" and the caller
indexes the file exactly as it does today.

Run:
  python3 dedupe-on-write.py <new_file_path>            # human readable verdict
  python3 dedupe-on-write.py <new_file_path> --json     # machine readable verdict
  python3 dedupe-on-write.py <new_file_path> --dry-run  # decide, change nothing
"""
import argparse
import contextlib
import fcntl
import json
import os
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone

_PLUGIN_ROOT = os.environ.get(
    'CLAUDE_PLUGIN_ROOT',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
sys.path.insert(0, _PLUGIN_ROOT)

import lib._site_packages  # noqa: F401

import lib.db as db

# Cosine SIMILARITY floor, higher is closer. Not a distance, and not directly
# comparable to what `db.search` returns: the `vec_learnings` table is declared
# without a distance metric, so sqlite-vec uses L2, and `db.search` halves it.
# Embeddings are unit-normalized (`get_embedding` passes normalize_embeddings),
# so for a returned `d` the cosine is `1 - 2 * d ** 2`. See `cosine_from`.
# Measured on the two fixture bodies that motivated this script, the
# near-duplicate pair sits at 0.9309 and an unrelated learning at 0.1925, so 0.88
# has room on both sides.
DEFAULT_COSINE_THRESHOLD = 0.88

# Absorption appends, so a popular topic could otherwise grow one file without
# bound. Past this size the sibling is kept instead, which is a visible duplicate
# rather than an unreadable megafile. The corpus tail today runs to ~44,000 chars.
MAX_ABSORBED_CHARS = 60000

# Number of nearest neighbours considered. Only the closest usable one matters;
# the extra slots exist so the new file's own row, if it is somehow already
# indexed, cannot crowd out the real candidate.
KNN_RESULTS = 5

# How long an absorb waits for another absorb to release the same survivor.
# Bounded on purpose: a hung or stalled holder must cost this write a dedupe,
# never the hook that is waiting on it, so the wait expires into the ordinary
# "kept" verdict and the caller indexes the file exactly as it does today.
# The guarded section is a read, a rename and two small commits, so anything
# still holding the lock after this is not making progress.
LOCK_TIMEOUT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.05


def cosine_from(distance: float) -> float:
    """Cosine similarity behind a `db.search` distance.

    `db.search` returns `l2 / 2` for unit-normalized vectors, and
    `l2 ** 2 == 2 - 2 * cos`, so `cos == 1 - 2 * (l2 / 2) ** 2`. Getting this
    inverted would absorb everything or nothing; `test_distinct_learning_is_kept`
    is the guard.
    """
    return round(1.0 - 2.0 * float(distance) ** 2, 4)


def repo_root_for(path: str) -> str:
    """Repo root owning *path*, or '' when the file is not inside a repo.

    The derivation itself is `db._derive_repo_root`, deliberately NOT a second
    copy of it. `boundary_refusal` compares this value against the stored
    `learnings.repo_root`, which that function wrote, so any divergence makes the
    scope query match zero rows and the dedupe degrades to global-only in
    silence. A previous copy here used `find` instead of `rfind` and skipped
    `git_utils.resolve_repo_root`, so every worktree-authored repo learning
    derived `<repo>/.claude/worktrees/x` against a stored `<repo>`.

    The only thing layered on top is the scope call: `~/.projects/learnings` is
    the global scope, not a repo, so the home directory yields ''.
    """
    root = db._derive_repo_root(path)
    if not root:
        return ''
    if os.path.normpath(root) == os.path.normpath(os.path.expanduser('~')):
        return ''
    return root


def scope_for(path: str, config) -> tuple:
    """(scope, repo_root) for *path*, decided the way the INDEXER decides it.

    `skills/index-learnings/index-learnings.py:extract_metadata_from_path` calls
    every file under `learnings.globalDir` global and everything else repo
    scoped, and `find_all_learning_files` skips a `.projects/learnings` dir that
    sits under globalDir for the same reason. That function wrote the `scope`
    column `boundary_refusal` reads, so this must not be a second opinion.

    Deriving scope from the repo root alone was: `learnings.globalDir` is a
    supported pointer at any directory, including a `.projects/learnings` inside
    another Git repo. In that configuration the indexer stores those files as
    global while a root-first inference calls each new one repo scoped, so every
    indexed sibling fails `boundary_refusal` as a cross-scope match and
    write-time dedupe silently switches off for the whole corpus.

    globalDir therefore wins and the repo derivation is only the fallback. The
    prefix test is a bare `startswith` on realpaths because that is exactly what
    the indexer does; a stricter separator-aware test here would reintroduce the
    same class of disagreement for a sibling directory sharing the prefix.
    """
    global_dir = (config or {}).get('learnings', {}).get('globalDir') or ''
    if global_dir:
        try:
            resolved = os.path.realpath(path)
            resolved_global = os.path.realpath(global_dir)
        except OSError:
            resolved = resolved_global = ''
        if resolved and resolved_global and resolved.startswith(resolved_global):
            return 'global', ''

    root = repo_root_for(path)
    return ('repo' if root else 'global'), root


def boundary_refusal(conn, result, new_scope: str, new_root: str):
    """Reason *result* may not absorb a learning at (new_scope, new_root), or None.

    Absorption appends the new body verbatim into the target and re-indexes the
    survivor under the TARGET's scope, so a merge across this boundary PROMOTES
    content: a repo-scoped learning absorbed into a global one puts one client's
    repo names, service names and identifiers into the corpus that is injected
    into every other client's session. `db.search` always ORs in
    `l.scope = 'global'`, so a global row is offered as the nearest candidate for
    a repo-scoped write on every run; this is the gate that refuses it.

    A merge is permitted only within one boundary: same scope, and for repo
    scope the same `repo_root`. Anything undeterminable refuses. Fail closed is
    the right direction here, because a visible duplicate is a far smaller harm
    than a cross-client content leak.
    """
    target_scope = result['metadata'].get('scope') or ''
    if target_scope not in ('global', 'repo'):
        return 'nearest match has no usable scope, refusing to absorb'

    if target_scope != new_scope:
        return (
            'nearest match is {}-scoped and this learning is {}-scoped, '
            'refusing a cross-scope absorb'.format(target_scope, new_scope)
        )

    if new_scope != 'repo':
        return None

    row = conn.execute(
        'SELECT repo_root FROM learnings WHERE id = ?', (result['id'],)
    ).fetchone()
    target_root = (row['repo_root'] if row is not None else '') or ''
    if not target_root or not new_root:
        return 'repo_root is unknown on one side, refusing to absorb'

    if os.path.normpath(target_root) != os.path.normpath(new_root):
        return (
            'nearest match belongs to repo_root {} and this learning to {}, '
            'refusing a cross-repo absorb'.format(target_root, new_root)
        )

    return None


def nearest_candidate(conn, content: str, new_path: str, config):
    """Closest indexed learning this file may legally be absorbed into.

    Returns (result_dict, similarity, None), or (None, None, reason) where the
    reason names the boundary that blocked the closest match. One KNN query, in
    scope of global plus the new file's own repo, then `boundary_refusal` drops
    every candidate that sits outside this learning's own scope or repo.
    """
    new_scope, root = scope_for(new_path, config)
    scope_repo_roots = [root] if root else []

    results = db.search(conn, content, scope_repo_roots, KNN_RESULTS, 1.0)

    refusal = None
    for result in results:
        stored = result['metadata'].get('file_path') or ''
        if not stored:
            continue
        if os.path.abspath(stored) == new_path:
            continue
        blocked = boundary_refusal(conn, result, new_scope, root)
        if blocked:
            refusal = refusal or blocked
            continue
        return result, cosine_from(result['distance']), None

    return None, None, refusal


@contextlib.contextmanager
def survivor_lock(target_path: str):
    """Hold an exclusive lock on *target_path* for one whole absorb sequence.

    Yields True while the lock is held and False when it could not be taken.

    The lock has to span the READ of the survivor through the rename, the
    database writes and the removal of the source, because every pair of those
    steps races. Two sessions selecting the same survivor concurrently -- the
    hook runs per generated file and several background jobs write learnings at
    once on this machine -- otherwise both read the pre-merge body, and the
    second rename silently discards the first merge while its source file has
    already been deleted. That is a permanently lost learning with no error
    anywhere.

    `flock` and not a lock file's existence: the kernel drops it when the holder
    exits, so a crashed absorb cannot wedge the write path. A holder that is
    alive but stuck is covered by LOCK_TIMEOUT_SECONDS, after which this yields
    False and the caller degrades to plain indexing -- the same fail-open
    direction as every other failure here.

    The lock file itself is left on disk. Unlinking it would let a waiter that
    already opened it lock a detached inode and think it had exclusivity. It is
    dotted and not `*.md`, so neither `find_all_learning_files` nor any indexer
    glob picks it up.
    """
    lock_path = os.path.join(
        os.path.dirname(target_path),
        '.{}.dedupe.lock'.format(os.path.basename(target_path)),
    )

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield False
        return

    held = False
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(LOCK_POLL_SECONDS)
        yield held
    finally:
        if held:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def replace_atomically(target_path: str, merged: str) -> None:
    """Swap *merged* into *target_path* without ever truncating it in place.

    `open(target_path, 'w')` truncates the SURVIVOR before a byte of the merge
    is written, so a crash, a full disk or an exception in between destroys the
    existing learning -- the one file this script exists to preserve. Write a
    sibling temp file, force it to disk, then swap it in with a single atomic
    rename, so any failure leaves the survivor exactly as it was.

    The temp name is unique (`mkstemp`), never `target_path + '.tmp'`. A fixed
    name is a second, unguarded shared resource: two absorbs racing on it can
    rename each other's partial writes over the survivor even though the merge
    itself is serialized. mkstemp also creates 0600, so the survivor's own mode
    is copied across before the swap rather than silently tightened.
    """
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(target_path) or '.',
        prefix='.{}.'.format(os.path.basename(target_path)),
        suffix='.tmp',
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(merged)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, stat.S_IMODE(os.stat(target_path).st_mode))
        os.replace(tmp_path, target_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def absorb(conn, result, new_path: str, content: str, dry_run: bool):
    """Append *content* to the existing learning and drop the new sibling."""
    target_path = os.path.abspath(result['metadata'].get('file_path') or '')

    # The KNN query and this write are not atomic, and the corpus can hold a row
    # for a file that has since been archived or renamed.
    if not os.path.isfile(target_path):
        return {'action': 'kept', 'reason': 'nearest match no longer exists on disk'}

    with survivor_lock(target_path) as held:
        if not held:
            return {
                'action': 'kept',
                'reason': 'another absorb holds {}, not waiting any longer'.format(
                    target_path
                ),
                'into': target_path,
            }

        # Re-checked under the lock: the absorb we just queued behind may have
        # been the one that archived or renamed this survivor.
        if not os.path.isfile(target_path):
            return {
                'action': 'kept',
                'reason': 'nearest match no longer exists on disk',
            }

        with open(target_path, 'r', encoding='utf-8') as handle:
            existing = handle.read()

        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        merged = '{}\n\n## Update {}\n\n{}\n'.format(
            existing.rstrip('\n'), today, content.strip()
        )

        if len(merged) > MAX_ABSORBED_CHARS:
            return {
                'action': 'kept',
                'reason': 'absorbing would exceed {} chars'.format(MAX_ABSORBED_CHARS),
                'into': target_path,
            }

        if dry_run:
            return {'action': 'absorbed', 'into': target_path, 'dry_run': True}

        replace_atomically(target_path, merged)

        # Re-index the survivor in place: same row id, same created_at, so
        # absorbing never grows the corpus.
        row = conn.execute(
            'SELECT id, scope, repo, file_path, topic, keywords, created_at, '
            'access_count, last_accessed FROM learnings WHERE id = ?',
            (result['id'],),
        ).fetchone()
        if row is not None:
            db.upsert_document(conn, row['id'], merged, {
                'scope': row['scope'],
                'repo': row['repo'],
                'file_path': row['file_path'],
                'topic': row['topic'],
                'keywords': row['keywords'],
                'created_at': row['created_at'],
                'access_count': row['access_count'],
                'last_accessed': row['last_accessed'],
            })

        # The new file is normally not indexed yet, but /compound can call this
        # after an index pass. Leaving a row behind would point the corpus at a
        # deleted file.
        orphans = conn.execute(
            'SELECT id FROM learnings WHERE file_path = ?', (new_path,)
        ).fetchall()
        for orphan in orphans:
            db.delete_document(conn, orphan['id'])

        # LAST, and only once every commit above has landed. Removing the source
        # first means a failing upsert or orphan sweep leaves the outer handler
        # reporting "kept" while the file is already gone: the hook then indexes
        # a missing path, the survivor's row still holds the pre-merge body, and
        # the absorbed learning is reachable from neither a file nor a row. The
        # invariant is that the new content always exists in at least one of
        # those two places, so every failure before this line is a visible
        # duplicate and never a loss.
        os.remove(new_path)

    return {'action': 'absorbed', 'into': target_path}


def decide(new_path: str, threshold: float, dry_run: bool):
    if not os.path.isfile(new_path):
        return {'action': 'kept', 'reason': 'no such file'}

    with open(new_path, 'r', encoding='utf-8') as handle:
        content = handle.read()
    if not content.strip():
        return {'action': 'kept', 'reason': 'empty file'}

    config = db.load_config()
    conn = db.get_connection(config)
    try:
        result, similarity, refusal = nearest_candidate(conn, content, new_path, config)
        if result is None:
            return {
                'action': 'kept',
                'reason': refusal or 'no comparable learning indexed',
            }
        if similarity < threshold:
            return {
                'action': 'kept',
                'similarity': similarity,
                'nearest': result['metadata'].get('file_path'),
            }
        verdict = absorb(conn, result, new_path, content, dry_run)
        verdict['similarity'] = similarity
        return verdict
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Absorb a newly written learning into an existing near-duplicate.'
    )
    parser.add_argument('path', help='path to the newly created learning file')
    parser.add_argument('--dry-run', action='store_true',
                        help='report the verdict without touching any file')
    parser.add_argument('--json', action='store_true', dest='as_json',
                        help='print the verdict as a single JSON object')
    args = parser.parse_args()

    new_path = os.path.abspath(args.path)

    try:
        config = db.load_config()
        threshold = config['learnings'].get(
            'writeDedupeCosineThreshold', DEFAULT_COSINE_THRESHOLD
        )
        verdict = decide(new_path, threshold, args.dry_run)
    except Exception as exc:  # fail open: a broken dedupe must not lose a learning
        verdict = {'action': 'kept', 'reason': 'dedupe failed: {}'.format(exc)}

    verdict.setdefault('path', new_path)

    if args.as_json:
        print(json.dumps(verdict))
    elif verdict['action'] == 'absorbed':
        print('absorbed {} into {}'.format(new_path, verdict.get('into')))
    else:
        print('kept {} ({})'.format(
            new_path, verdict.get('reason') or verdict.get('similarity')
        ))

    return 0


if __name__ == '__main__':
    sys.exit(main())
