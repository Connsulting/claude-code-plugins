#!/usr/bin/env python3
"""Walk Claude Code transcripts, extract auto-peek events, score signal vs noise.

For each peek event (UserPromptSubmit hook with `[auto-peek]` in stdout), we pair
it with the assistant reply that followed and compute:
  - injected_chars: bytes consumed in the context window
  - ack_mention: did Claude emit the required one-liner acknowledgment?
  - substantive_use: does the reply engage with learning content beyond the ack?

Run: python3 analyze-peeks.py [days_back] [--verbose]
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

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


def analyze(days_back: int = 30, verbose: bool = False) -> None:
    cutoff = 86400 * days_back
    now = os.path.getmtime(PROJECTS) if PROJECTS.exists() else 0
    peeks: list[dict[str, Any]] = []
    per_file_seen: Counter[str] = Counter()
    per_file_substantive: Counter[str] = Counter()
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
            for fname, _ in peek["files"]:
                per_file_seen[fname] += 1
                if score["substantive"]:
                    per_file_substantive[fname] += 1

    total = len(peeks)
    if not total:
        print("No peek events found.")
        return

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
    analyze(days, verbose)
