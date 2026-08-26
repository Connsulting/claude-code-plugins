"""Tests for the Big Plan HTTP server and plan index."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


RUNTIME_DIR = Path(__file__).parent
SERVER_PATH = RUNTIME_DIR / "server.py"
sys.path.insert(0, str(RUNTIME_DIR))
SPEC = importlib.util.spec_from_file_location("big_plan_server", SERVER_PATH)
assert SPEC and SPEC.loader
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


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


if __name__ == "__main__":
    unittest.main()
