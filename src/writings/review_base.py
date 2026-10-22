from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .domain import DomainError, Run, SourceSnapshot
from .store import SQLiteRunStore

__all__ = [
    "FrozenReviewBase",
    "ReviewBaseReader",
]


@dataclass(frozen=True, slots=True)
class FrozenReviewBase:
    """Immutable source bound to one admitted review run."""

    run_id: str
    source: SourceSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str):
            raise DomainError("run_id must be a string")

        if not self.run_id.strip():
            raise DomainError("run_id must not be empty")

        if not isinstance(self.source, SourceSnapshot):
            raise DomainError("source must be a SourceSnapshot")

    @property
    def document_id(self) -> str:
        return self.source.document_id

    @property
    def version(self) -> int:
        return self.source.version

    @property
    def text(self) -> str:
        return self.source.text

    @property
    def digest(self) -> str:
        return self.source.digest

    @classmethod
    def from_run(cls, run: Run) -> FrozenReviewBase:
        if not isinstance(run, Run):
            raise TypeError("run must be a Run")

        return cls(
            run_id=run.run_id,
            source=run.request.source,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source": self.source.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> FrozenReviewBase:
        if not isinstance(data, Mapping):
            raise DomainError("review base data must be a mapping")

        try:
            run_id = data["run_id"]
            source_data = data["source"]
        except KeyError as exc:
            raise DomainError(f"review base is missing {exc.args[0]}") from exc

        return cls(
            run_id=run_id,
            source=SourceSnapshot.from_dict(source_data),
        )


class ReviewBaseReader:
    """Reads the immutable source retained by an admitted review run."""

    def __init__(self, store: SQLiteRunStore) -> None:
        if not isinstance(store, SQLiteRunStore):
            raise TypeError("store must be a SQLiteRunStore")

        self._store = store

    def read(self, run_id: str) -> FrozenReviewBase:
        run = self._store.get_run(run_id)
        return FrozenReviewBase.from_run(run)
