from __future__ import annotations

import importlib
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"

if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

domain = importlib.import_module("writings.domain")
review_base = importlib.import_module("writings.review_base")
store_module = importlib.import_module("writings.store")

DomainError = domain.DomainError
ReviewRequest = domain.ReviewRequest
Run = domain.Run
SourceSnapshot = domain.SourceSnapshot

FrozenReviewBase = review_base.FrozenReviewBase
ReviewBaseReader = review_base.ReviewBaseReader
SQLiteRunStore = store_module.SQLiteRunStore


def make_snapshot(
    *,
    version: int = 1,
    text: str = "first draft",
) -> SourceSnapshot:
    return SourceSnapshot.create(
        document_id="document-1",
        version=version,
        text=text,
    )


def make_request(
    snapshot: SourceSnapshot | None = None,
) -> ReviewRequest:
    return ReviewRequest(
        request_id="request-1",
        owner_id="owner-1",
        source=snapshot or make_snapshot(),
        intent="Review this text for clarity.",
        idempotency_key="idempotency-1",
    )


def test_review_base_is_created_from_the_admitted_run() -> None:
    snapshot = make_snapshot()
    run = Run(
        run_id="run-1",
        request=make_request(snapshot),
    )

    review_base = FrozenReviewBase.from_run(run)

    assert review_base.run_id == "run-1"
    assert review_base.document_id == "document-1"
    assert review_base.version == 1
    assert review_base.text == "first draft"
    assert review_base.digest == snapshot.digest
    assert review_base.source == snapshot


def test_review_base_is_immutable() -> None:
    review_base = FrozenReviewBase.from_run(
        Run(
            run_id="run-1",
            request=make_request(),
        )
    )

    with pytest.raises(FrozenInstanceError):
        review_base.run_id = "run-2"  # type: ignore[misc]

    with pytest.raises(FrozenInstanceError):
        review_base.source.text = "changed"  # type: ignore[misc]


def test_later_document_version_does_not_change_review_base(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs.sqlite3"
    original = make_snapshot(
        version=1,
        text="first draft",
    )
    later = make_snapshot(
        version=2,
        text="first draft with later edits",
    )

    first_store = SQLiteRunStore(
        database,
        run_id_factory=lambda: "run-1",
    )
    receipt = first_store.admit(make_request(original))
    first_store.close()

    second_store = SQLiteRunStore(database)

    try:
        current_document = later
        reader = ReviewBaseReader(second_store)
        frozen = reader.read(receipt.run_id)

        assert current_document.version == 2
        assert current_document.text == "first draft with later edits"

        assert frozen.version == 1
        assert frozen.text == "first draft"
        assert frozen.digest == original.digest
        assert frozen.source != current_document
    finally:
        second_store.close()


def test_review_base_survives_run_reconnect() -> None:
    original = make_snapshot(
        version=1,
        text="review this exact source",
    )

    with SQLiteRunStore(
        ":memory:",
        run_id_factory=lambda: "run-1",
    ) as store:
        receipt = store.admit(make_request(original))
        reader = ReviewBaseReader(store)

        before_reconnect = reader.read(receipt.run_id)
        after_reconnect = reader.read(receipt.run_id)

        assert before_reconnect == after_reconnect
        assert after_reconnect.source == original


def test_review_base_round_trips_through_dictionaries() -> None:
    original = make_snapshot()
    run = Run(
        run_id="run-1",
        request=make_request(original),
    )

    review_base = FrozenReviewBase.from_run(run)
    restored = FrozenReviewBase.from_dict(review_base.to_dict())

    assert restored == review_base
    assert restored.source == original


def test_review_base_rejects_invalid_run_id() -> None:
    with pytest.raises(DomainError, match="run_id"):
        FrozenReviewBase(
            run_id="",
            source=make_snapshot(),
        )


def test_review_base_rejects_invalid_source() -> None:
    with pytest.raises(DomainError, match="source"):
        FrozenReviewBase(
            run_id="run-1",
            source="not-a-source",  # type: ignore[arg-type]
        )


def test_review_base_reader_requires_a_store() -> None:
    with pytest.raises(TypeError, match="SQLiteRunStore"):
        ReviewBaseReader(object())  # type: ignore[arg-type]
