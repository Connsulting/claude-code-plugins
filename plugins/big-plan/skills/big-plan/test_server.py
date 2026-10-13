"""Tests for the Big Plan HTTP server and plan index."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

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
            published = root / "plans" / "published.plan.md"
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
            self.assertIn(Path("plans"), visited)
            self.assertFalse(
                any("node_modules" in path.parts for path in visited),
                f"ignored tree was traversed: {visited}",
            )

    def test_state_plans_are_discovered_only_after_publication(self) -> None:
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home, "XDG_STATE_HOME": ""}):
            root = server.plans_root()
            plan = root / "example-repo" / "review.md"
            plan.parent.mkdir(parents=True)
            plan.write_text("# Review\n")
            self.assertTrue(server.is_plan_file(plan))
            self.assertEqual(server.list_markdown(root), [])
            server.promote(plan)
            self.assertEqual(server.list_markdown(root), [plan])
            self.assertIn("example-repo/review.md", server.render_index(root).decode())

    def test_xdg_root_and_in_repo_plan_names(self) -> None:
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(os.environ, {"XDG_STATE_HOME": state}):
            self.assertEqual(server.plans_root(), Path(state) / "big-plan" / "plans")
            self.assertTrue(server.is_plan_file(Path(state) / "big-plan/plans/repo/a.md"))
            self.assertTrue(server.is_plan_file(Path("/repo/a.plan.md")))
            self.assertTrue(server.is_plan_file(Path("/repo/PLAN.md")))
            self.assertFalse(server.is_plan_file(Path("/repo/drafts/a.md")))

    def test_external_plan_http_review_and_routing(self) -> None:
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home, "XDG_STATE_HOME": ""}):
            root = server.plans_root()
            plan = root / "example-repo" / "review.md"
            plan.parent.mkdir(parents=True)
            plan.write_text("# External Review\n")
            old_root = server.ROOT_DIR
            server.ROOT_DIR = root
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{httpd.server_port}"
            relative = "example-repo/review.md"
            try:
                request = Request(base + "/api/promote/" + relative, data=b"{}",
                                  headers={"Content-Type": "application/json"})
                with urlopen(request) as response:
                    self.assertEqual(response.status, 200)
                with urlopen(base) as response:
                    self.assertIn(relative, response.read().decode())
                with urlopen(base + "/" + relative) as response:
                    self.assertIn("External Review", response.read().decode())
                for request in (
                    Request(base + "/api/submit/" + relative),
                    Request(base + "/api/submit/" + relative,
                            data=json.dumps({"expectMode": "new_thread", "prompt": "Review"}).encode(),
                            headers={"Content-Type": "application/json"}),
                ):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(request)
                    self.assertEqual(error.exception.code, 400)
                    self.assertIn("repository cwd", error.exception.read().decode())
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
                server.ROOT_DIR = old_root


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


class HeadingCommentButtonTest(unittest.TestCase):
    def test_h2_and_h3_get_comment_buttons_h4_does_not(self) -> None:
        page = render.render_html(
            "# Title\n\n## Section\n\n### Subheading\n\n#### Deeper\n\nText\n",
            "Plan",
            {},
        )

        self.assertRegex(
            page,
            r'<button type="button" class="section-comment-button" '
            r'data-anchor="section" '
            r'aria-label="Add comment to this section">Comment</button>',
        )
        self.assertRegex(
            page,
            re.compile(
                r'<div class="heading-with-comment">'
                r'<h3[^>]*id="subheading"[^>]*>.*?</h3>'
                r'<button type="button" class="section-comment-button" '
                r'data-anchor="subheading" '
                r'aria-label="Add comment to this heading">Comment</button>'
                r"</div>",
                re.DOTALL,
            ),
        )
        self.assertIn('<h4 id="deeper"', page)
        h4_block = page[page.index('<h4 id="deeper"') : page.index("</h4>") + 5]
        next_chunk = page.split("</h4>", 1)[1].split("<p", 1)[0]
        self.assertNotIn("section-comment-button", h4_block)
        self.assertNotIn("section-comment-button", next_chunk)
        self.assertNotIn("heading-with-comment", h4_block)
        self.assertNotIn("heading-with-comment", next_chunk)

    def test_open_comment_on_h3_renders_after_the_heading_row(self) -> None:
        page = render.render_html(
            "## Section\n\n### Subheading\n\nBody\n",
            "Plan",
            {"comments": [{
                "id": "c1",
                "type": "text",
                "anchor": "subheading",
                "text": "Please expand this.",
                "timestamp": "2026-09-15T00:00:00+00:00",
                "resolved": False,
            }]},
        )

        heading_row = (
            '<div class="heading-with-comment">'
        )
        self.assertIn(heading_row, page)
        heading_idx = page.index(heading_row)
        comment_idx = page.index('class="comments-inline" data-anchor="subheading"')
        self.assertGreater(comment_idx, heading_idx)
        self.assertIn("Please expand this.", page)


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


class FeedbackHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.plan = self.root / "review.md"
        self.plan.write_text("# Review\n")
        root_patch = mock.patch.object(server, "ROOT_DIR", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.destination = {"mode": "thread", "sessionId": "mcp:author", "target": "Author",
                            "cwd": str(self.root), "reason": "authoring thread"}

    def stop_server(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def request(self, endpoint, payload=None, headers=None):
        request = Request(self.base + endpoint,
                          data=json.dumps(payload).encode() if payload is not None else None,
                          headers=headers or {"Content-Type": "application/json"})
        try:
            response = urlopen(request)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_registration_records_t3_thread_and_promotes(self) -> None:
        for thread_id in ["mcp:author", None]:
            status, record = self.request("/api/session/review.md", {
                "threadId": thread_id, "name": "Review", "cwd": str(self.root),
            })
            self.assertEqual(status, 200)
            self.assertEqual(record["threadId"], thread_id)
            self.assertEqual(json.loads(server.dispatch.provenance_path(self.plan).read_text()), record)
            self.assertTrue(server.promotion_path(self.plan).exists())

    def test_route_change_mode_or_identity_is_409_without_delivery(self) -> None:
        with mock.patch.object(server.dispatch, "route", return_value=self.destination), mock.patch.object(server.dispatch, "submit") as submit:
            for mode, session_id in [("new_thread", None), ("thread", "mcp:other")]:
                status, data = self.request("/api/submit/review.md", {
                    "prompt": "Feedback", "expectMode": mode, "expectSessionId": session_id,
                })
                self.assertEqual(status, 409)
                self.assertEqual(data["route"], self.destination)
            submit.assert_not_called()

    def test_validated_route_passes_to_delivery(self) -> None:
        with mock.patch.object(server.dispatch, "route", return_value=self.destination) as route, mock.patch.object(
            server.dispatch, "submit", return_value={**self.destination, "status": "sent"}
        ) as submit:
            status, data = self.request("/api/submit/review.md", {
                "prompt": "Feedback", "expectMode": "thread", "expectSessionId": "mcp:author",
            })
        self.assertEqual((status, data["status"]), (200, "sent"))
        route.assert_called_once()
        submit.assert_called_once_with(self.plan, "review.md", self.root, "Feedback", self.destination)

    def test_t3_preview_and_delivery_failures_are_502(self) -> None:
        failure = server.dispatch.DispatchFailed("T3 refused feedback")
        with mock.patch.object(server.dispatch, "route", side_effect=failure):
            for payload in [None, {"expectMode": "thread", "prompt": "Feedback"}]:
                status, data = self.request("/api/submit/review.md", payload)
                self.assertEqual(status, 502)
                self.assertIn("T3 refused", data["error"])
        with mock.patch.object(server.dispatch, "submit", side_effect=failure):
            status, data = self.request("/api/submit/review.md", {"prompt": "Feedback"})
            self.assertEqual(status, 502)
            self.assertIn("T3 refused", data["error"])

    def test_csrf_host_and_content_type_block_before_delivery(self) -> None:
        headers_list = [
            ({"Content-Type": "application/json", "Origin": "https://attacker.invalid"}, 403),
            ({"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}, 403),
            ({"Content-Type": "application/json", "Host": "attacker.invalid"}, 403),
            ({"Content-Type": "text/plain"}, 415),
        ]
        with mock.patch.object(server.dispatch, "submit") as submit, mock.patch.object(server.dispatch, "save_provenance") as register:
            for endpoint in ["/api/submit/review.md", "/api/session/review.md"]:
                for headers, expected in headers_list:
                    status, _ = self.request(endpoint, {"prompt": "Feedback"}, headers)
                    self.assertEqual(status, expected)
            submit.assert_not_called()
            register.assert_not_called()


if __name__ == "__main__":
    unittest.main()
