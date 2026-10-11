"""Saving and restoring the SGP30 baseline.

The SGP30 algorithm learns its own baseline over many hours. By saving it to file and sending
it back to the sensor at start-up, the readings are stable right away even after a restart.
"""

import json
import os
import time
from pathlib import Path

# Sensirion: a baseline older than 7 days must not be restored.
MAX_AGE_S = 7 * 24 * 3600


class BaselineStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> tuple[int, int] | None:
        """Returns (eco2_base, tvoc_base) if there is a valid and recent baseline, otherwise None."""
        try:
            data = json.loads(self.path.read_text())
            if time.time() - float(data["saved_at"]) > MAX_AGE_S:
                return None
            return int(data["eco2_base"]), int(data["tvoc_base"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, eco2_base: int, tvoc_base: int) -> bool:
        """Writes the baseline atomically (temporary file + rename)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "eco2_base": int(eco2_base),
                "tvoc_base": int(tvoc_base),
                "saved_at": time.time(),
            }))
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False
