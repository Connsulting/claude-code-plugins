"""
Ensure the plugin's isolated site-packages directory is on sys.path.

Import this module before any third-party imports (sqlite_vec,
sentence_transformers, torch) so they resolve from the target directory
populated by lib/bootstrap.py rather than the system interpreter.

Usage (at the top of every entry-point script, BEFORE lib.db):

    import lib._site_packages  # noqa: F401  -- path side-effect only
"""

import os
import sys

_SITE_PACKAGES = os.path.expanduser('~/.claude/state/compound-learning/site-packages')

if _SITE_PACKAGES not in sys.path:
    # Insert after the plugin root (position 1) so plugin-local modules still
    # take precedence, but before the system site-packages.
    sys.path.insert(1, _SITE_PACKAGES)
