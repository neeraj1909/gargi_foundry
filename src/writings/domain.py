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


@dataclass(frozen=True, slots=True)
class Run:
    """Immutable in-memory representation of a review run."""
    
    run_id: str
    request: ReviewRequest
    state: RunState = RunState.ADMITTED
    attempt: int = 0
    error_code: str | None = None 
    
    def __post_init__(self) -> None:
        _require_identifier(self.run_id, name="run_id")
        _require_non_negative_int(self.attempt, name="attempt")

        if not isinstance(self.state, RunState):
            raise DomainError("state must be a RunState")

        if self.error_code is not None:
            _require_text(
                self.error_code,
                name="error_code",
                maximum=MAX_ERROR_CODE_LENGTH,
            )
        
    @property
    def document_id(self) -> str:
        return self.request.document_id 
    
    @property
    def source_version(self) -> int:
        return self.request.source_version
    
    @property
    def source_digest(self) -> str:
        return self.request.source_digest 
    
    @property
    def is_terminal(self) -> bool:
        return self.state in {
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.UNKNOWN_EFFECT,
            RunState.REJECTED,
        }
        
    def transition(
        self,
        next_state: RunState,
        *,
        error_code: str | None = None,
    ) -> Run:
        """Return a new run after a valid state transition."""
        
        if not isinstance(next_state, RunState):
            raise DomainError("next_state must be a RunState")
        
        if next_state == self.state:
            return self
        
        allowed = _ALLOWED_TRANSITIONS[self.state]
        
        if next_state not in allowed:
            raise DomainError(
                f"invalid run transition: "
                f"{self.state.value} -> {next_state.value}"
            )
            
        if error_code is not None:
            _require_text(
                error_code,
                name="error_code",
                maximum=MAX_ERROR_CODE_LENGTH,
            )
            
        return replace(
            self,
            state=next_state,
            error_code=error_code,
        )
        
    def with_attempt(self) -> Run:
        """Return a new run with its provider-attempt count incremented."""
        
        return replace(self, self.attempt=self.attempt + 1)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "request": self.request.to_dict(),
            "state": self.state.value,
            "attempt": self.attempt,
            "error_code": self.error_code,
        }
        
    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Run:
        return cls(
            run_id=data["run_id"],
            request=ReviewRequest.from_dict(data["request"]),
            state=RunState(data["state"]),
            attempt=data["attempt"],
            error_code=data.get("error_code"),
        )


@dataclass(frozen=True, slots=True)
class Suggestion:
    """A typed, source-grounded editorial suggestion.

    Exact source grounding is checked later by guardrails.py because that
    boundary needs access to the authoritative SourceSnapshot and Run.
    """

    suggestion_id: str
    run_id: str
    document_id: str
    source_version: int
    source_digest: str
    start: int
    end: int
    quote: str
    category: str
    rationale: str
    replacement: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.suggestion_id, name="suggestion_id")
        _require_identifier(self.run_id, name="run_id")
        _require_identifier(self.document_id, name="document_id")

        _require_positive_int(
            self.source_version,
            name="source_version",
        )

        if not isinstance(self.source_digest, str):
            raise DomainError("source_digest must be a string")

        if len(self.source_digest) != 64:
            raise DomainError("source_digest must be a SHA-256 hex digest")

        _require_non_negative_int(self.start, name="start")
        _require_non_negative_int(self.end, name="end")

        if self.start >= self.end:
            raise DomainError("suggestion spans must be non-empty")

        _require_text(
            self.quote,
            name="quote",
            maximum=MAX_REPLACEMENT_LENGTH,
        )
        _require_text(
            self.category,
            name="category",
            maximum=MAX_CATEGORY_LENGTH,
        )
        _require_text(
            self.rationale,
            name="rationale",
            maximum=MAX_RATIONALE_LENGTH,
        )

        if self.replacement is not None:
            _require_text(
                self.replacement,
                name="replacement",
                maximum=MAX_REPLACEMENT_LENGTH,
                allow_empty=True,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "run_id": self.run_id,
            "document_id": self.document_id,
            "source_version": self.source_version,
            "source_digest": self.source_digest,
            "start": self.start,
            "end": self.end,
            "quote": self.quote,
            "category": self.category,
            "rationale": self.rationale,
            "replacement": self.replacement,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Suggestion:
        return cls(
            suggestion_id=data["suggestion_id"],
            run_id=data["run_id"],
            document_id=data["document_id"],
            source_version=data["source_version"],
            source_digest=data["source_digest"],
            start=data["start"],
            end=data["end"],
            quote=data["quote"],
            category=data["category"],
            rationale=data["rationale"],
            replacement=data.get("replacement"),
        )
        

@dataclass(frozen=True, slots=True)
class ReviewResult:
    """The typed result of one review operation."""

    run_id: str
    suggestions: tuple[Suggestion, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_identifier(self.run_id, name="run_id")

        if not isinstance(self.suggestions, tuple):
            object.__setattr__(
                self,
                "suggestions",
                tuple(self.suggestions),
            )

        if any(
            not isinstance(suggestion, Suggestion)
            for suggestion in self.suggestions
        ):
            raise DomainError(
                "suggestions must contain only Suggestion objects"
            )

    @property
    def has_changes(self) -> bool:
        return bool(self.suggestions)

    @property
    def is_no_change(self) -> bool:
        return not self.has_changes

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "suggestions": [
                suggestion.to_dict()
                for suggestion in self.suggestions
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewResult:
        return cls(
            run_id=data["run_id"],
            suggestions=tuple(
                Suggestion.from_dict(item)
                for item in data.get("suggestions", [])
            ),
        )


class DecisionKind(str, Enum):
    ACCEPT = "accept"
    DISMISS = "dismiss"


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """The result of an explicit author decision."""

    decision_id: str
    run_id: str
    suggestion_id: str
    owner_id: str
    document_id: str
    source_version: int
    source_digest: str
    kind: DecisionKind
    replacement: str | None
    applied: bool
    resulting_version: int

    def __post_init__(self) -> None:
        _require_identifier(self.decision_id, name="decision_id")
        _require_identifier(self.run_id, name="run_id")
        _require_identifier(self.suggestion_id, name="suggestion_id")
        _require_identifier(self.owner_id, name="owner_id")
        _require_identifier(self.document_id, name="document_id")

        _require_positive_int(
            self.source_version,
            name="source_version",
        )
        _require_non_negative_int(
            self.resulting_version,
            name="resulting_version",
        )

        if self.resulting_version < self.source_version:
            raise DomainError(
                "resulting_version cannot be older than source_version"
            )

        if not isinstance(self.source_digest, str):
            raise DomainError("source_digest must be a string")

        if not isinstance(self.kind, DecisionKind):
            raise DomainError("kind must be a DecisionKind")

        if not isinstance(self.applied, bool):
            raise DomainError("applied must be a boolean")

        if self.replacement is not None:
            _require_text(
                self.replacement,
                name="replacement",
                maximum=MAX_REPLACEMENT_LENGTH,
                allow_empty=True,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "run_id": self.run_id,
            "suggestion_id": self.suggestion_id,
            "owner_id": self.owner_id,
            "document_id": self.document_id,
            "source_version": self.source_version,
            "source_digest": self.source_digest,
            "kind": self.kind.value,
            "replacement": self.replacement,
            "applied": self.applied,
            "resulting_version": self.resulting_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DecisionResult:
        return cls(
            decision_id=data["decision_id"],
            run_id=data["run_id"],
            suggestion_id=data["suggestion_id"],
            owner_id=data["owner_id"],
            document_id=data["document_id"],
            source_version=data["source_version"],
            source_digest=data["source_digest"],
            kind=DecisionKind(data["kind"]),
            replacement=data.get("replacement"),
            applied=data["applied"],
            resulting_version=data["resulting_version"],
        )
