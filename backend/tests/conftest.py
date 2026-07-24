from __future__ import annotations

import os
import tempfile
from pathlib import Path

_test_root = Path(tempfile.mkdtemp(prefix="photosweep-tests-"))
os.environ["PHOTOSWEEP_DATA_DIR"] = str(_test_root / "data")
os.environ["PHOTOSWEEP_CACHE_DIR"] = str(_test_root / "cache")
os.environ["PHOTOSWEEP_TRASH_DIR"] = str(_test_root / "trash")
os.environ["PHOTOSWEEP_ALLOWED_MEDIA_ROOTS"] = str(_test_root / "media")
