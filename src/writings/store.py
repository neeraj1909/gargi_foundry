from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Any, Self

from .admission import (
    AdmissionConflict,
    AdmissionReceipt,
    canonical_request_json,
    request_payload_digest,
)
from .domain import (
    DomainError,
    ReviewRequest,
    ReviewResult,
    Run,
    RunState,
)


class RunNotFound(LookupError):
    """Raised when a requested run does not exist"""


class StoreInvariantError(RuntimeError):
    """Raised when persisted data violates the application contract"""


@dataclass(frozen=True, slots=True)
class RunEvent:
    """A retained event that can be replayed after reconnect."""

    run_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "payload": self.payload,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class Notification:
    """A durable worker notification created during admission."""

    notification_id: int
    run_id: str
    kind: str
    payload: dict[str, Any]
    created_at: str
    claimed_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "payload": self.payload,
            "created_at": self.created_at,
            "claimed_at": self.claimed_at,
        }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _new_run_id() -> str:
    return f"run-{uuid.uuid4().hex}"


def _json_text(value: Mapping[str, Any]) -> str:
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
        raise StoreInvariantError("stored JSON value must be an object")

    return decoded


class SQLiteRunStore:
    """SQLite-backed admission, run, notification, and event store."""

    def __init__(
        self,
        database: str | Path = ":memory:",
        *,
        run_id_factory: Callable[[], str] = _new_run_id,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self._connection = sqlite3.connect(
            str(database),
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._run_id_factory = run_id_factory
        self._clock = clock
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
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    run_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS
                    requests_owner_idempotency_idx
                ON requests(owner_id, idempotency_key);

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    error_code TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(request_id) REFERENCES requests(request_id)
                );

                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, sequence),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    claimed_at TEXT,
                    UNIQUE(run_id, kind),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
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

    def admit(self, request: ReviewRequest) -> AdmissionReceipt:
        """Atomically create or replay a request admission."""

        if not isinstance(request, ReviewRequest):
            raise TypeError("request must be a ReviewRequest")

        request_json = canonical_request_json(request)
        payload_digest = request_payload_digest(request)

        with self._transaction():
            existing_rows = self._connection.execute(
                """
                SELECT
                    request_id,
                    owner_id,
                    idempotency_key,
                    payload_digest,
                    request_json,
                    run_id
                FROM requests
                WHERE request_id = ?
                   OR (
                        owner_id = ?
                        AND idempotency_key = ?
                   )
                """,
                (
                    request.request_id,
                    request.owner_id,
                    request.idempotency_key,
                ),
            ).fetchall()

            if existing_rows:
                run_ids = {row["run_id"] for row in existing_rows}

                if len(run_ids) != 1:
                    raise AdmissionConflict(
                        "admission identity matches multiple stored runs"
                    )

                existing = existing_rows[0]

                if (
                    existing["payload_digest"] != payload_digest
                    or existing["request_json"] != request_json
                ):
                    raise AdmissionConflict(
                        "request identity was reused for different work"
                    )

                run_row = self._fetch_run_row(existing["run_id"])

                if run_row is None:
                    raise StoreInvariantError(
                        "request exists without its corresponding run"
                    )

                return AdmissionReceipt(
                    request_id=existing["request_id"],
                    run_id=existing["run_id"],
                    idempotency_key=existing["idempotency_key"],
                    payload_digest=existing["payload_digest"],
                    state=run_row["state"],
                    replayed=True,
                )

            run_id = self._run_id_factory()
            run = Run(
                run_id=run_id,
                request=request,
                state=RunState.ADMITTED,
            )
            now = self._clock()

            self._connection.execute(
                """
                INSERT INTO requests (
                    request_id,
                    owner_id,
                    idempotency_key,
                    payload_digest,
                    request_json,
                    run_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.owner_id,
                    request.idempotency_key,
                    payload_digest,
                    request_json,
                    run_id,
                    now,
                ),
            )

            self._connection.execute(
                """
                INSERT INTO runs (
                    run_id,
                    request_id,
                    state,
                    attempt,
                    error_code,
                    result_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    request.request_id,
                    run.state.value,
                    run.attempt,
                    run.error_code,
                    None,
                    now,
                    now,
                ),
            )

            self._insert_event(
                run_id=run.run_id,
                event_type="run.admitted",
                payload={
                    "request_id": request.request_id,
                    "owner_id": request.owner_id,
                    "payload_digest": payload_digest,
                },
            )

            self._connection.execute(
                """
                INSERT INTO notifications (
                    run_id,
                    kind,
                    payload_json,
                    created_at,
                    claimed_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    "run.admitted",
                    _json_text(
                        {
                            "request_id": request.request_id,
                            "run_id": run.run_id,
                            "payload_digest": payload_digest,
                        }
                    ),
                    now,
                    None,
                ),
            )

            return AdmissionReceipt(
                request_id=request.request_id,
                run_id=run.run_id,
                idempotency_key=request.idempotency_key,
                payload_digest=payload_digest,
                state=run.state.value,
                replayed=False,
            )

    def get_run(self, run_id: str) -> Run:
        with self._lock:
            row = self._fetch_run_row(run_id)

            if row is None:
                raise RunNotFound(run_id)

            return self._row_to_run(row)

    def pending_notifications(self) -> tuple[Notification, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT
                    notification_id,
                    run_id,
                    kind,
                    payload_json,
                    created_at,
                    claimed_at
                FROM notifications
                WHERE claimed_at IS NULL
                ORDER BY notification_id
                """
            ).fetchall()

            return tuple(
                Notification(
                    notification_id=row["notification_id"],
                    run_id=row["run_id"],
                    kind=row["kind"],
                    payload=_json_object(row["payload_json"]),
                    created_at=row["created_at"],
                    claimed_at=row["claimed_at"],
                )
                for row in rows
            )

    def claim_next(self) -> Run | None:
        """Claim one admitted notification for worker execution."""

        with self._transaction():
            row = self._connection.execute(
                """
                SELECT
                    n.notification_id,
                    r.run_id,
                    r.request_id,
                    r.state,
                    r.attempt,
                    r.error_code,
                    r.result_json,
                    r.created_at,
                    r.updated_at,
                    q.request_json
                FROM notifications AS n
                INNER JOIN runs AS r
                    ON r.run_id = n.run_id
                INNER JOIN requests AS q
                    ON q.request_id = r.request_id
                WHERE n.claimed_at IS NULL
                  AND r.state IN (?, ?)
                ORDER BY n.notification_id
                LIMIT 1
                """,
                (
                    RunState.ADMITTED.value,
                    RunState.QUEUED.value,
                ),
            ).fetchone()

            if row is None:
                return None

            current = self._row_to_run(row)

            if current.state is RunState.ADMITTED:
                queued = current.transition(RunState.QUEUED)
                self._insert_event(
                    run_id=current.run_id,
                    event_type="run.queued",
                    payload={
                        "request_id": current.request.request_id,
                        "run_id": current.run_id,
                    },
                )
            else:
                queued = current

            running = queued.transition(RunState.RUNNING).with_attempt()
            now = self._clock()

            updated_notification = self._connection.execute(
                """
                UPDATE notifications
                SET claimed_at = ?
                WHERE notification_id = ?
                  AND claimed_at IS NULL
                """,
                (now, row["notification_id"]),
            )

            if updated_notification.rowcount != 1:
                raise StoreInvariantError(
                    "worker notification was claimed by another worker"
                )

            self._connection.execute(
                """
                UPDATE runs
                SET state = ?,
                    attempt = ?,
                    error_code = ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (
                    running.state.value,
                    running.attempt,
                    running.error_code,
                    now,
                    running.run_id,
                ),
            )

            self._insert_event(
                run_id=running.run_id,
                event_type="run.running",
                payload={
                    "request_id": running.request.request_id,
                    "run_id": running.run_id,
                    "attempt": running.attempt,
                },
            )

            return running

    def mark_succeeded(
        self,
        run_id: str,
        result: ReviewResult,
    ) -> Run:
        """Persist a successful provider result and retained event."""

        if not isinstance(result, ReviewResult):
            raise TypeError("result must be a ReviewResult")

        if result.run_id != run_id:
            raise DomainError("review result belongs to a different run")

        with self._transaction():
            row = self._fetch_run_row(run_id)

            if row is None:
                raise RunNotFound(run_id)

            current = self._row_to_run(row)
            succeeded = current.transition(RunState.SUCCEEDED)
            now = self._clock()

            self._connection.execute(
                """
                UPDATE runs
                SET state = ?,
                    attempt = ?,
                    error_code = ?,
                    result_json = ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (
                    succeeded.state.value,
                    succeeded.attempt,
                    succeeded.error_code,
                    _json_text(result.to_dict()),
                    now,
                    succeeded.run_id,
                ),
            )

            self._insert_event(
                run_id=run_id,
                event_type="run.succeeded",
                payload=result.to_dict(),
            )

            return succeeded

    def mark_failed(
        self,
        run_id: str,
        *,
        error_code: str,
    ) -> Run:
        """Persist a failed provider attempt."""

        with self._transaction():
            row = self._fetch_run_row(run_id)

            if row is None:
                raise RunNotFound(run_id)

            current = self._row_to_run(row)
            failed = current.transition(
                RunState.FAILED,
                error_code=error_code,
            )
            now = self._clock()

            self._connection.execute(
                """
                UPDATE runs
                SET state = ?,
                    attempt = ?,
                    error_code = ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (
                    failed.state.value,
                    failed.attempt,
                    failed.error_code,
                    now,
                    failed.run_id,
                ),
            )

            self._insert_event(
                run_id=run_id,
                event_type="run.failed",
                payload={
                    "run_id": run_id,
                    "error_code": error_code,
                },
            )

            return failed

    def get_result(self, run_id: str) -> ReviewResult | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT result_json
                FROM runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()

            if row is None:
                raise RunNotFound(run_id)

            if row["result_json"] is None:
                return None

            return ReviewResult.from_dict(_json_object(row["result_json"]))

    def events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[RunEvent, ...]:
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
        ):
            raise ValueError("after_sequence must be a non-negative integer")

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT
                    run_id,
                    sequence,
                    event_type,
                    payload_json,
                    created_at
                FROM run_events
                WHERE run_id = ?
                  AND sequence > ?
                ORDER BY sequence
                """,
                (run_id, after_sequence),
            ).fetchall()

            return tuple(
                RunEvent(
                    run_id=row["run_id"],
                    sequence=row["sequence"],
                    event_type=row["event_type"],
                    payload=_json_object(row["payload_json"]),
                    created_at=row["created_at"],
                )
                for row in rows
            )

    def _fetch_run_row(self, run_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT
                r.run_id,
                r.request_id,
                r.state,
                r.attempt,
                r.error_code,
                r.result_json,
                r.created_at,
                r.updated_at,
                q.request_json
            FROM runs AS r
            INNER JOIN requests AS q
                ON q.request_id = r.request_id
            WHERE r.run_id = ?
            """,
            (run_id,),
        ).fetchone()

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> Run:
        return Run(
            run_id=row["run_id"],
            request=ReviewRequest.from_dict(_json_object(row["request_json"])),
            state=RunState(row["state"]),
            attempt=row["attempt"],
            error_code=row["error_code"],
        )

    def _insert_event(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> RunEvent:
        row = self._connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
            FROM run_events
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

        sequence = int(row["next_sequence"])
        created_at = self._clock()
        payload_json = _json_text(payload)

        self._connection.execute(
            """
            INSERT INTO run_events (
                run_id,
                sequence,
                event_type,
                payload_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                sequence,
                event_type,
                payload_json,
                created_at,
            ),
        )

        return RunEvent(
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            payload=dict(payload),
            created_at=created_at,
        )
