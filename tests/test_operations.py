from __future__ import annotations

import importlib
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"

if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

domain = importlib.import_module("writings.domain")
operations = importlib.import_module("writings.operations")

DomainError = domain.DomainError
SourceSnapshot = domain.SourceSnapshot
Suggestion = domain.Suggestion

DocumentNotFound = operations.DocumentNotFound
OperationConflict = operations.OperationConflict
OperationError = operations.OperationError
OperationRejected = operations.OperationRejected
RewriteOperation = operations.RewriteOperation
SQLiteOperationStore = operations.SQLiteOperationStore


def operation_id(number: int) -> str:
    return f"00000000-0000-0000-0000-{number:012d}"


def make_snapshot(
    *,
    text: str = "bad title",
    version: int = 1,
) -> SourceSnapshot:
    return SourceSnapshot.create(
        document_id="document-1",
        version=version,
        text=text,
    )


def make_operation(
    snapshot: SourceSnapshot,
    *,
    number: int = 1,
    owner_id: str = "owner-1",
    start: int = 0,
    end: int = 3,
    quote: str | None = None,
    replacement: str = "good",
) -> RewriteOperation:
    return RewriteOperation(
        operation_id=operation_id(number),
        owner_id=owner_id,
        document_id=snapshot.document_id,
        expected_version=snapshot.version,
        expected_digest=snapshot.digest,
        start=start,
        end=end,
        quote=snapshot.text[start:end] if quote is None else quote,
        replacement=replacement,
    )


def register(
    store: SQLiteOperationStore,
    snapshot: SourceSnapshot | None = None,
) -> SourceSnapshot:
    snapshot = snapshot or make_snapshot()
    store.register_document(
        owner_id="owner-1",
        snapshot=snapshot,
    )
    return snapshot


def test_valid_operation_updates_document_and_records_receipt() -> None:
    with SQLiteOperationStore(":memory:") as store:
        snapshot = register(store)
        operation = make_operation(snapshot)

        receipt = store.apply(operation)

        assert receipt.applied is True
        assert receipt.replayed is False
        assert receipt.source_version == 1
        assert receipt.resulting_version == 2
        assert receipt.event_sequence == 1

        document = store.get_document("document-1")

        assert document.snapshot.text == "good title"
        assert document.snapshot.version == 2
        assert document.snapshot.digest == receipt.resulting_digest

        events = store.events("document-1")

        assert len(events) == 1
        assert events[0].event_type == "rewrite.applied"
        assert events[0].payload["operation_id"] == operation.operation_id


def test_operation_can_be_created_from_a_grounded_suggestion() -> None:
    snapshot = make_snapshot(text="bad title")
    suggestion = Suggestion(
        suggestion_id="suggestion-1",
        run_id="run-1",
        document_id=snapshot.document_id,
        source_version=snapshot.version,
        source_digest=snapshot.digest,
        start=0,
        end=3,
        quote="bad",
        category="wording",
        rationale="Use a more precise adjective.",
        replacement="good",
    )

    operation = RewriteOperation.from_suggestion(
        operation_id=operation_id(1),
        owner_id="owner-1",
        suggestion=suggestion,
    )

    assert operation.document_id == snapshot.document_id
    assert operation.expected_version == snapshot.version
    assert operation.quote == "bad"
    assert operation.replacement == "good"


def test_missing_target_is_rejected_before_mutation() -> None:
    with SQLiteOperationStore(":memory:") as store:
        operation = make_operation(make_snapshot())

        with pytest.raises(OperationRejected) as error:
            store.apply(operation)

        assert error.value.code == "target_not_found"

        with pytest.raises(DocumentNotFound):
            store.get_document("document-1")


def test_wrong_owner_is_rejected_before_mutation() -> None:
    with SQLiteOperationStore(":memory:") as store:
        snapshot = register(store)
        operation = make_operation(
            snapshot,
            owner_id="owner-2",
        )

        with pytest.raises(OperationRejected) as error:
            store.apply(operation)

        assert error.value.code == "wrong_owner"
        assert store.get_document("document-1").snapshot == snapshot


def test_stale_revision_is_rejected_before_mutation() -> None:
    with SQLiteOperationStore(":memory:") as store:
        snapshot = register(store)

        store.apply(make_operation(snapshot, number=1))

        stale = make_operation(
            snapshot,
            number=2,
            start=4,
            end=9,
            quote="title",
            replacement="heading",
        )

        with pytest.raises(OperationRejected) as error:
            store.apply(stale)

        assert error.value.code == "stale_revision"
        assert store.get_document("document-1").snapshot.text == "good title"


def test_changed_quote_is_rejected_before_mutation() -> None:
    with SQLiteOperationStore(":memory:") as store:
        snapshot = register(store)
        operation = make_operation(
            snapshot,
            quote="not the source",
        )

        with pytest.raises(OperationRejected) as error:
            store.apply(operation)

        assert error.value.code == "changed_quote"
        assert store.get_document("document-1").snapshot == snapshot


def test_same_operation_replays_the_original_receipt() -> None:
    with SQLiteOperationStore(":memory:") as store:
        snapshot = register(store)
        operation = make_operation(snapshot)

        first = store.apply(operation)
        replay = store.apply(operation)

        assert replay.replayed is True
        assert {**replay.to_dict(), "replayed": False} == first.to_dict()
        assert store.get_document("document-1").snapshot.version == 2
        assert len(store.events("document-1")) == 1


def test_reusing_operation_id_with_changed_payload_conflicts() -> None:
    with SQLiteOperationStore(":memory:") as store:
        snapshot = register(store)
        operation = make_operation(snapshot)

        store.apply(operation)
        changed = replace(operation, replacement="excellent")

        with pytest.raises(OperationConflict) as error:
            store.apply(changed)

        assert error.value.code == "operation_id_reused"
        assert store.get_document("document-1").snapshot.text == "good title"


def test_overlapping_operations_abstain() -> None:
    snapshot = make_snapshot(text="abcdef")

    with SQLiteOperationStore(":memory:") as store:
        register(store, snapshot)

        first = make_operation(
            snapshot,
            number=1,
            start=0,
            end=3,
            quote="abc",
            replacement="ABC",
        )
        second = make_operation(
            snapshot,
            number=2,
            start=2,
            end=5,
            quote="cde",
            replacement="CDE",
        )

        store.apply(first)

        with pytest.raises(OperationRejected) as error:
            store.apply(second)

        assert error.value.code == "overlapping_operation"
        assert store.get_document("document-1").snapshot.text == "ABCdef"


def test_no_change_replacement_is_rejected() -> None:
    with SQLiteOperationStore(":memory:") as store:
        snapshot = register(store)
        operation = make_operation(
            snapshot,
            replacement="bad",
        )

        with pytest.raises(OperationRejected) as error:
            store.apply(operation)

        assert error.value.code == "no_change"
        assert store.get_document("document-1").snapshot == snapshot


def test_over_limit_replacement_is_rejected() -> None:
    snapshot = make_snapshot()

    with pytest.raises(OperationError) as error:
        make_operation(
            snapshot,
            replacement="x" * 20_001,
        )

    assert error.value.code == "replacement_too_long"


def test_atomic_failure_rolls_back_document_event_and_receipt() -> None:
    with SQLiteOperationStore(":memory:") as store:
        snapshot = register(store)
        operation = make_operation(snapshot)

        store._connection.execute(
            """
            CREATE TRIGGER abort_operation_event
            BEFORE INSERT ON operation_events
            BEGIN
                SELECT RAISE(ABORT, 'event rejected');
            END;
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="event rejected"):
            store.apply(operation)

        assert store.get_document("document-1").snapshot == snapshot
        assert store.events("document-1") == ()

        receipt_count = store._connection.execute(
            "SELECT COUNT(*) FROM operation_receipts"
        ).fetchone()[0]

        assert receipt_count == 0
