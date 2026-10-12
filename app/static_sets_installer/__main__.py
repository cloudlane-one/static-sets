"""Entry point for the ``static-set-installer`` console script."""

from __future__ import annotations

import sys

from static_sets_installer import main

if __name__ == "__main__":
    sys.exit(main())
