#!/usr/bin/env python3
"""Deprecated source-tree entrypoint for the md/jsonl importer.

Use ``bonus-drain import-legacy --backlog ... --runs ...`` in new automation.
This helper remains only for existing callers; it never performs DB/unit cutover
and never deletes or rebuilds an existing queue.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bonus_drain import cutover, db  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deprecated compatibility wrapper for bonus-drain import-legacy"
    )
    parser.add_argument("backlog", type=Path)
    parser.add_argument("runs", type=Path)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(
            os.environ.get(
                "BONUS_DB",
                str(
                    Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
                    / "bonus-drain"
                    / "queue.db"
                ),
            )
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="removed; retained only to return a safe migration diagnostic",
    )
    args = parser.parse_args(argv)
    if args.force:
        parser.error("--force was removed because import-legacy never rebuilds an existing queue")
    queue = db.QueueDB(args.database.expanduser())
    queue.initialize()
    result = cutover.import_legacy(args.backlog, args.runs, queue)
    print(json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":")))
    print(
        "warning: migrate.py is deprecated; use bonus-drain import-legacy",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
