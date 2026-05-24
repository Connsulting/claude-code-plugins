"""
Dependency bootstrap for compound-learning plugin.

Installs all required Python packages into an isolated target directory
(~/.claude/state/compound-learning/site-packages/) so they never pollute
the system Python environment.

This is the SINGLE install path for the plugin. hooks/setup.sh delegates
here; nothing else should call pip directly.

Call install() from the SessionStart hook before importing any third-party
dependencies.
"""

import importlib
import logging
import os
import subprocess
import sys
from typing import List

_SITE_PACKAGES = os.path.expanduser('~/.claude/state/compound-learning/site-packages')
_LOG_FILE = os.path.expanduser('~/.claude/plugins/compound-learning/activity.log')

logging.basicConfig(
    filename=_LOG_FILE,
    format='[%(asctime)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def _all_importable(packages: List[str]) -> bool:
    """Return True only if every package in the list can actually be imported.

    Uses import_module rather than find_spec so that env corruption (e.g.
    torch/transformers version skew that raises at import time) is detected
    and triggers a re-install.
    """
    # Add site-packages to path so we can test the target install
    if _SITE_PACKAGES not in sys.path:
        sys.path.insert(1, _SITE_PACKAGES)
    for pkg in packages:
        try:
            importlib.import_module(pkg)
        except Exception:
            return False
    return True


def install(force: bool = False) -> bool:
    """Ensure all required dependencies are installed into the target directory.

    Returns True if deps are ready, False if install failed.
    """
    required = ['sqlite_vec', 'sentence_transformers']

    if not force and _all_importable(required):
        return True

    os.makedirs(_SITE_PACKAGES, exist_ok=True)
    logger.info('[bootstrap] Installing Python dependencies into %s', _SITE_PACKAGES)

    # torch must come first from the CPU-only index so sentence-transformers
    # does not pull in CUDA wheels (this machine has no NVIDIA GPU).
    torch_cmd = [
        sys.executable, '-m', 'pip', 'install', '--quiet',
        '--target', _SITE_PACKAGES,
        'torch',
        '--index-url', 'https://download.pytorch.org/whl/cpu',
    ]

    rest_cmd = [
        sys.executable, '-m', 'pip', 'install', '--quiet',
        '--target', _SITE_PACKAGES,
        'pysqlite3-binary',
        'sqlite-vec',
        'sentence-transformers',
    ]

    for cmd in (torch_cmd, rest_cmd):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error('[bootstrap] pip failed: %s', result.stderr.strip())
            return False

    logger.info('[bootstrap] Dependencies installed successfully')
    return True


if __name__ == '__main__':
    success = install(force='--force' in sys.argv)
    sys.exit(0 if success else 1)
