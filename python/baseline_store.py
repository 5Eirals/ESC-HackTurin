"""Salvataggio e ripristino della baseline dell'SGP30.

L'algoritmo dell'SGP30 impara la propria baseline in molte ore. Salvandola su file e
rimandandola al sensore all'avvio, le letture sono stabili subito anche dopo un riavvio.
"""

import json
import os
import time
from pathlib import Path

# Sensirion: una baseline piu' vecchia di 7 giorni non va ripristinata.
MAX_AGE_S = 7 * 24 * 3600


class BaselineStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> tuple[int, int] | None:
        """Ritorna (eco2_base, tvoc_base) se c'e' una baseline valida e recente, altrimenti None."""
        try:
            data = json.loads(self.path.read_text())
            if time.time() - float(data["saved_at"]) > MAX_AGE_S:
                return None
            return int(data["eco2_base"]), int(data["tvoc_base"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, eco2_base: int, tvoc_base: int) -> bool:
        """Scrive la baseline in modo atomico (file temporaneo + rename)."""
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
