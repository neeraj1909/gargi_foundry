from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Any, Self

from .domain import DomainError, SourceSnapshot, Suggestion

__all__ = [
    "MAX_OPERATION_REPLACEMENT_LENGTH",
    "DocumentNotFound",
    "OperationConflict",
    "OperationError",
    "OperationEvent",
    "OperationReceipt",
    "OperationRejected",
    "OwnedDocument",
    "RewriteOperation",
    "SQLiteOperationStore",
    "operation_payload_digest",
]

MAX_OPERATION_REPLACEMENT_LENGTH = 20_000
MAX_IDENTIFIER_LENGTH = 256
DIGEST_LENGTH = 64


class OperationError(DomainError):
    """Base error for bounded text operations."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class OperationRejected(OperationError):
    """Raised when an operation fails an acceptance predicate."""


class OperationConflict(OperationError):
    """Raised when an operation identity is reused with new payload."""


class DocumentNotFound(LookupError):
    """Raised when a requested document does not exist."""

    def __init__(self, document_id: str) -> None:
        self.document_id = document_id
        super().__init__(f"document not found: {document_id}")


def _require_identifier(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise OperationError(f"{name}_type", f"{name} must be a string")

    if not value.strip():
        raise OperationError(f"{name}_empty", f"{name} must not be empty")

    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise OperationError(
            f"{name}_too_long",
            f"{name} exceeds {MAX_IDENTIFIER_LENGTH} characters",
        )

    return value


def _require_uuid(value: Any, *, name: str) -> str:
    value = _require_identifier(value, name=name)

    try:
        uuid.UUID(value)
    except (AttributeError, ValueError, TypeError) as exc:
        raise OperationError(
            f"{name}_invalid",
            f"{name} must be a UUID",
        ) from exc

    return value


def _require_positive_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OperationError(
            f"{name}_invalid",
            f"{name} must be a positive integer",
        )

    return value


def _require_non_negative_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OperationError(
            f"{name}_invalid",
            f"{name} must be a non-negative integer",
        )

    return value


def _require_text(
    value: Any,
    *,
    name: str,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise OperationError(f"{name}_type", f"{name} must be a string")

    if not allow_empty and not value.strip():
        raise OperationError(f"{name}_empty", f"{name} must not be empty")

    if len(value) > maximum:
        raise OperationError(
            f"{name}_too_long",
            f"{name} exceeds {maximum} characters",
        )

    return value


def _require_digest(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise OperationError(f"{name}_type", f"{name} must be a string")

    if len(value) != DIGEST_LENGTH:
        raise OperationError(
            f"{name}_invalid",
            f"{name} must be a SHA-256 hex digest",
        )

    if any(character not in "0123456789abcdef" for character in value):
        raise OperationError(
            f"{name}_invalid",
            f"{name} must be a lowercase SHA-256 hex digest",
        )

    return value


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_object(value: str) -> dict[str, Any]:
    decoded = json.loads(value)

    if not isinstance(decoded, dict):
        raise OperationError(
            "store_corrupt",
            "stored JSON value must be an object",
        )

    return decoded


def _spans_overlap(
    first_start: int,
    first_end: int,
    second_start: int,
    second_end: int,
) -> bool:
    return first_start < second_end and second_start < first_end


@dataclass(frozen=True, slots=True)
class RewriteOperation:
    """One explicit, source-bound text mutation proposal."""

    operation_id: str
    owner_id: str
    document_id: str
    expected_version: int
    expected_digest: str
    start: int
    end: int
    quote: str
    replacement: str

    def __post_init__(self) -> None:
        _require_uuid(self.operation_id, name="operation_id")
        _require_identifier(self.owner_id, name="owner_id")
        _require_identifier(self.document_id, name="document_id")
        _require_positive_int(
            self.expected_version,
            name="expected_version",
        )
        _require_digest(self.expected_digest, name="expected_digest")
        _require_non_negative_int(self.start, name="start")
        _require_non_negative_int(self.end, name="end")

        if self.start >= self.end:
            raise OperationError(
                "invalid_span",
                "operation span must be non-empty and half-open",
            )

        _require_text(
            self.quote,
            name="quote",
            maximum=MAX_OPERATION_REPLACEMENT_LENGTH,
        )
        _require_text(
            self.replacement,
            name="replacement",
            maximum=MAX_OPERATION_REPLACEMENT_LENGTH,
            allow_empty=True,
        )

    @classmethod
    def from_suggestion(
        cls,
        *,
        operation_id: str,
        owner_id: str,
        suggestion: Suggestion,
    ) -> RewriteOperation:
        if not isinstance(suggestion, Suggestion):
            raise TypeError("suggestion must be a Suggestion")

        if suggestion.replacement is None:
            raise OperationRejected(
                "missing_replacement",
                "a rewrite operation requires a replacement",
            )

        return cls(
            operation_id=operation_id,
            owner_id=owner_id,
            document_id=suggestion.document_id,
            expected_version=suggestion.source_version,
            expected_digest=suggestion.source_digest,
            start=suggestion.start,
            end=suggestion.end,
            quote=suggestion.quote,
            replacement=suggestion.replacement,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "owner_id": self.owner_id,
            "document_id": self.document_id,
            "expected_version": self.expected_version,
            "expected_digest": self.expected_digest,
            "start": self.start,
            "end": self.end,
            "quote": self.quote,
            "replacement": self.replacement,
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> RewriteOperation:
        return cls(
            operation_id=data["operation_id"],
            owner_id=data["owner_id"],
            document_id=data["document_id"],
            expected_version=data["expected_version"],
            expected_digest=data["expected_digest"],
            start=data["start"],
            end=data["end"],
            quote=data["quote"],
            replacement=data["replacement"],
        )


def operation_payload_digest(operation: RewriteOperation) -> str:
    """Return the stable digest used for idempotent operation replay."""

    payload = _canonical_json(operation.to_dict()).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class OperationReceipt:
    """Durable result of one committed text operation."""

    operation_id: str
    owner_id: str
    document_id: str
    source_version: int
    resulting_version: int
    source_digest: str
    resulting_digest: str
    payload_digest: str
    event_sequence: int
    applied: bool
    replayed: bool = False

    def __post_init__(self) -> None:
        _require_uuid(self.operation_id, name="operation_id")
        _require_identifier(self.owner_id, name="owner_id")
        _require_identifier(self.document_id, name="document_id")
        _require_positive_int(self.source_version, name="source_version")
        _require_positive_int(
            self.resulting_version,
            name="resulting_version",
        )
        _require_digest(self.source_digest, name="source_digest")
        _require_digest(self.resulting_digest, name="resulting_digest")
        _require_digest(self.payload_digest, name="payload_digest")
        _require_positive_int(self.event_sequence, name="event_sequence")

        if not isinstance(self.applied, bool):
            raise OperationError("applied_invalid", "applied must be a boolean")

        if not isinstance(self.replayed, bool):
            raise OperationError(
                "replayed_invalid",
                "replayed must be a boolean",
            )

        if self.applied and self.resulting_version <= self.source_version:
            raise OperationError(
                "resulting_version_invalid",
                "an applied operation must advance the document version",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "owner_id": self.owner_id,
            "document_id": self.document_id,
            "source_version": self.source_version,
            "resulting_version": self.resulting_version,
            "source_digest": self.source_digest,
            "resulting_digest": self.resulting_digest,
            "payload_digest": self.payload_digest,
            "event_sequence": self.event_sequence,
            "applied": self.applied,
            "replayed": self.replayed,
        }

    def to_persisted_dict(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload["replayed"] = False
        return payload

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> OperationReceipt:
        return cls(
            operation_id=data["operation_id"],
            owner_id=data["owner_id"],
            document_id=data["document_id"],
            source_version=data["source_version"],
            resulting_version=data["resulting_version"],
            source_digest=data["source_digest"],
            resulting_digest=data["resulting_digest"],
            payload_digest=data["payload_digest"],
            event_sequence=data["event_sequence"],
            applied=data["applied"],
            replayed=data.get("replayed", False),
        )


@dataclass(frozen=True, slots=True)
class OperationEvent:
    """Durable metadata describing one committed operation."""

    document_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class OwnedDocument:
    """A document snapshot owned by one author."""

    owner_id: str
    snapshot: SourceSnapshot

    def __post_init__(self) -> None:
        _require_identifier(self.owner_id, name="owner_id")

        if not isinstance(self.snapshot, SourceSnapshot):
            raise OperationError(
                "snapshot_invalid",
                "snapshot must be a SourceSnapshot",
            )


class SQLiteOperationStore:
    """SQLite boundary for atomic, idempotent text operations."""

    def __init__(
        self,
        database: str | Path = ":memory:",
    ) -> None:
        self._connection = sqlite3.connect(
            str(database),
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._initialize_schema()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _initialize_schema(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    digest TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS operation_events (
                    document_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(document_id, sequence),
                    FOREIGN KEY(document_id) REFERENCES documents(document_id)
                );

                CREATE TABLE IF NOT EXISTS operation_receipts (
                    operation_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(document_id)
                );
                """
            )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")

            try:
                yield
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def register_document(
        self,
        *,
        owner_id: str,
        snapshot: SourceSnapshot,
    ) -> None:
        """Register one owned source snapshot as the current document."""

        _require_identifier(owner_id, name="owner_id")

        if not isinstance(snapshot, SourceSnapshot):
            raise TypeError("snapshot must be a SourceSnapshot")

        with self._transaction():
            row = self._connection.execute(
                """
                SELECT document_id, owner_id, version, text, digest
                FROM documents
                WHERE document_id = ?
                """,
                (snapshot.document_id,),
            ).fetchone()

            if row is not None:
                existing = self._row_to_document(row)

                if existing.owner_id != owner_id or existing.snapshot != snapshot:
                    raise OperationConflict(
                        "document_identity_reused",
                        "document identity was registered with different data",
                    )

                return

            self._connection.execute(
                """
                INSERT INTO documents (
                    document_id,
                    owner_id,
                    version,
                    text,
                    digest
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    snapshot.document_id,
                    owner_id,
                    snapshot.version,
                    snapshot.text,
                    snapshot.digest,
                ),
            )

    def get_document(self, document_id: str) -> OwnedDocument:
        _require_identifier(document_id, name="document_id")

        with self._lock:
            row = self._connection.execute(
                """
                SELECT document_id, owner_id, version, text, digest
                FROM documents
                WHERE document_id = ?
                """,
                (document_id,),
            ).fetchone()

        if row is None:
            raise DocumentNotFound(document_id)

        return self._row_to_document(row)

    def events(
        self,
        document_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[OperationEvent, ...]:
        _require_identifier(document_id, name="document_id")
        _require_non_negative_int(after_sequence, name="after_sequence")

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT document_id, sequence, event_type, payload_json
                FROM operation_events
                WHERE document_id = ?
                  AND sequence > ?
                ORDER BY sequence ASC
                """,
                (document_id, after_sequence),
            ).fetchall()

        return tuple(
            OperationEvent(
                document_id=row["document_id"],
                sequence=int(row["sequence"]),
                event_type=row["event_type"],
                payload=_json_object(row["payload_json"]),
            )
            for row in rows
        )

    def apply(self, operation: RewriteOperation) -> OperationReceipt:
        """Validate and atomically apply one bounded rewrite."""

        if not isinstance(operation, RewriteOperation):
            raise TypeError("operation must be a RewriteOperation")

        payload_json = _canonical_json(operation.to_dict())
        payload_digest = operation_payload_digest(operation)

        with self._transaction():
            existing = self._connection.execute(
                """
                SELECT payload_json, payload_digest, receipt_json
                FROM operation_receipts
                WHERE operation_id = ?
                """,
                (operation.operation_id,),
            ).fetchone()

            if existing is not None:
                if (
                    existing["payload_json"] != payload_json
                    or existing["payload_digest"] != payload_digest
                ):
                    raise OperationConflict(
                        "operation_id_reused",
                        "operation UUID was reused with a different payload",
                    )

                receipt = OperationReceipt.from_dict(
                    _json_object(existing["receipt_json"])
                )
                return replace(receipt, replayed=True)

            row = self._connection.execute(
                """
                SELECT document_id, owner_id, version, text, digest
                FROM documents
                WHERE document_id = ?
                """,
                (operation.document_id,),
            ).fetchone()

            if row is None:
                raise OperationRejected(
                    "target_not_found",
                    "the target document does not exist",
                )

            current = self._row_to_document(row)

            if current.owner_id != operation.owner_id:
                raise OperationRejected(
                    "wrong_owner",
                    "the operation owner does not own the target document",
                )

            self._reject_overlapping_operation(operation)

            if current.snapshot.version != operation.expected_version:
                raise OperationRejected(
                    "stale_revision",
                    "the target revision is no longer current",
                )

            if current.snapshot.digest != operation.expected_digest:
                raise OperationRejected(
                    "stale_digest",
                    "the target digest is no longer current",
                )

            try:
                actual_quote = current.snapshot.text_span(
                    operation.start,
                    operation.end,
                )
            except DomainError as exc:
                raise OperationRejected(
                    "invalid_span",
                    str(exc),
                ) from exc

            if actual_quote != operation.quote:
                raise OperationRejected(
                    "changed_quote",
                    "the current target text differs from the captured quote",
                )

            if operation.replacement == operation.quote:
                raise OperationRejected(
                    "no_change",
                    "the replacement would not change the document",
                )

            updated_snapshot = SourceSnapshot.create(
                document_id=current.snapshot.document_id,
                version=current.snapshot.version + 1,
                text=(
                    current.snapshot.text[: operation.start]
                    + operation.replacement
                    + current.snapshot.text[operation.end :]
                ),
            )

            event_sequence = self._next_event_sequence(
                operation.document_id,
            )

            self._connection.execute(
                """
                UPDATE documents
                SET version = ?, text = ?, digest = ?
                WHERE document_id = ?
                """,
                (
                    updated_snapshot.version,
                    updated_snapshot.text,
                    updated_snapshot.digest,
                    updated_snapshot.document_id,
                ),
            )

            event_payload = {
                "operation_id": operation.operation_id,
                "owner_id": operation.owner_id,
                "expected_version": operation.expected_version,
                "resulting_version": updated_snapshot.version,
                "start": operation.start,
                "end": operation.end,
                "payload_digest": payload_digest,
            }

            self._connection.execute(
                """
                INSERT INTO operation_events (
                    document_id,
                    sequence,
                    event_type,
                    payload_json
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    operation.document_id,
                    event_sequence,
                    "rewrite.applied",
                    _canonical_json(event_payload),
                ),
            )

            receipt = OperationReceipt(
                operation_id=operation.operation_id,
                owner_id=operation.owner_id,
                document_id=operation.document_id,
                source_version=operation.expected_version,
                resulting_version=updated_snapshot.version,
                source_digest=operation.expected_digest,
                resulting_digest=updated_snapshot.digest,
                payload_digest=payload_digest,
                event_sequence=event_sequence,
                applied=True,
            )

            self._connection.execute(
                """
                INSERT INTO operation_receipts (
                    operation_id,
                    document_id,
                    payload_json,
                    payload_digest,
                    receipt_json
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    operation.operation_id,
                    operation.document_id,
                    payload_json,
                    payload_digest,
                    _canonical_json(receipt.to_persisted_dict()),
                ),
            )

            return receipt

    def _reject_overlapping_operation(
        self,
        operation: RewriteOperation,
    ) -> None:
        rows = self._connection.execute(
            """
            SELECT payload_json
            FROM operation_events
            WHERE document_id = ?
              AND event_type = ?
            """,
            (
                operation.document_id,
                "rewrite.applied",
            ),
        ).fetchall()

        for row in rows:
            payload = _json_object(row["payload_json"])

            if payload["expected_version"] != operation.expected_version:
                continue

            if _spans_overlap(
                int(payload["start"]),
                int(payload["end"]),
                operation.start,
                operation.end,
            ):
                raise OperationRejected(
                    "overlapping_operation",
                    "another operation already changed an overlapping span",
                )

    def _next_event_sequence(self, document_id: str) -> int:
        row = self._connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
            FROM operation_events
            WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()

        return int(row["next_sequence"])

    @staticmethod
    def _row_to_document(row: sqlite3.Row) -> OwnedDocument:
        return OwnedDocument(
            owner_id=row["owner_id"],
            snapshot=SourceSnapshot(
                document_id=row["document_id"],
                version=int(row["version"]),
                text=row["text"],
                digest=row["digest"],
            ),
        )
