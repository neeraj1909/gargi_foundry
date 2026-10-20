from __future__ import annotations

import importlib
import sys
from dataclasses import replace
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"

if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

domain = importlib.import_module("writings.domain")

ReviewRequest = domain.ReviewRequest
ReviewResult = domain.ReviewResult
RunState = domain.RunState
SourceSnapshot = domain.SourceSnapshot

from writings.admission import AdmissionConflict
from writings.observation import RunObserver
from writings.store import SQLiteRunStore
from writings.worker import Worker


def make_request(
    *,
    request_id: str = "request-1",
    owner_id: str = "owner-1",
    intent: str = "Review this text for clarity.",
    idempotency_key: str = "idempotency-1",
) -> ReviewRequest:
    source = SourceSnapshot.create(
        document_id="document-1",
        version=1,
        text="hello world",
    )

    return ReviewRequest(
        request_id=request_id,
        owner_id=owner_id,
        source=source,
        intent=intent,
        idempotency_key=idempotency_key,
    )


class SuccessfulProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def review(self, run, *, credential: str) -> ReviewResult:
        self.calls.append((run.run_id, credential))
        return ReviewResult(run_id=run.run_id)


class FailingProvider:
    def review(self, run, *, credential: str) -> ReviewResult:
        raise RuntimeError("provider failed")


def test_admission_persists_run_event_and_notification() -> None:
    request = make_request()

    with SQLiteRunStore(":memory:") as store:
        receipt = store.admit(request)

        run = store.get_run(receipt.run_id)
        events = store.events(receipt.run_id)
        notifications = store.pending_notifications()

        assert receipt.replayed is False
        assert run.state is RunState.ADMITTED
        assert events[0].event_type == "run.admitted"
        assert len(notifications) == 1
        assert notifications[0].run_id == receipt.run_id


def test_repeating_same_request_returns_original_admission() -> None:
    request = make_request()

    with SQLiteRunStore(":memory:") as store:
        first = store.admit(request)
        second = store.admit(request)

        assert first.run_id == second.run_id
        assert second.payload_digest == first.payload_digest
        assert second.replayed is True
        assert len(store.pending_notifications()) == 1


def test_reusing_identity_for_different_payload_is_rejected() -> None:
    request = make_request()
    changed_request = replace(
        request,
        intent="Review this text for tone.",
    )

    with SQLiteRunStore(":memory:") as store:
        store.admit(request)

        with pytest.raises(
            AdmissionConflict,
            match="different work",
        ):
            store.admit(changed_request)


def test_worker_claims_run_and_owns_provider_credential() -> None:
    provider = SuccessfulProvider()
    request = make_request()

    with SQLiteRunStore(":memory:") as store:
        receipt = store.admit(request)

        worker = Worker(
            store,
            provider,
            credential="provider-secret",
        )
        outcome = worker.run_once()

        assert outcome is not None
        assert outcome.error_code is None
        assert outcome.result is not None
        assert outcome.run.state is RunState.SUCCEEDED
        assert outcome.run.attempt == 1
        assert provider.calls == [
            (receipt.run_id, "provider-secret"),
        ]
        assert store.pending_notifications() == ()

        event_types = [event.event_type for event in store.events(receipt.run_id)]

        assert event_types == [
            "run.admitted",
            "run.queued",
            "run.running",
            "run.succeeded",
        ]


def test_worker_does_not_claim_the_same_notification_twice() -> None:
    provider = SuccessfulProvider()

    with SQLiteRunStore(":memory:") as store:
        store.admit(make_request())

        worker = Worker(
            store,
            provider,
            credential="provider-secret",
        )

        first = worker.run_once()
        second = worker.run_once()

        assert first is not None
        assert second is None
        assert len(provider.calls) == 1


def test_provider_failure_becomes_a_failed_run() -> None:
    request = make_request()

    with SQLiteRunStore(":memory:") as store:
        receipt = store.admit(request)

        worker = Worker(
            store,
            FailingProvider(),
            credential="provider-secret",
        )
        outcome = worker.run_once()

        assert outcome is not None
        assert outcome.result is None
        assert outcome.run.state is RunState.FAILED
        assert outcome.error_code == "RuntimeError"

        events = store.events(receipt.run_id)

        assert events[-1].event_type == "run.failed"
        assert events[-1].payload["error_code"] == "RuntimeError"


def test_reconnect_observes_existing_run_and_retained_events() -> None:
    request = make_request()

    with SQLiteRunStore(":memory:") as store:
        receipt = store.admit(request)
        observer = RunObserver(store)

        before_worker = observer.reconnect(receipt.run_id)

        assert before_worker.run.state is RunState.ADMITTED
        assert [event.event_type for event in before_worker.events] == ["run.admitted"]

        worker = Worker(
            store,
            SuccessfulProvider(),
            credential="provider-secret",
        )
        worker.run_once()

        after_reconnect = observer.reconnect(receipt.run_id)

        assert after_reconnect.run.state is RunState.SUCCEEDED
        assert after_reconnect.run.run_id == receipt.run_id
        assert after_reconnect.last_sequence == 4


def test_reconnect_can_resume_after_a_known_event_sequence() -> None:
    request = make_request()

    with SQLiteRunStore(":memory:") as store:
        receipt = store.admit(request)

        worker = Worker(
            store,
            SuccessfulProvider(),
            credential="provider-secret",
        )
        worker.run_once()

        observer = RunObserver(store)
        resumed = observer.reconnect(
            receipt.run_id,
            after_sequence=2,
        )

        assert [event.event_type for event in resumed.events] == [
            "run.running",
            "run.succeeded",
        ]


def test_admission_and_events_survive_store_reopen(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs.sqlite3"
    request = make_request()

    first_store = SQLiteRunStore(database)
    receipt = first_store.admit(request)
    first_store.close()

    second_store = SQLiteRunStore(database)

    try:
        observer = RunObserver(second_store)
        observation = observer.reconnect(receipt.run_id)

        assert observation.run.request == request
        assert observation.run.state is RunState.ADMITTED
        assert observation.events[0].event_type == "run.admitted"
    finally:
        second_store.close()
