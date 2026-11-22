"""Make the starter kit's ``a2a_connect`` package importable from the bridge.

The bridge builds its A2A frames and trace spans with the kit's ``protocol``,
``audio_frames``, ``tracing`` and ``fragments`` modules (task 3.7), so the kit
must be on the import path wherever the bridge runs:

* In the container, the Dockerfile copies ``a2a_connect`` next to ``bridge`` at
  ``/app``, so ``import a2a_connect`` already works and this is a no-op.
* In the repository (unit tests, the local harness, ``deploy.py`` build), the
  kit lives at ``src/starter-kit``; this adds that directory to ``sys.path`` on
  first import so no PYTHONPATH wiring is needed.

Nothing here imports the kit itself; it only arranges the path.
"""
from __future__ import annotations

import os
import sys


def ensure_kit_importable() -> None:
    try:
        import a2a_connect  # noqa: F401
        return
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))           # .../bridge
    # Repository layout: src/audio-agent/bridge -> src/starter-kit.
    candidate = os.path.normpath(os.path.join(here, "..", "..", "starter-kit"))
    if os.path.isdir(os.path.join(candidate, "a2a_connect")) and candidate not in sys.path:
        sys.path.insert(0, candidate)
