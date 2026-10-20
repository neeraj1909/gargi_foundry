from __future__ import annotations

import hashlib
import importlib
import sys
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE_ROOT))

domain = importlib.import_module("writings.domain")

DomainError = domain.DomainError
ReviewRequest = domain.ReviewRequest
ReviewResult = domain.ReviewResult
Run = domain.Run
RunState = domain.RunState
SourceSnapshot = domain.SourceSnapshot
Suggestion = domain.Suggestion


def make_snapshot(text: str = "hello world") -> SourceSnapshot:
    return SourceSnapshot.create(
        document_id="document-1",
        version=1,
        text=text,
    )


def make_request(snapshot: SourceSnapshot | None = None) -> ReviewRequest:
    return ReviewRequest(
        request_id="request-1",
        owner_id="owner-1",
        source=snapshot or make_snapshot(),
        intent="Review this text for clarity.",
        idempotency_key="idempotency-1",
    )


def test_source_snapshot_uses_exact_utf8_sha256_digest() -> None:
    text = "Café\n"

    snapshot = make_snapshot(text)

    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()

    assert snapshot.digest == expected
    assert snapshot.text == text


def test_source_snapshot_does_not_normalize_unicode_or_whitespace() -> None:
    text = "  Cafe\u0301\r\n"

    snapshot = make_snapshot(text)

    assert snapshot.text == text
    assert snapshot.digest == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_text_span_uses_half_open_offsets() -> None:
    snapshot = make_snapshot("hello world")

    assert snapshot.text_span(0, 5) == "hello"
    assert snapshot.text_span(6, 11) == "world"


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (-1, 2),
        (4, 2),
        (0, 12),
    ],
)
def test_invalid_text_spans_are_rejected(start: int, end: int) -> None:
    snapshot = make_snapshot()

    with pytest.raises(DomainError):
        snapshot.text_span(start, end)


def test_request_preserves_identity_and_idempotency_key() -> None:
    snapshot = make_snapshot()
    request = make_request(snapshot)

    assert request.request_id == "request-1"
    assert request.owner_id == "owner-1"
    assert request.idempotency_key == "idempotency-1"
    assert request.document_id == snapshot.document_id
    assert request.source_version == snapshot.version
    assert request.source_digest == snapshot.digest


def test_run_accepts_valid_state_transitions() -> None:
    run = Run(run_id="run-1", request=make_request())

    queued = run.transition(RunState.QUEUED)
    running = queued.transition(RunState.RUNNING)
    succeeded = running.transition(RunState.SUCCEEDED)

    assert run.state is RunState.ADMITTED
    assert queued.state is RunState.QUEUED
    assert running.state is RunState.RUNNING
    assert succeeded.state is RunState.SUCCEEDED
    assert succeeded.is_terminal


def test_run_rejects_invalid_state_transition() -> None:
    run = Run(run_id="run-1", request=make_request())

    with pytest.raises(DomainError, match="invalid run transition"):
        run.transition(RunState.RUNNING)


def test_run_attempt_increment_returns_a_new_run() -> None:
    run = Run(run_id="run-1", request=make_request())

    attempted = run.with_attempt()

    assert run.attempt == 0
    assert attempted.attempt == 1
    assert attempted.run_id == run.run_id
    assert attempted.request == run.request


def test_empty_review_result_is_an_explicit_no_change_result() -> None:
    result = ReviewResult(run_id="run-1")

    assert result.suggestions == ()
    assert result.has_changes is False
    assert result.is_no_change is True


def test_domain_objects_round_trip_through_dictionaries() -> None:
    snapshot = make_snapshot()
    request = make_request(snapshot)
    run = Run(
        run_id="run-1",
        request=request,
        state=RunState.RUNNING,
        attempt=1,
    )
    suggestion = Suggestion(
        suggestion_id="suggestion-1",
        run_id=run.run_id,
        document_id=snapshot.document_id,
        source_version=snapshot.version,
        source_digest=snapshot.digest,
        start=0,
        end=5,
        quote="hello",
        category="clarity",
        rationale="The opening can be clearer.",
        replacement="Hello",
    )
    result = ReviewResult(
        run_id=run.run_id,
        suggestions=(suggestion,),
    )

    assert SourceSnapshot.from_dict(snapshot.to_dict()) == snapshot
    assert ReviewRequest.from_dict(request.to_dict()) == request
    assert Run.from_dict(run.to_dict()) == run
    assert ReviewResult.from_dict(result.to_dict()) == result
