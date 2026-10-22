class SaleGate:
    """Prevents repeat confirmation while work runs and for a minimum cooldown."""

    def __init__(self, cooldown_seconds: float = 1.5):
        self.cooldown_seconds = cooldown_seconds
        self._running = False
        self._not_before = 0.0

    def start(self, now: float) -> bool:
        if self._running or now < self._not_before:
            return False
        self._running = True
        self._not_before = now + self.cooldown_seconds
        return True

    def finish(self) -> None:
        self._running = False

    def can_start(self, now: float) -> bool:
        return not self._running and now >= self._not_before
