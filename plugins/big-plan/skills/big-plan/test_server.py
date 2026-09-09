"""Tests for the Big Plan HTTP server and plan index."""

from __future__ import annotations

import importlib.util
import hashlib
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


RUNTIME_DIR = Path(__file__).parent
SERVER_PATH = RUNTIME_DIR / "server.py"
STYLE_PATH = RUNTIME_DIR / "assets" / "style.css"
sys.path.insert(0, str(RUNTIME_DIR))
SPEC = importlib.util.spec_from_file_location("big_plan_server", SERVER_PATH)
assert SPEC and SPEC.loader
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)
import render


class ListMarkdownTest(unittest.TestCase):
    def test_prunes_ignored_trees_before_visiting_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            published = root / ".projects" / "published.md"
            published.parent.mkdir()
            published.write_text("# Published\n")
            server.promote(published)

            ignored = root / "node_modules" / "package" / "hidden.md"
            ignored.parent.mkdir(parents=True)
            ignored.write_text("# Hidden\n")
            server.promote(ignored)

            visited: list[Path] = []
            real_walk = os.walk

            def recording_walk(path: Path):
                for directory, names, files in real_walk(path):
                    visited.append(Path(directory).relative_to(root))
                    yield directory, names, files

            old_filter = server.INDEX_FILTER
            server.INDEX_FILTER = "plans"
            try:
                with mock.patch.object(server.os, "walk", side_effect=recording_walk):
                    plans = server.list_markdown(root)
            finally:
                server.INDEX_FILTER = old_filter

            self.assertEqual(plans, [published])
            self.assertIn(Path("."), visited)
            self.assertIn(Path(".projects"), visited)
            self.assertFalse(
                any("node_modules" in path.parts for path in visited),
                f"ignored tree was traversed: {visited}",
            )


class SidebarControlsTest(unittest.TestCase):
    def test_plan_chrome_has_persistent_sidebar_controls(self) -> None:
        page = render.render_html("# Plan\n\n## Section\n\nText", "Plan", {})

        self.assertIn('class="toc-toggle sidebar-handle" aria-controls="toc-rail"', page)
        self.assertIn('class="comments-toggle sidebar-handle" aria-controls="comments-rail"', page)
        self.assertIn('<aside class="toc-rail" id="toc-rail"', page)
        self.assertIn('<aside class="comments-rail" id="comments-rail"', page)


class ExternalLinkRenderTest(unittest.TestCase):
    def test_http_links_open_in_a_new_tab_but_page_anchors_do_not(self) -> None:
        page = render.render_html(
            "## Section\n\n[HTTP](http://example.com/docs) and "
            "[HTTPS](https://example.net/docs)\n",
            "Plan",
            {},
        )

        for url, label in (
            ("http://example.com/docs", "HTTP"),
            ("https://example.net/docs", "HTTPS"),
        ):
            self.assertRegex(
                page,
                rf'<a(?=[^>]*href="{re.escape(url)}")'
                r'(?=[^>]*target="_blank")'
                r'(?=[^>]*rel="noopener noreferrer")[^>]*>'
                rf'{label}</a>',
            )
        self.assertNotRegex(
            page,
            r'<a class="anchor-link" href="#section"[^>]+(?:target|rel)=',
        )
        self.assertNotRegex(
            page,
            r'<a href="#section"[^>]+(?:target|rel)=',
        )


class AtxHeadingRenderTest(unittest.TestCase):
    """A leading # is a heading only when a space or tab follows it."""

    def body(self, md_text: str) -> str:
        return render._convert(md_text)[0]

    def test_pr_reference_starting_a_paragraph_is_not_a_heading(self) -> None:
        body = self.body("#2391's ground was taken\n")

        self.assertNotIn("<h1", body)
        self.assertIn("#2391's ground was taken", body)

    def test_pr_reference_starting_a_bullet_is_not_a_heading(self) -> None:
        body = self.body("- #2391's ground in a bullet\n")

        self.assertNotIn("<h1", body)
        self.assertRegex(body, r"<li[^>]*>#2391's ground in a bullet</li>")

    def test_real_headings_still_render(self) -> None:
        body = self.body("# Top\n\n## Section\n\n> ### Quoted\n\n- #### In a list\n")

        self.assertIn('<h1 id="top">Top</h1>', body)
        self.assertIn('<h2 id="section">Section</h2>', body)
        self.assertIn('<h3 id="quoted">Quoted</h3>', body)
        self.assertIn('<h4 id="in-a-list">In a list</h4>', body)

    def test_closing_hashes_are_still_stripped(self) -> None:
        body = self.body("## Trailing hashes ##\n")

        self.assertIn('<h2 id="trailing-hashes">Trailing hashes</h2>', body)

    def test_mid_line_hashtag_is_untouched(self) -> None:
        body = self.body("A PR #1234 mid-line\n")

        self.assertNotIn("<h1", body)
        self.assertIn("A PR #1234 mid-line", body)

    def test_hash_reference_inside_a_fence_stays_literal(self) -> None:
        body = self.body("Intro\n\n```text\n#1234 fenced\n```\n")

        self.assertNotIn("<h1", body)
        self.assertIn("#1234 fenced", body)


class ContentWidthTest(unittest.TestCase):
    def test_content_can_use_a_wide_desktop_viewport(self) -> None:
        style = STYLE_PATH.read_text()

        self.assertIn(
            ".content {\n"
            "  grid-area: content;\n"
            "  padding: 24px 32px 80px;\n"
            "  max-width: 1600px;\n"
            "  width: 100%;",
            style,
        )


class DecisionNoteRenderTest(unittest.TestCase):
    def test_selected_decision_restores_its_note(self) -> None:
        question = "Which rollout should we use?"
        anchor = "d-" + hashlib.md5(question.encode("utf-8")).hexdigest()[:10]
        page = render.render_html(
            "```decide\nWhich rollout should we use?\n- Staged\n- Full\n```",
            "Plan",
            {"comments": [{
                "type": "decision",
                "anchor": anchor,
                "choices": ["Staged"],
                "note": "Begin with the phone review.",
                "timestamp": "2026-09-08T00:00:00+00:00",
            }]},
        )

        self.assertIn('class="decide-note-input"', page)
        self.assertIn("Begin with the phone review.", page)
        self.assertIn('value="Staged" checked', page)


if __name__ == "__main__":
    unittest.main()
