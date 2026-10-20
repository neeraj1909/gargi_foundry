from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .domain import ReviewResult, Run
from .store import SQLiteRunStore


class ReviewProvider(Protocol):
    """Provider boundary owned and called by the worker."""

    def review(
        self,
        run: Run,
        *,
        credential: str,
    ) -> ReviewResult:
        """Execute one admitted run."""


class ProviderContractError(RuntimeError):
    """Raised when a provider returns an invalid result."""


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    """The durable result of one worker cycle."""

    run: Run
    result: ReviewResult | None
    error_code: str | None


class Worker:
    """Claims runs and owns the provider credential."""

    def __init__(
        self,
        store: SQLiteRunStore,
        provider: ReviewProvider,
        *,
        credential: str,
    ) -> None:
        if not isinstance(credential, str) or not credential:
            raise ValueError("credential must be a non-empty string")

        self._store = store
        self._provider = provider
        self._credential = credential

    def run_once(self) -> WorkerOutcome | None:
        """Claim and execute one queued run"""

        run = self._store.claim_next()

        if run is None:
            return None

        try:
            result = self._provider.review(
                run,
                credential=self._credential,
            )

            if not isinstance(result, ReviewResult):
                raise ProviderContractError("provider must return a ReviewResult")

        except Exception as error:  # noqa: BLE001
            error_code = type(error).__name__[:128] or "provider_error"
            failed = self._store.mark_failed(
                run.run_id,
                error_code=error_code,
            )

            return WorkerOutcome(
                run=failed,
                result=None,
                error_code=error_code,
            )

        succeeded = self._store.mark_succeeded(
            run.run_id,
            result,
        )

        return WorkerOutcome(
            run=succeeded,
            result=result,
            error_code=None,
        )
