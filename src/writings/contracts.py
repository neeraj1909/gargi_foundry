from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "MAX_UINT64",
    "ContractError",
    "OptionalRevision",
    "ReviewRequestContract",
    "TextTarget",
]

MAX_UINT64: Final = (1 << 64) - 1


class ContractError(ValueError):
    """Raised when a shared contract value violates its wire contract."""


def _require_string(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{name} must be a string")

    if not value.strip():
        raise ContractError(f"{name} must not be empty")

    return value


def _require_uint64(value: Any, *, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_UINT64
    ):
        raise ContractError(f"{name} must be an integer between 0 and {MAX_UINT64}")

    return value


def _require_mapping(value: Any, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")

    return value


def _require_keys(
    value: Mapping[str, Any],
    *,
    required: frozenset[str],
    name: str,
) -> None:
    missing = sorted(key for key in required if key not in value)

    if missing:
        raise ContractError(f"{name} is missing fields: {', '.join(missing)}")


def _reject_unknown_keys(
    value: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    name: str,
) -> None:
    unknown = sorted(key for key in value if key not in allowed)

    if unknown:
        raise ContractError(f"{name} contains unknown fields: {', '.join(unknown)}")


def _parse_wire_uint64(value: Any, *, name: str) -> int:
    if not isinstance(value, str):
        raise ContractError(f"{name} must be a decimal string on the wire")

    if not value:
        raise ContractError(f"{name} must not be empty")

    if any(character < "0" or character > "9" for character in value):
        raise ContractError(f"{name} must contain only decimal digits")

    parsed = int(value)

    return _require_uint64(parsed, name=name)


@dataclass(frozen=True, slots=True)
class OptionalRevision:
    """An optional uint64 revision with explicit presence semantics."""

    present: bool
    value: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.present, bool):
            raise ContractError("present must be a boolean")

        if not self.present:
            if self.value != 0:
                raise ContractError("an absent revision must use the canonical value 0")
            return

        _require_uint64(self.value, name="revision")

    @classmethod
    def absent(cls) -> OptionalRevision:
        """Represent an omitted expected_revision field."""

        return cls(present=False, value=0)

    @classmethod
    def supplied(cls, value: int) -> OptionalRevision:
        """Represent an explicitly supplied expected_revision field."""

        return cls(
            present=True,
            value=_require_uint64(value, name="revision"),
        )

    @property
    def uses_current_draft(self) -> bool:
        return not self.present

    @property
    def binds_saved_revision(self) -> bool:
        return self.present

    def to_wire_dict(self) -> dict[str, str]:
        """Return the protobuf-JSON representation of this field."""

        if not self.present:
            return {}

        return {"expectedRevision": str(self.value)}

    @classmethod
    def from_wire_value(cls, value: Any) -> OptionalRevision:
        """Parse a protobuf-JSON uint64 value."""

        return cls.supplied(
            _parse_wire_uint64(
                value,
                name="expectedRevision",
            )
        )


@dataclass(frozen=True, slots=True)
class TextTarget:
    """A document target with an optional expected revision."""

    document_id: str
    expected_revision: OptionalRevision

    def __post_init__(self) -> None:
        _require_string(self.document_id, name="document_id")

        if not isinstance(self.expected_revision, OptionalRevision):
            raise ContractError("expected_revision must be an OptionalRevision")

    def to_wire_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "documentId": self.document_id,
        }
        payload.update(self.expected_revision.to_wire_dict())
        return payload

    @classmethod
    def from_wire_dict(cls, data: Mapping[str, Any]) -> TextTarget:
        target = _require_mapping(data, name="target")

        _reject_unknown_keys(
            target,
            allowed=frozenset({"documentId", "expectedRevision"}),
            name="target",
        )
        _require_keys(
            target,
            required=frozenset({"documentId"}),
            name="target",
        )

        if "expectedRevision" in target:
            expected_revision = OptionalRevision.from_wire_value(
                target["expectedRevision"]
            )
        else:
            expected_revision = OptionalRevision.absent()

        return cls(
            document_id=_require_string(
                target["documentId"],
                name="documentId",
            ),
            expected_revision=expected_revision,
        )


@dataclass(frozen=True, slots=True)
class ReviewRequestContract:
    """The shared review request contract."""

    request_id: str
    owner_id: str
    target: TextTarget
    intent: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _require_string(self.request_id, name="request_id")
        _require_string(self.owner_id, name="owner_id")
        _require_string(self.intent, name="intent")
        _require_string(self.idempotency_key, name="idempotency_key")

        if not isinstance(self.target, TextTarget):
            raise ContractError("target must be a TextTarget")

    def to_wire_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "ownerId": self.owner_id,
            "target": self.target.to_wire_dict(),
            "intent": self.intent,
            "idempotencyKey": self.idempotency_key,
        }

    def to_json(self) -> str:
        """Serialize deterministically using protobuf-JSON conventions."""

        return json.dumps(
            self.to_wire_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_wire_dict(
        cls,
        data: Mapping[str, Any],
    ) -> ReviewRequestContract:
        payload = _require_mapping(data, name="request")

        _reject_unknown_keys(
            payload,
            allowed=frozenset(
                {
                    "requestId",
                    "ownerId",
                    "target",
                    "intent",
                    "idempotencyKey",
                }
            ),
            name="request",
        )
        _require_keys(
            payload,
            required=frozenset(
                {
                    "requestId",
                    "ownerId",
                    "target",
                    "intent",
                    "idempotencyKey",
                }
            ),
            name="request",
        )

        return cls(
            request_id=_require_string(
                payload["requestId"],
                name="requestId",
            ),
            owner_id=_require_string(
                payload["ownerId"],
                name="ownerId",
            ),
            target=TextTarget.from_wire_dict(payload["target"]),
            intent=_require_string(
                payload["intent"],
                name="intent",
            ),
            idempotency_key=_require_string(
                payload["idempotencyKey"],
                name="idempotencyKey",
            ),
        )

    @classmethod
    def from_json(cls, payload: str) -> ReviewRequestContract:
        if not isinstance(payload, str):
            raise ContractError("JSON payload must be a string")

        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ContractError("payload is not valid JSON") from exc

        return cls.from_wire_dict(decoded)
