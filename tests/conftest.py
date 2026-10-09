"""Use headless Qt by default; allow an explicit platform override."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
