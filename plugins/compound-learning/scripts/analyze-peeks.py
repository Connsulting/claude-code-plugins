#!/usr/bin/env python3
"""Walk Claude Code transcripts, extract auto-peek events, score signal vs noise.

For each peek event (UserPromptSubmit hook with `[auto-peek]` in stdout), we pair
it with the assistant reply that followed and compute:
  - injected_chars: bytes consumed in the context window
  - ack_mention: did Claude emit the required one-liner acknowledgment?
  - substantive_use: does the reply engage with learning content beyond the ack?

Pass --persist to write the per-file usefulness tally into the `peek_usefulness`
table, which is what build-pinned.py selects on. The write is a FULL REPLACE over
the scoring window, so a file that stops being useful decays out on the next run
instead of holding its rank forever.

--persist also folds the same events into `learning_usefulness_lifetime`, which
is the opposite: append/accumulate only, never replaced or deleted. prune-stale.py
reads it, because protection has to be lifetime even though eligibility is
windowed. Successive 30-day windows overlap by ~23 days on a weekly timer, so
only events strictly newer than the stored `watermark` are folded in. The
watermark is read, compared against and advanced inside ONE `BEGIN IMMEDIATE`
transaction on ONE connection, and a watermark that cannot be read aborts the
run non-zero rather than bootstrapping from "". That accumulation is ONE-WAY,
transcripts age out of ~/.claude/projects at ~31 days, so the table can never be
rebuilt. Back the DB up before changing how it is written.

Run: python3 analyze-peeks.py [days_back] [--verbose] [--persist]
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_PLUGIN_ROOT = os.environ.get(
    'CLAUDE_PLUGIN_ROOT',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
sys.path.insert(0, _PLUGIN_ROOT)

PROJECTS = Path.home() / ".claude" / "projects"
ACK_PATTERNS = [
    re.compile(r"found a stored learning", re.I),
    re.compile(r"stored learning on ", re.I),
    re.compile(r"relevant (stored )?learning", re.I),
    re.compile(r"noting the [a-z\- ]+ (gotcha|learning|pattern)", re.I),
]
FILENAME_SUMMARY_RE = re.compile(r"^\s*->\s+([^:]+\.md)\s*:\s*(.*)$", re.M)
LEARNING_BLOCK_RE = re.compile(r"^\[([a-f0-9]{32})\]\s*\n(.*?)(?=^\[[a-f0-9]{32}\]|\Z)", re.M | re.S)


def iter_transcript_events(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def extract_peek(event: dict[str, Any]) -> dict[str, Any] | None:
    att = event.get("attachment") if isinstance(event, dict) else None
    if not att or att.get("type") != "hook_success" or att.get("hookName") != "UserPromptSubmit":
        return None
    stdout = att.get("stdout") or ""
    if "[auto-peek]" not in stdout or "learning(s) found" not in stdout:
        return None
    header_match = re.search(r"\[auto-peek\]\s+(\d+)\s+learning\(s\)\s+found\s+for:\s+(.+)", stdout)
    if not header_match:
        return None
    count = int(header_match.group(1))
    keywords = header_match.group(2).strip()
    files = [(m.group(1).strip(), m.group(2).strip()) for m in FILENAME_SUMMARY_RE.finditer(stdout)]
    learnings = [(m.group(1), m.group(2).strip()) for m in LEARNING_BLOCK_RE.finditer(stdout)]
    return {
        "timestamp": event.get("timestamp"),
        "session_id": event.get("sessionId"),
        "keywords": keywords,
        "count": count,
        "files": files,
        "learnings": learnings,
        "injected_chars": len(stdout),
        "event_uuid": event.get("uuid"),
    }


def assistant_text(event: dict[str, Any]) -> str:
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return ""
    msg = event.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "\n".join(parts)
    return ""


def score_files(peek: dict[str, Any], reply: str) -> dict[str, bool]:
    """Per-FILE substantive-use verdicts for one peek.

    A peek usually injects several learnings. Crediting all of them whenever the
    reply engages with any one of them would let an unrelated learning ride a
    neighbour's usefulness into the pinned set, so each file is judged on its own
    filename citation and its own headings.
    """
    lowered = reply.lower()
    # The peek prints one `-> file: summary` line and one `[id]\nbody` block per
    # learning, in the same order, so index-align them when the counts agree.
    learnings = peek["learnings"] if len(peek["learnings"]) == len(peek["files"]) else []
    verdicts: dict[str, bool] = {}
    for i, (fname, _summary) in enumerate(peek["files"]):
        stem = fname.rsplit("/", 1)[-1].replace(".md", "")
        hit = bool(stem) and stem.lower() in lowered
        if not hit and learnings:
            body = learnings[i][1]
            for heading in re.findall(r"^#{1,6}\s+(.+)$", body, re.M)[:3]:
                heading = heading.strip()
                if len(heading.split()) >= 4 and heading.lower() in lowered:
                    hit = True
                    break
        verdicts[fname] = verdicts.get(fname, False) or hit
    return verdicts


def score_reply(peek: dict[str, Any], reply: str) -> dict[str, bool]:
    ack_mention = any(p.search(reply) for p in ACK_PATTERNS)
    # Substantive use: filename cited (without .md), OR any 4+ word sequence from the
    # first-line summaries / headings of the injected learnings appears in reply.
    filename_hit = False
    for fname, _summary in peek["files"]:
        stem = fname.rsplit("/", 1)[-1].replace(".md", "")
        if stem.lower() in reply.lower():
            filename_hit = True
            break
    phrase_hit = False
    for _lid, body in peek["learnings"]:
        for heading in re.findall(r"^#{1,6}\s+(.+)$", body, re.M)[:3]:
            heading = heading.strip()
            if len(heading.split()) >= 4 and heading.lower() in reply.lower():
                phrase_hit = True
                break
        if phrase_hit:
            break
    substantive = filename_hit or phrase_hit
    return {"ack_mention": ack_mention, "filename_hit": filename_hit, "phrase_hit": phrase_hit, "substantive": substantive}


def persist_usefulness(
    per_file_seen: Counter[str],
    per_file_substantive: Counter[str],
    per_file_ack: Counter[str],
    per_file_last_substantive: dict[str, str],
    days_back: int,
) -> int:
    """Full-replace the peek_usefulness table with this window's tally.

    Full replace (not upsert) is deliberate: it is what lets a formerly useful
    learning lose its pinned slot once it stops earning substantive use.
    """
    import lib._site_packages  # noqa: F401
    import lib.db as db

    config = db.load_config()
    conn = db.get_connection(config)
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("DELETE FROM peek_usefulness")
        conn.executemany(
            """INSERT INTO peek_usefulness
               (file_name, injections, substantive, ack_only, last_substantive, window_days, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    os.path.basename(fname),
                    seen,
                    per_file_substantive[fname],
                    per_file_ack[fname],
                    per_file_last_substantive.get(fname),
                    days_back,
                    now_iso,
                )
                for fname, seen in per_file_seen.items()
            ],
        )
        conn.commit()
        return len(per_file_seen)
    finally:
        conn.close()


WATERMARK_KEY = "watermark"


class WatermarkUnavailable(RuntimeError):
    """The lifetime watermark could not be read, so accumulation must not run."""


def read_watermark(conn) -> str:
    """The maximum peek-event timestamp already folded into the lifetime table.

    Returns "" ONLY when the meta row is genuinely absent, which is a true first
    run against a freshly created table. Seeding the accumulator from the whole
    visible window is the correct bootstrap for exactly that case.

    Any read FAILURE raises `WatermarkUnavailable` instead. "" means "fold in
    everything visible", so treating a lock timeout, a corrupt page or a
    permissions change as "" would add a second full copy of the window to a
    table that is append-only and can never be rebuilt. An unreadable watermark
    aborts the run; it never bootstraps it.

    Reads on the CALLER'S connection, inside the caller's BEGIN IMMEDIATE, so
    that read, decide, accumulate and advance are one serialized sequence. Read
    on a separate connection it was a TOCTOU: two concurrent --persist runs both
    saw the same watermark, both classified the same events as new, and both
    committed.
    """
    try:
        row = conn.execute(
            "SELECT value FROM learning_usefulness_lifetime_meta WHERE key = ?",
            (WATERMARK_KEY,),
        ).fetchone()
    except Exception as exc:
        raise WatermarkUnavailable(f"cannot read the lifetime watermark: {exc}") from exc
    return (row[0] or "") if row else ""


def accumulate_lifetime(
    events: list[tuple[str, str, bool, bool]], scan_start: str
) -> tuple[int, str]:
    """Fold events newer than the watermark into learning_usefulness_lifetime.

    `events` is one (timestamp, file_name, ack_mention, substantive) tuple per
    file injection seen in the window. Events carrying no timestamp are dropped
    by the caller: they cannot be watermarked, and counting them would
    double-count them forever, on every subsequent run.

    THE ONE INVARIANT: every event folded in is at or below the watermark that
    gets written. Only events in the half-open interval
    `watermark < event_ts <= scan_start` are accumulated, and the watermark then
    advances to the maximum timestamp ACTUALLY accumulated, which by that gate is
    itself at or below `scan_start`. So the written watermark is exactly
    `min(scan_start, max_accumulated_event_ts)` and nothing counted can ever land
    above it and re-fold.

    `scan_start` is captured by the caller BEFORE its transcript walk, not after.
    That walk takes minutes, and an event appended to an already-read transcript
    mid-walk would otherwise sit below a watermark derived from a file read later
    and be lost permanently. Bounding by scan start defers it to the next run
    instead. Re-reading a small overlap is free (the gate is idempotent); losing
    an event is not.

    Events ABOVE `scan_start` are excluded rather than counted-then-unwatermarked.
    A clock-skewed future timestamp is thereby a slight undercount for one run
    instead of a permanent double-count on every run after it, and it can no
    longer stall the accumulator, because the watermark never follows it up.
    Skips are logged so the skew is diagnosable rather than silent.

    Accumulate, never replace, and never DELETE: this table is the long-window
    signal prune-stale.py protects on, and it cannot be rebuilt once the source
    transcripts age out.

    THE WATERMARK READ, THE COUNTER MERGE AND THE WATERMARK ADVANCE ALL RUN IN
    ONE EXPLICIT TRANSACTION, on one connection. BEGIN IMMEDIATE takes the write
    lock up front, so a second concurrent run blocks rather than interleaving.
    If the counters landed and the watermark write then failed (crash, lock
    timeout, power loss) the next run would re-fold the identical event set and
    double-count permanently, silently inflating `injections`, which is the
    denominator the prune opportunity floor reads. Rolling both back is safe:
    the events stay above the unadvanced watermark and fold in next run.

    Returns (files_folded, watermark_that_was_in_force).
    """
    import lib._site_packages  # noqa: F401
    import lib.db as db

    config = db.load_config()
    now_iso = datetime.now(timezone.utc).isoformat()

    conn = db.get_connection(config)
    try:
        conn.execute("BEGIN IMMEDIATE")
        watermark = read_watermark(conn)

        new_seen: Counter[str] = Counter()
        new_substantive: Counter[str] = Counter()
        new_ack: Counter[str] = Counter()
        new_last_substantive: dict[str, str] = {}
        max_event_ts = ""
        skipped_future = 0
        max_skipped_ts = ""
        for event_ts, fname, ack_mention, substantive in events:
            if not event_ts or event_ts <= watermark:
                continue
            if event_ts > scan_start:
                # Above the scan start: either a clock-skewed future timestamp or
                # an event appended during the walk. Both are the next run's, and
                # both stay above this run's watermark, so neither is lost.
                skipped_future += 1
                if event_ts > max_skipped_ts:
                    max_skipped_ts = event_ts
                continue
            if event_ts > max_event_ts:
                max_event_ts = event_ts
            new_seen[fname] += 1
            if ack_mention:
                new_ack[fname] += 1
            if substantive:
                new_substantive[fname] += 1
                if event_ts > new_last_substantive.get(fname, ""):
                    new_last_substantive[fname] = event_ts

        if skipped_future:
            print(f"Skipped {skipped_future} peek event(s) timestamped after the "
                  f"scan start {scan_start} (latest {max_skipped_ts}); they stay "
                  f"above the watermark and fold in on the next run.")

        if not new_seen:
            conn.rollback()
            return 0, watermark

        conn.executemany(
            """INSERT INTO learning_usefulness_lifetime
               (file_name, injections, substantive, ack_only,
                last_substantive, first_seen, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(file_name) DO UPDATE SET
                 injections = injections + excluded.injections,
                 substantive = substantive + excluded.substantive,
                 ack_only = ack_only + excluded.ack_only,
                 last_substantive = MAX(COALESCE(last_substantive, ''),
                                        COALESCE(excluded.last_substantive, '')),
                 last_updated = excluded.last_updated""",
            [
                (
                    os.path.basename(fname),
                    seen,
                    new_substantive[fname],
                    new_ack[fname],
                    new_last_substantive.get(fname),
                    now_iso,
                    now_iso,
                )
                for fname, seen in new_seen.items()
            ],
        )
        conn.execute(
            "INSERT INTO learning_usefulness_lifetime_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (WATERMARK_KEY, max_event_ts),
        )
        conn.commit()
        return len(new_seen), watermark
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def analyze(days_back: int = 30, verbose: bool = False, persist: bool = False) -> None:
    cutoff = 86400 * days_back
    # Anchor the window to wall-clock now. Anchoring to the mtime of PROJECTS
    # silently widened the window whenever that directory had not gained a direct
    # child recently, letting months-old transcripts count as "last 30 days" and
    # defeating decay.
    now = time.time()
    # Captured BEFORE the walk below, and used as the upper bound on what may be
    # accumulated into the lifetime table AND as the ceiling on the watermark.
    # The rule: advance the watermark to min(scan_start, max accumulated event
    # timestamp). The walk takes minutes, so an event appended to a transcript
    # the walk has already passed carries a timestamp below a watermark taken
    # after the walk and would be skipped forever. Bounded by scan start it is
    # merely deferred to the next run.
    scan_start = datetime.now(timezone.utc).isoformat()
    peeks: list[dict[str, Any]] = []
    per_file_seen: Counter[str] = Counter()
    per_file_substantive: Counter[str] = Counter()
    per_file_ack: Counter[str] = Counter()
    per_file_last_substantive: dict[str, str] = {}
    # Lifetime accumulator: the raw per-injection facts, kept unclassified here.
    # Deciding which of them are new is done against the watermark INSIDE the
    # accumulator's transaction, because the scan below takes minutes and a
    # watermark read before it is stale by the time the merge commits. The
    # per_file_* counters above keep counting every in-window event, so
    # peek_usefulness and the printed report are unaffected.
    lifetime_events: list[tuple[str, str, bool, bool]] = []
    for jsonl in PROJECTS.rglob("*.jsonl"):
        try:
            if now - jsonl.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        events = list(iter_transcript_events(jsonl))
        # Index events by parentUuid to find the assistant reply that follows a peek-carrying user turn.
        for i, ev in enumerate(events):
            peek = extract_peek(ev)
            if not peek:
                continue
            # The reply is the next assistant message after this hook event within the same session.
            reply_text = ""
            for follow in events[i + 1 : i + 40]:
                if isinstance(follow, dict) and follow.get("type") == "assistant":
                    reply_text = assistant_text(follow)
                    if reply_text:
                        break
            score = score_reply(peek, reply_text)
            peek.update(score)
            peek["reply_chars"] = len(reply_text)
            peek["transcript"] = jsonl.name
            peeks.append(peek)
            file_verdicts = score_files(peek, reply_text)
            event_ts = peek.get("timestamp") or ""
            for fname, _ in peek["files"]:
                per_file_seen[fname] += 1
                if score["ack_mention"]:
                    per_file_ack[fname] += 1
                if file_verdicts.get(fname):
                    per_file_substantive[fname] += 1
                    if event_ts > per_file_last_substantive.get(fname, ""):
                        per_file_last_substantive[fname] = event_ts
                # An event with no timestamp cannot be watermarked, so it never
                # reaches the accumulator. Skipping undercounts slightly;
                # counting it would double-count forever, on every later run.
                if event_ts:
                    lifetime_events.append(
                        (event_ts, fname, score["ack_mention"], bool(file_verdicts.get(fname)))
                    )

    total = len(peeks)
    if not total:
        # An empty window is still a measurement: leaving the old tally in place
        # would let out-of-window evidence read as current on the next build.
        if persist:
            # Deliberately clears peek_usefulness ONLY. learning_usefulness_lifetime
            # and its watermark are left untouched: an empty rolling window means
            # "nothing recent", never "the accumulated history is void".
            persist_usefulness(Counter(), Counter(), Counter(), {}, days_back)
            print("Persisted an empty usefulness window (0 peek events); "
                  "peek_usefulness cleared (lifetime accumulator untouched).")
        else:
            print("No peek events found.")
        return

    if persist:
        n = persist_usefulness(
            per_file_seen, per_file_substantive, per_file_ack,
            per_file_last_substantive, days_back,
        )
        print(f"Persisted usefulness rows for {n} files into peek_usefulness "
              f"(window {days_back}d, full replace).")
        m, watermark = accumulate_lifetime(lifetime_events, scan_start)
        if m:
            print(f"Accumulated {m} file(s) into learning_usefulness_lifetime "
                  f"(events after {watermark or 'the beginning'}).")
        else:
            print("No peek events newer than the lifetime watermark "
                  f"({watermark or 'unset'}); accumulator unchanged.")

    total_chars = sum(p["injected_chars"] for p in peeks)
    ack = sum(1 for p in peeks if p["ack_mention"])
    subst = sum(1 for p in peeks if p["substantive"])
    fname_hit = sum(1 for p in peeks if p["filename_hit"])
    phrase_hit = sum(1 for p in peeks if p["phrase_hit"])

    # Dedup analysis: how many injections were re-injections of a file already seen in the session?
    seen_in_session: dict[str, set[str]] = defaultdict(set)
    redundant_injections = 0
    redundant_chars_approx = 0
    for p in sorted(peeks, key=lambda x: (x["session_id"] or "", x["timestamp"] or "")):
        sid = p["session_id"] or "unknown"
        fnames = [f for f, _ in p["files"]]
        new_in_peek = [f for f in fnames if f not in seen_in_session[sid]]
        redundant_in_peek = len(fnames) - len(new_in_peek)
        if fnames:
            redundant_injections += redundant_in_peek
            # Rough: chars proportional to fraction of files that were redundant.
            redundant_chars_approx += p["injected_chars"] * redundant_in_peek // len(fnames)
        seen_in_session[sid].update(fnames)
    total_file_injections = sum(len(p["files"]) for p in peeks)

    print(f"Transcripts scanned: last {days_back}d under {PROJECTS}")
    print(f"Peek events          : {total}")
    print(f"Sessions with peeks  : {len(seen_in_session)}")
    print(f"File injections      : {total_file_injections} ({redundant_injections} redundant, {100*redundant_injections/max(total_file_injections,1):.1f}%)")
    print(f"Chars injected total : {total_chars:,}  (avg {total_chars//total:,}/peek)")
    print(f"  of which redundant : ~{redundant_chars_approx:,} chars (~{100*redundant_chars_approx/max(total_chars,1):.1f}% wasted to dedup failure)")
    print(f"Est tokens injected  : ~{total_chars // 4:,}  (4 chars/token rule of thumb)")
    print(f"Ack one-liner rate   : {ack}/{total} ({100 * ack / total:.1f}%)")
    print(f"Filename-cited rate  : {fname_hit}/{total} ({100 * fname_hit / total:.1f}%)")
    print(f"Heading-cited rate   : {phrase_hit}/{total} ({100 * phrase_hit / total:.1f}%)")
    print(f"Substantive-use rate : {subst}/{total} ({100 * subst / total:.1f}%)  [filename OR heading]")
    print()

    print("Top 15 injected files (seen / substantive / ratio):")
    for fname, seen in per_file_seen.most_common(15):
        s = per_file_substantive[fname]
        ratio = f"{100 * s / seen:.0f}%" if seen else "-"
        print(f"  {seen:3d}  {s:3d}  {ratio:>4}  {fname}")

    print()
    print("Top 10 keyword queries:")
    kw_count = Counter(p["keywords"] for p in peeks)
    for kw, c in kw_count.most_common(10):
        print(f"  {c:3d}  {kw}")

    if verbose:
        print()
        print("Sample non-substantive peeks (no signal detected):")
        for p in peeks[:5]:
            if not p["substantive"]:
                print(f"  {p['timestamp']}  keywords={p['keywords']!r}  files={[f for f,_ in p['files']]}")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 30
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    persist = "--persist" in sys.argv
    try:
        analyze(days, verbose, persist)
    except WatermarkUnavailable as exc:
        # Fail CLOSED. Bootstrapping from "" here would re-fold the whole visible
        # window on top of counts already recorded, permanently and silently.
        print(f"ABORTED: {exc}", file=sys.stderr)
        print("learning_usefulness_lifetime was NOT touched. Re-run --persist once "
              "the database is readable.", file=sys.stderr)
        sys.exit(1)
