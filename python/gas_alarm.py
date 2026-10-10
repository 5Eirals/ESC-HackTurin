"""Allarme gas RELATIVO al valore a riposo.

Il criterio definitivo e' ancora da decidere: i parametri qui sotto sono un punto di
partenza da tarare sul campo (es. col pennarello all'alcol durante le prove).

Funzionamento:
1. Per i primi `learn_s` secondi impara il TVOC "a riposo" (media).
2. Poi il riposo segue lentamente l'ambiente (media mobile), ma solo quando non c'e' allarme.
3. ALLARME se il TVOC resta sopra `ratio_on` volte il riposo (e di almeno `min_delta_ppb`)
   per `hold_on_s` secondi consecutivi.
4. FINE ALLARME quando torna sotto `ratio_off` volte il riposo per `hold_off_s` secondi.
"""

LEARNING = "learning"
NORMAL = "normal"
ALERT = "alert"

# Il TVOC a riposo puo' essere ~0 ppb: sotto questo valore il rapporto perderebbe significato.
MIN_REST_PPB = 10.0


class GasAlarm:
    def __init__(self, learn_s=120, ratio_on=3.0, min_delta_ppb=100, hold_on_s=10,
                 ratio_off=1.5, hold_off_s=30, rest_alpha=0.01):
        self.learn_s = learn_s
        self.ratio_on = ratio_on
        self.min_delta_ppb = min_delta_ppb
        self.hold_on_s = hold_on_s
        self.ratio_off = ratio_off
        self.hold_off_s = hold_off_s
        self.rest_alpha = rest_alpha
        self.reset()

    def reset(self):
        """Ricomincia da capo (es. quando l'algoritmo del sensore riparte)."""
        self.state = LEARNING
        self.rest_tvoc = None
        self._learn_start = None
        self._learn_sum = 0.0
        self._learn_count = 0
        self._condition_since = None

    def update(self, tvoc_ppb: float, now_s: float) -> str | None:
        """Aggiorna con un nuovo campione. Ritorna "ALERT", "CLEAR" oppure None."""
        if self.state == LEARNING:
            if self._learn_start is None:
                self._learn_start = now_s
            self._learn_sum += tvoc_ppb
            self._learn_count += 1
            if now_s - self._learn_start >= self.learn_s:
                self.rest_tvoc = self._learn_sum / self._learn_count
                self.state = NORMAL
            return None

        rest = max(self.rest_tvoc, MIN_REST_PPB)

        if self.state == NORMAL:
            above = tvoc_ppb >= rest * self.ratio_on and tvoc_ppb - rest >= self.min_delta_ppb
            if not above:
                self._condition_since = None
                self.rest_tvoc += self.rest_alpha * (tvoc_ppb - self.rest_tvoc)
                return None
            if self._condition_since is None:
                self._condition_since = now_s
            if now_s - self._condition_since >= self.hold_on_s:
                self.state = ALERT
                self._condition_since = None
                return "ALERT"
            return None

        # ALERT
        if tvoc_ppb > rest * self.ratio_off:
            self._condition_since = None
            return None
        if self._condition_since is None:
            self._condition_since = now_s
        if now_s - self._condition_since >= self.hold_off_s:
            self.state = NORMAL
            self._condition_since = None
            return "CLEAR"
        return None

    def status(self, tvoc_ppb: float | None = None) -> dict:
        ratio = None
        if self.rest_tvoc is not None and tvoc_ppb is not None:
            ratio = round(tvoc_ppb / max(self.rest_tvoc, MIN_REST_PPB), 2)
        return {
            "state": self.state,
            "rest_tvoc_ppb": None if self.rest_tvoc is None else round(self.rest_tvoc, 1),
            "ratio": ratio,
            "ratio_on": self.ratio_on,
        }
