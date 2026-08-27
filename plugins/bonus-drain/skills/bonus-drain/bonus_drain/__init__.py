"""Provider-neutral Bonus Drain runtime.

The package deliberately exposes small modules instead of provider-specific helpers.  Shell
entrypoints under ``skills/bonus-drain`` are compatibility wrappers around :mod:`bonus_drain.cli`.
"""

from __future__ import annotations

__version__ = "0.3.1"

__all__ = ["__version__"]
