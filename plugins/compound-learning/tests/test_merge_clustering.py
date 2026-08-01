"""Clustering contract for scripts/auto-consolidate.py (Stream D / AC4).

AC4's real defect is the union floor, not the candidate search: the three
`teammate-*` learnings have pairwise cosines 0.7313 / 0.7802 / 0.7878, all below
the cosine union threshold, so today no edge is ever drawn between them and the
cluster does not exist at all. The fix is a second, independent union signal --
title-slug Jaccard, gated by a minimum cosine -- so the cosine floor does not
have to be dropped far enough to flood the review queue.

These tests are deliberately built on SYNTHETIC embeddings inserted straight
into vec_learnings. That buys exact, reproducible cosines (the whole point of
the change is a numeric boundary) and costs nothing at runtime, so the only test
that touches the real corpus is the marked-slow AC4 demonstration at the bottom.

Two properties are load-bearing and are asserted from both sides:

  * the Jaccard arm must union a pair the cosine arm alone would miss, and must
    NOT union a pair that merely shares title tokens while being semantically
    unrelated (that is what the min-cosine conjunct is for);
  * widening DISCOVERY must not widen AUTO-MERGE. A cluster held together only
    by the Jaccard arm still has to land in the human review queue.
"""

import importlib.util
import math
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import lib.db as db  # noqa: E402,F401  (schema side effects via the isolated_db fixture)


def _load_auto_consolidate():
    spec = importlib.util.spec_from_file_location(
        "auto_consolidate", PLUGIN_ROOT / "scripts" / "auto-consolidate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ac = _load_auto_consolidate()

EMBED_DIM = 384


# --- synthetic embedding helpers -------------------------------------------
#
# Every vector lives in a 2-D coordinate plane of the 384-D space. Two vectors
# in the SAME plane at angles a and b have cosine cos(a - b); two vectors in
# DISJOINT planes have cosine exactly 0. That makes each cluster's internal
# geometry exact and guarantees clusters cannot accidentally bleed into each
# other.


def _planar_vec(axis_a: int, axis_b: int, theta: float) -> list:
    v = [0.0] * EMBED_DIM
    v[axis_a] = math.cos(theta)
    v[axis_b] = math.sin(theta)
    return v


def _angle_for_cosine(cosine: float) -> float:
    return math.acos(cosine)


def _add_doc(conn, tmp_path: Path, file_name: str, h1: str, topic: str, vec: list) -> str:
    """Insert a global-scope learning with a hand-chosen embedding.

    The file is written to disk because classify_cluster() routes clusters with
    missing files to REVIEW -- without a real file every classification test
    would pass for the wrong reason.
    """
    doc_id = file_name.replace(".md", "")
    path = tmp_path / file_name
    content = (
        f"# {h1}\n\n"
        f"**Type:** gotcha\n"
        f"**Topic:** {topic}\n\n"
        f"## Problem\n{h1} body text.\n\n"
        f"## Solution\nDo the thing described in {h1}.\n"
    )
    path.write_text(content, encoding="utf-8")

    conn.execute(
        "INSERT INTO learnings "
        "(id, content, scope, repo, file_path, topic, keywords, created_at) "
        "VALUES (?, ?, 'global', '', ?, ?, '', ?)",
        (doc_id, content, str(path), topic, datetime.now(timezone.utc).isoformat()),
    )
    conn.execute(
        "INSERT INTO vec_learnings (id, embedding) VALUES (?, ?)",
        (doc_id, struct.pack(f"{EMBED_DIM}f", *vec)),
    )
    conn.execute("INSERT INTO fts_learnings (id, content) VALUES (?, ?)", (doc_id, content))
    conn.commit()
    return doc_id


def _cluster_containing(clusters: list, doc_id: str):
    """Return the set of member ids of the cluster holding *doc_id*, or None."""
    for cluster in clusters:
        ids = {m["id"] for m in cluster["members"]}
        if doc_id in ids:
            return ids
    return None


def _cluster_dict_containing(clusters: list, doc_id: str):
    for cluster in clusters:
        if doc_id in {m["id"] for m in cluster["members"]}:
            return cluster
    return None


# --- the second union signal ------------------------------------------------


def test_jaccard_unions_pair_below_cosine_floor(isolated_db, tmp_path) -> None:
    """A title-similar pair below the cosine union floor must still cluster.

    Modelled on the measured teammate trio: cosine 0.73 (below any cosine union
    floor this plan contemplates) with title-slug Jaccard 4/7 = 0.571 (above the
    0.33 floor) and cosine above the 0.70 min-cosine conjunct.

    Fails if the Jaccard arm is removed: with only the cosine arm these two draw
    no edge at all, which is exactly today's state.
    """
    _config, conn = isolated_db
    theta = _angle_for_cosine(0.73)

    a = _add_doc(
        conn, tmp_path,
        "teammate-idle-without-report-2026-07-23.md",
        "Teammate goes idle without a report", "delegation",
        _planar_vec(0, 1, 0.0),
    )
    b = _add_doc(
        conn, tmp_path,
        "teammate-idle-without-report-read-artifacts-fully-2026-07-21.md",
        "Teammate idle notifications carry no payload", "delegation",
        _planar_vec(0, 1, theta),
    )

    clusters = ac.find_clusters(conn)

    assert _cluster_containing(clusters, a) == {a, b}, (
        "a cosine-0.73 / Jaccard-0.57 pair must be unioned by the title arm; "
        f"clusters were {[sorted(m['id'] for m in c['members']) for c in clusters]}"
    )


def test_jaccard_does_not_union_below_min_cosine(isolated_db, tmp_path) -> None:
    """Identical title tokens are not enough on their own.

    Jaccard 1.0 at cosine 0.40 must stay separate. Without the min-cosine
    conjunct, every same-topic-prefix filename in the corpus would chain into
    one giant cluster and the review queue would become unreadable.
    """
    _config, conn = isolated_db
    theta = _angle_for_cosine(0.40)

    a = _add_doc(
        conn, tmp_path,
        "identical-title-tokens-here-2026-07-01.md",
        "First unrelated learning", "delegation",
        _planar_vec(0, 1, 0.0),
    )
    b = _add_doc(
        conn, tmp_path,
        "identical-title-tokens-here-2026-07-15.md",
        "Second unrelated learning", "delegation",
        _planar_vec(0, 1, theta),
    )

    clusters = ac.find_clusters(conn)

    assert _cluster_containing(clusters, a) is None
    assert _cluster_containing(clusters, b) is None


def test_title_slug_tokenizer_drops_trailing_date(isolated_db, tmp_path) -> None:
    """A shared trailing date must not count as title similarity.

    Every learning file name ends in `-YYYY-MM-DD`, so two files written on the
    same day share three tokens (`2026`, `07`, `21`) for free if the date
    survives tokenization. The fixture pair is chosen so that the date is the
    only thing deciding the outcome, straddling the 0.50 mergeTitleJaccard
    floor:

      * stripped   {alpha, widget} vs {beta}                  -> 0/3 = 0.0000
      * unstripped {alpha, widget, 2026, 07, 21}
                   vs {beta, 2026, 07, 21}                    -> 3/6 = 0.5000

    Cosine is 0.75: under the 0.85 cosine union floor, so the cosine arm draws
    no edge, and over the 0.70 min-cosine conjunct, so the Jaccard arm is the
    only thing that can. Delete `_DATE_SUFFIX_RE.sub` from `_title_tokens` and
    the un-stripped 0.5000 clears the floor, these two union, and this test
    fails. Behavioural check: they must NOT cluster.
    """
    _config, conn = isolated_db
    theta = _angle_for_cosine(0.75)

    a = _add_doc(
        conn, tmp_path,
        "alpha-widget-2026-07-21.md",
        "Alpha widget behaviour", "delegation",
        _planar_vec(0, 1, 0.0),
    )
    b = _add_doc(
        conn, tmp_path,
        "beta-2026-07-21.md",
        "Beta behaviour", "delegation",
        _planar_vec(0, 1, theta),
    )

    clusters = ac.find_clusters(conn)

    assert _cluster_containing(clusters, a) is None, (
        "files sharing only their date token must not be unioned"
    )
    assert _cluster_containing(clusters, b) is None


# --- the safety property for the whole stream -------------------------------


def test_auto_merge_gate_not_widened(isolated_db, tmp_path) -> None:
    """Widening discovery must not widen autonomous merging.

    A cluster held together ONLY by the Jaccard arm (cosine 0.73, well under the
    0.92 auto-merge floor) with differing H1 headings must classify as REVIEW.

    The AUTO control in the same DB is what makes this a real assertion rather
    than an artifact of the fixture: it proves this harness CAN produce an AUTO
    verdict, so the REVIEW above is a decision and not a side effect of a
    missing file or an unresolvable topic.
    """
    _config, conn = isolated_db

    jaccard_a = _add_doc(
        conn, tmp_path,
        "teammate-idle-without-report-2026-07-23.md",
        "Teammate goes idle without a report", "delegation",
        _planar_vec(0, 1, 0.0),
    )
    jaccard_b = _add_doc(
        conn, tmp_path,
        "teammate-idle-without-report-read-artifacts-fully-2026-07-21.md",
        "Teammate idle notifications carry no payload", "delegation",
        _planar_vec(0, 1, _angle_for_cosine(0.73)),
    )

    # Control: near-identical pair, one topic, one H1 -> must still auto-merge.
    auto_a = _add_doc(
        conn, tmp_path,
        "sqlite-busy-timeout-default-2026-07-02.md",
        "Sqlite busy timeout default", "database",
        _planar_vec(2, 3, 0.0),
    )
    auto_b = _add_doc(
        conn, tmp_path,
        "sqlite-busy-timeout-default-again-2026-07-09.md",
        "Sqlite busy timeout default", "database",
        _planar_vec(2, 3, _angle_for_cosine(0.95)),
    )

    clusters = ac.find_clusters(conn)

    jaccard_cluster = _cluster_dict_containing(clusters, jaccard_a)
    assert jaccard_cluster is not None, (
        "the Jaccard-only pair must be discovered before its classification can be judged"
    )
    assert {m["id"] for m in jaccard_cluster["members"]} == {jaccard_a, jaccard_b}
    decision, reason = ac.classify_cluster(jaccard_cluster)
    assert decision == "REVIEW", (
        f"a Jaccard-only cluster with differing H1s must go to review, got {decision}: {reason}"
    )

    auto_cluster = _cluster_dict_containing(clusters, auto_a)
    assert auto_cluster is not None
    assert {m["id"] for m in auto_cluster["members"]} == {auto_a, auto_b}
    auto_decision, auto_reason = ac.classify_cluster(auto_cluster)
    assert auto_decision == "AUTO", (
        f"control cluster should still auto-merge, got {auto_decision}: {auto_reason}"
    )


# --- AC4's real-data demonstration ------------------------------------------

NPM_TRIO = {
    "npm-overrides-cannot-fix-bundled-dependencies-2026-07-27.md",
    "npm-bundled-deps-unfixable-by-overrides-2026-07-22.md",
    "npm-overrides-noop-on-bundled-deps-2026-07-16.md",
}

TEAMMATE_TRIO = {
    "teammate-idle-without-report-read-artifacts-2026-07-21.md",
    "teammate-idle-without-report-2026-07-23.md",
    "teammate-auditors-idle-without-delivering-report-2026-07-22.md",
}


@pytest.mark.slow
def test_named_clusters_group_together(indexed_corpus) -> None:
    """AC4 on the real corpus: each named trio lands in ONE cluster.

    The teammate trio draws no edge at all today (pairwise cosines
    0.7313 / 0.7802 / 0.7878), so this is a genuine before/after and not a
    restatement of current behaviour.

    Skips cleanly when the corpus does not contain these files, matching the
    bundled-sample fallback in conftest.py, so the suite still runs for a
    developer without this personal corpus.
    """
    _config, conn, _stats = indexed_corpus

    present = {
        row[0].rsplit("/", 1)[-1]
        for row in conn.execute("SELECT file_path FROM learnings").fetchall()
    }
    missing = (NPM_TRIO | TEAMMATE_TRIO) - present
    if missing:
        pytest.skip(f"corpus does not contain the AC4 demonstration files: {sorted(missing)}")

    clusters = ac.find_clusters(conn)

    def cluster_index_for(file_name: str):
        for i, cluster in enumerate(clusters):
            if any(m["file"] == file_name for m in cluster["members"]):
                return i
        return None

    for name, trio in (("npm", NPM_TRIO), ("teammate", TEAMMATE_TRIO)):
        indices = {f: cluster_index_for(f) for f in sorted(trio)}
        assert None not in indices.values(), (
            f"{name} trio members not clustered at all: "
            f"{[f for f, i in indices.items() if i is None]}"
        )
        assert len(set(indices.values())) == 1, (
            f"{name} trio split across clusters: {indices}"
        )
