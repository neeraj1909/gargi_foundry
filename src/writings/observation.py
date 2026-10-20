from __future__ import annotations

from dataclasses import dataclass

from .domain import Run
from .store import RunEvent, SQLiteRunStore


@dataclass(frozen=True, slots=True)
class RunObservation:
    """The current run state plus retained events for that run"""

    run: Run
    events: tuple[RunEvent, ...]

    @property
    def last_sequence(self) -> int:
        if not self.events:
            return 0

        return self.events[-1].sequence


class RunObserver:
    """Reconnect-safe read boundary for an already-admitted run."""

    def __init__(self, store: SQLiteRunStore) -> None:
        self._store = store

    def observe(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> RunObservation:
        run = self._store.get_run(run_id)
        events = self._store.events(
            run_id,
            after_sequence=after_sequence,
        )

        return RunObservation(
            run=run,
            events=events,
        )

    def reconnect(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> RunObservation:
        """Observe an existing run without submitting a new request"""

        return self.observe(
            run_id,
            after_sequence=after_sequence,
        )
