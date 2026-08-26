# /// script
# dependencies = ["markdown", "pygments"]
# ///
"""Render a markdown plan to HTML with collapsible sections, anchors, and inline comments.

Each H2 (##) becomes a collapsible <details> block. Every heading, list item,
paragraph, and blockquote gets a stable anchor so comments can attach to it.
Comments live in a sidecar file named `<source>.comments.json` so Claude can
read them back in a future session.

Comment types stored in the sidecar:
    text     - free-form text comment (default)
    reaction - one of a small allowlist of emoji attached to an anchored block
    decision - a choice (or set of choices for multi) on a ```decide block
    status   - a task checkbox flipped open<->done from the UI

CLI:
    uv run render.py PATH.md [OUT.html]
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import html
import json
import re
import sys
from pathlib import Path

import markdown
from markdown.extensions import Extension
from markdown.extensions.toc import slugify_unicode
from markdown.treeprocessors import Treeprocessor


SECTION_BOUNDARY_LEVEL = 2  # H2 boundaries open/close <details> sections


def _asset_version() -> str:
    """Cache-busting stamp from the mtimes of the mutable client assets.

    Appended as ?v=... to the style.css/app.js/diff.js URLs so a browser that
    ignores Cache-Control (mobile Safari does on soft reloads) still refetches
    them the instant they change. mermaid.min.js is left unstamped since it is a
    large, stable vendored file.
    """
    assets = Path(__file__).parent / "assets"
    stamps = []
    for name in ("style.css", "app.js", "diff.js"):
        try:
            stamps.append(int((assets / name).stat().st_mtime))
        except OSError:
            pass
    return str(max(stamps)) if stamps else "0"


_BULLET_LINE = re.compile(r"^(?P<lead>[ ]*)(?:[-*+]|\d+\.)\s")
_FENCE_LINE = re.compile(r"^[ ]{0,3}(?:```|~~~)")


def normalize_list_indent(md_text: str) -> str:
    """Bump list-marker indents that are not multiples of 4 up to the next multiple.

    python-markdown's sane_lists requires 4-space-per-level indentation for nested
    lists, but humans naturally write 3 spaces to align under "1. ". Without this,
    `   - sub` collapses inline with the parent. Skips fenced code blocks.
    """
    out: list[str] = []
    in_fence = False
    for line in md_text.splitlines():
        if _FENCE_LINE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        m = _BULLET_LINE.match(line)
        if m:
            n = len(m.group("lead"))
            if n % 4 != 0:
                line = " " * (4 - n % 4) + line
        out.append(line)
    return "\n".join(out)


def add_blank_line_before_lists(md_text: str) -> str:
    """Insert a blank line when a list item directly follows a paragraph.

    python-markdown's sane_lists requires a blank line between a paragraph and
    a list; CommonMark / GFM are lenient. Without this, intros like
    ``From `~/wiki/`:`` followed immediately by bullets render as one glued
    paragraph with literal ``-`` chars. Skips fenced code blocks; preserves
    list-context across blank lines and indented continuations so multi-line
    list items and nested lists are untouched.

    Runs AFTER transform_task_lines so its data-md-line numbers stay aligned
    with the on-disk source.
    """
    out: list[str] = []
    in_fence = False
    in_list_context = False
    prev_line_blank = True  # start-of-file behaves as blank
    for line in md_text.splitlines():
        if _FENCE_LINE.match(line):
            in_fence = not in_fence
            out.append(line)
            in_list_context = False
            prev_line_blank = False
            continue
        if in_fence:
            out.append(line)
            prev_line_blank = line.strip() == ""
            continue

        is_blank = line.strip() == ""
        is_list = bool(_BULLET_LINE.match(line))
        is_indented = (not is_blank) and line[:1] in (" ", "\t")

        if is_list:
            if not in_list_context and not prev_line_blank:
                out.append("")
            in_list_context = True
        elif is_blank or is_indented:
            pass  # list context survives blank lines and indented continuations
        else:
            in_list_context = False

        out.append(line)
        prev_line_blank = is_blank
    return "\n".join(out)


_TASK_LINE_RE = re.compile(
    r"^(?P<lead>[ ]*[-*+]\s+)\[(?P<state>[ xX])\]\s+(?P<rest>.*)$"
)


def transform_task_lines(md_text: str) -> str:
    """Replace `- [ ]` / `- [x]` task markers with an inline span the client can find.

    The marker span carries `data-md-line` (1-based line number in the source
    .md), which the client sends back when toggling so the server can flip the
    `[ ]` / `[x]` on the exact source line. normalize_list_indent runs before
    this and preserves line count, so the line numbers stay accurate.

    The empty span contributes nothing to itertext(), so the parent <li>'s
    block anchor is hashed from just the task text.
    """
    out: list[str] = []
    for idx, line in enumerate(md_text.splitlines(), start=1):
        m = _TASK_LINE_RE.match(line)
        if m:
            state = "done" if m.group("state").lower() == "x" else "open"
            marker = (
                f'<span class="task-marker" data-state="{state}" '
                f'data-md-line="{idx}"></span> '
            )
            out.append(f"{m.group('lead')}{marker}{m.group('rest')}")
        else:
            out.append(line)
    return "\n".join(out)


_DECIDE_BLOCK_RE = re.compile(
    r"^```(decide(?:-multi)?)[^\n]*\n(.*?)^```\s*$",
    re.MULTILINE | re.DOTALL,
)


def extract_decide_blocks(md_text: str) -> tuple[str, dict[str, dict]]:
    """Replace ```decide / ```decide-multi blocks with placeholder divs.

    Markdown's `extra` extension passes block-level HTML through verbatim, so
    the placeholder survives md.convert() unchanged and we substitute the real
    decision card HTML post-render.
    """
    blocks: dict[str, dict] = {}

    def repl(m: re.Match) -> str:
        kind = m.group(1)
        body = m.group(2)
        block_id = f"D{len(blocks):04d}"
        blocks[block_id] = _parse_decide_block(kind, body)
        return f'\n<div class="__decide_placeholder__" data-id="{block_id}"></div>\n'

    return _DECIDE_BLOCK_RE.sub(repl, md_text), blocks


def _parse_decide_block(kind: str, body: str) -> dict:
    question = ""
    options: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^[-*+]\s+(.*)$", s)
        if m:
            options.append(m.group(1).strip())
        elif not question:
            question = s
    return {"kind": kind, "question": question, "options": options}


def _text_of(el) -> str:
    return " ".join("".join(el.itertext()).split())


class BlockAnchorTreeprocessor(Treeprocessor):
    """Stamp a stable data-anchor on every li/p/blockquote and table cell (td/th).

    Anchor = prefix + first 10 hex of md5(first 120 chars of normalized text),
    where the prefix is "c-" for table cells (td/th) and "b-" for everything else.
    The cell prefix keeps cells in their own namespace so a cell sharing a
    paragraph's text cannot perturb that paragraph's "b-" anchor.
    Stable across reorders; goes stale only if the element's text is edited.
    """

    TARGET_TAGS = ("li", "p", "blockquote", "td", "th")

    def run(self, root):
        seen: dict[str, int] = {}
        for el in root.iter():
            if el.tag not in self.TARGET_TAGS:
                continue
            if el.get("data-anchor"):
                continue
            text = _text_of(el)
            if not text:
                continue
            digest = hashlib.md5(text[:120].encode("utf-8")).hexdigest()[:10]
            # Cells get their own "c-" namespace so a cell whose text matches an
            # already-anchored paragraph cannot bump that paragraph's "b-" anchor
            # (which would silently move existing sidecar comments).
            prefix = "c-" if el.tag in ("td", "th") else "b-"
            anchor = f"{prefix}{digest}"
            n = seen.get(anchor, 0)
            seen[anchor] = n + 1
            if n:
                anchor = f"{anchor}-{n}"
            el.set("data-anchor", anchor)
        return None


class BlockAnchorExtension(Extension):
    def extendMarkdown(self, md):
        md.treeprocessors.register(BlockAnchorTreeprocessor(md), "block_anchor", 5)


def load_comments(md_path: Path) -> dict:
    sidecar = md_path.with_suffix(md_path.suffix + ".comments.json")
    if not sidecar.exists():
        return {"comments": []}
    try:
        return json.loads(sidecar.read_text())
    except json.JSONDecodeError:
        return {"comments": []}


def comments_for_anchor(comments: dict, anchor: str) -> list[dict]:
    return [
        c for c in comments.get("comments", [])
        if c.get("anchor") == anchor and not c.get("resolved")
    ]


def is_answered(c: dict) -> bool:
    """True when the last word in this thread came from an agent.

    An answered comment stays OPEN on purpose. `resolved` means "addressed in
    the plan", and resolved comments do not render at all, so an agent that
    answered and then resolved would delete its own answer before the reviewer
    ever saw it. Dismissal is the reviewer's press, not the agent's.
    """
    replies = c.get("replies")
    if not isinstance(replies, list) or not replies:
        return False
    last = replies[-1]
    return isinstance(last, dict) and last.get("role", "agent") == "agent"


def count_answered(comments: dict) -> int:
    return sum(
        1 for c in comments.get("comments", [])
        if not c.get("resolved") and is_answered(c)
    )


def _delete_button(cid: str) -> str:
    return (
        f'<button type="button" class="comment-delete" data-id="{cid}" '
        f'title="Delete this comment" aria-label="Delete">×</button>'
    )


def _reply_button(cid: str) -> str:
    return (
        f'<button type="button" class="comment-reply" data-id="{cid}" '
        f'title="Reply in this thread" aria-label="Reply">Reply</button>'
    )


def render_replies_html(c: dict) -> str:
    """The thread under one comment. '' when nobody has replied yet.

    Agent and reviewer replies are the same record with a different `role`, so
    a thread reads top to bottom as one conversation rather than two lists.
    """
    replies = c.get("replies")
    if not isinstance(replies, list) or not replies:
        return ""
    parts: list[str] = []
    for r in replies:
        if not isinstance(r, dict):
            continue
        role = "agent" if r.get("role", "agent") == "agent" else "reviewer"
        who = html.escape(str(r.get("author") or role))
        ts = html.escape(str(r.get("timestamp", "")))
        text = html.escape(str(r.get("text", "")))
        parts.append(
            f'<div class="reply {role}">'
            f'<div class="reply-meta"><span class="reply-who">{who}</span>'
            f'<span class="ts">{ts}</span></div>'
            f'<div class="reply-body">{text}</div>'
            f"</div>"
        )
    if not parts:
        return ""
    return f'<div class="replies">{"".join(parts)}</div>'


def render_comment_html(c: dict) -> str:
    """Render one non-reaction comment (text / decision / status).

    Reactions render as inline chips via render_reactions_inline_html and
    never go through this function.
    """
    ctype = c.get("type", "text")
    ts = html.escape(c.get("timestamp", ""))
    cid = html.escape(c.get("id", ""))
    delbtn = _delete_button(cid)
    replybtn = _reply_button(cid)
    replies = render_replies_html(c)
    # The marker the "N answered" chip navigates by, and what CSS tints.
    answered = ' data-answered="true"' if is_answered(c) else ""

    if ctype == "decision":
        choices = c.get("choices") or ([c["choice"]] if c.get("choice") else [])
        chosen = html.escape(", ".join(choices) if choices else "(no choice)")
        return (
            f'<div class="comment decision" data-id="{cid}"{answered}>'
            f'<div class="comment-meta"><span class="ts">{ts}</span>'
            f"{replybtn}{delbtn}</div>"
            f'<div class="comment-body"><strong>Decided:</strong> {chosen}</div>'
            f"{replies}"
            f"</div>"
        )

    if ctype == "status":
        checked = bool(c.get("checked"))
        state = "done" if checked else "open"
        text = html.escape(c.get("text", ""))
        suffix = f": {text}" if text else ""
        return (
            f'<div class="comment status" data-id="{cid}">'
            f'<div class="comment-meta"><span class="ts">{ts}</span>{delbtn}</div>'
            f'<div class="comment-body">Marked {state}{suffix}</div>'
            f"</div>"
        )

    # text (default; unknown types fall through here)
    text = html.escape(c.get("text", ""))
    quote_html = ""
    quote = (c.get("quote") or "").strip()
    if quote:
        truncated = quote[:140] + ("…" if len(quote) > 140 else "")
        quote_html = f'<div class="comment-quote">{html.escape(truncated)}</div>'
    return (
        f'<div class="comment" data-id="{cid}"{answered}>'
        f'<div class="comment-meta"><span class="ts">{ts}</span>'
        f"{replybtn}{delbtn}</div>"
        f'{quote_html}'
        f'<div class="comment-body">{text}</div>'
        f"{replies}"
        f"</div>"
    )


def render_reactions_inline_html(items: list[dict]) -> str:
    """Inline span of reaction chips. Tap a chip to delete (no confirmation).

    Returns '' if there are no reactions, so callers can omit the wrapper entirely.
    """
    reactions = [c for c in items if c.get("type") == "reaction"]
    if not reactions:
        return ""
    chips = "".join(
        f'<button type="button" class="reaction-chip" data-id="{html.escape(c.get("id",""))}" '
        f'title="Tap to remove {html.escape(c.get("emoji",""))}">'
        f'{html.escape(c.get("emoji",""))}</button>'
        for c in reactions
    )
    return f'<span class="reactions-row">{chips}</span>'


def render_non_reaction_comments_html(items: list[dict]) -> str:
    """The block-stacked comment list (text and decision only).

    Status entries are omitted because the checkbox state already visualizes
    them; a "Marked done: foo" comment under a checked task line is redundant.
    Reactions render via render_reactions_inline_html. '' if empty.
    """
    others = [c for c in items if c.get("type") not in ("reaction", "status")]
    if not others:
        return ""
    return "".join(render_comment_html(c) for c in others)


def render_decide_card(block: dict, comments: dict) -> str:
    q = block["question"]
    options = block["options"]
    anchor = "d-" + hashlib.md5(q.encode("utf-8")).hexdigest()[:10]
    multi = block["kind"] == "decide-multi"

    latest_choices: set[str] = set()
    decisions = [
        c for c in comments.get("comments", [])
        if c.get("type") == "decision"
        and c.get("anchor") == anchor
        and not c.get("resolved")
    ]
    if decisions:
        decisions.sort(key=lambda c: c.get("timestamp", ""))
        latest = decisions[-1]
        if isinstance(latest.get("choices"), list):
            latest_choices = {str(x) for x in latest["choices"]}
        elif latest.get("choice"):
            latest_choices = {str(latest["choice"])}

    input_type = "checkbox" if multi else "radio"
    name = "decide-" + anchor
    opts_parts: list[str] = []
    for opt in options:
        opt_id = "do-" + hashlib.md5((anchor + opt).encode("utf-8")).hexdigest()[:8]
        checked = " checked" if opt in latest_choices else ""
        opts_parts.append(
            f'<label class="decide-option" for="{opt_id}">'
            f'<input id="{opt_id}" type="{input_type}" name="{name}" '
            f'value="{html.escape(opt)}"{checked}> '
            f'<span class="decide-option-text">{html.escape(opt)}</span>'
            f"</label>"
        )

    # "Other..." escape hatch — any saved choice that doesn't match a
    # predefined option is treated as the prior Other value.
    other_value = ""
    predefined = set(options)
    for chosen in latest_choices:
        if chosen not in predefined:
            other_value = chosen
            break
    other_id = "do-other-" + hashlib.md5(anchor.encode("utf-8")).hexdigest()[:8]
    other_checked = " checked" if other_value else ""
    opts_parts.append(
        f'<label class="decide-option decide-other-row" for="{other_id}">'
        f'<input id="{other_id}" type="{input_type}" name="{name}" '
        f'value="__OTHER__"{other_checked}> '
        f'<span class="decide-option-text">'
        f'<span class="decide-other-label">Other:</span> '
        f'<input type="text" class="decide-other-input" '
        f'placeholder="type your own" value="{html.escape(other_value)}">'
        f"</span></label>"
    )

    kind_label = "pick one or more" if multi else "pick one"
    return (
        f'<div class="decide-card" data-anchor="{anchor}" '
        f'data-multi="{str(multi).lower()}">'
        f'<div class="decide-header">'
        f'<span class="decide-kind">{kind_label}</span>'
        f'<span class="decide-q">{html.escape(q)}</span>'
        f'<span class="decide-saved" hidden>saved</span>'
        f"</div>"
        f'<div class="decide-options">{"".join(opts_parts)}</div>'
        f"</div>"
    )


_DECIDE_SUB_RE = re.compile(
    r'(?:<p[^>]*>\s*)?<div class="__decide_placeholder__" data-id="(D\d+)"></div>(?:\s*</p>)?'
)


def substitute_decide_blocks(body_html: str, blocks: dict, comments: dict) -> str:
    def repl(m: re.Match) -> str:
        block = blocks.get(m.group(1))
        if not block:
            return ""
        return render_decide_card(block, comments)
    return _DECIDE_SUB_RE.sub(repl, body_html)


_MERMAID_BLOCK_RE = re.compile(
    r"^```mermaid[^\n]*\n(.*?)^```\s*$",
    re.MULTILINE | re.DOTALL,
)


def extract_mermaid_blocks(md_text: str) -> tuple[str, dict[str, str]]:
    """Replace ```mermaid fences with placeholder divs before markdown conversion.

    Pulled out before md.convert so codehilite never highlights them; the
    placeholder survives conversion and the raw graph source is substituted
    back post-render as a <pre class="mermaid"> whose textContent mermaid reads
    as the diagram definition.
    """
    blocks: dict[str, str] = {}

    def repl(m: re.Match) -> str:
        block_id = f"MM{len(blocks):04d}"
        blocks[block_id] = m.group(1)
        return f'\n<div class="__mermaid_placeholder__" data-id="{block_id}"></div>\n'

    return _MERMAID_BLOCK_RE.sub(repl, md_text), blocks


_MERMAID_SUB_RE = re.compile(
    r'(?:<p[^>]*>\s*)?<div class="__mermaid_placeholder__" data-id="(MM\d+)"></div>(?:\s*</p>)?'
)


def substitute_mermaid_blocks(body_html: str, blocks: dict[str, str]) -> str:
    """Swap each mermaid placeholder for a <pre class="mermaid"> of escaped source.

    mermaid reads the element's textContent as the graph definition, so the raw
    source is HTML-escaped text, not markdown-processed. Newlines and indentation
    are preserved exactly.
    """
    def repl(m: re.Match) -> str:
        raw = blocks.get(m.group(1))
        if raw is None:
            return ""
        return f'<pre class="mermaid">{html.escape(raw)}</pre>'
    return _MERMAID_SUB_RE.sub(repl, body_html)


# GitHub-style callout markers. A blockquote whose first paragraph begins with
# one of these tokens becomes a styled callout; the token itself is stripped from
# the rendered text. Kept as a <blockquote> (never a <div>) so BLOCK_OPEN_RE still
# matches it when injecting inline comments and reactions.
_CALLOUT_KINDS = ("NOTE", "TIP", "IMPORTANT", "WARNING", "CAUTION")

# Matches a blockquote open tag carrying a data-anchor, its first paragraph open
# tag, and the leading [!KIND] marker (with surrounding whitespace). The marker is
# consumed by the match and dropped from the replacement, stripping it from the
# rendered text. Anchors are content hashes computed BEFORE this strip, so removing
# the marker afterward leaves every anchor stable.
_CALLOUT_RE = re.compile(
    r'(?P<bq><blockquote\b[^>]*?data-anchor="[^"]+"[^>]*>)'
    r'(?P<p>\s*<p\b[^>]*>)'
    r'\s*\[!(?P<kind>' + "|".join(_CALLOUT_KINDS) + r')\]\s*',
    re.IGNORECASE,
)


def annotate_callouts(body_html: str) -> str:
    """Turn `> [!WARNING] ...` blockquotes into styled callouts.

    Adds class "callout callout-<kind>" to the blockquote and removes the
    [!KIND] marker token from the first paragraph. The element stays a
    <blockquote> on purpose: BLOCK_OPEN_RE only injects inline comments and
    reactions into li/p/blockquote, so switching to a <div> would silently
    break comments and reactions on callouts.
    """
    def repl(m: re.Match) -> str:
        kind = m.group("kind").lower()
        open_tag = m.group("bq").replace(
            "<blockquote", f'<blockquote class="callout callout-{kind}"', 1
        )
        return open_tag + m.group("p")

    return _CALLOUT_RE.sub(repl, body_html)


_TABLE_RE = re.compile(r"<table.*?</table>", re.DOTALL)


def wrap_tables(body_html: str) -> str:
    """Wrap each rendered <table> in a horizontally scrollable container.

    python-markdown emits a bare <table>; on a phone a wide table would widen
    the whole page, so each one scrolls inside its own box instead. <table>
    does not nest, so a non-greedy match handles multiple tables cleanly.
    """
    return _TABLE_RE.sub(
        lambda m: f'<div class="table-scroll">{m.group(0)}</div>', body_html
    )


# Match a complete heading element: <h2 id="slug">text</h2>
HEADING_RE = re.compile(
    r'<h(?P<level>[1-6])(?P<attrs>[^>]*?)id="(?P<slug>[^"]+)"(?P<rest>[^>]*?)>(?P<text>.*?)</h(?P=level)>',
    re.DOTALL,
)


def augment_heading(
    level: str, slug: str, attrs: str, rest: str, text: str,
    reactions_inline: str = "",
) -> str:
    """Add anchor link to a heading. The + and reaction buttons are injected client-side.

    Any existing reactions render as inline chips at the end of the heading text.
    """
    anchor_link = f'<a class="anchor-link" href="#{slug}" aria-label="link">#</a>'
    return (
        f'<h{level}{attrs}id="{slug}"{rest} data-anchor="{slug}">'
        f"{anchor_link}{text}{reactions_inline}"
        f"</h{level}>"
    )


def wrap_sections(body_html: str, comments: dict) -> str:
    """Walk body HTML, wrap each H2-bounded range in <details>, augment every heading."""
    out: list[str] = []
    open_section = False
    cursor = 0

    def close_section() -> None:
        nonlocal open_section
        if open_section:
            out.append("</div></details>\n")
            open_section = False

    for m in HEADING_RE.finditer(body_html):
        out.append(body_html[cursor:m.start()])
        cursor = m.end()

        level = m.group("level")
        slug = m.group("slug")
        attrs = m.group("attrs")
        rest = m.group("rest")
        text = m.group("text")

        existing = comments_for_anchor(comments, slug)
        reactions_inline = render_reactions_inline_html(existing)
        augmented = augment_heading(level, slug, attrs, rest, text, reactions_inline)
        non_reactions = render_non_reaction_comments_html(existing)
        inline = (
            f'<div class="comments-inline" data-anchor="{slug}">{non_reactions}</div>'
            if non_reactions else ""
        )

        if int(level) == SECTION_BOUNDARY_LEVEL:
            close_section()
            out.append('<details class="section" open>\n')
            out.append("<summary>")
            out.append(augmented)
            out.append("</summary>\n")
            out.append(
                f'<button type="button" class="section-comment-button" '
                f'data-anchor="{html.escape(slug)}" '
                f'aria-label="Add comment to this section">Comment</button>'
            )
            out.append('<div class="section-body">\n')
            if inline:
                out.append(inline)
            open_section = True
        else:
            out.append(augmented)
            if inline:
                out.append(inline)

    out.append(body_html[cursor:])
    close_section()
    return "".join(out)


# Match <li ... data-anchor="..."> ... </li> and <p ... data-anchor="..."> ... </p>
# and <blockquote ... data-anchor="..."> ... </blockquote>.
# Insert open comments inside the element at the end so they render in-place.
BLOCK_OPEN_RE = re.compile(
    r'<(?P<tag>li|p|blockquote)(?P<attrs>[^>]*?)data-anchor="(?P<anchor>b-[a-f0-9-]+)"(?P<rest>[^>]*?)>',
)


def inline_block_comments(body_html: str, comments: dict) -> str:
    """Insert <div class="comments-inline"> just before the closing tag of each anchored block."""
    out_parts: list[str] = []
    pos = 0
    while True:
        m = BLOCK_OPEN_RE.search(body_html, pos)
        if not m:
            out_parts.append(body_html[pos:])
            break

        tag = m.group("tag")
        anchor = m.group("anchor")
        end_tag = f"</{tag}>"
        close_pos = find_matching_close(body_html, m.end(), tag)
        if close_pos == -1:
            out_parts.append(body_html[pos:m.end()])
            pos = m.end()
            continue

        existing = comments_for_anchor(comments, anchor)
        out_parts.append(body_html[pos:close_pos])
        if existing:
            reactions_inline = render_reactions_inline_html(existing)
            non_reactions = render_non_reaction_comments_html(existing)
            # Reactions render INSIDE the element so the chip sits on the same
            # line as the content. Other comments render in a sibling block AFTER
            # the element so they don't get caught in :has() styling (e.g. the
            # strikethrough on completed task li).
            if reactions_inline:
                out_parts.append(reactions_inline)
            out_parts.append(end_tag)
            if non_reactions:
                out_parts.append(
                    f'<div class="comments-inline" data-anchor="{anchor}">{non_reactions}</div>'
                )
        else:
            out_parts.append(end_tag)
        pos = close_pos + len(end_tag)
    return "".join(out_parts)


def find_matching_close(text: str, start: int, tag: str) -> int:
    """Find the index of the matching `</tag>` after `start`, accounting for nesting."""
    open_re = re.compile(rf"<{tag}\b", re.IGNORECASE)
    close_re = re.compile(rf"</{tag}>", re.IGNORECASE)
    depth = 1
    i = start
    while i < len(text):
        m_open = open_re.search(text, i)
        m_close = close_re.search(text, i)
        if not m_close:
            return -1
        if m_open and m_open.start() < m_close.start():
            depth += 1
            i = m_open.end()
        else:
            depth -= 1
            if depth == 0:
                return m_close.start()
            i = m_close.end()
    return -1


def build_toc(body_html: str) -> str:
    items: list[tuple[int, str, str]] = []
    for m in HEADING_RE.finditer(body_html):
        level = int(m.group("level"))
        if level not in (2, 3):
            continue
        slug = m.group("slug")
        text = re.sub(r"<[^>]+>", "", m.group("text")).strip()
        items.append((level, slug, text))

    if not items:
        return ""

    parts = ['<nav class="toc"><div class="toc-title">Sections</div><ol>']
    for level, slug, text in items:
        cls = "toc-h2" if level == 2 else "toc-h3"
        parts.append(f'<li class="{cls}"><a href="#{slug}">{html.escape(text)}</a></li>')
    parts.append("</ol></nav>")
    return "".join(parts)


def _make_md() -> markdown.Markdown:
    return markdown.Markdown(
        extensions=[
            "extra",
            "toc",
            "fenced_code",
            "tables",
            "sane_lists",
            "codehilite",
            BlockAnchorExtension(),
        ],
        extension_configs={
            "toc": {"slugify": slugify_unicode, "permalink": False},
            "codehilite": {"guess_lang": False, "css_class": "hl"},
        },
    )


def _convert(md_text: str) -> tuple[str, dict, dict]:
    """Run the markdown preprocessing pipeline and convert to body HTML.

    Returns (body_html, decide_blocks, mermaid_blocks). Both block maps must be
    substituted into the body later via substitute_decide_blocks and
    substitute_mermaid_blocks.
    """
    md_text = normalize_list_indent(md_text)
    md_text = transform_task_lines(md_text)
    md_text = add_blank_line_before_lists(md_text)
    md_text, decide_blocks = extract_decide_blocks(md_text)
    md_text, mermaid_blocks = extract_mermaid_blocks(md_text)
    return _make_md().convert(md_text), decide_blocks, mermaid_blocks


_ANCHOR_RE = re.compile(r'data-anchor="([^"]+)"')


def _anchors_in_order(body_html: str) -> list[str]:
    """Ordered list of every data-anchor value in the rendered body."""
    return _ANCHOR_RE.findall(body_html)


def diff_change_classes(old_body: str, new_body: str) -> dict[str, str]:
    """Map each NEW-side block anchor to 'added' or 'changed' vs the old body.

    Anchors are content hashes (headings use their slug), so an unchanged block
    keeps the same anchor and aligns as 'equal'. A reworded block gets a new
    anchor and shows up in a 'replace' span ('changed'); a brand-new block shows
    up in an 'insert' span ('added'). Removed blocks have no new-side anchor to
    tag and are not surfaced.
    """
    old = _anchors_in_order(old_body)
    new = _anchors_in_order(new_body)
    sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    classes: dict[str, str] = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "insert":
            for anchor in new[j1:j2]:
                classes[anchor] = "added"
        elif tag == "replace":
            # Treat a replace of N old blocks with M new ones as N edits plus
            # (M - N) additions: the first N new blocks are 'changed', any extra
            # are 'added'. Keeps a new paragraph that lands next to a reworded
            # one from being mislabeled as an edit.
            edited = i2 - i1
            for idx, anchor in enumerate(new[j1:j2]):
                classes[anchor] = "changed" if idx < edited else "added"
    return classes


def annotate_changes(body_html: str, classes: dict[str, str]) -> str:
    """Stamp data-changed="added|changed" onto the first tag bearing each anchor.

    The first occurrence of a given anchor in the final HTML is always the block
    or heading element itself; comment-rail divs that reuse the same anchor come
    after it, so count=1 lands on the content element.
    """
    for anchor, cls in classes.items():
        body_html = re.sub(
            r'(data-anchor="' + re.escape(anchor) + r'")',
            r'\1 data-changed="' + cls + '"',
            body_html,
            count=1,
        )
    return body_html


def render_html(
    md_text: str,
    title: str,
    comments: dict,
    *,
    relative_path: str = "",
    snapshot_md: str | None = None,
) -> str:
    body, decide_blocks, mermaid_blocks = _convert(md_text)

    change_classes: dict[str, str] = {}
    if snapshot_md is not None and snapshot_md != md_text:
        snap_body, _, _ = _convert(snapshot_md)
        change_classes = diff_change_classes(snap_body, body)

    body = substitute_decide_blocks(body, decide_blocks, comments)
    body = substitute_mermaid_blocks(body, mermaid_blocks)
    toc = build_toc(body)
    sectioned = wrap_sections(body, comments)
    sectioned = inline_block_comments(sectioned, comments)
    sectioned = wrap_tables(sectioned)
    sectioned = annotate_callouts(sectioned)
    if change_classes:
        sectioned = annotate_changes(sectioned, change_classes)

    n_answered = count_answered(comments)

    return TEMPLATE.format(
        title=html.escape(title),
        relative_path=html.escape(relative_path),
        toc=toc,
        body=sectioned,
        has_changes="true" if change_classes else "false",
        changed_count=len(change_classes),
        has_answers="true" if n_answered else "false",
        answered_count=n_answered,
        asset_v=_asset_version(),
    )


# ---------------------------------------------------------------------------
# Unified diff view (the "diff" tab): a GitHub-style collapsed diff of the raw
# markdown source between the baseline snapshot and the current file, with a
# per-hunk "reviewed" button that accepts that hunk into the baseline.
# ---------------------------------------------------------------------------


def compute_diff_hunks(old_md: str, new_md: str, context: int = 3) -> tuple[list[dict], list[str], list[str]]:
    """Group the line diff into hunks (changed lines + `context` lines around them).

    Returns (hunks, old_lines, new_lines). Lines come from splitlines() so the
    accept endpoint can reconstruct the file with `"\\n".join(...)` and reattach
    a trailing newline. Each hunk carries the snapshot line range it occupies and
    the new lines that replace it, so "reviewed" is a pure splice.
    """
    old = old_md.splitlines()
    new = new_md.splitlines()
    sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    hunks: list[dict] = []
    for group in sm.get_grouped_opcodes(context):
        first, last = group[0], group[-1]
        old_start, old_end = first[1], last[2]
        new_start, new_end = first[3], last[4]
        rows: list[tuple] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for oi, nj in zip(range(i1, i2), range(j1, j2)):
                    rows.append(("context", oi + 1, nj + 1, [(old[oi], False)]))
            elif tag == "delete":
                for oi in range(i1, i2):
                    rows.append(("del", oi + 1, None, [(old[oi], True)]))
            elif tag == "insert":
                for nj in range(j1, j2):
                    rows.append(("add", None, nj + 1, [(new[nj], True)]))
            else:  # replace: pair lines index-wise and word-diff each pair
                paired = min(i2 - i1, j2 - j1)
                del_rows, add_rows = [], []
                for k in range(paired):
                    o_segs, n_segs = word_diff(old[i1 + k], new[j1 + k])
                    del_rows.append(("del", i1 + k + 1, None, o_segs))
                    add_rows.append(("add", None, j1 + k + 1, n_segs))
                for k in range(paired, i2 - i1):
                    del_rows.append(("del", i1 + k + 1, None, [(old[i1 + k], True)]))
                for k in range(paired, j2 - j1):
                    add_rows.append(("add", None, j1 + k + 1, [(new[j1 + k], True)]))
                rows.extend(del_rows)
                rows.extend(add_rows)
        hunks.append({
            "old_start": old_start, "old_end": old_end,
            "new_start": new_start, "new_end": new_end,
            "old_lines": old[old_start:old_end],
            "new_lines": new[new_start:new_end],
            "rows": rows,
        })
    return hunks, old, new


def _b64(lines: list[str]) -> str:
    """base64(JSON) of a line list. JSON preserves [] vs [""] (pure-insert hunks)."""
    return base64.b64encode(json.dumps(lines).encode("utf-8")).decode("ascii")


_WORD_RE = re.compile(r"\s+|\S+")


def word_diff(old_line: str, new_line: str) -> tuple[list[tuple[str, bool]], list[tuple[str, bool]]]:
    """Intra-line diff of two lines into (old_segments, new_segments).

    Each segment is (text, changed). Tokens are words and whitespace runs, so
    only the differing words get flagged, GitHub-style. Returns the removed-word
    highlights for the old line and the added-word highlights for the new line.
    """
    o = _WORD_RE.findall(old_line)
    n = _WORD_RE.findall(new_line)
    sm = difflib.SequenceMatcher(a=o, b=n, autojunk=False)
    o_segs: list[tuple[str, bool]] = []
    n_segs: list[tuple[str, bool]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        o_text = "".join(o[i1:i2])
        n_text = "".join(n[j1:j2])
        changed = tag != "equal"
        if o_text:
            o_segs.append((o_text, changed))
        if n_text:
            n_segs.append((n_text, changed))
    return o_segs, n_segs


def _segs_html(segs: list[tuple[str, bool]]) -> str:
    """Render line segments, wrapping changed words in <span class="wd">."""
    out = []
    for text, changed in segs:
        esc = html.escape(text)
        out.append(f'<span class="wd">{esc}</span>' if changed and text else esc)
    return "".join(out) or "&nbsp;"


def _diff_gap_html(lines: list[str], start_old_no: int, start_new_no: int) -> str:
    """A collapsed run of unchanged lines, expandable on click.

    Unchanged lines exist on both sides, but after earlier inserts/deletes the
    old-side and new-side numbering diverge, so each gutter gets its own base.
    """
    if not lines:
        return ""
    n = len(lines)
    rows = []
    for k, line in enumerate(lines):
        rows.append(
            f'<div class="diff-row context">'
            f'<span class="ln ln-old">{start_old_no + k}</span>'
            f'<span class="ln ln-new">{start_new_no + k}</span>'
            f'<span class="sign"> </span>'
            f'<code class="diff-code">{html.escape(line) or "&nbsp;"}</code></div>'
        )
    label = f"{n} unchanged line{'s' if n != 1 else ''}"
    return (
        f'<div class="diff-gap">'
        f'<button type="button" class="diff-expand">Expand {html.escape(label)}</button>'
        f'<div class="diff-gap-lines" hidden>{"".join(rows)}</div>'
        f"</div>"
    )


def _diff_hunk_html(hunk: dict, index: int) -> str:
    header = (
        f"@@ -{hunk['old_start'] + 1},{hunk['old_end'] - hunk['old_start']} "
        f"+{hunk['new_start'] + 1},{hunk['new_end'] - hunk['new_start']} @@"
    )
    row_html = []
    for kind, old_no, new_no, segs in hunk["rows"]:
        sign = "+" if kind == "add" else "-" if kind == "del" else " "
        row_html.append(
            f'<div class="diff-row {kind}">'
            f'<span class="ln ln-old">{old_no if old_no else ""}</span>'
            f'<span class="ln ln-new">{new_no if new_no else ""}</span>'
            f'<span class="sign">{sign}</span>'
            f'<code class="diff-code">{_segs_html(segs)}</code></div>'
        )
    reviewed_btn = (
        f'<button type="button" class="hunk-reviewed" '
        f'data-old-start="{hunk["old_start"]}" data-old-end="{hunk["old_end"]}" '
        f'data-old-b64="{_b64(hunk["old_lines"])}" '
        f'data-new-b64="{_b64(hunk["new_lines"])}" '
        f'title="Accept this change into the baseline so it stops showing">Reviewed</button>'
    )
    return (
        f'<section class="diff-hunk" data-index="{index}">'
        f'<div class="diff-hunk-head"><span class="diff-hunk-range">{html.escape(header)}</span>{reviewed_btn}</div>'
        f'<div class="diff-hunk-body">{"".join(row_html)}</div>'
        f"</section>"
    )


def render_diff_html(
    snapshot_md: str,
    current_md: str,
    title: str,
    *,
    relative_path: str = "",
) -> str:
    hunks, _old, new = compute_diff_hunks(snapshot_md, current_md)

    n_add = sum(1 for h in hunks for r in h["rows"] if r[0] == "add")
    n_del = sum(1 for h in hunks for r in h["rows"] if r[0] == "del")

    parts: list[str] = []
    if not hunks:
        parts.append('<div class="diff-empty">No changes since the baseline. The doc and the snapshot match.</div>')
    else:
        prev_old_end = 0
        prev_new_end = 0
        for i, hunk in enumerate(hunks):
            gap_lines = new[prev_new_end:hunk["new_start"]]
            parts.append(_diff_gap_html(gap_lines, prev_old_end + 1, prev_new_end + 1))
            parts.append(_diff_hunk_html(hunk, i))
            prev_old_end = hunk["old_end"]
            prev_new_end = hunk["new_end"]
        trailing = new[prev_new_end:]
        parts.append(_diff_gap_html(trailing, prev_old_end + 1, prev_new_end + 1))

    return DIFF_TEMPLATE.format(
        title=html.escape(title),
        relative_path=html.escape(relative_path),
        summary=f"{len(hunks)} hunk{'s' if len(hunks) != 1 else ''} &middot; "
                f'<span class="add-stat">+{n_add}</span> '
                f'<span class="del-stat">&minus;{n_del}</span>',
        diff_body="".join(parts),
        asset_v=_asset_version(),
    )


_RAW_BODY = "<!--RAW_BODY-->"


def render_raw_html(
    md_text: str,
    title: str,
    *,
    relative_path: str = "",
) -> str:
    """The plan source as preformatted text, so you can read it without downloading.

    The markdown is spliced in after .format() so a plan that contains `{braces}`
    cannot KeyError the chrome template.
    """
    return RAW_TEMPLATE.format(
        title=html.escape(title),
        relative_path=html.escape(relative_path),
        asset_v=_asset_version(),
    ).replace(_RAW_BODY, html.escape(md_text))


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="/assets/style.css?v={asset_v}">
</head>
<body data-relpath="{relative_path}" data-has-changes="{has_changes}" data-has-answers="{has_answers}">
<header class="topbar">
  <a class="home-link" href="/" title="All plans">All Plans</a>
  <h1 class="page-title">{title}</h1>
  <span class="topbar-hint">Press <kbd>?</kbd> for shortcuts</span>
  <span class="answers-bar">
    <button class="answers-count" aria-label="Jump to the next answered comment" title="Questions the agent answered. Tap to walk them; the × on a thread dismisses it.">{answered_count} answered</button>
  </span>
  <span class="changes-bar">
    <span class="changes-count" title="Blocks changed since your last review">{changed_count} changed</span>
    <a class="changes-difflink" href="?view=diff" title="See the explicit added/removed lines">View Diff</a>
    <button class="changes-toggle" aria-label="Show or hide change highlights">Hide Changes</button>
    <button class="changes-dismiss" aria-label="Mark changes as reviewed" title="Clear the baseline so the doc reads clean again">Reviewed All</button>
  </span>
  <button class="md-copy" aria-label="Copy full markdown" title="Copy the entire plan markdown to the clipboard"><svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M3.5 10.5h-1a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1h7a1 1 0 0 1 1 1v1"/></svg>Copy</button>
  <a class="md-raw" href="?view=raw" title="View the plan as raw markdown"><svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5.5 3.5 1.5 8l4 4.5M10.5 3.5l4 4.5-4 4.5"/></svg>Raw</a>
  <a class="md-download" href="/raw/{relative_path}" download title="Download the entire plan markdown"><svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 1.5v8m0 0L5 6.5m3 3 3-3"/><path d="M2.5 11v2a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-2"/></svg>Download</a>
  <a class="pdf-download" href="/pdf/{relative_path}" download title="Download the rendered plan as a PDF">PDF</a>
  <button class="send-feedback" aria-label="Send feedback to the authoring session" title="Send all open feedback to the session that wrote this plan" disabled><svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14.5 1.5 7.3 8.7"/><path d="M14.5 1.5 10 14.5l-2.7-5.8L1.5 6z"/></svg><span class="sf-label">Send</span></button>
</header>
<aside class="toc-rail" id="toc-rail" aria-label="Table of contents">
{toc}
</aside>
<button class="toc-toggle sidebar-handle" aria-controls="toc-rail" aria-expanded="true" aria-label="Hide table of contents" title="Hide sections">&#x2039;</button>
<main class="content">
{body}
<footer class="page-footer">
  <button class="copy-feedback" aria-label="Copy feedback round-trip prompt" title="Copy all open feedback as a prompt for the next session">Copy Feedback as Prompt</button>
</footer>
</main>
<aside class="comments-rail" id="comments-rail" aria-label="Comments">
  <h2>Comments</h2>
  <div class="comments-list" id="comments-list"></div>
</aside>
<button class="comments-toggle sidebar-handle" aria-controls="comments-rail" aria-expanded="true" aria-label="Hide comments panel" title="Hide comments">&#x203a;</button>
<dialog id="add-comment-dialog">
  <form method="dialog" id="add-comment-form">
    <h3>Add comment</h3>
    <div class="anchor-label">on <code id="add-comment-anchor"></code></div>
    <label>Comment <textarea id="add-comment-text" rows="5" required></textarea></label>
    <div class="actions">
      <button value="cancel" formnovalidate>Cancel</button>
      <button id="add-comment-submit" value="submit">Save (<kbd>Ctrl</kbd>+<kbd>Enter</kbd>)</button>
    </div>
  </form>
</dialog>
<dialog id="help-dialog">
  <form method="dialog">
    <h3>Keyboard shortcuts</h3>
    <div class="help-grid">
      <section>
        <h4>Navigation</h4>
        <dl>
          <dt><kbd>j</kbd> / <kbd>&darr;</kbd></dt><dd>Next section</dd>
          <dt><kbd>k</kbd> / <kbd>&uarr;</kbd></dt><dd>Previous section</dd>
          <dt><kbd>o</kbd> / <kbd>O</kbd></dt><dd>Open / close section</dd>
          <dt><kbd>t</kbd></dt><dd>Toggle table of contents</dd>
          <dt><kbd>m</kbd></dt><dd>Toggle comments panel</dd>
        </dl>
      </section>
      <section>
        <h4>Actions (on focused block)</h4>
        <dl>
          <dt><kbd>c</kbd></dt><dd>Add comment</dd>
          <dt><kbd>1</kbd> <kbd>2</kbd> <kbd>3</kbd></dt><dd>React &#x1F44D; &#x1F44E; &#x1F914;</dd>
          <dt><kbd>Space</kbd></dt><dd>Toggle task checkbox</dd>
        </dl>
      </section>
      <section>
        <h4>Forms</h4>
        <dl>
          <dt><kbd>Ctrl</kbd>+<kbd>Enter</kbd></dt><dd>Submit comment</dd>
          <dt><kbd>Esc</kbd></dt><dd>Close dialog / drawer / clear focus</dd>
          <dt><kbd>?</kbd></dt><dd>Show this help</dd>
        </dl>
      </section>
    </div>
    <div class="actions">
      <button value="close">Close</button>
    </div>
  </form>
</dialog>
<script src="/assets/mermaid.min.js"></script>
<script src="/assets/app.js?v={asset_v}"></script>
</body>
</html>
"""


DIFF_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>diff: {title}</title>
<link rel="stylesheet" href="/assets/style.css?v={asset_v}">
</head>
<body class="diff-page" data-relpath="{relative_path}">
<header class="topbar">
  <a class="home-link" href="/" title="All plans">All Plans</a>
  <h1 class="page-title">diff: {title}</h1>
  <span class="diff-summary">{summary}</span>
  <a class="diff-doclink" href="/{relative_path}" title="Back to the rendered plan">View Doc</a>
</header>
<main class="content diff-content">
{diff_body}
</main>
<script src="/assets/diff.js?v={asset_v}"></script>
</body>
</html>
"""


RAW_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>raw: {title}</title>
<link rel="stylesheet" href="/assets/style.css?v={asset_v}">
</head>
<body class="raw-page" data-relpath="{relative_path}">
<header class="topbar">
  <a class="home-link" href="/" title="All plans">All Plans</a>
  <h1 class="page-title">raw: {title}</h1>
  <a class="raw-doclink" href="/{relative_path}" title="Back to the rendered plan">View Doc</a>
  <a class="md-download" href="/raw/{relative_path}" download title="Download the entire plan markdown"><svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 1.5v8m0 0L5 6.5m3 3 3-3"/><path d="M2.5 11v2a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-2"/></svg>Download</a>
</header>
<main class="content raw-content">
  <pre class="raw-markdown">""" + _RAW_BODY + """</pre>
</main>
</body>
</html>
"""


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: render.py PATH.md [OUT.html]", file=sys.stderr)
        return 2
    md_path = Path(sys.argv[1])
    md_text = md_path.read_text()
    title = md_path.stem
    m = re.search(r"^# +(.+)$", md_text, re.MULTILINE)
    if m:
        title = m.group(1).strip()

    comments = load_comments(md_path)
    snap_path = md_path.with_suffix(md_path.suffix + ".snapshot")
    snapshot_md = snap_path.read_text() if snap_path.exists() else None
    out = render_html(
        md_text, title, comments,
        relative_path=str(md_path), snapshot_md=snapshot_md,
    )

    if len(sys.argv) >= 3:
        Path(sys.argv[2]).write_text(out)
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
