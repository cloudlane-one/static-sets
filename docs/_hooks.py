"""Build hooks for the documentation.

The CRD renderer is a notebook script in ``app/``, not part of the installed
package, so ``mkdocs-typer2`` cannot import it unless that directory is on the
build's import path. Adding it here keeps the command reference generated from the
real application instead of being written out by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
"""The directory the entry points live in."""

if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
