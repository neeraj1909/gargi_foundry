from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
from typing import Any, Mapping 


__all__ = [
    "DecisionKind",
    "DecisionResult",
    "DomainError",
    "ReviewRequest",
    "ReviewResult",
    "Run",
    "RunState",
    "SourceSnapshot",
    "Suggestion",
    "sha256_text",
]

MAX_ID_LENGTH = 256
MAX_INTENT_LENGTH = 4_000
MAX_CATEGORY_LENGTH = 64
MAX_RATIONALE_LENGTH = 2_000
MAX_REPLACEMENT_LENGTH = 20_000
MAX_ERROR_CODE_LENGTH = 128


class DomainError(ValueError):
    """Raised when a domain object violates a local invariant."""
    
    
def sha256_text(text: str) -> str:
    """Return the SHA-256 digest of exact UTF-8 text.
    The text is intentionally not normalized, stripped, or newline-converted.
    """
    
    if not isinstance(text, str):
        raise DomainError("source text must be a string")
    
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_text(
    value: str,
    *,
    name: str,
    maximum: int, 
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise DomainError(f"{name} must be a string")
    
    if not allow_empty and not value.strip():
        raise DomainError(f"{name} must not be empty")
    
    if len(value) > maximum:
        raise DomainError(f"{name} exceeds the maximum length of {maximum}")
    
    return value


def _require_identifier(value: str, * name: str) -> str:
    return _require_text(
        value,
        name=name,
        maximum=MAX_ID_LENGTH,
    )
    
    
def _require_positive_int(value: str, *, name: str)-> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DomainError(f"{name} must be a non-negative integer")
    
    return value 


def _require_non_negative_int(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DomainError(f"{name} must be a non-negative integer")

    return value


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """The exact source text used for one review."""
    
    document_id: str
    version: str
    text: str
    digest: str
    
    def __post_init__(self) -> None:
        _require_identifier(self.document_id, name="document_id")
        _require_positive_int(self.version, name="version")
        
        if not isinstance(self.text, str):
            raise DomainError("text must be a string")
        
        expected_digest = sha256_text(self.text)
        
        if self.digest != expected_digest:
            raise DomainError(
                "digest does not match the exact UTF-8 source text"
            )
            
    @classmethod
    def create(
        cls,
        *,
        document_id: str,
        version: int,
        text: str,
    ) -> SourceSnapshot:
        """Create a snapshot and calculate its digest."""
        
        return cls(
            document_id=document_id,
            version=version,
            text=text,
            digest=sha256_text(text),
        )
        
    def text_span(self, start: int, end: int) -> str:
        """Return a half-open span from the exact source text.
        
        Empty spans are allowed here as a general text utility.
        Suggestions apply stricter non-empty span validation.
        """
        
        _require_non_negative_int(start, name="start")
        _require_non_negative_int(end, name="end")
        
        if start > end:
            raise DomainError("start must not be greater than end")
        
        if end > len(self.text):
            raise DomainError("end is outside the source text")
        
        return self.text[start:end]
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "version": self.version,
            "text": self.text,
            "digest": self.digest,
        }
        
    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SourceSnapshot:
        return cls(
            document_id=data["document_id"],
            version=data["version"],
            text=data["text"],
            digest=data["digest"],
        )
        
        
@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """An author-owned request to review one immutable source snapshot."""
    
    request_id: str
    owner_id: str
    source: SourceSnapshot
    intent: str
    idempotency_key: str
    
    def __post_init__(self) -> None:
        _require_identifier(self.request_id, name="request_id")
        _require_identifier(self.owner_id, name="owner_id")
        _require_text(
            self.intent,
            name="intent",
            maximum=MAX_INTENT_LENGTH,
        )
        _require_identifier(
            self.idempotency_key,
            name="idempotency_key",
        )
        
    @property
    def document_id(self) -> str:
        return self.source.document_id
    
    @property
    def source_version(self) -> int:
        return self.source.version
    
    @property
    def source_digest(self) -> str:
        return self.source.digest 
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "owner_id": self.owner_id,
            "source": self.source.to_dict(),
            "intent": self.intent,
            "idempotency_key": self.idempotency_key,
        }
        
    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewRequest:
        return cls(
            request_id=data["request_id"],
            owner_id=data["owner_id"],
            source=SourceSnapshot.from_dict(data["source"]),
            intent=data["intent"],
            idempotency_key=data["idempotency_key"],
        )
        

class RunState(str, Enum):
    ADMITTED = "admitted"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN_EFFECT = "unknown_effect"
    REJECTED = "rejected"
    
    
_ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.ADMITTED: frozenset(
        {
            RunState.QUEUED,
            RunState.REJECTED,
        }
    ),
    RunState.QUEUED: frozenset({RunState.RUNNING}),
    RunState.RUNNING: frozenset(
        {
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.UNKNOWN_EFFECT,
        }
    ),
    RunState.SUCCEEDED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.UNKNOWN_EFFECT: frozenset(),
    RunState.REJECTED: frozenset(),
}
