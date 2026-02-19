#!/usr/bin/env python3
"""
MCP server for the compound-learning plugin.

Exposes search and index functionality as MCP tools so that any
MCP-compatible client (Codex CLI, Claude Code, etc.) can query the
learnings knowledge base without needing native hooks.

Run:
    python3 server.py            # stdio transport (default)
    python3 server.py --sse      # SSE transport on port 8765
"""

import sys
import os

# Locate plugin root so lib/ is importable regardless of cwd
_PLUGIN_ROOT = os.environ.get(
    "CLAUDE_PLUGIN_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
sys.path.insert(0, _PLUGIN_ROOT)

import json
import hashlib
import re
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from mcp.server.fastmcp import FastMCP

import lib.db as db

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "compound-learning",
    version="0.2.1",
    description=(
        "Knowledge-base backed by SQLite-vec. "
        "Search and index learnings extracted from coding sessions."
    ),
)


# ---------------------------------------------------------------------------
# Helpers (thin wrappers around existing modules)
# ---------------------------------------------------------------------------

# Reuse search helpers from scripts/search-learnings.py
STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "shall", "can", "need", "dare",
    "ought", "used", "to", "of", "in", "for", "on", "with", "at", "by",
    "from", "as", "into", "through", "during", "before", "after", "above",
    "below", "between", "under", "again", "further", "then", "once",
    "here", "there", "when", "where", "why", "how", "all", "each", "few",
    "more", "most", "other", "some", "such", "no", "nor", "not", "only",
    "own", "same", "so", "than", "too", "very", "just", "and", "but",
    "if", "or", "because", "until", "while", "about", "against", "this",
    "that", "these", "those", "what", "which", "who", "whom", "its", "it",
}


def _extract_keywords(query: str) -> set[str]:
    tokens = re.findall(r"\b\w+\b", query.lower())
    return {t for t in tokens if t not in STOPWORDS and len(t) >= 2}


def _keyword_overlap(keywords: set[str], document: str) -> float:
    if not keywords:
        return 0.0
    doc_lower = document.lower()
    return sum(1 for kw in keywords if kw in doc_lower) / len(keywords)


def _detect_repos(cwd: str, home: str) -> list[str]:
    repos: list[str] = []
    current = Path(cwd)
    home_path = Path(home)
    while True:
        if (current / ".projects" / "learnings").exists() and current != home_path:
            repos.append(current.name)
        if current == home_path or current.parent == current:
            break
        current = current.parent
    return repos


def _query_keyword(config: Any, keyword: str, repos: list[str], n: int):
    conn = db.get_connection(config)
    try:
        results = db.search(conn, keyword, repos, n_results=n, threshold=1.0)
        for r in results:
            r["matched_keyword"] = keyword
        return results
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def search_learnings(
    query: str,
    working_dir: str = "",
    max_results: int = 5,
    peek: bool = False,
    exclude_ids: str = "",
    keywords_json: str = "",
) -> str:
    """Search the learnings knowledge base using semantic vector search.

    Args:
        query: Natural-language search query (e.g. "JWT refresh token rotation").
        working_dir: Current working directory – used to detect repo-scoped learnings.
        max_results: Maximum number of results to return.
        peek: If true, return only high-confidence results (distance < threshold).
        exclude_ids: Comma-separated learning IDs to exclude (already seen).
        keywords_json: Optional JSON array of keywords for parallel search
                       (e.g. '["jwt", "authentication"]'). Overrides query.
    """
    config = db.load_config()
    high_threshold = config["learnings"]["highConfidenceThreshold"]
    possible_threshold = config["learnings"]["possiblyRelevantThreshold"]
    keyword_weight = config["learnings"]["keywordBoostWeight"]

    # Parse keywords
    if keywords_json:
        try:
            keywords = json.loads(keywords_json)
            if not isinstance(keywords, list) or not keywords:
                keywords = [query] if query else []
        except json.JSONDecodeError:
            keywords = [query] if query else []
    else:
        keywords = [query] if query else []

    if not keywords:
        return json.dumps({"status": "empty", "message": "No keywords provided"})

    query_kw = set()
    for kw in keywords:
        query_kw.update(_extract_keywords(kw))

    cwd = working_dir or os.getcwd()
    home = os.path.expanduser("~")
    repos = _detect_repos(cwd, home)

    exclude_set = set()
    if exclude_ids:
        exclude_set = {i.strip() for i in exclude_ids.split(",") if i.strip()}
    query_size = max_results + len(exclude_set)

    # Parallel keyword search
    all_results: list[list[dict]] = []
    with ThreadPoolExecutor(max_workers=max(len(keywords), 1)) as pool:
        futures = {
            pool.submit(_query_keyword, config, kw, repos, query_size): kw
            for kw in keywords
        }
        for f in as_completed(futures):
            try:
                all_results.append(f.result())
            except Exception:
                pass

    # Merge & re-rank
    best: dict[str, dict] = {}
    for batch in all_results:
        for r in batch:
            rid = r["id"]
            if rid not in best or r["distance"] < best[rid]["distance"]:
                best[rid] = r

    merged = sorted(best.values(), key=lambda x: x["distance"])

    for r in merged:
        overlap = _keyword_overlap(query_kw, r["document"])
        r["keyword_overlap"] = round(overlap, 4)
        r["original_distance"] = r["distance"]
        r["distance"] = round(r["distance"] * (1 - keyword_weight * overlap), 4)

    merged.sort(key=lambda x: x["distance"])

    if exclude_set:
        merged = [r for r in merged if r["id"] not in exclude_set]

    high = [r for r in merged if r["distance"] < high_threshold]
    possible = [r for r in merged if high_threshold <= r["distance"] < possible_threshold]

    if peek:
        if high:
            return json.dumps({"status": "found", "count": len(high), "keywords_searched": keywords, "learnings": high}, indent=2)
        return json.dumps({"status": "empty", "keywords_searched": keywords})

    if not high and not possible:
        return json.dumps({
            "status": "no_results",
            "message": f"No relevant learnings found (threshold {possible_threshold})",
            "query": query,
            "repos_searched": repos,
        })

    parts = []
    if high:
        parts.append(f"{len(high)} high confidence")
    if possible:
        parts.append(f"{len(possible)} possibly relevant")

    return json.dumps({
        "status": "success",
        "message": f"Found {' + '.join(parts)} learning(s)",
        "query": query,
        "repos_searched": repos,
        "high_confidence": high[:max_results],
        "possibly_relevant": possible[:max_results],
    }, indent=2)


@mcp.tool()
def index_learnings() -> str:
    """Re-index all learning markdown files into the SQLite-vec database.

    Discovers files in global (~/.projects/learnings/) and repo-scoped
    (.projects/learnings/) directories, generates embeddings, and upserts
    them.  Also prunes orphaned entries and regenerates the manifest.
    """
    # Import the indexer module
    sys.path.insert(0, os.path.join(_PLUGIN_ROOT, "skills", "index-learnings"))
    import importlib
    idx = importlib.import_module("index-learnings")

    # Capture stdout
    from io import StringIO
    buf = StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        idx.index_learning_files()
    finally:
        sys.stdout = old_stdout

    return buf.getvalue()


@mcp.tool()
def index_file(file_path: str) -> str:
    """Index a single learning markdown file into the database.

    Args:
        file_path: Absolute path to the .md file to index.
    """
    p = Path(file_path).expanduser().resolve()
    if not p.exists():
        return json.dumps({"status": "error", "message": f"File not found: {p}"})
    if not p.suffix == ".md":
        return json.dumps({"status": "error", "message": "Only .md files can be indexed"})

    sys.path.insert(0, os.path.join(_PLUGIN_ROOT, "skills", "index-learnings"))
    import importlib
    idx = importlib.import_module("index-learnings")

    config = db.load_config()
    ok = idx.index_single_file(p, config)
    if ok:
        return json.dumps({"status": "ok", "message": f"Indexed {p.name}"})
    return json.dumps({"status": "error", "message": f"Failed to index {p.name}"})


@mcp.tool()
def get_stats(working_dir: str = "") -> str:
    """Get statistics about the learnings knowledge base.

    Args:
        working_dir: Current working directory for repo-scope detection.

    Returns counts, topic breakdown, and scope information.
    """
    config = db.load_config()
    conn = db.get_connection(config)
    try:
        total = db.count_documents(conn)

        rows = conn.execute(
            "SELECT scope, repo, topic, COUNT(*) as cnt FROM learnings GROUP BY scope, repo, topic ORDER BY cnt DESC"
        ).fetchall()

        scopes: dict[str, int] = {}
        topics: dict[str, int] = {}
        for row in rows:
            scope_key = row["repo"] if row["scope"] == "repo" else "global"
            scopes[scope_key] = scopes.get(scope_key, 0) + row["cnt"]
            topics[row["topic"]] = topics.get(row["topic"], 0) + row["cnt"]

        cwd = working_dir or os.getcwd()
        home = os.path.expanduser("~")
        repos = _detect_repos(cwd, home)

        return json.dumps({
            "total_learnings": total,
            "by_scope": scopes,
            "by_topic": dict(sorted(topics.items(), key=lambda x: x[1], reverse=True)),
            "repos_in_scope": repos,
        }, indent=2)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--sse" in sys.argv:
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
