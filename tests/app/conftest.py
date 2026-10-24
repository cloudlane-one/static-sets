"""Environment the entry points read at import time.

``app/static_pod_manager.py`` reads its node and paths while it is imported and refuses
to start without them, which is the behaviour the image smoke test asserts. Importing it
with a plausible node here is the cheapest way to exercise the handlers themselves; the
tests that care about a specific value patch the resolved configuration instead.
"""

from __future__ import annotations

import os

os.environ.setdefault("STATIC_SETS_NODE", "worker-1")
os.environ.setdefault("STATIC_SETS_INSTANCE", "manager-abc")
os.environ.setdefault("STATIC_SETS_STATIC_POD_PATH", "/etc/kubernetes/manifests")
os.environ.setdefault("STATIC_SETS_BUNDLE_DIR", "/var/lib/static-sets")
