"""``python -m bonus_drain`` entrypoint."""

from __future__ import annotations

from .cli import main


if __name__ == "__main__":  # pragma: no cover - exercised by the installed wrapper
    raise SystemExit(main())
